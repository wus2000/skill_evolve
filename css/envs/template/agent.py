"""Template task agent — the env-private execution loop.

The agent is 100% the env's business: the mechanism never sees it. It only
sees the ``TaskResult`` your ``task_interface.run_one`` builds from what this
module returns. That means you are free to use ANY agent shape here (single
call, ReAct text loop, native function calling, code execution sandbox, ...)
as long as the RETURN CONTRACT below is honored.

RETURN CONTRACT — ``run_template_agent`` returns a plain dict:
  {
    "hard": int,            # 1 = task passed, 0 = failed (headline metric)
    "soft": float,          # partial credit in [0, 1] (== hard if binary)
    "n_turns": int,         # agent turns consumed
    "fail_reason": str,     # short diagnostic ("" when passed)
    "conversation": list,   # the trajectory — see TRAJECTORY CONTRACT below
    ...                     # any env-specific extras (predicted answer, gold,
                            # executor diagnostics, ...) — carried into
                            # TaskResult.extras automatically
  }

TRAJECTORY CONTRACT (mechanism-defined, css/trajectory.py):
  ``conversation`` is a flat, readable ``[{"role": str, "content": str}, ...]``
  transcript. The optimizer's analysis prompts consume EXACTLY this shape.
  If your agent runs on a richer transport (e.g. OpenAI function calling,
  where an assistant turn carries ``tool_calls`` and results come back as
  ``role="tool"`` messages), FLATTEN it here: render each action as text
  ("Action: <name>\\n<args>") and each observation as its content. See
  ``css/envs/bird/agent.py`` for the reference multi-turn function-calling
  implementation of this flattening.

GROUND-TRUTH FIREWALL (non-negotiable):
  The gold answer must NEVER appear in any prompt the agent sees, nor in any
  message of the returned conversation. Score AFTER the loop, outside the
  agent's sight. (``task_interface.run_one`` appends the post-rollout eval
  annotation — which does carry the gold, for the optimizer's analysis only —
  as a separate final message with role="evaluation".)

SKILL INJECTION:
  ``skill_text`` is the rendered skill document (strategy.md + rules.md).
  Inject it into the agent's system prompt verbatim. It is the ONLY channel
  through which the optimizer influences the agent — do not paraphrase it,
  do not merge it into task-specific text.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from css.model.client import LLMClient


# TODO(env): replace with your task's system prompt. Keep the skill block
# verbatim and clearly delimited; keep task-specific protocol instructions
# (output format, available actions) OUTSIDE the skill block.
_SYSTEM_TEMPLATE = """\
You are a task-solving agent.

## Skill document (your accumulated strategy and rules — follow it)
{skill_text}

## Task protocol
Answer the question. End your reply with a line of the exact form:
FINAL ANSWER: <answer>"""


def _extract_final_answer(text: str) -> str:
    """TODO(env): replace with your answer-extraction / action-parsing logic."""
    for line in reversed((text or "").splitlines()):
        stripped = line.strip()
        if stripped.upper().startswith("FINAL ANSWER:"):
            return stripped.split(":", 1)[1].strip()
    return (text or "").strip()


def _normalize(answer: str) -> str:
    """TODO(env): replace with your scoring normalization."""
    return " ".join((answer or "").lower().split())


def run_template_agent(
    client: "LLMClient",
    item: dict,
    skill_text: str,
    *,
    max_turns: int = 1,
    max_tokens: int = 16384,
    temperature: float = 0.0,
) -> dict[str, Any]:
    """Execute ONE task rollout and return the RETURN CONTRACT dict.

    This template implements the simplest possible agent — a single LLM call
    with exact-match scoring — so the package is runnable out of the box.
    For a multi-turn tool-using agent (the common case), replace the body
    with your ReAct / function-calling loop; ``css/envs/bird/agent.py`` is
    the reference implementation (tool schema, turn loop, flattening,
    max-turns termination, forced final answer).
    """
    question = str(item.get("question", ""))
    gold = str(item.get("answer", ""))  # NEVER enters a prompt below.

    system = _SYSTEM_TEMPLATE.format(skill_text=skill_text or "(empty)")
    user = question

    # ── Agent loop (TODO(env): replace single call with your loop) ────────
    conversation: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    fail_reason = ""
    predicted = ""
    try:
        # Target-side client API (css/model/client.py):
        #   complete_target(system, user)            -> str   (single-shot)
        #   complete_target_messages(messages)       -> str   (multi-turn text)
        #   complete_target_tools(messages, tools)   -> dict  (function calling)
        text = client.complete_target(
            system, user, max_tokens=max_tokens, temperature=temperature
        )
        conversation.append({"role": "assistant", "content": text or ""})
        predicted = _extract_final_answer(text or "")
    except Exception as e:  # noqa: BLE001 — an agent crash is a failed rollout
        fail_reason = f"agent-error: {type(e).__name__}: {e}"

    # ── Scoring (AFTER the loop — the agent never sees the gold) ─────────
    # TODO(env): replace exact match with your evaluator (execution accuracy,
    # test cases, judge, ...). Populate soft with partial credit if available.
    if fail_reason:
        hard = 0
        soft = 0.0
    else:
        hard = int(_normalize(predicted) == _normalize(gold))
        soft = float(hard)
        if not hard:
            fail_reason = "answer mismatch"

    return {
        "hard": hard,
        "soft": soft,
        "n_turns": 1,                 # TODO(env): count your loop's turns
        "fail_reason": fail_reason,
        "conversation": conversation,
        # Env-specific extras (absorbed into TaskResult.extras):
        "predicted_answer": predicted,
        "gold_answer": gold,
    }
