"""PRUNE: paired-bootstrap significance test for sibling dominance (design §5 / D11).

A node is pruned only when ALL hold:
  (a) it has been sufficiently invested in  (n_steps >= cfg.min_steps),
  (b) its L0 search has saturated            (node.is_saturated(cfg.N)),
  (c) its best sibling is *significantly* better on the validation set.

Significance (c) uses a paired bootstrap over per-task pass/fail indicators on
the shared validation set: resample task indices (the SAME indices for node and
sibling each draw), compute ``mean(sibling) - mean(node)`` per resample, and
take the requested CI. The sibling is significantly better iff the CI lower
bound is strictly > 0. The RNG is seeded from ``cfg.seed`` for determinism.
"""
from __future__ import annotations

import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from css.config import CSSConfig
    from css.data.tree import SearchTree, TreeNode


def paired_bootstrap_diff_ci(
    node_passes: list[int],
    sibling_passes: list[int],
    *,
    resamples: int,
    ci: float,
    seed: int,
) -> tuple[float, float]:
    """CI of ``mean(sibling) - mean(node)`` over PAIRED per-task pass indicators.

    The two lists are paired by index (same validation task). Each resample
    draws ``n`` task indices with replacement and applies them to BOTH lists,
    preserving pairing. Returns the ``(lower, upper)`` percentile bounds for the
    requested ``ci`` (e.g. 0.95 -> 2.5th / 97.5th percentiles). Deterministic
    given ``seed``.
    """
    n = len(node_passes)
    if n == 0 or len(sibling_passes) != n:
        return (0.0, 0.0)

    rng = random.Random(seed)
    diffs: list[float] = []
    for _ in range(resamples):
        idx = [rng.randrange(n) for _ in range(n)]
        node_mean = sum(node_passes[i] for i in idx) / n
        sib_mean = sum(sibling_passes[i] for i in idx) / n
        diffs.append(sib_mean - node_mean)

    diffs.sort()
    lower_q = (1.0 - ci) / 2.0
    upper_q = 1.0 - lower_q
    lo = _percentile(diffs, lower_q)
    hi = _percentile(diffs, upper_q)
    return (lo, hi)


def _percentile(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolation percentile at fraction ``q`` over a sorted list."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo_idx = int(pos)
    hi_idx = min(lo_idx + 1, len(sorted_vals) - 1)
    frac = pos - lo_idx
    return sorted_vals[lo_idx] + (sorted_vals[hi_idx] - sorted_vals[lo_idx]) * frac


def should_prune(
    node: "TreeNode",
    best_sibling: "TreeNode | None",
    *,
    cfg: "CSSConfig",
    node_passes: list[int],
    sibling_passes: list[int],
) -> tuple[bool, str]:
    """Decide whether ``node`` should be pruned in favor of ``best_sibling``.

    Returns ``(prune, reason)``. Prunes only when min-investment, saturation,
    and statistical sibling-dominance (paired-bootstrap CI lower bound > 0) all
    hold.
    """
    if node.n_steps < cfg.min_steps:
        return (False, f"n_steps {node.n_steps} < min_steps {cfg.min_steps}")
    if not node.is_saturated(cfg.N):
        return (False, "node not saturated")
    if best_sibling is None:
        return (False, "no sibling to compare")
    if not node_passes or len(node_passes) != len(sibling_passes):
        return (False, "no paired validation passes to compare")

    lo, hi = paired_bootstrap_diff_ci(
        node_passes,
        sibling_passes,
        resamples=cfg.prune_bootstrap_resamples,
        ci=cfg.prune_ci,
        seed=cfg.seed,
    )
    if lo > 0:
        return (
            True,
            f"sibling {best_sibling.node_id} significantly better "
            f"(diff CI [{lo:.4f}, {hi:.4f}] > 0)",
        )
    return (False, f"sibling not significantly better (diff CI [{lo:.4f}, {hi:.4f}])")


def prune_node(tree: "SearchTree", node_id: str) -> None:
    """Mark ``node_id`` as pruned (status='pruned')."""
    node = tree.get(node_id)
    if node is not None:
        node.status = "pruned"
