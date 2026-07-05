"""Tests for llmfleet.routing (three-source, two-tier session router).

Deterministic: injected clock (now_fn), injected server snapshots
(ingest_snapshot), poller disabled.
"""
from __future__ import annotations

import threading

from llmfleet.routing import ReplicaRouter, parse_metrics_text


class _Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def tick(self, dt: float) -> None:
        self.t += dt


def _urls(n):
    return ["http://h%d:800%d/v1/chat/completions" % (i, i) for i in range(n)]


def _router(n=2, clock=None, **kw):
    kw.setdefault("enable_poller", False)
    return ReplicaRouter(_urls(n), now_fn=clock or _Clock(), **kw)


def _feed(r, clock, snaps, polls=8, dt=3.0, base_tokens=0.0):
    """Feed `polls` snapshots per node; snaps[i] = (running, waiting, tok_rate)."""
    totals = [base_tokens] * len(snaps)
    for _ in range(polls):
        clock.tick(dt)
        for i, (run, wait, rate) in enumerate(snaps):
            totals[i] += rate * dt
            r.ingest_snapshot(i, run, wait, totals[i], 0.0)


# ── metrics parsing ──────────────────────────────────────────────────────────
def test_parse_metrics_text():
    text = (
        "# HELP vllm:num_requests_running x\n"
        'vllm:num_requests_running{engine="0",model_name="m"} 46.0\n'
        'vllm:num_requests_waiting{engine="0",model_name="m"} 3.0\n'
        'vllm:prompt_tokens_total{engine="0",model_name="m"} 9.41e+08\n'
        'vllm:generation_tokens_total{engine="0",model_name="m"} 2.1e+07\n'
        "other_metric 5\n"
    )
    out = parse_metrics_text(text)
    assert out == {"running": 46.0, "waiting": 3.0,
                   "prompt_total": 9.41e8, "gen_total": 2.1e7}
    assert parse_metrics_text("nothing relevant 1\n") == {}


# ── cold placement (least expected wait over server truth) ──────────────────
def test_cold_placement_prefers_lower_server_load():
    clock = _Clock()
    r = _router(2, clock)
    _feed(r, clock, [(40, 10, 1000.0), (5, 0, 1000.0)])  # node0 busy, node1 idle
    picks = [r.acquire("new-session-%d" % i) for i in range(10)]
    # All cold placements should avoid the loaded node initially; optimistic
    # increments then shift some to node 0 only after node 1 fills up.
    assert picks.count(1) >= 8
    for i in picks:
        r.release(i, 0.1)


def test_cold_placement_weights_heterogeneous_capacity():
    clock = _Clock()
    # node1 measured at half throughput (H20-like) under equal load.
    r = _router(2, clock)
    _feed(r, clock, [(20, 0, 2000.0), (20, 0, 1000.0)], polls=10)
    picks = [r.acquire("s%d" % i) for i in range(60)]
    fast, slow = picks.count(0), picks.count(1)
    # Fast node first absorbs until marginal expected waits equalize, then the
    # allocation approaches the 2:1 capacity ratio.
    assert fast > slow
    assert slow >= 8
    for i in picks:
        r.release(i, 0.1)


def test_busy_gate_rejects_idle_throughput_samples():
    clock = _Clock()
    r = _router(2, clock)
    # node1 idle (running=2 < threshold 8): its low token rate must NOT count.
    _feed(r, clock, [(20, 0, 2000.0), (2, 0, 10.0)], polls=10)
    snap = r.snapshot()
    assert snap["rate_ewma"][0] > 0
    assert snap["rate_ewma"][1] == 0  # sample gated out -> prior fills in


def test_optimistic_increments_spread_within_poll_gap():
    clock = _Clock()
    r = _router(2, clock)
    _feed(r, clock, [(10, 0, 1000.0), (10, 0, 1000.0)])
    picks = [r.acquire("k%d" % i) for i in range(20)]  # no release, no new poll
    counts = [picks.count(0), picks.count(1)]
    assert abs(counts[0] - counts[1]) <= 2  # disp counters keep it even
    for i in picks:
        r.release(i, 0.1)


# ── warm path (session table, home, spill, re-home) ─────────────────────────
def test_warm_turns_go_home():
    clock = _Clock()
    r = _router(2, clock)
    _feed(r, clock, [(10, 0, 1000.0), (10, 0, 1000.0)])
    home = r.acquire("episode-A")
    r.release(home, 0.1)
    for _ in range(5):
        idx = r.acquire("episode-A")
        assert idx == home
        r.release(idx, 0.1)
    assert r.snapshot()["counters"]["warm"] >= 5


def test_k_siblings_follow_home():
    clock = _Clock()
    r = _router(3, clock)
    home = r.acquire("task42")           # sibling 1 places cold
    sib2 = r.acquire("task42")           # siblings hit the table
    sib3 = r.acquire("task42")
    assert home == sib2 == sib3
    for i in (home, sib2, sib3):
        r.release(i, 0.1)


def test_home_saturation_spills_then_rehomes_with_lock():
    clock = _Clock()
    r = _router(2, clock)
    home = r.acquire("epi")
    r.release(home, 0.1)
    other = 1 - home
    # Saturate home massively on the server side; other stays idle.
    snaps = [(0, 0, 1000.0), (0, 0, 1000.0)]
    snaps[home] = (200, 50, 1000.0)
    snaps[other] = (2, 0, 1000.0)
    _feed(r, clock, snaps)
    spills = []
    for _ in range(5):
        idx = r.acquire("epi")
        spills.append(idx)
        r.release(idx, 0.1)
    assert all(i == other for i in spills)          # deterministic spill target
    assert r.snapshot()["counters"]["rehome"] == 1  # 5th spill re-homed
    # After re-home + saturation flip, session stays on the new home.
    idx = r.acquire("epi")
    assert idx == other
    r.release(idx, 0.1)


def test_session_ttl_expiry_falls_back_to_cold():
    clock = _Clock()
    r = _router(2, clock)
    first = r.acquire("old-session")
    r.release(first, 0.1)
    clock.tick(7300)  # > TTL 2h
    r.acquire("old-session")
    counters = r.snapshot()["counters"]
    assert counters["cold"] == 2  # expired entry re-placed cold


# ── health: cooldown, backoff, recovery slow-start ───────────────────────────
def test_unhealthy_cooldown_and_exponential_backoff():
    clock = _Clock()
    r = _router(2, clock)
    key = "sess"
    home = r.acquire(key)
    r.release(home, ok=False)
    r.mark_unhealthy(home)                    # 15s cooldown
    idx = r.acquire(key)                       # spills away from home
    assert idx != home
    r.release(idx, 0.1)
    r.mark_unhealthy(home)                     # streak=2 -> 30s
    clock.tick(20)
    snap = r.snapshot()
    assert snap["load"][home] == 0             # still cooling (20 < 30)
    idx = r.acquire(key)
    assert idx != home
    r.release(idx, 0.1)


def test_all_unhealthy_degrades_to_least_loaded():
    clock = _Clock()
    r = _router(2, clock)
    r.mark_unhealthy(0)
    r.mark_unhealthy(1)
    a = r.acquire("x")
    b = r.acquire("y")
    assert {a, b} == {0, 1}  # still serves, spread by load
    r.release(a); r.release(b)


def test_degraded_latency_vetoes_cold_placement():
    clock = _Clock()
    r = _router(3, clock)
    _feed(r, clock, [(10, 0, 1000.0)] * 3)
    # Build latency history: node 0 slow (60s), others fast (2s).
    for _ in range(12):
        for i in range(3):
            r.release(i, 60.0 if i == 0 else 2.0, ok=True)
        clock.tick(1)
    picks = [r.acquire("fresh-%d" % i) for i in range(12)]
    assert picks.count(0) == 0  # vetoed from cold placement
    for i in picks:
        r.release(i, 0.1)


# ── S-source degradation & fallbacks ─────────────────────────────────────────
def test_metrics_dead_falls_back_to_local_inflight():
    clock = _Clock()
    r = _router(2, clock)
    _feed(r, clock, [(50, 0, 1000.0), (50, 0, 1000.0)], polls=2)
    clock.tick(3.0 * 20)  # both snapshots go stale-dead (>10 polls)
    a = r.acquire("s1")   # local view: loads are 0 -> spread by HRW/disp
    b = r.acquire("s2")
    snap = r.snapshot()
    assert snap["s_fresh"] == [False, False]
    r.release(a); r.release(b)


# ── growth: zero remap of existing sessions ─────────────────────────────────
def test_fleet_growth_keeps_existing_homes_and_fills_new_node():
    clock = _Clock()
    r2 = _router(2, clock)
    homes = {}
    for i in range(40):
        key = "sess-%d" % i
        idx = r2.acquire(key)
        homes[key] = idx
        r2.release(idx, 0.1)
    # "Grow" by constructing a 3-node router that inherits the session table
    # (in-process growth = restart with new registry; table persists only in
    # memory, so growth without restart is the relevant case).
    r3 = ReplicaRouter(_urls(3), enable_poller=False, now_fn=clock)
    r3._sessions = r2._sessions  # same table object: simulate live extension
    moved = 0
    for key, old in homes.items():
        idx = r3.acquire(key)
        if idx != old:
            moved += 1
        r3.release(idx, 0.1)
    assert moved == 0  # warm sessions never remap on growth
    # New sessions flow to the idle new node first (all loads ~0, ties by
    # HRW give ~1/3; with server load on old nodes it would be stronger).
    _feed(r3, clock, [(30, 0, 1000.0), (30, 0, 1000.0), (0, 0, 1000.0)])
    fresh = [r3.acquire("fresh-%d" % i) for i in range(9)]
    assert fresh.count(2) >= 7
    for i in fresh:
        r3.release(i, 0.1)


# ── concurrency ──────────────────────────────────────────────────────────────
def test_thread_safety_under_concurrency():
    r = _router(2, _Clock())
    errors = []

    def work(tid):
        try:
            for i in range(200):
                idx = r.acquire("t%d-%d" % (tid, i % 7))
                r.release(idx, 0.001)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=work, args=(t,)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    snap = r.snapshot()
    assert snap["local_inflight"] == [0, 0]
    assert sum(snap["served"]) == 8 * 200


def test_single_url_short_circuit():
    r = ReplicaRouter(_urls(1), enable_poller=False, now_fn=_Clock())
    assert r.acquire("any") == 0
    r.release(0, 0.1)
    assert r.snapshot()["local_inflight"] == [0]


def test_mark_unhealthy_survives_long_outage():
    """Live incident 2026-07-05: an hours-dead endpoint grew fail_streak into
    the thousands and ``base * 2**streak`` raised OverflowError before min()
    could clamp — every LLM call then failed instantly. The exponent must be
    clamped BEFORE exponentiating."""
    r = ReplicaRouter(["http://a/v1"])
    for _ in range(5000):
        r.mark_unhealthy(0)  # must never raise
    snap = r.snapshot()
    assert snap  # router still serviceable
