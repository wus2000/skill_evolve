"""Skill document I/O — the physically separated L0/L1 files of one node.

Layout (design §2.1):

    <skill_dir>/
      strategy.md    ← L1 cognitive strategy (HOW to think)
      rules.md       ← L0 tactical rules (WHAT to do)
      metadata.json  ← node learning curve + pattern library (per-node)

Physical separation is the load-bearing mechanism that makes the L0/L1 boundary
*code-enforced* rather than prompt-suggested (design First Law): the EXPLOITATION
optimizer is only ever handed ``rules.md`` as an edit target, so it structurally
cannot mutate the strategy. The negative archive is tree-global and lives at the
tree root, not in a node's metadata.json (see ``css.data.negative_archive``).

This class is the single read/write authority for those files plus the derived
views each consumer needs:
  * ``combined_skill_text()`` — what the frozen task agent sees (read-only).
  * ``strategy`` / ``rules`` — raw bodies for the optimizers.
  * ``subsections()`` — parsed ``###`` units for REFINE.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from css.data.pattern import PatternLibrary
from css.data.tree import LearningCurvePoint
from css.markdown_utils import Subsection, check_refine_diff, parse_subsections, split_strategy

if TYPE_CHECKING:  # avoid runtime coupling; tree.py does not import this module
    from css.data.tree import TreeNode

STRATEGY_FILE = "strategy.md"
RULES_FILE = "rules.md"
METADATA_FILE = "metadata.json"


@dataclass
class SkillDocumentMetadata:
    """Per-node metadata persisted alongside the markdown files."""

    node_id: str = ""
    learning_curve: list[LearningCurvePoint] = field(default_factory=list)
    pattern_library: PatternLibrary = field(default_factory=PatternLibrary)
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "SkillDocumentMetadata":
        return cls(
            node_id=str(d.get("node_id", "")),
            learning_curve=[LearningCurvePoint.from_dict(p) for p in d.get("learning_curve", [])],
            pattern_library=PatternLibrary.from_dict(d.get("pattern_library", {})),
            extra=dict(d.get("extra", {})),
        )

    def to_dict(self, include_embeddings: bool = False) -> dict:
        return {
            "node_id": self.node_id,
            "learning_curve": [p.to_dict() for p in self.learning_curve],
            "pattern_library": self.pattern_library.to_dict(include_embeddings),
            "extra": self.extra,
        }


class SkillDocument:
    """Read/write manager for one node's strategy.md + rules.md + metadata.json."""

    def __init__(self, skill_dir: str, strategy: str = "", rules: str = "",
                 metadata: SkillDocumentMetadata | None = None) -> None:
        self.skill_dir = skill_dir
        self.strategy = strategy
        self.rules = rules
        self.metadata = metadata or SkillDocumentMetadata()

    # ── Paths ────────────────────────────────────────────────────────────
    @property
    def strategy_path(self) -> str:
        return os.path.join(self.skill_dir, STRATEGY_FILE)

    @property
    def rules_path(self) -> str:
        return os.path.join(self.skill_dir, RULES_FILE)

    @property
    def metadata_path(self) -> str:
        return os.path.join(self.skill_dir, METADATA_FILE)

    # ── I/O ──────────────────────────────────────────────────────────────
    @classmethod
    def load(cls, skill_dir: str) -> "SkillDocument":
        """Load a skill document from disk (missing files → empty content)."""
        strategy = _read_text(os.path.join(skill_dir, STRATEGY_FILE))
        rules = _read_text(os.path.join(skill_dir, RULES_FILE))
        meta_path = os.path.join(skill_dir, METADATA_FILE)
        if os.path.exists(meta_path):
            with open(meta_path, encoding="utf-8") as f:
                metadata = SkillDocumentMetadata.from_dict(json.load(f))
        else:
            metadata = SkillDocumentMetadata()
        return cls(skill_dir=skill_dir, strategy=strategy, rules=rules, metadata=metadata)

    def save(self, include_embeddings: bool = False) -> None:
        """Write strategy.md, rules.md, metadata.json to ``skill_dir``."""
        os.makedirs(self.skill_dir, exist_ok=True)
        _write_text(self.strategy_path, self.strategy)
        _write_text(self.rules_path, self.rules)
        with open(self.metadata_path, "w", encoding="utf-8") as f:
            json.dump(self.metadata.to_dict(include_embeddings), f, ensure_ascii=False, indent=2)

    # ── Node synchronization ─────────────────────────────────────────────
    def update_from_node(self, node: "TreeNode") -> None:
        """Flush a tree node's canonical in-memory state into this document.

        The :class:`~css.data.tree.TreeNode` is the canonical owner of strategy
        text, rules text, learning curve, and pattern library during a run; this
        document is its on-disk projection. The orchestrator (Phase 6) calls this
        before :meth:`save` so ``metadata.json`` never diverges from tree state.
        """
        self.strategy = node.strategy
        self.rules = node.rules
        self.metadata.node_id = node.node_id
        self.metadata.learning_curve = list(node.learning_curve)
        self.metadata.pattern_library = node.pattern_records

    # ── Strategy structure (for REFINE) ──────────────────────────────────
    def subsections(self) -> list[Subsection]:
        return parse_subsections(self.strategy)

    def has_subsections(self) -> bool:
        """True if strategy.md has at least one ``###`` subsection.

        Cold-start strategies that lack ``###`` structure cannot be REFINEd
        (the diff gate would always report ``no_subsection_changed``); the
        orchestrator should treat a False here as "REFINE unavailable".
        """
        return len(self.subsections()) > 0

    def strategy_parts(self) -> tuple[str, str, list[Subsection]]:
        """Return (name, preamble, subsections) of the strategy document."""
        return split_strategy(self.strategy)

    def refine_diff_ok(self, candidate_strategy: str, max_changed: int = 2) -> tuple[bool, str]:
        """Run the REFINE code gate of ``candidate_strategy`` against this doc.

        Delegates to :func:`css.markdown_utils.check_refine_diff`. The candidate
        must change 1..``max_changed`` ``###`` subsections and leave the rest
        byte-identical, else it is rejected / escalated to PROPOSAL.
        """
        return check_refine_diff(self.strategy, candidate_strategy, max_changed=max_changed)

    # ── Consumer views ───────────────────────────────────────────────────
    def combined_skill_text(self) -> str:
        """The read-only text injected into the frozen task agent.

        Strategy first (HOW to think), then rules (WHAT to do). Empty sections
        are omitted so a cold-start (empty rules) document is still clean.
        """
        parts: list[str] = []
        if self.strategy.strip():
            parts.append("# Cognitive Strategy\n\n" + self.strategy.strip())
        if self.rules.strip():
            parts.append("# Tactical Rules\n\n" + self.rules.strip())
        return "\n\n".join(parts)

    def __repr__(self) -> str:
        return (
            f"SkillDocument(dir={self.skill_dir!r}, "
            f"strategy_chars={len(self.strategy)}, rules_chars={len(self.rules)}, "
            f"subsections={len(self.subsections())})"
        )


# ── Helpers ────────────────────────────────────────────────────────────────
def _read_text(path: str) -> str:
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as f:
        return f.read()


def _write_text(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
