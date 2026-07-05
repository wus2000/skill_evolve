"""The materials subsystem (L1_tree_mechanism_design.md §2).

Post-burst, reads the burst's on-policy trajectories from disk and turns them
into strategy-behavior evidence: per-trajectory interpretation -> altitude screen
-> mining (behavior-mode groups, adherence ledger) -> living dossiers
(behavior_profile.md / frontier_analysis.md) -> failure-mode A/B/U attribution
-> cross-strategy global-unsolved synthesis.

Public entry points:
  * :func:`run_materials_pass` — the ``materials_fn`` the tree-search loop injects;
  * :func:`screen_items`       — the reusable Altitude Screen (also imported by
    the generation / exploration packages for their own products).
"""
from __future__ import annotations

from css.materials.pass_runner import run_materials_pass
from css.materials.screen import screen_items

__all__ = ["run_materials_pass", "screen_items"]
