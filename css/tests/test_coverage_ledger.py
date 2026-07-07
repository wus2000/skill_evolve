"""Coverage ledger + probe leads (docs/L1_actions_redesign.md §1).

The union-of-evidence semantics under test are the direct answer to the AW
post-mortem: per-burst residual INTERSECTION emptied at decision 1 (support
drift), starving exploration forever. Here a task leaves ``global_unsolved``
only by actually being solved somewhere.
"""
from __future__ import annotations

import threading

from css.coverage import CoverageLedger, coverage_path, load_coverage
from css.explore import leads as leads_mod


class _R:
    def __init__(self, task_id, passed):
        self.task_id = task_id
        self.passed = passed


class _G:
    def __init__(self, task_id, outcomes):
        self.task_id = task_id
        self.rollouts = [_R(task_id, p) for p in outcomes]


# ── states ───────────────────────────────────────────────────────────────────
def test_three_value_states_at_m1():
    led = CoverageLedger(["t1", "t2", "t3"], min_attempts=1)
    led.record("n0", "t1", False)
    led.record("n0", "t2", True)
    assert led.unsolved_set("n0") == {"t1"}      # m=1: one failed attempt counts
    assert led.solved_set("n0") == {"t2"}
    assert led.uncharted() == {"t3"}


def test_min_attempts_gate():
    led = CoverageLedger(["t1"], min_attempts=3)
    led.record("n0", "t1", False)
    led.record("n0", "t1", False)
    assert led.unsolved_set("n0") == set()        # 2 < m: still unattempted
    assert led.uncharted() == {"t1"}
    led.record("n0", "t1", False)
    assert led.unsolved_set("n0") == {"t1"}
    assert led.uncharted() == set()


def test_single_pass_always_means_solved():
    led = CoverageLedger(["t1"], min_attempts=3)
    led.record("n0", "t1", True)                  # solved even below m attempts
    assert led.solved_set("n0") == {"t1"}
    assert led.global_unsolved() == set()


def test_solved_task_is_never_uncharted_even_below_m():
    # Code-review regression (2026-07-07): with m>1 a task solved on its only
    # attempt is not 'attempted' (1 < m) but is certainly not a blind spot —
    # leaving it in uncharted() would block the MERGE transition forever.
    led = CoverageLedger(["t1", "t2"], min_attempts=3)
    led.record("n0", "t1", True)                  # single-attempt solve
    assert "t1" not in led.uncharted()
    assert led.uncharted() == {"t2"}


def test_readers_are_safe_against_concurrent_writers():
    # Readers take the (re-entrant) lock too: iterating dicts while worker
    # threads insert used to be able to raise RuntimeError.
    import threading as _t
    led = CoverageLedger(["t%d" % i for i in range(50)], min_attempts=1)
    stop = _t.Event()
    errors: list = []

    def writer():
        i = 0
        while not stop.is_set():
            led.record("n%d" % (i % 7), "t%d" % (i % 50), i % 3 == 0, kind="l0")
            i += 1

    def reader():
        try:
            while not stop.is_set():
                led.global_unsolved()
                led.paradigm_sensitive()
                led.signature()
                led.uncharted()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [_t.Thread(target=writer) for _ in range(3)] + [
        _t.Thread(target=reader) for _ in range(3)]
    for th in threads:
        th.start()
    import time as _time
    _time.sleep(0.3)
    stop.set()
    for th in threads:
        th.join()
    assert not errors, errors


# ── global sets: union-of-evidence ───────────────────────────────────────────
def _two_node_ledger() -> CoverageLedger:
    led = CoverageLedger(["t1", "t2", "t3", "t4", "t5", "t9"], min_attempts=1)
    # nA: fails t1 x2, t2 x3, t3 x1; solves t5.
    for _ in range(2):
        led.record("nA", "t1", False)
    for _ in range(3):
        led.record("nA", "t2", False)
    led.record("nA", "t3", False)
    led.record("nA", "t5", True)
    # nB: fails t2 x2, t4 x1; solves t3.
    for _ in range(2):
        led.record("nB", "t2", False)
    led.record("nB", "t4", False)
    led.record("nB", "t3", True)
    return led


def test_union_semantics_beat_intersection():
    led = _two_node_ledger()
    # Intersection of per-node failure sets would be {t2} — the union of
    # evidence keeps every task nobody has ever solved.
    assert led.global_unsolved() == {"t1", "t2", "t4"}
    assert led.paradigm_sensitive() == {"t3"}     # nA unsolved, nB solved
    assert led.uncharted() == {"t9"}
    assert led.solvers("t3") == ["nB"]
    assert led.shortfall("nA") == {"t3": ["nB"]}
    assert led.shortfall("nB") == {}
    assert led.exclusive_coverage("nB") == {"t3"}
    # Priority signal: t2 carries 5 failed attempts, t1 2, t4 1.
    assert led.failure_weight("t2") == 5
    assert led.failure_weight("t1") == 2


def test_task_leaves_only_by_being_solved():
    led = _two_node_ledger()
    assert "t2" in led.global_unsolved()
    for _ in range(50):                            # sampling churn: more failures
        led.record("nA", "t2", False)
    assert "t2" in led.global_unsolved(), "attempt churn must never clear the set"
    led.record("nB", "t2", True)                   # a REAL solve
    assert "t2" not in led.global_unsolved()


# ── signature ────────────────────────────────────────────────────────────────
def test_signature_flips_only_on_state_change():
    led = _two_node_ledger()
    s0 = led.signature()
    led.record("nA", "t1", False)                  # more attempts, same state
    assert led.signature() == s0
    led.record("nB", "t1", True)                   # state flip: unsolved -> solved
    assert led.signature() != s0


# ── recording from rollout groups ────────────────────────────────────────────
def test_record_groups_and_kinds():
    led = CoverageLedger(min_attempts=1)
    n = led.record_groups("n0", [_G("t1", [False, False, True]),
                                 _G("t2", [False])], kind="l0", decision_index=4)
    assert n == 4
    st = led.stats("n0", "t1")
    assert st["attempts"] == 3 and st["passes"] == 1
    assert st["kinds"] == {"l0": 3} and st["last_decision"] == 4
    led.record_groups("n0", [_G("t2", [True])], kind="verify", decision_index=5)
    st2 = led.stats("n0", "t2")
    assert st2["kinds"] == {"l0": 1, "verify": 1}


# ── persistence ──────────────────────────────────────────────────────────────
def test_save_load_roundtrip(tmp_path):
    out = str(tmp_path)
    led = _two_node_ledger()
    led.path = coverage_path(out)
    led.save()
    back = load_coverage(out, min_attempts=1)
    assert back.global_unsolved() == led.global_unsolved()
    assert back.paradigm_sensitive() == led.paradigm_sensitive()
    assert back.uncharted() == led.uncharted()
    assert back.signature() == led.signature()
    assert back.stats("nA", "t2")["attempts"] == 3


def test_load_missing_is_empty(tmp_path):
    led = load_coverage(str(tmp_path))
    assert not led.has_data()
    assert led.global_unsolved() == set()


# ── thread safety (verify fanout records concurrently) ──────────────────────
def test_concurrent_records_lose_nothing():
    led = CoverageLedger(min_attempts=1)

    def worker(i):
        for _ in range(100):
            led.record("n0", "t%d" % (i % 3), False, kind="verify")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    total = sum(led.stats("n0", "t%d" % j)["attempts"] for j in range(3))
    assert total == 600


# ── leads: signal only, never state (deadlock regression) ────────────────────
def test_probe_pass_is_a_lead_never_solved_state(tmp_path):
    out = str(tmp_path)
    led = _two_node_ledger()
    led.path = coverage_path(out)
    led.save()
    assert "t2" in load_coverage(out).global_unsolved()

    # A lucky probe cracks t2 — archived as a lead...
    lp = leads_mod.leads_path(out)
    assert leads_mod.record_lead(lp, task_id="t2",
                                 behavior_prompt="probe both sources in parallel",
                                 n_pass=2, k=2, session_ref="session_0003#probe_1",
                                 decision_index=3)
    # ...and the coverage ledger is UNTOUCHED: t2 stays globally unsolved
    # (the deadlock scenario: probe passes emptying global_unsolved would
    # trigger MERGE over strategies none of which solve t2).
    assert "t2" in load_coverage(out).global_unsolved()
    assert leads_mod.leads_for(lp, ["t2"])


def test_leads_cap_rank_and_dedupe(tmp_path):
    lp = leads_mod.leads_path(str(tmp_path))
    for i, (prompt, npass) in enumerate(
            [("a", 1), ("b", 2), ("c", 1), ("d", 2)]):
        leads_mod.record_lead(lp, task_id="t1", behavior_prompt=prompt,
                              n_pass=npass, k=2, decision_index=i, cap=3)
    bucket = leads_mod.load_leads(lp)["t1"]
    assert len(bucket) == 3                        # capped
    assert bucket[0]["behavior_prompt"] in ("b", "d")   # best pass-rate first
    # Exact-duplicate prompt folds into its best record.
    leads_mod.record_lead(lp, task_id="t1", behavior_prompt="b",
                          n_pass=2, k=2, decision_index=9, cap=3)
    bucket2 = leads_mod.load_leads(lp)["t1"]
    assert len(bucket2) == 3
    assert sum(1 for e in bucket2 if e["behavior_prompt"] == "b") == 1


def test_zero_pass_probe_records_nothing(tmp_path):
    lp = leads_mod.leads_path(str(tmp_path))
    assert not leads_mod.record_lead(lp, task_id="t1", behavior_prompt="x",
                                     n_pass=0, k=2)
    assert leads_mod.load_leads(lp) == {}


def test_render_leads_block(tmp_path):
    lp = leads_mod.leads_path(str(tmp_path))
    leads_mod.record_lead(lp, task_id="t1", behavior_prompt="parallel probes",
                          n_pass=2, k=2)
    text = leads_mod.render_leads(lp, ["t1", "t2"])
    assert "KNOWN LEADS" in text and "t1" in text and "2/2" in text
    assert leads_mod.render_leads(lp, ["t9"]) == ""
