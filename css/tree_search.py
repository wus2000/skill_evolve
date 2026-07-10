"""Tree-search orchestrator — the burst-granular L1 mechanism.

Implements L1_tree_mechanism_design.md §1: a flat-UCB loop over a three-state
node pool. Each decision either

  * runs a fixed ``cfg.burst_steps``-step L0 burst on an ACTIVE node
    (resuming its persistent rules state), or
  * spawns ONE child from a SATURATED node (root -> NEW, strategy node ->
    REFINE) and immediately runs the child's first burst — spawn and first
    burst are ATOMIC, so the selection pool never contains an unvisited node.

Saturation is judged at burst boundaries: the node's last
``cfg.saturation_dry_bursts`` (default 2) bursts all produced ZERO gate
accepts (user ruling 2026-07-05 — one dry burst is not evidence enough).
Saturated nodes are not killed: their results are locked into the run-level
``global_best`` snapshot, and their UCB score prices their next child.
TERMINAL is reached only by degree exhaustion (strategy nodes,
``cfg.node_degree``) or an explicit REFINE decline; the root is never terminal,
so a run ends on its decision budget (``cfg.max_decisions``).

Deployment is decoupled from selection: selection favors "how much more can
this grow" (val + slope + exploration bonus), the answer is "how high did
anything ever get" (``global_best``: the best (strategy, rules) snapshot).

Generation (NEW / REFINE pipelines, exploration) and the materials pass
(per-trajectory interpretation -> dossiers) are injected as callables so the
loop is unit-testable and the subsystems can land incrementally:

  * ``spawner(ctx) -> SpawnOutcome``  — produce a child node or decline;
  * ``materials_fn(ctx) -> None``     — post-burst dossier update (optional).

This module supersedes the legacy round loop in ``css.orchestrator``
(SELECT_BATCH -> exploit-to-saturation -> SYNC -> decide_branch -> proposal
cycle), which is retained only until the retirement pass removes it.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:  # pragma: no cover - type-only imports
    from css.config import CSSConfig
    from css.data.tree import SearchTree, TreeNode
    from css.model.client import LLMClient

_log = logging.getLogger("css")


# ──────────────────────────────────────────────────────────────────────────
# Layout (design §2.1: everything node-scoped lives under nodes/<id>/)
# ──────────────────────────────────────────────────────────────────────────
def node_dir(out_dir: str, node_id: str) -> str:
    return os.path.join(out_dir, "nodes", node_id)


def burst_dir(out_dir: str, node_id: str, burst_index: int, leaf: str = "") -> str:
    d = os.path.join(node_dir(out_dir, node_id), f"burst_{burst_index:04d}")
    return os.path.join(d, leaf) if leaf else d


def dossier_dir(out_dir: str, node_id: str) -> str:
    return os.path.join(node_dir(out_dir, node_id), "dossier")


def _append_jsonl(path: str, obj: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _write_json(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, path)


# ──────────────────────────────────────────────────────────────────────────
# Node birth: initial val measurement (baseline + gate incumbent)
# ──────────────────────────────────────────────────────────────────────────
def measure_initial_val(
    node: "TreeNode", env, target_client: "LLMClient", *,
    cfg: "CSSConfig", out_dir: str, val_items: "list[dict] | None" = None,
) -> str:
    """Measure the node's INITIAL skill (strategy + inherited rules) on the
    run-fixed val subset, before its first burst.

    Sets ``baseline_val_score`` and ``val_score`` (the L0 gate's first
    incumbent) and returns the predictions directory so the paired gate can
    seed its incumbent ledger from these rollouts (zero extra cost).
    """
    from css.data.rollout import aggregate_scores
    from css.evaluation.test_splits import env_extra_metrics, fmt_extra
    from css.orchestrator import _run_val_subset
    from css.rollout.batch import grouped_batch_rollout
    from css.skill_document import SkillDocument

    val_items = list(val_items) if val_items is not None else _run_val_subset(env, cfg)
    skill = SkillDocument(skill_dir="", strategy=node.strategy or "",
                          rules=node.rules or "").combined_skill_text()
    pred_dir = os.path.join(node_dir(out_dir, node.node_id), "val_baseline")
    groups = grouped_batch_rollout(
        env, val_items, skill, target_client,
        k_rollouts=cfg.k_rollouts,
        out_dir=pred_dir,
        max_workers=cfg.max_api_workers, task_timeout=cfg.task_timeout_s,
        epoch=0, node_id=node.node_id,
    )
    flat = [r for g in groups for r in g.rollouts]
    score = float(aggregate_scores(flat).get("task_hard", 0.0))
    node.baseline_val_score = score
    node.val_score = score
    _log.info("Node birth — node=%s initial val=%.4f (gate incumbent)%s",
              node.node_id, score, fmt_extra(env_extra_metrics(env, flat)))
    return pred_dir


# ──────────────────────────────────────────────────────────────────────────
# One burst
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class BurstResult:
    """Outcome of one B-step L0 burst at a node."""

    node_id: str
    burst_index: int              # the node's own burst ordinal (0-based)
    decision_index: int = 0       # global decision ordinal
    steps: int = 0
    n_accepted: int = 0
    val_before: float = 0.0
    val_after: float = 0.0
    reward: float = 0.0           # gated net val movement (val_after - val_before)
    best_updated: bool = False
    exploit_dir: str = ""         # step artifacts (rollout trajectories live here)
    stall_after: int = 0          # telemetry: steps_since_new_best at the boundary
                                  # (saturation itself is judged on burst_accepts)


def run_burst(
    tree: "SearchTree",
    node: "TreeNode",
    env,
    target_client: "LLMClient",
    optimizer_client: "LLMClient",
    *,
    cfg: "CSSConfig",
    out_dir: str,
    decision_index: int,
    ledger=None,
    coverage=None,
) -> BurstResult:
    """Run exactly ``cfg.burst_steps`` L0 steps at ``node`` (one tree visit).

    No saturation break inside the burst — saturation is judged by the caller
    at the burst boundary (consecutive dry bursts). The node's val
    refresh reuses the gate's accepted-candidate predictions (zero-rollout
    policy, same as the legacy round loop).
    """
    from css.data.rollout import aggregate_scores
    from css.data.tree import LearningCurvePoint
    from css.optimizer.exploitation import run_exploitation_epoch
    from css.orchestrator import _run_val_subset, _save_skill_snapshot, _val_skill_text
    from css.rollout.batch import grouped_batch_rollout
    from css.tracing import log_event

    burst_index = node.n_bursts
    train_items = list(env.train_items())
    val_items = _run_val_subset(env, cfg)
    exploit_dir = burst_dir(out_dir, node.node_id, burst_index, "exploit")

    # Gate incumbent-ledger seeding: point at the node's BIRTH predictions.
    # (First burst: seeds from the initial-val rollouts. Later bursts: the
    # node's persistent val_ledger already carries the incumbent; the seed
    # read is a no-op fallback.)
    cfg._val_baseline_dir = os.path.join(node_dir(out_dir, node.node_id), "val_baseline")

    val_before = node.val_score
    _log.info("Burst start — decision=%d node=%s burst=%d val=%.4f dry_streak=%d rules=%d chars",
              decision_index, node.node_id, burst_index, val_before,
              sum(1 for a in reversed(node.burst_accepts) if a == 0
                  ) if node.burst_accepts and node.burst_accepts[-1] == 0 else 0,
              len(node.rules or ""))
    log_event("burst_start", decision_index=decision_index, node_id=node.node_id,
              burst_index=burst_index, val_before=val_before,
              stall=node.step_buffer.steps_since_new_best())

    summary = run_exploitation_epoch(
        node, env, train_items, val_items, target_client, optimizer_client,
        cfg, exploit_dir,
        epoch=decision_index,
        current_score=node.val_score,
        exact_steps=cfg.burst_steps,
        ledger=ledger,
        coverage=coverage,
    )
    if coverage is not None:
        try:
            coverage.save()
        except Exception:  # noqa: BLE001 — the book must not kill the burst
            _log.exception("coverage ledger save failed (continuing)")

    # Val refresh — zero-rollout reuse of the gate's accepted predictions.
    best_val_dir = getattr(summary, "best_val_out_dir", "")
    if best_val_dir:
        if getattr(cfg, "gate_mode", "mean") == "paired":
            val_k = max(1, getattr(cfg, "gate_screen_k", 1))
        else:
            val_k = getattr(cfg, "exploitation_val_k", 0) or cfg.k_rollouts
        val_groups = grouped_batch_rollout(
            env, val_items, _val_skill_text(node), target_client,
            k_rollouts=val_k, out_dir=best_val_dir,
            max_workers=cfg.max_api_workers, task_timeout=cfg.task_timeout_s,
            epoch=decision_index, node_id=node.node_id,
        )
        val_flat = [r for g in val_groups for r in g.rollouts]
        node.val_score = float(aggregate_scores(val_flat).get("task_hard", 0.0))
    # else: no new best this burst -> val_score carries (skill unchanged).

    node.n_bursts += 1
    reward = node.val_score - val_before
    node.burst_rewards.append(reward)
    node.burst_accepts.append(int(summary.n_accepted))
    node.maturity += 1
    node.record_learning_point(LearningCurvePoint(
        epoch=decision_index,
        n_steps=node.n_steps,
        train_score=node.train_score,
        val_score=node.val_score,
        accept_rate=node.step_buffer.accept_rate(),
        accept_slope=node.accept_slope(cfg.W),
    ))

    # ── Burst-end document metabolism (design docs/L0_document_metabolism.md
    # §3): one whole-document tidy-up of the node's inheritable rules, gated
    # non-inferior. Placed AFTER the burst reward is recorded (its neutral
    # score wobble must not enter the L1 selection signal) and BEFORE the
    # skill snapshot (the tree inherits the tidied document). A failed or
    # rejected tidy-up leaves the document untouched; a crashed one must not
    # kill the burst.
    if (getattr(cfg, "consolidation_enabled", False)
            and getattr(cfg, "edit_pipeline", "v2") == "v3"):
        from css.optimizer.editpipe3.metabolism import run_burst_consolidation
        try:
            run_burst_consolidation(
                node, env, val_items, target_client, optimizer_client, cfg,
                burst_dir(out_dir, node.node_id, burst_index, "consolidation"),
                decision_index=decision_index)
        except Exception:  # noqa: BLE001 — metabolism must not kill the burst
            _log.exception("consolidation failed (document unchanged, "
                           "continuing)")

    _save_skill_snapshot(out_dir, node, decision_index)

    result = BurstResult(
        node_id=node.node_id,
        burst_index=burst_index,
        decision_index=decision_index,
        steps=summary.n_steps,
        n_accepted=summary.n_accepted,
        val_before=val_before,
        val_after=node.val_score,
        reward=reward,
        best_updated=bool(best_val_dir),
        exploit_dir=exploit_dir,
        stall_after=node.step_buffer.steps_since_new_best(),
    )

    # Mechanical dossier ledger (design §2.1 bursts.jsonl).
    _append_jsonl(os.path.join(dossier_dir(out_dir, node.node_id), "bursts.jsonl"), {
        "burst": burst_index, "decision": decision_index,
        "steps": result.steps, "accepted": result.n_accepted,
        "val_before": round(val_before, 6), "val_after": round(node.val_score, 6),
        "reward": round(reward, 6), "stall_after": result.stall_after,
        "rules_chars": len(node.rules or ""),
    })

    _log.info("Burst done — decision=%d node=%s burst=%d steps=%d accepted=%d "
              "val %.4f -> %.4f (r=%+.4f) stall=%d",
              decision_index, node.node_id, burst_index, result.steps,
              result.n_accepted, val_before, node.val_score, reward,
              result.stall_after)
    log_event("burst_done", decision_index=decision_index, node_id=node.node_id,
              burst_index=burst_index, steps=result.steps,
              n_accepted=result.n_accepted, val_before=val_before,
              val_after=node.val_score, reward=reward,
              stall_after=result.stall_after)
    return result


# ──────────────────────────────────────────────────────────────────────────
# Spawn interface (production impl lands in css.l1gen; tests inject fakes)
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class SpawnContext:
    """Everything a spawner needs to generate one child."""

    tree: "SearchTree"
    parent: "TreeNode"
    mode: str                     # "NEW" (root parent) | "REFINE" (strategy parent)
    new_node_id: str
    cfg: "CSSConfig"
    env: Any
    target_client: "LLMClient"
    optimizer_client: "LLMClient"
    out_dir: str
    decision_index: int
    ledger: Any = None


@dataclass
class SpawnOutcome:
    """Result of one spawn attempt.

    ``child is None`` with ``decline=True`` means the generation pipeline
    honestly found no defensible target (REFINE: no demonstrated A/B cause
    even after exploring U groups) — the parent goes TERMINAL, no junk child.
    ``child is None`` with ``decline=False`` is a pipeline failure — the
    decision is consumed but the parent stays saturated (retryable later).
    """

    child: "TreeNode | None" = None
    mode: str = ""
    decline: bool = False
    reason: str = ""
    artifacts_dir: str = ""


SpawnerFn = Callable[[SpawnContext], SpawnOutcome]
MaterialsFn = Callable[..., None]


def _unwired_spawner(ctx: SpawnContext) -> SpawnOutcome:  # pragma: no cover
    raise RuntimeError(
        "tree-search spawner is not wired: the NEW/REFINE generation pipelines "
        "(css.l1gen) must be provided via run_css_tree(spawner=...) — refusing "
        "to run a live search without generation"
    )


def root_spawn_mode(coverage, cfg: "CSSConfig", n_nodes: int = 1) -> str:
    """Root three-way dispatch (L1_actions_redesign §3): NEW vs MERGE.

    NEW while an unsolved frontier (or any uncharted blind spot) remains;
    MERGE on TRUE full coverage — every registered train task attempted and
    solved by SOME node. The degenerate-matrix case (nothing exclusive to
    fuse) is judged inside the MERGE pipeline, which then DECLINES (a root
    decline blocks without a strike). The transition is reversible: a MERGE
    child regressing re-opens global_unsolved and the next spawn is NEW.
    """
    if coverage is None or not coverage.has_data():
        return "NEW"
    if not coverage.registered_count():
        # Universe unknown (registration failed/skipped): TRUE full coverage
        # can never be declared over a merely-sampled subset.
        return "NEW"
    if coverage.global_unsolved() or coverage.uncharted():
        return "NEW"
    if coverage.fragile_set(
            rate_lt=getattr(cfg, "fragile_rate", 0.4),
            mature_step=getattr(cfg, "fragile_mature_step", 2),
            mature_min=getattr(cfg, "fragile_mature_min", 3),
            hist_attempts=getattr(cfg, "fragile_hist_attempts", 4)):
        # Solved-once is not solved-reliably: while any task's mature pass
        # rate sits under the fragile threshold there is still a NEW frontier
        # (user ruling 2026-07-08 — measured: AW hit union-unsolved=0 while a
        # task passed 2/18).
        return "NEW"
    if n_nodes < 2:
        # A one-node tree has no MERGE matrix (degenerate by construction —
        # the pipeline would only DECLINE); force NEW instead of burning the
        # decision (user ruling 2026-07-08).
        return "NEW"
    return "MERGE"


def meaningful_delta(cfg: "CSSConfig") -> float:
    """delta = max(k / n_val, floor) — a new best must clear >= k net val
    tasks to count as MEANINGFUL (user ruling 2026-07-08: the unit of
    substance is tasks, scaled by the val set; k=2 mirrors the verify
    min_net_flips>=2 anti-noise ruling; the floor keeps large val sets from
    degrading the threshold into noise)."""
    k = max(1, int(getattr(cfg, "saturation_meaningful_tasks", 2)))
    n_val = max(1, int(getattr(cfg, "n_val", 0) or 0))
    floor = float(getattr(cfg, "saturation_meaningful_floor", 0.01))
    return max(k / n_val, floor)


def node_stalled(node: "TreeNode", cfg: "CSSConfig") -> bool:
    """Saturation judgement at the burst boundary — no MEANINGFUL new best
    for the last ``saturation_dry_bursts`` bursts' worth of steps.

    Lineage of the signal (each supersedes the previous by user ruling):
    2026-07-05 zero-accept bursts -> 2026-07-06 no accept_new_best (a
    noise-limited paired gate keeps minting small item-win accepts at a
    basin's flat top) -> 2026-07-08 no MEANINGFUL new best: the gate's mean
    tie-break also mints noise-level new bests (measured live: a +0.05pp anb
    reset the stall clock and delayed saturation a full burst), so only a
    best that clears :func:`meaningful_delta` resets the clock. Bookkeeping
    best still updates on every anb. Resume-safe, no new state.
    """
    k = max(1, getattr(cfg, "saturation_dry_bursts", 2))
    window = k * max(1, getattr(cfg, "burst_steps", 5))
    return node.step_buffer.steps_since_meaningful_best(
        meaningful_delta(cfg)) >= window


def spawn_unlocked(node: "TreeNode", cfg: "CSSConfig") -> bool:
    """W_soft (S3, user ruling 2026-07-08): one burst without a meaningful
    new best unlocks spawn ARBITRATION on an active node — a plateau hint
    opens the option; the hard window (:func:`node_stalled`) stays the
    forced-spawn backstop."""
    soft = max(1, getattr(cfg, "spawn_soft_bursts", 1))
    window = soft * max(1, getattr(cfg, "burst_steps", 5))
    return node.step_buffer.steps_since_meaningful_best(
        meaningful_delta(cfg)) >= window


def supply_fraction(coverage, cfg: "CSSConfig") -> float:
    """Open-frontier fraction: |unsolved ∪ uncharted ∪ fragile| / registered."""
    if coverage is None or not coverage.registered_count():
        return 0.0
    supply = (coverage.global_unsolved() | coverage.uncharted()
              | coverage.fragile_set(
                  rate_lt=getattr(cfg, "fragile_rate", 0.4),
                  mature_step=getattr(cfg, "fragile_mature_step", 2),
                  mature_min=getattr(cfg, "fragile_mature_min", 3),
                  hist_attempts=getattr(cfg, "fragile_hist_attempts", 4)))
    return len(supply) / float(coverage.registered_count())


def spawn_arbitration(node: "TreeNode", coverage, cfg: "CSSConfig") -> "tuple[bool, dict]":
    """S3 action arbitration on an unlocked ACTIVE node: keep exploiting or
    spawn now?

    burst_score = mean meaningful gain of the last two bursts (a reward
    below delta counts as zero — churn is not yield); spawn_score =
    lambda * open-supply fraction. Ties keep exploiting. Returns
    (spawn_now, telemetry).
    """
    delta = meaningful_delta(cfg)
    rewards = list(getattr(node, "burst_rewards", []) or [])[-2:]
    gains = [r if r >= delta else 0.0 for r in rewards]
    burst_score = (sum(gains) / len(gains)) if gains else 0.0
    spawn_score = (float(getattr(cfg, "spawn_supply_lambda", 0.05))
                   * supply_fraction(coverage, cfg))
    return spawn_score > burst_score, {
        "burst_score": round(burst_score, 6),
        "spawn_score": round(spawn_score, 6), "delta": round(delta, 6)}


# ──────────────────────────────────────────────────────────────────────────
# Global-best tracking (deployment answer, decoupled from selection)
# ──────────────────────────────────────────────────────────────────────────
def _update_global_best(
    global_best: dict, node: "TreeNode", decision_index: int,
) -> bool:
    """Fold the node's current (val, best-skill) into the run snapshot."""
    if node.val_score <= float(global_best.get("val", float("-inf"))):
        return False
    rules = node.best_rules if node.best_rules else (node.rules or "")
    global_best.update({
        "val": node.val_score,
        "node_id": node.node_id,
        "decision_index": decision_index,
        "strategy": node.strategy or "",
        "rules": rules,
    })
    return True


# ──────────────────────────────────────────────────────────────────────────
# The main loop
# ──────────────────────────────────────────────────────────────────────────
def run_css_tree(
    env,
    target_client: "LLMClient",
    optimizer_client: "LLMClient",
    *,
    cfg: "CSSConfig",
    out_dir: str,
    resume: bool = False,
    spawner: "SpawnerFn | None" = None,
    burst_fn=None,
    materials_fn: "MaterialsFn | None" = None,
    max_decisions: int | None = None,
):
    """Full tree-search run: bare root -> flat-UCB decision loop -> global best.

    Returns a :class:`css.orchestrator.RunResult` (``rounds`` carries one
    decision-record dict per decision). A stage checkpoint is written after
    every decision (``dec_%04d``); ``resume=True`` continues from the latest
    checkpoint under ``out_dir`` (config fingerprint guarded).
    """
    import random

    from css.checkpoint import (
        Checkpoint, capture_rng_state, config_fingerprint,
        latest_checkpoint, load_checkpoint, restore_rng_state, save_checkpoint,
    )
    from css.data.negative_archive import NegativeArchive
    from css.data.task_ledger import TaskDifficultyLedger
    from css.data.tree import SearchTree, TreeNode
    from css.model.client import OptimizerOnlyClient, TargetOnlyClient
    from css.orchestrator import RunResult, _run_val_subset, _save_skill_snapshot, _val_skill_text
    from css.tracing import TracingLLMClient, init_trace, log_event
    from css.tree.select import select_node, total_bursts, total_selections

    os.makedirs(out_dir, exist_ok=True)
    init_trace(out_dir)
    log_event("run_start", mechanism="tree_search",
              target_model=cfg.target_model, optimizer_model=cfg.optimizer_model,
              burst_steps=cfg.burst_steps, node_degree=cfg.node_degree,
              max_decisions=max_decisions or cfg.max_decisions, resume=resume)

    # build_clients now bakes the tracing wrap in (so out-of-tree drivers get
    # audited too); keep this stitch ONLY for hand-built clients (unit tests,
    # legacy paths) and never double-wrap.
    if isinstance(target_client, TargetOnlyClient) and not isinstance(
            getattr(target_client, "_inner", None), TracingLLMClient):
        target_client._inner = TracingLLMClient(target_client._inner, role="target")
    if isinstance(optimizer_client, OptimizerOnlyClient) and not isinstance(
            getattr(optimizer_client, "_inner", None), TracingLLMClient):
        optimizer_client._inner = TracingLLMClient(optimizer_client._inner, role="optimizer")

    spawn = spawner or _unwired_spawner
    do_burst = burst_fn or run_burst
    budget = max_decisions if max_decisions is not None else cfg.max_decisions
    fp = config_fingerprint(cfg)
    # Normalize the "n_val=0 = whole split" launcher convention AFTER
    # fingerprinting (old checkpoints hashed the raw 0; changing the input to
    # the hash would refuse every legacy resume). meaningful_delta() reads
    # cfg.n_val, and 0 degenerates delta to k/max(1,0) = 2.0 — every new best
    # would be judged non-meaningful, so every burst looks dry: spurious
    # saturation + spawn pressure (found on the 2026-07-10 WebArena launch,
    # where n_train/n_val/n_test are all 0 by convention).
    if not getattr(cfg, "n_val", 0) and env is not None and hasattr(env, "val_items"):
        try:
            cfg.n_val = len(env.val_items())
        except Exception:  # noqa: BLE001 — envless harness paths stay as-is
            pass
    decisions: list[dict] = []
    global_best: dict = {}
    ckpt_path = latest_checkpoint(out_dir) if resume else None

    # Coverage ledger (L1_actions_redesign §1.1) — file-persisted, so resume
    # simply reloads it; the registered universe is the env's train set.
    from css.coverage import CoverageLedger, coverage_path
    coverage = CoverageLedger.load(
        coverage_path(out_dir),
        min_attempts=int(getattr(cfg, "ledger_min_attempts", 1)),
        recent_len=int(getattr(cfg, "coverage_recent_len", 12)))
    coverage.path = coverage_path(out_dir)
    # Register the FULL train universe (the uncharted domain). Silent failure
    # here would collapse uncharted() to empty and let root_spawn_mode declare
    # full coverage over a sampled subset (code-review finding 2026-07-07) —
    # so only the envless harness case stays quiet; real errors are LOUD, and
    # root_spawn_mode independently refuses MERGE while nothing is registered.
    from css.explore._util import item_id as _item_id
    if env is not None and hasattr(env, "train_items"):
        try:
            coverage.register_tasks(_item_id(it) for it in env.train_items())
        except Exception:  # noqa: BLE001
            _log.exception(
                "coverage: train-universe registration FAILED — uncharted() "
                "will under-report and MERGE stays disabled until registration "
                "succeeds")

    if ckpt_path:
        ckpt = load_checkpoint(ckpt_path)
        if ckpt.config_fingerprint and ckpt.config_fingerprint != fp:
            raise ValueError(
                f"resume refused: config fingerprint mismatch "
                f"(checkpoint={ckpt.config_fingerprint} current={fp})"
            )
        tree = ckpt.tree
        baseline_score = ckpt.baseline_score
        start_decision = ckpt.next_round
        ledger = TaskDifficultyLedger.from_dict(ckpt.ledger)
        global_best = dict(ckpt.global_best)
        decisions = list(ckpt.rounds)
        restore_rng_state(ckpt.rng_state)
        _log.info("RESUMED tree search from %s — next_decision=%d nodes=%d "
                  "global_best=%.4f@%s",
                  os.path.basename(ckpt_path), start_decision, len(tree.nodes),
                  float(global_best.get("val", 0.0)), global_best.get("node_id"))
    else:
        random.seed(cfg.seed)
        ledger = TaskDifficultyLedger(ceiling_rounds=getattr(cfg, "analysis_ceiling_rounds", 4))
        tree = SearchTree()
        # Root = bare agent: empty strategy, empty rules. Its own L0 bursts are
        # the strategy-free tactical arm (built-in ablation); its birth val is
        # the bare baseline anchor.
        root = TreeNode(node_id=tree.new_node_id(), branch_type="ROOT",
                        strategy="", rules="", created_epoch=0)
        tree.add_root(root)
        measure_initial_val(root, env, target_client, cfg=cfg, out_dir=out_dir)
        baseline_score = root.val_score
        _save_skill_snapshot(out_dir, root, 0)
        _update_global_best(global_best, root, decision_index=-1)
        # Bare-root test anchor (the paper's baseline row) — one test eval.
        try:
            if env is None:
                raise RuntimeError("no env (unit-test harness)")
            from css.evaluation.test_splits import evaluate_test_splits
            report = evaluate_test_splits(
                env, _val_skill_text(root), target_client, cfg,
                os.path.join(node_dir(out_dir, root.node_id), "test_baseline"),
                epoch=0, node_id=root.node_id, label="Bare-root baseline test",
            )
            primary = next(iter(report))
            global_best["test"] = report[primary]["score"]
            log_event("bare_root_test", score=report[primary]["score"])
        except Exception:  # noqa: BLE001 — the anchor must not block the run
            _log.exception("bare-root test anchor failed (continuing)")
        start_decision = 0
        save_checkpoint(Checkpoint(
            stage="init", next_round=0, baseline_score=baseline_score,
            tree=tree, archive=NegativeArchive(), config_fingerprint=fp,
            rng_state=capture_rng_state(), rounds=[], created_ts=time.time(),
            ledger=ledger.to_dict(), global_best=global_best,
        ), out_dir)
        _log.info("Tree search initialized — root=%s bare val=%.4f", root.node_id, baseline_score)

    terminated_reason = "max_decisions"
    for decision_index in range(start_decision, budget):
        # Flip degree-exhausted saturated strategy nodes to terminal (lazy).
        for n in tree.selectable_nodes():
            if n.status == "saturated" and n.degree_exhausted(cfg.node_degree):
                n.status = "terminal"
                _log.info("Node %s TERMINAL — degree exhausted (%d children)",
                          n.node_id, len(n.children_ids))
                log_event("node_terminal", node_id=n.node_id, reason="degree_exhausted",
                          decision_index=decision_index)

        pool = tree.selectable_nodes()
        if not pool:
            terminated_reason = "all_terminal"
            break

        node = select_node(tree, cfg=cfg)
        assert node is not None
        # Charge the selection (redesign §2): every pick consumes a decision,
        # burst and spawn alike — uncharged spawns were the root-monopoly
        # mechanism in the AW post-mortem.
        node.n_selections += 1
        _log.info("Decision %d/%d — selected %s (status=%s val=%.4f bursts=%d "
                  "sel=%d T=%d pool=%d)",
                  decision_index, budget, node.node_id, node.status,
                  node.val_score, node.n_bursts, node.n_selections,
                  total_selections(tree), len(pool))
        log_event("decision", decision_index=decision_index, node_id=node.node_id,
                  status=node.status, val=node.val_score, n_bursts=node.n_bursts,
                  n_selections=node.n_selections,
                  pool=[p.node_id for p in pool])

        record: dict = {"index": decision_index, "node_id": node.node_id}

        # ── S3 action arbitration (user ruling 2026-07-08): an ACTIVE node
        # past the soft window may spawn NOW when the open supply outweighs
        # its recent meaningful exploitation pace. The hard window
        # (node_stalled) below remains the forced backstop.
        spawn_now = False
        if node.status == "active" and spawn_unlocked(node, cfg):
            spawn_now, arb = spawn_arbitration(node, coverage, cfg)
            record.update(arbitration=arb, soft_unlocked=True)
            if spawn_now:
                _log.info(
                    "S3 arbitration — node %s spawns from ACTIVE: "
                    "spawn_score=%.4f > burst_score=%.4f (delta=%.4f)",
                    node.node_id, arb["spawn_score"], arb["burst_score"],
                    arb["delta"])
                log_event("s3_spawn_from_active", node_id=node.node_id,
                          decision_index=decision_index, **arb)

        if node.status == "active" and not spawn_now:
            br = do_burst(tree, node, env, target_client, optimizer_client,
                          cfg=cfg, out_dir=out_dir, decision_index=decision_index,
                          ledger=ledger, coverage=coverage)
            record.update(kind="burst", burst=br.burst_index, steps=br.steps,
                          accepted=br.n_accepted, reward=round(br.reward, 6),
                          val_after=round(br.val_after, 6))
            if materials_fn is not None:
                try:
                    materials_fn(tree=tree, node=node, burst_result=br, env=env,
                                 optimizer_client=optimizer_client, cfg=cfg,
                                 out_dir=out_dir, ledger=ledger)
                except Exception:  # noqa: BLE001 — dossiers must not kill the run
                    _log.exception("materials pass failed for %s burst %d "
                                   "(continuing)", node.node_id, br.burst_index)
            if node_stalled(node, cfg):
                node.status = "saturated"
                stall = node.step_buffer.steps_since_meaningful_best(
                    meaningful_delta(cfg))
                _log.info("Node %s SATURATED at burst boundary — no "
                          "MEANINGFUL new best (delta=%.4f) for %d steps "
                          "(window=%d)", node.node_id, meaningful_delta(cfg),
                          stall, max(1, getattr(cfg, "saturation_dry_bursts", 2))
                          * max(1, getattr(cfg, "burst_steps", 5)))
                log_event("node_saturated", node_id=node.node_id,
                          decision_index=decision_index,
                          stall_meaningful=stall,
                          burst_accepts=list(node.burst_accepts))
        else:  # saturated (or S3-arbitrated) -> spawn + first burst (atomic)
            mode = (root_spawn_mode(coverage, cfg, n_nodes=len(tree.nodes))
                    if node.is_root else "REFINE")
            ctx = SpawnContext(
                tree=tree, parent=node, mode=mode, new_node_id=tree.new_node_id(),
                cfg=cfg, env=env, target_client=target_client,
                optimizer_client=optimizer_client, out_dir=out_dir,
                decision_index=decision_index, ledger=ledger,
            )
            outcome = spawn(ctx)
            record.update(kind="spawn", mode=mode)
            if outcome.child is None:
                record.update(spawned=None, decline=outcome.decline,
                              reason=outcome.reason)
                log_event("spawn_none", decision_index=decision_index,
                          node_id=node.node_id, mode=mode,
                          decline=outcome.decline, reason=outcome.reason)
                if outcome.decline and not node.is_root \
                        and node.status != "active":
                    node.status = "terminal"
                    _log.info("Node %s TERMINAL — REFINE declined: %s",
                              node.node_id, outcome.reason)
                elif outcome.decline and node.status == "active":
                    # S3 path: a soft-unlocked ACTIVE node keeps exploiting —
                    # a declined spawn is a world-state verdict on the spawn,
                    # never a death sentence for a node that can still burst.
                    node.spawn_block_T = total_bursts(tree) + 1
                    _log.info("S3 spawn from ACTIVE node %s declined (%s) — "
                              "node stays active, spawn blocked one burst",
                              node.node_id, outcome.reason)
                elif outcome.decline:
                    # Root decline = world-state says the action is pointless
                    # right now (e.g. degenerate MERGE matrix), NOT a pipeline
                    # failure — block without a strike (redesign §2).
                    node.spawn_block_T = total_bursts(tree) + 3
                    _log.info("Root spawn DECLINED (%s) — blocked until 3 "
                              "more bursts land", outcome.reason)
                    log_event("root_spawn_declined", node_id=node.node_id,
                              reason=outcome.reason,
                              decision_index=decision_index)
                else:
                    # Cooldown, not a free retry: with unchanged materials the
                    # generator reproduces the same duplicate child, and UCB
                    # reselects the highest-val saturated node every decision
                    # (observed: AW burned decisions 6-10 in a spin). Block
                    # until any burst lands somewhere; 3 strikes -> terminal
                    # for strategy nodes. The root is the only NEW/MERGE entry
                    # point (a terminal root seals off the phase transition),
                    # so it gets an exponentially longer block instead
                    # (redesign §2).
                    node.spawn_fail_count += 1
                    node.spawn_block_T = total_bursts(tree)
                    if node.spawn_fail_count >= 3:
                        if node.is_root:
                            extra = 3 * (2 ** (node.spawn_fail_count - 3))
                            node.spawn_block_T = total_bursts(tree) + extra
                            _log.warning(
                                "Root spawn failed %d times (last: %s) — "
                                "long-blocked until %d more bursts land",
                                node.spawn_fail_count, outcome.reason, extra)
                            log_event("root_spawn_blocked",
                                      node_id=node.node_id,
                                      fail_count=node.spawn_fail_count,
                                      block_extra=extra,
                                      decision_index=decision_index)
                        elif node.status != "active":
                            node.status = "terminal"
                            _log.info("Node %s TERMINAL — %d spawns produced "
                                      "no child (last: %s)", node.node_id,
                                      node.spawn_fail_count, outcome.reason)
                            log_event("node_terminal", node_id=node.node_id,
                                      reason="spawn_exhausted",
                                      decision_index=decision_index)
                        else:
                            _log.warning(
                                "S3 spawn from ACTIVE node %s failed %d "
                                "times (last: %s) — node stays active, "
                                "spawn blocked", node.node_id,
                                node.spawn_fail_count, outcome.reason)
                    else:
                        _log.warning(
                            "Spawn produced no child (%s) — node %s blocked "
                            "from selection until the next burst lands "
                            "(fail %d/3)", outcome.reason, node.node_id,
                            node.spawn_fail_count)
            else:
                node.spawn_fail_count = 0     # fresh evidence: cooldown resets
                child = outcome.child
                child.created_epoch = decision_index
                # Spawn + first burst is atomic: the child enters the pool
                # already charged once (no inf-UCB newborns).
                child.n_selections = 1
                if mode == "NEW":
                    child.branch_type = "NEW"
                    child.rules = ""            # zero inheritance (user ruling)
                    child.best_rules = ""
                elif mode == "MERGE":
                    # MERGE integrates verified assets: the pipeline's
                    # selectively-assembled rules stay (L1_actions_redesign §6).
                    child.branch_type = "MERGE"
                else:
                    child.branch_type = "REFINE"
                tree.add_child(node.node_id, child)
                measure_initial_val(child, env, target_client, cfg=cfg, out_dir=out_dir)
                _save_skill_snapshot(out_dir, child, decision_index)
                log_event("spawned", decision_index=decision_index,
                          parent=node.node_id, child=child.node_id, mode=mode,
                          child_baseline=child.val_score,
                          strategy_len=len(child.strategy or ""),
                          rules_len=len(child.rules or ""))
                br = do_burst(tree, child, env, target_client, optimizer_client,
                              cfg=cfg, out_dir=out_dir, decision_index=decision_index,
                              ledger=ledger, coverage=coverage)
                if mode == "MERGE":
                    # Step 7 (L1_actions_redesign §6): the first burst has
                    # landed in the ledger — check the fusion's declared
                    # coverage. Record-only; never blocks the spawn.
                    try:
                        from css.l1gen.merge_pipeline import merge_coverage_check
                        merge_coverage_check(out_dir, child.node_id, cfg)
                    except Exception:  # noqa: BLE001
                        _log.exception("merge coverage check failed (continuing)")
                # Spawn reward is rebased to the PARENT's val (redesign §2):
                # measuring against the empty-rules baseline booked +0.43..
                # +0.59 of cold-start recovery as profit (20x a burst reward)
                # while every child sat below the incumbent. The child's own
                # first-burst reward keeps its meaning in child.burst_rewards.
                record.update(spawned=child.node_id,
                              child_baseline=round(child.baseline_val_score, 6),
                              reward=round(child.val_score - node.val_score, 6),
                              val_after=round(br.val_after, 6))
                if materials_fn is not None:
                    try:
                        materials_fn(tree=tree, node=child, burst_result=br, env=env,
                                     optimizer_client=optimizer_client, cfg=cfg,
                                     out_dir=out_dir, ledger=ledger)
                    except Exception:  # noqa: BLE001
                        _log.exception("materials pass failed for %s burst %d "
                                       "(continuing)", child.node_id, br.burst_index)
                if node_stalled(child, cfg):
                    child.status = "saturated"
                if node.degree_exhausted(cfg.node_degree):
                    node.status = "terminal"
                    _log.info("Node %s TERMINAL — degree exhausted after spawn",
                              node.node_id)

        # Deployment tracking + optional test eval on a new global best.
        improved_nodes = [tree.get(record["node_id"])] + (
            [tree.get(record["spawned"])] if record.get("spawned") else [])
        improved = False
        for n in improved_nodes:
            if n is not None and _update_global_best(global_best, n, decision_index):
                improved = True
        if improved:
            _log.info("Global best — val=%.4f node=%s", global_best["val"],
                      global_best["node_id"])
            log_event("global_best", decision_index=decision_index,
                      val=global_best["val"], node_id=global_best["node_id"])
            if getattr(cfg, "test_eval_on_new_best", True):
                try:
                    from css.evaluation.test_splits import evaluate_test_splits
                    best_node = tree.get(global_best["node_id"])
                    report = evaluate_test_splits(
                        env, _val_skill_text(best_node), target_client, cfg,
                        burst_dir(out_dir, best_node.node_id,
                                  max(0, best_node.n_bursts - 1), "test"),
                        epoch=decision_index, node_id=best_node.node_id,
                        label="Decision %d test (new global best)" % decision_index,
                    )
                    primary = next(iter(report))
                    global_best["test"] = report[primary]["score"]
                    record["test"] = report[primary]["score"]
                except Exception:  # noqa: BLE001 — reporting must not kill the run
                    _log.exception("test eval on new global best failed (continuing)")
        record["global_best_val"] = round(float(global_best.get("val", 0.0)), 6)
        decisions.append(record)
        _append_jsonl(os.path.join(out_dir, "decisions.jsonl"), record)

        save_checkpoint(Checkpoint(
            stage=f"dec_{decision_index:04d}", next_round=decision_index + 1,
            baseline_score=baseline_score, tree=tree, archive=NegativeArchive(),
            config_fingerprint=fp, rng_state=capture_rng_state(),
            rounds=decisions, created_ts=time.time(),
            ledger=ledger.to_dict(), global_best=global_best,
        ), out_dir)

    # Run artifacts: final tree snapshot + summary (self-contained writer; the
    # legacy logging_viz round renderer expects RoundResult objects).
    _write_json(os.path.join(out_dir, "tree_final.json"), tree.to_dict())
    _write_json(os.path.join(out_dir, "summary.json"), {
        "terminated_reason": terminated_reason,
        "decisions": len(decisions),
        "global_best": {k: v for k, v in global_best.items()
                        if k not in ("strategy", "rules")},
        "baseline_val": baseline_score,
        "nodes": {n.node_id: {"status": n.status, "val": n.val_score,
                              "bursts": n.n_bursts, "children": n.children_ids,
                              "branch": n.branch_type}
                  for n in tree.nodes.values()},
    })
    if global_best.get("strategy") is not None:
        gb_dir = os.path.join(out_dir, "global_best")
        os.makedirs(gb_dir, exist_ok=True)
        with open(os.path.join(gb_dir, "strategy.md"), "w", encoding="utf-8") as f:
            f.write(global_best.get("strategy", ""))
        with open(os.path.join(gb_dir, "rules.md"), "w", encoding="utf-8") as f:
            f.write(global_best.get("rules", ""))

    from css.tracing import log_event as _le
    _le("run_done", terminated_reason=terminated_reason,
        best_node_id=global_best.get("node_id"),
        best_val=float(global_best.get("val", 0.0)), n_decisions=len(decisions))
    _log.info("Tree search done — %s after %d decisions; global best val=%.4f @ %s",
              terminated_reason, len(decisions),
              float(global_best.get("val", 0.0)), global_best.get("node_id"))

    return RunResult(
        tree=tree,
        archive=NegativeArchive(),
        rounds=decisions,  # decision-record dicts (len()/iteration compatible)
        terminated_reason=terminated_reason,
        best_node_id=global_best.get("node_id"),
    )
