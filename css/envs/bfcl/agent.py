"""BFCL multi-turn task agent — native function-calling loop over scripted turns.

The agent talks to the frozen target model through
``target_client.complete_target_tools`` (structured ``tool_calls``; on an endpoint
without a tool-call parser the client's XML fallback, commit 9a1823b, yields the
same shape). It keeps the conversation in TWO forms, like the Bird agent:
  * ``api`` — native OpenAI shape (assistant ``tool_calls`` + ``role:"tool"``
    results) sent to the model each step;
  * ``traj`` — the CANONICAL ``{role, content:str}`` transcript that becomes
    ``TaskResult.messages`` (each call flattened to ``Action: name(args)``, each
    result to ``Observation: ...``).

Per BFCL: user turns are pre-scripted (NO user-simulator); each turn runs a
tool-calling sub-loop that ends when the model emits no call, capped at 20 steps
(exceeding fails the entry). On a miss_func holdout turn the withheld tools are
added and the empty user turn is replaced by the canned prompt. Backend state
accumulates across turns on FRESH per-rollout instances (checker.make_instances)
— never the upstream ``globals()`` registry.

GROUND-TRUTH FIREWALL: the gold call sequences (``item['ground_truth']``) are
never placed in any prompt or in ``traj``; they are used only by the checker and
by the post-rollout annotation that ``task_interface`` appends.
"""
from __future__ import annotations

import json
import os
import time
from typing import TYPE_CHECKING, Any

from css.envs.bfcl import checker
from css.envs.bfcl.prompts import CANNED_HOLDOUT_PROMPT, build_system

if TYPE_CHECKING:
    from css.model.client import LLMClient

_FUNC_DOC_DIR = os.path.join(os.path.dirname(__file__), "vendor", "func_doc")
_FUNC_DOC_FILE = {
    "GorillaFileSystem": "gorilla_file_system.json",
    "MathAPI": "math_api.json",
    "MessageAPI": "message_api.json",
    "TwitterAPI": "posting_api.json",
    "TicketAPI": "ticket_api.json",
    "TradingBot": "trading_bot.json",
    "TravelAPI": "travel_booking.json",
    "VehicleControlAPI": "vehicle_control.json",
}
# BFCL func-doc uses "dict"/"float"; OpenAI/JSON-schema wants "object"/"number".
_TYPE_MAP = {"dict": "object", "float": "number"}
_MAX_OBS_CHARS = 3000


def _load_func_docs() -> dict[str, list[dict]]:
    docs: dict[str, list[dict]] = {}
    for cls, fn in _FUNC_DOC_FILE.items():
        path = os.path.join(_FUNC_DOC_DIR, fn)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                docs[cls] = [json.loads(line) for line in f if line.strip()]
    return docs


_CLASS_FUNCS = _load_func_docs()


def _sanitize(schema: Any) -> Any:
    """Recursively remap BFCL types to JSON-schema types; drop the 'response' key."""
    if isinstance(schema, dict):
        return {
            k: (_TYPE_MAP.get(v, v) if k == "type" and isinstance(v, str) else _sanitize(v))
            for k, v in schema.items()
            if k != "response"
        }
    if isinstance(schema, list):
        return [_sanitize(x) for x in schema]
    return schema


def build_tools(involved_classes: list[str], held: set[str], released: set[str]) -> list[dict]:
    """OpenAI tool schemas for the involved backends, excluding still-held-out funcs."""
    tools: list[dict] = []
    for cls in involved_classes:
        for f in _CLASS_FUNCS.get(cls, []):
            name = f.get("name", "")
            if name in held and name not in released:
                continue
            tools.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": str(f.get("description", ""))[:900],
                    "parameters": _sanitize(f.get("parameters", {"type": "object", "properties": {}})),
                },
            })
    return tools


def _tc_name_args(tc: dict) -> tuple[str, dict]:
    fn = (tc or {}).get("function", {}) or {}
    name = str(fn.get("name", "") or "")
    try:
        args = json.loads(fn.get("arguments", "") or "{}")
    except (json.JSONDecodeError, TypeError):
        args = {}
    return name, (args if isinstance(args, dict) else {})


def _callstring(name: str, args: dict) -> str:
    """A BFCL executable call string, e.g. ``mv(source='a', destination='b')``.
    ``repr`` on json-decoded values yields valid Python literals the checker evals."""
    return "%s(%s)" % (name, ", ".join("%s=%r" % (k, v) for k, v in args.items()))


def _flatten_assistant(content: Any, tool_calls: list | None) -> str:
    parts: list[str] = []
    if isinstance(content, str) and content.strip():
        parts.append(content.strip())
    for tc in tool_calls or []:
        name, args = _tc_name_args(tc)
        if name:
            parts.append("Action: " + _callstring(name, args))
    return "\n\n".join(parts) if parts else "(no tool call)"


def run_bfcl_agent(
    target_client: "LLMClient",
    item: dict,
    skill_text: str,
    *,
    max_steps_per_turn: int = 20,
    max_tokens: int = 16384,
    temperature: "float | None" = None,  # None = client default (0.6)
    deadline_s: float = 540.0,
) -> dict:
    """Play one BFCL multi-turn entry; return the RETURN CONTRACT dict.

    Never raises: an LLM/exec error ends the rollout as a failure. Scoring uses
    the vendored checker on fresh isolated instances (parity-locked to upstream).
    """
    involved = list(item.get("involved_classes", []))
    long_ctx = bool(item.get("long_context"))
    missed = {str(k): list(v) for k, v in (item.get("missed_function") or {}).items()}
    held = {f for v in missed.values() for f in v}
    released: set[str] = set()
    gt = item.get("ground_truth") or []
    n_gt = len(gt)
    questions = item.get("question") or []

    system = build_system(skill_text)
    api: list[dict] = [{"role": "system", "content": system}]
    traj: list[dict] = [{"role": "system", "content": system}]

    # Live instances for observation execution (fresh, isolated — NO globals()).
    live = checker.make_instances(involved, item.get("initial_config", {}), long_ctx)
    live_ns = checker.method_namespace(live)

    decoded_per_turn: list[list[list[str]]] = []
    refrain_notes: list[dict] = []
    n_steps = 0
    force_terminated = False
    fail_reason = ""
    started = time.time()

    for t, umsg in enumerate(questions):
        released |= set(missed.get(str(t), []))
        tools = build_tools(involved, held, released)
        user = CANNED_HOLDOUT_PROMPT if str(t) in missed else (umsg[0]["content"] if umsg else "")
        api.append({"role": "user", "content": user})
        traj.append({"role": "user", "content": user})

        turn_steps: list[list[str]] = []
        emitted = False
        last_text = ""
        count = 0
        while True:
            if time.time() - started > deadline_s:
                force_terminated = True
                fail_reason = "episode-deadline (%.0fs)" % deadline_s
                break
            try:
                resp = target_client.complete_target_tools(
                    api, tools, max_tokens=max_tokens, temperature=temperature)
            except Exception as e:  # noqa: BLE001 — a turn failure ends the rollout
                force_terminated = True
                fail_reason = "LLM error: %s: %s" % (type(e).__name__, e)
                break
            content = resp.get("content")
            tool_calls = resp.get("tool_calls") or []
            last_text = content or ""
            assistant: dict[str, Any] = {"role": "assistant", "content": content}
            if tool_calls:
                assistant["tool_calls"] = tool_calls
            api.append(assistant)
            traj.append({"role": "assistant", "content": _flatten_assistant(content, tool_calls)})

            if not tool_calls:
                break  # no call -> this user turn is complete

            emitted = True
            step_calls = [_callstring(*_tc_name_args(tc)) for tc in tool_calls]
            turn_steps.append(step_calls)
            results = checker.execute_calls(step_calls, live_ns)
            for tc, res in zip(tool_calls, results):
                api.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": res})
            obs = " | ".join(results)
            traj.append({"role": "tool", "content": "Observation:\n" + obs[:_MAX_OBS_CHARS]})

            n_steps += 1
            count += 1
            if count > max_steps_per_turn:
                force_terminated = True
                fail_reason = "force-terminated (turn %d exceeded %d steps)" % (t, max_steps_per_turn)
                break

        decoded_per_turn.append(turn_steps)
        if t < n_gt and len(gt[t]) == 0:  # a refrain checkpoint (miss_param/miss_func)
            refrain_notes.append({"turn": t, "emitted_call": emitted, "said": last_text[:160]})
        if force_terminated:
            break

    # ── Scoring (AFTER the loop; the agent never saw the gold) ──────────────
    verdict = checker.score(decoded_per_turn, gt, item.get("initial_config", {}), involved, long_ctx)
    hard = int(verdict["valid"])
    soft = (verdict["turns_passed"] / verdict["n_turns"]) if verdict["n_turns"] else 0.0
    if not hard and not fail_reason:
        fail_reason = verdict["error"] or "state/response mismatch"

    return {
        "hard": hard,
        "soft": float(soft),
        "n_cases": 1,
        "n_pass": hard,
        "n_turns": len(decoded_per_turn),
        "fail_reason": "" if hard else fail_reason,
        "conversation": traj,
        # Env extras (absorbed into TaskResult.extras):
        "category": str(item.get("category", "")),
        "n_steps": n_steps,
        "force_terminated": force_terminated,
        "turns_passed": verdict["turns_passed"],
        "gt_turns": n_gt,
        "refrain_notes": refrain_notes,
        "error_type": verdict.get("error_type", ""),
        "elapsed_s": round(time.time() - started, 1),
    }
