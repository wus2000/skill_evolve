"""Locality-aware replica routing v3 — three signal sources, two-tier scheduling.

PURE INFRASTRUCTURE: this module exists only to speed experiments up on a
multi-replica (and soon heterogeneous) vLLM fleet. It is NOT part of the CSS
mechanism, plays no role in the paper's method, and the mechanism is
transport-agnostic; nothing here may leak into mechanism config or analysis.

Design (reviewed and approved 2026-07-04; see docs/infra_routing_v3.md):

SIGNALS (strictly separated roles)
  S  server truth   A daemon thread polls each node's vLLM ``/metrics``
                    (num_requests_running / num_requests_waiting — GLOBAL
                    across ALL client processes — plus prompt/generation
                    token counters whose deltas give the node's actual
                    token throughput). This is what coordinates several
                    独立 client processes without any IPC: they all read
                    the same server-side truth.
  L  local latency  Peak-EWMA request duration per node (spikes fast,
                    decays over time). HEALTH VETO ONLY — durations are
                    confounded by request type and cache hits, so they
                    never feed capacity estimates. Also the end-to-end
                    fallback when /metrics is unreachable.
  P  priors/caps    Registry annotations: ``w`` (rate prior, used until the
                    adaptive estimate warms up and as the S-dead fallback)
                    and ``max_inflight`` (hard per-node cap; the client-side
                    shadow of max_num_seqs — the weak node's tail fuse).

CAPACITY (adaptive, busy-gated)
  rate_i = EWMA of (Δprompt_tokens + Δgen_tokens)/Δt across polls, updated
  ONLY while the node is busy (running >= busy_threshold): an idle node's
  low throughput means "no work", not "no capacity". Cache hits inflate
  apparent throughput deliberately — that IS effective capacity, and the
  resulting positive feedback is capped by the load term below.

SCHEDULING (two tiers)
  cold (session-table miss: first turn of an episode, every single-shot
  optimizer call — i.e. exactly the requests with NO KV locality to lose):
      score_i = (load_i + 1) / (eff_w_i * slowstart_i)
      load_i  = running_i + waiting_i + dispatched_since_last_poll_i
      pick argmin among healthy, non-degraded, under-max_inflight nodes
      (constraints relax in stages if empty); ties break by HRW order.
      The winner becomes the session's HOME (LRU+TTL table).
  warm (table hit: later turns, K-rollout siblings):
      go home while home is healthy and under its personalized bound
      ceil(total_load * load_factor * eff_w_home); otherwise spill along
      the session's own HRW preference order (deterministic secondary).
      >= rehome_after_spills consecutive spills re-homes the session
      (persistent saturation/death), with a lock against ping-pong.

RELIABILITY
  Exponential-backoff cooldown on connection failures; slow-start ramp for
  new/recovered nodes; staged degradation (metrics dead -> local inflight +
  prior weight; table lost -> HRW; everything unhealthy -> least-loaded).
"""
from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
import urllib.request
from collections import OrderedDict
from typing import Any, Callable

_log = logging.getLogger("css.routing")

_SNAPSHOT_EVERY = 500          # acquires between periodic INFO snapshots

# ── Defaults (reviewed 2026-07-04; ReplicaRouter kwargs override for tests) ──
POLL_S = 3.0                   # /metrics poll period (plus one-shot phase jitter)
POLL_TIMEOUT_S = 1.5
STALE_POLLS = 10               # snapshot older than this many periods => S dead
BUSY_THRESHOLD = 8             # min running to accept a throughput sample
RATE_HALFLIFE_S = 60.0         # throughput EWMA half-life
RATE_MIN_SAMPLES = 5           # samples before the estimate replaces the prior
LATENCY_TAU_S = 30.0           # peak-EWMA decay time constant
LATENCY_ALPHA = 0.1
DEGRADED_FACTOR = 3.0          # ewma > factor x median => degraded
DEGRADED_MIN_SAMPLES = 10
SLOWSTART_S = 60.0             # linear ramp for new/recovered nodes
COOLDOWN_BASE_S = 15.0         # exponential backoff base ...
COOLDOWN_MAX_S = 120.0         # ... capped here
REHOME_AFTER_SPILLS = 5        # consecutive spills before a session re-homes
REHOME_LOCK_S = 60.0           # no second re-home within this window
BOUND_FLOOR = 4.0              # min warm bound: keeps K-sibling bursts and
                               # low-load regimes on home (bounds exist for
                               # macro balance, not to break trivial concurrency)
SESSION_TABLE_MAX = 200_000    # LRU entries (~20MB)
SESSION_TTL_S = 7200.0         # >> episode lifetime


def parse_metrics_text(text: str) -> "dict[str, float]":
    """Extract the four vLLM series we use from Prometheus text exposition.

    Sums across engine labels; returns {} for text with none of the series.
    """
    out: "dict[str, float]" = {}
    wanted = {
        "vllm:num_requests_running": "running",
        "vllm:num_requests_waiting": "waiting",
        "vllm:prompt_tokens_total": "prompt_total",
        "vllm:generation_tokens_total": "gen_total",
    }
    for line in (text or "").splitlines():
        if not line or line.startswith("#"):
            continue
        name = line.split("{", 1)[0].split(" ", 1)[0]
        key = wanted.get(name)
        if key is None:
            continue
        try:
            value = float(line.rsplit(" ", 1)[1])
        except (ValueError, IndexError):
            continue
        out[key] = out.get(key, 0.0) + value
    return out


def _default_metrics_fetch(metrics_url: str) -> str:
    with urllib.request.urlopen(metrics_url, timeout=POLL_TIMEOUT_S) as resp:
        return resp.read().decode("utf-8", errors="replace")


class _Node:
    """Per-replica state (all mutation under the router lock)."""

    __slots__ = (
        "url", "metrics_url", "w", "max_inflight",
        "running", "waiting", "snap_ts", "disp", "local_inflight",
        "prompt_total", "gen_total", "rate_ewma", "rate_n",
        "lat_ewma", "lat_ts", "lat_n",
        "unhealthy_until", "fail_streak", "up_since",
        "served", "busy_s",
    )

    def __init__(self, url: str, w: float, max_inflight: int, now: float) -> None:
        self.url = url
        root = url.split("/v1")[0] if "/v1" in url else url.rstrip("/")
        self.metrics_url = root + "/metrics"
        self.w = w
        self.max_inflight = max_inflight
        self.running = 0.0
        self.waiting = 0.0
        self.snap_ts = 0.0          # 0 = never polled (S dead)
        self.disp = 0               # dispatches since last snapshot
        self.local_inflight = 0
        self.prompt_total = -1.0    # <0 = no previous counter sample
        self.gen_total = -1.0
        self.rate_ewma = 0.0
        self.rate_n = 0
        self.lat_ewma = 0.0
        self.lat_ts = now
        self.lat_n = 0
        self.unhealthy_until = 0.0
        self.fail_streak = 0
        self.up_since = now
        self.served = 0
        self.busy_s = 0.0


class ReplicaRouter:
    """Thread-safe two-tier session router over N replicas (module docstring)."""

    def __init__(
        self,
        urls: "list[str]",
        *,
        weights: "list[float] | None" = None,
        max_inflights: "list[int] | None" = None,
        load_factor: float = 1.25,
        cooldown_s: float = COOLDOWN_BASE_S,
        poll_s: float = POLL_S,
        busy_threshold: int = BUSY_THRESHOLD,
        enable_poller: bool = True,
        metrics_fetch: "Callable[[str], str] | None" = None,
        now_fn: "Callable[[], float]" = time.time,
    ) -> None:
        if not urls:
            raise ValueError("ReplicaRouter needs at least one URL")
        self.load_factor = max(1.0, float(load_factor))
        self.cooldown_base = max(0.0, float(cooldown_s))
        self.poll_s = max(0.5, float(poll_s))
        self.busy_threshold = int(busy_threshold)
        self._now = now_fn
        self._fetch = metrics_fetch or _default_metrics_fetch
        now = self._now()
        self._nodes = [
            _Node(u,
                  (weights[i] if weights and i < len(weights) else 1.0),
                  (max_inflights[i] if max_inflights and i < len(max_inflights) else 0),
                  now)
            for i, u in enumerate(urls)
        ]
        self._lock = threading.Lock()
        # Session table: key_hash -> [home, last_used, spill_streak, rehome_lock_until]
        self._sessions: "OrderedDict[str, list]" = OrderedDict()
        self._acquires = 0
        self._counters = {"cold": 0, "warm": 0, "spill": 0, "rehome": 0, "veto": 0}
        self._poller_started = False
        self._enable_poller = enable_poller and len(self._nodes) > 1

    # ── HRW (tie-break, spill order, table-loss fallback) ──────────────────
    def hrw_order(self, session_key: str) -> "list[int]":
        scored = []
        for i, node in enumerate(self._nodes):
            digest = hashlib.sha1(
                (session_key + "\x00" + node.url).encode("utf-8", errors="replace")
            ).digest()
            scored.append((int.from_bytes(digest[:8], "big"), i))
        scored.sort(reverse=True)
        return [i for _, i in scored]

    # ── S source ingestion ──────────────────────────────────────────────────
    def ingest_snapshot(
        self, index: int, running: float, waiting: float,
        prompt_total: float, gen_total: float,
    ) -> None:
        """Apply one /metrics snapshot for node ``index`` (poller or tests)."""
        now = self._now()
        with self._lock:
            n = self._nodes[index]
            dt = now - n.snap_ts if n.snap_ts > 0 else 0.0
            if dt > 0 and n.prompt_total >= 0:
                # Busy-gated throughput sample (interval-average running).
                avg_running = (n.running + running) / 2.0
                if avg_running >= self.busy_threshold:
                    delta = max(0.0, (prompt_total - n.prompt_total)
                                + (gen_total - n.gen_total))
                    sample = delta / dt
                    alpha = 1.0 - 0.5 ** (dt / RATE_HALFLIFE_S)
                    if n.rate_n == 0:
                        n.rate_ewma = sample
                    else:
                        n.rate_ewma += alpha * (sample - n.rate_ewma)
                    n.rate_n += 1
            n.running = max(0.0, running)
            n.waiting = max(0.0, waiting)
            n.prompt_total = prompt_total
            n.gen_total = gen_total
            n.snap_ts = now
            n.disp = 0  # snapshot saw everything dispatched before it

    def _poll_loop(self) -> None:
        # One-shot phase jitter so several processes don't sample in lockstep.
        time.sleep((hash(id(self)) % 1000) / 1000.0)
        while True:
            for i, node in enumerate(self._nodes):
                try:
                    series = parse_metrics_text(self._fetch(node.metrics_url))
                    if series:
                        self.ingest_snapshot(
                            i,
                            series.get("running", 0.0),
                            series.get("waiting", 0.0),
                            series.get("prompt_total", 0.0),
                            series.get("gen_total", 0.0),
                        )
                except Exception:  # noqa: BLE001 — staleness handles gaps
                    pass
            time.sleep(self.poll_s)

    def _ensure_poller(self) -> None:
        if not self._enable_poller or self._poller_started:
            return
        self._poller_started = True
        threading.Thread(
            target=self._poll_loop, name="replica-router-poller", daemon=True
        ).start()

    # ── Derived quantities (call under lock) ────────────────────────────────
    def _s_fresh(self, n: _Node, now: float) -> bool:
        return n.snap_ts > 0 and (now - n.snap_ts) < STALE_POLLS * self.poll_s

    def _load(self, n: _Node, now: float) -> float:
        if self._s_fresh(n, now):
            return n.running + n.waiting + n.disp
        return float(n.local_inflight)  # S dead: fall back to what we can see

    def _eff_weights(self) -> "list[float]":
        rates = [n.rate_ewma if n.rate_n >= RATE_MIN_SAMPLES else None
                 for n in self._nodes]
        valid = [(r, self._nodes[i].w) for i, r in enumerate(rates) if r]
        if valid:
            # tok/s per unit of prior, from measured nodes; fill the rest by prior.
            per_w = sum(r for r, _ in valid) / max(1e-9, sum(w for _, w in valid))
            raw = [r if r else self._nodes[i].w * per_w
                   for i, r in enumerate(rates)]
        else:
            raw = [n.w for n in self._nodes]
        total = sum(raw) or 1.0
        return [x / total for x in raw]

    def _decayed_latency(self, n: _Node, now: float) -> float:
        if n.lat_n == 0:
            return 0.0
        return n.lat_ewma * math.exp(-(now - n.lat_ts) / LATENCY_TAU_S)

    def _degraded(self, now: float) -> "list[bool]":
        lats = [(self._decayed_latency(n, now), n.lat_n) for n in self._nodes]
        eligible = sorted(v for v, cnt in lats if cnt >= DEGRADED_MIN_SAMPLES)
        if len(eligible) < 2:
            return [False] * len(self._nodes)
        median = eligible[len(eligible) // 2]
        if median <= 0:
            return [False] * len(self._nodes)
        return [cnt >= DEGRADED_MIN_SAMPLES and v > DEGRADED_FACTOR * median
                for v, cnt in lats]

    def _slowstart(self, n: _Node, now: float) -> float:
        return min(1.0, max(0.05, (now - n.up_since) / SLOWSTART_S))

    def _healthy(self, n: _Node, now: float) -> bool:
        if now >= n.unhealthy_until:
            if n.unhealthy_until > n.up_since:
                n.up_since = n.unhealthy_until  # recovery moment: restart ramp
            return True
        return False

    def _session_get(self, key_hash: str, now: float) -> "list | None":
        entry = self._sessions.get(key_hash)
        if entry is None:
            return None
        if now - entry[1] > SESSION_TTL_S:
            del self._sessions[key_hash]
            return None
        self._sessions.move_to_end(key_hash)
        return entry

    def _session_put(self, key_hash: str, entry: "list") -> None:
        self._sessions[key_hash] = entry
        self._sessions.move_to_end(key_hash)
        while len(self._sessions) > SESSION_TABLE_MAX:
            self._sessions.popitem(last=False)

    # ── The decision ────────────────────────────────────────────────────────
    def acquire(self, session_key: str) -> int:
        """Pick a replica for this request and take a slot (release() after)."""
        self._ensure_poller()
        n_nodes = len(self._nodes)
        now = self._now()
        with self._lock:
            if n_nodes == 1:
                pick = 0
            else:
                pick = self._pick_locked(session_key, now)
            node = self._nodes[pick]
            node.local_inflight += 1
            node.disp += 1
            node.served += 1
            self._acquires += 1
            if self._acquires % _SNAPSHOT_EVERY == 0:
                _log.info(
                    "router: load=%s eff_w=%s served=%s busy_s=%s %s",
                    [round(self._load(n, now), 1) for n in self._nodes],
                    [round(w, 3) for w in self._eff_weights()],
                    [n.served for n in self._nodes],
                    [round(n.busy_s) for n in self._nodes],
                    dict(self._counters),
                )
            return pick

    def _pick_locked(self, session_key: str, now: float) -> int:
        key_hash = hashlib.sha1(
            session_key.encode("utf-8", errors="replace")).hexdigest()[:32]
        eff_w = self._eff_weights()
        degraded = self._degraded(now)
        loads = [self._load(n, now) for n in self._nodes]
        healthy = [self._healthy(n, now) for n in self._nodes]

        def under_cap(i: int) -> bool:
            cap = self._nodes[i].max_inflight
            return cap <= 0 or loads[i] < cap

        def score(i: int) -> float:
            return (loads[i] + 1.0) / (
                max(1e-9, eff_w[i]) * self._slowstart(self._nodes[i], now))

        def cold_pick() -> int:
            for stage in range(3):  # relax constraints in stages
                cand = [i for i in range(len(self._nodes))
                        if (healthy[i] or stage >= 2)
                        and (not degraded[i] or stage >= 1)
                        and under_cap(i)]
                if cand:
                    break
            else:
                cand = list(range(len(self._nodes)))
            if not cand:
                cand = list(range(len(self._nodes)))
            best = min(score(i) for i in cand)
            ties = [i for i in cand if score(i) <= best * (1 + 1e-9)]
            if len(ties) == 1:
                return ties[0]
            for i in self.hrw_order(session_key):  # deterministic tie-break
                if i in ties:
                    return i
            return ties[0]

        entry = self._session_get(key_hash, now)
        if entry is None:
            pick = cold_pick()
            self._counters["cold"] += 1
            if any(degraded):
                self._counters["veto"] += 1
            self._session_put(key_hash, [pick, now, 0, 0.0])
            return pick

        home = entry[0]
        total_load = sum(loads[i] for i in range(len(self._nodes)) if healthy[i])
        share = eff_w[home]
        bound = max(BOUND_FLOOR, math.ceil(total_load * self.load_factor * share))
        if degraded[home]:
            bound = max(BOUND_FLOOR / 2.0, bound / 2.0)  # accelerate drain, don't evict
        if healthy[home] and loads[home] < bound and under_cap(home):
            entry[1], entry[2] = now, 0
            self._counters["warm"] += 1
            return home

        # Spill along this session's own preference order (deterministic).
        spill_to = -1
        for i in self.hrw_order(session_key):
            if i != home and healthy[i] and under_cap(i):
                sh_bound = max(BOUND_FLOOR, math.ceil(
                    total_load * self.load_factor * eff_w[i]))
                if loads[i] < sh_bound:
                    spill_to = i
                    break
        if spill_to < 0:
            spill_to = cold_pick()
        entry[1] = now
        entry[2] += 1
        self._counters["spill"] += 1
        if entry[2] >= REHOME_AFTER_SPILLS and now >= entry[3]:
            entry[0], entry[2], entry[3] = spill_to, 0, now + REHOME_LOCK_S
            self._counters["rehome"] += 1
        return spill_to

    # ── Slot lifecycle ──────────────────────────────────────────────────────
    def release(self, index: int, busy_s: float = 0.0, ok: bool = True) -> None:
        now = self._now()
        with self._lock:
            n = self._nodes[index]
            if n.local_inflight > 0:
                n.local_inflight -= 1
            n.busy_s += max(0.0, busy_s)
            if ok:
                n.fail_streak = 0
                if busy_s > 0:  # peak-EWMA latency (health signal only)
                    decayed = self._decayed_latency(n, now)
                    blended = decayed * (1 - LATENCY_ALPHA) + busy_s * LATENCY_ALPHA
                    n.lat_ewma = max(blended, busy_s)
                    n.lat_ts = now
                    n.lat_n += 1

    def mark_unhealthy(self, index: int) -> None:
        """Connection-level failure: exponential-backoff cooldown."""
        now = self._now()
        with self._lock:
            n = self._nodes[index]
            n.fail_streak += 1
            cooldown = min(
                self.cooldown_base * (2 ** (n.fail_streak - 1)), COOLDOWN_MAX_S)
            n.unhealthy_until = now + cooldown

    # ── Observability ───────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        now = self._now()
        with self._lock:
            return {
                "urls": [n.url for n in self._nodes],
                "load": [round(self._load(n, now), 1) for n in self._nodes],
                "running": [n.running for n in self._nodes],
                "waiting": [n.waiting for n in self._nodes],
                "local_inflight": [n.local_inflight for n in self._nodes],
                "eff_w": [round(w, 4) for w in self._eff_weights()],
                "rate_ewma": [round(n.rate_ewma, 1) for n in self._nodes],
                "lat_ewma": [round(self._decayed_latency(n, now), 2)
                             for n in self._nodes],
                "served": [n.served for n in self._nodes],
                "busy_s": [round(n.busy_s, 1) for n in self._nodes],
                "sessions": len(self._sessions),
                "counters": dict(self._counters),
                "s_fresh": [self._s_fresh(n, now) for n in self._nodes],
            }
