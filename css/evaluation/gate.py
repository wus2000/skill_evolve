"""Validation gate for L0 EXPLOITATION — pure accept/reject decision (Phase 3).

Analogous to validation-based early stopping / model selection in NN training:
compare a candidate ``rules.md``'s selection-set score against the current and
best scores, and return a decision. The L0 step loop owns all side effects
(rollout, node mutation, step_buffer updates, I/O); this module is the pure
decision function.

Adapted from SkillOpt ``skillopt/evaluation/gate.py:31-73`` — same semantics,
re-typed to operate on ``rules`` text instead of a single skill document. There
is NO I/O and NO mutation here.

Decision rule
-------------
* ``cand_score > current_score`` -> ``accept`` (current becomes candidate).
  Additionally, if ``cand_score > best_score`` -> ``accept_new_best``
  (best becomes candidate, ``best_step`` becomes ``global_step``).
* otherwise -> ``reject`` (state unchanged).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

GateAction = Literal["accept_new_best", "accept", "reject"]


@dataclass(frozen=True)
class GateResult:
    """Immutable outcome of the validation gate."""

    action: GateAction
    current_rules: str
    current_score: float
    best_rules: str
    best_score: float
    best_step: int


def evaluate_gate(
    candidate_rules: str,
    cand_score: float,
    current_rules: str,
    current_score: float,
    best_rules: str,
    best_score: float,
    best_step: int,
    global_step: int,
) -> GateResult:
    """Pure gate decision: compare ``cand_score`` to current / best.

    Returns a :class:`GateResult` carrying the post-decision state. The caller
    decides what to do with it (mutate node, log, persist).
    """
    if cand_score > current_score:
        if cand_score > best_score:
            return GateResult(
                action="accept_new_best",
                current_rules=candidate_rules,
                current_score=cand_score,
                best_rules=candidate_rules,
                best_score=cand_score,
                best_step=global_step,
            )
        return GateResult(
            action="accept",
            current_rules=candidate_rules,
            current_score=cand_score,
            best_rules=best_rules,
            best_score=best_score,
            best_step=best_step,
        )
    return GateResult(
        action="reject",
        current_rules=current_rules,
        current_score=current_score,
        best_rules=best_rules,
        best_score=best_score,
        best_step=best_step,
    )
