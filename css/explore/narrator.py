"""Faithful narration of a probe trajectory (design §3.3).

A probe is one dispatched behavior prompt run as ``k`` real rollouts of one task.
The narrator renders a REPRESENTATIVE rollout's trajectory into an objective,
chronological account whose turn coverage is MECHANICALLY guaranteed:

  * every turn gets an anchor ``[tN]``; after the LLM narrates we check that each
    anchor is present;
  * if any are missing we regenerate ONCE, telling the model exactly which anchors
    it dropped;
  * if some are still missing we append a mechanical stub line per missing turn,
    built from the raw trajectory — so coverage is guaranteed regardless of the
    model.

The narration BODY is the model's job (narrate, don't judge). The harness then
appends two factual, labeled sections it controls: the ``Evaluation`` verdicts of
the k rollouts, and — only when the env exposes an instance-specific gold/approach
hint — a ``Reference approach (instance-specific)`` section. Those are never left
to the model, so the verdicts are always accurate and present.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from css.explore.prompts import NARRATOR_SYSTEM, narrator_regen_note
from css.trajectory import POST_ROLLOUT_EVAL_ROLE

if TYPE_CHECKING:  # pragma: no cover - typing only
    from css.data.rollout import TaskResult, TaskRolloutGroup

_SYSTEM_ROLE = "system"
_ASSISTANT_ROLE = "assistant"


# ── Turn extraction ───────────────────────────────────────────────────────────
def _content_text(msg: dict) -> str:
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    return str(content)


def _core_messages(messages: "list[dict]") -> "list[dict]":
    """Drop the leading system prompt and any trailing eval annotation(s).

    Those two are harness scaffolding, not agent turns: the system message
    carries the injected behavior prompt + action protocol, and the trailing
    :data:`POST_ROLLOUT_EVAL_ROLE` message is the post-rollout ground-truth
    annotation the agent never saw.
    """
    core = list(messages or [])
    if core and str(core[0].get("role", "")) == _SYSTEM_ROLE:
        core = core[1:]
    while core and str(core[-1].get("role", "")) == POST_ROLLOUT_EVAL_ROLE:
        core = core[:-1]
    return core


def _extract_turns(messages: "list[dict]") -> "list[dict]":
    """Split a trajectory into ``[{action, obs:[...]}]`` turns.

    A turn starts at each assistant (action) message and absorbs the following
    non-assistant (observation) messages until the next action. A leading
    non-assistant message (e.g. the initial task prompt) becomes turn 0 on its
    own. Turn index == position in the returned list.
    """
    core = _core_messages(messages)
    turns: "list[dict]" = []
    cur: "dict | None" = None
    for m in core:
        role = str(m.get("role", ""))
        if role == _ASSISTANT_ROLE:
            if cur is not None:
                turns.append(cur)
            cur = {"action": m, "obs": []}
        else:
            if cur is None:
                turns.append({"action": m, "obs": []})
            else:
                cur["obs"].append(m)
    if cur is not None:
        turns.append(cur)
    return turns


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def _render_turns_for_prompt(turns: "list[dict]", *, tool_trunc: int = 4000) -> str:
    """Render turns with their anchors for the narrator prompt."""
    blocks: "list[str]" = []
    for i, turn in enumerate(turns):
        action = turn["action"]
        a_role = str(action.get("role", ""))
        a_text = _content_text(action)
        if len(a_text) > tool_trunc:
            a_text = a_text[: tool_trunc // 2] + "\n…[truncated]…\n" + a_text[-tool_trunc // 2 :]
        lines = ["[t%d] %s:" % (i, a_role), a_text]
        for obs in turn["obs"]:
            o_role = str(obs.get("role", ""))
            o_text = _content_text(obs)
            if len(o_text) > tool_trunc:
                o_text = o_text[: tool_trunc // 2] + "\n…[truncated]…\n" + o_text[-tool_trunc // 2 :]
            lines.append("    observation (%s): %s" % (o_role, o_text))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _stub_line(turn: "dict", index: int) -> str:
    """Mechanical fallback narration for a turn the model refused to cover.

    Built straight from the raw trajectory so short errors / identifiers survive
    verbatim (they are the load-bearing detail the contract protects).
    """
    action = turn["action"]
    a_role = str(action.get("role", ""))
    a_text = _clip(_content_text(action), 300)
    obs_texts = [_clip(_content_text(o), 300) for o in turn["obs"]]
    obs_summary = " | ".join(t for t in obs_texts if t) or "(none)"
    return "[t%d] (action: %s: %s; observation summary: %s)" % (
        index, a_role, a_text or "(empty)", obs_summary,
    )


def _missing_anchors(narration: str, n_turns: int) -> "list[int]":
    """Turn indices whose ``[tN]`` anchor is absent from ``narration``."""
    text = narration or ""
    return [i for i in range(n_turns) if ("[t%d]" % i) not in text]


# ── Evaluation / reference sections (harness-owned, always factual) ───────────
def _verdict(r: "TaskResult") -> dict:
    return {
        "rollout_index": int(getattr(r, "rollout_index", 0)),
        "passed": bool(getattr(r, "passed", False)),
        "soft": float(getattr(r, "soft", 0.0)),
        "fail_reason": str(getattr(r, "fail_reason", "") or ""),
    }


def _evaluation_section(rollouts: "list[TaskResult]") -> str:
    n_pass = sum(1 for r in rollouts if getattr(r, "passed", False))
    lines = ["## Evaluation", "%d of %d rollout(s) passed." % (n_pass, len(rollouts))]
    for r in rollouts:
        v = _verdict(r)
        tag = "PASS" if v["passed"] else "FAIL"
        line = "- rollout %d: %s (soft=%.3f)" % (v["rollout_index"], tag, v["soft"])
        if not v["passed"] and v["fail_reason"]:
            line += " — %s" % _clip(v["fail_reason"], 300)
        lines.append(line)
    return "\n".join(lines)


def _reference_section(env: Any, item: dict) -> str:
    """Optional gold/approach gist, ONLY if the env exposes the hook.

    Per-env wiring lands in a later phase; here we probe ``env.gold_approach_gist``
    and, when present, render it as an explicitly instance-specific reference. Any
    failure omits the section rather than fabricating one.
    """
    hook = getattr(env, "gold_approach_gist", None)
    if hook is None:
        return ""
    try:
        gist = hook(item)
    except Exception:  # noqa: BLE001 — a broken hook must not break narration
        return ""
    if not gist or not str(gist).strip():
        return ""
    return (
        "## Reference approach (instance-specific)\n"
        "For THIS task instance only (not a general rule to copy): "
        + str(gist).strip()
    )


def _representative(group: "TaskRolloutGroup") -> "TaskResult | None":
    """Pick the rollout to narrate in full: the first PASS if any (a crack is the
    prize), else the first rollout. Deterministic."""
    rollouts = list(getattr(group, "rollouts", []))
    if not rollouts:
        return None
    for r in rollouts:
        if getattr(r, "passed", False):
            return r
    return rollouts[0]


# ── Public entry point ────────────────────────────────────────────────────────
def narrate_probe(
    group: "TaskRolloutGroup",
    *,
    purpose: str,
    optimizer_client: Any,
    env: Any,
    item: dict,
    cfg: Any,
) -> str:
    """Narrate one probe's representative trajectory + append eval/reference.

    Never raises: on a total narration failure the mechanical stub narration
    (full coverage from the raw trajectory) is returned with the sections.
    """
    rollouts = list(getattr(group, "rollouts", []))
    rep = _representative(group)
    tool_trunc = int(getattr(cfg, "tool_trunc", 8000))

    sections: "list[str]" = []
    if rep is None:
        sections.append("(no rollout was produced for this probe)")
    else:
        turns = _extract_turns(rep.messages)
        body = _narrate_body(turns, purpose=purpose, optimizer_client=optimizer_client,
                             tool_trunc=tool_trunc)
        sections.append(body)

    sections.append(_evaluation_section(rollouts))
    ref = _reference_section(env, item)
    if ref:
        sections.append(ref)
    return "\n\n".join(sections)


def _narrate_body(
    turns: "list[dict]", *, purpose: str, optimizer_client: Any, tool_trunc: int,
) -> str:
    """Produce the chronological narration body with guaranteed anchor coverage."""
    if not turns:
        return "(empty trajectory: the rollout produced no agent turns)"

    n = len(turns)
    rendered = _render_turns_for_prompt(turns, tool_trunc=tool_trunc)
    user = (
        "PROBE PURPOSE (focus your narration on what bears on this):\n"
        + (purpose.strip() if purpose and purpose.strip() else "(none stated)")
        + "\n\nTRAJECTORY — "
        + str(n)
        + " turns, anchors [t0]…[t%d]:\n\n" % (n - 1)
        + rendered
        + "\n\nNarrate every turn in order, each tagged with its exact anchor [tN]."
    )

    narration = _call_narrator(optimizer_client, NARRATOR_SYSTEM, user, tool_trunc)
    missing = _missing_anchors(narration, n)
    if missing:
        # One regeneration, naming exactly the anchors that were dropped.
        note = narrator_regen_note(["[t%d]" % i for i in missing])
        narration2 = _call_narrator(
            optimizer_client, NARRATOR_SYSTEM, user + "\n\n" + note, tool_trunc
        )
        # Keep whichever pass covered more turns.
        if len(_missing_anchors(narration2, n)) < len(missing):
            narration = narration2
        missing = _missing_anchors(narration, n)

    if missing:
        # Mechanical guarantee: append a stub for each still-missing turn.
        stubs = [_stub_line(turns[i], i) for i in missing]
        narration = (
            (narration.rstrip() + "\n\n" if narration.strip() else "")
            + "Uncovered turns (mechanical stub, verbatim from trajectory):\n"
            + "\n".join(stubs)
        )
    return narration


def _call_narrator(optimizer_client: Any, system: str, user: str, tool_trunc: int) -> str:
    """One optimizer completion for the narration body; ``""`` on failure.

    Narration is prose, not JSON, so this goes through ``complete_optimizer``
    directly (not the JSON-repair wrapper).
    """
    # Size the completion to the trajectory: a faithful ~1-sentence-per-turn
    # account of a long rollout still needs room.
    max_tokens = 16384  # 16K completion floor (user ruling 2026-07-08)
    try:
        text, _usage = optimizer_client.complete_optimizer(system, user, max_tokens=max_tokens)
    except Exception:  # noqa: BLE001 — fall through to the stub guarantee
        return ""
    return text or ""
