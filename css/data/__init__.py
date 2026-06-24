"""CSS core data structures (Phase 1).

All structures are plain ``@dataclass`` objects with ``from_dict`` / ``to_dict``
round-trip support for JSON persistence (metadata.json, negative_archive.json,
tree snapshots).
"""
from __future__ import annotations

from css.data.edit import EDIT_OPS, Edit, EditReport, Patch, RawPatch
from css.data.negative_archive import NegativeArchive, NegativeArchiveEntry
from css.data.pattern import (
    Observation,
    OccurrencePoint,
    PatternLibrary,
    PatternRecord,
)
from css.data.rollout import (
    TaskResult,
    TaskRolloutGroup,
    aggregate_scores,
    group_rollouts,
)
from css.data.step_buffer import StepBuffer, StepBufferEntry
from css.data.tree import LearningCurvePoint, SearchTree, TreeNode

__all__ = [
    # edit
    "Edit", "Patch", "EditReport", "RawPatch", "EDIT_OPS",
    # rollout
    "TaskResult", "TaskRolloutGroup", "group_rollouts", "aggregate_scores",
    # step buffer
    "StepBuffer", "StepBufferEntry",
    # pattern
    "Observation", "OccurrencePoint", "PatternRecord", "PatternLibrary",
    # negative archive
    "NegativeArchive", "NegativeArchiveEntry",
    # tree
    "TreeNode", "SearchTree", "LearningCurvePoint",
]
