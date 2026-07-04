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
# Valid values: "accept_new_best" | "accept" | "reject" | "reject_no_survivor"
StepAction = str

ACCEPT_ACTIONS: frozenset[str] = frozenset({"accept", "accept_new_best", "epoch_reset"})


@dataclass
class EditVerification:
    """Per-edit ablation verification result.

    Records the outcome of testing one MergedEdit against its target tasks
    using the binary solvability criterion. Full content is always preserved
    (never truncated) for checkpoint/resume and history injection.
    """

    section_target: str
    delta_type: str
    content: str                     # Full section content, NEVER truncated
    target_tasks: list[str] = field(default_factory=list)
    passed: bool = False
    task_results: dict = field(default_factory=dict)
    # task_results schema: {task_id: {"inc_pr": float, "cand_pr": float,
    #   "inc_solvable": bool, "cand_solvable": bool,
    #   "status": "GAINED"|"LOST"|"RETAINED"|"STILL_UNSOLVED"}}
    rationale: str = ""
    derivation: str = ""
    # Outcome category (see compute_verdict). "" on entries written before
    # the verdict system existed — consumers call compute_verdict() then.
    verdict: str = ""
    # Placement/anchor carried through verification so the surviving edit
    # rebuilt for the collective apply keeps its position information
    # (empty on entries written before these fields existed).
    after_section: str = ""
    point_anchor: str = ""

    def gained_tasks(self) -> list[str]:
        return [t for t, r in self.task_results.items()
                if isinstance(r, dict) and r.get("status") == "GAINED"]

    def lost_tasks(self) -> list[str]:
        return [t for t, r in self.task_results.items()
                if isinstance(r, dict) and r.get("status") == "LOST"]

    def compute_verdict(self) -> str:
        """Categorize this verification outcome (derivable from task_results).

        - clean_gain    : G>0, L=0 — direction confirmed
        - mixed_gain    : G>L>0 — passed with known collateral damage
        - mixed_loss    : 0<G<=L — REAL effect but net harmful (refine, don't drop)
        - clean_loss    : G=0, L>0 — direction harmful
        - marginal_gain : G=L=0, passed via continuous tiebreaker
        - null          : G=L=0, failed — no measurable effect
        """
        if self.verdict:
            return self.verdict
        g = len(self.gained_tasks())
        l = len(self.lost_tasks())
        if g > 0 and l == 0:
            return "clean_gain"
        if g > 0 and l > 0:
            return "mixed_gain" if g > l else "mixed_loss"
        if l > 0:
            return "clean_loss"
        return "marginal_gain" if self.passed else "null"

    def to_dict(self) -> dict:
        d = {
            "section_target": self.section_target,
            "delta_type": self.delta_type,
            "content": self.content,
            "target_tasks": list(self.target_tasks),
            "passed": self.passed,
            "task_results": dict(self.task_results),
            "rationale": self.rationale,
            "derivation": self.derivation,
        }
        if self.verdict:
            d["verdict"] = self.verdict
        if self.after_section:
            d["after_section"] = self.after_section
        if self.point_anchor:
            d["point_anchor"] = self.point_anchor
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "EditVerification":
        raw_tasks = d.get("target_tasks", [])
        if not isinstance(raw_tasks, list):
            raw_tasks = []
        raw_results = d.get("task_results", {})
        if not isinstance(raw_results, dict):
            raw_results = {}
        return cls(
            section_target=str(d.get("section_target", "")),
            delta_type=str(d.get("delta_type", "")),
            content=str(d.get("content", "")),
            target_tasks=[str(t) for t in raw_tasks if t],
            passed=bool(d.get("passed", False)),
            task_results=raw_results,
            rationale=str(d.get("rationale", "")),
            derivation=str(d.get("derivation", "")),
            verdict=str(d.get("verdict", "")),
            after_section=str(d.get("after_section", "")),
            point_anchor=str(d.get("point_anchor", "")),
        )


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

    # Exploitation V2: per-edit ablation verification results
    edit_verifications: list[EditVerification] = field(default_factory=list)
    # Number of merged edits that passed ablation verification
    n_survived: int = 0

    @property
    def accepted(self) -> bool:
        return self.action in ACCEPT_ACTIONS

    @property
    def score_delta(self) -> float:
        return self.score_after - self.score_before

    @classmethod
    def from_dict(cls, d: dict) -> "StepBufferEntry":
        raw_verifications = d.get("edit_verifications", [])
        if not isinstance(raw_verifications, list):
            raw_verifications = []
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
            edit_verifications=[
                EditVerification.from_dict(ev) for ev in raw_verifications
            ],
            n_survived=int(d.get("n_survived", 0)),
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
        if self.edit_verifications:
            d["edit_verifications"] = [
                ev.to_dict() for ev in self.edit_verifications
            ]
            d["n_survived"] = self.n_survived
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

    def steps_since_new_best(self) -> int:
        """Number of tail entries since the last ``accept_new_best`` step.

        An ``epoch_reset`` sentinel also stops the count: it marks a deliberate
        "give exploitation another chance" boundary, so stall detection starts
        fresh after it, mirroring how the sentinel resets the reject streak.
        """
        count = 0
        for e in reversed(self.entries):
            if e.action in ("accept_new_best", "epoch_reset"):
                break
            count += 1
        return count

    def is_saturated(self, n_threshold: int, stall_threshold: int = 0) -> bool:
        """Saturated when the last ``n_threshold`` steps were all rejects, OR —
        when ``stall_threshold`` > 0 — when ``stall_threshold`` consecutive
        steps have passed without an ``accept_new_best``.

        The stall condition exists because a noise-limited acceptance gate can
        keep accepting ~50% of candidates indefinitely (measured on BIRD: 9/17
        accepts with per-step p-values 0.07-0.99), so a consecutive-reject
        streak alone never fires even after best-score progress has stopped.

        ``n_threshold < 1`` is meaningless (would report an empty buffer as
        saturated); treated as never-saturated for the reject condition.
        """
        if n_threshold >= 1 and self.consecutive_rejects() >= n_threshold:
            return True
        return stall_threshold > 0 and self.steps_since_new_best() >= stall_threshold

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

    def recent_rejected_edits_v2(self, window: int) -> list[Edit]:
        """Extract failed edits from edit_verifications as Edit objects.

        Scans the last ``window`` step buffer entries and converts each
        EditVerification where ``passed=False`` into an Edit (using
        ``rewrite_section`` op) for reflect prompt compatibility.
        """
        out: list[Edit] = []
        for entry in self.recent(window):
            for ev in entry.edit_verifications:
                if not ev.passed:
                    out.append(Edit(
                        op="rewrite_section",
                        content=ev.content,
                        target=ev.section_target,
                        reason=ev.rationale,
                        source_tasks=list(ev.target_tasks),
                    ))
        return out

    @classmethod
    def from_dict(cls, d: dict) -> "StepBuffer":
        return cls(entries=[StepBufferEntry.from_dict(e) for e in d.get("entries", [])])

    def to_dict(self) -> dict:
        return {"entries": [e.to_dict() for e in self.entries]}
