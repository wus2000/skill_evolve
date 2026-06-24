"""Contrastive material extraction for Layer 1 cognitive-difference analysis.

CSS runs K rollouts per task (default K=3). When the same task yields both a
success and a failure under the *same* skill document, the pair isolates the
cognitive difference that mattered -- the task, instruction, and skill are held
constant, so only the agent's run-time decisions diverge. These same-task
(success, failure) pairs are the richest signal for the Phase 4 contrastive
analyst (design D5 / Phase 2.5).

This module supplies the rollout-side plumbing:

  * :func:`extract_contrastive_pairs` -- flatten all same-task (success, failure)
    pairs across rollout groups.
  * :func:`persistent_fail_tasks` -- the tasks where *no* rollout passed; these
    are not contrastable (no success to compare against) and are routed to a
    different analysis path (persistent-failure diagnosis).
  * :func:`format_contrastive_pair` -- render one (success, failure) pair as a
    side-by-side transcript that the analyst LLM reads.
  * :class:`ContrastiveDivergence` -- the *output* schema of the (Phase 4)
    contrastive analyst, defined here for Phase 2 so callers have a stable type.

NOTE: :class:`ContrastiveDivergence` is the contrastive analyst's output record.
Per the design it may later move beside ``Observation`` in
``css/data/pattern.py``; it lives here for Phase 2 to keep the analysis-output
schema co-located with the contrastive plumbing that feeds it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from css.data.rollout import TaskResult, TaskRolloutGroup
from css.trajectory import format_trajectory


@dataclass
class ContrastiveDivergence:
    """One analyst finding from comparing a success vs a failure rollout.

    Output schema of the Phase 4 contrastive analyst. ``divergence_point``
    locates *where* the two trajectories first diverged in a way that mattered;
    ``cognitive_difference`` describes the difference in reasoning/strategy that
    explains the divergence; ``is_systematic`` flags whether the analyst judged
    the difference to be a recurring, generalizable cognitive pattern (worth a
    skill edit) rather than a one-off.
    """

    task_id: str
    success_rollout_index: int
    failure_rollout_index: int
    divergence_point: str
    cognitive_difference: str
    is_systematic: bool

    @classmethod
    def from_dict(cls, d: dict) -> "ContrastiveDivergence":
        return cls(
            task_id=str(d.get("task_id", "")),
            success_rollout_index=int(d.get("success_rollout_index", 0)),
            failure_rollout_index=int(d.get("failure_rollout_index", 0)),
            divergence_point=str(d.get("divergence_point", "")),
            cognitive_difference=str(d.get("cognitive_difference", "")),
            is_systematic=bool(d.get("is_systematic", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "success_rollout_index": self.success_rollout_index,
            "failure_rollout_index": self.failure_rollout_index,
            "divergence_point": self.divergence_point,
            "cognitive_difference": self.cognitive_difference,
            "is_systematic": self.is_systematic,
        }


def extract_contrastive_pairs(
    groups: list[TaskRolloutGroup],
) -> list[tuple[TaskResult, TaskResult]]:
    """Flatten all same-task (success, failure) pairs across rollout groups.

    Each group contributes ``len(successes) * len(failures)`` pairs (see
    :meth:`TaskRolloutGroup.contrastive_pairs`). Group order is preserved.
    """
    pairs: list[tuple[TaskResult, TaskResult]] = []
    for group in groups:
        pairs.extend(group.contrastive_pairs())
    return pairs


def persistent_fail_tasks(
    groups: list[TaskRolloutGroup],
) -> list[TaskRolloutGroup]:
    """Return groups where no rollout passed (see ``is_persistent_fail``).

    These tasks have no success to contrast against and are handled by the
    persistent-failure diagnosis path rather than contrastive analysis. Group
    order is preserved.
    """
    return [g for g in groups if g.is_persistent_fail()]


def format_contrastive_pair(
    success: TaskResult,
    failure: TaskResult,
    *,
    tool_trunc: int = 8000,
) -> str:
    """Render a (success, failure) pair as a side-by-side analyst transcript.

    Both trajectories are rendered with :func:`css.trajectory.format_trajectory`
    (applying the D7-faithful tool-result elision) under ``## SUCCESS`` and
    ``## FAILURE`` headers tagged with their rollout indices. The framing is
    optimized for cognitive-difference analysis: same task, same skill, opposite
    outcome -- so the reader compares the two runs' decisions directly.
    """
    success_body = format_trajectory(success.messages, tool_trunc=tool_trunc)
    failure_body = format_trajectory(failure.messages, tool_trunc=tool_trunc)

    header = f"# Contrastive pair for task {success.task_id}"
    note = (
        "Same task and same skill document; the SUCCESS rollout passed and the "
        "FAILURE rollout did not. Compare the two trajectories to locate where "
        "they diverged and which cognitive difference explains the outcome."
    )
    fail_reason = (
        f"\n\nFailure reason: {failure.fail_reason}" if failure.fail_reason else ""
    )

    return (
        f"{header}\n{note}\n\n"
        f"## SUCCESS (rollout {success.rollout_index})\n"
        f"{success_body}\n\n"
        f"## FAILURE (rollout {failure.rollout_index})"
        f"{fail_reason}\n"
        f"{failure_body}"
    )
