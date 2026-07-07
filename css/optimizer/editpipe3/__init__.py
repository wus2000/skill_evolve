"""editpipe v3 — plan/draft/review/apply consolidation (docs/editpipe_v3_design.md)."""
from css.optimizer.editpipe3.docmodel import RulesDocV3
from css.optimizer.editpipe3.pipeline import apply_groups, consolidate

__all__ = ["RulesDocV3", "consolidate", "apply_groups"]
