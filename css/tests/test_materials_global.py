"""Global unsolved synthesis: coverage-ledger union semantics + signature skip.

Rewritten for docs/L1_actions_redesign.md §1.1 — the task sets come from the
coverage ledger (union-of-evidence), no longer from per-burst residual
intersection (the AW post-mortem pathology: one node's drifting minibatch
residual emptied the intersection at decision 1 and exploration never fired).
"""
from __future__ import annotations

import os

from css.config import CSSConfig
from css.coverage import CoverageLedger, coverage_path
from css.data.tree import SearchTree, TreeNode
from css.materials import common, global_unsolved
from css.tests import materials_helpers as H


def _cfg() -> CSSConfig:
    return CSSConfig(max_api_workers=2, ledger_min_attempts=1)


def _tree() -> SearchTree:
    tree = SearchTree()
    nA = TreeNode(node_id="nA")
    nB = TreeNode(node_id="nB")
    tree.add_root(nA)
    tree.add_child("nA", nB)
    return tree


def _seed_ledger(out: str) -> CoverageLedger:
    """nA fails t1/t2/t3, solves t5; nB fails t2/t4, solves t3.
    Union-of-evidence: global_unsolved={t1,t2,t4}; paradigm_sensitive={t3};
    uncharted={t9}. (The old intersection would have said {t2} only.)"""
    led = CoverageLedger(["t1", "t2", "t3", "t4", "t5", "t9"], min_attempts=1,
                         path=coverage_path(out))
    for _ in range(2):
        led.record("nA", "t1", False)
    for _ in range(3):
        led.record("nA", "t2", False)
    led.record("nA", "t3", False)
    led.record("nA", "t5", True)
    for _ in range(2):
        led.record("nB", "t2", False)
    led.record("nB", "t4", False)
    led.record("nB", "t3", True)
    led.save()
    # Frontier narratives feed the grouping context (unchanged machinery).
    common.write_json_atomic(common.dossier_path(out, "nA", "frontier_groups.json"),
                             {"m1": {"attribution": "A", "narrative": "nA stuck",
                                     "task_ids": ["t1", "t2"]}})
    common.write_json_atomic(common.dossier_path(out, "nB", "frontier_groups.json"),
                             {"m2": {"attribution": "U", "narrative": "nB stuck",
                                     "task_ids": ["t2", "t4"]}})
    return led


def test_union_contract_and_meta(tmp_path, monkeypatch):
    out = str(tmp_path)
    tree = _tree()
    _seed_ledger(out)
    fake = H.FakeStage({
        "global_group": lambda u: {"groups": [
            {"group_key": "g_entity", "task_ids": ["t2", "t1"], "character": "stuck"}]},
        "global_reading": lambda u: {"narrative_md": "shared mechanism",
                                     "common_mechanism": True, "summary": "s",
                                     "priority": 9},
    })
    monkeypatch.setattr(common, "run_json_stage", fake)

    contract = global_unsolved.synthesize_global(tree, tree.get("nA"), object(),
                                                 _cfg(), out)

    # LLM-grouped tasks keep their group; leftovers land in global_residual.
    assert set(contract.keys()) == {"g_entity", "global_residual"}
    assert contract["g_entity"]["task_ids"] == ["t2", "t1"]
    assert contract["global_residual"]["task_ids"] == ["t4"]

    meta = common.read_json(os.path.join(common.global_unsolved_dir(out), "meta.json"))
    # Priority order: t2 (5 failed attempts) > t1 (2) > t4 (1).
    assert meta["global_unsolved_task_ids"] == ["t2", "t1", "t4"]
    assert meta["paradigm_sensitive_task_ids"] == ["t3"]
    assert meta["uncharted_task_ids"] == ["t9"]
    assert common.read_text(os.path.join(common.global_unsolved_dir(out),
                                         "group_0.md")) == "shared mechanism"


def test_signature_resume_skips_llm_until_state_flip(tmp_path, monkeypatch):
    out = str(tmp_path)
    tree = _tree()
    led = _seed_ledger(out)
    fake = H.FakeStage({
        "global_group": lambda u: {"groups": [
            {"group_key": "g", "task_ids": ["t1", "t2", "t4"], "character": "c"}]},
        "global_reading": lambda u: {"narrative_md": "m", "common_mechanism": False,
                                     "summary": "s", "priority": 1},
    })
    monkeypatch.setattr(common, "run_json_stage", fake)
    first = global_unsolved.synthesize_global(tree, tree.get("nA"), object(), _cfg(), out)
    n = fake.count("global_group")

    # More attempts WITHOUT a state flip: signature unchanged -> no LLM rerun.
    led.record("nA", "t1", False)
    led.save()
    second = global_unsolved.synthesize_global(tree, tree.get("nA"), object(), _cfg(), out)
    assert second == first
    assert fake.count("global_group") == n

    # A real solve flips t1's state -> signature changes -> synthesis reruns.
    led.record("nB", "t1", True)
    led.save()
    global_unsolved.synthesize_global(tree, tree.get("nA"), object(), _cfg(), out)
    assert fake.count("global_group") == n + 1


def test_all_solved_writes_empty_contract(tmp_path, monkeypatch):
    out = str(tmp_path)
    tree = _tree()
    led = CoverageLedger(["t1", "t2"], min_attempts=1, path=coverage_path(out))
    led.record("nA", "t1", True)
    led.record("nA", "t2", False)
    led.record("nB", "t2", True)
    led.save()
    fake = H.FakeStage({})  # must not be called: nothing globally unsolved
    monkeypatch.setattr(common, "run_json_stage", fake)
    contract = global_unsolved.synthesize_global(tree, tree.get("nA"), object(),
                                                 _cfg(), out)
    assert contract == {}
    meta = common.read_json(os.path.join(common.global_unsolved_dir(out), "meta.json"))
    assert meta["global_unsolved_task_ids"] == []
    assert meta["paradigm_sensitive_task_ids"] == ["t2"]
    assert meta["uncharted_task_ids"] == []


def test_empty_ledger_skips_synthesis(tmp_path, monkeypatch):
    out = str(tmp_path)
    tree = _tree()
    fake = H.FakeStage({})
    monkeypatch.setattr(common, "run_json_stage", fake)
    contract = global_unsolved.synthesize_global(tree, tree.get("nA"), object(),
                                                 _cfg(), out)
    assert contract == {}
    meta = common.read_json(os.path.join(common.global_unsolved_dir(out), "meta.json"))
    assert meta["source"] == "no_coverage_data"
