"""L1 Strategy Cycle — diverse-iterate + objective-lift candidate search (v3).

When L0 saturates, this cycle searches for a better COGNITIVE STRATEGY. It is a
CANDIDATE GENERATOR, not a gatekeeper: it selects the best candidate by an
objective signal and hands it to the tree, whose val/test is the real judge.

  Step 1  One-time grounding analysis (1a/1b/1c parallel → 1d directions)
  Loop (up to cfg.max_l1_iterations rounds, each a DISTINCT philosophy):
    Step 2  Strategy proposal — a NEW cognitive philosophy, ledger-anchored
    Step 3  Test the candidate (new strategy, EMPTY rules) K times on a FIXED
            residual + regression task set
    Categorize vs the node's baseline (pass@K, symmetric is_persistent_fail):
            cracked / still_failed / regressed / maintained → lift, regression
    Step 4  Category-specific contrastive diagnosis (per-trajectory Layer-1
            analyzers → Layer-2 aggregate) — drives the next philosophy
    Keep-best by net_lift; early-stop when a clearly strong candidate appears

After the loop, the best candidate with lift>0 becomes an MCTS child node;
if none cracked any residual task, the last direction is archived.

Why EMPTY rules: testing the strategy alone handicaps it, which makes lift a
conservative LOWER bound on the deployed (post-exploitation, rules-restored)
artifact and regression an UPPER bound — a cracked-under-handicap residual will
(in expectation) also crack once L0 restores rules. Selection stays objective
(no LLM); the LLM is used only to produce rich diagnosis that steers the search.

All intermediate products are persisted under
``{out_dir}/{node_id}/l1_cycle/round_{N}/``.

Heavy imports are lazy so this module imports cheaply.
"""
from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

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
    """One round's full decision record — the unit of the cross-round ledger.

    Assembled in code from each round's objective categorization + the Layer-2
    diagnosis; NO dedicated LLM call generates the record itself. Later rounds
    read it (via :meth:`_IterationContext.render_ledger`) so the search
    accumulates — preserving the active ingredient, avoiding harm, attacking the
    residual — instead of re-deriving from scratch or drifting via depth-refine.
    """
    round: int
    # ── Generation (the philosophy this round explored) ─────────────────────
    philosophy: str = ""            # the declared cognitive philosophy
    mechanism_difference: str = ""  # how it differed in MECHANISM from priors
    strategy_name: str = ""
    strategy_summary: str = ""
    design_reasoning: str = ""
    was_refine: bool = False        # True if this round REFINED the prior strategy
    # ── Objective categorization vs baseline (pass@K, NO LLM) ───────────────
    lift: int = 0                   # #cracked (residual unlocked)
    regression: int = 0             # #regressed (solved task broken)
    net_lift: int = 0               # lift - regression
    n_residual: int = 0             # residual tasks tested this cycle
    n_regression: int = 0           # regression-guard tasks tested this cycle
    cracked_task_ids: list = field(default_factory=list)
    regressed_task_ids: list = field(default_factory=list)
    still_failed_task_ids: list = field(default_factory=list)
    # ── Layer-2 aggregate diagnosis (LLM; steers the next philosophy) ───────
    # {active_ingredient, harm, residual_characterization, residual_nature,
    #  next_direction_hint}
    diagnosis: dict = field(default_factory=dict)
    # Set instead of the above when Step 2 produced no usable strategy.
    failure_note: str = ""

    @property
    def residual_nature(self) -> str:
        return str((self.diagnosis or {}).get("residual_nature", "") or "")


@dataclass
class _IterationContext:
    iteration_round: int = 1
    previous_attempts: list[_PreviousAttempt] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Full serialization (disk / observability)."""
        return {
            "iteration_round": self.iteration_round,
            "previous_attempts": [
                {
                    "round": a.round,
                    "philosophy": a.philosophy,
                    "mechanism_difference": a.mechanism_difference,
                    "strategy_name": a.strategy_name,
                    "strategy_summary": a.strategy_summary,
                    "design_reasoning": a.design_reasoning,
                    "was_refine": a.was_refine,
                    "lift": a.lift,
                    "regression": a.regression,
                    "net_lift": a.net_lift,
                    "n_residual": a.n_residual,
                    "n_regression": a.n_regression,
                    "cracked_task_ids": a.cracked_task_ids,
                    "regressed_task_ids": a.regressed_task_ids,
                    "still_failed_task_ids": a.still_failed_task_ids,
                    "diagnosis": a.diagnosis,
                    "failure_note": a.failure_note,
                }
                for a in self.previous_attempts
            ],
        }

    def render_ledger(self) -> str:
        """Render the cross-round decision ledger as a distilled, readable text
        block for prompt injection (Step 2 generation).

        Per round it tells the next philosophy designer four things it must act
        on: the PHILOSOPHY already tried (do not repeat its mechanism), the
        OBJECTIVE result (lift/regression — what truly worked), the ACTIVE
        INGREDIENT to preserve, the HARM to avoid, and the RESIDUAL still open.
        Assembled purely from recorded fields — no raw JSON or trajectories.
        """
        if not self.previous_attempts:
            return "(no prior rounds — this is the first philosophy in this cycle)"
        blocks: list[str] = []
        for a in self.previous_attempts:
            if a.failure_note:
                blocks.append(
                    f"=== Round {a.round} ===\n"
                    f"Philosophy: (Step 2 produced no usable strategy)\n"
                    f"Note: {a.failure_note}"
                )
                continue
            d = a.diagnosis or {}
            cracked = ", ".join(str(t) for t in a.cracked_task_ids[:8]) or "none"
            regressed = ", ".join(str(t) for t in a.regressed_task_ids[:8]) or "none"
            mode = "REFINE of prior" if a.was_refine else "NEW philosophy"
            verdict = "EFFECTIVE (lift>0)" if a.lift > 0 else "ineffective (lift 0)"
            blocks.append(
                f"=== Round {a.round} [{mode}]: {a.strategy_name or '(unnamed)'} — {verdict} ===\n"
                f"Philosophy: {a.philosophy or '(undeclared)'}\n"
                f"How it differed in mechanism: {a.mechanism_difference or '(unstated)'}\n"
                f"Objective result: lift +{a.lift} (cracked: {cracked}) | "
                f"regression -{a.regression} (regressed: {regressed}) | "
                f"net {a.net_lift:+d} | still-failed {len(a.still_failed_task_ids)}/{a.n_residual}\n"
                f"Active ingredient (PRESERVE): {(d.get('active_ingredient') or '(none found)')[:300]}\n"
                f"Harm (AVOID): {(d.get('harm') or '(no strategy-level harm)')[:250]}\n"
                f"Residual still open: {(d.get('residual_characterization') or '?')[:250]} "
                f"[nature: {d.get('residual_nature', '?')} — if L0_tactical, leave it to L0]\n"
                f"  -> next-direction hint (cognitive): {(d.get('next_direction_hint') or '(none)')[:250]}"
            )
        return "\n\n".join(blocks)


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
You are synthesizing analysis into the NEXT strategic hypothesis for improving an \
AI agent's cognitive strategy. You receive TWO kinds of input:

A. FIXED ANALYSIS of the agent's post-exploitation state (computed once; identical \
every round of this cycle):
   1. L0 CEILING ANALYSIS — why tactical optimization stalled
   2. TRAJECTORY ANALYSIS — deep behavioral patterns from failure traces
   3. CONTRASTIVE LIMITATION ANALYSIS — why L0-identified divergences couldn't be \
fixed with rules

B. CYCLE LEDGER — what PRIOR ROUNDS of this same cycle already tried: each round's \
hypothesis/direction, the strategy, its OBJECTIVE result (pass rate), the verdict, \
and the post-mortem of WHY it failed (e.g. "the agent followed it but anchored to a \
wrong logical hypothesis"). Empty on the first round.

CRITICAL — synthesize ACROSS rounds; do NOT re-derive from scratch:
- The FIXED analysis (A) will keep suggesting the SAME high-level framing every \
round. The LEDGER (B) is authoritative on what has actually been tried and ruled \
out. If a direction was already tried, do NOT re-propose it under a new name.
- Build on what the post-mortems established. If prior rounds established the agent \
now FOLLOWS a structured approach but still fails because of X, the open problem is \
X — attack THAT, not the already-solved framing.
- PRESERVE what worked: anything the ledger shows as effective (e.g. a format the \
agent reliably adheres to) is a constraint to keep, not discard.

GROUND-TRUTH CONSTRAINT — the agent has NO access to ground-truth/expected answers \
at runtime. NEVER recommend a direction that requires comparing against or reverse- \
engineering from expected/ground-truth values; the agent cannot do it.

Output a JSON object:
{
  "cycle_synthesis": {
    "established": ["<what prior rounds CONFIRMED works — [] on round 1>"],
    "ruled_out": ["<directions already tried that are NOT the bottleneck — [] on round 1>"],
    "open_problem": "<the current binding constraint the next strategy must attack>"
  },
  "core_assumptions_and_limitations": "<key strategic limitation behind the open_problem>",
  "recommended_directions": [
    {
      "direction": "<the strategic change — MUST attack open_problem and differ from ruled_out>",
      "rationale": "<why this addresses the open problem, given what's already been tried>",
      "expected_impact": "<what types of tasks would benefit and how>",
      "risk": "<what could go wrong or what effective behaviors might be lost>"
    }
  ],
  "constraints": "<what is working well (from the current strategy AND prior rounds) that must be preserved>"
}

Output ONLY the JSON object — no prose, no fences."""


# ══════════════════════════════════════════════════════════════════════════════
# Step 2: Strategy proposal — a NEW cognitive philosophy, ledger-anchored
# ══════════════════════════════════════════════════════════════════════════════

_STEP2_SYSTEM = """\
You design a COGNITIVE STRATEGY — the document injected into an agent's SYSTEM \
PROMPT that tells it HOW TO THINK when approaching tasks.

ALTITUDE — THE ONE RULE YOU MUST NOT BREAK. L1 searches COGNITIVE STRATEGIES (ways of \
THINKING). It does NOT learn tactical rules. The system has a strict division of \
labour: YOU produce the thinking frame; a SEPARATE L0 optimizer then adds tactical \
rules (exact APIs, formats, idioms) on top of your strategy. Therefore:
  - GOOD (strategy): "Form a structural hypothesis about the data before acting."
  - BAD (tactical rule — NEVER write this): "Compute values in Python and write \
literals, not formula strings"; "preserve the header row"; "use exact date format".
  - When the diagnosis says the residual is tactical (formula strings, formats, \
headers, exact matching), that residual is L0's JOB. Do NOT try to fix it by encoding \
tactics into your strategy. Leave it. Stay at the altitude of THINKING. A strategy \
polluted with tactical rules is a failed strategy even if it happens to pass.

You operate in one of two MODES (given at the top of the input):

▸ MODE = NEW — propose a strategy on a GENUINELY DIFFERENT cognitive MECHANISM from \
every philosophy in the ledger. This is diverse exploration: do not re-propose a tried \
philosophy under a new name; state how yours differs in mechanism (not just wording). \
Drift into ever-more-elaborate variants of the same idea is the failure mode to avoid.

▸ MODE = REFINE — the CURRENT strategy (given in full) was tested and cracked NOTHING \
(lift 0), but its core cognitive idea looks sound and is worth one more try. KEEP its \
core philosophy; improve its OPERATIONALIZATION so the agent actually follows and \
benefits from it — per the diagnosis (e.g. it was too abstract / not enacted in the \
Thought→Action loop / a key thinking move was under-specified). Do NOT switch to an \
unrelated idea, and do NOT pile on tactics — same frame, made to actually work.

You receive a one-time GROUNDING analysis and a CYCLE LEDGER (every prior round's \
philosophy, its objective lift/regression, and its diagnosis: the active ingredient \
that worked, the harm to avoid, the residual). Use them under BOTH modes:
1. PRESERVE the active ingredient — anything the ledger shows OBJECTIVELY cracked \
tasks is a thinking behavior to KEEP; re-express it, never drop it.
2. AVOID the harm — never re-introduce a genuine strategy-level harm. ("Handicap" \
regressions are NOT harm; they vanish once L0 restores rules — do not contort to avoid them.)
3. PURSUE cognitive leverage — target failures a better WAY OF THINKING can unlock; \
leave purely tactical residual to L0.

STRATEGY FORMAT — two sections, nothing else:

  ## <Strategy Name>
  <A concise paragraph: the core mental model, the key insight, why this way of \
thinking is effective. Graspable in 30 seconds.>

  ### Details
  <Detailed expansion: cognitive mechanisms, thinking moves, when-to-switch triggers. \
Multiple paragraphs / #### sub-sections / bullets as needed. The agent reading ONLY \
this should know exactly HOW to think — not what API to call.>

FOLLOWABILITY — the agent runs in a ReAct loop that forces an Action every turn. Lead \
with a few OPERABLE mental moves it can enact inside the Thought→Action loop, each \
observable in a Thought line, stated briefly enough not to be skimmed. It is tested \
with NO tactical rules, so it must be SELF-CONTAINED — but self-contained as a way of \
THINKING, never by smuggling in tactics.

Output a JSON object:
{
  "philosophy": "<the core cognitive philosophy of THIS strategy, in one or two sentences>",
  "mechanism_difference": "<MODE=NEW: how this differs in COGNITIVE MECHANISM from every \
prior philosophy ('(first round)' if ledger empty). MODE=REFINE: what you changed in the \
operationalization and why, keeping the same core philosophy>",
  "strategy_text": "<full strategy.md body: ## Name + overview + ### Details>",
  "design_reasoning": "<why this pursues cognitive leverage while preserving the active \
ingredient and avoiding the harm — and why it stays at thinking altitude>"
}

Output ONLY the JSON object — no prose, no fences."""


# ══════════════════════════════════════════════════════════════════════════════
# Step 4: Category-specific contrastive diagnosis (Layer-1 analyzers + Layer-2)
# ══════════════════════════════════════════════════════════════════════════════
# The candidate was categorized OBJECTIVELY (no LLM) vs the baseline. The LLM's
# only job here is to explain WHY, to steer the next philosophy. Each Layer-1
# analyzer sees ONE task in depth (a contrast pair, or a single trajectory) — never
# all trajectories at once — then Layer-2 synthesizes the per-task analyses.

_CRACKED_ANALYZER_SYSTEM = """\
You analyze WHY a new cognitive strategy UNLOCKED a task the baseline could not solve.

Setup:
- The BASELINE agent (prior strategy + FULL tactical rules) FAILED this task on every attempt.
- The CANDIDATE agent (the NEW strategy, with NO tactical rules) SUCCEEDED.
- The candidate had no rules, so its COGNITIVE FRAME — not tactical detail — is what made the difference.

You receive: the strategy under test, ONE candidate SUCCESS trajectory, and ONE \
baseline FAILURE trajectory of the SAME task.

Compare the two move by move. Find the ACTIVE INGREDIENT: the specific cognitive \
move, framing, check, or decision the strategy induced in the candidate that the \
baseline never made — the thing that turned failure into success. Pinpoint the \
exact divergence point and quote both trajectories.

Constraints:
- The active ingredient must be a STRATEGY-level cognitive behavior the agent can \
reproduce on OTHER tasks — not a one-off tactical trick (the candidate had no rules \
to give it tactical tricks anyway).
- GROUND TRUTH: the agent never sees expected/ground-truth answers at runtime; the \
active ingredient must be doable WITHOUT them (you may read expected values to \
understand WHY it worked, but the deployed agent never has them).

Output a JSON object:
{
  "task_id": "<id>",
  "active_ingredient": "<the specific strategy-induced cognitive move that unlocked this task>",
  "baseline_missing": "<what the baseline did instead / failed to do at the same decision point>",
  "evidence": "<quotes/actions from BOTH trajectories pinpointing the divergence>",
  "generalizable": "<whether this likely helps other residual tasks, and which kinds>"
}

Output ONLY the JSON object — no prose, no fences."""


_REGRESSED_ANALYZER_SYSTEM = """\
You analyze WHY a new cognitive strategy BROKE a task the baseline solved — and, \
crucially, whether the strategy is actually at fault.

Setup:
- The BASELINE agent (prior strategy + FULL tactical rules) SOLVED this task.
- The CANDIDATE agent (the NEW strategy, with NO tactical rules) FAILED it.
- TWO things changed at once: the strategy changed AND the tactical rules were \
removed. You MUST separate their effects.

Classify the failure cause:
- "handicap": the candidate pursued a SOUND approach but tripped on a concrete \
TACTICAL detail the baseline's rules supplied (a specific openpyxl idiom, a known \
edge case, an exact range). This is EXPECTED and NOT the strategy's fault — once \
this strategy is deployed, the L0 optimizer re-adds tactical rules and this failure \
very likely disappears.
- "harm": the new strategy's COGNITIVE FRAME actively MISLED the agent — directed \
its attention wrongly, imposed a wrong mental model, or induced a counter-productive \
procedure the baseline never followed. This IS the strategy's fault and must be fixed.

You receive: the strategy under test, ONE baseline SUCCESS trajectory, and ONE \
candidate FAILURE trajectory of the SAME task.

Compare them at the point they diverge. Decide handicap vs harm from the EVIDENCE: a \
sound approach stumbling on a tactical detail is handicap; the new strategy steering \
the agent into a wrong approach is harm. When in genuine doubt, prefer "handicap" \
(do not penalize the strategy for missing rules) — but call clear misdirection "harm".

GROUND TRUTH: you may use the expected values you see to understand the divergence, \
but the deployed agent has none — your handicap/harm call and harm_detail must hold \
without them and must not instruct using them.

Output a JSON object:
{
  "task_id": "<id>",
  "failure_cause": "handicap | harm",
  "harm_detail": "<if harm: exactly how the new strategy misled the agent; if handicap: which tactical detail/rule was missing>",
  "evidence": "<quotes/actions at the divergence point supporting the classification>"
}

Output ONLY the JSON object — no prose, no fences."""


_STILLFAILED_ANALYZER_SYSTEM = """\
You characterize a RESIDUAL failure — a task BOTH the baseline and the new strategy \
fail. There is no successful trajectory to contrast against; characterize the \
difficulty from the failure alone.

You receive: the strategy under test and ONE candidate FAILURE trajectory (the new \
strategy, no tactical rules).

Determine:
1. The concrete POINT the agent gets wrong (where, in the trajectory, it goes off).
2. The NATURE of the residual difficulty:
   - "reasoning": the agent's approach/logic is wrong — a better cognitive strategy \
could still fix it (L1-addressable).
   - "tactical": the approach is sound but the agent trips on a concrete, recurring \
tactical/syntactic detail a specific RULE would fix (L0-addressable; expected to \
improve once rules are restored).
   - "perception": the agent misreads the task instruction or the spreadsheet \
structure before reasoning even begins.
   - "capability": the task needs an operation or precision the model simply cannot \
produce, regardless of strategy or rules.
3. A NEXT-DIRECTION HINT: if reasoning/perception, what KIND of cognitive frame might \
crack it next; otherwise, why a strategy cannot help.

GROUND TRUTH: the agent never sees expected/ground-truth answers; propose nothing \
that needs them (you may read expected values to understand the failure; the \
deployed agent cannot).

Output a JSON object:
{
  "task_id": "<id>",
  "residual_point": "<the concrete thing the agent gets wrong>",
  "residual_nature": "reasoning | tactical | perception | capability",
  "next_direction_hint": "<for reasoning/perception: what cognitive frame might address it; else why strategy can't help>"
}

Output ONLY the JSON object — no prose, no fences."""


_DIAGNOSE_AGGREGATE_SYSTEM = """\
You synthesize per-task analyses from ONE round of L1 STRATEGY search into a single \
actionable diagnosis that steers the NEXT round.

ALTITUDE — THIS IS THE MOST IMPORTANT CONSTRAINT. L1 searches COGNITIVE STRATEGIES \
(how the agent THINKS), NOT tactical rules (what exact API call / format to use). The \
two-layer system has a strict division of labour: L1 finds the thinking frame; a \
SEPARATE L0 optimizer then adds the tactical rules on top. So:
- A residual that is tactical (e.g. "writes a formula string instead of a computed \
value", "wrong date format", "didn't preserve the header row") is L0's job. It is the \
EXPECTED, normal leftover of any cognitive frame — NOT a failure of L1, and NOT \
something the next strategy should try to fix by encoding tactics.
- Your "next_direction_hint" MUST stay at cognitive altitude: a DIFFERENT WAY OF \
THINKING. It must NEVER be a list of tactical rules (do-compute-literals, \
preserve-headers, exact-match-format). If you catch yourself prescribing rules, you \
are at the wrong altitude — re-express as a thinking habit or re-frame, or redirect to \
a different cognitive leverage point entirely.

This round a candidate strategy (the new cognitive frame, tested with NO tactical \
rules) was compared against the baseline on a fixed task set. You receive:
- CRACKED analyses: tasks the strategy unlocked — each names the ACTIVE INGREDIENT.
- REGRESSED analyses: tasks the strategy broke — each classified "handicap" (missing \
tactical rule; EXPECTED; the L0 optimizer fixes it; NOT the strategy's fault) or \
"harm" (the strategy actively misled).
- STILL-FAILED analyses: residual tasks neither solved — each with its nature.
- The objective counts (lift = residual cracked, regression, net_lift).

Synthesize ACROSS tasks:
1. ACTIVE INGREDIENT — the consistent cognitive behavior(s) that produced the cracks; \
what to preserve. If nothing cracked, say so plainly.
2. HARM — only genuine "harm" regressions (IGNORE every "handicap"). What to AVOID. If \
all regressions were handicap, state there is no strategy-level harm.
3. RESIDUAL CHARACTERIZATION — the dominant pattern among still-failed tasks.
4. RESIDUAL NATURE — "L1_solvable" (a DIFFERENT cognitive frame could still crack some \
of these), "L0_tactical" (the thinking is fine; only tactical rules remain — L0's \
job), or "capability_limit" (the model cannot do it regardless).
5. NEXT DIRECTION HINT — a DIFFERENT cognitive mechanism for the next strategy, at \
cognitive altitude (a way of thinking, never tactical rules). Leave tactical residual \
to L0.
6. NEXT ACTION — choose how the next round should proceed. The key signal is the
OBJECTIVE counts plus your handicap-vs-harm split (deploy_net = lift - harm_regressions
is the post-exploitation net; handicap regressions recover once L0 restores rules):
   - "propose_new" — the cognitive frame is sound and worth banking / moving on. Choose \
this when the strategy cracked residual (lift > 0) AND its regressions are mostly \
HANDICAP (deploy_net >= 0) — its job is done, explore a DIFFERENT frame; OR when lift \
== 0 and the core idea looks WRONG (a dead end to abandon).
   - "refine_current" — the SAME idea should be improved next round. Choose this when \
lift == 0 but the core idea looks SOUND and merely poorly operationalized (the agent \
didn't follow it, or a key thinking move was under-specified); OR when lift > 0 but the \
regressions are dominated by genuine HARM (deploy_net < 0 — the frame actively misleads) \
and that harm looks removable while keeping the cracks. Never refine to add tactics.

Output a JSON object:
{
  "active_ingredient": "<consistent cognitive behavior(s) to preserve — '' if nothing cracked>",
  "harm": "<genuine strategy-level harm to avoid — '' if only handicap regressions>",
  "residual_characterization": "<dominant residual failure pattern>",
  "residual_nature": "L1_solvable | L0_tactical | capability_limit",
  "next_direction_hint": "<a DIFFERENT cognitive mechanism (a way of thinking) — NEVER tactical rules>",
  "next_action": "propose_new | refine_current",
  "next_action_reason": "<one line: why, grounded in the objective lift>"
}

Output ONLY the JSON object — no prose, no fences."""


# ══════════════════════════════════════════════════════════════════════════════
# Core L1 cycle implementation
# ══════════════════════════════════════════════════════════════════════════════

def _is_better_candidate(cand: dict, best: "dict | None") -> bool:
    """Keep-best ranking among EFFECTIVE candidates: maximize net_lift, then (on a
    tie) deploy_net = lift - harm_reg (the post-exploitation net lower bound — prefer
    the candidate whose regressions are more recoverable handicap and less genuine
    harm), then lift, then fewer regressions. Earliest wins ties (strict ``>`` never
    replaces an equal). Caller guarantees the candidate is effective.
    """
    if best is None:
        return True
    return (cand["net_lift"], cand["deploy_net"], cand["lift"], -cand["regression"]) > (
        best["net_lift"], best["deploy_net"], best["lift"], -best["regression"]
    )


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
    """Diverse-iterate L1 search — a CANDIDATE GENERATOR, not a gatekeeper.

    Against the node's baseline (``train_groups`` = its post-exploitation full-skill
    rollout), it fixes a residual + regression test set ONCE, then each round:
    produces a COGNITIVE strategy (Step 2), tests it with EMPTY rules K times
    (Step 3), categorizes it OBJECTIVELY vs the baseline (lift/regression, no LLM),
    and runs a category-specific contrastive diagnosis (Step 4) that steers the next
    round. The cycle stays at STRATEGY altitude — tactical residual is left to L0.

    Each round runs in one of two modes, decided objectively from the prior round:
      - a strategy that was EFFECTIVE (lift>0) is banked and the next round explores
        a NEW, mechanism-different philosophy (diverse exploration);
      - a strategy that cracked nothing (lift==0) is REFINED once (same idea, better
        operationalization) if the diagnosis judges its core sound, else abandoned
        for a new philosophy.

    It stops when it has collected ``l1_target_effective`` (default 3) effective
    strategies OR exhausts ``max_l1_iterations``, then returns the best effective one
    (by ``net_lift``) for the tree to judge on val/test. If nothing cracked any
    residual task, the last direction is archived.
    """
    cycle_dir = os.path.join(out_dir, node.node_id, "l1_cycle")
    os.makedirs(cycle_dir, exist_ok=True)

    max_iters = cfg.max_l1_iterations
    max_workers = getattr(cfg, "max_api_workers", 32)
    iteration_ctx = _IterationContext(iteration_round=1)

    # ── Fixed test set + baseline map (computed ONCE; identical every round) ──
    residual_items, regression_items, baseline_map = _select_l1_test_set(env, train_groups, cfg)
    test_items = residual_items + regression_items
    n_residual = len(residual_items)
    residual_ids = [tid for tid, g in baseline_map.items() if g.is_persistent_fail()]
    regression_ids = [tid for tid, g in baseline_map.items() if not g.is_persistent_fail()]
    _save_json(os.path.join(cycle_dir, "test_set.json"), {
        "operation": operation, "n_residual": n_residual,
        "n_regression": len(regression_items),
        "residual_task_ids": residual_ids, "regression_task_ids": regression_ids,
    })
    _log.info("L1 cycle for node %s: %d residual + %d regression tasks (baseline-derived)",
              node.node_id, n_residual, len(regression_items))

    if not test_items:
        _save_json(os.path.join(cycle_dir, "final_outcome.json"),
                   {"success": False, "operation": operation, "reason": "empty_test_set"})
        return ProposalOutcome(
            success=False, operation=operation,
            reason="empty_test_set: no residual/regression tasks resolvable from train_groups",
            n_iterations=0,
        )

    # ── One-time Step-1 grounding (1a/1b/1c cached + 1d directions) ──────────
    # The node's post-exploitation state is fixed, so this is computed ONCE and
    # fed to every round's Step 2 as the starting point; the LEDGER then steers.
    grounding_dir = os.path.join(cycle_dir, "grounding")
    grounding_ckpt = os.path.join(grounding_dir, "grounding.json")
    if os.path.exists(grounding_ckpt):
        _ckpt = _load_json(grounding_ckpt)
        analyses_1abc = _ckpt.get("analyses_1abc", {})
        grounding = _ckpt.get("grounding", {})
        _log.info("L1 cycle: loaded Step 1 grounding from checkpoint")
    else:
        fail_results = [r for g in train_groups for r in g.rollouts if not getattr(r, "passed", False)]
        analyses_1abc = _run_step1_analyses(
            optimizer_client, node, train_groups, fail_results, cfg=cfg,
            max_workers=max_workers, round_dir=grounding_dir,
        )
        grounding = _run_step1d(
            optimizer_client, analyses_1abc, _IterationContext(), round_dir=grounding_dir,
        )
        _save_json(grounding_ckpt, {
            "analyses_1abc": analyses_1abc, "grounding": grounding,
        })

    last_strategy = ""
    best: dict | None = None  # best EFFECTIVE candidate {strategy_text, iteration, lift, regression, net_lift}
    best_overall: dict | None = None  # best candidate regardless of effectiveness (fallback)
    n_effective = 0           # number of rounds with lift>0 (effective strategies collected)
    target_effective = int(getattr(cfg, "l1_target_effective", 3))
    next_mode = "new"         # generation mode for the upcoming round (round 1 = NEW)
    refine_target = ""        # strategy text to refine when next_mode == "refine"
    start_iteration = 1       # first round to actually run (after resume)

    # ── Resume: find the last fully-completed round and restore state ──────
    for _probe in range(max_iters, 0, -1):
        _probe_ckpt = os.path.join(cycle_dir, f"round_{_probe:04d}", "iteration_context.json")
        if not os.path.exists(_probe_ckpt):
            continue
        _saved_ctx = _load_json(_probe_ckpt)
        prev_attempts = _saved_ctx.get("previous_attempts", [])
        if not prev_attempts:
            break
        # Reconstruct iteration_ctx from the latest completed round
        iteration_ctx = _IterationContext(
            iteration_round=int(_saved_ctx.get("iteration_round", _probe + 1)),
            previous_attempts=[
                _PreviousAttempt(
                    round=int(a.get("round", 0)),
                    philosophy=str(a.get("philosophy", "") or ""),
                    mechanism_difference=str(a.get("mechanism_difference", "") or ""),
                    strategy_name=str(a.get("strategy_name", "") or ""),
                    strategy_summary=str(a.get("strategy_summary", "") or ""),
                    design_reasoning=str(a.get("design_reasoning", "") or ""),
                    was_refine=bool(a.get("was_refine", False)),
                    lift=int(a.get("lift", 0)),
                    regression=int(a.get("regression", 0)),
                    net_lift=int(a.get("net_lift", 0)),
                    n_residual=int(a.get("n_residual", 0)),
                    n_regression=int(a.get("n_regression", 0)),
                    cracked_task_ids=list(a.get("cracked_task_ids", [])),
                    regressed_task_ids=list(a.get("regressed_task_ids", [])),
                    still_failed_task_ids=list(a.get("still_failed_task_ids", [])),
                    diagnosis=dict(a.get("diagnosis", {}) or {}),
                    failure_note=str(a.get("failure_note", "") or ""),
                )
                for a in prev_attempts
            ],
        )
        # Replay accumulated state: best candidate, n_effective, last_strategy
        for a in iteration_ctx.previous_attempts:
            if a.failure_note:
                continue
            effective_a = a.lift > 0 and a.net_lift >= 0
            # Recover the FULL strategy text from the round's Step 2 checkpoint
            # (strategy_summary in the ledger is truncated to 500 chars).
            _full_strat = a.strategy_summary
            _step2_path = os.path.join(
                cycle_dir, f"round_{a.round:04d}", "step2", "strategy_proposal.json",
            )
            if os.path.exists(_step2_path):
                try:
                    _full_strat = _load_json(_step2_path).get("strategy_text", _full_strat)
                except Exception:
                    pass
            harm_a = int((a.diagnosis or {}).get("harm_reg", 0) or 0)
            deploy_a = int((a.diagnosis or {}).get("deploy_net", a.lift - harm_a))
            cand_a = {
                "strategy_text": _full_strat,
                "iteration": a.round, "lift": a.lift,
                "regression": a.regression, "net_lift": a.net_lift,
                "harm_reg": harm_a, "deploy_net": deploy_a,
            }
            if effective_a:
                n_effective += 1
                if _is_better_candidate(cand_a, best):
                    best = cand_a
            if _is_better_candidate(cand_a, best_overall):
                best_overall = cand_a
            last_strategy = _full_strat or last_strategy

        # Derive next_mode from the last completed round
        last_att = prev_attempts[-1]
        if last_att.get("failure_note"):
            next_mode, refine_target = "new", ""
        elif last_att.get("lift", 0) > 0 or last_att.get("was_refine", False):
            next_mode, refine_target = "new", ""
        else:
            na = str((last_att.get("diagnosis") or {}).get("next_action", "") or "").strip().lower()
            if na == "refine_current":
                next_mode = "refine"
                refine_target = last_att.get("strategy_summary", "")
            else:
                next_mode, refine_target = "new", ""

        start_iteration = _probe + 1
        _log.info("L1 cycle: resumed from round %d checkpoint (n_effective=%d, "
                  "start_iteration=%d)", _probe, n_effective, start_iteration)
        break

    if n_effective >= target_effective:
        _log.info("L1: already collected %d effective strategies from checkpoint — done",
                  n_effective)
        # Fall through to the post-loop best-selection logic below.

    for iteration in range(start_iteration, max_iters + 1):
        if n_effective >= target_effective:
            break
        round_dir = os.path.join(cycle_dir, f"round_{iteration:04d}")
        os.makedirs(round_dir, exist_ok=True)

        mode = next_mode
        _log.info("L1 round %d/%d (node %s) [mode=%s]", iteration, max_iters, node.node_id, mode)

        # ── Step 2: produce a cognitive strategy (NEW philosophy or REFINE) ──
        step2_ckpt = os.path.join(round_dir, "step2", "strategy_proposal.json")
        if os.path.exists(step2_ckpt):
            step2 = _load_json(step2_ckpt)
            _log.info("L1 round %d: loaded Step 2 from checkpoint", iteration)
        else:
            step2 = _run_step2(optimizer_client, node, grounding, iteration_ctx,
                               cfg=cfg, round_dir=round_dir,
                               mode=mode, refine_target=refine_target)
        if not step2 or not (step2.get("strategy_text") or "").strip():
            note = "Step 2 produced no usable strategy proposal (empty or unparseable)."
            _save_json(os.path.join(round_dir, "step2", "error.json"), {"error": note})
            iteration_ctx.previous_attempts.append(
                _PreviousAttempt(round=iteration, failure_note=note, was_refine=(mode == "refine")))
            iteration_ctx.iteration_round = iteration + 1
            next_mode, refine_target = "new", ""  # never refine a missing strategy
            continue

        strategy_text = step2["strategy_text"]
        last_strategy = strategy_text

        # ── Step 3: test candidate (strategy, EMPTY rules) K times ──────────
        step3_ckpt = os.path.join(round_dir, "step3", "candidate_groups.json")
        if os.path.exists(step3_ckpt):
            from css.data.rollout import TaskRolloutGroup
            candidate_groups = [TaskRolloutGroup.from_dict(d) for d in _load_json(step3_ckpt)]
            _log.info("L1 round %d: loaded Step 3 candidate_groups from checkpoint", iteration)
        else:
            candidate_groups = _run_step3(
                env, target_client, strategy_text, test_items,
                cfg=cfg, round_dir=round_dir, epoch=epoch, node_id=node.node_id,
            )
            _save_json(step3_ckpt, [g.to_dict() for g in candidate_groups])

        # ── Objective categorization vs baseline (pass@K, NO LLM) ───────────
        cats = _categorize(candidate_groups, baseline_map)
        _save_json(os.path.join(round_dir, "categorization.json"), {
            k: cats[k] for k in (
                "cracked", "still_failed", "regressed", "maintained",
                "lift", "regression", "net_lift", "n_residual", "n_regression")
        })

        # ── Step 4: category-specific contrastive diagnosis (steers next) ───
        # Also yields the OBJECTIVE harm_reg / deploy_net (= lift - harm_reg, the
        # post-exploitation net lower bound) used for keep-best tie-breaking.
        step4_ckpt = os.path.join(round_dir, "step4", "diagnosis.json")
        if os.path.exists(step4_ckpt):
            _diag_data = _load_json(step4_ckpt)
            diagnosis = _diag_data.get("aggregate", _diag_data)
            _log.info("L1 round %d: loaded Step 4 diagnosis from checkpoint", iteration)
        else:
            diagnosis = _diagnose_round(
                optimizer_client, strategy_text, cats, candidate_groups, baseline_map,
                cfg=cfg, max_workers=max_workers, round_dir=round_dir,
            )
        harm_reg = int((diagnosis or {}).get("harm_reg", 0) or 0)
        deploy_net = int((diagnosis or {}).get("deploy_net", cats["lift"] - harm_reg))

        # ── Keep-best tracking ──
        # Track ALL candidates for fallback deployment, plus strictly
        # EFFECTIVE ones (lift>0 AND net_lift>=0) for primary selection.
        cand = {
            "strategy_text": strategy_text, "iteration": iteration,
            "lift": cats["lift"], "regression": cats["regression"],
            "net_lift": cats["net_lift"], "harm_reg": harm_reg, "deploy_net": deploy_net,
        }
        effective = cats["lift"] > 0 and cats["net_lift"] >= 0
        if effective:
            n_effective += 1
            if _is_better_candidate(cand, best):
                best = cand
        if _is_better_candidate(cand, best_overall):
            best_overall = cand
        _log.info("L1 round %d [mode=%s]: lift +%d / regression -%d (harm %d) / net %+d / "
                  "deploy_net %+d -> %s (effective so far: %d/%d)",
                  iteration, mode, cats["lift"], cats["regression"], harm_reg,
                  cats["net_lift"], deploy_net,
                  "EFFECTIVE" if effective else "ineffective", n_effective, target_effective)

        # ── Append this round to the cross-round ledger ─────────────────────
        iteration_ctx.previous_attempts.append(_PreviousAttempt(
            round=iteration,
            philosophy=str(step2.get("philosophy", "") or ""),
            mechanism_difference=str(step2.get("mechanism_difference", "") or ""),
            strategy_name=_strategy_name(strategy_text),
            strategy_summary=strategy_text[:500],
            design_reasoning=str(step2.get("design_reasoning", "") or ""),
            was_refine=(mode == "refine"),
            lift=cats["lift"], regression=cats["regression"], net_lift=cats["net_lift"],
            n_residual=cats["n_residual"], n_regression=cats["n_regression"],
            cracked_task_ids=list(cats["cracked"]),
            regressed_task_ids=list(cats["regressed"]),
            still_failed_task_ids=list(cats["still_failed"]),
            diagnosis=diagnosis,
        ))
        iteration_ctx.iteration_round = iteration + 1
        _save_json(os.path.join(round_dir, "iteration_context.json"), iteration_ctx.to_dict())

        # ── Exit: collected enough effective strategies to choose from ──────
        if n_effective >= target_effective:
            _log.info("L1: collected %d effective strategies — stopping (will pick best)", n_effective)
            break

        # ── Decide the NEXT round's generation mode ─────────────────────────
        #   effective (lift>0)               -> NEW  (bank it; explore a different frame)
        #   ineffective AND already a refine  -> NEW  (one refine per idea — abandon)
        #   ineffective, first attempt        -> diagnosis next_action (refine_current|propose_new)
        if effective or mode == "refine":
            next_mode, refine_target = "new", ""
        else:
            na = str((diagnosis or {}).get("next_action", "") or "").strip().lower()
            if na == "refine_current":
                next_mode, refine_target = "refine", strategy_text
            else:
                next_mode, refine_target = "new", ""

    n_done = iteration_ctx.iteration_round - 1

    # ── Decide: ALWAYS deploy the best candidate ─────────────────────────
    # Priority: (1) best effective candidate (lift>0 AND net_lift>=0),
    # (2) best overall by same ranking (net_lift, deploy_net, lift, -regression).
    # There is no failure case — we always deploy and let L0 exploitation try.
    selected = best if best is not None else best_overall
    selection_reason = "effective" if best is not None else "fallback_best_overall"

    if selected is not None:
        best_strat = selected["strategy_text"]
        new_node = _build_node(
            node, new_node_id=new_node_id, branch_type=operation,
            strategy=best_strat, rules="",
            refine_count=node.refine_count, epoch=epoch,
        )
        _save_json(os.path.join(cycle_dir, "final_outcome.json"), {
            "success": True, "operation": operation, "new_node_id": new_node.node_id,
            "selected_round": selected["iteration"], "lift": selected["lift"],
            "regression": selected["regression"], "net_lift": selected["net_lift"],
            "harm_reg": selected.get("harm_reg", 0), "deploy_net": selected.get("deploy_net", 0),
            "n_effective": n_effective, "n_iterations": n_done,
            "selection_reason": selection_reason,
        })
        _log.info("L1 cycle DEPLOY (%s): best = round %d "
                  "(lift +%d, regression -%d, net %+d, deploy_net %+d) -> node %s; "
                  "tree val/test is the final judge",
                  selection_reason, selected["iteration"], selected["lift"],
                  selected["regression"], selected["net_lift"],
                  selected.get("deploy_net", 0), new_node.node_id)
        return ProposalOutcome(
            success=True, operation=operation, new_node=new_node,
            reason=(f"{selection_reason}: round {selected['iteration']} "
                    f"lift={selected['lift']} net={selected['net_lift']} "
                    f"deploy_net={selected.get('deploy_net', 0)}"),
            n_iterations=n_done,
        )

    # All rounds failed to produce any strategy (Step 2 errors) — archive.
    last_diag = ((iteration_ctx.previous_attempts[-1].diagnosis or {})
                 if iteration_ctx.previous_attempts else {})
    archived = _archive_failed_cycle(
        archive, strategy_snapshot=last_strategy,
        origin=f"{operation.lower()}_l1_no_candidate",
        diagnosis_summary=(last_diag.get("residual_characterization", "") or "all rounds failed to produce a strategy"),
        epoch=epoch, source_node_id=node.node_id, n_iterations=n_done,
    )
    _save_json(os.path.join(cycle_dir, "final_outcome.json"), {
        "success": False, "operation": operation, "n_iterations": n_done,
        "reason": "no_candidate: all rounds failed to produce a usable strategy",
    })
    _log.info("L1 cycle FAILED: no effective strategy (lift>0 AND net_lift>=0) in %d rounds — archived",
              n_done)
    return ProposalOutcome(
        success=False, operation=operation, archived=archived,
        reason=f"no_effective: {n_done} rounds, none reached lift>0 AND net_lift>=0",
        n_iterations=n_done,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Step implementations
# ══════════════════════════════════════════════════════════════════════════════

def _run_step1_analyses(
    client: "LLMClient",
    node: "TreeNode",
    train_groups: "list[TaskRolloutGroup]",
    fail_results: "list[TaskResult]",
    *,
    cfg: "CSSConfig",
    max_workers: int,
    round_dir: str,
) -> dict:
    """Step 1 (1a/1b/1c) — the FIXED multi-dimensional analysis of the node's
    post-exploitation state.

    Its inputs (``node``, ``train_groups``, ``fail_results``) do NOT change during
    the L1 cycle, so the three analyses produce the same result every round.
    Therefore this is computed ONCE per cycle and cached; only the 1d synthesis
    re-runs per hypothesis (folding in the growing ledger).
    """
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

    return results_1abc


def _run_step1d(
    client: "LLMClient",
    analyses_1abc: dict,
    iteration_ctx: "_IterationContext",
    *,
    round_dir: str,
) -> dict:
    """Step 1d — synthesize the NEXT hypothesis from the (cached) fixed analyses
    1a/1b/1c PLUS the cross-round ledger.

    Re-run every time a fresh hypothesis is needed (round 1, or after a
    ``hypothesis_failure``). Folding in the ledger is what lets a re-synthesis
    build on what prior rounds tried — ruling out spent directions instead of
    re-deriving the same one from the (unchanged) fixed analyses.
    """
    hypothesis = _step1d(client, analyses_1abc, iteration_ctx)
    _save_json(os.path.join(round_dir, "step1", "1d_hypothesis.json"), hypothesis)
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


def _step1d(client: "LLMClient", analyses: dict,
            iteration_ctx: "_IterationContext") -> dict:
    """1d — synthesize the next hypothesis from the fixed analyses 1a/1b/1c PLUS
    the cross-round ledger (so it builds on prior rounds, not re-derives)."""
    user_parts = ["# A. FIXED ANALYSIS of the node's post-exploitation state"]
    if "1a" in analyses:
        user_parts.append("## 1. L0 Ceiling Analysis\n" + json.dumps(analyses["1a"], indent=2, ensure_ascii=False))
    if "1b" in analyses:
        user_parts.append("## 2. Trajectory Analysis\n" + json.dumps(analyses["1b"], indent=2, ensure_ascii=False))
    if "1c" in analyses:
        user_parts.append("## 3. Contrastive Limitation Analysis\n" + json.dumps(analyses["1c"], indent=2, ensure_ascii=False))

    user_parts.append(
        "# B. CYCLE LEDGER — what prior rounds of THIS cycle already tried "
        "(authoritative on what is ruled out)\n" + iteration_ctx.render_ledger()
    )

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
    grounding: dict,
    iteration_ctx: _IterationContext,
    *,
    cfg: "CSSConfig",
    round_dir: str,
    mode: str = "new",
    refine_target: str = "",
) -> dict | None:
    """Step 2: produce a cognitive strategy in one of two modes.

    ``mode="new"`` — propose a genuinely different cognitive mechanism (diverse
    exploration). ``mode="refine"`` — improve ``refine_target`` (the prior round's
    strategy that cracked nothing) keeping its core idea but fixing how it is
    operationalized. ``grounding`` is the one-time Step-1 analysis; from round 2
    the LEDGER is the authoritative steer (see :meth:`render_ledger`).
    """
    step_dir = os.path.join(round_dir, "step2")
    os.makedirs(step_dir, exist_ok=True)

    if mode == "refine":
        mode_header = (
            "## MODE = REFINE\n"
            "The strategy below was tested and cracked NOTHING (lift 0), but its core "
            "cognitive idea is judged sound. KEEP its core philosophy; improve its "
            "OPERATIONALIZATION (per the latest diagnosis) so the agent actually follows "
            "and benefits from it. Do NOT switch ideas; do NOT add tactical rules.\n\n"
            "### Strategy to refine (the prior round's strategy)\n" + (refine_target or "(missing)")
        )
    else:
        mode_header = (
            "## MODE = NEW\n"
            "Propose a strategy on a GENUINELY DIFFERENT cognitive mechanism from every "
            "philosophy in the ledger (diverse exploration). Preserve the active ingredient, "
            "avoid the harm, and pursue a different cognitive leverage point. Leave tactical "
            "residual to L0."
        )

    user_parts = [
        mode_header,
        "## One-time grounding analysis (why L0 stalled + failure patterns + initial directions)\n"
        + json.dumps(grounding, indent=2, ensure_ascii=False),
        "## Current strategy.md of the node being branched (reference baseline)\n"
        + (node.strategy or "(empty)").strip(),
    ]

    if iteration_ctx.previous_attempts:
        user_parts.append(
            "## CYCLE LEDGER — every prior philosophy this cycle tried, its objective "
            "lift/regression, and its diagnosis (active ingredient to PRESERVE, harm to "
            "AVOID, residual; next_action). This is your authoritative steer.\n"
            + iteration_ctx.render_ledger()
        )
    else:
        user_parts.append(
            "## CYCLE LEDGER\n(empty — this is the first philosophy; ground it in the analysis above)"
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
    test_items: "list[dict]",
    *,
    cfg: "CSSConfig",
    round_dir: str,
    epoch: int,
    node_id: str,
) -> "list[TaskRolloutGroup]":
    """Step 3: test the candidate — the NEW strategy with EMPTY rules — K times
    on the FIXED residual + regression task set.

    Empty rules is deliberate (see module docstring): it handicaps the candidate
    so that any *crack* is a conservative signal that survives once L0 restores
    rules. ``k_rollouts = cfg.k_rollouts`` gives the same pass@K resolution as the
    baseline, so categorization is symmetric. Returns the candidate's rollouts
    grouped by task (ready for :func:`_categorize`).
    """
    from css.rollout.batch import batch_rollout
    from css.data.rollout import group_rollouts
    from css.skill_document import SkillDocument

    step_dir = os.path.join(round_dir, "step3", "rollout")
    os.makedirs(step_dir, exist_ok=True)

    candidate_doc = SkillDocument(skill_dir="", strategy=strategy_text, rules="")
    skill_text = candidate_doc.combined_skill_text()

    results = batch_rollout(
        env,
        test_items,
        skill_text,
        target_client,
        k_rollouts=cfg.k_rollouts,
        out_dir=step_dir,
        max_workers=cfg.max_api_workers,
        task_timeout=cfg.task_timeout_s,
        epoch=epoch,
        node_id=node_id,
    )
    groups = group_rollouts(results)
    _log.info("Step 3: tested %d tasks x %d rollouts (%d task-passes)",
              len(groups), cfg.k_rollouts,
              sum(1 for g in groups if not g.is_persistent_fail()))
    return groups


def _analyze_one(client: "LLMClient", system: str, user: str, *,
                 task_id: str, label: str) -> dict:
    """One Layer-1 per-task analysis call (parse + context-aware LLM repair).

    Best-effort: returns ``{"raw": ...}`` on unrecoverable parse failure rather
    than raising, so one bad analysis never sinks the whole diagnosis.
    """
    text = _safe_optimizer_call(client, system, user, max_tokens=4096)
    res = _parse_json_safe(text, None)
    if res is None:
        repaired = repair_json_via_llm(client, system, user, text, stage=label)
        if repaired is not None:
            res = _parse_json_safe(repaired, None)
    res = res if isinstance(res, dict) else {"raw": text}
    res["task_id"] = str(task_id)
    return res


def _diagnose_round(
    client: "LLMClient",
    strategy_text: str,
    categories: dict,
    candidate_groups: "list[TaskRolloutGroup]",
    baseline_map: "dict[str, TaskRolloutGroup]",
    *,
    cfg: "CSSConfig",
    max_workers: int,
    round_dir: str,
) -> dict:
    """Step 4: category-specific contrastive diagnosis (two layers).

    Layer 1 (parallel, ONE task in depth each — never all trajectories at once):
      - cracked     -> contrast (candidate SUCCESS × baseline FAILURE) -> active_ingredient
      - regressed   -> contrast (baseline SUCCESS × candidate FAILURE) -> handicap|harm
      - still_failed -> candidate FAILURE alone (no contrast) -> residual nature
    Layer 2 synthesizes the per-task analyses into the round diagnosis
    ``{active_ingredient, harm, residual_characterization, residual_nature,
    next_direction_hint}`` that steers the next philosophy.

    Selection is already done OBJECTIVELY (``categories``); this is purely to
    enrich the feedback signal. Best-effort: a degraded call never blocks the cycle.
    """
    from css.trajectory import format_trajectory

    step_dir = os.path.join(round_dir, "step4")
    per_dir = os.path.join(step_dir, "per_task")
    os.makedirs(per_dir, exist_ok=True)

    cand_by_id = {str(g.task_id): g for g in candidate_groups}
    per_cat = int(getattr(cfg, "l1_diagnosis_per_category", 5))
    tt = cfg.tool_trunc
    strat = (strategy_text or "")[:2500]

    def _traj(r) -> str:
        # System prompt = the strategy, already shown once at the top; drop it.
        return format_trajectory(r.messages, tool_trunc=tt, include_system=False)

    # ── Build Layer-1 jobs: one task each, the right trajectories per category ──
    jobs: list[tuple] = []  # (category, system_prompt, user_prompt, task_id, label)
    for tid in categories.get("cracked", [])[:per_cat]:
        cg, bg = cand_by_id.get(tid), baseline_map.get(tid)
        succ = cg.successes[0] if cg and cg.successes else None
        fail = bg.failures[0] if bg and bg.failures else None
        if succ and fail:
            user = "\n\n".join([
                f"## Strategy under test\n{strat}",
                f"## CANDIDATE trajectory (new strategy, NO rules) — SUCCEEDED (task {tid})\n{_traj(succ)}",
                f"## BASELINE trajectory (prior strategy + full rules) — FAILED (task {tid})\n{_traj(fail)}",
            ])
            jobs.append(("cracked", _CRACKED_ANALYZER_SYSTEM, user, tid, "diag_cracked"))
    # Classify ALL regressed tasks (not just a sample): the handicap-vs-harm split
    # feeds the OBJECTIVE harm_reg count, which drives deploy_net (= lift - harm_reg,
    # the post-exploitation net lower bound) used for keep-best tie-breaking.
    for tid in categories.get("regressed", []):
        cg, bg = cand_by_id.get(tid), baseline_map.get(tid)
        succ = bg.successes[0] if bg and bg.successes else None
        fail = cg.failures[0] if cg and cg.failures else None
        if succ and fail:
            user = "\n\n".join([
                f"## Strategy under test\n{strat}",
                f"## BASELINE trajectory (prior strategy + full rules) — SUCCEEDED (task {tid})\n{_traj(succ)}",
                f"## CANDIDATE trajectory (new strategy, NO rules) — FAILED (task {tid})\n{_traj(fail)}",
            ])
            jobs.append(("regressed", _REGRESSED_ANALYZER_SYSTEM, user, tid, "diag_regressed"))
    for tid in categories.get("still_failed", [])[:per_cat]:
        cg = cand_by_id.get(tid)
        fail = cg.failures[0] if cg and cg.failures else None
        if fail:
            user = "\n\n".join([
                f"## Strategy under test\n{strat}",
                f"## CANDIDATE trajectory (new strategy, NO rules) — FAILED (task {tid})\n{_traj(fail)}",
            ])
            jobs.append(("still_failed", _STILLFAILED_ANALYZER_SYSTEM, user, tid, "diag_still"))

    # ── Run Layer 1 in parallel (with per-task checkpoint) ────────────────
    layer1: dict[str, list] = {"cracked": [], "regressed": [], "still_failed": []}
    remaining_jobs: list[tuple] = []
    for (cat, sysp, usr, tid, label) in jobs:
        per_task_path = os.path.join(per_dir, f"{cat}_task_{tid}.json")
        if os.path.exists(per_task_path):
            cached = _load_json(per_task_path)
            if isinstance(cached, dict):
                layer1[cat].append(cached)
                continue
        remaining_jobs.append((cat, sysp, usr, tid, label))
    if remaining_jobs:
        n_cached = len(jobs) - len(remaining_jobs)
        if n_cached:
            _log.info("L1 Step 4: loaded %d/%d per-task analyses from checkpoint",
                      n_cached, len(jobs))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futs = {
                pool.submit(_analyze_one, client, sysp, usr, task_id=tid, label=label): cat
                for (cat, sysp, usr, tid, label) in remaining_jobs
            }
            for fut in as_completed(futs):
                cat = futs[fut]
                try:
                    res = fut.result()
                except Exception as exc:  # noqa: BLE001 — degrade, never block the cycle
                    _log.warning("Layer-1 %s analyzer failed: %s", cat, exc)
                    continue
                layer1[cat].append(res)
                _save_json(
                    os.path.join(per_dir, f"{cat}_task_{res.get('task_id', 'x')}.json"), res
                )

    # ── Objective harm count (from the per-task regressed classifications) ──
    # harm_reg = regressed tasks the strategy GENUINELY broke (cognitive misdirection
    # that persists at deploy), as opposed to handicap (missing rule; recovers once L0
    # restores rules). deploy_net = lift - harm_reg is the post-exploitation net lower
    # bound; the cycle uses it to tie-break keep-best among equal-net_lift candidates.
    lift_n = int(categories.get("lift", 0) or 0)
    n_regressed = len(categories.get("regressed", []))
    harm_reg = sum(1 for x in layer1["regressed"]
                   if str(x.get("failure_cause", "")).strip().lower() == "harm")
    deploy_net = lift_n - harm_reg

    # ── Layer 2: synthesize the per-task analyses into the round diagnosis ──
    agg_user = "\n\n".join([
        "## Objective counts\n" + json.dumps({
            "lift": categories.get("lift", 0),
            "regression": categories.get("regression", 0),
            "net_lift": categories.get("net_lift", 0),
            "harm_regressions": harm_reg,
            "handicap_regressions": n_regressed - harm_reg,
            "deploy_net_lower_bound": deploy_net,
            "n_residual": categories.get("n_residual", 0),
            "n_still_failed": len(categories.get("still_failed", [])),
        }, indent=2),
        "## CRACKED per-task analyses (the active ingredient that worked)\n"
        + json.dumps(layer1["cracked"], indent=2, ensure_ascii=False),
        "## REGRESSED per-task analyses (each classified handicap vs harm)\n"
        + json.dumps(layer1["regressed"], indent=2, ensure_ascii=False),
        "## STILL-FAILED per-task analyses (residual nature)\n"
        + json.dumps(layer1["still_failed"], indent=2, ensure_ascii=False),
    ])
    text = _safe_optimizer_call(client, _DIAGNOSE_AGGREGATE_SYSTEM, agg_user, max_tokens=4096)
    diagnosis = _parse_json_safe(text, None)
    if diagnosis is None:
        repaired = repair_json_via_llm(
            client, _DIAGNOSE_AGGREGATE_SYSTEM, agg_user, text, stage="diag_aggregate"
        )
        if repaired is not None:
            diagnosis = _parse_json_safe(repaired, None)
    diagnosis = diagnosis if isinstance(diagnosis, dict) else {}
    # Objective counts (NOT LLM) attached for the loop's keep-best tie-break.
    diagnosis["harm_reg"] = harm_reg
    diagnosis["deploy_net"] = deploy_net
    _save_json(os.path.join(step_dir, "diagnosis.json"),
               {"layer1": layer1, "aggregate": diagnosis})
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


def _select_l1_test_set(
    env,
    train_groups: "list[TaskRolloutGroup]",
    cfg: "CSSConfig",
) -> "tuple[list[dict], list[dict], dict[str, TaskRolloutGroup]]":
    """Select the FIXED L1 test set + baseline map (computed ONCE per cycle).

    The baseline is the node-being-branched's post-exploitation rollout
    (``train_groups`` = its full skill: prior strategy + full rules). Against it:

      residual  : up to ``cfg.l1_diagnostic_tasks`` tasks the baseline CANNOT
                  solve (``is_persistent_fail`` = 0/K) — the only tasks a new
                  strategy can *crack* (the lift set).
      regression: up to ``cfg.l1_regression_tasks`` tasks the baseline solves
                  robustly (K/K pass) — to detect strategy-induced *harm*.
      baseline_map: task_id -> the baseline ``TaskRolloutGroup`` (rollouts +
                  trajectories), for objective categorization and the per-task
                  contrastive diagnosis.

    All three are FIXED for the whole cycle so lift/regression are comparable
    round-to-round (every candidate is tested on the identical task set).
    ``baseline_map`` keys are exactly the resolvable tested tasks.

    Returns ``(residual_items, regression_items, baseline_map)``.
    """
    persistent_fail = [g for g in train_groups if g.is_persistent_fail()]
    # ``all([])`` is True, so guard against zero-rollout groups (all rollouts
    # errored/timed out) being mis-selected as fully-passing regression tasks.
    passing = [
        g for g in train_groups
        if g.rollouts and all(r.passed for r in g.rollouts)
    ]

    train_index = _train_item_index(env)
    residual_items: list[dict] = []
    regression_items: list[dict] = []
    baseline_map: dict[str, "TaskRolloutGroup"] = {}

    for g in persistent_fail:
        if len(residual_items) >= cfg.l1_diagnostic_tasks:
            break
        tid = str(g.task_id)
        if tid in train_index and tid not in baseline_map:
            residual_items.append(train_index[tid])
            baseline_map[tid] = g

    for g in passing:
        if len(regression_items) >= cfg.l1_regression_tasks:
            break
        tid = str(g.task_id)
        if tid in train_index and tid not in baseline_map:
            regression_items.append(train_index[tid])
            baseline_map[tid] = g

    return residual_items, regression_items, baseline_map


def _categorize(
    candidate_groups: "list[TaskRolloutGroup]",
    baseline_map: "dict[str, TaskRolloutGroup]",
) -> dict:
    """Objectively categorize each tested task by candidate-vs-baseline pass@K.

    Symmetric on ``is_persistent_fail`` (0/K) — no LLM, no ground truth:

      cracked     : baseline fails (0/K), candidate passes (>=1/K)  -> +lift
      still_failed: baseline fails,       candidate fails
      regressed   : baseline passes,      candidate fails (0/K)      -> +regression
      maintained  : baseline passes,      candidate passes

    A candidate task with NO rollouts (all errored/timed out) is treated as a
    fail — NOT as ``is_persistent_fail`` (which is False for an empty group).

    Returns category task-id lists plus ``lift``/``regression``/``net_lift`` and
    the residual/regression-set sizes.
    """
    cand_by_id = {str(g.task_id): g for g in candidate_groups}
    cats: dict[str, list] = {
        "cracked": [], "still_failed": [], "regressed": [], "maintained": [],
    }
    n_residual = 0
    n_regression = 0
    for tid, base_g in baseline_map.items():
        base_fail = base_g.is_persistent_fail()
        if base_fail:
            n_residual += 1
        else:
            n_regression += 1
        cand_g = cand_by_id.get(tid)
        cand_fail = True if (cand_g is None or not cand_g.rollouts) else cand_g.is_persistent_fail()
        if base_fail and not cand_fail:
            cats["cracked"].append(tid)
        elif base_fail and cand_fail:
            cats["still_failed"].append(tid)
        elif (not base_fail) and cand_fail:
            cats["regressed"].append(tid)
        else:
            cats["maintained"].append(tid)
    lift = len(cats["cracked"])
    regression = len(cats["regressed"])
    return {
        **cats,
        "lift": lift,
        "regression": regression,
        "net_lift": lift - regression,
        "n_residual": n_residual,
        "n_regression": n_regression,
    }


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
    """Atomic JSON write (tmp + os.replace) — crash-safe checkpoint."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, path)
    except Exception as exc:
        _log.warning("Failed to save %s: %s", path, exc)


def _load_json(path: str) -> Any:
    """Load a JSON file from disk."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


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


def _strategy_name(strategy_text: str) -> str:
    """The strategy's display name — its first heading/line — for the ledger."""
    for line in (strategy_text or "").splitlines():
        s = line.strip()
        if s.startswith("#"):
            return s.lstrip("#").strip()[:120]
        if s:
            return s[:120]
    return ""


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
# Public entry point (called from orchestrator._run_branch_operation)
# ══════════════════════════════════════════════════════════════════════════════


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
    env=None,
    target_client: "LLMClient" = None,
    train_groups: "list[TaskRolloutGroup]" = None,
    out_dir: str = "",
) -> "ProposalOutcome":
    """Delegate to :func:`run_l1_cycle` (the v3 diverse-iterate L1 search)."""
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
