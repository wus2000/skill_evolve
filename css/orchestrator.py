"""CSS run entry point + shared node/run helpers.

``run_css`` — the entry every launcher calls — delegates to the TREE-SEARCH
mechanism (:mod:`css.tree_search`, design: ``L1_tree_mechanism_design.md``),
wiring in the production generation spawner (:mod:`css.l1gen`) and the
materials pass (:mod:`css.materials`).

The legacy round loop that used to live here (SELECT_BATCH -> exploit to
saturation -> SYNC -> decide_branch -> eight-round PROPOSAL exam) was RETIRED
with the mechanism redesign, together with ``css.proposal``, ``css.coldstart``
and ``css.tree.branching``. Its measured failure modes — the empty-rules
one-shot exam, the forced deploy of net-negative children, the inf-UCB chain
degeneration — are documented in the design doc §0.

This module keeps:
  * the frozen result records (:class:`RoundResult`, :class:`RunResult`) —
    ``RunResult.rounds`` now carries the tree loop's per-decision dicts;
  * the node-level helpers shared with :mod:`css.tree_search`
    (``_run_val_subset`` / ``_val_skill_text`` / ``_save_skill_snapshot``).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

_log = logging.getLogger("css")

if TYPE_CHECKING:  # pragma: no cover - type-only imports
    from css.config import CSSConfig
    from css.data.negative_archive import NegativeArchive
    from css.data.tree import SearchTree, TreeNode
    from css.model.client import LLMClient


# ──────────────────────────────────────────────────────────────────────────
# Result records (frozen public API)
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class RoundResult:
    """Legacy per-round record (kept for old checkpoints / logging_viz)."""

    round_index: int
    selected_node_ids: list[str] = field(default_factory=list)
    branches: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    global_best_score: float = 0.0


@dataclass
class RunResult:
    """Outcome of a full CSS run.

    Under the tree-search mechanism ``rounds`` holds one plain dict per tree
    decision (see ``css.tree_search`` decision records); the legacy
    :class:`RoundResult` shape is still accepted by consumers that duck-type.
    """

    tree: "SearchTree"
    archive: "NegativeArchive"
    rounds: list = field(default_factory=list)
    terminated_reason: str = ""
    best_node_id: str | None = None


# ──────────────────────────────────────────────────────────────────────────
# Node helpers shared with css.tree_search
# ──────────────────────────────────────────────────────────────────────────
def _save_skill_snapshot(out_dir: str, node: "TreeNode", round_index: int) -> None:
    """Persist strategy.md + rules.md for a node at a given decision/round."""
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


def _run_val_subset(env, cfg: "CSSConfig") -> list[dict]:
    """The RUN-FIXED val subset used for every node's baseline + L0 gate + val_score.

    Same tasks for ALL nodes (deterministic, seeded by ``cfg.seed``) so node
    val_scores stay directly comparable for SELECT, and so the gate's
    best-step predictions can be reused by the val refresh. ``0`` or a knob
    ``>= len(val)`` selects the whole val set.
    """
    from css.data.task_ledger import uniform_subset
    return uniform_subset(
        list(env.val_items()), getattr(cfg, "exploitation_val_size", 0), seed=cfg.seed
    )


def _val_skill_text(node: "TreeNode") -> str:
    """Skill text for validation evals: the node's BEST rules if recorded.

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
# Entry point
# ──────────────────────────────────────────────────────────────────────────
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
    """Full CSS run under the tree-search mechanism (production wiring).

    Wires the l1gen spawner (NEW/REFINE generation) and the materials pass
    (per-trajectory interpretation -> living dossiers) into
    :func:`css.tree_search.run_css_tree`.

    ``max_rounds`` is accepted for launcher compatibility but the tree loop is
    budgeted by ``cfg.max_decisions`` (a decision = one burst or one spawn);
    a mismatch is logged rather than guessed at.
    """
    from css.l1gen.spawner import make_spawner
    from css.materials.pass_runner import run_materials_pass
    from css.tree_search import run_css_tree

    if max_rounds != cfg.max_decisions:
        _log.info("run_css: max_rounds=%d is legacy-informational; the tree "
                  "budget is cfg.max_decisions=%d", max_rounds, cfg.max_decisions)

    return run_css_tree(
        env,
        target_client,
        optimizer_client,
        cfg=cfg,
        out_dir=out_dir,
        resume=resume,
        spawner=make_spawner(),
        materials_fn=run_materials_pass,
    )
