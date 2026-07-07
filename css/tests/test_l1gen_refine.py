"""REFINE controlled-edit pipeline: decline path, keep-list enforcement, controlled
apply byte-identity, and rules-inheritance adjudication.

Optimizer LLM is a scripted fake on ``css.l1gen._llm.complete_optimizer_json``
dispatching on ``stage``. ``css.explore`` is absent, so the U-group exploration
call returns empty findings (the decline path relies on this).
"""
from __future__ import annotations

import json
import os

import pytest

from css.config import CSSConfig
from css.data.tree import SearchTree, TreeNode
from css.l1gen import _llm
from css.l1gen.refine_pipeline import _assemble_rules, run_refine_pipeline
from css.l1gen.sections import raw_section_text
from css.tree_search import SpawnContext


def _cfg(**kw) -> CSSConfig:
    base = dict(n_train=4, n_val=2, n_test=2, burst_steps=5, l0_stall_steps=8, N=5,
                node_degree=3, max_decisions=8, test_eval_on_new_best=False,
                alpha=0.5, beta=0.5, W=10, gen_novelty_retries=2)
    base.update(kw)
    return CSSConfig(**base)


def _fake(script, calls=None):
    def fn(client, system, user, *, parse=None, ok=None, required=None,
           max_tokens=4096, repair_max_tokens=16384, stage=""):
        if calls is not None:
            calls.append(stage)
        if stage not in script:
            raise AssertionError("unexpected optimizer stage: %r" % stage)
        v = script[stage]
        return v(system, user) if callable(v) else v
    return fn


_PARENT = ("## Ground Every Claim\n"
           "Read before you write.\n\n"
           "## Decompose Before Committing\n"
           "Break the task into parts.\n\n"
           "## Keep A Ledger\n"
           "Track what you have already done.\n")
_RULES = "- old rule 1\n- old rule 2\n- old rule 3\n"


def _refine_ctx(tmp_path, cfg=None, new_id="n0002",
                parent_strategy=_PARENT, parent_rules=_RULES) -> SpawnContext:
    tree = SearchTree()
    root = TreeNode(node_id="n0000", branch_type="ROOT", strategy="", rules="")
    tree.add_root(root)
    parent = TreeNode(node_id="n0001", branch_type="NEW", strategy=parent_strategy,
                      rules=parent_rules, best_rules="", status="saturated")
    tree.add_child("n0000", parent)
    return SpawnContext(tree=tree, parent=parent, mode="REFINE", new_node_id=new_id,
                        cfg=cfg or _cfg(), env=None, target_client=None,
                        optimizer_client=None, out_dir=str(tmp_path),
                        decision_index=5, ledger=None)


_REFINE_HAPPY = {
    _llm.STAGE_REFINE_CAUSE: {
        "has_target": True, "target_type": "A", "target_group": "g1",
        "cause": "missing staging mechanism",
        "sections_implicated": ["Decompose Before Committing"],
        "keep_list": ["Keep A Ledger"], "no_target_reason": ""},
    _llm.STAGE_REFINE_PLAN: {"ops": [{
        "op": "replace_section", "section": "Decompose Before Committing",
        "content": "## Decompose Before Committing\nStage a plan, then commit part by part.\n",
        "rationale": "addresses the A cause"}]},
    _llm.STAGE_REFINE_CONFRONT: {"proceed": True, "reason": "scoped to the A cause",
                                 "which_ops": []},
    _llm.STAGE_SCREEN: [{"index": 0, "verdict": "pass", "violated_criteria": [],
                         "feedback": "", "quoted_offense": ""}],
    _llm.STAGE_REFINE_COHERENCE: {"items": []},
    _llm.STAGE_REFINE_RATIONALE: {"target_problem": "g1", "idea_sources": "the cause",
                                  "expected_behavior_changes": ["stages the plan"]},
    _llm.STAGE_REFINE_ALTITUDE: {"verdict": "pass"},
    _llm.STAGE_REFINE_INHERIT: [
        {"rule_excerpt": "- old rule 1", "verdict": "keep", "reason": "still applies"},
        {"rule_excerpt": "- old rule 2", "verdict": "rewrite",
         "rewritten": "- reworded rule 2", "reason": "worded against old strategy"},
        {"rule_excerpt": "- old rule 3", "verdict": "drop", "reason": "patched the cured disease"}],
}


def test_refine_success_controlled_edit_and_inheritance(tmp_path, monkeypatch):
    monkeypatch.setattr("css.l1gen._llm.complete_optimizer_json", _fake(_REFINE_HAPPY))
    out = run_refine_pipeline(_refine_ctx(tmp_path))
    assert out.child is not None and not out.decline
    assert out.child.branch_type == "REFINE"
    assert "Stage a plan" in out.child.strategy
    # keep-list section byte-identical parent<->child (controlled-experiment guarantee)
    assert raw_section_text(out.child.strategy, "Keep A Ledger") == \
        raw_section_text(_PARENT, "Keep A Ledger")
    assert raw_section_text(out.child.strategy, "Ground Every Claim") == \
        raw_section_text(_PARENT, "Ground Every Claim")
    # inheritance assembly: keep + rewrite survive (in order), drop omitted
    assert out.child.rules.strip().splitlines() == ["- old rule 1", "- reworded rule 2"]
    gd = os.path.join(str(tmp_path), "nodes", "n0002", "gen")
    for f in ("cause_confirmation.json", "edit_plan.json", "confrontation.json",
              "applied.json", "coherence_diff.json", "rationale.json",
              "altitude_check.json", "inherit_decisions.json"):
        assert os.path.exists(os.path.join(gd, f)), f


def test_refine_decline_when_no_ab_target(tmp_path, monkeypatch):
    calls = []
    # cause confirmation returns "no target" BOTH times (before and after the
    # U-group exploration, which returns empty because css.explore is absent).
    script = {_llm.STAGE_REFINE_CAUSE: {
        "has_target": False, "target_type": "",
        "no_target_reason": "the approach is sound; failures are local execution"}}
    monkeypatch.setattr("css.l1gen._llm.complete_optimizer_json", _fake(script, calls))
    out = run_refine_pipeline(_refine_ctx(tmp_path))
    assert out.child is None and out.decline is True   # honest no-junk-child path
    assert "no defensible A/B target" in out.reason
    assert calls.count(_llm.STAGE_REFINE_CAUSE) == 2, "must retry after exploring U groups"


def test_refine_explores_escalated_u_group_before_decline(tmp_path, monkeypatch):
    # A U group flagged escalate_to_exploration -> REFINE probes it before deciding
    # to decline (design §4.2: re-confirm cause WITH findings first).
    dd = tmp_path / "nodes" / "n0001" / "dossier"
    dd.mkdir(parents=True)
    (dd / "frontier_attribution.json").write_text(json.dumps({
        "u_grp": {"attribution": "U", "task_ids": ["7", "8"], "summary": "resists",
                  "escalate_to_exploration": True}}))
    seen = {}

    def fake_explore(group_key, **kw):
        # P2 contract: group_tasks arrive as RESOLVED item dicts.
        tasks = [t.get("task_id") for t in kw["group_tasks"]]
        seen.update(group_key=group_key, mode=kw["mode"], tasks=tasks)
        return "probe found no missing mechanism"

    monkeypatch.setattr("css.explore.api.get_or_explore", fake_explore)
    # cause returns no A/B target BOTH times -> explores the U group -> still declines.
    monkeypatch.setattr("css.l1gen._llm.complete_optimizer_json",
                        _fake({_llm.STAGE_REFINE_CAUSE: {"has_target": False,
                                                         "no_target_reason": "sound approach"}}))
    out = run_refine_pipeline(_refine_ctx(tmp_path))
    assert out.child is None and out.decline is True
    assert seen == {"group_key": "u_grp", "mode": "REFINE", "tasks": ["7", "8"]}


def test_refine_keep_list_violation_is_caught(tmp_path, monkeypatch):
    # Plan targets a keep-list section -> mechanical check rejects it BEFORE any
    # confront/screen call; with zero retries the spawn fails (not a decline).
    script = {
        _llm.STAGE_REFINE_CAUSE: {
            "has_target": True, "target_type": "A", "cause": "x",
            "sections_implicated": ["Keep A Ledger"], "keep_list": ["Keep A Ledger"]},
        _llm.STAGE_REFINE_PLAN: {"ops": [{
            "op": "replace_section", "section": "Keep A Ledger",
            "content": "## Keep A Ledger\nhijacked body\n", "rationale": "bad"}]},
    }
    monkeypatch.setattr("css.l1gen._llm.complete_optimizer_json", _fake(script))
    out = run_refine_pipeline(_refine_ctx(tmp_path, cfg=_cfg(gen_novelty_retries=0)))
    assert out.child is None and out.decline is False
    assert "keep-list" in out.reason.lower()


def test_refine_inherit_empty_decisions_falls_back_to_full_inherit(tmp_path, monkeypatch):
    script = dict(_REFINE_HAPPY)
    script[_llm.STAGE_REFINE_INHERIT] = []   # unparseable/empty -> conservative full inherit
    monkeypatch.setattr("css.l1gen._llm.complete_optimizer_json", _fake(script))
    out = run_refine_pipeline(_refine_ctx(tmp_path))
    assert out.child is not None
    assert out.child.rules.strip() == _RULES.strip(), "full inherit on inheritance noise"


def test_refine_resume_skips_persisted_steps(tmp_path, monkeypatch):
    monkeypatch.setattr("css.l1gen._llm.complete_optimizer_json", _fake(_REFINE_HAPPY))
    first = run_refine_pipeline(_refine_ctx(tmp_path))
    assert first.child is not None
    monkeypatch.setattr("css.l1gen._llm.complete_optimizer_json", _fake({}))
    second = run_refine_pipeline(_refine_ctx(tmp_path))
    assert second.child is not None
    assert second.child.strategy == first.child.strategy
    assert second.child.rules == first.child.rules


# ── inheritance assembly (unit) ──────────────────────────────────────────────
def test_assemble_rules_keep_drop_rewrite_order():
    decisions = [
        {"rule_excerpt": "- a", "verdict": "keep"},
        {"rule_excerpt": "- b", "verdict": "rewrite", "rewritten": "- B2"},
        {"rule_excerpt": "- c", "verdict": "drop"},
        {"rule_excerpt": "- d", "verdict": "keep"},
    ]
    out = _assemble_rules(decisions)
    assert out.strip().splitlines() == ["- a", "- B2", "- d"]


def test_assemble_rules_all_dropped_is_empty():
    assert _assemble_rules([{"rule_excerpt": "- a", "verdict": "drop"}]) == ""
