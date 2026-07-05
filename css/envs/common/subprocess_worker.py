"""Host-side subprocess-worker plumbing shared by task environments.

Environments whose engines cannot run inside the harness process — not
thread-safe (ALFWorld/tatsu), wrong interpreter (AppWorld needs py3.11 while
the harness runs py3.8), or process-global monkey-patching (AppWorld/freezegun
mocks wall-clock time process-wide) — run each episode in a dedicated worker
subprocess speaking a JSON-lines protocol: requests on stdin, one JSON event
per line on stdout.

Concurrency-safety rationale (the reasons this module looks the way it does):

  * ``subprocess.Popen`` is the fork+exec path — safe to call from many
    threads: CPython's ``_posixsubprocess`` keeps the fork->exec window
    async-signal-safe, and exec wipes inherited lock state. ``preexec_fn``
    (Python code inside that window) breaks the guarantee and is BANNED here;
    process-group creation uses ``start_new_session=True`` (C-implemented).
  * ``start_new_session=True`` + group signaling reaps GRANDCHILDREN too
    (engine subprocesses such as Fast Downward) — the failure mode behind the
    historical "un-killable grandchild wedged the whole batch" incident.
  * Spawns are rate-limited by a module semaphore: at a batch-wave start,
    hundreds of simultaneous interpreter launches (heavy imports) would
    stampede the CPU; a bounded gate smooths the herd without limiting
    steady-state concurrency.
  * Every read is ``select()``-bounded. A silent worker raises
    :class:`WorkerTimeout`; a dead one raises :class:`WorkerDied`. Harness
    threads must never block unboundedly (the batch deadline is the last
    resort, not the first).
  * The worker side complements this with stdin-EOF suicide and (Linux)
    PR_SET_PDEATHSIG — see css/envs/common/worker_runtime.py.

The engine-slot cap is a separate concern from LLM concurrency: an env whose
workers are RAM-heavy declares its own ceiling with :class:`EngineSlotLimiter`
(a semaphore acquired inside ``run_one``), which holds globally even when the
mechanism runs several batch_rollout calls concurrently (e.g. the paired gate
rolling both sides at once).
"""
from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import threading
import time
from typing import Any

CLOSE_SENTINEL = "__CLOSE__"  # must match css/envs/common/worker_runtime.py

DEFAULT_SPAWN_LIMIT = 12

_spawn_gate = threading.BoundedSemaphore(DEFAULT_SPAWN_LIMIT)
_spawn_gate_lock = threading.Lock()


def set_spawn_limit(n: int) -> None:
    """Reconfigure the global spawn gate (call at launcher init, not mid-batch)."""
    global _spawn_gate
    with _spawn_gate_lock:
        _spawn_gate = threading.BoundedSemaphore(max(1, int(n)))


def ensure_nofile_limit(min_soft: int = 8192) -> "tuple[int, int]":
    """Raise RLIMIT_NOFILE's soft limit toward ``min_soft`` (never lowers).

    N workers x 3 pipes + HTTP sockets exceed the common 1024 default and fail
    as EMFILE at the worst possible moment (a full batch wave). Best-effort:
    returns the resulting ``(soft, hard)`` and never raises.
    """
    try:
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = min(max(soft, min_soft), hard if hard > 0 else min_soft)
        if target > soft:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
            soft = target
        return soft, hard
    except Exception:  # noqa: BLE001 — advisory only
        return -1, -1


class WorkerError(RuntimeError):
    """Base class for worker-protocol failures."""


class WorkerTimeout(WorkerError, TimeoutError):
    """No protocol line arrived within the caller's deadline.

    Also a ``TimeoutError`` so pre-existing ``except TimeoutError`` clauses
    (the ALFWorld agent's episode-deadline handling) keep working unchanged.
    """


class WorkerDied(WorkerError):
    """The worker exited (or closed stdout) without a protocol line."""


class SubprocessWorkerHost:
    """One worker subprocess with bounded I/O and whole-group teardown.

    Thin and env-agnostic: envs own the protocol semantics (event names,
    request shapes); this class owns process lifecycle, bounded reads, and
    the kill state machine. See ALFWorld/AppWorld agents for usage.
    """

    def __init__(
        self,
        argv: "list[str]",
        *,
        env: "dict[str, str] | None" = None,
        stderr_path: str = "",
        name: str = "worker",
    ) -> None:
        self.name = name
        self._stderr_f = None
        if stderr_path:
            self._stderr_f = open(stderr_path, "ab")
            stderr_dst: Any = self._stderr_f
        else:
            stderr_dst = subprocess.DEVNULL
        with _spawn_gate:
            # fork+exec via Popen (thread-safe); NO preexec_fn, ever.
            self.proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr_dst,
                env=env,
                text=True,
                encoding="utf-8",
                bufsize=1,
                start_new_session=True,  # own process group: group-kill reaps grandchildren
                close_fds=True,
            )

    # ── Reads ────────────────────────────────────────────────────────────
    def read_event(self, timeout: float) -> dict:
        """Read one JSON protocol line with a hard deadline.

        Skips blank and non-JSON lines (stray engine noise that escaped the
        worker's fd redirection). Raises :class:`WorkerTimeout` on deadline,
        :class:`WorkerDied` if the worker exits or closes stdout first.
        """
        stdout = self.proc.stdout
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise WorkerTimeout(
                    "%s timed out after %.0fs" % (self.name, timeout))
            # poll(), NOT select(): select() is hard-limited to fd numbers
            # < FD_SETSIZE (1024) regardless of RLIMIT_NOFILE. Measured
            # failure 2026-07-06 (ALFWorld, 256 workers x pipes + two-endpoint
            # HTTP pools): 153/400 rollouts died with "filedescriptor out of
            # range in select()" once pipe fds crossed 1023, silently gutting
            # gate measurements. poll() has no such limit.
            poller = select.poll()
            poller.register(stdout, select.POLLIN | select.POLLHUP)
            events = poller.poll(min(remaining, 5.0) * 1000)
            if not events:
                if self.proc.poll() is not None:
                    raise WorkerDied(
                        "%s exited (code %s) without a protocol line"
                        % (self.name, self.proc.returncode))
                continue
            line = stdout.readline()
            if not line:
                raise WorkerDied(
                    "%s closed stdout (code %s)" % (self.name, self.proc.poll()))
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue  # stray non-protocol output; keep reading

    # ── Writes ───────────────────────────────────────────────────────────
    def send_line(self, line: str) -> None:
        """Write one request line (single-line contract enforced here)."""
        assert self.proc.stdin is not None
        self.proc.stdin.write(line.replace("\n", " ") + "\n")
        self.proc.stdin.flush()

    def send_json(self, obj: dict) -> None:
        """Write one JSON request line (newline-safe for multi-line payloads)."""
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    # ── Lifecycle ────────────────────────────────────────────────────────
    def _signal_group(self, sig: int) -> None:
        try:
            os.killpg(self.proc.pid, sig)  # pgid == pid (start_new_session)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                self.proc.send_signal(sig)
            except Exception:  # noqa: BLE001
                pass

    def close(self, grace: float = 5.0, sentinel: "str | None" = CLOSE_SENTINEL) -> None:
        """Graceful-then-forced teardown of the whole process group.

        sentinel -> wait(grace) -> killpg(TERM) -> wait -> killpg(KILL) -> wait.
        Idempotent; always reaps (no zombie table entries).
        """
        try:
            if self.proc.poll() is None and sentinel is not None \
                    and self.proc.stdin is not None:
                try:
                    self.proc.stdin.write(sentinel + "\n")
                    self.proc.stdin.flush()
                except Exception:  # noqa: BLE001 — pipe may already be gone
                    pass
                try:
                    self.proc.wait(timeout=grace)
                except subprocess.TimeoutExpired:
                    pass
            if self.proc.poll() is None:
                self._signal_group(signal.SIGTERM)
                try:
                    self.proc.wait(timeout=grace)
                except subprocess.TimeoutExpired:
                    pass
            if self.proc.poll() is None:
                self._signal_group(signal.SIGKILL)
            try:
                self.proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        finally:
            if self._stderr_f is not None:
                try:
                    self._stderr_f.close()
                except Exception:  # noqa: BLE001
                    pass
                self._stderr_f = None

    def kill(self) -> None:
        """Immediate group kill + reap (timeout/abandon path)."""
        self.close(grace=0.0, sentinel=None)


class EngineSlotLimiter:
    """Global engine-slot ceiling for one env instance.

    RAM-heavy workers (AppWorld ~300-500MB each) need a cap independent of the
    LLM thread count — and it must hold across CONCURRENT batch_rollout calls
    (the paired gate rolls both sides at once), so it is a semaphore acquired
    inside ``run_one``, not a batch-level ``min()``.
    """

    def __init__(self, n_slots: int) -> None:
        self.n_slots = max(1, int(n_slots))
        self._sem = threading.BoundedSemaphore(self.n_slots)

    def slot(self):
        """``with limiter.slot(): ...`` around the engine-holding section."""
        return self._Slot(self._sem)

    class _Slot:
        def __init__(self, sem: threading.BoundedSemaphore) -> None:
            self._sem = sem

        def __enter__(self):
            self._sem.acquire()
            return self

        def __exit__(self, *exc: Any) -> None:
            self._sem.release()
