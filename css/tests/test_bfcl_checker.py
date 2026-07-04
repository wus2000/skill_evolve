"""BFCL checker: upstream-parity, graded soft score, and per-rollout ISOLATION.

The isolation test is the regression for the finding that made us adapt the
checker: the upstream ``execute_multi_turn_func_call`` shares live instances via
module ``globals()`` keyed by ``model_name+id+class``, so concurrent rollouts of
the SAME entry with the same key cross-contaminate (measured 1/8 correct). Our
``checker.score`` builds fresh instances per call; this test runs gold and
CORRUPTED replays of the same entry concurrently and asserts each thread gets its
own correct verdict (no cross-talk).
"""
from __future__ import annotations

import json
import os
import threading

import pytest

from css.envs.bfcl import checker

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SPLIT_DIR = os.path.join(REPO, "data", "bfcl_split_seed42")
_HAVE = os.path.exists(os.path.join(SPLIT_DIR, "train", "items.json"))
pytestmark = pytest.mark.skipif(not _HAVE, reason="bfcl manifests not generated")


def _items():
    with open(os.path.join(SPLIT_DIR, "train", "items.json"), encoding="utf-8") as f:
        return json.load(f)


def _by_category(items, n=5):
    out = []
    for cat in ("base", "miss_func", "miss_param", "long_context"):
        out += [it for it in items if it["category"] == cat][:n]
    return out


def _score_gold(it):
    return checker.score(
        checker.gold_as_model(it["ground_truth"]), it["ground_truth"],
        it["initial_config"], it["involved_classes"], bool(it["long_context"]))


def test_gold_selfcheck_all_categories():
    for it in _by_category(_items(), n=5):
        r = _score_gold(it)
        assert r["valid"], "%s: gold replay should pass, got %s" % (it["id"], r["error"])
        assert r["turns_passed"] == r["n_turns"]


_UP = os.path.join(REPO, "env_candidates", "gorilla", "berkeley-function-call-leaderboard")


@pytest.mark.skipif(not os.path.exists(_UP), reason="pinned upstream checkout absent")
def test_upstream_parity_on_gold_replays():
    """Our verdict must equal upstream's on >=20 gold replays across all 4 cats."""
    import sys
    sys.path.insert(0, _UP)
    from bfcl_eval.eval_checker.multi_turn_eval.multi_turn_checker import multi_turn_checker as UP
    items = _by_category(_items(), n=5)
    assert len(items) >= 20
    for it in items:
        mine = _score_gold(it)["valid"]
        up = UP(checker.gold_as_model(it["ground_truth"]), it["ground_truth"],
                dict(it), it["category"], "parity_%s" % it["id"])["valid"]
        assert mine == up, "%s: verdict mismatch mine=%s upstream=%s" % (it["id"], mine, up)


def test_soft_is_fraction_of_turns_passed():
    it = next(x for x in _items() if x["category"] == "base" and len(x["ground_truth"]) >= 3)
    gt = it["ground_truth"]
    model = checker.gold_as_model(gt)
    model[1] = [[]]  # break turn 1
    r = checker.score(model, gt, it["initial_config"], it["involved_classes"], False)
    assert not r["valid"]
    assert r["turns_passed"] == 1
    assert r["n_turns"] == len(gt)


def test_per_rollout_isolation_under_8way_concurrency():
    """8 threads, half gold (expect valid) half corrupted (expect invalid), on the
    SAME entry concurrently — each must get its own correct verdict."""
    it = next(x for x in _items() if x["category"] == "base" and len(x["ground_truth"]) >= 3)
    gt = it["ground_truth"]
    corrupt = checker.gold_as_model(gt)
    corrupt[1] = [["cd(folder='__nonexistent_dir__')"]]  # diverge turn 1

    results: dict[int, bool] = {}

    def worker(i):
        model = checker.gold_as_model(gt) if i % 2 == 0 else [list(s) for s in corrupt]
        r = checker.score(model, gt, it["initial_config"], it["involved_classes"], False)
        results[i] = r["valid"]

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for i in range(8):
        expected = (i % 2 == 0)
        assert results[i] is expected, "thread %d cross-contaminated: got %s" % (i, results[i])
