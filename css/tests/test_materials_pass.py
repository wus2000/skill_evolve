"""End-to-end materials pass: all stages produce their persisted products and the
pass is non-fatal."""
from __future__ import annotations

import os

from css.config import CSSConfig
from css.data.tree import SearchTree, TreeNode
from css.materials import common, pass_runner
from css.tests import materials_helpers as H
from css.tree_search import BurstResult


def _cfg() -> CSSConfig:
    return CSSConfig(max_api_workers=2, screen_batch_size=10, l0_stall_steps=8)


def test_full_pass_products(tmp_path, monkeypatch):
    out = str(tmp_path)
    exploit = os.path.join(out, "nodes", "n0000", "burst_0000", "exploit")
    H.write_rollout(exploit, 0, "taskP", 0, hard=1)             # solved
    H.write_rollout(exploit, 0, "taskF", 0, hard=0, fail_reason="stuck")  # residual
    H.write_rollout(exploit, 1, "taskP", 0, hard=1)
    H.write_rollout(exploit, 1, "taskF", 0, hard=0, fail_reason="stuck")

    fake = H.FakeStage(H.default_handlers())
    monkeypatch.setattr(common, "run_json_stage", fake)

    tree = SearchTree()
    node = TreeNode(node_id="n0000", strategy="## Exploration\nScan before acting.")
    tree.add_root(node)
    br = BurstResult(node_id="n0000", burst_index=0, decision_index=0, steps=2,
                     n_accepted=1, val_before=0.4, val_after=0.5, reward=0.1,
                     exploit_dir=exploit)

    # must not raise
    pass_runner.run_materials_pass(
        tree=tree, node=node, burst_result=br, env=None,
        optimizer_client=object(), cfg=_cfg(), out_dir=out, ledger=None)

    bdir = common.analysis_burst_dir(out, "n0000", 0)
    idir = common.interpretations_dir(out, "n0000", 0)
    # Layer 1 products
    assert len([f for f in os.listdir(idir) if f.startswith("traj_")]) == 4
    assert os.path.exists(os.path.join(idir, "screen_verdicts.json"))
    # Layer 2 products
    assert os.path.exists(os.path.join(bdir, "grouping.json"))
    assert os.path.isdir(os.path.join(bdir, "group_analyses"))
    assert os.path.exists(os.path.join(bdir, "adherence_ledger.json"))
    assert os.path.exists(os.path.join(bdir, "burst_summary.md"))
    # Layer 3 living documents + diffs
    assert common.read_text(common.dossier_path(out, "n0000", "behavior_profile.md"))
    assert os.path.exists(os.path.join(bdir, "profile_update_diff.json"))
    # frontier: attribution contract + living doc, residual recorded
    attribution = common.read_json(common.dossier_path(out, "n0000", "frontier_attribution.json"))
    assert attribution and all(
        set(v.keys()) == {"attribution", "task_ids", "summary", "escalate_to_exploration"}
        for v in attribution.values())
    assert common.read_text(common.dossier_path(out, "n0000", "frontier_analysis.md"))
    # residual is the failing task only
    hist = common.read_jsonl(common.dossier_path(out, "n0000", "residual_history.jsonl"))
    assert hist[-1]["residual_task_ids"] == ["taskF"]
    # global synthesis
    assert os.path.exists(os.path.join(common.global_unsolved_dir(out), "groups.json"))


def test_pass_survives_a_failing_stage(tmp_path, monkeypatch):
    """A stage that raises is logged and does not block independent later stages."""
    out = str(tmp_path)
    exploit = os.path.join(out, "nodes", "n0000", "burst_0000", "exploit")
    H.write_rollout(exploit, 0, "taskF", 0, hard=0)

    handlers = H.default_handlers()

    def boom(user):
        raise RuntimeError("mining blew up")
    handlers["group_pass1"] = boom  # sink mining; frontier/global still run

    fake = H.FakeStage(handlers)
    monkeypatch.setattr(common, "run_json_stage", fake)
    tree = SearchTree()
    node = TreeNode(node_id="n0000", strategy="s")
    tree.add_root(node)
    br = BurstResult(node_id="n0000", burst_index=0, exploit_dir=exploit)

    # non-fatal
    pass_runner.run_materials_pass(
        tree=tree, node=node, burst_result=br, env=None,
        optimizer_client=object(), cfg=_cfg(), out_dir=out, ledger=None)

    # interpretation still happened; frontier still produced its index
    assert os.path.exists(os.path.join(common.interpretations_dir(out, "n0000", 0),
                                       "screen_verdicts.json"))
    assert common.read_json(common.dossier_path(out, "n0000", "frontier_attribution.json")) is not None
