"""SELECT: UCB1-variant node selection over the search tree.

Tree-search mechanism (L1_tree_mechanism_design.md §1, revised by
docs/L1_actions_redesign.md §2): selection is a FLAT argmax over the
selectable pool (ACTIVE + SATURATED nodes) — the tree organizes lineage
(evidence inheritance, spawn semantics), it is not a hierarchical bandit; at
depth <= a few, root-to-leaf descent buys nothing.

The score combines exploitation (current validation height — NOT a lifetime
average, which would systematically undervalue rising-value nodes) and a UCB1
exploration bonus that decays as the node accrues SELECTIONS relative to the
global selection count:

    score = val_score + beta * sqrt(ln(max(T, 2)) / n_selections)

where ``T`` is the total selections charged across the tree and
``n_selections`` the node's own count. Every selection is charged — burst,
successful spawn, failed spawn alike: each consumed one decision
(AW post-mortem 2026-07-07: the root sat at n_bursts=3 across 8 selections
while its children's bursts raised the burst-based T, INFLATING the root's
own bonus; monopoly followed).

The former ``alpha * accept_slope(W)`` trend term is DELETED (redesign §2):
the behavior it rewarded ("still improving") is already expressed by val
rising between selections, the behavior it punished ("stalled") is owned by
the saturation state machine, and in practice it scored a newborn's
inevitable early-accept-then-settle pattern as the maximum negative trend
while scoring basin-top churn accepts positive — the same signal
``node_stalled`` rules meaningless.

Spawn + first-burst is atomic, so every node in the pool has
``n_selections >= 1`` — the legacy ``n == 0 -> +inf`` branch is GONE (it was
the chain-degeneration mechanism: fresh PROPOSAL nodes were unconditionally
selected). A saturated node's score prices its next CHILD (val stays as the
basin-quality prior; the bonus thickens only while it is NOT being charged).
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
    total_selections: int,
    beta: float,
) -> float:
    """UCB1-variant score for one node (selection units).

    ``val_score + beta*sqrt(ln(max(T,2))/n_selections)``. ``n_selections``
    is clamped to >= 1: the pool contract guarantees it, and the clamp keeps
    a malformed checkpoint from resurrecting the inf-UCB pathology.
    """
    n_i = max(1, node.n_selections)
    exploration = beta * math.sqrt(math.log(max(total_selections, 2)) / n_i)
    return node.val_score + exploration


def total_bursts(tree: "SearchTree") -> int:
    """Global bursts landed across all nodes.

    NOT the UCB ``T`` anymore — bursts remain the EVIDENCE clock: the
    spawn-failure cooldown unlocks when a burst lands somewhere (new
    materials), not when another decision merely gets burned.
    """
    return sum(n.n_bursts for n in tree.nodes.values())


def total_selections(tree: "SearchTree") -> int:
    """Global selections charged across all nodes (the T in the bonus)."""
    return sum(n.n_selections for n in tree.nodes.values())


def select_node(tree: "SearchTree", *, cfg: "CSSConfig") -> "TreeNode | None":
    """Argmax UCB1 over the selectable pool; ``None`` if the pool is empty."""
    pool = tree.selectable_nodes()
    if not pool:
        return None
    total = total_selections(tree)
    return max(
        pool,
        key=lambda n: ucb1_score(n, total_selections=total, beta=cfg.beta),
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
    total = total_selections(tree)
    limit = k if k is not None else cfg.concurrency_limit
    kk = min(len(pool), limit)
    if kk <= 0:
        return []
    ranked = sorted(
        pool,
        key=lambda n: ucb1_score(n, total_selections=total, beta=cfg.beta),
        reverse=True,
    )
    return ranked[:kk]
