"""L1 Strategy Cycle — hypothesis-test-verify loop (v2 design).

This module implements the five-step L1 cycle that replaces the old single-shot
PROPOSAL/REFINE pipeline.  When L0 saturates, this cycle runs:

  Step 1  Multi-dimensional analysis (1a/1b/1c parallel → 1d synthesis)
  Step 2  Strategy proposal + behavioral predictions (single LLM call)
  Step 3  Focused testing (rollout with new strategy, empty rules)
  Step 4  Two-layer verification (per-trajectory Judge → aggregate diagnosis)
  Step 5  Iteration control (succeed → MCTS node, or loop back with feedback)

Only strategies that pass verification create MCTS child nodes.  Failed
directions are archived in the negative archive.

All intermediate products are persisted to disk under
``{out_dir}/{node_id}/l1_cycle/round_{N}/step{1-4}/``.

Heavy imports are lazy so this module imports cheaply.
"""
from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from css.model.json_repair import repair_json_via_llm

if TYPE_CHECKING:
    from css.config import CSSConfig
    from css.data.negative_archive import NegativeArchive, NegativeArchiveEntry
    from css.data.pattern import PatternLibrary, PatternRecord
    from css.data.rollout import TaskResult, TaskRolloutGroup
    from css.data.tree import TreeNode
    from css.model.client import LLMClient

_log = logging.getLogger(__name__)


# ── Public result type ──────────────────────────────────────────────────────

@dataclass
class ProposalOutcome:
    """Result of one L1 strategy cycle attempt."""

    success: bool
    operation: str  # "PROPOSAL" | "REFINE"
    new_node: "TreeNode | None" = None
    archived: "NegativeArchiveEntry | None" = None
    reason: str = ""
    n_iterations: int = 0

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "operation": self.operation,
            "new_node": self.new_node.to_dict() if self.new_node is not None else None,
            "archived": (
                self.archived.to_dict(include_embedding=True)
                if self.archived is not None
                else None
            ),
            "reason": self.reason,
            "n_iterations": self.n_iterations,
        }


# ── Iteration context (feedback between rounds) ────────────────────────────

@dataclass
class _PreviousAttempt:
    round: int
    strategy_summary: str
    diagnosis: str  # adherence_failure | hypothesis_failure | partial_success
    adherence_results: list[dict]
    improvement_results: list[dict]
    judge_diagnosis: str
    judge_suggestion: str


@dataclass
class _IterationContext:
    iteration_round: int = 1
    previous_attempts: list[_PreviousAttempt] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "iteration_round": self.iteration_round,
            "previous_attempts": [
                {
                    "round": a.round,
                    "strategy_summary": a.strategy_summary,
                    "diagnosis": a.diagnosis,
                    "adherence_results": a.adherence_results,
                    "improvement_results": a.improvement_results,
                    "judge_diagnosis": a.judge_diagnosis,
                    "judge_suggestion": a.judge_suggestion,
                }
                for a in self.previous_attempts
            ],
        }


# ══════════════════════════════════════════════════════════════════════════════
# Step 1: Multi-dimensional analysis
# ══════════════════════════════════════════════════════════════════════════════

_STEP1A_SYSTEM = """\
You are a strategic analyst examining why L0 tactical optimization has hit a \
ceiling. The L0 optimizer has tried many rule edits but can no longer improve. \
Your job is to identify what characteristics of the CURRENT STRATEGY are \
creating a ceiling that tactical rules cannot break through.

You receive:
- The current strategy.md (the cognitive framework the agent follows)
- The current rules.md (the best tactical rules L0 produced)
- Score trajectory (accept/reject history of recent L0 steps)
- Recently rejected edits (rule changes that failed the acceptance gate)

Analyze: What aspect of the current strategic framework prevents further L0 \
progress? Is there a fundamental assumption in the strategy that limits the \
agent's effectiveness? Are there task categories where the strategy's mental \
model is structurally inadequate?

Output a JSON object:
{
  "ceiling_analysis": "<thorough analysis of why L0 optimization stalled — \
what strategic-level limitation prevents better rules from working>",
  "strategic_assumptions": ["<list of implicit assumptions in the current \
strategy that might be limiting>"],
  "bottleneck_areas": ["<task types or problem categories where the ceiling \
is most apparent>"]
}

Output ONLY the JSON object — no prose, no fences."""


_STEP1B_SYSTEM = """\
You are a trajectory analyst performing deep behavioral analysis on agent \
failure trajectories. You are examining WHY the agent fails at specific tasks, \
looking for patterns in the agent's THINKING PROCESS — not surface errors.

You receive 3-5 representative failure trajectories with full execution traces.

For each trajectory, identify:
1. The critical decision point where the agent's approach diverged from what \
would succeed
2. What mental model or reasoning pattern led to the wrong decision
3. Whether the failure stems from the agent's STRATEGY (how it thinks) vs \
its RULES (what it does)

Look for SYSTEMATIC patterns across trajectories — shared cognitive blind \
spots, common wrong assumptions, or recurring failure mechanisms.

Output a JSON object:
{
  "trajectory_analyses": [
    {
      "task_id": "<id>",
      "critical_decision_point": "<where the approach went wrong>",
      "reasoning_failure": "<what cognitive pattern led to failure>",
      "strategic_vs_tactical": "strategic | tactical | both",
      "evidence": "<specific quotes/actions from the trajectory>"
    }
  ],
  "systematic_patterns": [
    {
      "pattern": "<description of the shared cognitive pattern>",
      "affected_tasks": ["<task_ids>"],
      "root_mechanism": "<why this pattern keeps occurring>"
    }
  ]
}

Output ONLY the JSON object — no prose, no fences."""


_STEP1C_SYSTEM = """\
You are reviewing the L0 contrastive analysis to understand its limitations. \
L0 found divergences between successful and failing rollouts of the same task, \
but the rule edits derived from these divergences failed to improve performance.

You receive:
- L0 contrastive analyst diagnoses
- Rules that were tried but rejected based on these diagnoses
- Mixed-result task groups (some rollouts passed, some failed)

Analyze: Why couldn't the divergences identified by L0 be fixed with rules? \
Are the divergences symptoms of a deeper strategic issue? Does the difference \
between success and failure require a change in HOW the agent thinks, not \
just WHAT rules it follows?

Output a JSON object:
{
  "limitation_analysis": "<why L0 contrastive findings couldn't be fixed \
with rules>",
  "deeper_issues": [
    {
      "l0_finding": "<what L0's contrastive analysis found>",
      "why_rules_failed": "<why rule-level fixes didn't work>",
      "strategic_implication": "<what this suggests about needed strategy change>"
    }
  ],
  "strategy_change_indicators": ["<signals that a strategic shift is needed>"]
}

Output ONLY the JSON object — no prose, no fences."""


_STEP1D_SYSTEM = """\
You are synthesizing three independent analyses into a strategic hypothesis \
for improving an AI agent's cognitive strategy:

1. L0 CEILING ANALYSIS: why tactical optimization stalled
2. TRAJECTORY ANALYSIS: deep behavioral patterns from failure traces
3. CONTRASTIVE LIMITATION ANALYSIS: why L0-identified divergences couldn't be \
fixed with rules

Your task is to produce a STRATEGIC-LEVEL synthesis — not a list of tactical \
fixes, but insights about what fundamental change in the agent's thinking \
approach is needed.

Output a JSON object:
{
  "core_assumptions_and_limitations": "<the key strategic assumptions that \
are limiting agent performance — state these as high-level judgments about \
the strategy, not as a list of bugs>",
  "recommended_directions": [
    {
      "direction": "<name of the strategic change>",
      "rationale": "<why this direction addresses the identified limitations>",
      "expected_impact": "<what types of tasks would benefit and how>",
      "risk": "<what could go wrong or what effective behaviors might be lost>"
    }
  ],
  "constraints": "<what is working well in the current strategy that must be \
preserved in any new approach>"
}

Output ONLY the JSON object — no prose, no fences."""


# ══════════════════════════════════════════════════════════════════════════════
# Step 2: Strategy proposal + behavioral predictions
# ══════════════════════════════════════════════════════════════════════════════

_STEP2_SYSTEM = """\
You are an L1 strategy designer for an AI agent optimization system. Based on \
multi-dimensional analysis of the agent's performance, you design a new \
cognitive strategy — the document that tells the agent HOW TO THINK when \
approaching tasks.

You are designing both the strategy AND the criteria by which it will be \
verified. This is critical: you know what behavior you expect from this \
strategy, so you must articulate that expectation clearly enough for an \
independent judge to evaluate it from trajectory evidence.

STRATEGY FORMAT — two sections, nothing else:

  ## <Strategy Name>
  <A concise paragraph: the core mental model, key insight, and what makes \
this way of thinking effective. A reader should grasp the strategy in 30 \
seconds.>

  ### Details
  <Detailed expansion: cognitive mechanisms, thinking processes, mental moves, \
when-to-switch triggers, adaptation to different situations. As long as \
needed — use multiple paragraphs, sub-sections (####), bullet lists. The \
agent reading only this document should know exactly HOW to think through \
any task.>

ALTITUDE — a strategy describes HOW to think, not WHAT to do:
  - GOOD: "Form a structural hypothesis about the data before acting"
  - BAD: "Always check range boundaries" (that's a rule, not a strategy)

Output a JSON object:
{
  "strategy_text": "<full strategy.md body: ## Name + overview + ### Details + \
detail>",
  "design_reasoning": "<why this strategy addresses the diagnosed limitations>",
  "adherence_criteria": [
    {
      "id": "AC-1",
      "expected_behavior_pattern": "<observable behavior in trajectories when \
the agent follows this strategy>",
      "current_behavior_contrast": "<what the agent does now in the same \
situation, as a comparison baseline>"
    }
  ],
  "improvement_expectations": [
    {
      "id": "IE-1",
      "target_problem": "<the specific failure mode this strategy addresses>",
      "improvement_mechanism": "<how the strategy changes agent behavior to \
fix this>",
      "trajectory_evidence": "<what an observer should see in the trajectory \
text if the improvement is working>"
    }
  ]
}

QUALITY REQUIREMENTS for adherence_criteria and improvement_expectations:
- Must be BEHAVIORAL-PARADIGM level, not L0 rule level
- Must reference things observable in trajectory message sequences ([role] + \
content blocks)
- adherence_criteria: 2-3 items, each with a current_behavior_contrast baseline
- improvement_expectations: 2-3 items, each with concrete trajectory_evidence

Output ONLY the JSON object — no prose, no fences."""


# ══════════════════════════════════════════════════════════════════════════════
# Step 4: Two-layer verification
# ══════════════════════════════════════════════════════════════════════════════

_STEP4_PER_TRAJ_SYSTEM = """\
You are judging whether an agent followed a proposed cognitive strategy and \
whether the strategy produced improvements. You receive ONE agent execution \
trajectory along with the strategy's adherence criteria and improvement \
expectations.

Your task:
1. For each adherence criterion: extract BEHAVIORAL EVIDENCE from the \
trajectory showing whether the agent's thinking aligns with the expected \
pattern. Compare against the current_behavior_contrast baseline.
2. For each improvement expectation: extract evidence of whether the \
trajectory_evidence described in the expectation is actually observable.

Ground every judgment in SPECIFIC evidence from the trajectory. If evidence is \
ambiguous, say so — do not guess.

Output a JSON object:
{
  "task_id": "<from input>",
  "outcome": "<pass or fail>",
  "criteria_assessments": [
    {
      "criterion_id": "AC-1",
      "behavioral_evidence": "<what the agent actually did in this trajectory \
relevant to this criterion>",
      "verdict": "adhered | not_adhered | partial",
      "analysis": "<why you made this judgment>"
    }
  ],
  "expectation_assessments": [
    {
      "expectation_id": "IE-1",
      "behavioral_evidence": "<evidence of improvement or lack thereof>",
      "verdict": "improved | not_improved | inconclusive",
      "analysis": "<why you made this judgment>"
    }
  ]
}

Output ONLY the JSON object — no prose, no fences."""


_STEP4_AGGREGATE_SYSTEM = """\
You are aggregating per-trajectory verification results into an overall \
diagnosis of whether a proposed strategy is effective.

You receive:
- The strategy proposal (strategy text + adherence criteria + improvement \
expectations)
- Per-trajectory judge verdicts from multiple test trajectories

Your task: synthesize the per-trajectory evidence into cross-trajectory \
patterns and make an overall judgment.

Output a JSON object:
{
  "adherence_verdicts": [
    {
      "criterion_id": "AC-1",
      "verdict": "adhered | not_adhered | partial",
      "evidence": "<cross-trajectory pattern summary>",
      "analysis": "<overall judgment reasoning>"
    }
  ],
  "improvement_verdicts": [
    {
      "expectation_id": "IE-1",
      "verdict": "improved | not_improved | inconclusive",
      "evidence": "<cross-trajectory improvement pattern>",
      "analysis": "<overall judgment reasoning>"
    }
  ],
  "overall_diagnosis": {
    "strategy_effective": true or false,
    "primary_issue": "none | adherence_failure | hypothesis_failure | \
partial_success",
    "diagnosis_detail": "<what specifically is the problem, if any>",
    "iteration_suggestion": "<what the next iteration should try differently>"
  }
}

DECISION CRITERIA:
- strategy_effective=true: majority of adherence criteria are adhered AND \
majority of improvement expectations show improvement
- adherence_failure: the agent is NOT following the strategy (the strategy \
text needs rephrasing for the agent to understand)
- hypothesis_failure: the agent follows the strategy but it doesn't help \
(the strategic direction is wrong, need to go back to analysis)
- partial_success: some aspects work, some don't (refine the strategy in the \
working direction)

Output ONLY the JSON object — no prose, no fences."""


# ══════════════════════════════════════════════════════════════════════════════
# Core L1 cycle implementation
# ══════════════════════════════════════════════════════════════════════════════

def run_l1_cycle(
    node: "TreeNode",
    library: "PatternLibrary",
    archive: "NegativeArchive",
    env,
    target_client: "LLMClient",
    optimizer_client: "LLMClient",
    *,
    cfg: "CSSConfig",
    operation: str,
    new_node_id: str,
    epoch: int,
    train_groups: "list[TaskRolloutGroup]",
    out_dir: str,
) -> "ProposalOutcome":
    """Run the full L1 hypothesis-test-verify cycle.

    This is the v2 replacement for the old ``run_proposal``/``run_refine``.
    The cycle iterates up to ``cfg.max_l1_iterations`` times through:
      Step 1 → Step 2 → Step 3 → Step 4 → Step 5 (decision)

    On success, returns an outcome with ``new_node`` set.  On exhausting all
    iterations, archives the last failed direction and returns failure.
    """
    cycle_dir = os.path.join(out_dir, node.node_id, "l1_cycle")
    os.makedirs(cycle_dir, exist_ok=True)

    max_iters = cfg.max_l1_iterations
    iteration_ctx = _IterationContext(iteration_round=1)
    max_workers = getattr(cfg, "max_api_workers", 32)

    # The Step-1 failure-trajectory analysis (1b) consumes the node's failed
    # rollouts; the diagnostic/regression subsets are derived inside Step 3.
    fail_results = [r for g in train_groups for r in g.rollouts if not getattr(r, "passed", False)]

    last_strategy = ""

    for iteration in range(1, max_iters + 1):
        round_dir = os.path.join(cycle_dir, f"round_{iteration:04d}")
        os.makedirs(round_dir, exist_ok=True)
        _log.info("L1 cycle iteration %d/%d for node %s", iteration, max_iters, node.node_id)

        restart_from = "step1"
        if iteration > 1:
            last_attempt = iteration_ctx.previous_attempts[-1] if iteration_ctx.previous_attempts else None
            if last_attempt and last_attempt.diagnosis == "hypothesis_failure":
                restart_from = "step1"
            else:
                restart_from = "step2"

        # ── Step 1: Multi-dimensional analysis ──────────────────────────
        if restart_from == "step1":
            hypothesis = _run_step1(
                optimizer_client, node, train_groups, fail_results, cfg=cfg,
                max_workers=max_workers, round_dir=round_dir,
            )
        # else: reuse last hypothesis (unchanged since we're only revising strategy)

        # ── Step 2: Strategy proposal + predictions ─────────────────────
        step2_result = _run_step2(
            optimizer_client, node, hypothesis, iteration_ctx, cfg=cfg,
            round_dir=round_dir,
        )
        # A Step-2 failure (no parseable proposal or empty strategy) still records
        # an attempt so the restart logic sees a non-empty history and the next
        # Step 2 receives feedback that its prior output was unusable — otherwise
        # the loop silently freezes the stale hypothesis and reproduces the failure.
        def _record_step2_failure(detail: str) -> None:
            iteration_ctx.previous_attempts.append(_PreviousAttempt(
                round=iteration,
                strategy_summary="(Step 2 produced no usable strategy proposal)",
                diagnosis="step2_failure",
                adherence_results=[],
                improvement_results=[],
                judge_diagnosis=detail,
                judge_suggestion=(
                    "Emit a single valid JSON object with all required fields "
                    "(strategy_text, adherence_criteria, improvement_expectations); "
                    "no prose, no markdown fences."
                ),
            ))
            iteration_ctx.iteration_round = iteration + 1

        if not step2_result:
            _save_json(os.path.join(round_dir, "step2", "error.json"),
                       {"error": "Step 2 produced no usable output"})
            _record_step2_failure("Step 2 output was missing or unparseable JSON.")
            continue

        # ``or ""`` guards against ``strategy_text: null`` (key present, value None).
        strategy_text = step2_result.get("strategy_text") or ""
        if not strategy_text.strip():
            _record_step2_failure("Step 2 proposal had an empty strategy_text.")
            continue

        last_strategy = strategy_text

        # ── Step 3: Focused testing ─────────────────────────────────────
        test_results = _run_step3(
            env, target_client, strategy_text, train_groups,
            cfg=cfg, round_dir=round_dir, epoch=epoch, node_id=node.node_id,
        )

        # ── Step 4: Two-layer verification ──────────────────────────────
        diagnosis = _run_step4(
            optimizer_client, step2_result, test_results,
            cfg=cfg, max_workers=max_workers, round_dir=round_dir,
        )

        # ── Step 5: Iteration control ──────────────────────────────────
        # ``or {}`` guards both a missing key and ``overall_diagnosis: null``.
        overall = diagnosis.get("overall_diagnosis") or {}
        if not isinstance(overall, dict):
            overall = {}
        strategy_effective = _coerce_bool(overall.get("strategy_effective", False))
        primary_issue = overall.get("primary_issue", "none")

        _save_json(os.path.join(round_dir, "step5_decision.json"), {
            "strategy_effective": strategy_effective,
            "primary_issue": primary_issue,
            "iteration": iteration,
        })

        if strategy_effective:
            rules = "" if operation == "PROPOSAL" else _inherit_rules_for_refine(
                optimizer_client, strategy_text, node.rules, cfg=cfg
            )
            new_node = _build_node(
                node,
                new_node_id=new_node_id,
                branch_type=operation,
                strategy=strategy_text,
                rules=rules,
                refine_count=(node.refine_count + 1 if operation == "REFINE"
                              else node.refine_count),
                epoch=epoch,
            )
            _save_json(os.path.join(cycle_dir, "final_outcome.json"), {
                "success": True,
                "operation": operation,
                "n_iterations": iteration,
                "new_node_id": new_node.node_id,
            })
            return ProposalOutcome(
                success=True,
                operation=operation,
                new_node=new_node,
                reason=f"strategy_verified: iteration {iteration}",
                n_iterations=iteration,
            )

        # Build iteration feedback for the next round.
        attempt = _PreviousAttempt(
            round=iteration,
            strategy_summary=strategy_text[:500],
            diagnosis=primary_issue,
            adherence_results=diagnosis.get("adherence_verdicts", []),
            improvement_results=diagnosis.get("improvement_verdicts", []),
            judge_diagnosis=overall.get("diagnosis_detail", ""),
            judge_suggestion=overall.get("iteration_suggestion", ""),
        )
        iteration_ctx.previous_attempts.append(attempt)
        iteration_ctx.iteration_round = iteration + 1

        _save_json(os.path.join(round_dir, "iteration_context.json"),
                   iteration_ctx.to_dict())

        _log.info("L1 iteration %d: %s — %s", iteration, primary_issue,
                  overall.get("diagnosis_detail", "")[:200])

    # All iterations exhausted — archive the last failed direction.
    archived = _archive_failed_cycle(
        archive,
        strategy_snapshot=last_strategy,
        origin=f"{operation.lower()}_l1_exhausted",
        diagnosis_summary=(
            iteration_ctx.previous_attempts[-1].judge_diagnosis
            if iteration_ctx.previous_attempts else "no diagnosis"
        ),
        epoch=epoch,
        source_node_id=node.node_id,
        n_iterations=max_iters,
    )
    _save_json(os.path.join(cycle_dir, "final_outcome.json"), {
        "success": False,
        "operation": operation,
        "n_iterations": max_iters,
        "reason": "max_iterations_exhausted",
    })
    return ProposalOutcome(
        success=False,
        operation=operation,
        archived=archived,
        reason=f"max_iterations_exhausted: {max_iters} rounds without verification pass",
        n_iterations=max_iters,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Step implementations
# ══════════════════════════════════════════════════════════════════════════════

def _run_step1(
    client: "LLMClient",
    node: "TreeNode",
    train_groups: "list[TaskRolloutGroup]",
    fail_results: "list[TaskResult]",
    *,
    cfg: "CSSConfig",
    max_workers: int,
    round_dir: str,
) -> dict:
    """Step 1: 1a/1b/1c parallel → 1d synthesis."""
    from css.trajectory import format_trajectory

    step_dir = os.path.join(round_dir, "step1")
    os.makedirs(step_dir, exist_ok=True)

    tool_trunc = cfg.tool_trunc
    results_1abc: dict[str, Any] = {}

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {}

        # 1a: L0 ceiling analysis
        futures["1a"] = pool.submit(
            _step1a, client, node, cfg=cfg, tool_trunc=tool_trunc
        )

        # 1b: Failure trajectory deep analysis
        rep_trajs = _select_representative_failures(fail_results, node.pattern_records, k=5)
        futures["1b"] = pool.submit(
            _step1b, client, rep_trajs, tool_trunc=tool_trunc
        )

        # 1c: L0 contrastive limitation review
        mixed_groups = [g for g in train_groups if g.contrastive_pairs()]
        futures["1c"] = pool.submit(
            _step1c, client, node, mixed_groups, tool_trunc=tool_trunc
        )

        for key, fut in futures.items():
            try:
                results_1abc[key] = fut.result()
            except Exception as exc:
                _log.warning("Step 1%s failed: %s", key, exc)
                results_1abc[key] = {"error": str(exc)}

    for key, data in results_1abc.items():
        _save_json(os.path.join(step_dir, f"1{key}_analysis.json"), data)

    # 1d: Hypothesis synthesis
    hypothesis = _step1d(client, results_1abc)
    _save_json(os.path.join(step_dir, "1d_hypothesis.json"), hypothesis)
    return hypothesis


def _step1a(client: "LLMClient", node: "TreeNode", *, cfg: "CSSConfig",
            tool_trunc: int) -> dict:
    """1a — L0 ceiling analysis."""
    step_buffer = node.step_buffer
    rejected = step_buffer.recent_rejected_edits(cfg.W) if step_buffer else []
    score_history = []
    for entry in (step_buffer.entries[-cfg.W:] if step_buffer else []):
        score_history.append({
            "step": entry.step,
            "action": entry.action,
            "score_before": entry.score_before,
            "score_after": entry.score_after,
        })

    user_parts = [
        "## Current strategy.md\n" + (node.strategy or "(empty)").strip(),
        "## Current rules.md\n" + (node.rules or "(empty)").strip(),
        "## Score trajectory (recent L0 steps)\n" + json.dumps(score_history, indent=2),
    ]
    if rejected:
        rej_text = "\n".join(
            f"- [{e.op}] {(e.content or '')[:200]}" for e in rejected[:10]
        )
        user_parts.append("## Recently rejected edits\n" + rej_text)

    user = "\n\n".join(user_parts)
    text = _safe_optimizer_call(client, _STEP1A_SYSTEM, user, max_tokens=4096)
    result = _parse_json_safe(text, None)
    if result is None:
        repaired = repair_json_via_llm(client, _STEP1A_SYSTEM, user, text, stage="step1a")
        if repaired is not None:
            result = _parse_json_safe(repaired, None)
    return result if result is not None else {"ceiling_analysis": text}


def _step1b(client: "LLMClient", trajectories: "list[TaskResult]", *,
            tool_trunc: int) -> dict:
    """1b — failure trajectory deep analysis."""
    from css.trajectory import format_trajectory

    traj_parts = []
    for r in trajectories[:5]:
        header = f"### Task {r.task_id} (outcome: {'PASS' if r.passed else 'FAIL'})"
        desc = getattr(r, "task_description", "") or ""
        traj_text = format_trajectory(r.messages, tool_trunc=tool_trunc)
        traj_parts.append(f"{header}\n{desc}\n\n{traj_text}")

    user = "## Failure trajectories for analysis\n\n" + "\n\n---\n\n".join(traj_parts)
    text = _safe_optimizer_call(client, _STEP1B_SYSTEM, user, max_tokens=8192)
    result = _parse_json_safe(text, None)
    if result is None:
        repaired = repair_json_via_llm(client, _STEP1B_SYSTEM, user, text, stage="step1b")
        if repaired is not None:
            result = _parse_json_safe(repaired, None)
    return result if result is not None else {"raw_analysis": text}


def _step1c(client: "LLMClient", node: "TreeNode",
            mixed_groups: "list[TaskRolloutGroup]", *, tool_trunc: int) -> dict:
    """1c — L0 contrastive analysis limitation review."""
    from css.trajectory import format_trajectory

    user_parts = []
    step_buffer = node.step_buffer
    rejected = step_buffer.recent_rejected_edits(10) if step_buffer else []
    if rejected:
        rej_text = "\n".join(
            f"- [{e.op}] reason: {e.reason or 'N/A'} | content: {(e.content or '')[:150]}"
            for e in rejected[:8]
        )
        user_parts.append("## Rules tried but rejected\n" + rej_text)

    for g in mixed_groups[:3]:
        pairs = g.contrastive_pairs()
        if not pairs:
            continue
        for succ, fail in pairs[:1]:
            user_parts.append(
                f"### Mixed task {g.task_id}\n"
                f"**Success rollout:**\n{format_trajectory(succ.messages, tool_trunc=tool_trunc)[:3000]}\n\n"
                f"**Failure rollout:**\n{format_trajectory(fail.messages, tool_trunc=tool_trunc)[:3000]}"
            )

    if not user_parts:
        return {"limitation_analysis": "No mixed-result tasks available for contrastive review."}

    user = "\n\n".join(user_parts)
    text = _safe_optimizer_call(client, _STEP1C_SYSTEM, user, max_tokens=4096)
    result = _parse_json_safe(text, None)
    if result is None:
        repaired = repair_json_via_llm(client, _STEP1C_SYSTEM, user, text, stage="step1c")
        if repaired is not None:
            result = _parse_json_safe(repaired, None)
    return result if result is not None else {"raw_analysis": text}


def _step1d(client: "LLMClient", analyses: dict) -> dict:
    """1d — hypothesis synthesis from 1a/1b/1c."""
    user_parts = []
    if "1a" in analyses:
        user_parts.append("## 1. L0 Ceiling Analysis\n" + json.dumps(analyses["1a"], indent=2, ensure_ascii=False))
    if "1b" in analyses:
        user_parts.append("## 2. Trajectory Analysis\n" + json.dumps(analyses["1b"], indent=2, ensure_ascii=False))
    if "1c" in analyses:
        user_parts.append("## 3. Contrastive Limitation Analysis\n" + json.dumps(analyses["1c"], indent=2, ensure_ascii=False))

    user = "\n\n".join(user_parts)
    text = _safe_optimizer_call(client, _STEP1D_SYSTEM, user, max_tokens=4096)
    result = _parse_json_safe(text, None)
    if result is None:
        repaired = repair_json_via_llm(client, _STEP1D_SYSTEM, user, text, stage="step1d")
        if repaired is not None:
            result = _parse_json_safe(repaired, None)
    return result if result is not None else {"raw_synthesis": text}


def _run_step2(
    client: "LLMClient",
    node: "TreeNode",
    hypothesis: dict,
    iteration_ctx: _IterationContext,
    *,
    cfg: "CSSConfig",
    round_dir: str,
) -> dict | None:
    """Step 2: Strategy proposal + behavioral predictions."""
    step_dir = os.path.join(round_dir, "step2")
    os.makedirs(step_dir, exist_ok=True)

    user_parts = [
        "## Step 1d Hypothesis\n" + json.dumps(hypothesis, indent=2, ensure_ascii=False),
        "## Current strategy.md (reference)\n" + (node.strategy or "(empty)").strip(),
        "## Current rules.md (reference)\n" + (node.rules or "(empty)").strip()[:2000],
    ]

    if iteration_ctx.previous_attempts:
        user_parts.append(
            "## Iteration Context (CRITICAL — previous attempts and their diagnoses)\n"
            + json.dumps(iteration_ctx.to_dict(), indent=2, ensure_ascii=False)
        )

    user = "\n\n".join(user_parts)
    text = _safe_optimizer_call(client, _STEP2_SYSTEM, user, max_tokens=8192)
    result = _parse_json_safe(text, None)
    if result is None:
        repaired = repair_json_via_llm(client, _STEP2_SYSTEM, user, text, stage="step2")
        if repaired is not None:
            result = _parse_json_safe(repaired, None)
    if result is None:
        _save_json(os.path.join(step_dir, "raw_response.txt"), {"raw": text})
        return None
    _save_json(os.path.join(step_dir, "strategy_proposal.json"), result)
    return result


def _run_step3(
    env,
    target_client: "LLMClient",
    strategy_text: str,
    train_groups: "list[TaskRolloutGroup]",
    *,
    cfg: "CSSConfig",
    round_dir: str,
    epoch: int,
    node_id: str,
) -> "list[TaskResult]":
    """Step 3: Focused testing — rollout new strategy with empty rules."""
    from css.rollout.batch import batch_rollout
    from css.skill_document import SkillDocument

    step_dir = os.path.join(round_dir, "step3", "rollout")
    os.makedirs(step_dir, exist_ok=True)

    candidate_doc = SkillDocument(skill_dir="", strategy=strategy_text, rules="")
    skill_text = candidate_doc.combined_skill_text()

    diagnostic_items = _select_diagnostic_tasks(env, train_groups, cfg)

    results = batch_rollout(
        env,
        diagnostic_items,
        skill_text,
        target_client,
        k_rollouts=1,
        out_dir=step_dir,
        max_workers=cfg.max_api_workers,
        task_timeout=cfg.task_timeout_s,
        epoch=epoch,
        node_id=node_id,
    )
    _log.info("Step 3: tested %d tasks, %d passed",
              len(results), sum(1 for r in results if r.passed))
    return results


def _run_step4(
    client: "LLMClient",
    proposal: dict,
    test_results: "list[TaskResult]",
    *,
    cfg: "CSSConfig",
    max_workers: int,
    round_dir: str,
) -> dict:
    """Step 4: two-layer verification."""
    from css.trajectory import format_trajectory

    step_dir = os.path.join(round_dir, "step4")
    per_traj_dir = os.path.join(step_dir, "per_trajectory")
    os.makedirs(per_traj_dir, exist_ok=True)

    adherence_criteria = proposal.get("adherence_criteria", [])
    improvement_expectations = proposal.get("improvement_expectations", [])
    criteria_text = json.dumps(adherence_criteria, indent=2, ensure_ascii=False)
    expectations_text = json.dumps(improvement_expectations, indent=2, ensure_ascii=False)

    # Layer 1: per-trajectory Judge (parallel)
    per_traj_verdicts = []

    def _judge_one(result: "TaskResult") -> dict:
        traj_text = format_trajectory(result.messages, tool_trunc=cfg.tool_trunc)
        user = (
            f"## Strategy adherence criteria\n{criteria_text}\n\n"
            f"## Improvement expectations\n{expectations_text}\n\n"
            f"## Trajectory (task {result.task_id}, outcome: "
            f"{'PASS' if result.passed else 'FAIL'})\n\n{traj_text}"
        )
        text = _safe_optimizer_call(
            client, _STEP4_PER_TRAJ_SYSTEM, user, max_tokens=4096
        )
        verdict = _parse_json_safe(text, None)
        if verdict is None:
            repaired = repair_json_via_llm(
                client, _STEP4_PER_TRAJ_SYSTEM, user, text, stage="step4_judge"
            )
            if repaired is not None:
                verdict = _parse_json_safe(repaired, None)
        if verdict is None:
            verdict = {"task_id": result.task_id, "raw": text}
        verdict["task_id"] = str(result.task_id)
        verdict["outcome"] = "pass" if result.passed else "fail"
        return verdict

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_judge_one, r): r for r in test_results}
        for fut in as_completed(futures):
            try:
                verdict = fut.result()
                per_traj_verdicts.append(verdict)
                _save_json(
                    os.path.join(per_traj_dir, f"task_{verdict.get('task_id', 'unknown')}.json"),
                    verdict
                )
            except Exception as exc:
                _log.warning("Per-trajectory judge failed: %s", exc)

    # Layer 2: aggregate diagnosis
    aggregate_user_parts = [
        "## Strategy proposal\n" + json.dumps({
            "strategy_text": (proposal.get("strategy_text") or "")[:2000],
            "adherence_criteria": adherence_criteria,
            "improvement_expectations": improvement_expectations,
        }, indent=2, ensure_ascii=False),
        "## Per-trajectory verdicts\n" + json.dumps(per_traj_verdicts, indent=2, ensure_ascii=False),
    ]
    aggregate_user = "\n\n".join(aggregate_user_parts)
    agg_text = _safe_optimizer_call(
        client, _STEP4_AGGREGATE_SYSTEM, aggregate_user, max_tokens=4096
    )
    diagnosis = _parse_json_safe(agg_text, None)
    if diagnosis is None:
        repaired = repair_json_via_llm(
            client, _STEP4_AGGREGATE_SYSTEM, aggregate_user, agg_text, stage="step4_agg"
        )
        if repaired is not None:
            diagnosis = _parse_json_safe(repaired, None)
    if diagnosis is None:
        diagnosis = {
            "overall_diagnosis": {
                "strategy_effective": False,
                "primary_issue": "hypothesis_failure",
                "diagnosis_detail": "Failed to parse aggregate verdict",
                "iteration_suggestion": "Retry with different approach",
            }
        }
    _save_json(os.path.join(step_dir, "aggregated_verdict.json"), diagnosis)
    return diagnosis


# ══════════════════════════════════════════════════════════════════════════════
# Helper functions
# ══════════════════════════════════════════════════════════════════════════════

def _select_representative_failures(
    fail_results: "list[TaskResult]",
    pattern_library: "PatternLibrary",
    k: int = 5,
) -> "list[TaskResult]":
    """Select k representative failure trajectories by pattern coverage."""
    if len(fail_results) <= k:
        return list(fail_results)

    failure_patterns = list(pattern_library.by_polarity("failure"))
    top_patterns = sorted(failure_patterns, key=lambda p: p.support_count, reverse=True)[:k * 2]

    task_pattern_coverage: dict[str, set[str]] = {}
    for pat in top_patterns:
        for obs in pat.observations:
            tid = str(obs.task_id)
            if tid not in task_pattern_coverage:
                task_pattern_coverage[tid] = set()
            task_pattern_coverage[tid].add(pat.pattern_id)

    result_by_task: dict[str, "TaskResult"] = {}
    for r in fail_results:
        tid = str(r.task_id)
        if tid not in result_by_task:
            result_by_task[tid] = r

    scored = sorted(
        result_by_task.items(),
        key=lambda item: len(task_pattern_coverage.get(item[0], set())),
        reverse=True,
    )
    return [r for _, r in scored[:k]]


def _select_diagnostic_tasks(
    env,
    train_groups: "list[TaskRolloutGroup]",
    cfg: "CSSConfig",
) -> list[dict]:
    """Select tasks for Step 3 focused testing."""
    persistent_fail = [g for g in train_groups if g.is_persistent_fail()]
    # ``all([])`` is True, so guard against zero-rollout groups (all rollouts
    # errored/timed out) being mis-selected as fully-passing regression tasks.
    passing = [
        g for g in train_groups
        if g.rollouts and all(r.passed for r in g.rollouts)
    ]

    n_diag = cfg.l1_diagnostic_tasks
    n_regress = cfg.l1_regression_tasks

    train_index = _train_item_index(env)

    items = []
    seen = set()

    for g in persistent_fail[:n_diag]:
        tid = str(g.task_id)
        if tid in train_index and tid not in seen:
            items.append(train_index[tid])
            seen.add(tid)
        if len(items) >= n_diag:
            break

    for g in passing[:n_regress]:
        tid = str(g.task_id)
        if tid in train_index and tid not in seen:
            items.append(train_index[tid])
            seen.add(tid)
        if len(items) >= n_diag + n_regress:
            break

    return items


def _train_item_index(env) -> dict:
    """Map task_id -> env train item dict."""
    index = {}
    try:
        for item in env.train_items():
            if isinstance(item, dict):
                tid = str(item.get("task_id", item.get("id", "")))
                if tid:
                    index[tid] = item
    except Exception:
        pass
    return index


def _inherit_rules_for_refine(
    client: "LLMClient",
    new_strategy: str,
    old_rules: str,
    *,
    cfg: "CSSConfig",
) -> str:
    """For REFINE: semantically inherit non-conflicting rules.

    On any failure of the semantic-inheritance LLM call, degrade to KEEPING the
    parent rules verbatim rather than dropping them — a REFINE node must never be
    strictly worse than its parent on tactical guidance because of a transient
    optimizer error.
    """
    if not old_rules or not old_rules.strip():
        return ""
    try:
        from css.proposal.inheritance import proposal_inherit_rules
        return proposal_inherit_rules(client, new_strategy, old_rules, cfg=cfg)
    except Exception:
        return old_rules


def _build_node(
    parent: "TreeNode",
    *,
    new_node_id: str,
    branch_type: str,
    strategy: str,
    rules: str,
    refine_count: int,
    epoch: int,
) -> "TreeNode":
    """Construct the accepted child node."""
    from css.data.tree import TreeNode

    return TreeNode(
        node_id=new_node_id,
        branch_type=branch_type,
        parent_id=parent.node_id,
        strategy=strategy,
        rules=rules,
        refine_count=refine_count,
        created_epoch=epoch,
    )


def _archive_failed_cycle(
    archive: "NegativeArchive",
    *,
    strategy_snapshot: str,
    origin: str,
    diagnosis_summary: str,
    epoch: int,
    source_node_id: str,
    n_iterations: int,
) -> "NegativeArchiveEntry":
    """Write a failed L1 cycle direction to the negative archive."""
    from css.data.negative_archive import NegativeArchiveEntry

    entry = NegativeArchiveEntry(
        entry_id=archive.new_entry_id(),
        strategy_snapshot=strategy_snapshot,
        origin=origin,
        root_cause=f"L1 cycle exhausted after {n_iterations} iterations",
        failure_evidence=f"diagnosis: {diagnosis_summary}",
        created_epoch=epoch,
        created_step=-1,
        source_node_id=source_node_id,
    )
    archive.add(entry)
    return entry


def _save_json(path: str, data: Any) -> None:
    """Persist a JSON-serializable object to disk."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    except Exception as exc:
        _log.warning("Failed to save %s: %s", path, exc)


def _safe_optimizer_call(
    client: "LLMClient", system: str, user: str, *, max_tokens: int
) -> str:
    """Call the optimizer model, degrading to ``""`` on any error.

    Mirrors the degradation contract honoured elsewhere in the pipeline: a
    transient optimizer API failure (rate limit, timeout, network) inside one L1
    step must not crash the whole CSS run. The empty string flows into
    :func:`_parse_json_safe`, which returns the caller's fallback.
    """
    try:
        text, _usage = client.complete_optimizer(system, user, max_tokens=max_tokens)
        return text or ""
    except Exception as exc:  # noqa: BLE001 — degrade, never crash the run
        _log.warning("optimizer call failed (%s); degrading to empty output", exc)
        return ""


def _coerce_bool(value: Any) -> bool:
    """Coerce an LLM-supplied truthiness value to a real bool.

    LLMs frequently emit the STRING ``"false"`` (which is truthy in Python) where
    a JSON boolean was requested. Treat the common textual forms explicitly so a
    rejected verdict is never read as acceptance.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1")
    return bool(value)


def _parse_json_safe(text: str, fallback: Any) -> Any:
    """Extract a JSON OBJECT from LLM output, with fallback on parse failure.

    Returns ``fallback`` whenever the text cannot be parsed OR the parsed value is
    not a dict (e.g. the model emitted a bare JSON array): every caller expects an
    object and would ``AttributeError`` on a list/scalar.
    """
    import re
    if not text:
        return fallback
    for pattern in [
        re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL),
        re.compile(r"\{.*\}", re.DOTALL),
    ]:
        m = pattern.search(text)
        if m:
            try:
                candidate = m.group(1) if "```" in pattern.pattern else m.group(0)
                parsed = json.loads(candidate)
            except (json.JSONDecodeError, IndexError):
                continue
            if isinstance(parsed, dict):
                return parsed
    return fallback


# ══════════════════════════════════════════════════════════════════════════════
# Backward-compatible wrappers (called from orchestrator._branch_pass)
# ══════════════════════════════════════════════════════════════════════════════

# Keep the old type alias for backward compatibility with orchestrator.
RolloutValidateFn = Callable[[str, str, "list[str]"], "tuple[float, float]"]


def run_proposal(
    node: "TreeNode",
    l1_signals: list,
    library: "PatternLibrary",
    archive: "NegativeArchive",
    optimizer_client: "LLMClient",
    *,
    cfg: "CSSConfig",
    new_node_id: str,
    epoch: int,
    success_results: "list[TaskResult]" = None,
    persistent_fail_groups: "list[TaskRolloutGroup]" = None,
    rollout_validate_fn: RolloutValidateFn = None,
    env=None,
    target_client: "LLMClient" = None,
    train_groups: "list[TaskRolloutGroup]" = None,
    out_dir: str = "",
) -> "ProposalOutcome":
    """Backward-compatible wrapper — delegates to run_l1_cycle."""
    if env is not None and target_client is not None and train_groups is not None:
        return run_l1_cycle(
            node, library, archive, env, target_client, optimizer_client,
            cfg=cfg, operation="PROPOSAL", new_node_id=new_node_id,
            epoch=epoch, train_groups=train_groups, out_dir=out_dir,
        )
    return ProposalOutcome(
        success=False, operation="PROPOSAL",
        reason="missing_env: run_proposal requires env, target_client, train_groups for v2 L1 cycle",
    )


def run_refine(
    node: "TreeNode",
    l1_signals: list,
    library: "PatternLibrary",
    archive: "NegativeArchive",
    optimizer_client: "LLMClient",
    *,
    cfg: "CSSConfig",
    new_node_id: str,
    epoch: int,
    success_results: "list[TaskResult]" = None,
    persistent_fail_groups: "list[TaskRolloutGroup]" = None,
    rollout_validate_fn: RolloutValidateFn = None,
    env=None,
    target_client: "LLMClient" = None,
    train_groups: "list[TaskRolloutGroup]" = None,
    out_dir: str = "",
) -> "ProposalOutcome":
    """Backward-compatible wrapper — delegates to run_l1_cycle."""
    if env is not None and target_client is not None and train_groups is not None:
        return run_l1_cycle(
            node, library, archive, env, target_client, optimizer_client,
            cfg=cfg, operation="REFINE", new_node_id=new_node_id,
            epoch=epoch, train_groups=train_groups, out_dir=out_dir,
        )
    return ProposalOutcome(
        success=False, operation="REFINE",
        reason="missing_env: run_refine requires env, target_client, train_groups for v2 L1 cycle",
    )
