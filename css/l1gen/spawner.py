"""Production spawner: dispatch a tree-search spawn to NEW or REFINE generation.

:func:`make_spawner` returns the ``SpawnerFn`` the tree-search loop injects via
``run_css_tree(spawner=...)``. It routes ``ctx.mode`` ("NEW" for a root parent,
"REFINE" for a strategy parent) to the matching pipeline and contains every
exception: a pipeline crash becomes ``SpawnOutcome(child=None, decline=False,
reason=<trace summary>)`` so a bad spawn consumes one decision but never kills the
run (the parent stays saturated and is retryable).
"""
from __future__ import annotations

import logging
import traceback

from css.tree_search import SpawnContext, SpawnerFn, SpawnOutcome

_log = logging.getLogger("css.l1gen")


def make_spawner() -> "SpawnerFn":
    def spawn(ctx: SpawnContext) -> SpawnOutcome:
        mode = (ctx.mode or "").upper()
        try:
            if mode == "NEW":
                from css.l1gen.new_pipeline import run_new_pipeline
                return run_new_pipeline(ctx)
            if mode == "REFINE":
                from css.l1gen.refine_pipeline import run_refine_pipeline
                return run_refine_pipeline(ctx)
            if mode == "MERGE":
                from css.l1gen.merge_pipeline import run_merge_pipeline
                return run_merge_pipeline(ctx)
            return SpawnOutcome(child=None, mode=ctx.mode, decline=False,
                                reason="unknown spawn mode %r" % ctx.mode)
        except Exception as exc:  # noqa: BLE001 — a spawn crash must not kill the run
            tb = traceback.format_exc(limit=6)
            _log.exception("spawn pipeline crashed (mode=%s parent=%s child=%s)",
                           mode, getattr(ctx.parent, "node_id", "?"), ctx.new_node_id)
            summary = "%s: %s" % (type(exc).__name__, str(exc)[:200])
            return SpawnOutcome(child=None, mode=ctx.mode, decline=False,
                                reason="spawn pipeline crashed — " + summary + "\n" + tb[-500:])
    return spawn
