"""Tests for css/envs/common/{subprocess_worker,worker_runtime}.

Real subprocesses (stdlib-only worker scripts under sys.executable) — the
lifecycle guarantees under test (group kill reaping grandchildren, bounded
reads, EOF suicide) are exactly the ones stubs cannot exercise.
"""
from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import tempfile
import threading
import time

import pytest

from css.envs.common.subprocess_worker import (
    CLOSE_SENTINEL,
    EngineSlotLimiter,
    SubprocessWorkerHost,
    WorkerDied,
    WorkerTimeout,
    ensure_nofile_limit,
)


def _load_runtime():
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "envs", "common", "worker_runtime.py")
    spec = importlib.util.spec_from_file_location("worker_runtime_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_worker(body: str) -> str:
    """Write a stdlib-only worker script; returns its path."""
    f = tempfile.NamedTemporaryFile(
        "w", suffix="_worker.py", delete=False, encoding="utf-8")
    f.write(body)
    f.close()
    return f.name


_ECHO_WORKER = """\
import json, sys
print(json.dumps({"event": "ready"}), flush=True)
for line in sys.stdin:
    line = line.rstrip("\\n")
    if line == "__CLOSE__":
        break
    print(json.dumps({"event": "echo", "line": line}), flush=True)
"""

# Spawns a 60s-sleeping GRANDCHILD, reports its pid, ignores SIGTERM, never
# honors CLOSE — only a group SIGKILL can end this family.
_STUBBORN_WORKER = """\
import json, os, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
print(json.dumps({"event": "ready", "grandchild": child.pid}), flush=True)
time.sleep(60)
"""

_SILENT_WORKER = "import time\ntime.sleep(60)\n"


def _host(script: str) -> SubprocessWorkerHost:
    import sys
    return SubprocessWorkerHost([sys.executable, script], name="test-worker")


def test_roundtrip_and_graceful_close():
    host = _host(_write_worker(_ECHO_WORKER))
    assert host.read_event(10)["event"] == "ready"
    host.send_line("hello world")
    ev = host.read_event(10)
    assert ev == {"event": "echo", "line": "hello world"}
    host.close()
    assert host.proc.poll() is not None  # reaped, no zombie


def test_send_json_is_newline_safe():
    host = _host(_write_worker(_ECHO_WORKER))
    host.read_event(10)
    host.send_json({"op": "execute", "code": "line1\nline2\nline3"})
    ev = host.read_event(10)
    assert json.loads(ev["line"]) == {"op": "execute", "code": "line1\nline2\nline3"}
    host.close()


def test_read_timeout_raises_and_is_timeouterror_compatible():
    host = _host(_write_worker(_SILENT_WORKER))
    t0 = time.time()
    with pytest.raises(WorkerTimeout):
        host.read_event(1.2)
    assert time.time() - t0 < 8
    assert issubclass(WorkerTimeout, TimeoutError)  # legacy except-clauses keep working
    host.kill()
    assert host.proc.poll() is not None


def test_worker_died_detected():
    host = _host(_write_worker("import sys; sys.exit(3)\n"))
    with pytest.raises(WorkerDied):
        host.read_event(10)
    host.close()


def test_group_kill_reaps_grandchild():
    host = _host(_write_worker(_STUBBORN_WORKER))
    ready = host.read_event(10)
    gc_pid = ready["grandchild"]
    # sanity: grandchild alive
    os.kill(gc_pid, 0)
    t0 = time.time()
    host.close(grace=1.0)
    assert host.proc.poll() is not None
    assert time.time() - t0 < 15
    # group SIGKILL must have taken the grandchild with it
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(gc_pid, 0)
            time.sleep(0.1)
        except ProcessLookupError:
            break
    with pytest.raises(ProcessLookupError):
        os.kill(gc_pid, 0)


def test_engine_slot_limiter_caps_concurrency():
    limiter = EngineSlotLimiter(2)
    active = []
    peak = []
    lock = threading.Lock()

    def work():
        with limiter.slot():
            with lock:
                active.append(1)
                peak.append(len(active))
            time.sleep(0.15)
            with lock:
                active.pop()

    threads = [threading.Thread(target=work) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert max(peak) <= 2


def test_ensure_nofile_limit_never_lowers():
    import resource

    before_soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    soft, hard = ensure_nofile_limit(min_soft=1024)
    assert soft >= min(before_soft, 1024) or soft == -1


# ── worker_runtime (loaded by file path, as real workers do) ─────────────────


def test_runtime_truncate_middle():
    rt = _load_runtime()
    assert rt.truncate_middle("abc", 10) == "abc"
    long = "x" * 500 + "MID" + "y" * 500
    out = rt.truncate_middle(long, 100)
    assert len(out) < 200
    assert "chars truncated" in out
    assert out.startswith("x") and out.endswith("y")
    assert rt.truncate_middle(None, 10) == ""


def test_runtime_sentinel_matches_host():
    rt = _load_runtime()
    assert rt.CLOSE_SENTINEL == CLOSE_SENTINEL


def test_runtime_eof_suicide_contract():
    """A worker built on iter_stdin_lines must exit when the parent closes stdin."""
    import sys

    rt_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "envs", "common", "worker_runtime.py")
    body = f"""\
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("rt", {rt_path!r})
rt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rt)
print(json.dumps({{"event": "ready"}}), flush=True)
for line in rt.iter_stdin_lines():
    print(json.dumps({{"event": "echo", "line": line}}), flush=True)
"""
    host = SubprocessWorkerHost([sys.executable, _write_worker(body)], name="eof-test")
    assert host.read_event(10)["event"] == "ready"
    host.proc.stdin.close()  # simulate parent death: pipe EOF
    host.proc.wait(timeout=10)
    assert host.proc.returncode == 0


if __name__ == "__main__":
    import sys
    import traceback

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except Exception:
            failed += 1
            print(f"  FAIL  {fn.__name__}")
            traceback.print_exc()
    print(f"{len(fns) - failed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
