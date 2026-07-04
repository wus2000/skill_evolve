"""AppWorld environment worker — a standalone one-episode subprocess CLI.

WHY A SUBPROCESS (see also css/envs/alfworld/worker.py):
  * AppWorld's unified mode allows exactly ONE live world per OS process —
    ``freezegun`` mocks wall-clock time process-wide and DB/memory management
    assumes a single active world. One worker process per episode makes the
    isolation hold BY CONSTRUCTION (this is the officially documented
    constraint that rules out threads/asyncio in one process).
  * ``appworld`` requires Python >= 3.11 while the harness process may run
    3.8: the worker interpreter is configurable (``appworld_python``), and the
    css process NEVER imports appworld.

PROTOCOL (JSON lines; requests on stdin — JSON so multi-line code survives):
  parent -> child : {"op": "execute", "code": str}
                    {"op": "evaluate"}
                    {"op": "gold"}
                    the literal ``__CLOSE__`` (or stdin EOF) ends the episode
  child  -> parent (on the child's ORIGINAL stdout, duplicated before fd
  redirection; the engine's own noise goes to stderr):
      {"event": "ready", "instruction": str, "supervisor": {...},
       "metadata": {...}}
      {"event": "execute_result", "output": str(truncated), "completed": bool}
      {"event": "evaluation", "success": bool, "report": {...}}
      {"event": "gold", "solution_code": str, "answer": str|null}
      {"event": "fatal", "error": str}

GROUND-TRUTH FIREWALL: the ``gold`` op is only honored when the world was
opened with ``ground_truth_mode="full"`` (train/dev); the HOST decides when to
call it (after the episode, for the optimizer-only annotation). Nothing gold
is ever emitted in ``ready``/``execute_result`` events.

argv: worker.py <task_id> <experiment_name> <ground_truth_mode>
                <max_interactions> <obs_max_chars>
env : APPWORLD_ROOT must point at the data checkout (contains data/).

This module imports ONLY the standard library at module level; appworld loads
lazily inside main() in the worker interpreter.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import traceback

# Shared worker-side runtime, loaded BY FILE PATH (the worker interpreter is
# the env's own python; the css package need not be importable there).
_RT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), os.pardir, "common",
    "worker_runtime.py")
_rt_spec = importlib.util.spec_from_file_location("worker_runtime", _RT_PATH)
_runtime = importlib.util.module_from_spec(_rt_spec)
_rt_spec.loader.exec_module(_runtime)

CLOSE_SENTINEL = _runtime.CLOSE_SENTINEL


def _jsonable(value):
    """Best-effort conversion of engine objects to JSON-serializable data."""
    try:
        json.dumps(value)
        return value
    except Exception:  # noqa: BLE001
        pass
    if hasattr(value, "to_dict"):
        try:
            return _jsonable(value.to_dict())
        except Exception:  # noqa: BLE001
            pass
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return str(value)


def _supervisor_dict(task) -> dict:
    sup = getattr(task, "supervisor", None)
    if sup is None:
        return {}
    if isinstance(sup, dict):
        return {str(k): str(v) for k, v in sup.items()}
    out = {}
    for key in ("first_name", "last_name", "email", "phone_number"):
        out[key] = str(getattr(sup, key, ""))
    return out


def _task_metadata(world) -> dict:
    """Difficulty/complexity metadata (available on all splits; optimizer-only)."""
    for holder in (getattr(world, "task", None),
                   getattr(getattr(world, "task", None), "ground_truth", None)):
        md = getattr(holder, "metadata", None)
        if md is None:
            continue
        if isinstance(md, dict):
            return _jsonable(md)
        out = {}
        for key in ("difficulty", "num_apis", "num_apps",
                    "num_solution_code_lines"):
            if hasattr(md, key):
                out[key] = _jsonable(getattr(md, key))
        if out:
            return out
    return {}


def _tracker_report(tracker) -> dict:
    """Extract a per-requirement report from the evaluation TestTracker.

    Defensive across appworld versions: tries dict-like dumps first, then
    known attribute names, then str(). The report is optimizer-only fuel for
    failure attribution (requirement descriptions carry no solution path).
    """
    report: dict = {}
    for name in ("to_dict", "as_dict", "dict", "report"):
        fn = getattr(tracker, name, None)
        if callable(fn):
            try:
                out = fn()
                if isinstance(out, (dict, list, str)) and out:
                    report["detail"] = _jsonable(out)
                    return report
            except Exception:  # noqa: BLE001
                continue
    for attr in ("passes", "failures", "passed_tests", "failed_tests",
                 "test_results"):
        val = getattr(tracker, attr, None)
        if isinstance(val, (list, tuple)) and val:
            report[attr] = [_jsonable(v) for v in val]
    if not report:
        report["detail"] = str(tracker)
    return report


def main() -> int:
    _runtime.harden()  # PDEATHSIG (Linux): die with the parent even mid-step
    proto = _runtime.protocol_channel()
    try:
        task_id = sys.argv[1]
        experiment_name = sys.argv[2]
        ground_truth_mode = sys.argv[3] if len(sys.argv) > 3 else "minimal"
        max_interactions = int(sys.argv[4]) if len(sys.argv) > 4 else 50
        obs_max_chars = int(sys.argv[5]) if len(sys.argv) > 5 else 6000

        from appworld import AppWorld

        root = os.environ.get("APPWORLD_ROOT", "")
        if root:
            try:
                from appworld import update_root

                update_root(root)
            except Exception:  # noqa: BLE001 — env var alone may suffice
                pass

        world = AppWorld(
            task_id=task_id,
            experiment_name=experiment_name,
            ground_truth_mode=ground_truth_mode,
            max_interactions=max_interactions,
        )
        _runtime.emit(proto, {
            "event": "ready",
            "instruction": str(world.task.instruction),
            "supervisor": _supervisor_dict(world.task),
            "metadata": _task_metadata(world),
        })
    except Exception:  # noqa: BLE001 — report through the protocol, then die
        _runtime.emit(proto, {"event": "fatal", "error": traceback.format_exc()})
        return 1

    try:
        for line in _runtime.iter_stdin_lines():
            if not line.strip():
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                _runtime.emit(proto, {
                    "event": "fatal",
                    "error": "malformed request line: %r" % line[:200]})
                return 1
            op = request.get("op", "")
            if op == "execute":
                output = world.execute(str(request.get("code", "")))
                _runtime.emit(proto, {
                    "event": "execute_result",
                    "output": _runtime.truncate_middle(
                        str(output), obs_max_chars),
                    "completed": bool(world.task_completed()),
                })
            elif op == "evaluate":
                tracker = world.evaluate(suppress_errors=True)
                _runtime.emit(proto, {
                    "event": "evaluation",
                    "success": bool(getattr(tracker, "success", False)),
                    "report": _tracker_report(tracker),
                })
            elif op == "gold":
                gt = getattr(world.task, "ground_truth", None)
                code = str(getattr(gt, "compiled_solution_code", "") or "")
                answer = getattr(gt, "answer", None)
                _runtime.emit(proto, {
                    "event": "gold",
                    "solution_code": code,
                    "answer": _jsonable(answer) if answer is not None else None,
                })
            else:
                _runtime.emit(proto, {
                    "event": "fatal", "error": "unknown op: %r" % op})
                return 1
    except Exception:  # noqa: BLE001
        _runtime.emit(proto, {"event": "fatal", "error": traceback.format_exc()})
        return 1
    finally:
        try:
            world.close()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
