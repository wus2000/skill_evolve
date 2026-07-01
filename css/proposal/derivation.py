"""Phase 5 Layer 5a/5b — derive a strategy change and validate it cheaply.

.. note:: LEGACY in v3. Cold start and L1 cycle no longer call this module;
   retained for test compatibility.

This module is the SECOND half of a PROPOSAL (the first half is
:mod:`css.proposal.root_cause`, Layer 4). The design's central claim (D4 / D10)
is that a good strategy change is a LOGICAL CONSEQUENCE of a correctly diagnosed
root cause — NOT free LLM "creativity". So nothing here generates a strategy
from a blank page; every output is *derived* from a :class:`RootCause` and the
paired SUCCESS counterpart patterns (the cases where the agent already thought
the right way and succeeded). The job is to SYSTEMATIZE those success behaviors
into the strategy document so they happen on purpose, every time.

Three public operations (the Layer 5a/5b contract):

  * :func:`derive_strategy` (Layer 5a, "derive not generate") — turns a
    ``RootCause`` + its counterpart success patterns into a full new strategy.md
    body (organized as ``###`` subsections) plus a rationale that shows the
    change is the logical consequence of the root cause's assumption-level
    finding.
  * :func:`check_negative_archive` (Layer 5b gate, "reminder not prohibition")
    — recalls the top-K most-similar ABANDONED strategies from the tree-global
    negative archive (Jaccard word-overlap on strategy text), and — if the top
    hit is too close — forces the LLM to articulate HOW the new direction
    differs. A high similarity does NOT veto; an inability to articulate a
    difference does.
  * :func:`retrospective_validate` (Layer 5b cheap pre-rollout check) — asks the
    LLM for (1) positive evidence in success trajectories, (2) counterfactuals on
    persistent failures, and (3) a coverage estimate in [0, 1], then applies the
    coverage gate against ``cfg.coverage_low`` / ``cfg.coverage_high``.

Robustness contract (mirrors css/proposal/root_cause.py and css/analysis):
malformed LLM output never crashes — the functions degrade to a documented safe
default rather than raising.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from css.model.json_repair import complete_optimizer_json
from css.proposal.root_cause import RootCause

if TYPE_CHECKING:  # pragma: no cover - type-only imports
    from css.config import CSSConfig
    from css.data.negative_archive import NegativeArchive
    from css.data.pattern import PatternRecord
    from css.data.rollout import TaskResult, TaskRolloutGroup
    from css.model.client import LLMClient


# ── Data model ───────────────────────────────────────────────────────────────

@dataclass
class StrategyProposal:
    """A derived new strategy body + the rationale that makes it a consequence.

    ``strategy_text`` is a full strategy.md body: a ``## Strategy Name`` header with
    an overview paragraph, followed by a ``### Details`` section with detailed expansion. ``rationale`` explains *why* this is
    the logical consequence of ``root_cause`` and which counterpart success
    behaviors it systematizes. ``targeted_pattern_ids`` are the L1 failure
    patterns the change is meant to suppress — carried through to the Layer 5c
    rollout so it can measure the TARGET pattern's occurrence-rate change.
    """

    strategy_text: str
    rationale: str
    root_cause: "RootCause"
    targeted_pattern_ids: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "StrategyProposal":
        rc_raw = d.get("root_cause", {})
        rc = rc_raw if isinstance(rc_raw, RootCause) else RootCause.from_dict(
            rc_raw if isinstance(rc_raw, dict) else {}
        )
        pids = d.get("targeted_pattern_ids", [])
        if isinstance(pids, str):
            pids = [pids]
        elif not isinstance(pids, list):
            pids = []
        return cls(
            strategy_text=str(d.get("strategy_text", "")),
            rationale=str(d.get("rationale", "")),
            root_cause=rc,
            targeted_pattern_ids=[str(p) for p in pids],
        )

    def to_dict(self) -> dict:
        return {
            "strategy_text": self.strategy_text,
            "rationale": self.rationale,
            "root_cause": self.root_cause.to_dict(),
            "targeted_pattern_ids": list(self.targeted_pattern_ids),
        }


@dataclass
class ValidationResult:
    """Result of the Layer 5b retrospective (pre-rollout) check.

    ``coverage`` is the LLM's estimate, clamped to [0, 1], of the fraction of the
    persistent-fail set the change would plausibly flip. ``verdict`` is the gate
    decision: ``"reconsider"`` when coverage is below ``cfg.coverage_low``,
    otherwise ``"proceed"``.
    """

    coverage: float
    verdict: str  # "proceed" | "reconsider"
    positive_evidence: list[str] = field(default_factory=list)
    counterfactuals: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "ValidationResult":
        verdict = str(d.get("verdict", "reconsider"))
        if verdict not in ("proceed", "reconsider"):
            verdict = "reconsider"
        pe = d.get("positive_evidence", [])
        cf = d.get("counterfactuals", [])
        if not isinstance(pe, list):
            pe = [str(pe)] if pe else []
        if not isinstance(cf, list):
            cf = [str(cf)] if cf else []
        try:
            cov = float(d.get("coverage", 0.0))
        except (TypeError, ValueError):
            cov = 0.0
        return cls(
            coverage=max(0.0, min(1.0, cov)),
            verdict=verdict,
            positive_evidence=[str(x) for x in pe],
            counterfactuals=[str(x) for x in cf],
        )

    def to_dict(self) -> dict:
        return {
            "coverage": self.coverage,
            "verdict": self.verdict,
            "positive_evidence": list(self.positive_evidence),
            "counterfactuals": list(self.counterfactuals),
        }


# ── Robust JSON extraction (mirrors css/proposal/root_cause.py conventions) ──

def _json_candidates(text: str) -> list[str]:
    """Yield candidate JSON substrings from noisy LLM output (best-first)."""
    if not text:
        return []
    candidates: list[str] = []
    for m in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.DOTALL):
        candidates.append(m.group(1).strip())
    candidates.append(text.strip())
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        candidates.append(m.group(0))
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        candidates.append(m.group(0))
    return candidates


def _parse_obj(text: str) -> dict:
    """Extract the first parseable JSON object from noisy LLM output.

    Returns ``{}`` when nothing object-shaped is found (never raises). A bare
    JSON list is wrapped under no key — callers that want a list should not use
    this helper.
    """
    for cand in _json_candidates(text):
        if not cand:
            continue
        try:
            obj = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            return obj
    return {}


# ── Rendering helpers ────────────────────────────────────────────────────────

def _fmt_root_cause(rc: "RootCause") -> str:
    """Render the four-level diagnosis as the grounding for the derivation."""
    lines = [
        f"  pattern_ids: {', '.join(rc.pattern_ids) or '(none)'}",
        f"  1. behavioral (what the agent did): {rc.behavioral}",
        f"  2. process (why, from its THOUGHT): {rc.process}",
        f"  3. strategy (the strategy text to blame): {rc.strategy}",
        f"  4. assumption (the hidden belief to break): {rc.assumption}",
    ]
    if rc.leverage:
        lines.append(f"  leverage: {rc.leverage}")
    if rc.l0_explanation:
        lines.append(f"  why L0 rules could not fix it: {rc.l0_explanation}")
    ev = rc.evidence or {}
    cited = [f"      {k}: {v}" for k, v in ev.items() if k != "_flag" and v]
    if cited:
        lines.append("  cited evidence:")
        lines.extend(cited)
    return "\n".join(lines)


def _fmt_success_behaviors(p: "PatternRecord", *, max_obs: int = 5) -> str:
    """Render the concrete success behaviors a counterpart pattern exhibits."""
    obs = sorted(
        p.observations,
        key=lambda o: 0 if o.significance == "critical" else 1,
    )[:max_obs]
    lines: list[str] = []
    for o in obs:
        what = (o.what or "").strip()
        ev = (o.evidence or "").strip()
        cons = (o.consequence or "").strip()
        line = f"      - {what}"
        if ev:
            line += f"\n        evidence: \"{ev}\""
        if cons:
            line += f"\n        led to: {cons}"
        lines.append(line)
    return "\n".join(lines) if lines else "      (no recorded success behaviors)"


def _fmt_counterpart_patterns(patterns: list["PatternRecord"]) -> str:
    """Render the SUCCESS counterpart patterns to systematize."""
    if not patterns:
        return (
            "  (no paired success counterpart was found — derive the change from "
            "the root cause's assumption-level finding alone, stating clearly "
            "what success behavior you are projecting and why.)"
        )
    blocks: list[str] = []
    for p in patterns:
        blocks.append(
            f"  [{p.pattern_id}] {p.name}\n"
            f"    cognitive_aspect: {p.cognitive_aspect}\n"
            f"    description: {p.description}\n"
            f"    success behaviors to systematize:\n{_fmt_success_behaviors(p)}"
        )
    return "\n\n".join(blocks)


def _result_brief(r: "TaskResult", *, max_chars: int = 600) -> str:
    """A compact one-trajectory brief for the retrospective prompt."""
    # Keep it cheap: the retrospective is a pre-rollout sanity check, not a full
    # trajectory re-analysis. We surface the outcome + a short reasoning excerpt.
    parts = [f"task={r.task_id} rollout={r.rollout_index} hard={r.hard} soft={r.soft:.2f}"]
    if r.fail_reason:
        parts.append(f"fail_reason={r.fail_reason}")
    excerpt = ""
    for m in reversed(r.messages):
        if m.get("role") == "assistant":
            c = m.get("content")
            if isinstance(c, str) and c.strip():
                excerpt = c.strip()
                break
            if isinstance(c, list):
                for blk in c:
                    if isinstance(blk, dict) and isinstance(blk.get("text"), str):
                        excerpt = blk["text"].strip()
                        break
                if excerpt:
                    break
    if excerpt:
        parts.append("reasoning: " + excerpt[:max_chars])
    return "  - " + "; ".join(parts)


# ── Layer 5a: derive_strategy ────────────────────────────────────────────────

_DERIVE_SYSTEM = """\
You are a COGNITIVE-STRATEGY designer for an AI agent. The agent follows a \
written strategy document when it solves tasks. A root-cause analyst has already \
diagnosed — through a four-level progressive inquiry — WHY a persistent failure \
pattern keeps recurring and which hidden ASSUMPTION in the current strategy is \
responsible. You are now given that diagnosis together with the paired SUCCESS \
counterpart patterns: cases where, on similar tasks, the agent already thought \
the right way and succeeded.

Your task is to DERIVE the strategy change, NOT to invent one. This is the most \
important rule. The new strategy must be the LOGICAL CONSEQUENCE of the \
diagnosis: you start from the broken assumption (level 4), and you SYSTEMATIZE \
the concrete behaviors shown in the success counterparts so that they happen on \
purpose, every time — not by luck. Do not add unrelated advice, do not \
brainstorm new ideas the evidence does not support. Every instruction you write \
must trace back either to (a) replacing the broken assumption, or (b) codifying \
an observed success behavior.

Work through these steps internally, then emit the result:
  1. Name the components of the current strategy the diagnosis implicates (the \
     level-3 strategy text).
  2. State the DIRECTION of change that breaking the level-4 assumption forces \
     (what must now be true that the old strategy took for granted as false, or \
     vice-versa).
  3. Turn the success-counterpart behaviors into a concrete MECHANISM the agent \
     can follow deterministically (a trigger -> action the agent can actually \
     execute mid-task), not a vague exhortation.
  4. Write the FULL new strategy document body as the result of applying that \
     mechanism. Preserve the cognitive dimensions of the old strategy that are not \
     implicated; change only what the diagnosis requires. The document must be \
     self-contained and usable as-is.

CRITICAL — what a strategy document IS (and is NOT). This is an L1 COGNITIVE \
STRATEGY: it describes HOW the agent THINKS, not a checklist of L0 tactical rules. \
Get the ALTITUDE right or the output is worthless.

Structure the strategy document as TWO sections:

  ## <Strategy Name>
  <A concise paragraph describing the strategy's overall approach — the core
  mental model, the key insight, and what makes this way of thinking effective.
  This overview should let a reader grasp the strategy in 30 seconds.>

  ### Details
  <Detailed expansion of the strategy: the cognitive mechanisms, thinking
  processes, mental moves, when-to-switch triggers, and how the strategy adapts
  to different task situations. This section can be as long as needed to fully
  articulate the strategy — use multiple paragraphs, sub-sections with ####,
  bullet lists, or any markdown structure that communicates clearly. The goal
  is to be thorough enough that an agent reading only this document knows
  exactly HOW to think through any task it encounters.>

Hard constraints on the document:
  - The overview paragraph describes the ESSENCE of the strategy at a glance.
  - The ### Details section provides the COMPLETE specification the agent needs.
  - Every instruction must describe a METHOD (how to think), never a GOAL (what to
    achieve) and never a prohibition. Prohibitions belong in rules.md.
  - The document must be self-contained: an agent that reads only this document
    (plus rules.md) should be able to execute tasks effectively.

Output ONLY a JSON object:
  {
    "strategy_text": "<full new strategy.md body: ## Strategy Name header with \
overview paragraph + ### Details section with detailed expansion, markdown>",
    "rationale": "<why this is the LOGICAL CONSEQUENCE of the root cause: name \
the broken assumption, the direction it forces, and which success-counterpart \
behaviors you systematized into which cognitive dimension>",
    "targeted_pattern_ids": ["<id>", ...]
  }
No prose, no markdown fences around the JSON — just the JSON object."""

_DERIVE_USER_TMPL = """\
ROOT-CAUSE DIAGNOSIS (the grounding you must derive from):
-------------------------------------------------------------
{root_cause}
-------------------------------------------------------------

SUCCESS COUNTERPART PATTERNS (the right-way-of-thinking behaviors to systematize):
-------------------------------------------------------------
{counterparts}
-------------------------------------------------------------

CURRENT STRATEGY DOCUMENT (change only what the diagnosis implicates; preserve the rest):
-------------------------------------------------------------
{current_strategy}
-------------------------------------------------------------

Derive — do not invent — the new strategy. The change must be the logical \
consequence of breaking the level-4 assumption, and it must systematize the \
success-counterpart behaviors into a concrete, executable mechanism. Respond with \
ONLY the JSON object described in the instructions."""


def derive_strategy(
    client: "LLMClient",
    root_cause: "RootCause",
    counterpart_patterns: list["PatternRecord"],
    *,
    cfg: "CSSConfig",
    current_strategy: str = "",
) -> "StrategyProposal":
    """Layer 5a — derive a new strategy from a root cause (design D4 / D10).

    "Derive, not generate": builds a substantive prompt that presents the new
    strategy as the logical consequence of ``root_cause`` (start from the broken
    assumption) while explicitly SYSTEMATIZING the behaviors shown in the paired
    ``counterpart_patterns`` (the success side). The result is a full new
    strategy.md body organized as flat ``##`` sections plus a rationale tying the
    change back to the diagnosis.

    ``current_strategy`` (the optimizing node's strategy text) is passed in
    EXPLICITLY by the orchestrator so the derivation can preserve the parts the
    diagnosis does not implicate. It is a per-call argument — NOT read from shared
    ``cfg`` state — so concurrent Phase-6 nodes cannot clobber each other.
    ``targeted_pattern_ids`` default to the root cause's ``pattern_ids`` when the
    LLM omits or garbles them. Never raises: on malformed output the returned
    proposal carries an empty ``strategy_text`` (the caller's downstream gates
    then treat it as a non-proposal).
    """
    user = _DERIVE_USER_TMPL.format(
        root_cause=_fmt_root_cause(root_cause),
        counterparts=_fmt_counterpart_patterns(counterpart_patterns),
        current_strategy=current_strategy or "(current strategy text unavailable)",
    )

    try:
        from css.tracing import stage_context
        with stage_context(client, "strategy_derivation"):
            obj = complete_optimizer_json(
                client, _DERIVE_SYSTEM, user, parse=_parse_obj,
                max_tokens=8192, stage="derive",
            )
    except Exception:
        obj = {}
    proposal = StrategyProposal.from_dict(
        {
            "strategy_text": obj.get("strategy_text", ""),
            "rationale": obj.get("rationale", ""),
            "root_cause": root_cause,
            "targeted_pattern_ids": obj.get("targeted_pattern_ids", []),
        }
    )
    # Default the targeted patterns to the diagnosed patterns when absent/garbled.
    valid = set(root_cause.pattern_ids)
    proposal.targeted_pattern_ids = [
        p for p in proposal.targeted_pattern_ids if p in valid
    ]
    if not proposal.targeted_pattern_ids:
        proposal.targeted_pattern_ids = list(root_cause.pattern_ids)

    from css.tracing import log_event
    _rat = (proposal.rationale or "").strip().replace("\n", " ")
    log_event("strategy_derived",
              strategy_text_len=len(proposal.strategy_text or ""),
              rationale_summary=_rat[:400],
              targeted_pattern_ids=list(proposal.targeted_pattern_ids),
              n_counterparts=len(counterpart_patterns))

    return proposal


# ── Layer 5b: negative-archive gate ("reminder not prohibition") ─────────────

_NEG_ARCHIVE_SYSTEM = """\
You are guarding against repeating a strategy direction the search has ALREADY \
disproven. You are given a NEW candidate strategy and the single most-similar \
ABANDONED strategy recalled from the negative archive (a strategy that previously \
failed rollout validation or was pruned).

This is a REMINDER, not a prohibition. High textual/semantic similarity does NOT \
automatically veto the new candidate — two strategies can read alike yet differ \
in the root cause they address, the timing/trigger of the behavior, or the \
low-level rule basis they assume. Your job is to decide whether the new candidate \
is MEANINGFULLY DIFFERENT from the abandoned one.

  - If there IS a clear, articulable difference (different root cause, different \
    mechanism/trigger, different precondition), allow it to PROCEED and state the \
    difference.
  - If the new candidate is, in substance, the SAME failed direction dressed up \
    differently — no articulable difference — BLOCK it.

Output ONLY a JSON object:
  {"proceed": true|false, "difference": "<the articulated difference, or why none exists>"}
No prose, no fences — just the JSON object."""

_NEG_ARCHIVE_USER_TMPL = """\
NEW CANDIDATE STRATEGY:
-------------------------------------------------------------
{candidate}
-------------------------------------------------------------

The candidate is close to the following PREVIOUSLY-ABANDONED directions \
(nearest first). It must be meaningfully different from ALL of them to proceed:

{abandoned_block}

Is the new candidate MEANINGFULLY DIFFERENT from EVERY abandoned direction above? \
If it is, in substance, any one of them dressed up differently, BLOCK it. \
Articulate the difference if there is one. Respond with ONLY the JSON object."""


def _fmt_archived_hit(entry, score: float) -> str:
    """Render one near archived hit for the difference-articulation prompt."""
    return (
        f"--- abandoned (cosine = {score:.3f}; origin = {entry.origin}) ---\n"
        f"{entry.strategy_snapshot or '(no snapshot recorded)'}\n"
        f"why it was tried: {entry.root_cause or '(not recorded)'}\n"
        f"failure evidence: {entry.failure_evidence or '(not recorded)'}"
    )


def check_negative_archive(
    client: "LLMClient",
    strategy_text: str,
    archive: "NegativeArchive",
    *,
    top_k: int = 5,
    sim_threshold: float = 0.35,
) -> tuple[bool, str]:
    """Layer 5b negative-archive gate (design D12; "reminder not prohibition").

    Recalls the top-``k`` most-similar abandoned strategies from ``archive``
    using Jaccard word-overlap on strategy text, and inspects the closest hit.
    If that hit's similarity is below ``sim_threshold``, the candidate is novel
    — return ``(True, ...)``. Otherwise the candidate is close to a disproven
    direction, so the LLM is asked to ARTICULATE the difference; the candidate
    proceeds ONLY if the LLM gives a clear difference, else it is blocked.

    Returns ``(proceed, reason)``. Never raises: a recall or LLM failure
    degrades to ``(True, ...)`` so the gate never silently kills an otherwise
    valid proposal on infrastructure noise (the downstream 5c rollout is the
    authoritative check); the reason records the degradation.
    """
    if not (strategy_text or "").strip():
        return False, "empty candidate strategy"
    if archive is None or len(archive) == 0:
        return True, "negative archive empty — no prior direction to compare against"

    try:
        hits = archive.recall_by_text(strategy_text, top_k)
    except Exception as exc:  # noqa: BLE001
        return True, f"archive recall failed ({exc!r}); deferring to 5c rollout"

    if not hits:
        return True, "no entries in negative archive to compare against"

    near = [(e, s) for (e, s) in hits if s >= sim_threshold]
    if not near:
        top_entry, top_score = hits[0]
        return (
            True,
            f"top archived similarity {top_score:.3f} < threshold {sim_threshold:.2f} "
            f"(entry {top_entry.entry_id}) — direction is novel",
        )
    top_entry, top_score = near[0]

    # Close to disproven direction(s): force the LLM to articulate the difference.
    abandoned_block = "\n\n".join(_fmt_archived_hit(e, s) for e, s in near)
    user = _NEG_ARCHIVE_USER_TMPL.format(
        candidate=strategy_text,
        abandoned_block=abandoned_block,
    )
    try:
        from css.tracing import stage_context
        with stage_context(client, "negative_archive_check"):
            obj = complete_optimizer_json(
                client, _NEG_ARCHIVE_SYSTEM, user, parse=_parse_obj,
                stage="neg_archive",
            )
    except Exception as exc:  # noqa: BLE001
        return True, (
            f"similar to abandoned {top_entry.entry_id} ({top_score:.3f}) but "
            f"difference-check LLM call failed ({exc!r}); deferring to 5c rollout"
        )
    proceed = bool(obj.get("proceed", False))
    difference = str(obj.get("difference", "")).strip()
    # "Reminder not prohibition": proceed requires a clearly articulated difference.
    if proceed and difference:
        return True, (
            f"similar to abandoned {top_entry.entry_id} ({top_score:.3f}) but "
            f"meaningfully different: {difference}"
        )
    return False, (
        f"too similar to abandoned {top_entry.entry_id} ({top_score:.3f}); "
        + (f"no articulable difference: {difference}" if difference
           else "LLM gave no articulable difference")
    )


# ── Layer 5b: retrospective coverage check ───────────────────────────────────

_RETRO_SYSTEM = """\
You are running a CHEAP, PRE-ROLLOUT sanity check on a proposed cognitive-strategy \
change before committing expensive rollout compute to it. You are given the new \
strategy, the root cause it addresses, a sample of SUCCESS trajectories (where the \
agent already passed), and a sample of PERSISTENT-FAIL tasks (where every rollout \
failed). Do NOT re-run anything — reason retrospectively from the trajectories \
provided.

Produce three things:
  1. positive_evidence: concrete signs in the SUCCESS trajectories that the new \
     strategy's mechanism is already (perhaps accidentally) what made them \
     succeed — i.e. the change would reinforce a real winning behavior, not \
     fight it. Reference specific trajectory moments.
  2. counterfactuals: for the PERSISTENT-FAIL tasks, a per-task judgement of \
     whether the new strategy's mechanism would PLAUSIBLY have changed the \
     agent's trajectory toward success. Ground each judgement in what the failing \
     trajectory actually did wrong — reference the specific failure point and \
     explain how the new strategy would have redirected the agent's thinking at \
     that point.
  3. coverage: your single best estimate, a number in [0, 1], of the FRACTION of \
     the persistent-fail tasks the change would plausibly flip to success. Be \
     calibrated and conservative — this gates whether we spend rollout budget.

Output ONLY a JSON object:
  {
    "positive_evidence": ["<specific evidence grounded in a trajectory moment>", ...],
    "counterfactuals": ["<per-task analysis: what went wrong, and how the new \
strategy would have changed the agent's approach at that specific point>", ...],
    "coverage": <float in [0,1]>
  }
No prose, no fences — just the JSON object."""

_RETRO_USER_TMPL = """\
PROPOSED NEW STRATEGY:
-------------------------------------------------------------
{strategy}
-------------------------------------------------------------

ROOT CAUSE IT ADDRESSES:
{root_cause}

RATIONALE (what success behavior it systematizes):
{rationale}

SUCCESS TRAJECTORIES (positive-evidence material):
{successes}

PERSISTENT-FAIL TASKS (counterfactual material — every rollout failed):
{failures}

Estimate, conservatively, the coverage (fraction of the persistent-fail tasks the \
change would plausibly flip). Respond with ONLY the JSON object described."""


def retrospective_validate(
    client: "LLMClient",
    proposal: "StrategyProposal",
    success_results: list["TaskResult"],
    persistent_fail_groups: list["TaskRolloutGroup"],
    *,
    cfg: "CSSConfig",
) -> "ValidationResult":
    """Layer 5b — cheap pre-rollout retrospective check (design §4.3 / D4).

    Prompts the optimizer over the SUCCESS trajectories (positive evidence) and
    the PERSISTENT-FAIL tasks (counterfactual reasoning) for a calibrated
    ``coverage`` estimate in [0, 1], then applies the coverage gate:

      * coverage < ``cfg.coverage_low``  -> ``"reconsider"`` (root cause too weak).
      * otherwise                         -> ``"proceed"``.

    Boundary choice (documented): the design specifies "< low -> reconsider,
    > high -> proceed". We make ``coverage_high`` the load-bearing PROCEED gate
    (``coverage >= cfg.coverage_high`` proceeds, else reconsider), so the literal
    ">30% -> proceed" threshold is honored and the borderline band ``[low, high]``
    is treated conservatively as reconsider — the cheap early-kill should not spend
    rollout compute on weak coverage. ``coverage_low`` is retained to distinguish
    "clearly insufficient" (< low) from "borderline" (band) in the reason text.

    Never raises: on malformed output the result carries ``coverage=0.0`` and
    ``verdict="reconsider"`` (a safe early-kill), keeping the orchestrator robust.
    """
    # Trim the material so the prompt stays a *cheap* check (not a re-analysis).
    succ_sample = list(success_results)[:8]
    fail_sample = list(persistent_fail_groups)[:8]

    succ_block = "\n".join(_result_brief(r) for r in succ_sample) or "  (none)"
    fail_lines: list[str] = []
    for g in fail_sample:
        fail_lines.append(f"  task {g.task_id} (all {len(g.rollouts)} rollouts failed):")
        for r in g.failures[:2]:
            fail_lines.append("  " + _result_brief(r))
    fail_block = "\n".join(fail_lines) or "  (none)"

    user = _RETRO_USER_TMPL.format(
        strategy=proposal.strategy_text or "(empty)",
        root_cause=_fmt_root_cause(proposal.root_cause),
        rationale=proposal.rationale or "(none provided)",
        successes=succ_block,
        failures=fail_block,
    )

    try:
        from css.tracing import stage_context
        with stage_context(client, "retrospective_validate"):
            obj = complete_optimizer_json(
                client, _RETRO_SYSTEM, user, parse=_parse_obj,
                max_tokens=4096, stage="retro",
            )
    except Exception:
        return ValidationResult(coverage=0.0, verdict="reconsider")

    result = ValidationResult.from_dict(obj)
    # Apply the coverage gate (verdict from the LLM is advisory; code decides):
    # proceed only when coverage clears the high threshold (design "> high").
    result.verdict = "proceed" if result.coverage >= cfg.coverage_high else "reconsider"
    return result
