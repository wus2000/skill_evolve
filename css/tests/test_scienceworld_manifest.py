"""Validation of the generated ScienceWorld split manifests.

Skipped when the manifests have not been generated
(tools/make_scienceworld_split.py). When present, asserts the invariants the
mechanism relies on: 90 uniquely-mapped AgentBoard instances, stable unique ids,
no (task, variation) leakage between train and the test splits, and a well-formed
schema per protocol.
"""
from __future__ import annotations

import json
import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SPLIT_DIR = os.path.join(REPO, "data", "scienceworld_split_seed42")


def _load(name):
    path = os.path.join(SPLIT_DIR, name, "items.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


_HAVE = os.path.exists(os.path.join(SPLIT_DIR, "test_agentboard", "items.json"))
pytestmark = pytest.mark.skipif(not _HAVE, reason="scienceworld manifests not generated")


def _pairs(items):
    return {(i["task_name"], i["variation"]) for i in items}


def test_agentboard_90_unique_mapping():
    ab = _load("test_agentboard")
    assert len(ab) == 90, "expected 90 AgentBoard instances, got %d" % len(ab)
    ids = [i["id"] for i in ab]
    assert len(ids) == len(set(ids)), "duplicate AgentBoard ids"
    # every instance maps to exactly one (task, variation)
    assert len(_pairs(ab)) == 90, "AgentBoard instances not uniquely mapped to (task,var)"
    for i in ab:
        assert i["protocol"] == "agentboard"
        assert i["task_name"] and isinstance(i["variation"], int)
        assert i["goal"] and isinstance(i["subgoals"], list) and i["subgoals"]
        assert i["difficulty"] in ("easy", "hard")


def test_native_splits_schema_and_ids():
    for name in ("train", "val", "test_secondary"):
        items = _load(name)
        assert items, "%s manifest empty" % name
        ids = [i["id"] for i in items]
        assert len(ids) == len(set(ids)), "duplicate ids in %s" % name
        for i in items:
            assert i["protocol"] == "original"
            assert i["task_name"] and isinstance(i["variation"], int)


def test_no_train_test_leakage():
    train, val = _pairs(_load("train")), _pairs(_load("val"))
    ab, sec = _pairs(_load("test_agentboard")), _pairs(_load("test_secondary"))
    assert not (train & sec), "train/secondary (task,var) overlap"
    assert not (train & ab), "train/AgentBoard overlap"
    assert not (val & ab), "val/AgentBoard overlap"
    assert not (sec & ab), "secondary/AgentBoard overlap"


def test_same_task_universe():
    universe = {i["task_name"] for i in _load("test_agentboard")}
    for name in ("train", "val", "test_secondary"):
        tasks = {i["task_name"] for i in _load(name)}
        assert tasks <= universe, "%s draws from outside the AgentBoard task universe: %s" % (
            name, tasks - universe)


def test_sizes_in_expected_ranges():
    assert 150 <= len(_load("train")) <= 400
    assert 40 <= len(_load("val")) <= 150
    assert 80 <= len(_load("test_secondary")) <= 220


def _scienceworld_importable():
    try:
        import scienceworld  # noqa: F401
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _scienceworld_importable(),
                    reason="scienceworld (JVM) not installed in this interpreter")
def test_subgoal_matcher_reproduces_pr_on_real_replay():
    """A recovered AgentBoard instance, replayed via ScienceWorld's gold path under
    the AgentBoard simplification, must drive its SubgoalMatcher to PR==1.0 — this
    is what proves the var-recovery is faithful AND the matcher is protocol-correct
    on a REAL episode (not just hand-built streams)."""
    from scienceworld import ScienceWorldEnv
    from css.envs.scienceworld.scoring import SubgoalMatcher

    simpl = "selfWateringFlowerPots,openContainers,openDoors,noElectricalAction"
    ab = _load("test_agentboard")
    # pick a short-gold instance for a fast test (lifespan gold is ~7 steps)
    inst = next((i for i in ab if i["task_name"] == "lifespan-longest-lived"), ab[0])
    env = ScienceWorldEnv(taskName="", envStepLimit=120)
    try:
        env.load(inst["task_name"], inst["variation"], simpl, generateGoldPath=True)
        env.reset()
        gold = env.get_gold_action_sequence()
        assert gold and not str(gold[0]).startswith("ERROR")
        matcher = SubgoalMatcher(inst["subgoals"])
        for act in gold:
            obs, _, done, _ = env.step(act)
            matcher.update(obs)
            if done:
                break
        assert matcher.sr == 1 and matcher.pr == 1.0, (
            "recovered instance %s did not reproduce all subgoals (PR=%.2f)"
            % (inst["id"], matcher.pr))
    finally:
        env.close()


def test_gold_actions_cover_manifest():
    path = os.path.join(SPLIT_DIR, "gold_actions.json")
    if not os.path.exists(path):
        pytest.skip("gold_actions.json not generated")
    with open(path, encoding="utf-8") as f:
        gold = json.load(f)
    # every AgentBoard variation should have a gold action list (possibly empty
    # only if generation genuinely failed — assert coverage of the KEYS).
    ab = _load("test_agentboard")
    missing = [(i["task_name"], i["variation"]) for i in ab
               if "%s::%d" % (i["task_name"], i["variation"]) not in gold]
    assert not missing, "gold_actions.json missing AgentBoard keys: %s" % missing[:5]
