"""Phase 6 cold start (Phase 0) — derive the ROOT strategy before the search loop.

The search tree needs a seed: a single ROOT node carrying an initial cognitive
strategy (``strategy_0``) and empty rules. The design (design_final_en.md §6,
training_mechanism_v6.md D3) bootstraps that seed by *attributing the bare LLM's
weaknesses*: run the frozen target model on the train tasks with NO skill at all,
analyse what it systematically gets wrong, and derive a first strategy that
targets exactly those weaknesses.

Concretely (the contract):

  1. **Bare rollout.** ``grouped_batch_rollout`` over ``env.train_items()`` with
     ``skill_text=""`` and the target client. The baseline score is the
     ``task_hard`` of ``aggregate_scores`` over the flattened rollouts.
  2. **Analysis.** Host the rollouts' patterns on a throwaway ``TreeNode``
     (``node_id="n0000"``, empty strategy/rules) and run ``run_analysis_epoch``
     with ``l0_saturated=True`` — cold start treats the bare LLM as already
     saturated (there is no L0 optimization to do first), so its persistent
     failure patterns immediately qualify as L1 signals.
  3. **Derive ``strategy_0``.** Attribute the root cause of the significant
     failure patterns (``attribute_root_cause``) and derive a strategy from the
     top cause + its success counterparts (``derive_strategy``). If no signals
     qualify, fall back to the most-significant failure patterns by support; if
     derivation still yields nothing, fall back to a minimal generic strategy.
  4. **Seed the tree.** A fresh ``SearchTree`` with one ROOT node (the derived
     ``strategy_0``, empty rules, the analysed pattern library) at epoch 0.

Heavy Phase-2/4/5 modules are imported lazily inside :func:`cold_start` so that
importing this module stays cheap and free of model/embedding side effects.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - type-only imports
    from css.analysis.embedding import Embedder
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
    """A minimal generic ``strategy_0`` when derivation yields nothing.

    Kept deliberately generic (a couple of ``###`` subsections so the document
    has the same shape REFINE/PROPOSAL expect) — the real strategy emerges from
    the search; this only guarantees the ROOT node is well-formed when the bare
    rollouts produced no actionable failure signal.
    """
    return (
        "### Understand before acting\n"
        "Read the full task and all provided inputs carefully before deciding "
        "on an approach. Restate the goal and the success criteria in your own "
        "terms, and identify the concrete outputs that will be checked.\n\n"
        "### Verify before finishing\n"
        "Before declaring the task complete, re-check the produced result "
        "against the stated requirements. Look for the most likely mistakes for "
        "this kind of task and confirm each requirement is actually satisfied."
    )


def _seed_failure_signals(
    library: "PatternLibrary",
    signals: "list[PatternRecord]",
    *,
    cfg: "CSSConfig",
) -> "list[PatternRecord]":
    """Pick the failure patterns to diagnose for ``strategy_0``.

    Prefer the qualifying L1 signals from Layer 3. When none qualify (a common
    cold-start case — the longitudinal trend predicate needs history this single
    epoch may not give), fall back to the most-significant active failure
    patterns by observation support (then by latest occurrence rate), capped at a
    handful so the attribution prompt stays focused.
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


def cold_start(
    env,
    target_client: "LLMClient",
    optimizer_client: "LLMClient",
    embedder: "Embedder",
    *,
    cfg: "CSSConfig",
    out_dir: str,
) -> "ColdStartResult":
    """Phase 0 — derive the ROOT strategy from the bare LLM's weaknesses.

    See the module docstring for the full pipeline. Never raises on a degenerate
    bare rollout or malformed optimizer output: derivation failures fall back to
    a minimal generic ``strategy_0`` so the search always gets a well-formed ROOT
    node. ``out_dir`` receives the bare-rollout artifacts under a ``coldstart``
    subdirectory.
    """
    import os

    # Lazy Phase-2/4/5 imports (keep module import cheap / side-effect-free).
    from css.data.negative_archive import NegativeArchive
    from css.data.tree import SearchTree, TreeNode
    from css.proposal.derivation import derive_strategy
    from css.proposal.root_cause import attribute_root_cause
    from css.rollout.batch import grouped_batch_rollout
    from css.data.rollout import aggregate_scores
    from css.analysis.pipeline import run_analysis_epoch

    cold_dir = os.path.join(out_dir, "coldstart")
    os.makedirs(cold_dir, exist_ok=True)

    # ── 1. Bare rollout: the frozen target model with NO skill ──────────────
    train_items = list(env.train_items())
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
    analysis = run_analysis_epoch(
        optimizer_client,
        embedder,
        temp_node,
        groups,
        epoch=0,
        l0_saturated=True,  # cold start: nothing to L0-optimize; treat as saturated
        cfg=cfg,
    )
    library = temp_node.pattern_records
    n_patterns = len(library)

    # ── 3. Derive strategy_0 from the significant failure patterns ──────────
    strategy_0 = ""
    signals = _seed_failure_signals(library, analysis.l1_signals, cfg=cfg)
    if signals:
        root_causes = attribute_root_cause(
            optimizer_client,
            signals,
            library,
            remedy_history=[],  # no L0 remedies tried yet at cold start
            cfg=cfg,
            current_strategy="",  # no strategy yet to blame
        )
        if root_causes:
            top = root_causes[0]
            # Paired SUCCESS counterparts to systematize (resolve via library).
            counterparts: list = []
            seen: set[str] = set()
            for pid in top.pattern_ids:
                rec = library.get(pid)
                if rec is None or not getattr(rec, "counterpart_id", ""):
                    continue
                cp = library.get(rec.counterpart_id)
                if cp is None or getattr(cp, "polarity", None) != "success":
                    continue
                if cp.pattern_id in seen:
                    continue
                seen.add(cp.pattern_id)
                counterparts.append(cp)
            proposal = derive_strategy(
                optimizer_client,
                top,
                counterparts,
                cfg=cfg,
                current_strategy="",
            )
            strategy_0 = (proposal.strategy_text or "").strip()

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
