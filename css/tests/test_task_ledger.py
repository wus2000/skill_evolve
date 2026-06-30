"""Tests for the global task-difficulty ledger and dataset subsampling."""
from __future__ import annotations

from css.data.task_ledger import (
    TaskDifficultyLedger,
    difficulty_weighted_subset,
    uniform_subset,
)


# ── uniform_subset: the shared knob rule ────────────────────────────────────

def test_uniform_subset_knob_rule():
    items = list(range(50))
    assert uniform_subset(items, 0, seed=1) == items          # 0 -> all
    assert uniform_subset(items, -5, seed=1) == items         # <0 -> all
    assert uniform_subset(items, 50, seed=1) == items         # ==len -> all
    assert uniform_subset(items, 999, seed=1) == items        # >len -> all
    sub = uniform_subset(items, 10, seed=1)
    assert len(sub) == 10
    assert sub == sorted(sub)                                  # stable original order
    assert uniform_subset(items, 10, seed=1) == sub           # deterministic
    assert uniform_subset(items, 10, seed=2) != sub           # seed-dependent


# ── ledger classification ───────────────────────────────────────────────────

def _feed(led, tid, rounds):
    for ri, outcomes in enumerate(rounds):
        for h in outcomes:
            led.record(tid, h, ri)
    led.flush()


def test_ledger_classifies_buckets():
    led = TaskDifficultyLedger(ceiling_rounds=4)
    _feed(led, "mastered", [[1, 1, 1], [1, 1, 1]])
    _feed(led, "frontier", [[1, 0, 0], [1, 0, 0]])
    _feed(led, "hard", [[0, 0, 0], [0, 0, 0]])
    _feed(led, "ceiling", [[0, 0]] * 5)
    _feed(led, "flipped", [[0, 0, 0], [1, 1, 1]])
    assert led.classify("mastered") == "mastered"
    assert led.classify("frontier") == "frontier"
    assert led.classify("hard") == "hard"
    assert led.classify("ceiling") == "ceiling"
    assert led.classify("flipped") == "flipped"
    assert led.classify("never_seen") == "unseen"
    # lifetime solve rate sanity
    assert led.stats["mastered"].solve_rate == 1.0
    assert 0.0 < led.stats["frontier"].solve_rate < 1.0
    assert led.stats["hard"].solve_rate == 0.0


def test_ledger_checkpoint_roundtrip():
    led = TaskDifficultyLedger(ceiling_rounds=3)
    _feed(led, "t", [[1, 0], [0, 0]])
    d = led.to_dict()
    led2 = TaskDifficultyLedger.from_dict(d)
    assert led2.ceiling_rounds == 3
    assert led2.stats["t"].attempts == led.stats["t"].attempts
    assert led2.stats["t"].solves == led.stats["t"].solves
    assert led2.classify("t") == led.classify("t")


# ── difficulty-weighted subsampling ─────────────────────────────────────────

def _mk(prefix, n):
    return [{"id": f"{prefix}_{i}"} for i in range(n)]


def test_weighted_subset_fallback_when_empty():
    items = _mk("x", 40)
    led = TaskDifficultyLedger()
    # No data -> uniform fallback (deterministic, right size).
    out = difficulty_weighted_subset(items, 10, led, seed=1)
    assert len(out) == 10
    assert out == difficulty_weighted_subset(items, 10, led, seed=1)


def test_weighted_subset_take_all_when_size_covers():
    items = _mk("x", 8)
    led = TaskDifficultyLedger()
    assert difficulty_weighted_subset(items, 8, led, seed=1) == items
    assert difficulty_weighted_subset(items, 0, led, seed=1) == items


def test_weighted_subset_oversamples_frontier_over_mastered():
    led = TaskDifficultyLedger(ceiling_rounds=4)
    items = []
    # 10 frontier, 10 mastered, 10 hard, 10 flipped, 60 unseen.
    for i in range(10):
        _feed(led, f"frontier_{i}", [[1, 0, 0], [1, 0, 0]])
        _feed(led, f"mastered_{i}", [[1, 1, 1], [1, 1, 1]])
        _feed(led, f"hard_{i}", [[0, 0, 0], [0, 0, 0]])
        _feed(led, f"flipped_{i}", [[0, 0, 0], [1, 1, 1]])
    items += _mk("frontier", 10) + _mk("mastered", 10) + _mk("hard", 10)
    items += _mk("flipped", 10) + _mk("unseen", 60)

    out = difficulty_weighted_subset(items, 20, led, seed=7)
    assert len(out) == 20
    kinds = [o["id"].rsplit("_", 1)[0] for o in out]
    n_frontier = kinds.count("frontier")
    n_mastered = kinds.count("mastered")
    # Frontier (0.45) is sampled far more than mastered (0.10); unseen only fills.
    assert n_frontier > n_mastered
    assert n_frontier >= 7
    assert kinds.count("unseen") == 0  # buckets cover the budget; no remainder used
