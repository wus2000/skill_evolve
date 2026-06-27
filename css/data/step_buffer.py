"""Step buffer — the L0 optimization history of a single tree node.

Each L0 EXPLOITATION step appends one :class:`StepBufferEntry` recording what
was tried and whether the gate accepted it. The buffer is the node's
self-feedback memory and serves three roles:

  1. Saturation detection: ``N`` consecutive rejects ⇒ L0 saturated (design D7).
  2. SELECT signal: ``accept_slope`` over the recent ``W`` steps feeds the UCB1
     variant (design D11).
  3. Optimizer context: prior failure patterns + rejected edits are injected
     into the next step's prompt so the optimizer does not re-propose dead ends.

This mirrors the role of SkillOpt's per-step buffer (``trainer.py`` 441-480) but
is a first-class, serializable structure here because it is persisted per node
and read by the tree manager.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from css.data.edit import Edit

# Gate outcome vocabulary (mirrors SkillOpt evaluation/gate.py GateAction).
StepAction = str  # "accept_new_best" | "accept" | "reject"

ACCEPT_ACTIONS: frozenset[str] = frozenset({"accept", "accept_new_best", "epoch_reset"})


@dataclass
class StepBufferEntry:
    """One L0 optimization step's record."""

    step: int
    action: StepAction                 # accept_new_best | accept | reject
    score_before: float
    score_after: float

    # Edits that were applied this step (the candidate patch's edits).
    applied_edits: list[Edit] = field(default_factory=list)
    # Edits proposed-and-rejected this step (candidate lost the gate), so the
    # next step's optimizer prompt can avoid re-trying them.
    rejected_edits: list[Edit] = field(default_factory=list)
    # Short LLM-named failure patterns observed in this step's trajectories.
    failure_patterns: list[str] = field(default_factory=list)

    epoch: int = -1
    reasoning: str = ""

    @property
    def accepted(self) -> bool:
        return self.action in ACCEPT_ACTIONS

    @property
    def score_delta(self) -> float:
        return self.score_after - self.score_before

    @classmethod
    def from_dict(cls, d: dict) -> "StepBufferEntry":
        return cls(
            step=int(d.get("step", 0)),
            action=str(d.get("action", "reject")),
            score_before=float(d.get("score_before", 0.0)),
            score_after=float(d.get("score_after", 0.0)),
            applied_edits=[Edit.from_dict(e) for e in d.get("applied_edits", [])],
            rejected_edits=[Edit.from_dict(e) for e in d.get("rejected_edits", [])],
            failure_patterns=list(d.get("failure_patterns", [])),
            epoch=int(d.get("epoch", -1)),
            reasoning=str(d.get("reasoning", "")),
        )

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "step": self.step,
            "action": self.action,
            "score_before": self.score_before,
            "score_after": self.score_after,
            "epoch": self.epoch,
        }
        if self.applied_edits:
            d["applied_edits"] = [e.to_dict() for e in self.applied_edits]
        if self.rejected_edits:
            d["rejected_edits"] = [e.to_dict() for e in self.rejected_edits]
        if self.failure_patterns:
            d["failure_patterns"] = self.failure_patterns
        if self.reasoning:
            d["reasoning"] = self.reasoning
        return d


@dataclass
class StepBuffer:
    """Ordered L0 step history with the analytics the tree manager needs."""

    entries: list[StepBufferEntry] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.entries)

    def append(self, entry: StepBufferEntry) -> None:
        self.entries.append(entry)

    @property
    def n_steps(self) -> int:
        return len(self.entries)

    def consecutive_rejects(self) -> int:
        """Number of rejects at the tail (resets on any accept)."""
        count = 0
        for e in reversed(self.entries):
            if e.accepted:
                break
            count += 1
        return count

    def is_saturated(self, n_threshold: int) -> bool:
        """Saturated when the last ``n_threshold`` steps were all rejects.

        ``n_threshold < 1`` is meaningless (would report an empty buffer as
        saturated); treated as never-saturated.
        """
        if n_threshold < 1:
            return False
        return self.consecutive_rejects() >= n_threshold

    def reset_saturation(self) -> None:
        """Break the consecutive-reject streak so exploitation can resume.

        Inserts a synthetic ``accept`` sentinel so ``consecutive_rejects``
        resets to 0 while preserving all prior history for prompt context.
        Called at the start of each new epoch/round to prevent cross-epoch
        saturation deadlock.
        """
        if not self.entries or self.consecutive_rejects() == 0:
            return
        self.entries.append(StepBufferEntry(
            step=-1,
            action="epoch_reset",
            score_before=self.entries[-1].score_after,
            score_after=self.entries[-1].score_after,
            epoch=self.entries[-1].epoch,
        ))

    def recent(self, window: int) -> list[StepBufferEntry]:
        return self.entries[-window:] if window > 0 else list(self.entries)

    def accept_rate(self, window: int | None = None) -> float:
        """Fraction of accepted steps (optionally over the last ``window``).

        ``window=None`` ⇒ all entries. ``window=0`` is treated as "all entries"
        too (consistent with :meth:`recent`), but the ``is not None`` guard makes
        the intent explicit rather than relying on ``0`` being falsy.
        """
        entries = self.recent(window) if window is not None else self.entries
        if not entries:
            return 0.0
        return sum(1 for e in entries if e.accepted) / len(entries)

    def accept_slope(self, window: int) -> float:
        """Linear-regression slope of the accept indicator over recent steps.

        Returns the least-squares slope of ``accepted ∈ {0,1}`` against step
        index across the last ``window`` entries. Positive ⇒ still learning,
        ~0 ⇒ plateaued, negative ⇒ degrading. Used by SELECT (design D11).

        Fewer than 2 points ⇒ slope 0.0 (no trend information).
        """
        entries = self.recent(window)
        n = len(entries)
        if n < 2:
            return 0.0
        xs = list(range(n))
        ys = [1.0 if e.accepted else 0.0 for e in entries]
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        denom = sum((x - mean_x) ** 2 for x in xs)
        if denom == 0:
            return 0.0
        num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
        return num / denom

    def recent_failure_patterns(self, window: int) -> list[str]:
        """De-duplicated failure patterns from the last ``window`` steps."""
        seen: dict[str, None] = {}
        for e in self.recent(window):
            for p in e.failure_patterns:
                seen.setdefault(p, None)
        return list(seen)

    def recent_rejected_edits(self, window: int) -> list[Edit]:
        """Rejected edits from the last ``window`` steps (for prompt injection)."""
        out: list[Edit] = []
        for e in self.recent(window):
            out.extend(e.rejected_edits)
        return out

    @classmethod
    def from_dict(cls, d: dict) -> "StepBuffer":
        return cls(entries=[StepBufferEntry.from_dict(e) for e in d.get("entries", [])])

    def to_dict(self) -> dict:
        return {"entries": [e.to_dict() for e in self.entries]}
