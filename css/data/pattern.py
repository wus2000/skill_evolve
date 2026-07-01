"""Behavioral pattern data model (analysis Layers 1-3).

Data flow:
  Layer 1 (per-trajectory)  → :class:`Observation` (LLM names a
                              ``cognitive_aspect`` — now a generalizable
                              behavioral-pattern label; cites trajectory evidence).
  Layer 2 (clustering)       → :class:`PatternRecord` groups observations that
                              describe the same behavioral pattern, optionally
                              paired with a success/failure counterpart.
  Layer 3 (longitudinal)     → :class:`PatternRecord.occurrence_history` tracks
                              the pattern's per-epoch occurrence rate.

This is the structural backbone of the analysis pipeline.  The ``Observation``
structure is intentionally generic: the ``cognitive_aspect`` field holds
whatever the Layer 1 LLM names the pattern (historically a cognitive tendency;
now a behavioral arc pattern label).  The downstream pipeline clusters by this
field and is agnostic to its semantic content.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Polarity = Literal["failure", "success", "neutral"]
Significance = Literal["critical", "notable"]


@dataclass
class Observation:
    """One Layer-1 behavioral observation extracted from a single trajectory.

    The ``cognitive_aspect`` is *named by the LLM* — a generalizable behavioral
    pattern label (e.g. "Extensive data exploration before solution attempt").
    CSS deliberately predefines no dimensions; the taxonomy emerges bottom-up
    in Layer 2 clustering. This field is free text.
    """

    obs_id: str
    task_id: str
    rollout_index: int
    node_id: str
    epoch: int

    what: str                          # what the agent did/thought
    cognitive_aspect: str              # LLM-named aspect (free text, not enum)
    evidence: str                      # quoted trajectory text
    consequence: str                   # what outcome it led to
    significance: Significance = "notable"
    polarity: Polarity = "neutral"

    # Set by Layer 2 once this observation is assigned to a cluster/pattern.
    pattern_id: str = ""
    # Optional cached embedding vector (Layer 2a). Kept out of git-tracked
    # metadata when large; stored as a plain list for JSON round-trip.
    embedding: list[float] | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "Observation":
        return cls(
            obs_id=str(d.get("obs_id", "")),
            task_id=str(d.get("task_id", "")),
            rollout_index=int(d.get("rollout_index", 0)),
            node_id=str(d.get("node_id", "")),
            epoch=int(d.get("epoch", -1)),
            what=str(d.get("what", "")),
            cognitive_aspect=str(d.get("cognitive_aspect", "")),
            evidence=str(d.get("evidence", "")),
            consequence=str(d.get("consequence", "")),
            significance=d.get("significance", "notable"),
            polarity=d.get("polarity", "neutral"),
            pattern_id=str(d.get("pattern_id", "")),
            embedding=d.get("embedding"),
        )

    def to_dict(self, include_embedding: bool = False) -> dict:
        d: dict[str, Any] = {
            "obs_id": self.obs_id,
            "task_id": self.task_id,
            "rollout_index": self.rollout_index,
            "node_id": self.node_id,
            "epoch": self.epoch,
            "what": self.what,
            "cognitive_aspect": self.cognitive_aspect,
            "evidence": self.evidence,
            "consequence": self.consequence,
            "significance": self.significance,
            "polarity": self.polarity,
        }
        if self.pattern_id:
            d["pattern_id"] = self.pattern_id
        if include_embedding and self.embedding is not None:
            d["embedding"] = self.embedding
        return d


@dataclass
class OccurrencePoint:
    """One epoch's occurrence statistics for a pattern (Layer 3)."""

    epoch: int
    occurrence_rate: float             # fraction of tasks exhibiting the pattern
    support_count: int                 # number of observations this epoch
    n_tasks: int = 0                   # tasks considered this epoch (denominator)

    @classmethod
    def from_dict(cls, d: dict) -> "OccurrencePoint":
        return cls(
            epoch=int(d.get("epoch", -1)),
            occurrence_rate=float(d.get("occurrence_rate", 0.0)),
            support_count=int(d.get("support_count", 0)),
            n_tasks=int(d.get("n_tasks", 0)),
        )

    def to_dict(self) -> dict:
        return {
            "epoch": self.epoch,
            "occurrence_rate": self.occurrence_rate,
            "support_count": self.support_count,
            "n_tasks": self.n_tasks,
        }


@dataclass
class PatternRecord:
    """A cross-trajectory cognitive pattern with longitudinal tracking.

    ``pattern_id`` is stable across epochs (assigned at creation, preserved by
    incremental matching). ``counterpart_id`` links a failure pattern to its
    success counterpart (a "dual pattern", design Layer 2c) — the success side
    directly suggests what a new strategy should systematize.
    """

    pattern_id: str
    name: str                          # LLM-unified pattern name
    description: str = ""
    cognitive_aspect: str = ""
    polarity: Polarity = "neutral"

    observations: list[Observation] = field(default_factory=list)
    occurrence_history: list[OccurrencePoint] = field(default_factory=list)

    # Number of distinct L0 remedy attempts that failed to durably suppress
    # this pattern. Together with L0 saturation this is the remedy_resistance
    # evidence for an L1 signal (design Layer 3).
    remedy_resistance: int = 0
    counterpart_id: str = ""           # paired success/failure pattern
    centroid: list[float] | None = None  # cluster centroid embedding (Layer 2)
    status: str = "active"             # active | merged | retired

    @property
    def latest_occurrence(self) -> float:
        return self.occurrence_history[-1].occurrence_rate if self.occurrence_history else 0.0

    @property
    def support_count(self) -> int:
        """Total observations attached to this pattern."""
        return len(self.observations)

    def occurrence_trend(self, window: int) -> float:
        """Least-squares slope of occurrence_rate over the last ``window`` epochs.

        Negative ⇒ pattern is decaying under L0 optimization (an L0 problem);
        flat/positive ⇒ persists despite L0 (an L1 signal candidate).
        """
        pts = self.occurrence_history[-window:] if window > 0 else self.occurrence_history
        n = len(pts)
        if n < 2:
            return 0.0
        xs = list(range(n))
        ys = [p.occurrence_rate for p in pts]
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        denom = sum((x - mean_x) ** 2 for x in xs)
        if denom == 0:
            return 0.0
        num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
        return num / denom

    @classmethod
    def from_dict(cls, d: dict) -> "PatternRecord":
        return cls(
            pattern_id=str(d.get("pattern_id", "")),
            name=str(d.get("name", "")),
            description=str(d.get("description", "")),
            cognitive_aspect=str(d.get("cognitive_aspect", "")),
            polarity=d.get("polarity", "neutral"),
            observations=[Observation.from_dict(o) for o in d.get("observations", [])],
            occurrence_history=[OccurrencePoint.from_dict(p) for p in d.get("occurrence_history", [])],
            remedy_resistance=int(d.get("remedy_resistance", 0)),
            counterpart_id=str(d.get("counterpart_id", "")),
            centroid=d.get("centroid"),
            status=str(d.get("status", "active")),
        )

    def to_dict(self, include_embeddings: bool = False) -> dict:
        d: dict[str, Any] = {
            "pattern_id": self.pattern_id,
            "name": self.name,
            "description": self.description,
            "cognitive_aspect": self.cognitive_aspect,
            "polarity": self.polarity,
            "observations": [o.to_dict(include_embeddings) for o in self.observations],
            "occurrence_history": [p.to_dict() for p in self.occurrence_history],
            "remedy_resistance": self.remedy_resistance,
            "status": self.status,
        }
        if self.counterpart_id:
            d["counterpart_id"] = self.counterpart_id
        if include_embeddings and self.centroid is not None:
            d["centroid"] = self.centroid
        return d


@dataclass
class PatternLibrary:
    """The pattern collection of one tree node, keyed by stable ``pattern_id``.

    Incremental growth (Layer 2 / Layer 3) mutates this in place across epochs.
    Phase 1 provides storage + the deterministic L1-signal predicate; the
    embedding-matching and clustering logic is Phase 4.
    """

    patterns: dict[str, PatternRecord] = field(default_factory=dict)
    _next_id: int = 0

    def __len__(self) -> int:
        return len(self.patterns)

    def __iter__(self):
        return iter(self.patterns.values())

    def get(self, pattern_id: str) -> PatternRecord | None:
        return self.patterns.get(pattern_id)

    def add(self, pattern: PatternRecord) -> None:
        self.patterns[pattern.pattern_id] = pattern

    def new_pattern_id(self) -> str:
        """Allocate a stable, monotonically increasing pattern id."""
        pid = f"p{self._next_id:04d}"
        self._next_id += 1
        return pid

    def active(self) -> list[PatternRecord]:
        return [p for p in self.patterns.values() if p.status == "active"]

    def by_polarity(self, polarity: Polarity) -> list[PatternRecord]:
        return [p for p in self.active() if p.polarity == polarity]

    def is_l1_signal(
        self,
        pattern: PatternRecord,
        *,
        trend_window: int,
        l0_saturated: bool,
        min_task_fraction: float,
        remedy_threshold: int = 0,
        require_remedy_count: bool = False,
        trend_eps: float = 1e-3,
    ) -> bool:
        """Code-computed L1-signal predicate (design Layer 3, all must hold):

        (a) occurrence rate shows no significant *declining* trend over the
            recent ``trend_window`` epochs (slope >= -trend_eps);
        (b) L0 is saturated. Per the authoritative design (design_final_en.md
            §Layer 3), "N consecutive rejects = remedy_resistance evidence" — so
            L0 saturation *is* the remedy-resistance signal. The separate
            ``remedy_resistance`` integer counter (v6 D4) is an optional, stricter
            tightening, applied only when ``require_remedy_count=True``;
        (c) the pattern still affects a significant fraction of tasks
            (latest occurrence rate >= ``min_task_fraction``).

        A pattern satisfying all of these needs a *thinking-level* change (L1),
        because L0 rule optimization has demonstrably failed to suppress it.

        Note: criterion (b) is left as a design ambiguity between the
        authoritative English doc (saturation = evidence) and v6 D4
        (remedy_resistance >= K). The default follows the authoritative doc;
        flip ``require_remedy_count`` to enforce the stricter v6 D4 reading.
        """
        # A pattern with no tracked occurrences cannot be a signal — guards a
        # vacuous positive when min_task_fraction <= 0 and history is empty.
        if not pattern.occurrence_history:
            return False
        no_decline = pattern.occurrence_trend(trend_window) >= -trend_eps
        resists_l0 = l0_saturated
        if require_remedy_count:
            resists_l0 = resists_l0 and pattern.remedy_resistance >= remedy_threshold
        widespread = pattern.latest_occurrence >= min_task_fraction
        return bool(no_decline and resists_l0 and widespread)

    def l1_signals(
        self,
        *,
        trend_window: int,
        l0_saturated: bool,
        min_task_fraction: float,
        remedy_threshold: int = 0,
        require_remedy_count: bool = False,
    ) -> list[PatternRecord]:
        """All active failure patterns currently qualifying as L1 signals."""
        return [
            p for p in self.by_polarity("failure")
            if self.is_l1_signal(
                p,
                trend_window=trend_window,
                l0_saturated=l0_saturated,
                min_task_fraction=min_task_fraction,
                remedy_threshold=remedy_threshold,
                require_remedy_count=require_remedy_count,
            )
        ]

    @classmethod
    def from_dict(cls, d: dict) -> "PatternLibrary":
        lib = cls(_next_id=int(d.get("_next_id", 0)))
        raw = d.get("patterns", [])
        # to_dict serializes patterns as a LIST; guard against a dict-keyed
        # format being passed in (would otherwise iterate keys and lose data).
        if isinstance(raw, dict):
            raw = list(raw.values())
        elif not isinstance(raw, list):
            raise ValueError(
                f"PatternLibrary.from_dict expected 'patterns' to be a list, "
                f"got {type(raw).__name__}"
            )
        for pd in raw:
            rec = PatternRecord.from_dict(pd)
            lib.patterns[rec.pattern_id] = rec
        return lib

    def to_dict(self, include_embeddings: bool = False) -> dict:
        return {
            "_next_id": self._next_id,
            "patterns": [p.to_dict(include_embeddings) for p in self.patterns.values()],
        }
