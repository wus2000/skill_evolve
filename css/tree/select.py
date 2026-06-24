"""SELECT: UCB1-variant node selection over the search tree (design §5 / D11).

The score for a node combines exploitation (validation score), an
accept-rate-trend term (still-improving nodes are favored), and a UCB1
exploration bonus that decays as the node accrues L0 steps relative to the
global step budget:

    ucb1 = val_score + alpha * accept_slope(W) + beta * sqrt(ln(T) / n_i)

where ``T`` is the global total invested L0 steps and ``n_i`` the node's L0
steps. An unvisited node (``n_i == 0``) scores ``+inf`` to force exploration.

``select_node`` is the argmax over active nodes; ``select_batch`` is the
concurrent top-K used by the orchestrator's SELECT_BATCH each round.
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
    total_steps: int,
    alpha: float,
    beta: float,
    window: int,
) -> float:
    """UCB1-variant score for one node.

    ``val_score + alpha*accept_slope(window) + beta*sqrt(ln(max(total_steps,1))/n_i)``.
    An unvisited node (``n_i == 0``) returns ``float('inf')`` to force exploration.
    """
    n_i = node.n_steps
    if n_i <= 0:
        return float("inf")
    exploration = beta * math.sqrt(math.log(max(total_steps, 1)) / n_i)
    return node.val_score + alpha * node.accept_slope(window) + exploration


def _total_steps(tree: "SearchTree") -> int:
    """Global total invested L0 steps across all nodes (design T)."""
    return sum(n.n_steps for n in tree.nodes.values())


def select_node(tree: "SearchTree", *, cfg: "CSSConfig") -> "TreeNode | None":
    """Argmax UCB1 over active nodes; ``None`` if there are none."""
    active = tree.active_nodes()
    if not active:
        return None
    total = _total_steps(tree)
    return max(
        active,
        key=lambda n: ucb1_score(
            n, total_steps=total, alpha=cfg.alpha, beta=cfg.beta, window=cfg.W
        ),
    )


def select_batch(
    tree: "SearchTree", *, cfg: "CSSConfig", k: int | None = None
) -> list["TreeNode"]:
    """Top-K active nodes by UCB1 (descending), K = min(active, k or concurrency_limit)."""
    active = tree.active_nodes()
    if not active:
        return []
    total = _total_steps(tree)
    limit = k if k is not None else cfg.concurrency_limit
    kk = min(len(active), limit)
    if kk <= 0:
        return []
    ranked = sorted(
        active,
        key=lambda n: ucb1_score(
            n, total_steps=total, alpha=cfg.alpha, beta=cfg.beta, window=cfg.W
        ),
        reverse=True,
    )
    return ranked[:kk]
