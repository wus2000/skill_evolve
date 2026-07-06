"""Mutation-aware (stack, site) lease scheduler for the WebArena farm.

Concurrency contract (design in docs/env_prep/webarena_PREP.md §5):
- ``retrieve``/``navigate`` tasks share any stack freely (read-only pool).
- ``mutate`` tasks hold an EXCLUSIVE lease on one (stack, site) lane for the
  whole episode; on release the lane is marked dirty and refreshed (container
  recreate from the warmed golden snapshot) before the next mutating episode.
  Every rollout of the same task therefore starts from clean site state — the
  paired gate's K repeats never observe each other's mutations.

This module is deliberately transport-agnostic: the farm topology arrives as
plain dicts (``stacks``: name -> {site -> base_url}) and refreshing is a
callable hook so the single-stack P0 setup (refresh = no-op or blocking
docker-recreate via ssh) and the P2 multi-stack farm share one scheduler.
Thread-safe; designed for use from the rollout thread pool.
"""
from __future__ import annotations

import logging
import threading
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
    dirty: bool = False


class SiteLeaseManager:
    """Hands out per-episode leases over N site stacks.

    refresh_fn(stack_name, site) -> None restores one site container to its
    golden state; it runs BEFORE a mutating lease is granted on a dirty lane
    (lazy refresh: pay the cost only when the lane is actually needed again,
    so back-to-back read-only traffic never waits on container recreates).
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

    # -- read-only pool ----------------------------------------------------
    def _next_stack(self) -> str:
        names = sorted(self._stacks)
        with self._rr_lock:
            self._rr = (self._rr + 1) % len(names)
            return names[self._rr]

    # -- public API ---------------------------------------------------------
    def acquire(self, sites: "list[str]", task_type: str,
                timeout_s: float = 1800.0) -> Lease:
        """Block until a suitable stack is free; return the lease.

        Read-only episodes round-robin across stacks without locking.
        Mutating episodes lock EVERY site they touch on one stack (multisite
        mutate tasks pin all their sites together to keep cross-site state
        consistent), refreshing dirty lanes first.
        """
        wanted = [s for s in sites if s in next(iter(self._stacks.values()))]
        if task_type != MUTATING_TASK_TYPE:
            stack = self._next_stack()
            return Lease(stack=stack, urls=self._stacks[stack],
                         sites=(), exclusive=False)

        deadline = threading.Event()
        timer = threading.Timer(timeout_s, deadline.set)
        timer.daemon = True
        timer.start()
        try:
            while not deadline.is_set():
                for stack in sorted(self._stacks):
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
                        for site, lane in zip(wanted, lanes):
                            if lane.dirty:
                                try:
                                    self._refresh(stack, site)
                                except Exception:
                                    _log.exception(
                                        "webarena/scheduler — refresh failed "
                                        "(stack=%s site=%s); granting lease on "
                                        "possibly-dirty lane", stack, site)
                                lane.dirty = False
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
            lane.dirty = True   # mutation assumed; next mutating lease refreshes
            lane.lock.release()
