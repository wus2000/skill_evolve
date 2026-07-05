"""Exploration subsystem (probe-based free exploration; L1_tree_mechanism_design §3).

The public contract for the generation package is :func:`get_or_explore` (cache or
run a director session -> screened findings markdown) and :func:`mark_stale`.
"""
from __future__ import annotations

from css.explore.api import get_or_explore, mark_stale

__all__ = ["get_or_explore", "mark_stale"]
