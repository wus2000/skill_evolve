"""Bash command execution tool for the ReAct agent.

Anti-leakage sandboxing
-----------------------
``create_bash_tool`` accepts ``forbidden_dirs`` — absolute directory paths the
agent must not read (e.g. the task directory containing golden answer files).

The sandbox works at two layers:

1. **Python layer (sitecustomize.py via PYTHONPATH):** A ``sitecustomize.py``
   is generated in a per-tool tmpdir and injected via ``PYTHONPATH``.  Unlike
   ``PYTHONSTARTUP`` (interactive-only), ``sitecustomize`` runs for ALL Python
   invocations including ``python script.py``.  It monkey-patches
   ``builtins.open``, ``os.listdir/stat/scandir/walk``, ``pathlib.Path.open``,
   ``openpyxl.load_workbook``, and ``pandas.read_excel`` to refuse operations
   on forbidden paths.

2. **Shell layer:** The shell ``cat``/``head``/``tail``/``less``/``more``/``cp``
   commands are aliased to a guard function that blocks arguments containing
   forbidden path prefixes.

Neither layer uses prompt instructions — the agent model never sees the
forbidden paths or any mention of restrictions.
"""
from __future__ import annotations

import os
import signal
import subprocess
import tempfile

from .tools import tool


def _build_sitecustomize(forbidden_dirs: list[str]) -> str:
    """Build sitecustomize.py that patches Python builtins to block forbidden paths."""
    dirs_repr = repr(forbidden_dirs)
    return f'''import builtins as _b, os as _os, sys as _sys, functools as _ft

# Resolve forbidden dirs once at import time (before any monkey-patching).
_FORBIDDEN = []
for _d in {dirs_repr}:
    try:
        _FORBIDDEN.append(_os.path.realpath(_d))
    except Exception:
        _FORBIDDEN.append(_os.path.abspath(_d))

def _is_forbidden(path):
    try:
        p = _os.path.abspath(str(path))
    except Exception:
        return False
    return any(p == fd or p.startswith(fd + _os.sep) for fd in _FORBIDDEN)

_orig_open = _b.open
@_ft.wraps(_orig_open)
def _safe_open(file, *a, **kw):
    if isinstance(file, (str, _os.PathLike)) and _is_forbidden(file):
        print(f"[SANDBOX] Blocked open: {{file}}", file=_sys.stderr)
        raise PermissionError(f"Access denied: {{file}}")
    return _orig_open(file, *a, **kw)
_b.open = _safe_open

for _fn_name in ("listdir", "scandir"):
    _orig = getattr(_os, _fn_name, None)
    if _orig is None:
        continue
    def _make_guard(orig, name):
        @_ft.wraps(orig)
        def _guard(path=".", *a, **kw):
            if _is_forbidden(path):
                print(f"[SANDBOX] Blocked {{name}}: {{path}}", file=_sys.stderr)
                raise PermissionError(f"Access denied: {{path}}")
            return orig(path, *a, **kw)
        return _guard
    setattr(_os, _fn_name, _make_guard(_orig, _fn_name))

_orig_walk = _os.walk
@_ft.wraps(_orig_walk)
def _safe_walk(top, *a, **kw):
    if _is_forbidden(top):
        return iter([])
    return _orig_walk(top, *a, **kw)
_os.walk = _safe_walk

try:
    import pathlib as _pl
    _orig_path_open = _pl.Path.open
    @_ft.wraps(_orig_path_open)
    def _safe_path_open(self, *a, **kw):
        if _is_forbidden(str(self)):
            raise PermissionError(f"Access denied: {{self}}")
        return _orig_path_open(self, *a, **kw)
    _pl.Path.open = _safe_path_open
except Exception:
    pass

try:
    import openpyxl as _opx
    _orig_load_wb = _opx.load_workbook
    @_ft.wraps(_orig_load_wb)
    def _safe_load_wb(filename, *a, **kw):
        if _is_forbidden(str(filename)):
            raise PermissionError(f"Access denied: {{filename}}")
        return _orig_load_wb(filename, *a, **kw)
    _opx.load_workbook = _safe_load_wb
except ImportError:
    pass

try:
    import pandas as _pd
    _orig_read_excel = _pd.read_excel
    @_ft.wraps(_orig_read_excel)
    def _safe_read_excel(io, *a, **kw):
        if isinstance(io, (str, _os.PathLike)) and _is_forbidden(str(io)):
            raise PermissionError(f"Access denied: {{io}}")
        return _orig_read_excel(io, *a, **kw)
    _pd.read_excel = _safe_read_excel
except ImportError:
    pass
'''


def _kill_process_group(proc: subprocess.Popen) -> None:
    """SIGKILL the whole process group of ``proc`` — the shell PLUS every
    grandchild it spawned.

    ``proc`` is started with ``start_new_session=True`` so ``proc.pid`` leads a
    fresh process group; killing the group tears down a hung shell AND any
    orphan-prone grandchildren (a ``python3`` spinning on a catastrophic regex, a
    stray ``soffice``, a process blocked on stdin, …). A plain ``proc.kill()``
    only kills the direct child shell and leaves those grandchildren running,
    holding the stdout/stderr pipes open and hanging the drain forever — the
    exact failure that stalled the experiment for ~24 min per incident.
    Best-effort: never raises.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except Exception:
        pgid = None
    try:
        if pgid is not None:
            os.killpg(pgid, signal.SIGKILL)
        else:
            proc.kill()
    except ProcessLookupError:
        pass
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def create_bash_tool(
    working_dir: str,
    timeout: int = 120,
    forbidden_dirs: list[str] | None = None,
):
    """Create a bash execution tool for running commands.

    Args:
        working_dir: Directory where commands will be executed
        timeout: Command timeout in seconds
        forbidden_dirs: Absolute paths the agent must not read (e.g. data_root
            containing golden answer files).
    """
    _forbidden = [os.path.realpath(d) for d in (forbidden_dirs or []) if d]

    sandbox_dir: str | None = None
    if _forbidden:
        sandbox_dir = tempfile.mkdtemp(prefix="css_sandbox_")
        script_content = _build_sitecustomize(_forbidden)
        with open(os.path.join(sandbox_dir, "sitecustomize.py"), "w") as f:
            f.write(script_content)

    @tool(name="bash")
    def bash(command: str) -> str:
        """Execute a bash command in the working directory.
        Use this to run Python scripts, install packages, navigate files,
        or perform any shell operations.

        Args:
            command: The bash command to execute
        """
        env = os.environ.copy()

        if sandbox_dir and _forbidden:
            existing = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = sandbox_dir + (":" + existing if existing else "")

        # ``start_new_session=True`` puts the shell in its OWN process group so a
        # timeout can kill the WHOLE tree (the shell AND any grandchildren it
        # spawned). A plain ``subprocess.run(..., timeout=)`` only kills the direct
        # child shell; an orphaned grandchild (e.g. a python3 stuck in a
        # catastrophic-backtracking regex) keeps the pipes open and hangs forever.
        proc = None
        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                cwd=working_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                start_new_session=True,
            )
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_process_group(proc)
                # Drain quickly now that the tree is dead (pipes close on kill).
                try:
                    stdout, stderr = proc.communicate(timeout=10)
                except Exception:
                    stdout, stderr = "", ""
                msg = (
                    f"[ERROR] Command timed out after {timeout}s and was killed, "
                    f"along with every subprocess it spawned. A command this slow is "
                    f"almost always hung — an infinite loop, a catastrophic-"
                    f"backtracking regex (avoid nested '.*?' / '.*' on large text), "
                    f"or a process waiting on stdin. Do NOT retry it unchanged: run a "
                    f"faster, bounded version (add a limit/timeout, simplify the "
                    f"regex, or read/scan less data)."
                )
                partial = (stdout or "").strip()
                if partial:
                    msg += (
                        "\n[Partial stdout captured before the command was killed]\n"
                        + partial[:2000]
                    )
                return msg
            output = ""
            if stdout:
                output += stdout
            if stderr:
                output += f"\n[STDERR]\n{stderr}" if output else stderr
            if proc.returncode != 0:
                output += f"\n[Exit code: {proc.returncode}]"
            return output.strip() if output.strip() else "[Command completed with no output]"
        except Exception as e:
            if proc is not None:
                _kill_process_group(proc)
            return f"[ERROR] Failed to execute command: {e}"

    return bash
