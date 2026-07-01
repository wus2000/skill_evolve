"""Cold start (Phase 0) — design the ROOT behavioral paradigm.

The search tree needs a seed: a single ROOT node carrying an initial behavioral
paradigm (``strategy_0``) and empty rules.  Cold start bootstraps that seed by
analyzing the bare LLM's natural behavior and designing a first paradigm that
systematizes its accidental successes and fills its behavioral gaps.

Concretely:

  1. **Bare rollout.**  ``grouped_batch_rollout`` over ``env.train_items()`` with
     ``skill_text=""`` and the target client.  The baseline score is the
     ``task_hard`` of ``aggregate_scores`` over the flattened rollouts.
  2. **Analysis.**  Host the rollouts' patterns on a throwaway ``TreeNode``
     (``node_id="n0000"``, empty strategy/rules) and run ``run_analysis_epoch``
     with ``l0_saturated=True`` — cold start treats the bare LLM as already
     saturated so all failure patterns qualify immediately.
  3. **Design ``strategy_0``.**  Use the SAME analysis products and design
     quality as the L1 cycle: the behavioral pattern landscape feeds a design
     brief, which feeds the paradigm designer.  A lightweight self-critique
     checks the design against actual trajectories before committing.
  4. **Seed the tree.**  A fresh ``SearchTree`` with one ROOT node (the
     designed ``strategy_0``, empty rules, the analysed pattern library).

Heavy modules are imported lazily inside :func:`cold_start` so that importing
this module stays cheap and side-effect-free.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - type-only imports
    from css.config import CSSConfig
    from css.data.negative_archive import NegativeArchive
    from css.data.pattern import PatternLibrary, PatternRecord
    from css.data.tree import SearchTree
    from css.model.client import LLMClient


# The throwaway node that hosts the bare-rollout pattern library during cold
# start. It is never added to the tree; only its ``pattern_records`` survive,
# transplanted onto the real ROOT node.
_COLD_START_NODE_ID = "n0000"


@dataclass
class ColdStartResult:
    """Outcome of Phase 0 cold start.

    ``tree`` carries exactly one ROOT node (``strategy_0``, empty rules, the
    bare-rollout pattern library). ``archive`` is the fresh tree-global negative
    archive threaded into the search loop. ``baseline_score`` is the bare-LLM
    ``task_hard`` over the train set (the floor every node must beat).
    ``n_patterns`` is the number of patterns extracted from the bare rollouts.
    """

    tree: "SearchTree"
    archive: "NegativeArchive"
    baseline_score: float
    n_patterns: int


def _fallback_strategy_0() -> str:
    """A minimal generic ``strategy_0`` when paradigm design yields nothing.

    Uses the Phase structure format that the L1 cycle expects.  The real
    paradigm emerges from the search; this only guarantees the ROOT node is
    well-formed when the bare rollouts produced no actionable signal.
    """
    return (
        "## Systematic Explore-Then-Execute\n"
        "A disciplined approach: understand the task and explore available "
        "information before committing to a solution, then verify the result "
        "before submitting. Most failures stem from acting before building "
        "sufficient understanding of the task and the data.\n\n"
        "### Phase 1: Understand\n"
        "Read the task carefully. Identify what is being asked and what "
        "information or tools are available. Do not begin solving yet.\n\n"
        "### Phase 2: Explore\n"
        "Use available tools to examine the relevant data or environment. "
        "Build concrete familiarity with what you are working with.\n\n"
        "### Phase 3: Solve\n"
        "Based on your understanding and exploration, construct a solution. "
        "If the task is complex, break it into smaller parts and address "
        "each part before combining.\n\n"
        "### Phase 4: Verify\n"
        "Before submitting, check your solution against the original task "
        "requirements. If something does not match, return to Phase 2 or 3.\n\n"
        "### Transitions & Recovery\n"
        "Move forward only when the current phase's purpose is fulfilled. "
        "If you encounter unexpected results at any phase, return to Explore "
        "to gather more information before proceeding."
    )


def _save_coldstart_artifacts(cold_dir: str, node, analysis) -> None:
    """Persist cold-start analysis intermediate products."""
    import json
    import os

    art_dir = os.path.join(cold_dir, "analysis")
    os.makedirs(art_dir, exist_ok=True)

    patterns = []
    for p in node.pattern_records.active():
        patterns.append({
            "pattern_id": p.pattern_id,
            "name": p.name,
            "description": p.description,
            "cognitive_aspect": p.cognitive_aspect,
            "polarity": p.polarity,
            "counterpart_id": p.counterpart_id,
            "support_count": p.support_count,
            "n_observations": len(p.observations),
        })
    with open(os.path.join(art_dir, "patterns.json"), "w", encoding="utf-8") as f:
        json.dump(patterns, f, ensure_ascii=False, indent=2)

    obs_list = []
    for p in node.pattern_records.active():
        for o in p.observations:
            obs_list.append({
                "obs_id": o.obs_id,
                "task_id": o.task_id,
                "cognitive_aspect": o.cognitive_aspect,
                "what": o.what,
                "significance": o.significance,
                "polarity": o.polarity,
                "pattern_id": o.pattern_id,
            })
    with open(os.path.join(art_dir, "observations.json"), "w", encoding="utf-8") as f:
        json.dump(obs_list, f, ensure_ascii=False, indent=2)

    if analysis.divergences:
        divs = []
        for d in analysis.divergences:
            divs.append({
                "task_id": d.task_id,
                "divergence_point": d.divergence_point,
                "cognitive_difference": d.cognitive_difference,
                "is_systematic": d.is_systematic,
            })
        with open(os.path.join(art_dir, "divergences.json"), "w", encoding="utf-8") as f:
            json.dump(divs, f, ensure_ascii=False, indent=2)


def _save_coldstart_derivation(cold_dir, signals, root_causes, proposal,
                               counterparts, strategy_0):
    """Persist root cause attribution and strategy derivation products."""
    import json
    import os

    art_dir = os.path.join(cold_dir, "derivation")
    os.makedirs(art_dir, exist_ok=True)

    sig_list = []
    for s in (signals or []):
        sig_list.append({
            "pattern_id": s.pattern_id,
            "name": s.name,
            "polarity": s.polarity,
            "support_count": s.support_count,
            "remedy_resistance": s.remedy_resistance,
        })
    with open(os.path.join(art_dir, "signals.json"), "w", encoding="utf-8") as f:
        json.dump(sig_list, f, ensure_ascii=False, indent=2)

    rc_list = []
    for rc in (root_causes or []):
        rc_list.append(rc.to_dict())
    with open(os.path.join(art_dir, "root_causes.json"), "w", encoding="utf-8") as f:
        json.dump(rc_list, f, ensure_ascii=False, indent=2)

    if proposal is not None:
        with open(os.path.join(art_dir, "proposal.json"), "w", encoding="utf-8") as f:
            json.dump(proposal.to_dict(), f, ensure_ascii=False, indent=2)

    cp_list = []
    for cp in (counterparts or []):
        cp_list.append({
            "pattern_id": cp.pattern_id,
            "name": cp.name,
            "polarity": cp.polarity,
            "cognitive_aspect": cp.cognitive_aspect,
            "description": cp.description,
        })
    with open(os.path.join(art_dir, "counterparts.json"), "w", encoding="utf-8") as f:
        json.dump(cp_list, f, ensure_ascii=False, indent=2)

    with open(os.path.join(art_dir, "strategy_0.md"), "w", encoding="utf-8") as f:
        f.write(strategy_0 or "")


def _seed_failure_signals(
    library: "PatternLibrary",
    signals: "list[PatternRecord]",
    *,
    cfg: "CSSConfig",
) -> "list[PatternRecord]":
    """Pick the failure patterns for cold-start analysis.

    Prefer the qualifying L1 signals from Layer 3. When none qualify (a common
    cold-start case — the longitudinal trend predicate needs history this single
    epoch may not give), fall back to the most-significant active failure
    patterns by observation support, capped at a handful.
    """
    if signals:
        return signals
    failures = library.by_polarity("failure")
    if not failures:
        return []
    ranked = sorted(
        failures,
        key=lambda p: (p.support_count, p.latest_occurrence),
        reverse=True,
    )
    cap = max(1, int(getattr(cfg, "neg_archive_top_k", 5)))
    return ranked[:cap]


# ── Cold-start paradigm design prompts ──────────────────────────────────────

_COLDSTART_DESIGN_SYSTEM = """\
You are designing the FIRST behavioral paradigm for an AI agent — a multi-phase \
plan governing how it approaches tasks from start to finish.

The agent has just completed a "bare run" — solving tasks with NO strategy \
guidance, relying only on its natural capabilities. You receive a comprehensive \
analysis of its natural behavior: what behavioral patterns emerged, which led to \
success vs failure, and what the tasks require.

Your design has TWO sources:
1. SYSTEMATIZE accidental successes: the analysis shows behavioral patterns \
that naturally led to success — but they happened by accident, not by design. \
Your paradigm should make them happen DELIBERATELY, every time.
2. FILL behavioral gaps: the analysis identifies what failing tasks need that \
the agent's natural behavior does not provide. Based on your understanding of \
what solving these tasks requires, design phases that fill those gaps.

This is the agent's FIRST paradigm. Prioritize CLARITY and FOLLOWABILITY — the \
agent has never received behavioral guidance before, so each phase must be \
concrete enough for it to know exactly what to do.

ALTITUDE: design the SHAPE of the trajectory (phases, transitions, recovery \
logic). Leave execution DETAILS (specific API arguments, output formats, edge \
cases) to the separate L0 tactical optimizer that runs after you.

SELF-CHECK: an observer watching the agent's action sequence should be able to \
tell it is following your paradigm. If they would need to inspect individual \
tool arguments to tell, you are too detailed.

DOCUMENT FORMAT:

  ## <Paradigm Name>
  <Overview: core approach, why effective. 2-3 sentences.>

  ### Phase 1: <Phase Name>
  <Purpose. Entry condition. Core behaviors (observable actions). Exit condition.>

  ### Phase N: <Phase Name>
  <...>

  ### Transitions & Recovery
  <Phase-to-phase logic; what to do when things go wrong.>

GROUND-TRUTH CONSTRAINT: the agent has NO access to expected answers at runtime.

Output ONLY a JSON object:
{
  "paradigm_name": "<descriptive name>",
  "strategy_text": "<the full paradigm document in the format above>",
  "design_grounding": "<for each phase: what evidence grounds it — which \
success pattern you systematized, or what task need you filled>"
}
No prose, no fences — just the JSON object."""


_COLDSTART_CRITIQUE_SYSTEM = """\
You are performing a final quality check on a newly designed behavioral paradigm \
before it is deployed as the agent's first task-solving approach. There is no \
test run — this check is the last gate.

You receive:
- The designed paradigm (full Phase structure)
- 2-3 actual SUCCESS trajectories (where the agent naturally succeeded)
- 2-3 actual FAILURE trajectories (where the agent naturally failed)
- The agent's available actions (tools and interaction loop)

Check TWO things:

1. SAFETY: Would this paradigm DISRUPT what the agent already does well \
naturally? Compare each paradigm phase against the success trajectories — does \
any phase force the agent into a behavior that conflicts with its natural \
success patterns? If so, flag the conflict.

2. EXECUTABILITY: Can the agent actually PERFORM each phase with its available \
tools? Check each phase against the action space — is there a tool or action \
for what the phase asks the agent to do?

Output ONLY a JSON object:
{
  "safety_issues": ["<specific conflict between a paradigm phase and a natural \
success behavior — empty list if none>"],
  "executability_issues": ["<phase that requires an unavailable action — empty \
list if none>"],
  "verdict": "approve | revise",
  "revision_suggestions": ["<if revise: what to change and why>"],
  "revised_strategy_text": "<if revise: the corrected paradigm document; if \
approve: copy the original unchanged>"
}
No prose, no fences — just the JSON object."""


def cold_start(
    env,
    target_client: "LLMClient",
    optimizer_client: "LLMClient",
    *,
    cfg: "CSSConfig",
    out_dir: str,
    ledger=None,
) -> "ColdStartResult":
    """Phase 0 — derive the ROOT strategy from the bare LLM's weaknesses.

    See the module docstring for the full pipeline. Never raises on a degenerate
    bare rollout or malformed optimizer output: derivation failures fall back to
    a minimal generic ``strategy_0`` so the search always gets a well-formed ROOT
    node. ``out_dir`` receives the bare-rollout artifacts under a ``coldstart``
    subdirectory.
    """
    import os

    # Lazy imports (keep module import cheap / side-effect-free).
    from css.data.negative_archive import NegativeArchive
    from css.data.tree import SearchTree, TreeNode
    from css.rollout.batch import grouped_batch_rollout
    from css.data.rollout import aggregate_scores
    from css.analysis.pipeline import (
        run_analysis_epoch,
        render_pattern_landscape,
        render_representative_trajectories,
    )
    from css.model.json_repair import complete_optimizer_json

    cold_dir = os.path.join(out_dir, "coldstart")
    os.makedirs(cold_dir, exist_ok=True)

    # ── 1. Bare rollout: the frozen target model with NO skill ──────────────
    # Subset the train set for the cold-start rollout when configured (large
    # datasets: a full bare rollout of every task is wasteful before search even
    # begins). Deterministic; 0 / >= len => the whole train set.
    from css.data.task_ledger import uniform_subset
    train_items = uniform_subset(
        list(env.train_items()), getattr(cfg, "coldstart_train_size", 0), seed=cfg.seed
    )
    groups = grouped_batch_rollout(
        env,
        train_items,
        "",  # bare: empty skill
        target_client,
        k_rollouts=cfg.k_rollouts,
        out_dir=cold_dir,
        max_workers=cfg.max_api_workers,
        task_timeout=cfg.task_timeout_s,
        epoch=0,
        node_id=_COLD_START_NODE_ID,
    )
    # Seed the global difficulty ledger with the bare-LLM (no-skill) solve map.
    if ledger is not None:
        ledger.update_from_groups(groups, 0)
    flat = [r for g in groups for r in g.rollouts]
    baseline_score = float(aggregate_scores(flat).get("task_hard", 0.0))

    # ── 2. Analysis on a throwaway node; bare LLM is treated as saturated ───
    temp_node = TreeNode(
        node_id=_COLD_START_NODE_ID,
        branch_type="ROOT",
        strategy="",
        rules="",
        created_epoch=0,
    )
    import os as _os
    analysis = run_analysis_epoch(
        optimizer_client,
        temp_node,
        groups,
        epoch=0,
        l0_saturated=True,  # cold start: nothing to L0-optimize; treat as saturated
        cfg=cfg,
        out_dir=_os.path.join(cold_dir, "analysis"),
    )
    library = temp_node.pattern_records
    n_patterns = len(library)

    # Persist cold-start analysis artifacts.
    _save_coldstart_artifacts(cold_dir, temp_node, analysis)

    # ── 3. Design strategy_0 using the behavioral paradigm approach ──────────
    # Same analysis quality as the L1 cycle: pattern landscape → design brief →
    # paradigm design → lightweight self-critique.
    import json as _json

    strategy_0 = ""
    _signals = _seed_failure_signals(library, analysis.l1_signals, cfg=cfg)

    # Build the result lookup for representative trajectory rendering.
    _all_results: dict = {}
    for _g in groups:
        for _r in _g.rollouts:
            _all_results[(str(_r.task_id), _r.rollout_index)] = _r

    # 3a. Render the behavioral landscape from analysis products.
    landscape_text = render_pattern_landscape(
        library, analysis.divergences,
        max_patterns=20, max_evidence_len=200,
    )
    rep_text = render_representative_trajectories(
        library, _all_results,
        max_exemplars=6, tool_trunc=cfg.tool_trunc,
    )

    # Action space description (if the env provides it).
    action_desc = ""
    if hasattr(env, "action_space_description"):
        action_desc = env.action_space_description()

    # 3b. Paradigm design (mirrors L1 Step 2 quality, MODE=INITIAL).
    design_user_parts = [
        "## Behavioral Pattern Landscape (from ALL bare-run trajectories)\n"
        + landscape_text,
        "## Representative trajectories (analysis-selected)\n" + rep_text,
    ]
    if action_desc:
        design_user_parts.append("## Agent action space\n" + action_desc)
    design_user_parts.append(
        f"## Bare-run baseline\nSuccess rate: {baseline_score:.3f} "
        f"(on {len(train_items)} tasks × {cfg.k_rollouts} rollouts)"
    )
    design_user = "\n\n".join(design_user_parts)

    def _parse_design(text):
        """Parse the paradigm design JSON."""
        import re as _re
        for pat in [_re.compile(r"```(?:json)?\s*(.*?)```", _re.DOTALL),
                    _re.compile(r"(\{.*\})", _re.DOTALL)]:
            m = pat.search(text or "")
            if m:
                try:
                    return _json.loads(m.group(1))
                except (_json.JSONDecodeError, ValueError):
                    continue
        try:
            return _json.loads(text or "")
        except (_json.JSONDecodeError, ValueError):
            return None

    try:
        design_obj = complete_optimizer_json(
            optimizer_client, _COLDSTART_DESIGN_SYSTEM, design_user,
            parse=_parse_design, max_tokens=8192, stage="coldstart_design",
        )
    except Exception:
        design_obj = None

    if isinstance(design_obj, dict):
        strategy_0 = (design_obj.get("strategy_text") or "").strip()

    # 3c. Lightweight self-critique (safety + executability check).
    critique_obj = None
    if strategy_0:
        from css.trajectory import format_trajectory as _fmt_traj
        succ_trajs = [r for g in groups for r in g.rollouts if r.passed][:3]
        fail_trajs = [r for g in groups for r in g.rollouts if not r.passed][:3]
        critique_parts = [
            "## Designed paradigm\n" + strategy_0,
        ]
        if succ_trajs:
            critique_parts.append(
                "## Natural SUCCESS trajectories\n" + "\n\n---\n\n".join(
                    f"Task {r.task_id}:\n{_fmt_traj(r.messages, tool_trunc=cfg.tool_trunc)}"
                    for r in succ_trajs
                )
            )
        if fail_trajs:
            critique_parts.append(
                "## Natural FAILURE trajectories\n" + "\n\n---\n\n".join(
                    f"Task {r.task_id}:\n{_fmt_traj(r.messages, tool_trunc=cfg.tool_trunc)}"
                    for r in fail_trajs
                )
            )
        if action_desc:
            critique_parts.append("## Agent action space\n" + action_desc)
        critique_user = "\n\n".join(critique_parts)
        try:
            critique_obj = complete_optimizer_json(
                optimizer_client, _COLDSTART_CRITIQUE_SYSTEM, critique_user,
                parse=_parse_design, max_tokens=8192, stage="coldstart_critique",
            )
        except Exception:
            critique_obj = None
        if isinstance(critique_obj, dict):
            verdict = str(critique_obj.get("verdict", "")).strip().lower()
            if verdict == "revise":
                revised = (critique_obj.get("revised_strategy_text") or "").strip()
                if revised:
                    strategy_0 = revised

    # Persist design artifacts.
    _deriv_dir = _os.path.join(cold_dir, "derivation")
    _os.makedirs(_deriv_dir, exist_ok=True)
    with open(_os.path.join(_deriv_dir, "strategy_0.md"), "w", encoding="utf-8") as _f:
        _f.write(strategy_0 or "")
    if design_obj:
        with open(_os.path.join(_deriv_dir, "design.json"), "w", encoding="utf-8") as _f:
            _json.dump(design_obj, _f, ensure_ascii=False, indent=2)
    if critique_obj:
        with open(_os.path.join(_deriv_dir, "critique.json"), "w", encoding="utf-8") as _f:
            _json.dump(critique_obj, _f, ensure_ascii=False, indent=2)

    if not strategy_0:
        strategy_0 = _fallback_strategy_0()

    # ── 4. Seed the tree with the ROOT node ─────────────────────────────────
    tree = SearchTree()
    root = TreeNode(
        node_id=tree.new_node_id(),
        branch_type="ROOT",
        strategy=strategy_0,
        rules="",
        pattern_records=library,
        created_epoch=0,
    )
    tree.add_root(root)

    return ColdStartResult(
        tree=tree,
        archive=NegativeArchive(),
        baseline_score=baseline_score,
        n_patterns=n_patterns,
    )
