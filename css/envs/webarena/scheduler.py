"""Mutation-aware (stack, site) lease scheduler for the WebArena farm.

Concurrency contract (design in docs/env_prep/webarena_PREP.md §5):
- ``retrieve``/``navigate`` tasks share any stack freely (read-only pool) but
  AVOID stacks where a site they need is mid-refresh: a container recreate
  leaves the site unreachable for its whole boot window (minutes for gitlab),
  and an episode landing there would record a fake failure. Residual mutations
  from earlier tasks are tolerated by design — official WebArena runs the
  whole benchmark on one un-reset instance, so read-only-on-dirty matches
  upstream semantics; read-only-on-booting does not.
- ``mutate`` tasks hold an EXCLUSIVE lease on one (stack, site) lane for the
  whole episode (multisite mutate tasks pin all their sites together). On
  release the lane is marked dirty and refreshed EAGERLY in the background
  (container recreate from the golden image; refresh_fn blocks until the site
  serves again), so the boot window overlaps other work instead of stalling
  the next mutating lease. A dirty lane that escaped the eager pass (refresh
  failure, or a mutating acquire winning the lock race) is refreshed
  synchronously before the next mutating lease is granted. Every rollout of
  the same task therefore starts from clean site state — the paired gate's K
  repeats never observe each other's mutations.

This module is deliberately transport-agnostic: the farm topology arrives as
plain dicts (``stacks``: name -> {site -> base_url}) and refreshing is a
callable hook so the single-stack P0 setup (refresh = no-op or blocking
docker-recreate via ssh) and the P2 multi-stack farm share one scheduler.
Thread-safe; designed for use from the rollout thread pool.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

_log = logging.getLogger("css.webarena")

MUTATING_TASK_TYPE = "mutate"


@dataclass
class Lease:
    stack: str
    urls: dict            # site -> base_url for THIS stack
    sites: tuple          # sites this lease pinned (mutating) — () for read-only
    exclusive: bool


@dataclass
class _Lane:
    lock: threading.Lock = field(default_factory=threading.Lock)
    dirty: bool = False        # mutated since last refresh (guarded by _mu)
    refreshing: bool = False   # container recreate in flight (guarded by _mu)


class SiteLeaseManager:
    """Hands out per-episode leases over N site stacks.

    refresh_fn(stack_name, site) -> None restores one site container to its
    golden state and MUST block until the site actually serves again (the
    farm-side readiness gate) — the scheduler treats its return as "lane
    usable". It runs eagerly in a background thread right after a mutating
    release, with a synchronous fallback before granting a mutating lease on
    a lane that is still dirty.
    """

    def __init__(self, stacks: "dict[str, dict[str, str]]",
                 refresh_fn: "Callable[[str, str], None] | None" = None):
        if not stacks:
            raise ValueError("SiteLeaseManager needs at least one stack")
        self._stacks = dict(stacks)
        self._refresh = refresh_fn or (lambda stack, site: None)
        self._lanes = {(s, site): _Lane()
                       for s, urls in self._stacks.items() for site in urls}
        self._rr = 0
        self._rr_lock = threading.Lock()
        self._mu = threading.Lock()   # guards lane.dirty / lane.refreshing

    @staticmethod
    def _log_wait(t0: float, wanted: "list[str]", task_type: str) -> None:
        """Lane-starvation telemetry: waits over 30s mean the stack pool is
        the bottleneck (grounds for adding stacks), not a fault."""
        waited = time.monotonic() - t0
        if waited >= 30.0:
            _log.info("webarena/scheduler — lane wait %.0fs (sites=%s type=%s)",
                      waited, wanted, task_type)

    # -- refresh -------------------------------------------------------------
    def _refresh_locked(self, stack: str, site: str, lane: _Lane) -> None:
        """Refresh a lane if dirty. Caller MUST hold lane.lock; the
        ``refreshing`` flag steers read-only traffic away for the duration."""
        with self._mu:
            if not lane.dirty:
                return
            lane.refreshing = True
        try:
            self._refresh(stack, site)
            with self._mu:
                lane.dirty = False
        except Exception:
            _log.exception(
                "webarena/scheduler — refresh failed (stack=%s site=%s); "
                "lane stays dirty", stack, site)
        finally:
            with self._mu:
                lane.refreshing = False

    def _eager_refresh(self, stack: str, site: str) -> None:
        lane = self._lanes[(stack, site)]
        if not lane.lock.acquire(blocking=False):
            return  # a mutating acquire won the race; it refreshes synchronously
        try:
            self._refresh_locked(stack, site, lane)
        finally:
            lane.lock.release()

    # -- public API ---------------------------------------------------------
    def acquire(self, sites: "list[str]", task_type: str,
                timeout_s: float = 1800.0) -> Lease:
        """Block until a suitable stack is free; return the lease.

        Read-only episodes round-robin across stacks, skipping stacks where a
        needed site is mid-refresh. Mutating episodes lock EVERY site they
        touch on one stack, refreshing still-dirty lanes first.
        """
        wanted = [s for s in sites if s in next(iter(self._stacks.values()))]
        names = sorted(self._stacks)
        t0 = time.monotonic()

        if task_type != MUTATING_TASK_TYPE:
            deadline_ts = t0 + timeout_s
            while True:
                with self._rr_lock:
                    self._rr = (self._rr + 1) % len(names)
                    start = self._rr
                for i in range(len(names)):
                    stack = names[(start + i) % len(names)]
                    with self._mu:
                        busy = any(self._lanes[(stack, s)].refreshing
                                   for s in wanted)
                    if not busy:
                        self._log_wait(t0, wanted, task_type)
                        return Lease(stack=stack, urls=self._stacks[stack],
                                     sites=(), exclusive=False)
                if time.monotonic() >= deadline_ts:
                    break
                time.sleep(0.5)
            raise TimeoutError(
                f"all stacks mid-refresh for sites={wanted} within {timeout_s}s")

        deadline = threading.Event()
        timer = threading.Timer(timeout_s, deadline.set)
        timer.daemon = True
        timer.start()
        try:
            while not deadline.is_set():
                for stack in names:
                    lanes = [self._lanes[(stack, s)] for s in wanted]
                    acquired = []
                    ok = True
                    for lane in lanes:
                        if lane.lock.acquire(blocking=False):
                            acquired.append(lane)
                        else:
                            ok = False
                            break
                    if ok:
                        self._log_wait(t0, wanted, task_type)
                        for site, lane in zip(wanted, lanes):
                            self._refresh_locked(stack, site, lane)
                        return Lease(stack=stack, urls=self._stacks[stack],
                                     sites=tuple(wanted), exclusive=True)
                    for lane in acquired:
                        lane.lock.release()
                # brief backoff before rescanning stacks
                deadline.wait(0.5)
        finally:
            timer.cancel()
        raise TimeoutError(f"no free lane for sites={wanted} within {timeout_s}s")

    def release(self, lease: Lease) -> None:
        if not lease.exclusive:
            return
        for site in lease.sites:
            lane = self._lanes[(lease.stack, site)]
            with self._mu:
                lane.dirty = True   # mutation assumed
            lane.lock.release()
        for site in lease.sites:
            threading.Thread(
                target=self._eager_refresh, args=(lease.stack, site),
                daemon=True,
                name=f"wa-refresh-{lease.stack}-{site}").start()
