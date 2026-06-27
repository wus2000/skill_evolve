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

    Uses the two-section format (``## Name`` + overview + ``### Details`` + detail)
    that REFINE/PROPOSAL expect — the real strategy emerges from the search;
    this only guarantees the ROOT node is well-formed when the bare rollouts
    produced no actionable failure signal.
    """
    return (
        "## Methodical Task Execution\n"
        "A disciplined approach that prioritizes thorough understanding of the "
        "task before acting, followed by systematic verification of the result. "
        "The core insight is that most failures stem from rushing to act before "
        "fully grasping what is being asked.\n\n"
        "### Details\n"
        "Before taking any action, read the full task description and all provided "
        "inputs carefully. Restate the goal and success criteria in your own terms, "
        "and identify the concrete outputs that will be checked. Form an explicit "
        "plan that maps each requirement to a specific action.\n\n"
        "When executing, work through the plan step by step, verifying each "
        "intermediate result before proceeding. If an unexpected situation arises, "
        "pause and re-evaluate the plan rather than pressing forward with assumptions.\n\n"
        "Before declaring the task complete, re-check the produced result against "
        "the stated requirements. Look for the most likely mistakes for this kind "
        "of task and confirm each requirement is actually satisfied."
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

    # ── 3. Derive strategy_0 from the significant failure patterns ──────────
    strategy_0 = ""
    signals = _seed_failure_signals(library, analysis.l1_signals, cfg=cfg)
    root_causes = []
    counterparts: list = []
    proposal = None
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

    _save_coldstart_derivation(cold_dir, signals, root_causes, proposal,
                               counterparts, strategy_0)

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
