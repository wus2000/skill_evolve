"""Frontier: residual grouping, A/B/U attribution, escalation, and the exact
frontier_attribution.json contract shape."""
from __future__ import annotations

from css.config import CSSConfig
from css.data.step_buffer import StepBufferEntry
from css.data.tree import TreeNode
from css.materials import common, frontier
from css.tests import materials_helpers as H
from css.tree_search import BurstResult


def _cfg() -> CSSConfig:
    return CSSConfig(max_api_workers=2, l0_stall_steps=8)


def _stalled_node() -> TreeNode:
    node = TreeNode(node_id="n0000", strategy="s")
    for i in range(8):
        node.step_buffer.append(StepBufferEntry(step=i, action="reject",
                                                score_before=0.0, score_after=0.0))
    return node


def _records():
    # hardtask never solved (residual); easytask solved
    return [H.interp_record("s0_hardtask_r0", "hardtask", False),
            H.interp_record("s0_easytask_r0", "easytask", True)]


def _base_fake(narrative_resp):
    return H.FakeStage({
        "frontier_group": lambda u: {"groups": [
            {"group_key": "mode_stuck", "rationale": "stuck on hardtask",
             "task_ids": ["hardtask"]}]},
        "frontier_narrative": narrative_resp,
        "frontier_integrate": lambda u: {"document_md": "## Frontier\nmode_stuck.\n"
                                         "## Evolution log\n- burst"},
        "frontier_audit": lambda u: {"ledger": [], "unaccounted": []},
    })


def test_attribution_contract_and_persistent_U_escalation(tmp_path, monkeypatch):
    out = str(tmp_path)
    # prior burst had this mode as U -> persistent U
    common.write_json_atomic(
        common.dossier_path(out, "n0000", "frontier_groups.json"),
        {"mode_stuck": {"attribution": "U", "narrative": "prev", "task_ids": ["hardtask"],
                        "first_burst": 0, "last_burst": 0}})

    # LLM flag is False; escalation must still fire from persistent-U + stalled
    fake = _base_fake(lambda u: {"narrative": "behavior cannot reach goal",
                                 "attribution": "U", "attribution_rationale": "no cause",
                                 "escalate_to_exploration": False})
    monkeypatch.setattr(common, "run_json_stage", fake)

    res = frontier.update_frontier(_stalled_node(), BurstResult(node_id="n0000", burst_index=1),
                                   _records(), {"adherence": {"reading": "r"}}, object(), _cfg(), out)

    attribution = common.read_json(common.dossier_path(out, "n0000", "frontier_attribution.json"))
    assert set(attribution.keys()) == {"mode_stuck"}
    entry = attribution["mode_stuck"]
    # exact CONTRACT shape
    assert set(entry.keys()) == {"attribution", "task_ids", "summary", "escalate_to_exploration"}
    assert entry["attribution"] == "U"
    assert entry["task_ids"] == ["hardtask"]
    assert entry["escalate_to_exploration"] is True
    assert res["attribution"] == attribution
    # residual history recorded
    hist = common.read_jsonl(common.dossier_path(out, "n0000", "residual_history.jsonl"))
    assert hist and hist[-1]["residual_task_ids"] == ["hardtask"]
    # living document written
    assert common.read_text(common.dossier_path(out, "n0000", "frontier_analysis.md"))


def test_U_without_persistence_or_stall_does_not_escalate(tmp_path, monkeypatch):
    out = str(tmp_path)
    fake = _base_fake(lambda u: {"narrative": "local execution slip", "attribution": "U",
                                 "attribution_rationale": "insufficient evidence",
                                 "escalate_to_exploration": False})
    monkeypatch.setattr(common, "run_json_stage", fake)
    # fresh node (not stalled), no prior groups
    res = frontier.update_frontier(TreeNode(node_id="n0000", strategy="s"),
                                   BurstResult(node_id="n0000", burst_index=0),
                                   _records(), {"adherence": {"reading": "r"}}, object(), _cfg(), out)
    assert res["attribution"]["mode_stuck"]["escalate_to_exploration"] is False


def test_A_attribution_never_escalates(tmp_path, monkeypatch):
    out = str(tmp_path)
    # even with the LLM flag true, an A verdict is a REFINE target, not an escalation
    fake = _base_fake(lambda u: {"narrative": "strategy lacks a mechanism",
                                 "attribution": "A", "attribution_rationale": "missing",
                                 "escalate_to_exploration": True})
    monkeypatch.setattr(common, "run_json_stage", fake)
    res = frontier.update_frontier(_stalled_node(), BurstResult(node_id="n0000", burst_index=1),
                                   _records(), {"adherence": {"reading": "r"}}, object(), _cfg(), out)
    entry = res["attribution"]["mode_stuck"]
    assert entry["attribution"] == "A"
    assert entry["escalate_to_exploration"] is False


def test_no_residuals_writes_empty_attribution(tmp_path, monkeypatch):
    out = str(tmp_path)
    fake = _base_fake(lambda u: {"narrative": "n", "attribution": "U",
                                 "attribution_rationale": "r", "escalate_to_exploration": False})
    monkeypatch.setattr(common, "run_json_stage", fake)
    # all tasks solved -> no residual
    records = [H.interp_record("s0_easy_r0", "easy", True)]
    res = frontier.update_frontier(TreeNode(node_id="n0000", strategy="s"),
                                   BurstResult(node_id="n0000", burst_index=0),
                                   records, {"adherence": {"reading": "r"}}, object(), _cfg(), out)
    assert res["attribution"] == {}
    assert common.read_json(common.dossier_path(out, "n0000", "frontier_attribution.json")) == {}
    assert fake.count("frontier_group") == 0  # nothing to group
