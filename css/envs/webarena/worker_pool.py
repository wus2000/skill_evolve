"""Multi-process browser workers — the multi-core scale-out for WebArena.

The single-loop BrowserPool (browser_loop.py) is GIL-bound: at C=50 the event
loop thread pegged ONE core (CDP-response deserialization + AXTree parse are
pure Python), process total 99.8% = 1 core, while chromium and 79 cores sat
idle. More event-loop *threads* can't help (one Python thread runs at a time).
So to use the box's cores we run M worker PROCESSES (M GILs → M cores), each
hosting the validated single-loop BrowserPool + run_episode_async with its own
LLM client. Total concurrency = M × contexts-per-worker; CPU = M loop cores.

The main process keeps the lease scheduler and the Verified scorer: run_one
acquires the lease, ships (item, skill, lease-view, workdir) to a worker over an
IPC queue, blocks on the result, then scores (the worker wrote HAR + response to
the shared filesystem) and releases the lease. Workers are spawned (NOT forked —
the main process is multi-threaded by the time the pool starts) and monitored:
a dead worker is restarted and its in-flight episodes fail cleanly.
"""
from __future__ import annotations

import itertools
import logging
import multiprocessing as _mp
import os
import queue as _queue
import sys
import threading
from typing import Any

_log = logging.getLogger("css.webarena.workers")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))


class _LeaseView:
    """Picklable stand-in for scheduler.Lease carrying only what an episode
    reads (stack/urls/sites). The real lease (acquire/release) stays in main."""
    __slots__ = ("stack", "urls", "sites")

    def __init__(self, stack: str, urls: dict, sites: list):
        self.stack, self.urls, self.sites = stack, urls, sites


# ── worker process ───────────────────────────────────────────────────────────
def _worker_main(cfg: Any, wid: int, req_q: Any, res_q: Any,
                 browser_procs: int, max_contexts: int) -> None:
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)
    logging.basicConfig(level=logging.INFO)
    try:
        from css.rollout.batch import _ensure_fd_limit
        _ensure_fd_limit()
    except Exception:  # noqa: BLE001
        pass
    from css.model.client import build_clients, TargetOnlyClient
    from css.envs.webarena.browser_loop import BrowserPool
    from css.envs.webarena.agent import run_episode_async

    client = build_clients(cfg)[0]     # target client only (own connection pool)
    # Tracing sinks are process-global and the main process's TracingLLMClient
    # wrap (tree_search.run_css_tree) never reaches this spawned process — so
    # without this block every target/scribe LLM call of a WebArena episode
    # vanished from the audit trail (llm_calls.jsonl stayed 0 bytes while the
    # thread-pool envs recorded 9 GB). All workers append to the run's single
    # trace/llm_calls pair; the writer's flock makes that process-safe.
    out_root = str(getattr(cfg, "out_root", "") or "")
    if out_root:
        try:
            from css.tracing import TracingLLMClient, init_trace
            init_trace(out_root)
            if isinstance(client, TargetOnlyClient):
                client._inner = TracingLLMClient(client._inner, role="target")
            else:  # defensive: build_clients contract is TargetOnlyClient
                client = TracingLLMClient(client, role="target")
        except Exception as exc:  # noqa: BLE001 — tracing must never kill a worker
            _log.warning("worker %d tracing init failed (untraced): %s", wid, exc)
    extra = getattr(cfg, "extra", {}) or {}
    pool = BrowserPool(
        browser_procs=browser_procs, max_contexts=max_contexts,
        nav_timeout_ms=int(extra.get("webarena_nav_timeout_ms", 30000)),
        har_content=str(extra.get("webarena_har_content", "omit")),
        contexts_per_browser=int(extra.get("webarena_contexts_per_browser", 0)))
    pool.start()

    # Send results off the loop thread: the loop's done-callback drops (rid,
    # result) here; a sender thread pickles + pushes to the IPC queue so
    # serialization never stalls the event loop.
    out: "_queue.Queue" = _queue.Queue()

    def _sender() -> None:
        while True:
            item = out.get()
            if item is None:
                return
            try:
                res_q.put(item)
            except Exception as exc:  # noqa: BLE001
                _log.error("worker %d res_q.put failed: %s", wid, exc)
    threading.Thread(target=_sender, name=f"wa-w{wid}-send", daemon=True).start()
    _log.info("worker %d up (browsers=%d contexts=%d)", wid, browser_procs,
              max_contexts)

    while True:
        msg = req_q.get()
        if msg is None:            # shutdown sentinel
            break
        rid, item, skill, lv, workdir, timeout = msg
        lease = _LeaseView(lv["stack"], lv["urls"], lv["sites"])

        def _done(fut: Any, rid: int = rid) -> None:
            try:
                out.put((rid, {"ok": True, "episode": fut.result()}))
            except Exception as exc:  # noqa: BLE001 — episode/timeout error
                out.put((rid, {"ok": False,
                               "error": f"{type(exc).__name__}: {exc}"[:300]}))

        try:
            f = pool.submit(
                lambda: run_episode_async(pool, item, skill, client, cfg,
                                          lease, workdir),
                timeout=timeout)
            f.add_done_callback(_done)
        except Exception as exc:  # noqa: BLE001 — submit itself failed
            out.put((rid, {"ok": False,
                           "error": f"submit: {type(exc).__name__}: {exc}"[:200]}))

    out.put(None)
    pool.shutdown()


# ── main-side dispatcher ──────────────────────────────────────────────────────
class MultiprocBrowserPool:
    _singleton: "MultiprocBrowserPool | None" = None
    _lock = threading.Lock()

    def __init__(self, cfg: Any):
        extra = getattr(cfg, "extra", {}) or {}
        self._cfg = cfg
        self._m = max(1, int(extra.get("webarena_worker_procs", 16)))
        self._bpw = max(1, int(extra.get("webarena_browser_procs", 1)))
        # webarena_max_contexts is the TOTAL episode concurrency; split evenly
        # across the M workers (ceil), so per-worker cap = total / M.
        _total = max(self._m, int(extra.get("webarena_max_contexts", 128)))
        self._cpw = -(-_total // self._m)
        self._ctx = _mp.get_context("spawn")   # never fork a threaded parent
        self._req: list = []                   # per-worker request queues
        self._procs: list = []
        self._res: Any = None                  # shared result queue
        self._pending: "dict[int, dict]" = {}  # rid -> {ev, worker, result}
        self._pending_lock = threading.Lock()
        self._ids = itertools.count(1)
        self._rr = itertools.count(0)
        self._started = False

    @classmethod
    def get(cls, cfg: Any) -> "MultiprocBrowserPool":
        with cls._lock:
            if cls._singleton is None:
                p = cls(cfg)
                p.start()
                cls._singleton = p
                import atexit
                atexit.register(p.shutdown)
            return cls._singleton

    def start(self) -> None:
        if self._started:
            return
        self._res = self._ctx.Queue()
        for wid in range(self._m):
            self._spawn(wid)
        threading.Thread(target=self._dispatch, name="wa-mp-dispatch",
                         daemon=True).start()
        threading.Thread(target=self._monitor, name="wa-mp-monitor",
                         daemon=True).start()
        self._started = True
        _log.info("MultiprocBrowserPool up: %d workers × %d contexts = %d "
                  "concurrent (%d browsers/worker)", self._m, self._cpw,
                  self._m * self._cpw, self._bpw)

    def _spawn(self, wid: int) -> None:
        q = self._ctx.Queue()
        p = self._ctx.Process(
            target=_worker_main, name=f"wa-worker-{wid}",
            args=(self._cfg, wid, q, self._res, self._bpw, self._cpw),
            daemon=True)
        p.start()
        if wid < len(self._procs):
            self._req[wid], self._procs[wid] = q, p
        else:
            self._req.append(q); self._procs.append(p)

    def _dispatch(self) -> None:
        while True:
            try:
                rid, payload = self._res.get()
            except Exception:  # noqa: BLE001 — queue closed on shutdown
                return
            with self._pending_lock:
                slot = self._pending.get(rid)
                if slot is not None:
                    slot["result"] = payload
                    slot["ev"].set()

    def _monitor(self) -> None:
        import time
        while self._started:
            time.sleep(5)
            for wid, p in enumerate(list(self._procs)):
                if p is not None and not p.is_alive():
                    _log.error("worker %d died (exit=%s) — failing its in-flight "
                               "episodes + restarting", wid, p.exitcode)
                    with self._pending_lock:
                        for rid, slot in list(self._pending.items()):
                            if slot["worker"] == wid and not slot["ev"].is_set():
                                slot["result"] = {"ok": False,
                                                  "error": "worker process died"}
                                slot["ev"].set()
                    try:
                        self._spawn(wid)
                    except Exception as exc:  # noqa: BLE001
                        _log.error("worker %d respawn failed: %s", wid, exc)

    def submit(self, item: dict, skill_text: str, lease_view: dict,
               workdir: str, timeout: int) -> dict:
        """Dispatch one episode to a worker; block until its result. Returns the
        episode dict {messages, n_turns, agent_response} or raises on error."""
        if not self._started:
            self.start()
        rid = next(self._ids)
        wid = next(self._rr) % self._m
        ev = threading.Event()
        with self._pending_lock:
            self._pending[rid] = {"ev": ev, "worker": wid, "result": None}
        self._req[wid].put((rid, item, skill_text, lease_view, workdir, timeout))
        got = ev.wait(timeout + 90)     # grace over the worker-side wait_for
        with self._pending_lock:
            slot = self._pending.pop(rid, None)
        if not got or slot is None or slot["result"] is None:
            raise TimeoutError(f"episode {rid} timed out on worker {wid}")
        payload = slot["result"]
        if not payload.get("ok"):
            raise RuntimeError(payload.get("error", "worker error"))
        return payload["episode"]

    def shutdown(self) -> None:
        if not self._started:
            return
        self._started = False
        for q in self._req:
            try:
                q.put(None)
            except Exception:  # noqa: BLE001
                pass
        for p in self._procs:
            try:
                p.join(timeout=8)
                if p.is_alive():
                    p.terminate()
            except Exception:  # noqa: BLE001
                pass
        _log.info("MultiprocBrowserPool shut down")
