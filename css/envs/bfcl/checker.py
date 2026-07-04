"""BFCL multi-turn checker — adapted from bfcl_eval for per-rollout isolation.

Behaviourally identical to the upstream ``multi_turn_checker`` (pinned 6ea5797)
in the only thing the mechanism reads — the per-entry PASS/FAIL verdict — with
one deliberate structural change:

  * upstream ``execute_multi_turn_func_call`` stashes each live backend instance
    in the module ``globals()`` keyed by ``model_name+id+class``. Concurrent
    rollouts of the SAME entry with the same key silently cross-contaminate
    (measured 1/8 correct; docs/env_prep/bfcl_PREP.md §5). Here every call to
    :func:`score` builds its OWN fresh instance set and evaluates calls in a
    private namespace — nothing is shared across rollouts. This is the isolation
    the CSS concurrent evaluator needs; ``test_bfcl_checker.py`` locks both
    upstream parity (verdict equality on gold replays) and 8-way isolation.

Documented deviations from upstream, none of which change the verdict:
  * calls are evaluated as bare ``method(...)`` in an explicit namespace instead
    of ``instance.method(...)`` in ``globals()`` (same last-wins behaviour when
    two involved classes share a method name);
  * the failure ``error_type`` strings are our own (upstream's are not consumed
    by the mechanism); the boolean ``valid`` matches upstream.

Scoring semantics (upstream): for each turn, execute the model's decoded calls on
its own instance set (state accumulates across turns), execute the turn's ground
truth on a separate set, then — only when the GT turn is non-empty — require
(a) STATE: every non-private attribute of every involved instance equal, and
(b) RESPONSE: the GT result strings are an unordered sub-multiset of the model's
results accumulated across all turns so far. First failing turn fails the entry.
Empty-GT ("refrain") turns are executed for downstream state but not checked
(upstream's ``multi_turn_irrelevance_checker`` is defined-but-never-called, so
refraining is enforced only indirectly — see the PREP doc).
"""
from __future__ import annotations

import copy
import inspect
import json
from typing import Any

from css.envs.bfcl.vendor import (
    gorilla_file_system,
    math_api,
    message_api,
    posting_api,
    ticket_api,
    trading_bot,
    travel_booking,
    vehicle_control,
)

# Class-name -> vendored class. Mirrors bfcl_eval CLASS_FILE_PATH_MAPPING for the
# 8 multi-turn backends (the V4 memory/web_search classes are out of scope).
CLASS_MAP: dict[str, type] = {
    "GorillaFileSystem": gorilla_file_system.GorillaFileSystem,
    "MathAPI": math_api.MathAPI,
    "MessageAPI": message_api.MessageAPI,
    "TwitterAPI": posting_api.TwitterAPI,
    "TicketAPI": ticket_api.TicketAPI,
    "TradingBot": trading_bot.TradingBot,
    "TravelAPI": travel_booking.TravelAPI,
    "VehicleControlAPI": vehicle_control.VehicleControlAPI,
}
STATELESS_CLASSES = {"MathAPI"}
# Upstream's minimal safety denylist (bare function head).
_BLOCKED = {"kill", "exit", "quit", "remove", "unlink", "popen", "Popen", "run"}


def make_instances(
    involved_classes: list[str], initial_config: dict, long_context: bool
) -> dict[str, Any]:
    """Fresh backend instances for one rollout, loaded from ``initial_config``.

    Deterministic given the config (RNG is per-instance seeded from
    ``initial_config[cls]['random_seed']``; see the determinism probe). NO global
    state is touched — this is the isolation the concurrent evaluator relies on.
    """
    instances: dict[str, Any] = {}
    for cls in involved_classes:
        obj = CLASS_MAP[cls]()
        if cls not in STATELESS_CLASSES:
            obj._load_scenario(
                copy.deepcopy((initial_config or {}).get(cls, {})),
                long_context=long_context,
            )
        instances[cls] = obj
    return instances


def method_namespace(instances: dict[str, Any]) -> dict[str, Any]:
    """Map each public bound method name to the method (last-wins on collision,
    matching upstream ``class_method_name_mapping``)."""
    ns: dict[str, Any] = {}
    for obj in instances.values():
        for name, meth in inspect.getmembers(obj, predicate=inspect.ismethod):
            if not name.startswith("_"):
                ns[name] = meth
    return ns


def _stringify(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        try:
            return json.dumps(result)
        except Exception:  # noqa: BLE001
            return str(result)
    return str(result)


def execute_calls(call_strings: list[str], ns: dict[str, Any]) -> list[str]:
    """Execute BFCL call strings (e.g. ``mv(source='a', destination='b')``) in the
    instance namespace; return the upstream-shaped result strings (str / json /
    ``Error during execution: ...``). Never raises."""
    results: list[str] = []
    for call in call_strings:
        head = call.split("(", 1)[0].strip() if "(" in call else call.strip()
        head = head.rsplit(".", 1)[-1]
        if head in _BLOCKED:
            results.append(f"Error during execution: Function call {head} is not allowed.")
            continue
        try:
            results.append(_stringify(eval(call, {"__builtins__": {}}, ns)))  # noqa: S307
        except Exception as e:  # noqa: BLE001 — a failed call is an error observation
            results.append(f"Error during execution: {e}")
    return results


def _compare_state(model: dict[str, Any], gt: dict[str, Any]) -> tuple[bool, dict]:
    """Every non-private attribute of every involved instance must be equal."""
    diff: dict[str, Any] = {}
    for cls, gobj in gt.items():
        mobj = model[cls]
        for attr in vars(gobj):
            if attr.startswith("_"):
                continue
            if getattr(mobj, attr, object()) != getattr(gobj, attr):
                diff[f"{cls}.{attr}"] = {
                    "model": getattr(mobj, attr, None),
                    "ground_truth": getattr(gobj, attr),
                }
    return (not diff), diff


def _is_subsequence_unordered(need: list[str], have: list[str]) -> tuple[bool, list[str]]:
    """``need`` is an unordered sub-multiset of ``have`` (upstream semantics)."""
    pool = list(have)
    missing: list[str] = []
    for item in need:
        try:
            pool.remove(item)
        except ValueError:
            missing.append(item)
    return (not missing), missing


def score(
    model_calls_per_turn: list[list[list[str]]],
    gt_per_turn: list[list[str]],
    initial_config: dict,
    involved_classes: list[str],
    long_context: bool,
) -> dict:
    """Return the per-entry verdict + graded turn progress.

    ``model_calls_per_turn[t]`` is a list of steps, each a list of call strings
    (one step = one assistant tool-calling turn). ``gt_per_turn[t]`` is the gold
    call list for turn ``t``.

    Returns ``{valid: bool, turns_passed: int, n_turns: int, error_type, error}``.
    ``soft = turns_passed / n_turns``.
    """
    n = len(gt_per_turn)
    model = make_instances(involved_classes, initial_config, long_context)
    gt = make_instances(involved_classes, initial_config, long_context)
    m_ns, g_ns = method_namespace(model), method_namespace(gt)
    model_results: list[str] = []

    for t in range(n):
        steps = model_calls_per_turn[t] if t < len(model_calls_per_turn) else []
        for step_calls in steps:
            model_results.extend(execute_calls(step_calls, m_ns))
        gt_calls = gt_per_turn[t]
        gt_results = execute_calls(gt_calls, g_ns)
        if not gt_calls:  # refrain turn: executed for state accuracy, not checked
            continue
        ok, diff = _compare_state(model, gt)
        if not ok:
            return {
                "valid": False, "turns_passed": t, "n_turns": n,
                "error_type": "multi_turn:state_mismatch",
                "error": "turn %d: state mismatch on %s" % (t, list(diff)[:4]),
            }
        ok, missing = _is_subsequence_unordered(gt_results, model_results)
        if not ok:
            return {
                "valid": False, "turns_passed": t, "n_turns": n,
                "error_type": "multi_turn:response_mismatch",
                "error": "turn %d: missing response(s) %s" % (t, missing[:3]),
            }
    return {"valid": True, "turns_passed": n, "n_turns": n, "error_type": "", "error": ""}


def gold_as_model(gt_per_turn: list[list[str]]) -> list[list[list[str]]]:
    """Wrap a gold call sequence as a single-step model trajectory (self-check /
    gold-replay: each turn's gold calls become one step)."""
    return [[turn] for turn in gt_per_turn]
