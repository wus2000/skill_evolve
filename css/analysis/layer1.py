"""Layer 1 — per-trajectory cognitive annotation (design §4.3 / D4).

Layer 1 is the entry point of the analysis pipeline: it reads raw trajectories
and produces structured, evidence-cited cognitive observations. Two analysts run
here, both driven by the *optimizer* LLM (never the frozen target):

  * :func:`annotate_trajectory` — an OPEN-ENDED single-trajectory analyst. It
    observes *how the agent THINKS* (planning, verification, assumption-handling,
    recovery, …) and — critically — **names its own** ``cognitive_aspect`` for
    each observation rather than picking from a fixed list. CSS deliberately
    predefines no cognitive dimensions (D4); the taxonomy emerges bottom-up in
    Layer 2 clustering, so Layer 1 must not constrain it.

  * :func:`annotate_contrastive_pair` — a same-task (success, failure) analyst.
    Task, instruction, and skill are held constant, so the pair isolates the one
    cognitive difference that flipped the outcome (D5). It yields a single
    :class:`ContrastiveDivergence`.

:func:`run_layer1` orchestrates both over a batch of rollout groups, stamping
provenance (``node_id`` / ``epoch`` / ``polarity``) onto every observation.

Robustness contract: a malformed LLM response NEVER crashes the pipeline. The
offending trajectory or pair simply contributes no observations / no divergence.

LLM imports are kept light here (we only depend on the :class:`LLMClient`
protocol surface). Heavy deps (embeddings / faiss) live in sibling modules and
are imported lazily there, not here.
"""
from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from css.data.pattern import Observation, Significance
from css.rollout.contrastive import (
    ContrastiveDivergence,
    format_contrastive_pair,
)
from css.trajectory import format_trajectory

if TYPE_CHECKING:  # pragma: no cover - type-only imports
    from css.config import CSSConfig
    from css.data.rollout import TaskResult, TaskRolloutGroup
    from css.model.client import LLMClient


# ── Prompt: single-trajectory open-ended cognitive annotation ────────────────

_SINGLE_SYSTEM = """\
You are a cognitive analyst studying HOW an AI agent thinks while it solves a \
task. You are given one full trajectory (the agent's own conversation: its \
reasoning, tool calls, and the results it saw). Your job is NOT to summarize \
what the agent did, nor to grade the answer. Your job is to characterize the \
agent's *thinking* — the cognitive habits, strategies, and decision tendencies \
that the trajectory reveals.

Focus on the agent's MIND, not its actions. For example you might notice how it \
plans before acting, whether and how it verifies its own work, how it handles \
assumptions and ambiguity, how it reacts to errors or unexpected tool output, \
whether it commits early to one interpretation, how it decomposes the problem, \
how it manages attention across a long context, or how it decides it is done. \
These are ONLY examples to prime you — do NOT treat them as a checklist or a \
fixed vocabulary.

CRITICAL — open-ended naming: for each observation you must invent your OWN \
short, precise label for the cognitive aspect you saw (the "cognitive_aspect" \
field). Name the specific thinking habit in your own words; do NOT pick from a \
predefined list, and do NOT default to generic buckets like "planning" or \
"verification" when a sharper, more specific name fits what actually happened.

Every observation MUST cite concrete evidence: a short quote or close paraphrase \
of the exact trajectory moment that shows the cognitive aspect. Vague claims \
with no traceable evidence are useless — omit them.

Mark each observation's significance:
  - "critical": this thinking habit plausibly determined the outcome (it is the \
    kind of thing worth changing the agent's strategy over).
  - "notable": a real, citable cognitive tendency, but secondary to the outcome.

Report only genuine, well-supported observations (typically 2–6). Quality over \
quantity; do not pad.

Output ONLY a JSON list, each element:
  {
    "what": "<what the agent thought/did, described at the cognitive level>",
    "cognitive_aspect": "<your own concise label for the thinking habit>",
    "evidence": "<short quote or close paraphrase from the trajectory>",
    "consequence": "<the outcome this thinking led to in this trajectory>",
    "significance": "critical" | "notable"
  }
No prose, no markdown fences, no commentary — just the JSON list."""

_SINGLE_USER_TMPL = """\
Task id: {task_id}
Rollout index: {rollout_index}
Outcome: {outcome}{fail_reason}{task_desc}

Trajectory (the agent's full conversation):
-------------------------------------------
{trajectory}
-------------------------------------------

Analyze HOW this agent thinks. Name your own cognitive aspects and cite \
evidence. Respond with ONLY the JSON list described in the instructions."""


# ── Prompt: same-task contrastive (success vs failure) analysis ──────────────

_CONTRASTIVE_SYSTEM = """\
You are a cognitive analyst comparing two trajectories of the SAME task under \
the SAME skill document: one rollout SUCCEEDED and one FAILED. Because the task, \
instructions, and skill are identical, any difference in outcome must come from \
a difference in how the two runs *thought* or *decided*. Your job is to isolate \
that single decisive cognitive difference.

Do not list every surface difference. Find the ONE divergence that mattered: the \
moment where the two runs' reasoning or strategy first parted ways in a manner \
that explains the opposite outcomes.

Output ONLY a single JSON object:
  {
    "divergence_point": "<where/when the two runs first meaningfully diverged>",
    "cognitive_difference": "<the difference in thinking/strategy that explains \
the success vs the failure>",
    "is_systematic": true | false
  }
Set "is_systematic" to true only if this difference looks like a recurring, \
generalizable cognitive pattern worth changing the agent's strategy over (not a \
one-off slip or luck). No prose, no markdown fences — just the JSON object."""

_CONTRASTIVE_USER_TMPL = """\
{pair}

Identify the single decisive cognitive difference between the SUCCESS and the \
FAILURE. Respond with ONLY the JSON object described in the instructions."""


# ── Robust JSON extraction (mirrors css/optimizer/reflect.py conventions) ────

def _json_candidates(text: str) -> list[str]:
    """Yield candidate JSON substrings from noisy LLM output (best-first)."""
    if not text:
        return []
    candidates: list[str] = []
    # 1) fenced ```json ... ``` blocks
    for m in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.DOTALL):
        candidates.append(m.group(1).strip())
    # 2) the whole text (already-clean array/object)
    candidates.append(text.strip())
    # 3) first bare [...] array
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        candidates.append(m.group(0))
    # 4) first bare {...} object
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        candidates.append(m.group(0))
    return candidates


def _parse_obs_list(text: str) -> list[dict]:
    """Extract a JSON list of observation dicts from noisy LLM output.

    Tolerates a bare array, a fenced array, a single bare object, or an object
    wrapping the list under ``observations`` / ``items``. Returns ``[]`` when
    nothing parseable is found rather than raising.
    """
    for cand in _json_candidates(text):
        if not cand:
            continue
        try:
            obj = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        coerced = _coerce_obs_list(obj)
        if coerced is not None:
            return coerced
    return []


def _coerce_obs_list(obj: Any) -> list[dict] | None:
    """Coerce a parsed JSON value into a list of observation dicts, or None."""
    if isinstance(obj, list):
        return [o for o in obj if isinstance(o, dict)]
    if isinstance(obj, dict):
        for key in ("observations", "items", "obs", "list"):
            inner = obj.get(key)
            if isinstance(inner, list):
                return [o for o in inner if isinstance(o, dict)]
        # A single observation emitted bare.
        if "what" in obj or "cognitive_aspect" in obj:
            return [obj]
    return None


def _parse_divergence(text: str) -> dict | None:
    """Extract the single contrastive-divergence object, or None."""
    for cand in _json_candidates(text):
        if not cand:
            continue
        try:
            obj = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, list):
            obj = next((o for o in obj if isinstance(o, dict)), None)
        if isinstance(obj, dict) and (
            "divergence_point" in obj or "cognitive_difference" in obj
        ):
            return obj
    return None


def _norm_significance(value: Any) -> "Significance":
    """Normalize an LLM significance value to the allowed literal."""
    s = str(value).strip().lower()
    return "critical" if s == "critical" else "notable"


# ── Analysts ─────────────────────────────────────────────────────────────────

def annotate_trajectory(
    client: "LLMClient",
    result: "TaskResult",
    *,
    cfg: "CSSConfig",
) -> list[Observation]:
    """Open-ended Layer-1 annotation of a single trajectory.

    Calls the optimizer once with an open-ended cognitive-analysis prompt and
    parses the response into :class:`Observation` objects. ``cognitive_aspect``
    is whatever the LLM named it. ``obs_id`` is the stable
    ``f"{task_id}:r{rollout_index}:{i}"``; ``polarity`` is derived from
    ``result.passed`` (success / failure). ``node_id`` and ``epoch`` are stamped
    by :func:`run_layer1` (left as the result's own values here).

    Never raises on malformed LLM output — returns ``[]`` for that trajectory.
    """
    trajectory = format_trajectory(result.messages, tool_trunc=cfg.tool_trunc)
    outcome = "PASSED (success)" if result.passed else "FAILED (failure)"
    fail_reason = (
        f"\nFailure reason: {result.fail_reason}"
        if (not result.passed and result.fail_reason)
        else ""
    )
    task_desc = (
        f"\nTask description: {result.task_description}"
        if result.task_description
        else ""
    )
    user = _SINGLE_USER_TMPL.format(
        task_id=result.task_id,
        rollout_index=result.rollout_index,
        outcome=outcome,
        fail_reason=fail_reason,
        task_desc=task_desc,
        trajectory=trajectory,
    )

    try:
        text, _usage = client.complete_optimizer(_SINGLE_SYSTEM, user)
    except Exception:
        return []

    polarity = "success" if result.passed else "failure"
    observations: list[Observation] = []
    for i, raw in enumerate(_parse_obs_list(text)):
        what = str(raw.get("what", "")).strip()
        aspect = str(raw.get("cognitive_aspect", "")).strip()
        # Skip empty / contentless entries — they carry no signal downstream.
        if not what and not aspect:
            continue
        observations.append(
            Observation(
                obs_id=f"{result.task_id}:r{result.rollout_index}:{i}",
                task_id=result.task_id,
                rollout_index=result.rollout_index,
                node_id=result.node_id,
                epoch=result.epoch,
                what=what,
                cognitive_aspect=aspect,
                evidence=str(raw.get("evidence", "")).strip(),
                consequence=str(raw.get("consequence", "")).strip(),
                significance=_norm_significance(raw.get("significance", "notable")),
                polarity=polarity,
            )
        )
    return observations


def annotate_contrastive_pair(
    client: "LLMClient",
    success: "TaskResult",
    failure: "TaskResult",
    *,
    cfg: "CSSConfig",
) -> "ContrastiveDivergence | None":
    """Analyze one same-task (success, failure) pair for its decisive divergence.

    Returns a :class:`ContrastiveDivergence`, or ``None`` if the LLM output is
    malformed/empty. Never raises.
    """
    pair = format_contrastive_pair(success, failure, tool_trunc=cfg.tool_trunc)
    user = _CONTRASTIVE_USER_TMPL.format(pair=pair)

    try:
        text, _usage = client.complete_optimizer(_CONTRASTIVE_SYSTEM, user)
    except Exception:
        return None

    obj = _parse_divergence(text)
    if obj is None:
        return None

    divergence_point = str(obj.get("divergence_point", "")).strip()
    cognitive_difference = str(obj.get("cognitive_difference", "")).strip()
    if not divergence_point and not cognitive_difference:
        return None

    is_systematic = obj.get("is_systematic", False)
    if isinstance(is_systematic, str):
        is_systematic = is_systematic.strip().lower() in ("true", "yes", "1")

    return ContrastiveDivergence(
        task_id=success.task_id,
        success_rollout_index=success.rollout_index,
        failure_rollout_index=failure.rollout_index,
        divergence_point=divergence_point,
        cognitive_difference=cognitive_difference,
        is_systematic=bool(is_systematic),
    )


def run_layer1(
    client: "LLMClient",
    groups: list["TaskRolloutGroup"],
    *,
    node_id: str,
    epoch: int,
    cfg: "CSSConfig",
) -> tuple[list[Observation], list[ContrastiveDivergence]]:
    """Run Layer 1 over a batch of rollout groups.

    For every rollout in every group: annotate the trajectory and stamp
    ``node_id`` / ``epoch`` (overriding whatever provenance the result carried)
    onto each resulting observation; polarity is already set from
    ``result.passed``. For every same-task (success, failure) pair: extract the
    contrastive divergence.

    Returns ``(observations, divergences)``. Malformed LLM output for any single
    rollout or pair is skipped, never fatal.
    """
    observations: list[Observation] = []
    divergences: list[ContrastiveDivergence] = []

    for group in groups:
        for rollout in group.rollouts:
            obs = annotate_trajectory(client, rollout, cfg=cfg)
            for o in obs:
                o.node_id = node_id
                o.epoch = epoch
            observations.extend(obs)

        for success, failure in group.contrastive_pairs():
            div = annotate_contrastive_pair(client, success, failure, cfg=cfg)
            if div is not None:
                divergences.append(div)

    return observations, divergences
