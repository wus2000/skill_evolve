"""Tests for css/model/routing.py (locality-aware bounded-load router)."""
from __future__ import annotations

import threading
import time

from css.model.routing import ReplicaRouter


def _urls(n):
    return ["http://h%d:800%d/v1/chat/completions" % (i, i) for i in range(n)]


def test_single_url_short_circuit():
    r = ReplicaRouter(_urls(1))
    idx = r.acquire("any")
    assert idx == 0
    r.release(idx)
    assert r.snapshot()["inflight"] == [0]


def test_locality_same_key_same_choice_when_unloaded():
    r = ReplicaRouter(_urls(3))
    key = "session-abc"
    first = r.acquire(key)
    r.release(first)
    for _ in range(5):
        idx = r.acquire(key)
        assert idx == first  # unloaded -> always the HRW-preferred replica
        r.release(idx)


def test_distribution_roughly_uniform_over_keys():
    r = ReplicaRouter(_urls(2))
    counts = [0, 0]
    for i in range(2000):
        idx = r.acquire("key-%d" % i)
        counts[idx] += 1
        r.release(idx)
    assert 800 < counts[0] < 1200  # ~50/50 with slack


def test_bounded_load_spills_hot_key():
    """Many sessions preferring one replica spill once it exceeds its bound."""
    r = ReplicaRouter(_urls(2), load_factor=1.25)
    # Find a key preferring replica 0, then open MANY concurrent sessions
    # with distinct keys that all prefer replica 0.
    hot_keys = [k for k in ("k%d" % i for i in range(4000))
                if r.hrw_order(k)[0] == 0][:100]
    assert len(hot_keys) == 100
    picks = [r.acquire(k) for k in hot_keys]  # all held concurrently
    counts = [picks.count(0), picks.count(1)]
    # Without bounds this would be 100/0; the bound forces a near-even split.
    assert counts[1] >= 35, counts
    snap = r.snapshot()
    assert sum(snap["inflight"]) == 100


def test_release_decrements_and_tracks_busy():
    r = ReplicaRouter(_urls(2))
    idx = r.acquire("k")
    r.release(idx, busy_s=2.5)
    snap = r.snapshot()
    assert snap["inflight"][idx] == 0
    assert snap["busy_s"][idx] == 2.5


def test_unhealthy_cooldown_skips_then_recovers():
    r = ReplicaRouter(_urls(2), cooldown_s=0.3)
    key = "sticky-session"
    preferred = r.hrw_order(key)[0]
    other = 1 - preferred
    r.mark_unhealthy(preferred)
    idx = r.acquire(key)
    assert idx == other  # cooled replica skipped
    r.release(idx)
    time.sleep(0.35)
    idx = r.acquire(key)
    assert idx == preferred  # recovered after cooldown
    r.release(idx)


def test_all_unhealthy_degrades_to_least_loaded():
    r = ReplicaRouter(_urls(2), cooldown_s=60)
    r.mark_unhealthy(0)
    r.mark_unhealthy(1)
    a = r.acquire("x")
    b = r.acquire("y")
    assert {a, b} <= {0, 1}  # still serves; retry loop owns real failure
    r.release(a); r.release(b)


def test_consistency_adding_replica_remaps_minority():
    """HRW: growing the fleet remaps only ~1/N of keys (cache-warm growth)."""
    r2 = ReplicaRouter(_urls(2))
    r3 = ReplicaRouter(_urls(2) + ["http://h9:8009/v1/chat/completions"])
    keys = ["s-%d" % i for i in range(3000)]
    moved = 0
    for k in keys:
        old = r2.hrw_order(k)[0]
        new = r3.hrw_order(k)[0]
        if new != old:
            moved += 1
            assert new == 2  # keys only move TO the new replica, never between old ones
    assert 0.20 < moved / len(keys) < 0.47  # ~1/3 expected


def test_thread_safety_under_concurrency():
    r = ReplicaRouter(_urls(2), load_factor=1.25)
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
    assert snap["inflight"] == [0, 0]          # every slot released
    assert sum(snap["served"]) == 8 * 200
