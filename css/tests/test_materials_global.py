"""Global unsolved: mechanical cross-node intersection + signature resume-skip."""
from __future__ import annotations

import os

from css.config import CSSConfig
from css.data.tree import SearchTree, TreeNode
from css.materials import common, global_unsolved
from css.tests import materials_helpers as H


def _cfg() -> CSSConfig:
    return CSSConfig(max_api_workers=2)


def _tree_with_residuals(out: str):
    tree = SearchTree()
    nA = TreeNode(node_id="nA")
    nB = TreeNode(node_id="nB")
    tree.add_root(nA)
    tree.add_child("nA", nB)
    common.append_jsonl(common.dossier_path(out, "nA", "residual_history.jsonl"),
                        {"burst": 0, "residual_task_ids": ["t1", "t2", "t3"]})
    common.append_jsonl(common.dossier_path(out, "nB", "residual_history.jsonl"),
                        {"burst": 0, "residual_task_ids": ["t2", "t3", "t4"]})
    common.write_json_atomic(common.dossier_path(out, "nA", "frontier_groups.json"),
                             {"m1": {"attribution": "A", "narrative": "nA fails t2/t3",
                                     "task_ids": ["t2", "t3"]}})
    common.write_json_atomic(common.dossier_path(out, "nB", "frontier_groups.json"),
                             {"m2": {"attribution": "U", "narrative": "nB fails t2/t3",
                                     "task_ids": ["t2", "t3", "t4"]}})
    return tree, nA


def test_intersection_and_contract(tmp_path, monkeypatch):
    out = str(tmp_path)
    tree, node = _tree_with_residuals(out)
    fake = H.FakeStage({
        "global_group": lambda u: {"groups": [
            {"group_key": "g_common", "task_ids": ["t2", "t3"], "character": "both stuck"}]},
        "global_reading": lambda u: {"narrative_md": "same mechanism",
                                     "common_mechanism": True, "summary": "common",
                                     "priority": 9},
    })
    monkeypatch.setattr(common, "run_json_stage", fake)

    contract = global_unsolved.synthesize_global(tree, node, object(), _cfg(), out)

    assert set(contract.keys()) == {"g_common"}
    entry = contract["g_common"]
    assert set(entry.keys()) == {"task_ids", "summary", "priority", "common_mechanism"}
    assert entry["task_ids"] == ["t2", "t3"]
    assert entry["priority"] == 9.0 and entry["common_mechanism"] is True

    meta = common.read_json(os.path.join(common.global_unsolved_dir(out), "meta.json"))
    assert meta["global_hard_task_ids"] == ["t2", "t3"]
    assert meta["paradigm_sensitive_task_ids"] == ["t1", "t4"]
    # k-th group narrative persisted
    assert common.read_text(os.path.join(common.global_unsolved_dir(out), "group_0.md")) \
        == "same mechanism"


def test_signature_resume_skips_llm(tmp_path, monkeypatch):
    out = str(tmp_path)
    tree, node = _tree_with_residuals(out)
    fake = H.FakeStage({
        "global_group": lambda u: {"groups": [
            {"group_key": "g", "task_ids": ["t2", "t3"], "character": "c"}]},
        "global_reading": lambda u: {"narrative_md": "m", "common_mechanism": False,
                                     "summary": "s", "priority": 1},
    })
    monkeypatch.setattr(common, "run_json_stage", fake)
    first = global_unsolved.synthesize_global(tree, node, object(), _cfg(), out)
    n = fake.count("global_group")
    # unchanged residual landscape -> signature match -> no new LLM work
    second = global_unsolved.synthesize_global(tree, node, object(), _cfg(), out)
    assert second == first
    assert fake.count("global_group") == n


def test_no_global_hard_when_disjoint(tmp_path, monkeypatch):
    out = str(tmp_path)
    tree = SearchTree()
    tree.add_root(TreeNode(node_id="nA"))
    tree.add_child("nA", TreeNode(node_id="nB"))
    common.append_jsonl(common.dossier_path(out, "nA", "residual_history.jsonl"),
                        {"burst": 0, "residual_task_ids": ["t1"]})
    common.append_jsonl(common.dossier_path(out, "nB", "residual_history.jsonl"),
                        {"burst": 0, "residual_task_ids": ["t2"]})
    fake = H.FakeStage({})  # must not be called: intersection is empty
    monkeypatch.setattr(common, "run_json_stage", fake)
    contract = global_unsolved.synthesize_global(tree, tree.get("nA"), object(), _cfg(), out)
    assert contract == {}
    meta = common.read_json(os.path.join(common.global_unsolved_dir(out), "meta.json"))
    assert meta["global_hard_task_ids"] == []
    assert meta["paradigm_sensitive_task_ids"] == ["t1", "t2"]
