"""Layer 1 interpretation: protocol fields, full-volume load, resume-skip."""
from __future__ import annotations

import os

from css.config import CSSConfig
from css.data.tree import TreeNode
from css.materials import common, interpret
from css.tests import materials_helpers as H
from css.tree_search import BurstResult


def _cfg(**kw) -> CSSConfig:
    base = dict(max_api_workers=2, screen_batch_size=10)
    base.update(kw)
    return CSSConfig(**base)


def _burst(exploit_dir: str) -> BurstResult:
    return BurstResult(node_id="n0000", burst_index=2, decision_index=3,
                       steps=5, exploit_dir=exploit_dir)


def test_interp_required_hook():
    assert interpret._interp_required({"narrative": "x", "outcome_causality": "y",
                                       "behavior_signature": "z"}) == []
    missing = interpret._interp_required({"narrative": "x"})
    assert any("outcome_causality" in m for m in missing)
    assert any("behavior_signature" in m for m in missing)
    assert interpret._interp_required("not a dict")  # non-empty -> flagged


def test_interpret_full_volume_and_protocol(tmp_path, monkeypatch):
    exploit = str(tmp_path / "nodes" / "n0000" / "burst_0002" / "exploit")
    # two steps, two tasks, k=1 -> 4 trajectories (a task recurs across steps).
    H.write_rollout(exploit, 0, "taskA", 0, hard=1)
    H.write_rollout(exploit, 0, "taskB", 0, hard=0, fail_reason="wrong")
    H.write_rollout(exploit, 1, "taskA", 0, hard=1)
    H.write_rollout(exploit, 1, "taskB", 0, hard=0, fail_reason="wrong")

    fake = H.FakeStage({"interp": H.default_interp})
    monkeypatch.setattr(common, "run_json_stage", fake)

    node = TreeNode(node_id="n0000", strategy="## Exploration\nScan first.")
    records = interpret.interpret_burst(node, _burst(exploit), object(), _cfg(), str(tmp_path))

    assert len(records) == 4, "every on-policy step rollout is interpreted (full volume)"
    assert fake.count("interp") == 4
    for r in records:
        assert set(("traj_id", "task_id", "rollout_index", "step", "passed", "interp")) <= set(r)
        it = r["interp"]
        assert it["narrative"] and it["outcome_causality"] and it["behavior_signature"]
    # step is part of trajectory identity (taskA appears in step 0 AND step 1).
    ids = {r["traj_id"] for r in records}
    assert "s0_taskA_r0" in ids and "s1_taskA_r0" in ids
    # each product persisted under interpretations/
    idir = common.interpretations_dir(str(tmp_path), "n0000", 2)
    assert len(os.listdir(idir)) == 4


def test_interpret_resume_skips_completed(tmp_path, monkeypatch):
    exploit = str(tmp_path / "nodes" / "n0000" / "burst_0002" / "exploit")
    H.write_rollout(exploit, 0, "taskA", 0, hard=1)
    H.write_rollout(exploit, 0, "taskB", 0, hard=0)

    fake = H.FakeStage({"interp": H.default_interp})
    monkeypatch.setattr(common, "run_json_stage", fake)
    node = TreeNode(node_id="n0000", strategy="s")
    r1 = interpret.interpret_burst(node, _burst(exploit), object(), _cfg(), str(tmp_path))
    assert fake.count("interp") == 2

    # Second pass: products already on disk -> no new LLM calls, same records.
    r2 = interpret.interpret_burst(node, _burst(exploit), object(), _cfg(), str(tmp_path))
    assert fake.count("interp") == 2, "resume must skip already-interpreted trajectories"
    assert {r["traj_id"] for r in r1} == {r["traj_id"] for r in r2}


def test_interp_cap(tmp_path, monkeypatch):
    exploit = str(tmp_path / "nodes" / "n0000" / "burst_0002" / "exploit")
    for k in range(6):
        H.write_rollout(exploit, 0, f"task{k}", 0, hard=0)
    fake = H.FakeStage({"interp": H.default_interp})
    monkeypatch.setattr(common, "run_json_stage", fake)
    node = TreeNode(node_id="n0000", strategy="s")
    records = interpret.interpret_burst(node, _burst(exploit), object(),
                                        _cfg(interp_max_per_burst=3), str(tmp_path))
    assert len(records) == 3, "interp_max_per_burst caps the interpreted volume"
