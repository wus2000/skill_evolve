"""In-process JVM env pool for ScienceWorld (execution substrate "(e)").

ScienceWorld differs from ALFWorld/AppWorld: its engine is ALREADY an isolated
JVM subprocess, driven from Python by a thin ``py4j`` RPC handle, and the handle
is importable under the harness's own Python. So it needs neither a separate
worker interpreter (AppWorld) nor a not-thread-safe-engine subprocess (ALFWorld).
Verified isolated at N=8 (env_candidates/scienceworld_smoke/concurrency_probe.json).

This pool keeps N live ``ScienceWorldEnv`` (each its own JVM) and hands them to
rollout slots:
  * a bounded semaphore caps TOTAL live JVMs at ``size`` (the RAM-bound ceiling;
    holds across concurrent batch_rollout calls, like AppWorld's EngineSlotLimiter);
  * idle envs are reused across episodes (JVM boot ~0.7s is amortized);
  * a spawn gate smooths the batch-start creation storm;
  * an env is RECYCLED (closed; a fresh one replaces it lazily) after
    ``recycle_episodes`` episodes to bound slow RSS creep, or immediately when a
    rollout marks it broken (a py4j error or a watchdog force-close of a wedged JVM).

The engine import is LAZY (inside the factory) so this module — and the env
package — imports cleanly where ``scienceworld`` is not installed (tests inject a
fake factory).
"""
from __future__ import annotations

import os
import threading
from collections import deque
from typing import Callable, Optional


def _default_factory(env_step_limit: int):
    """Create one real ``ScienceWorldEnv`` (its own JVM). Import is lazy."""
    from scienceworld import ScienceWorldEnv

    return ScienceWorldEnv(taskName="", envStepLimit=env_step_limit)


class PooledEnv:
    """Handle to one pooled ``ScienceWorldEnv`` (its own JVM).

    ``force_close`` is the watchdog entry point: called from another thread when
    an episode exceeds its deadline, it shuts the gateway down so an in-flight,
    wedged ``step()`` unblocks with an exception (py4j has no read timeout).
    Marking ``broken`` ensures the env is discarded, not returned to the pool.
    """

    def __init__(self, env, pool: "ScienceWorldPool") -> None:
        self.env = env
        self._pool = pool
        self.episodes = 0
        self.broken = False
        self._closed = False
        self._lock = threading.Lock()

    def force_close(self) -> None:
        with self._lock:
            self.broken = True
            if self._closed:
                return
            self._closed = True
        self._safe_close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._safe_close()

    def _safe_close(self) -> None:
        try:
            self.env.close()
        except Exception:  # noqa: BLE001 — teardown is best-effort
            pass


class ScienceWorldPool:
    """Thread-safe pool of in-process ScienceWorld JVM envs (see module doc)."""

    def __init__(
        self,
        size: int,
        env_step_limit: int,
        *,
        recycle_episodes: int = 200,
        spawn_limit: int = 8,
        java_tool_options: str = "",
        factory: Optional[Callable[[int], object]] = None,
    ) -> None:
        self.size = max(1, int(size))
        self.env_step_limit = int(env_step_limit)
        self.recycle_episodes = max(1, int(recycle_episodes))
        self._factory = factory or _default_factory
        # -Xmx cap etc. are injected via the JVM's env var (py4j inherits it);
        # process-wide is fine — every ScienceWorld JVM wants the same cap.
        if java_tool_options:
            os.environ.setdefault("JAVA_TOOL_OPTIONS", java_tool_options)
        self._sem = threading.BoundedSemaphore(self.size)
        self._spawn = threading.BoundedSemaphore(max(1, int(spawn_limit)))
        self._idle: "deque[PooledEnv]" = deque()
        self._lock = threading.Lock()
        self._live = 0  # total JVMs alive (idle + checked out); diagnostics

    def acquire(self) -> PooledEnv:
        """Borrow an env (reuse an idle one, else spawn). Blocks at the ``size`` cap."""
        self._sem.acquire()
        try:
            with self._lock:
                if self._idle:
                    return self._idle.popleft()
            with self._spawn:  # smooth the batch-start spawn storm
                env = self._factory(self.env_step_limit)
            pe = PooledEnv(env, self)
            with self._lock:
                self._live += 1
            return pe
        except BaseException:
            # creation failed → don't leak the slot
            self._sem.release()
            raise

    def release(self, pe: PooledEnv, *, broken: bool = False) -> None:
        """Return an env. Discards (and lets a fresh one replace it) when broken
        or past the recycle threshold; otherwise parks it idle for reuse."""
        try:
            pe.episodes += 1
            if broken or pe.broken or pe.episodes >= self.recycle_episodes:
                self._discard(pe)
            else:
                with self._lock:
                    self._idle.append(pe)
        finally:
            self._sem.release()

    def _discard(self, pe: PooledEnv) -> None:
        pe.close()
        with self._lock:
            self._live = max(0, self._live - 1)

    def close_all(self) -> None:
        """Tear down every idle env (checked-out ones close on their own release)."""
        with self._lock:
            idle = list(self._idle)
            self._idle.clear()
        for pe in idle:
            pe.close()
        with self._lock:
            self._live = max(0, self._live - len(idle))

    @property
    def n_live(self) -> int:
        with self._lock:
            return self._live
