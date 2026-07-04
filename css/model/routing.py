"""Locality-aware replica routing with bounded loads.

PROBLEM. Multi-turn agent episodes want SESSION AFFINITY (every turn of an
episode re-sends the same growing prefix; landing on the same replica turns
that prefix into vLLM prefix-cache hits). But pure sticky hashing has no load
feedback: episode WEIGHTS are heavy-tailed (a 50-turn episode with 60s calls
occupies a replica ~3500 busy-seconds; a 6-turn one ~40s), and K-rollout
siblings share one key, so with few replicas the busy-time split drifts far
from uniform (measured live 2026-07-04: ~80/20 busy-seconds on 2 replicas
from two independent experiments — one endpoint saturated, one idle).

DESIGN (the textbook fix — consistent hashing with bounded loads, as used by
production LLM routers and Envoy's ring-hash + least-request hybrids):

  * **HRW (rendezvous) hashing** gives each session key a stable preference
    ORDER over replicas. Adding/removing a replica remaps only ~1/N of keys
    (extensibility: growing the fleet keeps most sessions cache-warm), with
    no ring construction and uniform spread for any N.
  * **Bounded loads**: the router tracks IN-FLIGHT requests per replica (a
    duration-weighted load signal by construction — a 60s call holds a slot
    60s). A session lands on its preferred replica only while that replica's
    in-flight count is under ``load_factor x fair-share``; otherwise it
    spills to the next replica in ITS OWN preference order (deterministic
    per key, so a spilled session tends to spill to the same secondary —
    locality degrades gracefully instead of randomly).
  * **Health cooldown**: connection-level failures mark a replica unhealthy
    for ``cooldown_s``; it is skipped while cooling and re-probed after.
    With every replica unhealthy the router degrades to least-loaded (the
    caller's retry loop owns the final failure).

Locality/balance trade-off knob: ``load_factor`` (1.0 = strict least-loaded
fair share, +inf = pure sticky). Default 1.25 caps any replica
at load_factor/N of total in-flight (62.5% on two replicas) while leaving
most sessions pinned.

Future extension (documented, not built): heterogeneous replica weights via
weighted rendezvous (score^(1/w)); plug in where the HRW score is computed.
"""
from __future__ import annotations

import hashlib
import logging
import math
import threading
import time

_log = logging.getLogger("css.routing")

_SNAPSHOT_EVERY = 500  # acquires between periodic INFO snapshots


class ReplicaRouter:
    """Thread-safe session router over N replica URLs (see module docstring)."""

    def __init__(
        self,
        urls: "list[str]",
        *,
        load_factor: float = 1.25,
        cooldown_s: float = 15.0,
    ) -> None:
        self.urls = [u for u in urls if u]
        if not self.urls:
            raise ValueError("ReplicaRouter needs at least one URL")
        self.load_factor = max(1.0, float(load_factor))
        self.cooldown_s = max(0.0, float(cooldown_s))
        n = len(self.urls)
        self._lock = threading.Lock()
        self._inflight = [0] * n
        self._served = [0] * n
        self._busy_s = [0.0] * n
        self._unhealthy_until = [0.0] * n
        self._acquires = 0

    # ── Key -> preference order (HRW / rendezvous) ─────────────────────────
    def hrw_order(self, session_key: str) -> "list[int]":
        """Replica indices, highest rendezvous score first (stable per key)."""
        scored = []
        for i, url in enumerate(self.urls):
            digest = hashlib.sha1(
                (session_key + "\x00" + url).encode("utf-8", errors="replace")
            ).digest()
            scored.append((int.from_bytes(digest[:8], "big"), i))
        scored.sort(reverse=True)
        return [i for _, i in scored]

    # ── Slot lifecycle ─────────────────────────────────────────────────────
    def acquire(self, session_key: str) -> int:
        """Pick a replica for this session and take an in-flight slot."""
        n = len(self.urls)
        if n == 1:
            with self._lock:
                self._inflight[0] += 1
                self._served[0] += 1
            return 0
        now = time.time()
        with self._lock:
            healthy = [i for i in range(n) if self._unhealthy_until[i] <= now]
            pool = healthy if healthy else list(range(n))
            pool_set = set(pool)
            # Fair share of the load INCLUDING this request, over the healthy
            # pool, stretched by load_factor.
            total = sum(self._inflight) + 1
            bound = max(1, math.ceil(total * self.load_factor / len(pool)))
            pick = -1
            for i in self.hrw_order(session_key):
                if i in pool_set and self._inflight[i] < bound:
                    pick = i
                    break
            if pick < 0:  # every candidate at bound -> least loaded wins
                pick = min(pool, key=lambda i: self._inflight[i])
            self._inflight[pick] += 1
            self._served[pick] += 1
            self._acquires += 1
            if self._acquires % _SNAPSHOT_EVERY == 0:
                _log.info(
                    "router: inflight=%s served=%s busy_s=%s",
                    list(self._inflight), list(self._served),
                    [round(b) for b in self._busy_s])
            return pick

    def release(self, index: int, busy_s: float = 0.0) -> None:
        with self._lock:
            if 0 <= index < len(self._inflight) and self._inflight[index] > 0:
                self._inflight[index] -= 1
            if 0 <= index < len(self._busy_s):
                self._busy_s[index] += max(0.0, busy_s)

    def mark_unhealthy(self, index: int) -> None:
        """Connection-level failure: skip this replica for ``cooldown_s``."""
        with self._lock:
            if 0 <= index < len(self._unhealthy_until):
                self._unhealthy_until[index] = time.time() + self.cooldown_s

    # ── Observability ──────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "urls": list(self.urls),
                "inflight": list(self._inflight),
                "served": list(self._served),
                "busy_s": [round(b, 1) for b in self._busy_s],
                "unhealthy_until": list(self._unhealthy_until),
            }
