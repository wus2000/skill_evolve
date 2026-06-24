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
         (6) BRANCH: saturated nodes spawn REFINE / PROPOSAL (P5), escalating
             REFINE -> PROPOSAL when the REFINE gate fails; new children are
             attached to the tree on success.

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

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - type-only imports
    from css.analysis.embedding import Embedder
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
def _run_node_epoch(
    tree: "SearchTree",
    node: "TreeNode",
    env,
    target_client: "LLMClient",
    optimizer_client: "LLMClient",
    embedder: "Embedder",
    *,
    cfg: "CSSConfig",
    out_dir: str,
    round_index: int,
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

    train_items = list(env.train_items())
    val_items = list(env.val_items())
    skill_text = _combined_skill_text(node)

    # (1) Epoch rollout on the train set with the node's current skill (P2).
    train_groups = grouped_batch_rollout(
        env,
        train_items,
        skill_text,
        target_client,
        k_rollouts=cfg.k_rollouts,
        out_dir=_node_round_dir(out_dir, node, round_index, "train"),
        max_workers=cfg.max_api_workers,
        task_timeout=cfg.task_timeout_s,
        epoch=round_index,
        node_id=node.node_id,
    )
    epoch_results_flat = [r for g in train_groups for r in g.rollouts]
    node.train_score = float(aggregate_scores(epoch_results_flat).get("task_hard", 0.0))

    # (2) L0 EXPLOITATION over the epoch rollouts + the val set (P3). Mutates the
    #     node's rules / step_buffer / best_* in place.
    run_exploitation_epoch(
        node,
        env,
        val_items,
        epoch_results_flat,
        target_client,
        optimizer_client,
        cfg,
        _node_round_dir(out_dir, node, round_index, "exploit"),
        epoch=round_index,
        current_score=node.val_score,
    )

    # (3) Layer 1-3 analysis on the epoch groups (P4). Mutates node.pattern_records
    #     in place and reports the L1 signals captured at this epoch.
    l0_saturated = node.is_saturated(cfg.N)
    analysis = run_analysis_epoch(
        optimizer_client,
        embedder,
        node,
        train_groups,
        epoch=round_index,
        l0_saturated=l0_saturated,
        cfg=cfg,
    )

    # (4) Validation eval with the node's BEST skill -> node.val_score + curve.
    val_skill_text = _val_skill_text(node)
    val_groups = grouped_batch_rollout(
        env,
        val_items,
        val_skill_text,
        target_client,
        k_rollouts=cfg.k_rollouts,
        out_dir=_node_round_dir(out_dir, node, round_index, "val"),
        max_workers=cfg.max_api_workers,
        task_timeout=cfg.task_timeout_s,
        epoch=round_index,
        node_id=node.node_id,
    )
    val_flat = [r for g in val_groups for r in g.rollouts]
    node.val_score = float(aggregate_scores(val_flat).get("task_hard", 0.0))
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

    return {
        "node": node,
        "train_groups": train_groups,
        "val_groups": val_groups,
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
    embedder: "Embedder",
    *,
    cfg: "CSSConfig",
    out_dir: str,
    round_index: int,
) -> tuple[list[str], bool]:
    """BRANCH pass over this round's still-active nodes.

    Returns ``(branch_labels, produced_new_node)``. For each node:
      * not saturated -> ``EXPLOITATION`` (keep optimizing next round).
      * saturated + L1 signals -> REFINE (while budget) else PROPOSAL; on REFINE
        gate failure (``escalate_to_proposal``) immediately retry as PROPOSAL.
      * saturated + no signal -> ``NONE`` and the node is marked ``saturated``.
    On a successful PROPOSAL/REFINE the new child is attached to the tree.
    """
    from css.proposal.proposal import run_proposal, run_refine
    from css.tree.branching import decide_branch, make_rollout_validate_fn

    labels: list[str] = []
    produced = False

    for node_id, payload in payloads.items():
        node = tree.get(node_id)
        if node is None or node.status != "active":
            continue

        l1_signals = payload["l1_signals"]
        decision = decide_branch(node, l1_signals, cfg=cfg)

        if decision == "EXPLOITATION":
            labels.append("EXPLOITATION")
            continue
        if decision == "NONE":
            # Saturated with no remedy-resistant signal: this node is done.
            node.status = "saturated"
            labels.append("NONE")
            continue

        # REFINE or PROPOSAL: run the operation with the real Layer-5c closure.
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
            embedder,
            cfg=cfg,
            out_dir=out_dir,
            round_index=round_index,
            run_refine=run_refine,
            run_proposal=run_proposal,
            make_rollout_validate_fn=make_rollout_validate_fn,
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
    embedder: "Embedder",
    *,
    cfg: "CSSConfig",
    out_dir: str,
    round_index: int,
    run_refine,
    run_proposal,
    make_rollout_validate_fn,
) -> tuple[str, bool]:
    """Run one REFINE/PROPOSAL (escalating REFINE->PROPOSAL on gate failure).

    Returns ``(label, produced_new_node)`` where ``label`` is e.g.
    ``"REFINE:success"`` / ``"PROPOSAL:fail"``.
    """
    persistent_fail_groups = payload["persistent_fail_groups"]
    success_results = payload["success_results"]
    library = node.pattern_records

    def _validate_fn():
        return make_rollout_validate_fn(
            env,
            target_client,
            embedder,
            optimizer_client,
            node,
            library,
            persistent_fail_groups,
            cfg=cfg,
            out_dir=out_dir,
            epoch=round_index,
        )

    operation = decision  # "REFINE" or "PROPOSAL"
    runner = run_refine if operation == "REFINE" else run_proposal

    outcome = runner(
        node,
        l1_signals,
        library,
        archive,
        embedder,
        optimizer_client,
        cfg=cfg,
        new_node_id=tree.new_node_id(),
        epoch=round_index,
        success_results=success_results,
        persistent_fail_groups=persistent_fail_groups,
        rollout_validate_fn=_validate_fn(),
    )

    # REFINE gate failure -> escalate to a full PROPOSAL this same round.
    if (
        operation == "REFINE"
        and not outcome.success
        and "escalate_to_proposal" in (outcome.reason or "")
    ):
        operation = "PROPOSAL"
        outcome = run_proposal(
            node,
            l1_signals,
            library,
            archive,
            embedder,
            optimizer_client,
            cfg=cfg,
            new_node_id=tree.new_node_id(),
            epoch=round_index,
            success_results=success_results,
            persistent_fail_groups=persistent_fail_groups,
            rollout_validate_fn=_validate_fn(),
        )

    if outcome.success and outcome.new_node is not None:
        tree.add_child(node.node_id, outcome.new_node)
        if operation == "REFINE":
            # Consume one unit of the source node's REFINE budget so a saturated
            # node escalates to PROPOSAL after cfg.K local edits (decide_branch
            # reads node.refine_count). The child carries refine_count+1 already.
            node.refine_count += 1
        return f"{operation}:success", True

    # A non-escalating REFINE that produced no child still consumes budget: bump
    # the source's refine_count so repeated remedy-resistant signals eventually
    # escalate to PROPOSAL (decide_branch) instead of looping REFINE forever.
    if operation == "REFINE":
        node.refine_count += 1
        return f"{operation}:fail", False

    # A saturated node whose terminal PROPOSAL failed has no further productive
    # move: retire it from the active set so SELECT stops re-spending rounds on
    # it and the loop can drain to termination.
    node.status = "saturated"
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
    embedder: "Embedder",
    *,
    cfg: "CSSConfig",
    out_dir: str,
    round_index: int,
) -> "RoundResult":
    """Run one orchestrator round: SELECT_BATCH -> per-node epochs -> SYNC.

    Per-node epochs run sequentially (correct + deterministic; the contract
    permits a ``ThreadPoolExecutor(max_workers=cfg.concurrency_limit)`` since each
    node/round uses a distinct ``out_dir``). The SYNC point then runs PRUNE and
    BRANCH over exactly the nodes touched this round.
    """
    from css.tree.select import select_batch

    selected = select_batch(tree, cfg=cfg)
    selected_ids = [n.node_id for n in selected]

    # Per-node epochs. Keyed by node_id so the SYNC point can pair siblings.
    payloads: dict[str, dict] = {}
    for node in selected:
        payloads[node.node_id] = _run_node_epoch(
            tree,
            node,
            env,
            target_client,
            optimizer_client,
            embedder,
            cfg=cfg,
            out_dir=out_dir,
            round_index=round_index,
        )

    # ── SYNC POINT ──────────────────────────────────────────────────────────
    pruned = _prune_pass(tree, payloads, cfg=cfg)
    branches, _produced = _branch_pass(
        tree,
        payloads,
        archive,
        env,
        target_client,
        optimizer_client,
        embedder,
        cfg=cfg,
        out_dir=out_dir,
        round_index=round_index,
    )

    best = _best_unpruned(tree)
    global_best = best.val_score if best is not None else 0.0

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
    embedder: "Embedder",
    *,
    cfg: "CSSConfig",
    out_dir: str,
    max_rounds: int = 10,
) -> "RunResult":
    """Full CSS run: cold start -> round loop -> artifacts.

    Cold start (Phase 0) seeds the ROOT node; rounds run until either no active
    node is non-saturated AND no branch produced a new node in the round (the
    search is exhausted) or ``max_rounds`` is reached. Writes the run artifacts
    (tree snapshot / rounds / summary) under ``out_dir`` before returning.
    """
    from css.coldstart import cold_start
    from css.logging_viz import write_run_artifacts

    cs = cold_start(
        env,
        target_client,
        optimizer_client,
        embedder,
        cfg=cfg,
        out_dir=out_dir,
    )
    tree, archive = cs.tree, cs.archive

    rounds: list[RoundResult] = []
    terminated_reason = "max_rounds"
    for round_index in range(max_rounds):
        # Termination check BEFORE the round: nothing active left to explore.
        if not tree.active_nodes():
            terminated_reason = "no_active_nodes"
            break

        rnd = run_round(
            tree,
            archive,
            env,
            target_client,
            optimizer_client,
            embedder,
            cfg=cfg,
            out_dir=out_dir,
            round_index=round_index,
        )
        rounds.append(rnd)

        # Exhausted: every active node is saturated (no EXPLOITATION pending) and
        # this round produced no new child. A new PROPOSAL/REFINE success keeps
        # the search alive; an all-saturated, no-growth round ends it.
        non_saturated = any(
            "EXPLOITATION" in b for b in rnd.branches
        )
        produced = any(b.endswith(":success") for b in rnd.branches)
        if not non_saturated and not produced and not tree.active_nodes():
            terminated_reason = "exhausted"
            break

    best = _best_unpruned(tree)
    best_node_id = best.node_id if best is not None else None

    run = RunResult(
        tree=tree,
        archive=archive,
        rounds=rounds,
        terminated_reason=terminated_reason,
        best_node_id=best_node_id,
    )
    write_run_artifacts(run, out_dir)
    return run
