"""Phase 6 capstone — the round-based main loop that wires Phases 1-5 together.

This is the CSS orchestrator (design_final_en.md §7, training_mechanism_v6.md
D14). After cold start seeds a ROOT node, the search proceeds in concurrent
*rounds*:

  SELECT_BATCH (top-K active nodes by UCB1, Phase 6 :mod:`css.tree.select`)
    -> for each selected node, one EPOCH:
         (1) epoch rollout on the train set with the node's current skill (P2),
         (2) L0 EXPLOITATION over those rollouts + the val set (P3),
         (3) Layer 1-3 analysis on the epoch groups (P4) — captures L1 signals,
         (4) periodic validation eval -> update val_score + learning curve.
    -> SYNC POINT:
         (5) PRUNE: paired-bootstrap sibling-dominance test (P6 prune),
         (6) BRANCH: saturated nodes spawn a PROPOSAL (P5) — the L1 diverse-iterate
             cycle searches for a new cognitive strategy; a successful one is
             attached to the tree as a new child. (REFINE was unified into PROPOSAL.)

The loop terminates when no active node is non-saturated AND no branch produced
a new node in a round (the search has nothing left to do), or when ``max_rounds``
is hit (a safety / test cap — the design has no step budget).

Determinism: SELECT / PRUNE / branching are pure given inputs; the paired
bootstrap is seeded from ``cfg.seed``. Per-node work runs sequentially by default
(correct + deterministic); each node/round gets a distinct ``out_dir`` so a
ThreadPoolExecutor variant would stay isolated.

All heavy Phase-2/3/4/5 modules are imported LAZILY inside the functions so that
importing this module stays cheap and side-effect-free (no model / embedding /
faiss at import time).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

_log = logging.getLogger("css")

if TYPE_CHECKING:  # pragma: no cover - type-only imports
    from css.config import CSSConfig
    from css.data.negative_archive import NegativeArchive
    from css.data.rollout import TaskResult, TaskRolloutGroup
    from css.data.tree import SearchTree, TreeNode
    from css.model.client import LLMClient


# ──────────────────────────────────────────────────────────────────────────
# Result records (frozen public API)
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class RoundResult:
    """Outcome of one orchestrator round.

    ``selected_node_ids`` is the SELECT_BATCH for the round; ``branches`` are the
    per-selected-node branch decisions actually taken at the SYNC point
    (``EXPLOITATION`` / ``REFINE`` / ``PROPOSAL`` / ``NONE``, suffixed
    ``:success`` / ``:fail`` for the operations that ran); ``pruned`` are the node
    ids pruned this round; ``global_best_score`` is the best ``val_score`` in the
    tree after the round.
    """

    round_index: int
    selected_node_ids: list[str] = field(default_factory=list)
    branches: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    global_best_score: float = 0.0


@dataclass
class RunResult:
    """Outcome of a full CSS run."""

    tree: "SearchTree"
    archive: "NegativeArchive"
    rounds: list["RoundResult"] = field(default_factory=list)
    terminated_reason: str = ""
    best_node_id: str | None = None


def _round_to_dict(r: "RoundResult") -> dict:
    """Serialize a RoundResult for the checkpoint's rounds-history metadata."""
    return {
        "round_index": r.round_index,
        "selected_node_ids": list(r.selected_node_ids),
        "branches": list(r.branches),
        "pruned": list(r.pruned),
        "global_best_score": r.global_best_score,
    }


def _round_from_dict(d: dict) -> "RoundResult":
    """Rehydrate a RoundResult from checkpoint metadata."""
    return RoundResult(
        round_index=int(d.get("round_index", -1)),
        selected_node_ids=list(d.get("selected_node_ids", [])),
        branches=list(d.get("branches", [])),
        pruned=list(d.get("pruned", [])),
        global_best_score=float(d.get("global_best_score", 0.0)),
    )


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────
def _best_unpruned(tree: "SearchTree") -> "TreeNode | None":
    """Highest-val_score node among non-pruned nodes (for run/round reporting).

    A pruned node was retired precisely because a sibling scored significantly
    higher, so under the prune gate it can never be the global max; excluding
    pruned nodes makes that invariant explicit rather than implicit.
    """
    candidates = [n for n in tree.nodes.values() if n.status != "pruned"]
    if not candidates:
        return None
    return max(candidates, key=lambda n: n.val_score)


def _combined_skill_text(node: "TreeNode") -> str:
    """Render a node's strategy + rules exactly as the frozen agent will see it.

    Uses a transient (dir-less) :class:`~css.skill_document.SkillDocument` so the
    rendering is identical to the Layer-5c validation closure and to cold start.
    """
    from css.skill_document import SkillDocument

    doc = SkillDocument(skill_dir="", strategy=node.strategy or "", rules=node.rules or "")
    return doc.combined_skill_text()


def _save_analysis_artifacts(
    out_dir: str, node: "TreeNode", round_index: int, analysis
) -> None:
    """Persist analysis intermediate products for auditability."""
    import json
    import os

    art_dir = os.path.join(
        out_dir, node.node_id, f"round_{round_index:04d}", "analysis"
    )
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
            "remedy_resistance": p.remedy_resistance,
            "status": p.status,
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

    if analysis.l1_signals:
        signals = []
        for s in analysis.l1_signals:
            signals.append({
                "pattern_id": s.pattern_id,
                "name": s.name,
                "polarity": s.polarity,
                "support_count": s.support_count,
                "remedy_resistance": s.remedy_resistance,
            })
        with open(os.path.join(art_dir, "l1_signals.json"), "w", encoding="utf-8") as f:
            json.dump(signals, f, ensure_ascii=False, indent=2)

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


def _save_branch_artifacts(
    out_dir: str, node: "TreeNode", round_index: int,
    operation: str, outcome,
) -> None:
    """Persist PROPOSAL/REFINE intermediate products for auditability."""
    import json
    import os

    art_dir = os.path.join(
        out_dir, node.node_id, f"round_{round_index:04d}", "branch"
    )
    os.makedirs(art_dir, exist_ok=True)

    summary = {
        "operation": operation,
        "success": outcome.success,
        "reason": outcome.reason,
        "n_iterations": outcome.n_iterations,
    }
    if outcome.new_node is not None:
        summary["new_node_id"] = outcome.new_node.node_id
        summary["new_strategy_len"] = len(outcome.new_node.strategy or "")
        summary["new_rules_len"] = len(outcome.new_node.rules or "")
    with open(os.path.join(art_dir, "outcome.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    if outcome.archived is not None:
        with open(os.path.join(art_dir, "archived_entry.json"), "w", encoding="utf-8") as f:
            json.dump(outcome.archived.to_dict(include_embedding=False), f, ensure_ascii=False, indent=2)

    if outcome.new_node is not None:
        with open(os.path.join(art_dir, "new_strategy.md"), "w", encoding="utf-8") as f:
            f.write(outcome.new_node.strategy or "")
        with open(os.path.join(art_dir, "new_rules.md"), "w", encoding="utf-8") as f:
            f.write(outcome.new_node.rules or "")


def _save_skill_snapshot(out_dir: str, node: "TreeNode", round_index: int) -> None:
    """Persist strategy.md + rules.md for a node at a given round."""
    import os

    snap_dir = os.path.join(out_dir, "skill_snapshots", node.node_id, f"round_{round_index:04d}")
    os.makedirs(snap_dir, exist_ok=True)
    with open(os.path.join(snap_dir, "strategy.md"), "w", encoding="utf-8") as f:
        f.write(node.strategy or "")
    with open(os.path.join(snap_dir, "rules.md"), "w", encoding="utf-8") as f:
        f.write(node.rules or "")
    meta = {
        "node_id": node.node_id,
        "parent_id": node.parent_id,
        "branch_type": node.branch_type,
        "round": round_index,
        "val_score": node.val_score,
        "best_score": node.best_score,
        "n_steps": node.n_steps,
    }
    with open(os.path.join(snap_dir, "metadata.json"), "w", encoding="utf-8") as f:
        import json
        json.dump(meta, f, indent=2, ensure_ascii=False)


def _node_round_dir(out_dir: str, node: "TreeNode", round_index: int, leaf: str) -> str:
    """Distinct per-node / per-round / per-stage output directory."""
    import os

    return os.path.join(out_dir, node.node_id, f"round_{round_index:04d}", leaf)


def _task_pass_map(groups: list["TaskRolloutGroup"]) -> dict[str, int]:
    """task_id -> majority-vote pass indicator (1 if pass_rate >= 0.5 else 0)."""
    return {g.task_id: (1 if g.pass_rate >= 0.5 else 0) for g in groups}


def _paired_val_passes(
    node_groups: list["TaskRolloutGroup"],
    sibling_groups: list["TaskRolloutGroup"],
) -> tuple[list[int], list[int]]:
    """Build PAIRED per-task pass vectors for node vs sibling on shared val tasks.

    Aligns by ``task_id`` (the validation set is shared) and keeps only tasks
    present for BOTH so the bootstrap stays paired. Order is deterministic
    (node's first-seen task order).
    """
    node_map = _task_pass_map(node_groups)
    sib_map = _task_pass_map(sibling_groups)
    node_passes: list[int] = []
    sib_passes: list[int] = []
    for tid in node_map:  # preserves node's task order
        if tid in sib_map:
            node_passes.append(node_map[tid])
            sib_passes.append(sib_map[tid])
    return node_passes, sib_passes


def _success_results(groups: list["TaskRolloutGroup"]) -> list["TaskResult"]:
    """Flatten the passing rollouts across all groups (P5 ``success_results``)."""
    return [r for g in groups for r in g.successes]


def _persistent_fail_groups(groups: list["TaskRolloutGroup"]) -> list["TaskRolloutGroup"]:
    """Groups where no rollout passed — the P5/5c persistent-fail subset."""
    return [g for g in groups if g.is_persistent_fail()]


# ──────────────────────────────────────────────────────────────────────────
# Per-node epoch (the body run for each selected node)
# ──────────────────────────────────────────────────────────────────────────
def _run_val_subset(env, cfg: "CSSConfig") -> list[dict]:
    """The RUN-FIXED val subset used for every node's baseline + L0 gate + val_score.

    Same tasks for ALL nodes (deterministic, seeded by ``cfg.seed``) so node
    val_scores stay directly comparable for SELECT / PRUNE, and so the gate's
    best-step predictions can be reused by the final val eval. ``0`` or a knob
    ``>= len(val)`` selects the whole val set.
    """
    from css.data.task_ledger import uniform_subset
    return uniform_subset(
        list(env.val_items()), getattr(cfg, "exploitation_val_size", 0), seed=cfg.seed
    )


def _measure_initial_val(
    node: "TreeNode", env, target_client: "LLMClient", *,
    cfg: "CSSConfig", out_dir: str, round_index: int,
    val_items: "list[dict] | None" = None,
) -> float:
    """Measure the node's INITIAL skill (strategy + initial rules) on the val set,
    BEFORE any exploitation, and set it as the node's baseline + gate incumbent.

    This is the node's true starting point: ``node.val_score`` (the incumbent the
    L0 gate compares its first edit against — never 0 or a train floor) and
    ``node.baseline_val_score`` (the floor exploitation is measured FROM). Called
    once per node (cold-start root + each newly-branched node's first epoch).
    ``val_items`` (the run-fixed val subset) is passed in so the baseline is on
    the SAME tasks the gate uses; it defaults to the full val set.
    """
    from css.rollout.batch import grouped_batch_rollout
    from css.data.rollout import aggregate_scores
    from css.skill_document import SkillDocument

    val_items = list(val_items) if val_items is not None else list(env.val_items())
    skill = SkillDocument(skill_dir="", strategy=node.strategy or "",
                          rules=node.rules or "").combined_skill_text()
    groups = grouped_batch_rollout(
        env, val_items, skill, target_client,
        k_rollouts=cfg.k_rollouts,
        out_dir=_node_round_dir(out_dir, node, round_index, "val_baseline"),
        max_workers=cfg.max_api_workers, task_timeout=cfg.task_timeout_s,
        epoch=round_index, node_id=node.node_id,
    )
    flat = [r for g in groups for r in g.rollouts]
    score = float(aggregate_scores(flat).get("task_hard", 0.0))
    node.baseline_val_score = score
    node.val_score = score
    from css.evaluation.test_splits import env_extra_metrics, fmt_extra
    _log.info("Node baseline — round=%d node=%s initial_strategy_val=%.3f "
              "(gate incumbent)%s",
              round_index, node.node_id, score,
              fmt_extra(env_extra_metrics(env, flat)))
    return score


def _run_node_epoch(
    tree: "SearchTree",
    node: "TreeNode",
    env,
    target_client: "LLMClient",
    optimizer_client: "LLMClient",
    *,
    cfg: "CSSConfig",
    out_dir: str,
    round_index: int,
    ledger=None,
) -> dict:
    """Run one epoch for a single node and return the per-node SYNC payload.

    Returns a dict carrying the epoch's train groups, val groups, captured L1
    signals, and the persistent-fail / success splits needed at the SYNC point.
    The node's in-memory state (rules, pattern library, val_score, learning
    curve, maturity) is mutated in place by Phases 3/4.
    """
    from css.analysis.pipeline import run_analysis_epoch
    from css.data.rollout import aggregate_scores
    from css.data.tree import LearningCurvePoint
    from css.optimizer.exploitation import run_exploitation_epoch
    from css.rollout.batch import grouped_batch_rollout
    from css.tracing import log_event

    train_items = list(env.train_items())
    # Run-fixed val subset for baseline + L0 gate + val_score (same tasks every node).
    val_items = _run_val_subset(env, cfg)

    _log.info("Epoch start — round=%d node=%s train=%d val=%d steps=%d",
              round_index, node.node_id, len(train_items), len(val_items), node.n_steps)

    log_event("epoch_start", round_index=round_index, node_id=node.node_id,
              n_train=len(train_items), n_val=len(val_items),
              skill_len=len(_combined_skill_text(node)), n_steps=node.n_steps,
              rules_len=len(node.rules or ""), strategy_len=len(node.strategy or ""))

    # (0) First epoch only: measure the node's INITIAL strategy on val so the L0
    # gate's incumbent (node.val_score) is the true starting point, not 0 / a train
    # floor. Already set for the cold-start root (measured at cold-start).
    if node.baseline_val_score < 0:
        _measure_initial_val(node, env, target_client,
                             cfg=cfg, out_dir=out_dir, round_index=round_index,
                             val_items=val_items)

    # (1) L0 EXPLOITATION: batch-step loop until saturation or hard cap.
    # The paired gate seeds its incumbent ledger from the val_baseline
    # predictions (same skill => zero-rollout bootstrap); tell it where they
    # live for this node/round.
    cfg._val_baseline_dir = _node_round_dir(out_dir, node, round_index, "val_baseline")
    node.step_buffer.reset_saturation()
    l0_steps_before = node.n_steps
    exploit_summary = run_exploitation_epoch(
        node,
        env,
        train_items,
        val_items,
        target_client,
        optimizer_client,
        cfg,
        _node_round_dir(out_dir, node, round_index, "exploit"),
        epoch=round_index,
        current_score=node.val_score,
    )
    _log.info("Exploitation done — round=%d node=%s steps=%d->%d best=%.3f saturated=%s rules=%d chars",
              round_index, node.node_id, l0_steps_before, node.n_steps,
              node.best_score, node.is_saturated(cfg.N, stall_threshold=getattr(cfg, "l0_stall_steps", 0)), len(node.rules or ""))

    log_event("exploitation_done", round_index=round_index, node_id=node.node_id,
              steps_before=l0_steps_before, steps_after=node.n_steps,
              new_steps=node.n_steps - l0_steps_before,
              best_score=node.best_score, saturated=node.is_saturated(cfg.N, stall_threshold=getattr(cfg, "l0_stall_steps", 0)),
              consecutive_rejects=node.step_buffer.consecutive_rejects(),
              rules_len=len(node.rules or ""))

    # Save skill snapshot immediately after exploitation so the best rules
    # are persisted even if the round is interrupted during L1 PROPOSAL.
    _save_skill_snapshot(out_dir, node, round_index)

    # (2) Post-exploitation train rollout with the BEST skill.
    # Done AFTER exploitation so analysis sees on-policy trajectories that
    # reflect what problems remain unsolved by the optimized rules.
    post_skill_text = _val_skill_text(node)
    # Bound the analysis rollout to a subset for large datasets (0 / >= len => all),
    # biased by the GLOBAL difficulty ledger toward the most informative tasks
    # (frontier/contrastive > recently-flipped > learnable-hard > mastered;
    # proven-ceiling down-weighted). Falls back to uniform until the ledger has data.
    from css.data.task_ledger import difficulty_weighted_subset
    analysis_items = difficulty_weighted_subset(
        train_items, getattr(cfg, "analysis_train_size", 0), ledger,
        seed=cfg.seed + round_index,
        fracs={
            "frontier": getattr(cfg, "analysis_frac_frontier", 0.45),
            "hard": getattr(cfg, "analysis_frac_hard", 0.30),
            "flipped": getattr(cfg, "analysis_frac_flipped", 0.15),
            "mastered": getattr(cfg, "analysis_frac_mastered", 0.10),
        },
    )
    train_groups = grouped_batch_rollout(
        env,
        analysis_items,
        post_skill_text,
        target_client,
        k_rollouts=cfg.k_rollouts,
        out_dir=_node_round_dir(out_dir, node, round_index, "train"),
        max_workers=cfg.max_api_workers,
        task_timeout=cfg.task_timeout_s,
        epoch=round_index,
        node_id=node.node_id,
    )
    # Refresh the global ledger with this round's best-skill train outcomes.
    if ledger is not None:
        ledger.update_from_groups(train_groups, round_index)
    epoch_results_flat = [r for g in train_groups for r in g.rollouts]
    node.train_score = float(aggregate_scores(epoch_results_flat).get("task_hard", 0.0))

    n_pass = sum(1 for r in epoch_results_flat if getattr(r, "passed", False))
    _log.info("Train rollout done — round=%d node=%s score=%.3f pass=%d/%d",
              round_index, node.node_id, node.train_score, n_pass, len(epoch_results_flat))

    log_event("train_rollout_done", round_index=round_index, node_id=node.node_id,
              train_score=node.train_score, n_results=len(epoch_results_flat),
              n_pass=n_pass, n_groups=len(train_groups))

    # (3) Layer 1-3 analysis on post-exploitation trajectories.
    l0_saturated = node.is_saturated(cfg.N, stall_threshold=getattr(cfg, "l0_stall_steps", 0))
    from css.analysis.pipeline import analysis_env_context
    analysis = run_analysis_epoch(
        optimizer_client,
        node,
        train_groups,
        epoch=round_index,
        l0_saturated=l0_saturated,
        cfg=cfg,
        out_dir=_node_round_dir(out_dir, node, round_index, "analysis"),
        env_context=analysis_env_context(env, cfg),
    )
    _log.info("Analysis done — round=%d node=%s obs=%d patterns=%d l1_signals=%d l0_saturated=%s",
              round_index, node.node_id, analysis.n_observations, analysis.n_patterns,
              len(analysis.l1_signals), l0_saturated)

    log_event("analysis_done", round_index=round_index, node_id=node.node_id,
              n_observations=analysis.n_observations, n_patterns=analysis.n_patterns,
              n_l1_signals=len(analysis.l1_signals), l0_saturated=l0_saturated,
              l1_signal_ids=[getattr(s, "pattern_id", "") for s in analysis.l1_signals])

    _save_analysis_artifacts(out_dir, node, round_index, analysis)

    # (4) Validation eval with the node's BEST skill -> node.val_score + curve.
    # Zero-rollout policy: the gate already measured every candidate on the
    # full val set. New best this round -> its gate predictions ARE the best
    # skill's val measurement; re-read them from disk (paired mode: the K=1
    # screen; mean mode: the evaluate_candidate predictions at
    # exploitation_val_k). No new best -> the skill did not change, so
    # node.val_score from the previous round (or the node baseline) already
    # measures it; skip entirely (re-rolling an unchanged skill each round
    # was pure duplicate cost).
    val_skill_text = _val_skill_text(node)
    best_val_dir = getattr(exploit_summary, "best_val_out_dir", "")
    if best_val_dir:
        if getattr(cfg, "gate_mode", "mean") == "paired":
            val_k = max(1, getattr(cfg, "gate_screen_k", 1))
        else:
            val_k = getattr(cfg, "exploitation_val_k", 0) or cfg.k_rollouts
        _log.info("Val eval reusing gate predictions (K=%d): %s", val_k, best_val_dir)
        val_groups = grouped_batch_rollout(
            env,
            val_items,
            val_skill_text,
            target_client,
            k_rollouts=val_k,
            out_dir=best_val_dir,
            max_workers=cfg.max_api_workers,
            task_timeout=cfg.task_timeout_s,
            epoch=round_index,
            node_id=node.node_id,
        )
        val_flat = [r for g in val_groups for r in g.rollouts]
        node.val_score = float(aggregate_scores(val_flat).get("task_hard", 0.0))
        from css.evaluation.test_splits import env_extra_metrics, fmt_extra
        _log.info("Val eval — round=%d node=%s val=%.4f%s",
                  round_index, node.node_id, node.val_score,
                  fmt_extra(env_extra_metrics(env, val_flat)))
    else:
        # PRUNE degrades conservatively on the skip path: empty val_groups
        # means "no paired validation passes" -> never prunes on stale data.
        val_groups = []
        _log.info("Val eval skipped — no new best this round; val_score=%.4f carried",
                  node.val_score)

    # (5) Test eval with the node's BEST skill -> generalization measure.
    # Every env-declared split is evaluated and reported (e.g. AppWorld runs
    # test_normal AND test_challenge with TGC+SGC); the PRIMARY (first)
    # split's task_hard remains the mechanism's test_score.
    from css.evaluation.test_splits import evaluate_test_splits

    split_report = evaluate_test_splits(
        env, val_skill_text, target_client, cfg,
        _node_round_dir(out_dir, node, round_index, "test"),
        epoch=round_index, node_id=node.node_id,
        label="Round %d test" % round_index,
    )
    primary = next(iter(split_report))
    test_groups = split_report[primary]["groups"]
    test_score = split_report[primary]["score"]
    extra_split_scores = {
        name: {"score": rep["score"], **rep["extra"]}
        for name, rep in split_report.items()
    }

    node.record_learning_point(
        LearningCurvePoint(
            epoch=round_index,
            n_steps=node.n_steps,
            train_score=node.train_score,
            val_score=node.val_score,
            accept_rate=node.step_buffer.accept_rate(),
            accept_slope=node.accept_slope(cfg.W),
        )
    )
    node.maturity += 1

    _log.info("Epoch done — round=%d node=%s train=%.3f val=%.3f test=%.3f best=%.3f maturity=%d",
              round_index, node.node_id, node.train_score, node.val_score,
              test_score, node.best_score, node.maturity)

    log_event("epoch_done", round_index=round_index, node_id=node.node_id,
              train_score=node.train_score, val_score=node.val_score,
              test_score=test_score,
              test_splits=extra_split_scores,
              best_score=node.best_score, maturity=node.maturity,
              accept_rate=node.step_buffer.accept_rate(),
              accept_slope=node.accept_slope(cfg.W))

    return {
        "node": node,
        "train_groups": train_groups,
        "val_groups": val_groups,
        "test_groups": test_groups,
        "test_score": test_score,
        "l1_signals": list(analysis.l1_signals),
        "success_results": _success_results(train_groups),
        "persistent_fail_groups": _persistent_fail_groups(train_groups),
    }


def _val_skill_text(node: "TreeNode") -> str:
    """Skill text for the validation eval: the node's BEST rules if recorded.

    EXPLOITATION threads ``best_rules`` (the rules.md body that achieved
    ``best_score``); validating with the best snapshot — rather than rules that
    may have advanced past it on an accept-not-best step — is the fair node
    comparison. Falls back to the live rules when no best is recorded yet.
    """
    from css.skill_document import SkillDocument

    rules = node.best_rules if node.best_rules else (node.rules or "")
    doc = SkillDocument(skill_dir="", strategy=node.strategy or "", rules=rules)
    return doc.combined_skill_text()


# ──────────────────────────────────────────────────────────────────────────
# SYNC point: prune + branch
# ──────────────────────────────────────────────────────────────────────────
def _prune_pass(
    tree: "SearchTree",
    payloads: dict[str, dict],
    *,
    cfg: "CSSConfig",
) -> list[str]:
    """PRUNE check over this round's nodes; returns the list of pruned node ids.

    For each node that has siblings, picks the best sibling by ``val_score`` and
    runs :func:`css.tree.prune.should_prune` with paired per-task validation pass
    vectors (only when both nodes were evaluated this round). Deterministic
    (bootstrap seeded from ``cfg.seed``).
    """
    from css.tree.prune import prune_node, should_prune

    pruned: list[str] = []
    for node_id, payload in payloads.items():
        node = tree.get(node_id)
        if node is None or node.status != "active":
            continue
        siblings = [s for s in tree.siblings(node_id) if s.status == "active"]
        if not siblings:
            continue
        best_sibling = max(siblings, key=lambda s: s.val_score)

        sib_payload = payloads.get(best_sibling.node_id)
        if sib_payload is None:
            # The best sibling was not run this round -> no paired val vectors to
            # compare; should_prune will report "no paired validation passes".
            node_passes: list[int] = []
            sib_passes: list[int] = []
        else:
            node_passes, sib_passes = _paired_val_passes(
                payload["val_groups"], sib_payload["val_groups"]
            )

        prune, _reason = should_prune(
            node,
            best_sibling,
            cfg=cfg,
            node_passes=node_passes,
            sibling_passes=sib_passes,
        )
        if prune:
            prune_node(tree, node_id)
            pruned.append(node_id)
    return pruned


def _branch_pass(
    tree: "SearchTree",
    payloads: dict[str, dict],
    archive: "NegativeArchive",
    env,
    target_client: "LLMClient",
    optimizer_client: "LLMClient",
    *,
    cfg: "CSSConfig",
    out_dir: str,
    round_index: int,
) -> tuple[list[str], bool]:
    """BRANCH pass over this round's still-active nodes.

    Returns ``(branch_labels, produced_new_node)``. For each node:
      * not saturated -> ``EXPLOITATION`` (keep optimizing next round).
      * saturated     -> ``PROPOSAL`` (the L1 diverse-iterate cycle). On success the
        new child is attached to the tree; on failure the node is marked
        ``saturated`` (done branching). (REFINE was unified into PROPOSAL.)
    """
    from css.tree.branching import decide_branch
    from css.tracing import log_event

    labels: list[str] = []
    produced = False

    for node_id, payload in payloads.items():
        node = tree.get(node_id)
        if node is None or node.status != "active":
            continue

        l1_signals = payload["l1_signals"]
        decision = decide_branch(node, l1_signals, cfg=cfg)

        log_event("branch_decision", round_index=round_index, node_id=node_id,
                  decision=decision, n_l1_signals=len(l1_signals),
                  saturated=node.is_saturated(cfg.N, stall_threshold=getattr(cfg, "l0_stall_steps", 0)))

        if decision == "EXPLOITATION":
            labels.append("EXPLOITATION")
            continue

        # PROPOSAL: run the L1 diverse-iterate cycle.
        label, made = _run_branch_operation(
            tree,
            node,
            decision,
            l1_signals,
            payload,
            archive,
            env,
            target_client,
            optimizer_client,
            cfg=cfg,
            out_dir=out_dir,
            round_index=round_index,
        )
        labels.append(label)
        produced = produced or made

    return labels, produced


def _run_branch_operation(
    tree: "SearchTree",
    node: "TreeNode",
    decision: str,
    l1_signals: list,
    payload: dict,
    archive: "NegativeArchive",
    env,
    target_client: "LLMClient",
    optimizer_client: "LLMClient",
    *,
    cfg: "CSSConfig",
    out_dir: str,
    round_index: int,
) -> tuple[str, bool]:
    """Run one PROPOSAL via the L1 diverse-iterate cycle (REFINE unified into it).

    Returns ``(label, produced_new_node)`` where ``label`` is e.g.
    ``"PROPOSAL:success"`` / ``"PROPOSAL:fail"``.
    """
    from css.proposal.proposal import run_proposal

    library = node.pattern_records
    train_groups = payload["train_groups"]

    operation = decision  # always "PROPOSAL" in v3 (REFINE unified into PROPOSAL)

    # Inject the action-space description so the L1 paradigm designer knows
    # what behavioral building blocks (tools, interaction loop) the agent has.
    if hasattr(env, "action_space_description"):
        cfg._env_action_space = env.action_space_description()

    outcome = run_proposal(
        node,
        l1_signals,
        library,
        archive,
        optimizer_client,
        cfg=cfg,
        new_node_id=tree.new_node_id(),
        epoch=round_index,
        env=env,
        target_client=target_client,
        train_groups=train_groups,
        out_dir=out_dir,
    )

    from css.tracing import log_event

    _save_branch_artifacts(out_dir, node, round_index, operation, outcome)

    if outcome.success and outcome.new_node is not None:
        tree.add_child(node.node_id, outcome.new_node)
        log_event("branch_result", round_index=round_index, node_id=node.node_id,
                  operation=operation, success=True,
                  new_node_id=outcome.new_node.node_id,
                  new_strategy_len=len(outcome.new_node.strategy or ""),
                  new_rules_len=len(outcome.new_node.rules or ""),
                  reason=outcome.reason or "")
        _save_skill_snapshot(out_dir, outcome.new_node, round_index)
        return f"{operation}:success", True

    # PROPOSAL produced no effective strategy -> the node is done branching.
    node.status = "saturated"
    log_event("branch_result", round_index=round_index, node_id=node.node_id,
              operation=operation, success=False, reason=outcome.reason or "")
    return f"{operation}:fail", False


# ──────────────────────────────────────────────────────────────────────────
# Round + full run
# ──────────────────────────────────────────────────────────────────────────
def run_round(
    tree: "SearchTree",
    archive: "NegativeArchive",
    env,
    target_client: "LLMClient",
    optimizer_client: "LLMClient",
    *,
    cfg: "CSSConfig",
    out_dir: str,
    round_index: int,
    ledger=None,
) -> "RoundResult":
    """Run one orchestrator round: SELECT_BATCH -> per-node epochs -> SYNC.

    Per-node epochs run sequentially (correct + deterministic; the contract
    permits a ``ThreadPoolExecutor(max_workers=cfg.concurrency_limit)`` since each
    node/round uses a distinct ``out_dir``). The SYNC point then runs PRUNE and
    BRANCH over exactly the nodes touched this round.
    """
    from css.tree.select import select_batch
    from css.tracing import log_event

    selected = select_batch(tree, cfg=cfg)
    selected_ids = [n.node_id for n in selected]
    _log.info("Round %d start — selected=%s active=%d total=%d",
              round_index, selected_ids, len(tree.active_nodes()), len(tree.nodes))

    log_event("round_start", round_index=round_index,
              selected=[n.node_id for n in selected],
              n_active=len(tree.active_nodes()),
              n_total=len(tree.nodes))

    # Per-node epochs. Keyed by node_id so the SYNC point can pair siblings.
    from concurrent.futures import ThreadPoolExecutor, as_completed

    concurrency = getattr(cfg, "concurrency_limit", 4)

    def _run_one(node):
        return node.node_id, _run_node_epoch(
            tree,
            node,
            env,
            target_client,
            optimizer_client,
            cfg=cfg,
            out_dir=out_dir,
            round_index=round_index,
            ledger=ledger,
        )

    payloads: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = [pool.submit(_run_one, n) for n in selected]
        for fut in as_completed(futures):
            nid, payload = fut.result()
            payloads[nid] = payload

    # ── SYNC POINT ──────────────────────────────────────────────────────────
    _log.info("Round %d SYNC — pruning + branching", round_index)
    pruned = _prune_pass(tree, payloads, cfg=cfg)
    if pruned:
        _log.info("Pruned nodes: %s", pruned)
        log_event("prune", round_index=round_index, pruned=pruned)
    branches, _produced = _branch_pass(
        tree,
        payloads,
        archive,
        env,
        target_client,
        optimizer_client,
        cfg=cfg,
        out_dir=out_dir,
        round_index=round_index,
    )

    best = _best_unpruned(tree)
    global_best = best.val_score if best is not None else 0.0
    _log.info("Round %d done — branches=%s global_best=%.3f", round_index, branches, global_best)

    return RoundResult(
        round_index=round_index,
        selected_node_ids=selected_ids,
        branches=branches,
        pruned=pruned,
        global_best_score=global_best,
    )


def run_css(
    env,
    target_client: "LLMClient",
    optimizer_client: "LLMClient",
    *,
    cfg: "CSSConfig",
    out_dir: str,
    max_rounds: int = 10,
    resume: bool = False,
) -> "RunResult":
    """Full CSS run: cold start -> round loop -> artifacts.

    Cold start (Phase 0) seeds the ROOT node; rounds run until either no active
    node is non-saturated AND no branch produced a new node in the round (the
    search is exhausted) or ``max_rounds`` is reached. Writes the run artifacts
    (tree snapshot / rounds / summary) under ``out_dir`` before returning.

    A stage checkpoint is written after cold start and after every round (see
    :mod:`css.checkpoint`). With ``resume=True`` and an existing checkpoint under
    ``out_dir``, the run RESUMES from the next stage — cold start and all
    completed rounds are skipped, their state loaded from the checkpoint. The
    checkpoint's config fingerprint must match the current config or resume is
    refused.
    """
    import os
    import random
    import time

    from css.checkpoint import (
        Checkpoint,
        capture_rng_state,
        config_fingerprint,
        latest_checkpoint,
        load_checkpoint,
        restore_rng_state,
        save_checkpoint,
    )
    from css.coldstart import cold_start
    from css.data.task_ledger import TaskDifficultyLedger
    from css.logging_viz import write_run_artifacts
    from css.model.client import OptimizerOnlyClient, TargetOnlyClient
    from css.tracing import TracingLLMClient, init_trace, log_event

    # ── Initialize tracing ──────────────────────────────────────────────────
    os.makedirs(out_dir, exist_ok=True)
    trace_path = init_trace(out_dir)
    log_event("run_start", max_rounds=max_rounds,
              target_model=cfg.target_model, optimizer_model=cfg.optimizer_model,
              n_train=cfg.n_train, n_val=cfg.n_val, max_workers=cfg.max_api_workers,
              max_turns=cfg.max_turns, resume=resume)

    # Wrap clients with tracing so every LLM call is recorded.
    if isinstance(target_client, TargetOnlyClient):
        target_client._inner = TracingLLMClient(target_client._inner, role="target")
    if isinstance(optimizer_client, OptimizerOnlyClient):
        optimizer_client._inner = TracingLLMClient(optimizer_client._inner, role="optimizer")

    fp = config_fingerprint(cfg)
    rounds: list[RoundResult] = []
    ckpt_path = latest_checkpoint(out_dir) if resume else None

    if ckpt_path:
        # ── RESUME: load the checkpoint and skip every completed stage ──────
        ckpt = load_checkpoint(ckpt_path)
        if ckpt.config_fingerprint and ckpt.config_fingerprint != fp:
            raise ValueError(
                f"resume refused: config fingerprint mismatch "
                f"(checkpoint={ckpt.config_fingerprint} current={fp}); the data "
                f"split / models / search params changed since this checkpoint"
            )
        tree, archive = ckpt.tree, ckpt.archive
        baseline_score = ckpt.baseline_score
        start_round = ckpt.next_round
        ledger = TaskDifficultyLedger.from_dict(ckpt.ledger)
        restore_rng_state(ckpt.rng_state)
        rounds = [_round_from_dict(r) for r in ckpt.rounds]
        _log.info("RESUMED from %s — stage=%s next_round=%d nodes=%d archive=%d baseline=%.3f",
                  os.path.basename(ckpt_path), ckpt.stage, start_round,
                  len(tree.nodes), len(archive), baseline_score)
        log_event("resume", checkpoint=os.path.basename(ckpt_path), stage=ckpt.stage,
                  next_round=start_round, n_nodes=len(tree.nodes), n_archive=len(archive))
    else:
        # ── FRESH: cold start, then checkpoint the seeded tree ──────────────
        random.seed(cfg.seed)
        ledger = TaskDifficultyLedger(ceiling_rounds=getattr(cfg, "analysis_ceiling_rounds", 4))
        cs = cold_start(
            env,
            target_client,
            optimizer_client,
            cfg=cfg,
            out_dir=out_dir,
            ledger=ledger,
        )
        tree, archive = cs.tree, cs.archive
        baseline_score = cs.baseline_score
        start_round = 0

        # Save ROOT node skill snapshot + measure the COLD-START STRATEGY on the val
        # set as the root's baseline / gate incumbent (replaces the old bare-LLM
        # train floor, which mismatched the val-based gate).
        root = tree.get(tree.root_id) if tree.root_id else None
        if root:
            _save_skill_snapshot(out_dir, root, 0)
            _measure_initial_val(root, env, target_client, cfg=cfg, out_dir=out_dir,
                                 round_index=0, val_items=_run_val_subset(env, cfg))
        bare_test = getattr(cs, "bare_test_score", -1.0)
        _log.info("Cold start done — baseline(strategy@val)=%.3f bare_floor(train)=%.3f "
                  "bare_test=%.3f patterns=%d root=%s strategy=%d chars",
                  root.val_score if root else 0.0, cs.baseline_score,
                  bare_test, cs.n_patterns,
                  tree.root_id, len(root.strategy or "") if root else 0)

        log_event("cold_start_done", baseline_score=cs.baseline_score,
                  bare_test_score=bare_test,
                  n_patterns=cs.n_patterns,
                  root_id=tree.root_id,
                  strategy_len=len(root.strategy or "") if root else 0)

        save_checkpoint(Checkpoint(
            stage="coldstart", next_round=0, baseline_score=baseline_score,
            tree=tree, archive=archive, config_fingerprint=fp,
            rng_state=capture_rng_state(), rounds=[], created_ts=time.time(),
            ledger=ledger.to_dict(),
        ), out_dir)

    terminated_reason = "max_rounds"
    for round_index in range(start_round, max_rounds):
        if not tree.active_nodes():
            terminated_reason = "no_active_nodes"
            break

        rnd = run_round(
            tree,
            archive,
            env,
            target_client,
            optimizer_client,
            cfg=cfg,
            out_dir=out_dir,
            round_index=round_index,
            ledger=ledger,
        )
        rounds.append(rnd)

        log_event("round_done", round_index=round_index,
                  branches=rnd.branches, pruned=rnd.pruned,
                  global_best_score=rnd.global_best_score)

        # Save skill snapshots for all active nodes at round end.
        for node in tree.active_nodes():
            _save_skill_snapshot(out_dir, node, round_index)

        # Write incremental tree snapshot after each round (crash-safe).
        write_run_artifacts(
            RunResult(tree=tree, archive=archive, rounds=rounds,
                      terminated_reason="in_progress",
                      best_node_id=(_best_unpruned(tree) or type('', (), {'node_id': None})()).node_id),
            out_dir,
        )

        # Stage checkpoint: the resumable boundary after this completed round.
        save_checkpoint(Checkpoint(
            stage=f"round_{round_index:04d}", next_round=round_index + 1,
            baseline_score=baseline_score, tree=tree, archive=archive,
            config_fingerprint=fp, rng_state=capture_rng_state(),
            rounds=[_round_to_dict(r) for r in rounds], created_ts=time.time(),
            ledger=ledger.to_dict(),
        ), out_dir)

        non_saturated = any(
            "EXPLOITATION" in b for b in rnd.branches
        )
        produced = any(b.endswith(":success") for b in rnd.branches)
        if not non_saturated and not produced and not tree.active_nodes():
            terminated_reason = "exhausted"
            break

    best = _best_unpruned(tree)
    best_node_id = best.node_id if best is not None else None

    log_event("run_done", terminated_reason=terminated_reason,
              best_node_id=best_node_id,
              best_val_score=best.val_score if best else 0.0,
              n_rounds=len(rounds),
              best_strategy=best.strategy[:500] if best else "",
              best_rules=best.rules[:500] if best else "")

    run = RunResult(
        tree=tree,
        archive=archive,
        rounds=rounds,
        terminated_reason=terminated_reason,
        best_node_id=best_node_id,
    )
    write_run_artifacts(run, out_dir)
    return run
