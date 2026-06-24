"""CSS — Cognitive Strategy Search.

A two-level skill-document optimization framework: the outer level searches over
cognitive strategies (HOW to think, ``strategy.md`` / L1) while the inner level
performs SkillOpt-style tactical rule optimization (WHAT to do, ``rules.md`` /
L0) within each strategy, using tree-based branching search.

See ``design_final_en.md`` for the authoritative design and
``training_mechanism_v6.md`` for the negotiated rationale (D0-D16).

Phase 1 (this milestone): configuration, core data structures, and skill
document I/O. Later phases add rollout (2), EXPLOITATION (3), the analysis
pipeline (4), PROPOSAL/REFINE (5), and tree orchestration (6).
"""
from __future__ import annotations

from css.config import CSSConfig
from css.skill_document import SkillDocument, SkillDocumentMetadata

__all__ = ["CSSConfig", "SkillDocument", "SkillDocumentMetadata"]

__version__ = "0.1.0"  # Phase 1
