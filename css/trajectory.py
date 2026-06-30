"""Trajectory rendering and lossless-ish truncation for analysis prompts.

Per design D7 the optimizer must see trajectories *faithfully*: reasoning and
action text is never clipped. The ONLY allowed reduction is eliding a single
oversized tool/observation message (content length >= ``tool_trunc``), and even
then we preserve a head and a tail with an explicit elision marker so the
optimizer can tell how much was removed. We deliberately do NOT port SkillOpt's
``fmt_minibatch_trajectories`` 500/800/2000-char clips (reflect.py:72-90), which
are lossy across every message type.

CANONICAL TRAJECTORY CONTRACT (the mechanism defines it; every env adapts to it).
``TaskResult.messages`` is a list of ``{"role": str, "content": str}`` turns — a
plain, readable transcript. The mechanism analyses trajectories ONLY in this
shape; it does not know, and must not be taught, any env's native agent format.
An env whose task-execution agent runs on a richer transport (e.g. OpenAI
function-calling, where an assistant turn carries ``tool_calls`` and a tool
result is a ``role: "tool"`` message) is responsible for FLATTENING that into
this readable ``{role, content}`` form before returning it — render each action
as text (``"Action: <name>\\n<args>"``) and each observation as its content.
That adaptation lives in the env, never here. ``content`` may also be a list of
content parts; only string content is measured/elided — structured content is
passed through verbatim (clipping it would corrupt structure).
"""
from __future__ import annotations

from typing import Any

# ── Ground-truth firewall ───────────────────────────────────────────────────
# CSS trains skill documents (strategy.md / rules.md) on the TRAIN split, where
# ground-truth answers exist for evaluation, then DEPLOYS them on TEST, where the
# agent has NO ground truth. It is fine — useful, even — for the OPTIMIZER to SEE
# ground truth while analysing a trajectory: that is how it understands what truly
# went wrong. The invariant is NOT "hide gt from the optimizer"; it is that NOTHING
# the optimizer PRODUCES (its analysis, the patterns it mines, and above all the
# strategy.md / rules.md it emits) may reference or depend on ground truth, because
# those artifacts are deployed where no ground truth exists. We therefore do NOT
# scrub trajectories; we enforce the invariant in every optimizer prompt via the
# clause below (injected centrally at the optimizer LLM entry point).

#: Canonical train/test firewall clause. Injected into EVERY optimizer system prompt
#: at the single optimizer LLM call site, so the optimizer may freely reason FROM
#: ground truth yet never emit an output that depends on it.
GROUND_TRUTH_FIREWALL = """\
TRAIN/TEST FIREWALL (non-negotiable). You are operating during TRAINING on the \
training split, where ground-truth / "expected" / golden answers may be visible to \
you (in evaluation notes, fail reasons, or trajectories). It is fine to LOOK at them \
to understand what truly went wrong. But everything you PRODUCE — your analysis, the \
patterns you mine, and above all the strategy.md / rules.md that result — is DEPLOYED \
at TEST time, where the agent has NO ground truth and sees only the task instruction \
and the input. Therefore nothing you output may reference, depend on, compare against, \
validate with, or instruct the agent to use expected / ground-truth / golden / \
"Expected Results" values. Reason FROM ground truth privately if it helps your \
diagnosis, but the agent can NEVER access it at run time, so any analysis, pattern, \
strategy, or rule you emit that requires it is invalid by construction. State your \
findings in terms of what the agent can observe from the task and input alone."""


def _content_str(content: Any) -> str | None:
    """Return ``content`` if it is a plain string, else ``None``.

    Only plain-string content participates in truncation; list/structured
    content is left untouched.
    """
    return content if isinstance(content, str) else None


def truncate_tool_results(messages: list[dict], tool_trunc: int) -> list[dict]:
    """Elide only oversized single tool/observation messages.

    A message is replaced ONLY when its ``content`` is a string of length
    ``>= tool_trunc``. In that case the content becomes::

        head[:tool_trunc // 2] + "\\n...[truncated N chars]...\\n" + tail[-tool_trunc // 2:]

    where ``N`` is the number of characters dropped. Every shorter message and
    all non-string (structured) content is copied byte-identically. The input
    list is not mutated; new dicts are returned for elided messages and the
    originals are reused otherwise.

    ``tool_trunc <= 0`` disables truncation (returns messages unchanged) rather
    than nuking every message to a bare marker — a misconfigured threshold must
    never silently destroy content.
    """
    if tool_trunc <= 0:
        return list(messages)
    half = tool_trunc // 2
    out: list[dict] = []
    for msg in messages:
        content = _content_str(msg.get("content"))
        if content is not None and len(content) >= tool_trunc:
            head = content[:half]
            tail = content[-half:] if half > 0 else ""
            dropped = len(content) - len(head) - len(tail)
            new_content = f"{head}\n...[truncated {dropped} chars]...\n{tail}"
            new_msg = dict(msg)
            new_msg["content"] = new_content
            out.append(new_msg)
        else:
            out.append(msg)
    return out


# ── Post-rollout evaluation annotation (the mechanism<->env contract) ───────
# Trajectory analysis must reason about CORRECTNESS, which needs the evaluation
# outcome and the ground-truth reference. The task-execution agent is firewalled
# from ground truth DURING rollout; but AFTER evaluation each env appends ONE
# annotation message carrying the outcome + ground truth, for the optimizer's
# analysis only. The agent never saw it; the GROUND_TRUTH_FIREWALL (injected into
# every optimizer prompt) keeps optimizer OUTPUTS from depending on it.
# SpreadsheetBench is the reference implementation (its trailing
# "[POST-EXECUTION VERIFICATION]" message); new envs use
# :func:`eval_annotation_message` for a uniform shape.
#
# The annotation uses a DISTINCT role ("evaluation"), NOT "system", so it is never
# confused with the task agent's system prompt and is never dropped by
# ``include_system=False`` (which only drops a LEADING system message).

POST_ROLLOUT_EVAL_ROLE = "evaluation"
POST_ROLLOUT_EVAL_MARKER = (
    "[POST-ROLLOUT EVALUATION — analysis only; the task agent never saw this]"
)


def eval_annotation_message(*, outcome: str, ground_truth: str = "", detail: str = "") -> dict:
    """Build the unified post-rollout eval+GT annotation appended to a trajectory.

    Returns a canonical ``{role, content:str}`` message (role
    :data:`POST_ROLLOUT_EVAL_ROLE`) that the env appends as the LAST message of
    ``TaskResult.messages``. ``outcome`` is the scored result (e.g. "EX=1
    (pass)"); ``ground_truth`` is the expected answer the env exposes for analysis
    (e.g. a gold SQL); ``detail`` is any extra note (e.g. a fail reason).
    """
    body = f"{POST_ROLLOUT_EVAL_MARKER}\n\nOutcome: {outcome}"
    if detail:
        body += f"\n{detail}"
    if ground_truth:
        body += f"\n\nGround truth (expected answer — analysis only):\n{ground_truth}"
    return {"role": POST_ROLLOUT_EVAL_ROLE, "content": body}


def format_trajectory(
    messages: list[dict], *, tool_trunc: int = 4000, include_system: bool = True
) -> str:
    """Render messages as a readable transcript for analysis prompts.

    Applies :func:`truncate_tool_results` first, then emits ``"[role]\\ncontent"``
    blocks joined by blank lines. Structured (list) content is stringified for
    display only.

    ``include_system`` (default True) keeps the full transcript — including the
    leading system prompt that carries the injected skill (strategy + rules) and
    the ReAct action protocol. Pass ``include_system=False`` to drop ONLY that
    leading system message (any later system message — e.g. a post-execution
    verification note appended at the end — is preserved). The L0 reflect stage
    uses this: it already receives the current ``rules.md`` separately, so
    repeating the large, identical system prompt across all eight minibatch
    trajectories would only burn context to no benefit.
    """
    msgs = list(messages)
    if not include_system and msgs and str(msgs[0].get("role", "")) == "system":
        msgs = msgs[1:]
    truncated = truncate_tool_results(msgs, tool_trunc)
    blocks: list[str] = []
    for msg in truncated:
        role = str(msg.get("role", ""))
        content = msg.get("content", "")
        if not isinstance(content, str):
            content = str(content)
        blocks.append(f"[{role}]\n{content}")
    return "\n\n".join(blocks)
