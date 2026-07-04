"""Worker-side runtime helpers — stdlib-only, alien-interpreter safe.

Worker scripts run under the ENVIRONMENT's interpreter (e.g. a py3.10/3.11
conda env) where the ``css`` package may not be importable at all. Load this
module BY FILE PATH, never by package import::

    import importlib.util as _ilu
    _rt_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            os.pardir, "common", "worker_runtime.py")
    _spec = _ilu.spec_from_file_location("worker_runtime", _rt_path)
    runtime = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(runtime)

Everything here is best-effort hardening + protocol plumbing shared by all
subprocess workers (see css/envs/common/subprocess_worker.py for the host
side and the concurrency-safety rationale):

  * ``harden()``       — PR_SET_PDEATHSIG (Linux) so a worker whose parent
                         dies mid-engine-step is killed by the kernel even if
                         it never returns to the stdin read; optional address-
                         space rlimit turning box-level OOM into a clean
                         in-worker MemoryError.
  * ``protocol_channel()`` — dup the REAL stdout for protocol lines, then
                         point fd1 at fd2 so every stray write (Python, C
                         extensions, engine subprocesses) lands on stderr and
                         cannot corrupt the line protocol.
  * ``truncate_middle()`` — bound a single protocol message (huge engine
                         outputs would fill the 64K pipe buffer AND blow the
                         LLM context).
  * ``iter_stdin_lines()`` — the parent-death contract: yields request lines
                         until the CLOSE sentinel OR stdin EOF. EOF means the
                         parent is gone (its pipe end closed) — the worker
                         must fall off the loop and exit ("EOF suicide").
"""
from __future__ import annotations

import json
import os
import sys

CLOSE_SENTINEL = "__CLOSE__"

# Kept for reference by hosts sizing their pipes; protocol messages larger
# than the kernel pipe buffer would block the writer if the reader stalls.
DEFAULT_MESSAGE_CAP = 65536


def harden(pdeathsig: bool = True, max_rss_gb: float = 0.0) -> None:
    """Best-effort worker self-hardening. Never raises.

    Runs AFTER exec in the worker's own interpreter, so (unlike
    ``preexec_fn``) it is thread-safe by construction with respect to the
    spawning parent.
    """
    if pdeathsig and sys.platform.startswith("linux"):
        try:
            import ctypes
            import signal

            PR_SET_PDEATHSIG = 1
            libc = ctypes.CDLL(None, use_errno=True)
            libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
        except Exception:  # noqa: BLE001 — hardening is optional
            pass
    if max_rss_gb and max_rss_gb > 0:
        try:
            import resource

            limit = int(max_rss_gb * (1024 ** 3))
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        except Exception:  # noqa: BLE001
            pass


def protocol_channel():
    """Duplicate the real stdout for protocol lines, then fd1 -> fd2.

    After this, ``print()`` and any C-level/child-process write to fd1 land on
    stderr (which the host discards or logs); only the returned file object
    reaches the host's protocol reader.
    """
    proto = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return proto


def truncate_middle(text: str, max_chars: int = 6000) -> str:
    """Head+tail truncation with an explicit marker (protocol- and LLM-safe)."""
    text = "" if text is None else str(text)
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    head = max_chars * 2 // 3
    tail = max_chars - head
    omitted = len(text) - head - tail
    return (
        text[:head]
        + f"\n... [{omitted} chars truncated] ...\n"
        + text[-tail:]
    )


def emit(proto, payload: dict) -> None:
    """Write one protocol event line and flush."""
    proto.write(json.dumps(payload, ensure_ascii=False) + "\n")
    proto.flush()


def iter_stdin_lines():
    """Yield request lines until CLOSE_SENTINEL or stdin EOF (parent died)."""
    for line in sys.stdin:
        line = line.rstrip("\n")
        if line == CLOSE_SENTINEL:
            return
        yield line
