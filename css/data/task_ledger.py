"""Global per-task difficulty ledger + dataset subsampling.

Two concerns live here:

  * :func:`uniform_subset` — the deterministic "knob >= len OR <= 0 => take all,
    else subsample" rule shared by every dataset-size knob.

  * :class:`TaskDifficultyLedger` — a SINGLE, run-global, ever-accumulating record
    of how often each TRAIN task is solved. It is fed by every train rollout
    (cold start seeds it; each round's post-exploitation rollout refreshes it) and
    drives :func:`difficulty_weighted_subset`, which biases the (bounded) analysis
    rollout toward the tasks whose trajectories teach the optimizer the most.

Sampling philosophy (see the design discussion): the most informative task to
analyse is NOT the hardest one, it is the one whose outcome is most UNCERTAIN /
MOVING — because (a) mixed-outcome tasks are the only source of same-task
contrastive pairs (the highest-value analysis material), and (b) frontier tasks
are the ones a strategy/rule change can actually flip. We therefore stratify by
the binary solve history into: frontier (mixed) > recently-flipped > learnable-
hard > mastered, and DOWN-WEIGHT proven-ceiling tasks (unsolved for many rounds).
No soft score is used — the accumulated solve RATE over repeated attempts is the
difficulty signal.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from css.data.rollout import TaskRolloutGroup


# ── Deterministic subsetting (the shared knob rule) ─────────────────────────

def uniform_subset(items: list, size: int, *, seed: int) -> list:
    """Return ``items`` (whole) when ``size <= 0`` or ``size >= len(items)``;
    otherwise a deterministic ``size``-element subsample (seeded shuffle), kept
    in the original order so the result is a stable sub-list.
    """
    n = len(items)
    if size is None or size <= 0 or size >= n:
        return list(items)
    idx = list(range(n))
    random.Random(seed).shuffle(idx)
    keep = sorted(idx[:size])
    return [items[i] for i in keep]


# ── Per-task difficulty record ──────────────────────────────────────────────

@dataclass
class TaskStat:
    """Accumulated solve history for one train task (all values lifetime/global)."""

    attempts: int = 0
    solves: int = 0
    last_solved_round: int = -1
    rounds_seen: int = 0
    unsolved_streak: int = 0        # consecutive ROUNDS seen with zero solves
    last_round_rate: float = -1.0   # most recent folded round's solve rate
    prev_round_rate: float = -1.0   # the round before that
    # Open (not-yet-folded) round accumulator:
    cur_round: int = -2
    cur_attempts: int = 0
    cur_solves: int = 0

    @property
    def ever_solved(self) -> bool:
        return self.solves > 0

    @property
    def solve_rate(self) -> float:
        """Lifetime solve rate over all attempts (0.0 when never attempted)."""
        return self.solves / self.attempts if self.attempts else 0.0

    @property
    def moved(self) -> bool:
        """True when the last two folded rounds swung sharply (cracked/regressed)."""
        if self.last_round_rate < 0 or self.prev_round_rate < 0:
            return False
        return abs(self.last_round_rate - self.prev_round_rate) >= 0.5

    def to_dict(self) -> dict:
        return {
            "attempts": self.attempts, "solves": self.solves,
            "last_solved_round": self.last_solved_round, "rounds_seen": self.rounds_seen,
            "unsolved_streak": self.unsolved_streak,
            "last_round_rate": self.last_round_rate, "prev_round_rate": self.prev_round_rate,
            "cur_round": self.cur_round, "cur_attempts": self.cur_attempts,
            "cur_solves": self.cur_solves,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TaskStat":
        return cls(**{k: d[k] for k in d if k in cls.__dataclass_fields__})


# ── The ledger ──────────────────────────────────────────────────────────────

class TaskDifficultyLedger:
    """Run-global, ever-accumulating per-task solve history (train tasks)."""

    def __init__(self, ceiling_rounds: int = 4) -> None:
        self.stats: dict[str, TaskStat] = {}
        # A task unsolved for >= ceiling_rounds consecutive rounds is treated as a
        # likely capability ceiling and down-weighted for analysis.
        self.ceiling_rounds = ceiling_rounds

    # -- updates --
    def record(self, task_id: str, hard: int, round_index: int) -> None:
        """Record ONE rollout outcome (``hard`` in {0,1}) for a task in a round."""
        if not task_id:
            return
        s = self.stats.get(task_id)
        if s is None:
            s = TaskStat(cur_round=round_index)
            self.stats[task_id] = s
        if round_index != s.cur_round and s.cur_attempts > 0:
            self._fold(s)
            s.cur_round = round_index
        elif s.cur_round < -1:
            s.cur_round = round_index
        s.attempts += 1
        s.cur_attempts += 1
        h = 1 if int(hard) >= 1 else 0
        s.solves += h
        s.cur_solves += h
        if h:
            s.last_solved_round = round_index

    def update_from_groups(self, groups: "list[TaskRolloutGroup]", round_index: int) -> None:
        """Fold every rollout in ``groups`` into the ledger for ``round_index``."""
        for g in groups or []:
            for r in getattr(g, "rollouts", []) or []:
                self.record(str(getattr(r, "task_id", "")), int(getattr(r, "hard", 0)), round_index)

    def _fold(self, s: TaskStat) -> None:
        rate = s.cur_solves / s.cur_attempts if s.cur_attempts else 0.0
        s.prev_round_rate = s.last_round_rate
        s.last_round_rate = rate
        s.rounds_seen += 1
        s.unsolved_streak = 0 if s.cur_solves > 0 else s.unsolved_streak + 1
        s.cur_attempts = 0
        s.cur_solves = 0

    def flush(self) -> None:
        """Fold every task's open round so reads see fully up-to-date stats."""
        for s in self.stats.values():
            if s.cur_attempts > 0:
                self._fold(s)
                s.cur_attempts = 0
                s.cur_solves = 0

    # -- reads --
    def classify(self, task_id: str) -> str:
        """Bucket a task from its solve history: frontier | flipped | hard |
        mastered | ceiling | unseen (priority order applied by the sampler)."""
        s = self.stats.get(task_id)
        if s is None or s.attempts == 0:
            return "unseen"
        sr = s.solve_rate
        if s.moved:
            return "flipped"
        if 0.0 < sr < 1.0:
            return "frontier"
        if sr <= 0.0:
            return "ceiling" if s.unsolved_streak >= self.ceiling_rounds else "hard"
        return "mastered"

    def has_data(self) -> bool:
        return any(s.attempts > 0 for s in self.stats.values())

    # -- checkpoint --
    def to_dict(self) -> dict:
        return {
            "ceiling_rounds": self.ceiling_rounds,
            "stats": {tid: s.to_dict() for tid, s in self.stats.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TaskDifficultyLedger":
        led = cls(ceiling_rounds=int(d.get("ceiling_rounds", 4)))
        led.stats = {tid: TaskStat.from_dict(sd) for tid, sd in (d.get("stats") or {}).items()}
        return led


# ── Difficulty-weighted analysis subsampling ────────────────────────────────

# Bucket sampling priority (a task is counted in its highest-priority bucket).
_BUCKET_ORDER = ("flipped", "frontier", "hard", "mastered")


def difficulty_weighted_subset(
    items: list,
    size: int,
    ledger: "TaskDifficultyLedger | None",
    *,
    seed: int,
    fracs: dict | None = None,
    id_of: Callable[[dict], str] = lambda it: str(it.get("id", it.get("task_id", ""))),
) -> list:
    """Pick ``size`` items biased by difficulty (frontier > flipped > hard >
    mastered; ceiling/unseen fill only as remainder).

    Falls back to :func:`uniform_subset` when ``size`` covers the whole set or the
    ledger has no data yet. Deterministic given ``seed``.
    """
    n = len(items)
    if size is None or size <= 0 or size >= n:
        return list(items)
    if ledger is None or not ledger.has_data():
        return uniform_subset(items, size, seed=seed)

    ledger.flush()
    fr = {
        "frontier": 0.45, "hard": 0.30, "flipped": 0.15, "mastered": 0.10,
    }
    if fracs:
        fr.update({k: float(v) for k, v in fracs.items() if k in fr})

    # Partition items into buckets (highest-priority bucket wins).
    buckets: dict[str, list] = {b: [] for b in _BUCKET_ORDER}
    remainder: list = []  # ceiling / unseen
    for it in items:
        b = ledger.classify(id_of(it))
        (buckets[b] if b in buckets else remainder).append(it)

    rng = random.Random(seed)
    for b in buckets:
        rng.shuffle(buckets[b])
    rng.shuffle(remainder)

    total_frac = sum(fr[b] for b in _BUCKET_ORDER) or 1.0
    chosen: list = []
    chosen_ids: set = set()
    # First pass: each bucket's proportional target (capped by availability).
    for b in _BUCKET_ORDER:
        target = round(size * fr[b] / total_frac)
        for it in buckets[b][:target]:
            chosen.append(it)
            chosen_ids.add(id_of(it))

    # Fill any shortfall (from rounding / thin buckets) from leftover bucket items
    # first, then the remainder (ceiling/unseen), deterministically.
    if len(chosen) < size:
        leftovers = [it for b in _BUCKET_ORDER for it in buckets[b]
                     if id_of(it) not in chosen_ids] + remainder
        for it in leftovers:
            if len(chosen) >= size:
                break
            if id_of(it) in chosen_ids:
                continue
            chosen.append(it)
            chosen_ids.add(id_of(it))

    return chosen[:size]
