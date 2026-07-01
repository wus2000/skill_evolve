"""Edit / Patch data model for L0 (rules.md) optimization.

These are the atomic units the L0 optimizer (EXPLOITATION) produces and the
code applies to ``rules.md``. The model is adapted from SkillOpt's
``skillopt/types.py`` (``Edit`` / ``Patch``), but deliberately simplified for
CSS's two-file architecture:

  * SkillOpt edits a single document and protects an epoch-level region with
    ``<!-- SLOW_UPDATE_START/END -->`` markers (see SkillOpt ``optimizer/skill.py``).
  * CSS instead physically separates ``strategy.md`` (L1, read-only at L0) from
    ``rules.md`` (L0, editable). Therefore edits operate only on ``rules.md`` and
    need no marker parsing / protected-region logic.

Phase 1 defines the data types only. The deterministic ``apply`` logic lives in
the Module 2 / Phase 3 edit engine (``css/edit_engine.py``), which consumes these
types.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# The four supported edit operations on rules.md free-form text.
#   append        : add content at the end of rules.md
#   insert_after   : insert content on the line after `target`
#   replace        : replace the first occurrence of `target` with content
#   delete         : remove the first occurrence of `target`
EditOp = Literal[
    "append", "insert_after", "replace", "delete",
    "add_section", "rewrite_section", "delete_section",
]

EDIT_OPS: tuple[str, ...] = (
    "append", "insert_after", "replace", "delete",
    "add_section", "rewrite_section", "delete_section",
)


@dataclass
class Edit:
    """A single edit operation on ``rules.md``.

    Attributes
    ----------
    op:
        One of :data:`EDIT_OPS`. Section-level ops (``add_section``,
        ``rewrite_section``, ``delete_section``) use ``target`` as the ``###``
        section heading to locate, and ``content`` as the full section body
        (heading included for ``add_section``/``rewrite_section``).
    content:
        Text to add (for ``append`` / ``insert_after`` / ``replace``). Ignored
        for ``delete``.
    target:
        Anchor text to locate within ``rules.md`` (required for
        ``insert_after`` / ``replace`` / ``delete``; ignored for ``append``).
    support_count:
        How many minibatches / trajectories independently proposed this edit.
        Populated by the hierarchical aggregation stage (Phase 3.4). ``None``
        before aggregation.
    source_type:
        Whether the edit originated from analysing failure or success
        trajectories. Used for provenance and ranking.
    merge_level:
        Aggregation tree depth at which this edit was merged (Phase 3.4).
    reason:
        Optional free-text rationale the optimizer attached to this edit
        (useful for the step_buffer / debugging).
    """

    op: EditOp
    content: str = ""
    target: str = ""
    support_count: int | None = None
    source_type: Literal["failure", "success"] | None = None
    merge_level: int | None = None
    reason: str = ""
    source_tasks: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "Edit":
        raw_tasks = d.get("source_tasks", [])
        if not isinstance(raw_tasks, list):
            raw_tasks = []
        return cls(
            op=d.get("op", "append"),
            content=d.get("content", ""),
            target=d.get("target", ""),
            support_count=d.get("support_count"),
            source_type=d.get("source_type"),
            merge_level=d.get("merge_level"),
            reason=d.get("reason", ""),
            source_tasks=[str(t) for t in raw_tasks if t],
        )

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"op": self.op, "content": self.content}
        if self.target:
            d["target"] = self.target
        if self.support_count is not None:
            d["support_count"] = self.support_count
        if self.source_type is not None:
            d["source_type"] = self.source_type
        if self.merge_level is not None:
            d["merge_level"] = self.merge_level
        if self.reason:
            d["reason"] = self.reason
        if self.source_tasks:
            d["source_tasks"] = list(self.source_tasks)
        return d


@dataclass
class Patch:
    """An ordered set of edits with the optimizer's reasoning.

    Output of the L0 optimizer step (after hierarchical merge + selection); the
    edit engine applies the edits sequentially to produce a candidate
    ``rules.md``.
    """

    edits: list[Edit] = field(default_factory=list)
    reasoning: str = ""
    ranking_details: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "Patch":
        raw = d.get("edits", [])
        return cls(
            edits=[Edit.from_dict(e) if isinstance(e, dict) else e for e in raw],
            reasoning=d.get("reasoning", ""),
            ranking_details=d.get("ranking_details"),
        )

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "reasoning": self.reasoning,
            "edits": [e.to_dict() if isinstance(e, Edit) else e for e in self.edits],
        }
        if self.ranking_details is not None:
            d["ranking_details"] = self.ranking_details
        return d

    def __len__(self) -> int:
        return len(self.edits)


# Per-edit application status, produced by the edit engine (Phase 3) when a
# patch is applied. Defined here so the data model is self-contained.
EditStatus = Literal["applied", "skipped", "error"]


@dataclass
class EditReport:
    """Observability record for a single applied edit.

    ``status`` is the coarse outcome; ``detail`` carries the specific reason
    (e.g. ``"target_not_found"``). The edit engine returns one report per edit
    in a patch so callers can see exactly what landed.
    """

    index: int
    op: str
    status: EditStatus
    detail: str = ""
    target_preview: str = ""
    content_preview: str = ""
    error: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "EditReport":
        return cls(
            index=int(d.get("index", 0)),
            op=d.get("op", ""),
            status=d.get("status", "error"),
            detail=d.get("detail", ""),
            target_preview=d.get("target_preview", ""),
            content_preview=d.get("content_preview", ""),
            error=d.get("error", ""),
        )

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "index": self.index,
            "op": self.op,
            "status": self.status,
        }
        for attr in ("detail", "target_preview", "content_preview", "error"):
            val = getattr(self, attr)
            if val:
                d[attr] = val
        return d


@dataclass
class RawPatch:
    """Analyst output with provenance — a single minibatch's edit proposals.

    Produced by the L0 Reflect stage (``css/optimizer/reflect.py``): each
    minibatch of trajectories (analysed as *failures* or *successes*
    separately) yields one ``RawPatch``. The hierarchical aggregation stage
    (``css/optimizer/aggregate.py``) then merges many ``RawPatch`` objects into
    a single ``Patch`` whose edits carry ``support_count``.

    Adapted from SkillOpt ``skillopt/types.py:205-244`` (``RawPatch``), but
    ``failure_summary`` is kept as a list of plain dicts rather than a typed
    ``FailureSummaryEntry`` to avoid coupling to SkillOpt's type hierarchy.

    Attributes
    ----------
    patch:
        The parsed edit proposals for this minibatch.
    source_type:
        ``"failure"`` or ``"success"`` — which trajectory class was analysed.
    batch_size:
        Number of trajectories in the analysed minibatch (provenance).
    failure_summary:
        Optional structured summary entries the analyst attached (free-form
        dicts; used for observability / step_buffer failure_patterns).
    """

    patch: Patch
    source_type: str = "failure"
    batch_size: int = 0
    failure_summary: list[dict] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict | None) -> "RawPatch | None":
        if d is None:
            return None
        inner = d.get("patch", d)
        if not isinstance(inner, dict):
            return None
        patch = Patch.from_dict(inner)
        summary = d.get("failure_summary", [])
        if not isinstance(summary, list):
            summary = []
        return cls(
            patch=patch,
            source_type=d.get("source_type", "failure"),
            batch_size=int(d.get("batch_size", 0)),
            failure_summary=[fs for fs in summary if isinstance(fs, dict)],
        )

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "patch": self.patch.to_dict(),
            "source_type": self.source_type,
            "batch_size": self.batch_size,
        }
        if self.failure_summary:
            d["failure_summary"] = list(self.failure_summary)
        return d


@dataclass
class MergedEdit:
    """One edit unit produced by the merger — section-level OR point-level.

    **Section-level** (``delta_type`` in ``new_section``, ``section_rewrite``,
    ``section_refinement``): represents the COMPLETE target state of one ``###``
    section.

    **Point-level** (``delta_type`` in ``point_edit``, ``point_add``,
    ``point_remove``): a minimal, localized change within a section.  Multiple
    point edits may target the same section as long as they modify
    non-overlapping text (independence verified by the edit validator).
    """

    section_target: str          # "### Data Loading" -- the section this edit belongs to
    delta_type: str              # section ops: "new_section" | "section_rewrite" | "section_refinement"
                                 # point ops:   "point_edit" | "point_add" | "point_remove"
    after_section: str = ""      # For new_section: insert after this heading.
    content: str = ""            # Section ops: full section text (### heading + body).
                                 # point_edit/point_add: the new/replacement text.
                                 # point_remove: unused.
    point_anchor: str = ""       # Point ops only — the text to locate in the section.
                                 # point_edit: the text being replaced.
                                 # point_add: insert new content after this text.
                                 # point_remove: the text to delete.
    target_tasks: list[str] = field(default_factory=list)
    rationale: str = ""
    derivation: str = ""

    @property
    def is_point(self) -> bool:
        return self.delta_type in _POINT_DELTA_TYPES

    def to_dict(self) -> dict:
        d = {
            "section_target": self.section_target,
            "delta_type": self.delta_type,
            "content": self.content,
            "target_tasks": list(self.target_tasks),
            "rationale": self.rationale,
            "derivation": self.derivation,
        }
        if self.after_section:
            d["after_section"] = self.after_section
        if self.point_anchor:
            d["point_anchor"] = self.point_anchor
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "MergedEdit":
        raw_tasks = d.get("target_tasks", [])
        if not isinstance(raw_tasks, list):
            raw_tasks = []
        return cls(
            section_target=str(d.get("section_target", "")),
            delta_type=str(d.get("delta_type", "section_rewrite")),
            after_section=str(d.get("after_section", "")),
            content=str(d.get("content", "")),
            point_anchor=str(d.get("point_anchor", "")),
            target_tasks=[str(t) for t in raw_tasks if t],
            rationale=str(d.get("rationale", "")),
            derivation=str(d.get("derivation", "")),
        )


SECTION_DELTA_TYPES = frozenset({"new_section", "section_rewrite", "section_refinement", "delete_section"})
_POINT_DELTA_TYPES = frozenset({"point_edit", "point_add", "point_remove"})
