"""Layer 1 interpretation (two-pass): prose pass, extract pass, split, resume."""
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


# ── unit: split / echo-validation / required hook ────────────────────────────
def test_split_prose_lossless_three_fields():
    parts = interpret._split_prose(H.DEFAULT_PROSE)
    assert "explored broadly" in parts["narrative"]
    assert "Exploration: followed" in parts["narrative"]
    assert parts["outcome_causality"].startswith("Broad exploration")
    assert parts["anomalies"] == ""          # "None observed." normalizes to empty


def test_split_prose_without_headings_keeps_everything_in_narrative():
    prose = "A free reading with no headings at all; every word must survive."
    parts = interpret._split_prose(prose)
    assert parts["narrative"] == prose
    assert parts["outcome_causality"] == "" and parts["anomalies"] == ""


def test_clean_adherence_echo_validation():
    kept, dropped = interpret._clean_adherence(
        [{"section": "Exploration", "verdict": "followed", "evidence_steps": [1]},
         {"section": "Phantom Section", "verdict": "followed"},
         {"section": "Exploration", "verdict": "not-a-verdict"}],
        ["Exploration"])
    assert len(kept) == 1 and dropped == 2
    assert kept[0]["section"] == "Exploration"


def test_extract_required_hook():
    assert interpret._extract_required({"behavior_signature": "x"}) == []
    assert interpret._extract_required({"behavior_signature": ""})
    assert interpret._extract_required("not a dict")


# ── integration: full volume, protocol, resume, cap ──────────────────────────
def test_interpret_full_volume_and_protocol(tmp_path, monkeypatch):
    exploit = str(tmp_path / "nodes" / "n0000" / "burst_0002" / "exploit")
    # two steps, two tasks, k=1 -> 4 trajectories (a task recurs across steps).
    H.write_rollout(exploit, 0, "taskA", 0, hard=1)
    H.write_rollout(exploit, 0, "taskB", 0, hard=0, fail_reason="wrong")
    H.write_rollout(exploit, 1, "taskA", 0, hard=1)
    H.write_rollout(exploit, 1, "taskB", 0, hard=0, fail_reason="wrong")

    fake = H.FakeStage({"interp_extract": H.default_extract})
    monkeypatch.setattr(common, "run_json_stage", fake)
    client = H.ProseClient()

    node = TreeNode(node_id="n0000", strategy="## Exploration\nScan first.")
    records = interpret.interpret_burst(node, _burst(exploit), client, _cfg(), str(tmp_path))

    assert len(records) == 4, "every on-policy step rollout is interpreted (full volume)"
    assert client.calls == 4, "one prose pass per trajectory"
    assert fake.count("interp_extract") == 4, "one extract pass per trajectory"
    for r in records:
        assert set(("traj_id", "task_id", "rollout_index", "step", "passed",
                    "interp", "interp_prose")) <= set(r)
        it = r["interp"]
        assert it["narrative"] and it["outcome_causality"]
        assert it["behavior_signature"] == "explore-then-commit"
        assert it["adherence"] and it["adherence"][0]["section"] == "Exploration"
        # Retired fields must not reappear.
        assert "strategy_signals" not in it and "task_group_hint" not in it \
            and "key_steps" not in it
        assert r["interp_prose"] == H.DEFAULT_PROSE.strip()
    ids = {r["traj_id"] for r in records}
    assert "s0_taskA_r0" in ids and "s1_taskA_r0" in ids
    idir = common.interpretations_dir(str(tmp_path), "n0000", 2)
    assert len(os.listdir(idir)) == 4


def test_extract_failure_keeps_prose_fields(tmp_path, monkeypatch):
    exploit = str(tmp_path / "nodes" / "n0000" / "burst_0002" / "exploit")
    H.write_rollout(exploit, 0, "taskA", 0, hard=1)
    fake = H.FakeStage({"interp_extract": lambda u: {}})   # extract yields nothing
    monkeypatch.setattr(common, "run_json_stage", fake)
    client = H.ProseClient()
    node = TreeNode(node_id="n0000", strategy="## Exploration\nScan first.")
    records = interpret.interpret_burst(node, _burst(exploit), client, _cfg(), str(tmp_path))
    it = records[0]["interp"]
    assert it["_extract_error"], "honest failure marker"
    assert it["narrative"], "prose-derived fields survive an extract failure"
    assert it["behavior_signature"] == "" and it["adherence"] == []
    assert records[0]["interp_prose"] == H.DEFAULT_PROSE.strip()


def test_interpret_resume_skips_completed(tmp_path, monkeypatch):
    exploit = str(tmp_path / "nodes" / "n0000" / "burst_0002" / "exploit")
    H.write_rollout(exploit, 0, "taskA", 0, hard=1)
    H.write_rollout(exploit, 0, "taskB", 0, hard=0)

    fake = H.FakeStage({"interp_extract": H.default_extract})
    monkeypatch.setattr(common, "run_json_stage", fake)
    client = H.ProseClient()
    node = TreeNode(node_id="n0000", strategy="s")
    r1 = interpret.interpret_burst(node, _burst(exploit), client, _cfg(), str(tmp_path))
    assert client.calls == 2

    r2 = interpret.interpret_burst(node, _burst(exploit), client, _cfg(), str(tmp_path))
    assert client.calls == 2, "resume must skip already-interpreted trajectories"
    assert {r["traj_id"] for r in r1} == {r["traj_id"] for r in r2}


def test_interp_cap(tmp_path, monkeypatch):
    exploit = str(tmp_path / "nodes" / "n0000" / "burst_0002" / "exploit")
    for k in range(6):
        H.write_rollout(exploit, 0, f"task{k}", 0, hard=0)
    fake = H.FakeStage({"interp_extract": H.default_extract})
    monkeypatch.setattr(common, "run_json_stage", fake)
    node = TreeNode(node_id="n0000", strategy="s")
    records = interpret.interpret_burst(node, _burst(exploit), H.ProseClient(),
                                        _cfg(interp_max_per_burst=3), str(tmp_path))
    assert len(records) == 3, "interp_max_per_burst caps the interpreted volume"
