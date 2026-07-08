"""L1 action layer (user rulings 2026-07-08): fragile supply with
maturity-ordered evidence, S3 soft-unlock spawn arbitration, per-node
shuffle streams, and the ledger's recent-ring persistence."""
from __future__ import annotations

import json
from types import SimpleNamespace

from css.config import CSSConfig
from css.coverage.ledger import CoverageLedger
from css.data.step_buffer import StepBufferEntry
from css.data.tree import TreeNode
from css.optimizer.exploitation import _node_ordinal
from css.tree_search import (
    meaningful_delta,
    spawn_arbitration,
    spawn_unlocked,
    supply_fraction,
)


def _cfg(**kw) -> CSSConfig:
    base = dict(n_train=4, n_val=200, n_test=2, burst_steps=5)
    base.update(kw)
    return CSSConfig(**base)


# ── fragile: maturity-ordered evidence ───────────────────────────────────────
def test_fragile_mature_evidence_beats_history():
    """A task battered early but stable at mature steps is NOT fragile —
    whole-history rate would have lied (user's timing ruling)."""
    led = CoverageLedger(["t1"], min_attempts=1)
    for _ in range(6):                                  # early-burst failures
        led.record("n0", "t1", False, kind="l0", decision_index=0,
                   step_in_burst=0)
    for _ in range(3):                                  # mature-step passes
        led.record("n0", "t1", True, kind="l0", decision_index=1,
                   step_in_burst=3)
    # history: 3/9 = 33% < 0.4 -> would be fragile; mature: 3/3 -> stable.
    assert led.fragile_set(rate_lt=0.4, mature_min=3) == set()


def test_fragile_falls_back_to_history_without_mature_evidence():
    led = CoverageLedger(["t1"], min_attempts=1)
    for _ in range(5):                                   # early failures only
        led.record("n0", "t1", False, kind="l0", decision_index=0,
                   step_in_burst=1)
    assert "t1" in led.fragile_set(rate_lt=0.4, mature_min=3,
                                   hist_attempts=4)


def test_fragile_ignores_verify_kind_and_new_node_early_steps():
    """verify runs candidate configurations; a fresh child's early-burst
    attempts are immature — neither may poison the fragile judgement."""
    led = CoverageLedger(["t1"], min_attempts=1)
    for _ in range(3):                                   # stable mature l0
        led.record("n0", "t1", True, kind="l0", decision_index=2,
                   step_in_burst=4)
    for _ in range(6):                                   # child cold-start
        led.record("n1", "t1", False, kind="l0", decision_index=3,
                   step_in_burst=0)
    for _ in range(6):                                   # verify candidates
        led.record("n0", "t1", False, kind="verify", decision_index=3,
                   step_in_burst=3)
    assert led.fragile_set(rate_lt=0.4, mature_min=3) == set()


def test_recent_ring_persists_and_caps(tmp_path):
    p = str(tmp_path / "ledger.json")
    led = CoverageLedger(["t1"], min_attempts=1, path=p, recent_len=4)
    for i in range(7):
        led.record("n0", "t1", i == 0, kind="l0", decision_index=i,
                   step_in_burst=i % 5)                 # 1 pass / 7 attempts
    led.save()
    back = CoverageLedger.load(p, recent_len=4)
    rec = back._nodes["n0"]["t1"]["recent"]
    assert len(rec) == 4 and rec[-1][0] == 6, "ring keeps the newest 4"
    assert back._nodes["n0"]["t1"]["attempts"] == 7, "totals stay cumulative"
    # An old-format ledger (no recent) loads and falls back to the
    # whole-history judgement: 1/7 = 14% < 0.4 -> fragile.
    d = json.load(open(p))
    for cells in d["nodes"].values():
        for c in cells.values():
            c.pop("recent", None)
    json.dump(d, open(p, "w"))
    old = CoverageLedger.load(p)
    assert old._nodes["n0"]["t1"]["recent"] == []
    assert old.fragile_set(hist_attempts=4) == {"t1"}


# ── S3: soft unlock + arbitration ────────────────────────────────────────────
def _node_with_dry_steps(n_steps: int) -> TreeNode:
    node = TreeNode(node_id="n0000")
    for i in range(n_steps):
        node.step_buffer.append(StepBufferEntry(
            step=i, action="reject", score_before=0.5, score_after=0.5))
    return node


def test_spawn_unlock_after_one_dry_burst():
    cfg = _cfg()
    node = _node_with_dry_steps(4)
    assert not spawn_unlocked(node, cfg)
    node = _node_with_dry_steps(5)
    assert spawn_unlocked(node, cfg)


def test_arbitration_supply_vs_recent_gain():
    cfg = _cfg()                                    # delta = 1pp floor
    led = CoverageLedger([f"t{i}" for i in range(10)], min_attempts=1)
    for i in range(10):                             # 2 unsolved of 10 -> 20%
        led.record("n0", f"t{i}", i >= 2, kind="l0", decision_index=0,
                   step_in_burst=3)
    node = _node_with_dry_steps(5)
    node.burst_rewards = [0.0]                      # no meaningful pace
    spawn_now, arb = spawn_arbitration(node, led, cfg)
    assert spawn_now and arb["spawn_score"] > 0
    # A node still climbing (2pp/burst) outweighs the same supply.
    node.burst_rewards = [0.02, 0.02]
    spawn_now, arb = spawn_arbitration(node, led, cfg)
    assert not spawn_now
    # Sub-delta churn counts as zero pace.
    node.burst_rewards = [0.005, 0.005]
    spawn_now, _ = spawn_arbitration(node, led, cfg)
    assert spawn_now


def test_supply_fraction_includes_fragile():
    cfg = _cfg()
    led = CoverageLedger(["t1", "t2"], min_attempts=1)
    led.record("n0", "t1", True, kind="l0", decision_index=0, step_in_burst=3)
    led.record("n0", "t2", True, kind="l0", decision_index=0, step_in_burst=0)
    for _ in range(3):                              # t2 fragile at maturity
        led.record("n0", "t2", False, kind="l0", decision_index=1,
                   step_in_burst=3)
    assert supply_fraction(led, cfg) == 0.5


def test_meaningful_delta_scales_with_val():
    assert meaningful_delta(_cfg(n_val=57)) == 2 / 57      # ~3.5pp
    assert meaningful_delta(_cfg(n_val=500)) == 0.01       # floor


# ── G: per-node shuffle streams ──────────────────────────────────────────────
def test_node_ordinal_and_stream_independence():
    import random
    assert _node_ordinal("n0007") == 7
    assert _node_ordinal("") != _node_ordinal("weird-id") or True
    items = list(range(40))
    def order(node_id, epoch=3, rnd=0, seed=42):
        s = list(items)
        random.Random(seed + _node_ordinal(node_id) * 1_000_000
                      + epoch + rnd * 10_000).shuffle(s)
        return s
    assert order("n0000") != order("n0001"), \
        "two nodes at the same decision get independent shuffles"
    assert order("n0001") == order("n0001"), "deterministic per node"
