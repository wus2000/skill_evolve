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

        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=working_dir,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
            output = ""
            if result.stdout:
                output += result.stdout
            if result.stderr:
                stderr_clean = result.stderr
                output += f"\n[STDERR]\n{stderr_clean}" if output else stderr_clean
            if result.returncode != 0:
                output += f"\n[Exit code: {result.returncode}]"
            return output.strip() if output.strip() else "[Command completed with no output]"
        except subprocess.TimeoutExpired:
            return f"[ERROR] Command timed out after {timeout} seconds"
        except Exception as e:
            return f"[ERROR] Failed to execute command: {e}"

    return bash
