"""L1 generation subsystem — NEW + REFINE pipelines and the production spawner.

Plugs into the tree-search loop's ``SpawnerFn`` contract (``css.tree_search``):
``run_css_tree(spawner=make_spawner())``. The generation pipelines (design §4)
turn a SATURATED node's spawn decision into a child ``TreeNode`` (or an honest
decline), persisting every step under ``nodes/<child>/gen/`` for step-granular
resume and human audit.
"""
from __future__ import annotations

from css.l1gen.spawner import make_spawner

__all__ = ["make_spawner"]
