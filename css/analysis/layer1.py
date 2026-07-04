"""Layer 1 — per-trajectory behavioral arc annotation.

Layer 1 is the entry point of the analysis pipeline: it reads raw trajectories
and produces structured, evidence-cited behavioral observations that capture
HOW the agent acts — its phases, action patterns, transitions, and decision
points — rather than only how it "thinks."

Two analysts run here, both driven by the *optimizer* LLM (never the frozen
target):

  * :func:`annotate_trajectory` — annotates a single trajectory's behavioral
    arc: what phases the agent went through, what actions it took in each,
    and which phase-level behaviors were critical to the outcome.  The
    ``cognitive_aspect`` field (kept for data-structure compat) now captures
    generalizable *behavioral pattern* labels (e.g. "premature solution
    attempt without data exploration") — the taxonomy emerges bottom-up in
    Layer 2 clustering.

  * :func:`annotate_contrastive_pair` — a same-task (success, failure) pair
    analyst.  It isolates the behavioral ARC DIVERGENCE: at which phase the
    two runs' action sequences first parted ways.

:func:`run_layer1` orchestrates both over a batch of rollout groups, stamping
provenance (``node_id`` / ``epoch`` / ``polarity``) onto every observation.

Robustness contract: a malformed LLM response NEVER crashes the pipeline. The
offending trajectory or pair simply contributes no observations / no divergence.

LLM imports are kept light here (we only depend on the :class:`LLMClient`
protocol surface).  Heavy deps (embeddings / faiss) live in sibling modules and
are imported lazily there, not here.
"""
from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from css.data.pattern import Observation, Significance
from css.model.json_repair import complete_optimizer_json
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
You are annotating an agent's task-solving trajectory to capture its BEHAVIORAL \
ARC — the sequence of phases the agent went through and the action patterns \
within each phase.

Your annotations feed a downstream system that clusters similar behavioral \
patterns across many trajectories to discover recurring ARC TYPES (e.g. \
"explore-then-commit", "immediate-attempt-then-fix"). Therefore:

- Each observation should capture ONE distinct phase-level behavioral pattern — \
what the agent DID (its observable actions and decisions), not just what it \
thought.
- The "cognitive_aspect" field is the clustering key: name the behavioral \
pattern in generalizable language so the same pattern from a different \
trajectory gets a similar label. Focus on ACTION-LEVEL patterns (what the \
agent did and in what order), not abstract cognitive tendencies.
- Assess whether the behavior HELPED or HINDERED task completion ("polarity").

WHAT TO ANNOTATE — trace the trajectory as a sequence of PHASES and find the \
behavioral patterns that matter:
- What PHASES did the agent go through? (e.g. understanding the task → \
exploring data → attempting a solution → handling errors → verifying → \
submitting). How much of the trajectory was spent in each?
- Within each phase, what ACTION PATTERNS were notable? (e.g. the agent \
explored broadly vs narrowly, committed early vs late, verified vs did not)
- Where were the critical TRANSITION POINTS — moments where the agent shifted \
from one phase to another, or where it should have shifted but did not?
- Did the overall BEHAVIORAL ARC suit the task? Was the problem in the arc \
itself (wrong phase structure or ordering) or in the execution details within \
a sound arc (wrong API usage, format errors)?

DIG INTO THE TRAJECTORY. Do not give abstract labels — trace what actually \
happened. Reference the agent's specific actions, tool calls, and their results. \
Your analysis must be grounded in concrete trajectory content: if you cannot \
point to a specific action or decision, you do not have an observation.

For each observation:
- "what": a thorough analysis of a specific phase-level behavior, grounded in \
this trajectory — reference specific actions (tool calls, code, queries), their \
results, the agent's response to those results, and the consequence for the \
overall trajectory arc.
- "cognitive_aspect": a generalizable behavioral pattern label — describe what \
the agent did at the level of its approach/strategy (not task-specific details). \
Examples: "Extensive data exploration before solution attempt", "Immediate \
solution without schema inspection", "Error-driven iterative refinement", \
"Single-attempt submission without verification".
- "evidence": specific actions, tool calls, and their outcomes from the trajectory.
- "consequence": how this behavior affected the trajectory's outcome — trace \
the causal chain.
- "polarity": "positive" if this behavior contributed to success, "negative" \
if it contributed to failure.
- "significance": "critical" if this behavior plausibly determined the outcome \
(a different action here would likely have changed success/failure), "notable" \
if it was a secondary factor.

Your output is the ONLY record of this trajectory analysis. Be thorough — a \
pattern you miss cannot be recovered by downstream systems. Report every \
distinct behavioral pattern you observe (typically 3–8 per trajectory).

Output ONLY a JSON list, each element:
  {
    "what": "<thorough analysis of a phase-level behavior, grounded in specific \
trajectory actions>",
    "cognitive_aspect": "<generalizable behavioral pattern label for clustering>",
    "evidence": "<specific actions, tool calls, and outcomes from the trajectory>",
    "consequence": "<how this behavior affected the outcome, with causal chain>",
    "polarity": "positive" | "negative",
    "significance": "critical" | "notable"
  }
No prose, no markdown fences, no commentary — just the JSON list."""

# json_list_wrap variant. response_format={"type": "json_object"} grammar-forbids
# a top-level array, so the "JSON list" instruction is unsatisfiable and the model
# emits a single bare element (measured: exactly 1 observation per trajectory on
# every json-mode run). The wrapped form asks for an object the grammar CAN
# produce; :func:`_coerce_obs_list` already unwraps the "observations" key.
_SINGLE_SYSTEM_WRAPPED = _SINGLE_SYSTEM.replace(
    "Output ONLY a JSON list, each element:",
    'Output ONLY a single JSON object of the form {"observations": [<element>, '
    "<element>, ...]}, where each <element> is:",
).replace(
    "just the JSON list.",
    "just the JSON object.",
)
assert _SINGLE_SYSTEM_WRAPPED != _SINGLE_SYSTEM  # anchor-drift guard

_SINGLE_USER_TMPL = """\
Task id: {task_id}
Rollout index: {rollout_index}
Outcome: {outcome}{fail_reason}{task_desc}{env_context}

Trajectory (the agent's full conversation):
-------------------------------------------
{trajectory}
-------------------------------------------

Analyze HOW this agent thinks. Dig into the trajectory's specific content — \
trace the agent's actual reasoning, decisions, and their consequences. Ground \
every observation in concrete trajectory moments. Respond with ONLY the JSON \
{json_shape} described in the instructions."""


def _fmt_env_context(env_context: str) -> str:
    """Render the optional environment-context block (no outer newlines)."""
    text = (env_context or "").strip()
    if not text:
        return ""
    return (
        "Environment context (how this environment works):\n"
        "-------------------------------------------\n"
        f"{text}\n"
        "-------------------------------------------"
    )


# ── Prompt: same-task contrastive (success vs failure) analysis ──────────────

_CONTRASTIVE_SYSTEM = """\
You are comparing two trajectories of the SAME task under the SAME skill \
document: one rollout SUCCEEDED and one FAILED. Because the task, instructions, \
and skill are identical, the difference in outcome must come from a difference \
in the agents' BEHAVIORAL ARC — what they did, in what order, and how they \
responded to intermediate results. Your job is to isolate the decisive \
behavioral divergence.

Do not list every surface difference. Find the ARC DIVERGENCE: the point where \
the two runs' ACTION SEQUENCES first parted ways in a manner that explains the \
opposite outcomes. Focus on WHAT THEY DID (actions, tool calls, their ordering) \
rather than what they thought in isolation.

DIG INTO BOTH TRAJECTORIES. Reference specific actions, tool calls, and their \
results from each run. Show exactly what the success run did differently — which \
phase it was in, what action it took, how it responded to the result — and \
trace how that behavioral difference propagated to the opposite outcomes.

Classify the divergence:
- "approach_difference": the two runs followed different BEHAVIORAL ARCS — \
different phases, different ordering, different overall approach. This is an L1 \
(paradigm-level) difference that a paradigm change could address.
- "execution_difference": the two runs followed a similar arc but one made a \
specific mistake in execution detail (wrong API argument, syntax error, missed \
edge case). This is an L0 (tactical-level) difference.

Output ONLY a single JSON object:
  {
    "divergence_point": "<the specific trajectory moment where the two runs' \
action sequences first diverged — reference the actual actions/tool calls from \
both runs>",
    "cognitive_difference": "<a detailed analysis of the behavioral difference \
that explains success vs failure — trace actions and their consequences in both \
runs from the divergence point to the outcome>",
    "is_systematic": true | false,
    "divergence_level": "approach_difference" | "execution_difference"
  }
Set "is_systematic" to true only if this difference is a recurring, \
generalizable behavioral pattern (not a one-off slip or luck). \
No prose, no markdown fences — just the JSON object."""

_CONTRASTIVE_USER_TMPL = """\
{env_context}{pair}

Identify the decisive cognitive difference between the SUCCESS and the FAILURE. \
Dig into the specific trajectory content — trace what each run actually did at \
the critical moment. Respond with ONLY the JSON object described in the \
instructions."""


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

    Tolerates a bare array, a fenced array, a single bare object, an object
    wrapping the list under ``observations`` / ``items``, or NDJSON
    (newline-delimited JSON objects). Returns ``[]`` when nothing parseable
    is found rather than raising.
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

    # Fallback: NDJSON — multiple JSON objects concatenated with whitespace.
    # Many LLMs (e.g. Qwen3) return {...}\n{...}\n{...} instead of [{...}, ...].
    objs = _parse_ndjson_objects(text)
    if objs:
        return objs
    return []


def _parse_ndjson_objects(text: str) -> list[dict]:
    """Parse newline-delimited JSON objects from text.

    Uses ``json.JSONDecoder.raw_decode`` to consume concatenated objects
    separated by whitespace or commas. Returns only dicts that look like
    observation records (contain ``what`` or ``cognitive_aspect``).
    """
    if not text or not text.strip():
        return []
    decoder = json.JSONDecoder()
    results: list[dict] = []
    pos = 0
    length = len(text)
    while pos < length:
        while pos < length and text[pos] in " \t\n\r,":
            pos += 1
        if pos >= length:
            break
        try:
            obj, end = decoder.raw_decode(text, pos)
            pos = end
            if isinstance(obj, dict) and ("what" in obj or "cognitive_aspect" in obj):
                results.append(obj)
        except json.JSONDecodeError:
            pos += 1
    return results


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
    env_context: str = "",
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
    wrap = bool(getattr(cfg, "json_list_wrap", False))
    env_block = _fmt_env_context(env_context)
    user = _SINGLE_USER_TMPL.format(
        task_id=result.task_id,
        rollout_index=result.rollout_index,
        outcome=outcome,
        fail_reason=fail_reason,
        task_desc=task_desc,
        env_context=("\n" + env_block) if env_block else "",
        trajectory=trajectory,
        json_shape="object" if wrap else "list",
    )
    system = _SINGLE_SYSTEM_WRAPPED if wrap else _SINGLE_SYSTEM

    from css.tracing import stage_context
    try:
        with stage_context(client, "layer1_annotate"):
            obs_list = complete_optimizer_json(
                client, system, user, parse=_parse_obs_list,
                max_tokens=8192, stage="obs",
            )
    except Exception:
        return []

    outcome_polarity = "success" if result.passed else "failure"
    observations: list[Observation] = []
    for i, raw in enumerate(obs_list):
        what = str(raw.get("what", "")).strip()
        aspect = str(raw.get("cognitive_aspect", "")).strip()
        if not what and not aspect:
            continue
        llm_polarity = str(raw.get("polarity", "")).strip().lower()
        if llm_polarity == "positive":
            polarity = "success"
        elif llm_polarity == "negative":
            polarity = "failure"
        else:
            polarity = outcome_polarity
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
    env_context: str = "",
) -> "ContrastiveDivergence | None":
    """Analyze one same-task (success, failure) pair for its decisive divergence.

    Returns a :class:`ContrastiveDivergence`, or ``None`` if the LLM output is
    malformed/empty. Never raises.
    """
    pair = format_contrastive_pair(success, failure, tool_trunc=cfg.tool_trunc)
    env_block = _fmt_env_context(env_context)
    user = _CONTRASTIVE_USER_TMPL.format(
        env_context=(env_block + "\n\n") if env_block else "",
        pair=pair,
    )

    from css.tracing import stage_context
    try:
        with stage_context(client, "layer1_contrastive"):
            obj = complete_optimizer_json(
                client, _CONTRASTIVE_SYSTEM, user, parse=_parse_divergence,
                stage="divergence",
            )
    except Exception:
        return None

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
    env_context: str = "",
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
    from concurrent.futures import ThreadPoolExecutor, as_completed

    max_workers = getattr(cfg, "max_api_workers", 32)

    all_rollouts = [(group, rollout) for group in groups for rollout in group.rollouts]
    all_pairs = [
        (success, failure)
        for group in groups
        for success, failure in group.contrastive_pairs()
    ]

    observations: list[Observation] = []
    divergences: list[ContrastiveDivergence] = []

    def _annotate_one(rollout):
        return annotate_trajectory(client, rollout, cfg=cfg, env_context=env_context)

    def _annotate_pair(pair):
        return annotate_contrastive_pair(
            client, pair[0], pair[1], cfg=cfg, env_context=env_context
        )

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        obs_futures = {pool.submit(_annotate_one, r): r for _, r in all_rollouts}
        pair_futures = {pool.submit(_annotate_pair, p): p for p in all_pairs}

        for fut in as_completed(obs_futures):
            obs = fut.result()
            for o in obs:
                o.node_id = node_id
                o.epoch = epoch
            observations.extend(obs)

        for fut in as_completed(pair_futures):
            div = fut.result()
            if div is not None:
                divergences.append(div)

    return observations, divergences
