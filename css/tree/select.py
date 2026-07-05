"""SELECT: UCB1-variant node selection over the search tree.

Tree-search mechanism (L1_tree_mechanism_design.md §1): selection is a FLAT
argmax over the selectable pool (ACTIVE + SATURATED nodes) — the tree organizes
lineage (evidence inheritance, spawn semantics), it is not a hierarchical
bandit; at depth <= a few, root-to-leaf descent buys nothing.

The score combines exploitation (current validation height — NOT a lifetime
average, which would systematically undervalue rising-value nodes), an
accept-rate-trend term (still-improving nodes are favored), and a UCB1
exploration bonus that decays as the node accrues BURSTS relative to the
global burst count:

    score = val_score + alpha * accept_slope(W) + beta * sqrt(ln(T) / n_bursts)

where ``T`` is the total bursts invested across the tree and ``n_bursts`` the
node's own burst count. Spawn + first-burst is atomic, so every node in the
pool has ``n_bursts >= 1`` — the legacy ``n == 0 -> +inf`` branch is GONE (it
was the chain-degeneration mechanism: fresh PROPOSAL nodes were unconditionally
selected). A saturated node's score prices its next CHILD (val stays as the
basin-quality prior; slope ~ 0; the bonus keeps thickening while others burst).
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from css.config import CSSConfig
    from css.data.tree import SearchTree, TreeNode


def ucb1_score(
    node: "TreeNode",
    *,
    total_bursts: int,
    alpha: float,
    beta: float,
    window: int,
) -> float:
    """UCB1-variant score for one node (burst units).

    ``val_score + alpha*accept_slope(window) + beta*sqrt(ln(max(T,2))/n_bursts)``.
    ``n_bursts`` is clamped to >= 1: the pool contract guarantees it, and the
    clamp keeps a malformed checkpoint from resurrecting the inf-UCB pathology.
    """
    n_i = max(1, node.n_bursts)
    exploration = beta * math.sqrt(math.log(max(total_bursts, 2)) / n_i)
    return node.val_score + alpha * node.accept_slope(window) + exploration


def total_bursts(tree: "SearchTree") -> int:
    """Global bursts invested across all nodes (the T in the bonus)."""
    return sum(n.n_bursts for n in tree.nodes.values())


def select_node(tree: "SearchTree", *, cfg: "CSSConfig") -> "TreeNode | None":
    """Argmax UCB1 over the selectable pool; ``None`` if the pool is empty."""
    pool = tree.selectable_nodes()
    if not pool:
        return None
    total = total_bursts(tree)
    return max(
        pool,
        key=lambda n: ucb1_score(
            n, total_bursts=total, alpha=cfg.alpha, beta=cfg.beta, window=cfg.W
        ),
    )


def select_batch(
    tree: "SearchTree", *, cfg: "CSSConfig", k: int | None = None
) -> list["TreeNode"]:
    """Top-K selectable nodes by UCB1 (descending).

    Retained for the (future) parallel-burst regime; the serial loop uses
    :func:`select_node`.
    """
    pool = tree.selectable_nodes()
    if not pool:
        return []
    total = total_bursts(tree)
    limit = k if k is not None else cfg.concurrency_limit
    kk = min(len(pool), limit)
    if kk <= 0:
        return []
    ranked = sorted(
        pool,
        key=lambda n: ucb1_score(
            n, total_bursts=total, alpha=cfg.alpha, beta=cfg.beta, window=cfg.W
        ),
        reverse=True,
    )
    return ranked[:kk]
