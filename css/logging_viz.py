"""Phase 6 — logging & visualization helpers for the CSS search tree.

Pure serialization / formatting over :class:`css.data.tree.SearchTree` and the
orchestrator's run results. No network, no model, no heavy Phase 2/4/5 imports
at module load — the only dependencies are the stdlib ``json`` and ``os``.

Public API (frozen contract):
  * :func:`tree_snapshot` — serializable dict view of the tree.
  * :func:`write_run_artifacts` — dump tree_snapshot.json + rounds.json +
    summary.json under ``out_dir``; return ``{name: path}``.
  * :func:`format_tree` — ascii indented tree for logs.
"""
from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # avoid importing heavy modules at runtime
    from css.data.tree import SearchTree, TreeNode
    from css.orchestrator import RunResult


# ──────────────────────────────────────────────────────────────────────────
# Snapshot
# ──────────────────────────────────────────────────────────────────────────
def _node_snapshot(node: "TreeNode") -> dict[str, Any]:
    """Serializable view of a single node (no skill text, no embeddings)."""
    return {
        "node_id": node.node_id,
        "parent_id": node.parent_id,
        "branch_type": node.branch_type,
        "status": node.status,
        "val_score": node.val_score,
        "best_score": node.best_score,
        "n_steps": node.n_steps,
        "refine_count": node.refine_count,
        "maturity": node.maturity,
        "n_patterns": len(node.pattern_records),
        "learning_curve": [p.to_dict() for p in node.learning_curve],
    }


def tree_snapshot(tree: "SearchTree") -> dict[str, Any]:
    """Build a JSON-serializable snapshot of the search tree.

    Returns a dict with ``"nodes"`` (a list of per-node dicts) and ``"root_id"``.
    Each node dict carries id/parent/branch_type/status, scores, step/refine/
    maturity counters, pattern count, and the full learning curve.
    """
    return {
        "root_id": tree.root_id,
        "nodes": [_node_snapshot(n) for n in tree.nodes.values()],
    }


# ──────────────────────────────────────────────────────────────────────────
# Artifacts
# ──────────────────────────────────────────────────────────────────────────
def _round_to_dict(rnd: Any) -> dict[str, Any]:
    """Serialize a RoundResult (dataclass) without importing its type."""
    return {
        "round_index": getattr(rnd, "round_index", None),
        "selected_node_ids": list(getattr(rnd, "selected_node_ids", []) or []),
        "branches": list(getattr(rnd, "branches", []) or []),
        "pruned": list(getattr(rnd, "pruned", []) or []),
        "global_best_score": getattr(rnd, "global_best_score", None),
    }


def write_run_artifacts(run: "RunResult", out_dir: str) -> dict[str, str]:
    """Write tree_snapshot.json, rounds.json, and summary.json under ``out_dir``.

    Returns a mapping ``{artifact_name: absolute_or_given_path}`` for the three
    files written.
    """
    os.makedirs(out_dir, exist_ok=True)

    snapshot = tree_snapshot(run.tree)
    rounds = [_round_to_dict(r) for r in (run.rounds or [])]
    summary = {
        "terminated_reason": run.terminated_reason,
        "best_node_id": run.best_node_id,
        "n_rounds": len(run.rounds or []),
    }

    paths = {
        "tree_snapshot": os.path.join(out_dir, "tree_snapshot.json"),
        "rounds": os.path.join(out_dir, "rounds.json"),
        "summary": os.path.join(out_dir, "summary.json"),
    }

    with open(paths["tree_snapshot"], "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, indent=2, sort_keys=True)
    with open(paths["rounds"], "w", encoding="utf-8") as fh:
        json.dump(rounds, fh, indent=2, sort_keys=True)
    with open(paths["summary"], "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True)

    return paths


# ──────────────────────────────────────────────────────────────────────────
# ASCII tree
# ──────────────────────────────────────────────────────────────────────────
def _format_line(node: "TreeNode", depth: int) -> str:
    indent = "  " * depth
    return (
        f"{indent}{node.node_id} [{node.branch_type}/{node.status}] "
        f"val={node.val_score:.3f} steps={node.n_steps}"
    )


def format_tree(tree: "SearchTree") -> str:
    """Render the tree as an indented ascii listing rooted at ``tree.root_id``.

    Each line: ``node_id [branch_type/status] val=.. steps=..``. Falls back to
    listing any orphan nodes (no reachable root) so nothing is silently dropped.
    """
    lines: list[str] = []
    seen: set[str] = set()

    def _walk(node_id: str, depth: int) -> None:
        node = tree.nodes.get(node_id)
        if node is None or node_id in seen:
            return
        seen.add(node_id)
        lines.append(_format_line(node, depth))
        for child in tree.children(node_id):
            _walk(child.node_id, depth + 1)

    if tree.root_id is not None and tree.root_id in tree.nodes:
        _walk(tree.root_id, 0)

    # Append any nodes not reachable from the root (defensive; keeps logs honest).
    for node_id in tree.nodes:
        if node_id not in seen:
            _walk(node_id, 0)

    return "\n".join(lines)
