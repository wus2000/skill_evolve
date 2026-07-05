"""NEW generation pipeline: novelty retry loop, success shape, step-granular resume.

The optimizer LLM is a scripted fake monkeypatched onto
``css.l1gen._llm.complete_optimizer_json`` (the single seam), dispatching on the
``stage`` argument. No network, no env.
"""
from __future__ import annotations

import json
import os

import pytest

from css.config import CSSConfig
from css.data.tree import SearchTree, TreeNode
from css.l1gen import _llm
from css.l1gen.new_pipeline import run_new_pipeline
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


def _new_ctx(tmp_path, cfg=None, new_id="n0001") -> SpawnContext:
    tree = SearchTree()
    root = TreeNode(node_id="n0000", branch_type="ROOT", strategy="", rules="",
                    status="saturated")
    tree.add_root(root)
    return SpawnContext(tree=tree, parent=root, mode="NEW", new_node_id=new_id,
                        cfg=cfg or _cfg(), env=None, target_client=None,
                        optimizer_client=None, out_dir=str(tmp_path),
                        decision_index=3, ledger=None)


_STRAT = ("## Reframe The Goal\n"
          "Restate the objective in your own terms before you act.\n\n"
          "## Verify Before Acting\n"
          "Check each precondition against a real observation, never an assumption.\n")

_HAPPY = {
    _llm.STAGE_NEW_TARGET: {"target_group": "multi-hop joins",
                            "why_all_paradigms_fail": "they commit before verifying",
                            "leverage": "large group"},
    _llm.STAGE_NEW_CONCEPT: {"core_behavioral_commitment": "stage the plan explicitly",
                             "how_it_differs_from_each_prior": [],
                             "expected_mechanism": "verification precedes action"},
    _llm.STAGE_NEW_NOVELTY: {"novel": True, "duplicates": "", "reason": "distinct behavior"},
    _llm.STAGE_NEW_DRAFT: {"strategy_md": _STRAT,
                           "rationale": {"target_problem": "multi-hop joins",
                                         "idea_sources": "the diagnosis",
                                         "expected_behavior_changes": ["stages the plan first"]}},
    _llm.STAGE_NEW_ALTITUDE: {"verdict": "pass"},
}


def test_new_success_zero_inheritance(tmp_path, monkeypatch):
    monkeypatch.setattr("css.l1gen._llm.complete_optimizer_json", _fake(_HAPPY))
    ctx = _new_ctx(tmp_path)
    out = run_new_pipeline(ctx)
    assert out.child is not None and not out.decline
    assert out.child.branch_type == "NEW"
    assert out.child.rules == "", "NEW children start with zero rules"
    assert "Reframe The Goal" in out.child.strategy
    # deployable original + rationale + step artifacts persisted
    assert os.path.exists(os.path.join(str(tmp_path), "nodes", "n0001", "strategy.md"))
    assert os.path.exists(os.path.join(str(tmp_path), "nodes", "n0001", "dossier", "rationale.md"))
    gd = os.path.join(str(tmp_path), "nodes", "n0001", "gen")
    for f in ("exploration_ref.json", "target_selection.json", "conception.json",
              "novelty_verdict.json", "draft.json", "altitude_check.json"):
        assert os.path.exists(os.path.join(gd, f)), f


def test_new_novelty_retry_then_fail(tmp_path, monkeypatch):
    calls = []
    script = {
        _llm.STAGE_NEW_TARGET: _HAPPY[_llm.STAGE_NEW_TARGET],
        _llm.STAGE_NEW_CONCEPT: {"core_behavioral_commitment": "c"},
        _llm.STAGE_NEW_NOVELTY: {"novel": False, "duplicates": "n0000", "reason": "same as n0000"},
    }
    monkeypatch.setattr("css.l1gen._llm.complete_optimizer_json", _fake(script, calls))
    out = run_new_pipeline(_new_ctx(tmp_path, cfg=_cfg(gen_novelty_retries=1)))
    assert out.child is None and out.decline is False   # pipeline failure, not a decline
    assert "novelty" in out.reason
    # gen_novelty_retries=1 => two conception+novelty attempts, then give up.
    assert calls.count(_llm.STAGE_NEW_CONCEPT) == 2
    assert calls.count(_llm.STAGE_NEW_NOVELTY) == 2
    assert _llm.STAGE_NEW_DRAFT not in calls


def test_new_resume_skips_persisted_steps(tmp_path, monkeypatch):
    monkeypatch.setattr("css.l1gen._llm.complete_optimizer_json", _fake(_HAPPY))
    first = run_new_pipeline(_new_ctx(tmp_path))
    assert first.child is not None

    # Second attempt at the SAME child id/out_dir: every step product exists, so a
    # fake that raises on ANY optimizer call must still rebuild the child.
    monkeypatch.setattr("css.l1gen._llm.complete_optimizer_json", _fake({}))
    second = run_new_pipeline(_new_ctx(tmp_path))
    assert second.child is not None
    assert second.child.strategy == first.child.strategy


def test_new_sources_top_priority_group_for_exploration(tmp_path, monkeypatch):
    # When the materials global-unsolved synthesis exists, NEW explores the
    # HIGHEST-priority group ahead of design (design §4.1 step 0).
    gdir = tmp_path / "global" / "unsolved"
    gdir.mkdir(parents=True)
    (gdir / "groups.json").write_text(json.dumps({
        "grp_low": {"task_ids": ["1"], "summary": "s1", "priority": 1.0},
        "grp_high": {"task_ids": ["2", "3"], "summary": "s2", "priority": 9.0},
    }))
    seen = {}

    def fake_explore(group_key, **kw):
        seen.update(group_key=group_key, tasks=list(kw["group_tasks"]), mode=kw["mode"])
        return "PROBE FINDINGS: behavioral element X cracks the group"

    monkeypatch.setattr("css.explore.api.get_or_explore", fake_explore)
    monkeypatch.setattr("css.l1gen._llm.complete_optimizer_json", _fake(_HAPPY))
    out = run_new_pipeline(_new_ctx(tmp_path))
    assert out.child is not None
    assert seen == {"group_key": "grp_high", "tasks": ["2", "3"], "mode": "NEW"}
    er = json.loads((tmp_path / "nodes" / "n0001" / "gen" / "exploration_ref.json").read_text())
    assert er["source"] == "explore" and "PROBE FINDINGS" in er["findings"]


def test_new_draft_without_sections_fails(tmp_path, monkeypatch):
    script = dict(_HAPPY)
    script[_llm.STAGE_NEW_DRAFT] = {"strategy_md": "flat prose with no ## sections",
                                    "rationale": {}}
    monkeypatch.setattr("css.l1gen._llm.complete_optimizer_json", _fake(script))
    out = run_new_pipeline(_new_ctx(tmp_path))
    assert out.child is None and out.decline is False
    assert "section" in out.reason
