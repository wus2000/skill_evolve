"""Validation of the generated BFCL multi-turn split manifests.

Skipped when the manifests have not been generated (tools/make_bfcl_split.py).
When present, asserts the invariants the mechanism + the split design rely on:
exact counts, ZERO scenario leakage across train/val/test, every challenge type
in every split, the composite probe confined to test indices, unique ids, and a
well-formed item schema. A determinism check (regenerate -> byte-identical) runs
only when the pinned upstream checkout is available.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SPLIT_DIR = os.path.join(REPO, "data", "bfcl_split_seed42")
_ACTIVE = ("base", "miss_func", "miss_param", "long_context")
_HAVE = os.path.exists(os.path.join(SPLIT_DIR, "test", "items.json"))
pytestmark = pytest.mark.skipif(not _HAVE, reason="bfcl manifests not generated")


def _load(name):
    path = os.path.join(SPLIT_DIR, name, "items.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _idx(items):
    return {it["scenario_index"] for it in items}


def test_manifest_counts():
    assert len(_load("train")) == 480
    assert len(_load("val")) == 160
    assert len(_load("test")) == 160
    assert len(_load("composite_probe")) == 40


def test_zero_scenario_leakage():
    tr, va, te = _idx(_load("train")), _idx(_load("val")), _idx(_load("test"))
    assert len(tr) == 120 and len(va) == 40 and len(te) == 40
    assert not (tr & va), "train/val scenario leakage"
    assert not (tr & te), "train/test scenario leakage"
    assert not (va & te), "val/test scenario leakage"


def test_every_category_in_every_split():
    for split in ("train", "val", "test"):
        cats = {it["category"] for it in _load(split)}
        assert cats == set(_ACTIVE), "%s missing categories: %s" % (split, set(_ACTIVE) - cats)
        # balanced: 120/40/40 per category
        per = {c: sum(1 for it in _load(split) if it["category"] == c) for c in _ACTIVE}
        assert len(set(per.values())) == 1, "%s category counts unbalanced: %s" % (split, per)


def test_composite_probe_confined_to_test_indices():
    cp = _load("composite_probe")
    assert all(it["category"] == "composite" for it in cp)
    assert _idx(cp) <= _idx(_load("test")), "composite probe leaks non-test scenarios"


def test_ids_unique_across_active_splits():
    ids = [it["id"] for s in ("train", "val", "test") for it in _load(s)]
    assert len(ids) == len(set(ids)) == 800


def test_item_schema():
    required = {"id", "category", "scenario_index", "involved_classes",
                "initial_config", "question", "ground_truth", "long_context"}
    for it in _load("test"):
        assert required <= set(it), "missing keys: %s" % (required - set(it))
        assert it["category"] in _ACTIVE
        assert isinstance(it["question"], list) and it["question"]
        assert isinstance(it["ground_truth"], list)
        # long_context flag consistent with category
        assert it["long_context"] == (it["category"] in ("long_context", "composite"))


_PINNED = os.path.join(
    REPO, "env_candidates", "gorilla", "berkeley-function-call-leaderboard",
    "bfcl_eval", "data", "BFCL_v4_multi_turn_base.json")


@pytest.mark.skipif(not os.path.exists(_PINNED), reason="pinned upstream checkout absent")
def test_split_generation_is_deterministic():
    """Regenerate from the pinned source and assert byte-identical manifests."""
    before = {s: hashlib.sha256(open(os.path.join(SPLIT_DIR, s, "items.json"), "rb").read()).hexdigest()
              for s in ("train", "val", "test", "composite_probe")}
    subprocess.run([sys.executable, os.path.join(REPO, "tools", "make_bfcl_split.py")],
                   check=True, cwd=REPO, capture_output=True)
    after = {s: hashlib.sha256(open(os.path.join(SPLIT_DIR, s, "items.json"), "rb").read()).hexdigest()
             for s in ("train", "val", "test", "composite_probe")}
    assert before == after, "manifest generation is not deterministic"
