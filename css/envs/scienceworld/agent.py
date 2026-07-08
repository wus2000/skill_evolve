"""ScienceWorld task agent — full-history ReAct loop over a pooled in-process JVM env.

RETURN CONTRACT (see css/envs/template/agent.py): a plain dict with
``hard/soft/n_turns/fail_reason/conversation`` + env extras. The trajectory is
the canonical flat ``[{role, content:str}, ...]`` transcript; the eval annotation
is appended by ``task_interface.run_one`` (NOT here).

Protocol:
  * one LLM call per env step: ``<reasoning>...</reasoning><action>...</action>``
    (NOT ``<think>`` — a Qwen reserved token stripped at enable_thinking=false);
  * the action tag is the only rigid element; a missing tag falls back to the safe
    free action ``look around`` and is COUNTED, never silently repaired;
  * NO semantic repair / fuzzy matching — ungrammatical actions reach the env and
    fail visibly ("No known action ...") so reflection can attribute them;
  * ``check valid actions`` is intercepted (AgentBoard-faithful): it returns the
    generic action templates and consumes a step but does NOT move the simulator
    or update Progress Rate;
  * two scoring protocols share the loop (see scoring.py): ORIGINAL keys off the
    native 0-100 score; AGENTBOARD latches regex subgoals against observations.

GROUND-TRUTH FIREWALL: nothing here reads gold paths; the agent sees only the
goal, observations, and its own history.
"""
from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Any

from css.envs.scienceworld.prompts import build_first_user, build_system_prompt
from css.envs.scienceworld.scoring import (
    SubgoalMatcher,
    is_invalid_observation,
    native_hard,
    native_soft,
)

if TYPE_CHECKING:
    from css.model.client import LLMClient
    from css.envs.scienceworld.pool import PooledEnv

_ACTION_RE = re.compile(r"<action>(.*?)</action>", re.DOTALL | re.IGNORECASE)
_FALLBACK_ACTION = "look around"
_CHECK_VALID = "check valid actions"


def parse_action(response: str) -> "tuple[str, bool]":
    """Extract the command from an assistant reply.

    Returns ``(command, parse_ok)``. First ``<action>`` match wins; the content
    is stripped of surrounding quotes/backticks and a trailing period, reduced to
    its first non-empty line. A missing/empty tag falls back to ``look around``
    with ``parse_ok=False``. Case is PRESERVED (ScienceWorld object referents are
    not uniformly lowercase, unlike ALFWorld's grammar).
    """
    m = _ACTION_RE.search(response or "")
    if not m:
        return _FALLBACK_ACTION, False
    content = m.group(1).strip()
    for line in content.splitlines():
        prev = None
        while line != prev:  # peel nested quotes/periods/whitespace to a fixpoint
            prev = line
            line = line.strip().strip("`'\"").rstrip(".")
        if line:
            return line, True
    return _FALLBACK_ACTION, False


def valid_action_templates(env) -> "list[str]":
    """AgentBoard-faithful ``get_action_space(abstract=True)``: the generic action
    templates (minus reset), plus the ``check valid actions`` meta-action."""
    try:
        acts = [a for a in env.get_possible_actions() if "reset" not in a]
    except Exception:  # noqa: BLE001
        acts = []
    if _CHECK_VALID not in acts:
        acts.append(_CHECK_VALID)
    return acts


def run_scienceworld_agent(
    client: "LLMClient",
    pe: "PooledEnv",
    *,
    task_name: str,
    variation: int,
    simplification: str,
    step_budget: int,
    protocol: str,
    goal: str,
    subgoals: "list[str]",
    skill_text: str,
    max_tokens: int = 16384,
    temperature: "float | None" = None,  # None = client default (0.6)
    deadline_s: float = 540.0,
) -> "dict[str, Any]":
    """Play ONE episode on the given pooled env; return the RETURN CONTRACT dict.

    ``protocol`` is ``"agentboard"`` (subgoal SR/PR, ``goal``/``subgoals`` supplied)
    or ``"original"`` (native score, ``goal`` may be empty → the native task desc).
    ``engine_broken`` in the returned extras tells ``run_one`` to discard the env.
    """
    env = pe.env
    conversation: "list[dict]" = []
    matcher = SubgoalMatcher(subgoals) if protocol == "agentboard" else None
    n_turns = n_invalid = n_parse_fail = n_check = 0
    native_score = 0
    done = False
    engine_broken = False
    fail_reason = ""
    started = time.time()
    valid_templates: "list[str] | None" = None

    try:
        env.load(task_name, int(variation), simplification, generateGoldPath=False)
        obs, info = env.reset()
        native_score = int(info.get("score", 0))
        goal_text = (goal or "").strip() or env.get_task_description()
        system = build_system_prompt(skill_text)
        conversation.append({"role": "system", "content": system})
        conversation.append(
            {"role": "user", "content": build_first_user(goal_text, obs, env.inventory())})

        for _ in range(int(step_budget)):
            if pe.broken:
                fail_reason = "env-wedged (watchdog force-close)"
                engine_broken = True
                break
            if time.time() - started > deadline_s:
                fail_reason = "episode-deadline (%.0fs)" % deadline_s
                break
            reply = client.complete_target_messages(
                conversation, max_tokens=max_tokens, temperature=temperature)
            conversation.append({"role": "assistant", "content": reply or ""})
            action, parse_ok = parse_action(reply or "")
            if not parse_ok:
                n_parse_fail += 1

            # AgentBoard-faithful meta-action: return templates, no simulator move.
            if action.strip().lower() == _CHECK_VALID:
                n_check += 1
                n_turns += 1
                if valid_templates is None:
                    valid_templates = valid_action_templates(env)
                conversation.append({
                    "role": "user",
                    "content": "Choose an action from these valid actions: "
                               + ", ".join(valid_templates)})
                continue

            obs, _reward, done, info = env.step(action)
            native_score = int(info.get("score", native_score))
            n_turns += 1
            if is_invalid_observation(obs):
                n_invalid += 1
            if matcher is not None:
                matcher.update(obs)
            conversation.append({"role": "user", "content": obs})

            if protocol == "agentboard":
                if matcher.sr:
                    done = True
                    break
            elif done:  # native isCompleted: success OR step-limit OR negative score
                break
    except Exception as e:  # noqa: BLE001 — an engine/agent crash is a failed rollout
        engine_broken = True
        fail_reason = "engine-error: %s: %s" % (type(e).__name__, e)
        if not conversation:
            conversation = [{"role": "system", "content": ""}]

    # ── Scoring (AFTER the loop; the agent never saw the gold) ──────────────
    if protocol == "agentboard" and matcher is not None:
        hard = matcher.sr
        soft = matcher.pr
        if not hard and not fail_reason:
            fail_reason = "subgoals %d/%d" % (matcher.n_done, matcher.n_subgoals)
    else:
        hard = native_hard(native_score)
        soft = native_soft(native_score)
        if not hard and not fail_reason:
            fail_reason = "score=%d (need 100)" % native_score

    return {
        "hard": int(hard),
        "soft": float(soft),
        "n_cases": 1,
        "n_pass": int(hard),
        "n_turns": n_turns,
        "fail_reason": "" if hard else fail_reason,
        "conversation": conversation,
        # Env extras (absorbed into TaskResult.extras):
        "native_score": native_score,
        "n_invalid": n_invalid,
        "n_parse_fail": n_parse_fail,
        "n_check_valid": n_check,
        "engine_broken": engine_broken,
        "agentboard_pr": (matcher.pr if matcher is not None else None),
        "agentboard_sr": (matcher.sr if matcher is not None else None),
        "agentboard_subgoals_done": (matcher.n_done if matcher is not None else None),
        "agentboard_subgoals_total": (matcher.n_subgoals if matcher is not None else None),
        "elapsed_s": round(time.time() - started, 1),
    }
