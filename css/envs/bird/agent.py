"""Bird task-execution agent: a native OpenAI function-calling ReAct loop.

The agent talks to the frozen target model through
``target_client.complete_target_tools`` (the generic function-calling capability
on the target path). It keeps the conversation in TWO forms:

  * ``api_messages`` - native OpenAI shape (assistant ``tool_calls`` + ``role:
    "tool"`` results) sent back to the model each turn.
  * ``traj``         - the CANONICAL mechanism trajectory: a list of
    ``{role, content:str}`` turns where each action is flattened to readable
    text (``"Action: execute_sql\\n```sql ... ```"``) and each observation to
    its text. This is what goes into ``TaskResult.messages`` — the env adapts to
    the mechanism's trajectory contract, never the reverse.

The gold SQL is NEVER placed in any prompt or in ``traj``; it is used only by
the EX evaluator after the loop ends.
"""
from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

from css.envs.bird.prompts import TOOLS, build_system, build_user
from css.envs.bird.sql_tools import execute_sql, ex_match, format_result, get_schema

if TYPE_CHECKING:
    from css.model.client import LLMClient

_MAX_SCHEMA_CHARS = 8000
_MAX_OBS_CHARS = 3000


def _render_action(name: str, sql: str) -> str:
    """Flatten one tool call into readable trajectory text."""
    if sql:
        return f"Action: {name}\n```sql\n{sql}\n```"
    return f"Action: {name}"


def _render_assistant(content: Any, tool_calls: list | None) -> str:
    """Flatten a native assistant turn into canonical readable content."""
    parts: list[str] = []
    if isinstance(content, str) and content.strip():
        parts.append(content.strip())
    for tc in tool_calls or []:
        fn = (tc or {}).get("function", {}) or {}
        name = str(fn.get("name", "") or "")
        if not name:
            continue
        try:
            args = json.loads(fn.get("arguments", "") or "{}")
        except (json.JSONDecodeError, TypeError):
            args = {}
        parts.append(_render_action(name, str(args.get("sql", ""))))
    return "\n\n".join(parts) if parts else "(no action)"


def run_bird_agent(
    target_client: "LLMClient",
    item: dict,
    skill_text: str,
    *,
    max_turns: int = 10,
    exec_timeout: float = 30.0,
    max_tokens: int = 16384,
    temperature: "float | None" = None,  # None = client default (0.6)
) -> dict:
    """Run the function-calling ReAct agent on one Bird item.

    Returns a dict with: ``predicted_sql``, ``gold_sql``, ``hard`` (EX 0/1),
    ``soft`` (Jaccard), ``n_turns``, ``fail_reason``, and ``conversation`` (the
    canonical ``{role, content:str}`` trajectory). Never raises.
    """
    db_path = str(item.get("db_path", ""))
    gold_sql = str(item.get("SQL", item.get("gold_sql", "")))

    out: dict[str, Any] = {
        "predicted_sql": "",
        "gold_sql": gold_sql,
        "hard": 0,
        "soft": 0.0,
        "n_turns": 0,
        "fail_reason": "",
        "conversation": [],
    }

    if not db_path or not os.path.exists(db_path):
        out["fail_reason"] = f"db not found: {db_path}"
        return out

    schema = get_schema(db_path, sample_rows=2)
    if len(schema) > _MAX_SCHEMA_CHARS:
        schema = schema[:_MAX_SCHEMA_CHARS] + "\n...[schema truncated]"

    system_msg = build_system(skill_text)
    user_msg = build_user(item, schema)

    # Native (model-facing) messages and the canonical trajectory run in lockstep.
    api_messages: list[dict] = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": user_msg},
    ]
    traj: list[dict] = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": user_msg},
    ]

    final_sql = ""
    last_exec_sql = ""

    for turn in range(max_turns):
        try:
            resp = target_client.complete_target_tools(
                api_messages, TOOLS, max_tokens=max_tokens, temperature=temperature
            )
        except Exception as e:  # noqa: BLE001 - a turn failure ends the rollout cleanly
            out["fail_reason"] = f"LLM error: {type(e).__name__}: {e}"
            break

        out["n_turns"] = turn + 1
        content = resp.get("content")
        tool_calls = resp.get("tool_calls") or []

        assistant_api: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            assistant_api["tool_calls"] = tool_calls
        api_messages.append(assistant_api)
        traj.append({"role": "assistant", "content": _render_assistant(content, tool_calls)})

        if not tool_calls:
            nudge = (
                "Please use the available tools to proceed. Call execute_sql to "
                "run a query, or submit_final_sql to submit your answer."
            )
            api_messages.append({"role": "user", "content": nudge})
            traj.append({"role": "user", "content": nudge})
            continue

        should_break = False
        for tc in tool_calls:
            fn = (tc or {}).get("function", {}) or {}
            name = str(fn.get("name", "") or "")
            try:
                args = json.loads(fn.get("arguments", "") or "{}")
            except (json.JSONDecodeError, TypeError):
                args = {}
            tc_id = tc.get("id", "")
            sql = str(args.get("sql", ""))

            if name == "submit_final_sql":
                final_sql = sql
                api_messages.append(
                    {"role": "tool", "tool_call_id": tc_id,
                     "content": "Final SQL submitted successfully."}
                )
                traj.append({"role": "tool", "content": "Observation: final SQL submitted."})
                should_break = True
                break

            if name == "execute_sql":
                last_exec_sql = sql
                res = execute_sql(db_path, sql, timeout=exec_timeout)
                obs = format_result(res)
                if len(obs) > _MAX_OBS_CHARS:
                    obs = obs[:_MAX_OBS_CHARS] + "\n...[output truncated]"
                api_messages.append(
                    {"role": "tool", "tool_call_id": tc_id, "content": obs}
                )
                traj.append({"role": "tool", "content": "Observation:\n" + obs})
            else:
                msg = f"Unknown tool: {name}. Use execute_sql or submit_final_sql."
                api_messages.append({"role": "tool", "tool_call_id": tc_id, "content": msg})
                traj.append({"role": "tool", "content": "Observation: " + msg})

        if should_break:
            break

    # Fallback: no explicit submit but the agent ran a query -> use the last one.
    if not final_sql and last_exec_sql:
        final_sql = last_exec_sql

    out["predicted_sql"] = final_sql
    out["conversation"] = traj

    if final_sql:
        ev = ex_match(final_sql, gold_sql, db_path, timeout=exec_timeout)
        out["hard"] = int(ev.get("ex", 0))
        out["soft"] = float(ev.get("soft", 0.0))
        if ev.get("ex", 0) < 1 and not out["fail_reason"]:
            out["fail_reason"] = ev.get("error", "") or f"EX=0: pred='{final_sql[:120]}'"
    elif not out["fail_reason"]:
        out["fail_reason"] = "no final SQL produced"

    return out
