"""Async browser pool for high-concurrency WebArena episodes.

One dedicated thread runs a single asyncio event loop that owns K async chromium
browsers. Each episode gets its own ``BrowserContext`` (isolated cookies/cache,
its own ``network.har``) on one of the K browsers; many episodes run
concurrently as coroutines on the loop. Blocking LLM calls are offloaded to a
thread pool so the loop never stalls.

Why: the sync-per-episode design (``sync_playwright()`` + ``browser.launch()``
per episode) is thread-affine — every one of the rollout threads needs its own
full browser (~2-4 GB), capping concurrency at ~24 on the harness host and
leaking process/FD/memory across a long run. Here concurrency = N contexts
(~100 MB each) over K browsers (~1 GB each), so 100+ episodes fit in a few tens
of GB and browsers are reused (no per-episode launch/teardown churn).

Contract used by env.run_one (sync, on a rollout thread):
    pool = BrowserPool.get(cfg)          # lazy singleton, started on first use
    fut  = pool.submit(lambda: run_episode_async(pool, ...))
    result = fut.result(timeout=task_timeout)   # concurrent.futures.Future

Contract used by run_episode_async (coroutine, on the loop):
    async with pool.acquire(storage_state=..., har_path=..., headers=...) as (ctx, page):
        ...                              # await page.goto / aexecute_action / aprocess
    reply = await pool.offload(client.complete_target_messages, msgs)   # blocking LLM
"""
from __future__ import annotations

import asyncio
import atexit
import functools
import logging
import threading
from contextlib import asynccontextmanager
from typing import Any, Callable

_log = logging.getLogger("css.webarena.pool")

VIEWPORT = {"width": 1280, "height": 720}


class BrowserPool:
    """K reused async chromium browsers on one event-loop thread, serving
    context-per-episode with an N-context concurrency cap."""

    _singleton: "BrowserPool | None" = None
    _singleton_lock = threading.Lock()

    def __init__(self, *, browser_procs: int, max_contexts: int,
                 nav_timeout_ms: int, har_content: str,
                 contexts_per_browser: int = 0):
        self._k = max(1, int(browser_procs))
        self._max_contexts = max(1, int(max_contexts))
        self._nav_timeout = int(nav_timeout_ms)
        self._har_content = har_content
        # optional recycle: rebuild a browser after this many contexts to bound
        # any residual per-browser growth over a very long run (0 = never).
        self._cpb = int(contexts_per_browser)

        self._loop: "asyncio.AbstractEventLoop | None" = None
        self._thread: "threading.Thread | None" = None
        self._pw: Any = None
        self._browsers: list[Any] = []
        self._served: list[int] = []          # contexts served per browser (recycle)
        self._rr = -1
        self._sema: "asyncio.Semaphore | None" = None
        self._pick_lock: "asyncio.Lock | None" = None
        self._exec: Any = None
        self._started = False

    # ── lifecycle ────────────────────────────────────────────────────────────
    @classmethod
    def get(cls, cfg: Any) -> "BrowserPool":
        """Process-wide singleton, configured from cfg.extra, started lazily."""
        with cls._singleton_lock:
            if cls._singleton is None:
                extra = getattr(cfg, "extra", {}) or {}
                # webarena_max_browsers kept as a back-compat alias for the
                # concurrency cap; browser_procs is the (small) process count.
                max_ctx = int(extra.get("webarena_max_contexts",
                                        extra.get("webarena_max_browsers", 64)))
                pool = cls(
                    browser_procs=int(extra.get("webarena_browser_procs", 8)),
                    max_contexts=max_ctx,
                    nav_timeout_ms=int(extra.get("webarena_nav_timeout_ms", 30000)),
                    har_content=str(extra.get("webarena_har_content", "omit")),
                    contexts_per_browser=int(
                        extra.get("webarena_contexts_per_browser", 0)))
                pool.start()
                cls._singleton = pool
                atexit.register(pool.shutdown)
            return cls._singleton

    def start(self) -> None:
        if self._started:
            return
        from concurrent.futures import ThreadPoolExecutor
        self._exec = ThreadPoolExecutor(
            max_workers=self._max_contexts,
            thread_name_prefix="wa-llm")
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, name="wa-browser-loop", daemon=True)
        self._thread.start()
        # block until playwright + K browsers are up (fail fast if not)
        asyncio.run_coroutine_threadsafe(self._init(), self._loop).result()
        self._started = True
        _log.info("BrowserPool up: %d browsers, max_contexts=%d",
                  len(self._browsers), self._max_contexts)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _init(self) -> None:
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        self._sema = asyncio.Semaphore(self._max_contexts)
        self._pick_lock = asyncio.Lock()
        for _ in range(self._k):
            self._browsers.append(await self._launch())
            self._served.append(0)

    async def _launch(self) -> Any:
        # channel="chromium" = full chromium in new-headless mode, matching the
        # sync path (avoids the chromium-headless-shell download that no-oped on
        # the harness host).
        return await self._pw.chromium.launch(headless=True, channel="chromium")

    # ── submission (called from rollout threads) ─────────────────────────────
    def submit(self, coro_factory: Callable[[], Any],
               timeout: "float | None" = None) -> Any:
        """Schedule an episode coroutine on the loop; returns a
        concurrent.futures.Future the caller blocks on. If timeout is set, the
        coroutine is wrapped in asyncio.wait_for so a hung episode is cancelled
        on the loop (its `async with acquire` finally then frees context+slot)."""
        if not self._started:
            self.start()

        async def _wrapped() -> Any:
            coro = coro_factory()
            if timeout:
                return await asyncio.wait_for(coro, timeout)
            return await coro

        return asyncio.run_coroutine_threadsafe(_wrapped(), self._loop)

    async def offload(self, fn: Callable, *args: Any, **kwargs: Any) -> Any:
        """Run a blocking callable (LLM call, gzip write) off the event loop."""
        return await self._loop.run_in_executor(
            self._exec, functools.partial(fn, *args, **kwargs))

    # ── per-episode context (called from the coroutine, on the loop) ──────────
    @asynccontextmanager
    async def acquire(self, *, storage_state: Any, har_path: str,
                      headers: "dict | None"):
        """Acquire an isolated context+page on a pooled browser. The semaphore
        caps concurrency; the context is always closed (flushing network.har)."""
        assert self._sema is not None
        async with self._sema:
            browser = await self._pick()
            ctx = await browser.new_context(
                viewport=VIEWPORT, device_scale_factor=1,
                storage_state=storage_state,
                record_har_path=har_path,
                record_har_content=self._har_content)
            ctx.set_default_timeout(self._nav_timeout)   # sync in async_api
            if headers:
                await ctx.set_extra_http_headers(headers)
            try:
                page = await ctx.new_page()
                yield ctx, page
            finally:
                try:
                    await ctx.close()   # flushes network.har — required for scoring
                except Exception as exc:  # noqa: BLE001
                    _log.warning("context close failed: %s", exc)

    async def _pick(self) -> Any:
        """Round-robin a live browser; relaunch a dead one in place; optionally
        recycle a browser that has served too many contexts."""
        async with self._pick_lock:
            n = len(self._browsers)
            for _ in range(n):
                self._rr = (self._rr + 1) % n
                b = self._browsers[self._rr]
                recycle = self._cpb and self._served[self._rr] >= self._cpb
                if b.is_connected() and not recycle:
                    self._served[self._rr] += 1
                    return b
                # dead or due for recycle → rebuild in place
                try:
                    await b.close()
                except Exception:  # noqa: BLE001
                    pass
                self._browsers[self._rr] = await self._launch()
                self._served[self._rr] = 1
                _log.info("BrowserPool: %s browser slot %d",
                          "recycled" if recycle else "relaunched dead", self._rr)
                return self._browsers[self._rr]
            # unreachable unless n==0
            b = await self._launch()
            self._browsers.append(b); self._served.append(1)
            return b

    # ── shutdown ─────────────────────────────────────────────────────────────
    def shutdown(self) -> None:
        if not self._started or self._loop is None:
            return
        self._started = False
        try:
            asyncio.run_coroutine_threadsafe(
                self._close_all(), self._loop).result(timeout=30)
        except Exception:  # noqa: BLE001
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=10)
        if self._exec:
            self._exec.shutdown(wait=False)
        _log.info("BrowserPool shut down")

    async def _close_all(self) -> None:
        for b in self._browsers:
            try:
                await b.close()
            except Exception:  # noqa: BLE001
                pass
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
