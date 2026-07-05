"""Layer 2 mining: two-pass grouping, partition normalization, adherence tally."""
from __future__ import annotations

import os

from css.config import CSSConfig
from css.materials import common, mining
from css.tests import materials_helpers as H


def _cfg(**kw) -> CSSConfig:
    base = dict(max_api_workers=2)
    base.update(kw)
    return CSSConfig(**base)


def _records():
    return [
        H.interp_record("t1", "taskA", True, signature="explore"),
        H.interp_record("t2", "taskB", True, signature="explore"),
        H.interp_record("t3", "taskC", False, signature="mixed"),
        H.interp_record("t4", "taskD", False, signature="commit"),
    ]


def test_two_pass_grouping_reassigns_boundary(tmp_path, monkeypatch):
    records = _records()
    by_id = {r["traj_id"]: r for r in records}
    fake = H.FakeStage({
        "group_pass1": lambda u: {"groups": [
            {"group_key": "A", "rationale": "explore-first mode",
             "member_traj_ids": ["t1", "t2", "t3"], "representative_traj_ids": ["t1"],
             "uncertain_traj_ids": ["t3"]},
            {"group_key": "B", "rationale": "commit-early mode",
             "member_traj_ids": ["t4"], "representative_traj_ids": ["t4"],
             "uncertain_traj_ids": []}]},
        # pass 2 reads t3's full narrative and moves it A -> B
        "group_pass2": lambda u: [{"traj_id": "t3", "group_key": "B"}],
    })
    monkeypatch.setattr(common, "run_json_stage", fake)

    path = str(tmp_path / "grouping.json")
    grouping = mining._group_two_pass(records, by_id, path, object(), _cfg())

    groups = {g["group_key"]: g for g in grouping["groups"]}
    assert groups["A"]["member_traj_ids"] == ["t1", "t2"]
    assert set(groups["B"]["member_traj_ids"]) == {"t4", "t3"}
    assert grouping["pass2_reassignments"] == [{"traj_id": "t3", "from": "A", "to": "B"}]
    assert fake.count("group_pass1") == 1 and fake.count("group_pass2") == 1
    assert os.path.exists(path)

    # resume: cached grouping is reused, no new calls
    again = mining._group_two_pass(records, by_id, path, object(), _cfg())
    assert again == grouping
    assert fake.count("group_pass1") == 1


def test_normalize_enforces_partition():
    all_ids = ["t1", "t2", "t3", "t4"]
    # t2 duplicated across groups (first wins); t4 left out entirely (-> unclustered)
    raw = [
        {"group_key": "A", "member_traj_ids": ["t1", "t2"]},
        {"group_key": "B", "member_traj_ids": ["t2", "t3"]},
    ]
    norm = mining._normalize_groups(raw, all_ids)
    members = {g["group_key"]: g["member_traj_ids"] for g in norm}
    assert members["A"] == ["t1", "t2"]
    assert members["B"] == ["t3"]  # t2 deduped to first group
    assert "unclustered" in members and members["unclustered"] == ["t4"]
    # every id assigned exactly once
    flat = [m for g in norm for m in g["member_traj_ids"]]
    assert sorted(flat) == all_ids and len(flat) == len(set(flat))


def test_mechanical_adherence_counts():
    records = [
        H.interp_record("t1", "a", True, adherence=[
            {"section": "Exploration", "verdict": "followed", "note": "scanned"},
            {"section": "Commitment", "verdict": "ignored", "note": "never committed"}]),
        H.interp_record("t2", "b", False, adherence=[
            {"section": "Exploration", "verdict": "partial", "note": "half scan"},
            {"section": "Exploration", "verdict": "followed", "note": ""}]),
    ]
    sections = mining._mechanical_adherence(records)
    exp = sections["Exploration"]
    assert exp["followed"] == 2 and exp["partial"] == 1 and exp["ignored"] == 0
    assert set(exp["evidence_traj_ids"]) <= {"t1", "t2"}
    assert sections["Commitment"]["ignored"] == 1
