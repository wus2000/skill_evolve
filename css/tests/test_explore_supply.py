"""P2 exploration supply lines (docs/L1_actions_redesign.md §4-5, §7).

Covers: id -> item resolution for the probe layer, uncharted-task injection
into NEW's step-0 menu, leads riding into downstream prompts, ledger-signature
invalidation of cached findings, and REFINE's shortfall-first targeting with
solver contrast.
"""
from __future__ import annotations

import json
import os

from css.config import CSSConfig
from css.coverage import CoverageLedger, coverage_path
from css.data.tree import SearchTree, TreeNode
from css.explore import api as explore_api
from css.explore.leads import leads_path, record_lead
from css.l1gen import _context, _llm
from css.l1gen.new_pipeline import run_new_pipeline
from css.l1gen.refine_pipeline import run_refine_pipeline
from css.tree_search import SpawnContext


def _cfg(**kw) -> CSSConfig:
    base = dict(n_train=4, n_val=2, n_test=2, burst_steps=5, l0_stall_steps=8,
                N=5, node_degree=3, max_decisions=8, ledger_min_attempts=1)
    base.update(kw)
    return CSSConfig(**base)


class _Env:
    """Minimal env: train_items() only (what resolution needs)."""

    def __init__(self, ids):
        self._items = [{"task_id": t, "instruction": "do %s" % t} for t in ids]

    def train_items(self):
        return list(self._items)


# ── id -> item resolution ────────────────────────────────────────────────────
def test_resolve_ids_against_env_and_drop_phantoms():
    env = _Env(["t1", "t2"])
    items = _context.resolve_task_items(env, ["t2", "phantom", "t1"])
    assert [i["task_id"] for i in items] == ["t2", "t1"]
    assert all("instruction" in i for i in items)


def test_resolve_wraps_ids_without_env():
    items = _context.resolve_task_items(None, ["t1"])
    assert items == [{"task_id": "t1"}]
    # dicts pass through untouched
    d = [{"task_id": "t9", "payload": 1}]
    assert _context.resolve_task_items(None, d) == d


# ── NEW step 0: uncharted injection + leads into findings ────────────────────
_HAPPY_NEW = {
    _llm.STAGE_NEW_TARGET: {"target_group": "g", "why_all_paradigms_fail": "w"},
    _llm.STAGE_NEW_CONCEPT: {"core_behavioral_commitment": "commit"},
    _llm.STAGE_NEW_NOVELTY: {"novel": True, "duplicates": "", "reason": ""},
    _llm.STAGE_NEW_DRAFT: {"strategy_md": "## Mechanism\nAct differently.\n",
                           "rationale": {"target_problem": "p"}},
    _llm.STAGE_NEW_ALTITUDE: {"verdict": "pass"},
}


def _new_ctx(tmp_path, cfg=None) -> SpawnContext:
    tree = SearchTree()
    root = TreeNode(node_id="n0000", branch_type="ROOT", strategy="", rules="",
                    status="saturated")
    tree.add_root(root)
    return SpawnContext(tree=tree, parent=root, mode="NEW", new_node_id="n0001",
                        cfg=cfg or _cfg(), env=None, target_client=None,
                        optimizer_client=None, out_dir=str(tmp_path),
                        decision_index=3, ledger=None)


def _fake_llm(script, seen_users=None):
    def fn(client, system, user, *, parse=None, ok=None, required=None,
           max_tokens=4096, repair_max_tokens=16384, stage=""):
        if seen_users is not None:
            seen_users.setdefault(stage, []).append(user)
        v = script[stage]
        return v(system, user) if callable(v) else v
    return fn


def test_new_explores_uncharted_when_no_unsolved_groups(tmp_path, monkeypatch):
    # groups.json empty but uncharted tasks exist -> exploration still fires,
    # menu = the uncharted frontier (design §4: no blind spots).
    gdir = tmp_path / "global" / "unsolved"
    gdir.mkdir(parents=True)
    (gdir / "groups.json").write_text("{}")
    (gdir / "meta.json").write_text(json.dumps(
        {"uncharted_task_ids": ["u1", "u2"]}))
    seen = {}

    def fake_explore(group_key, **kw):
        seen.update(group_key=group_key,
                    tasks=[t.get("task_id") for t in kw["group_tasks"]],
                    briefing=kw["briefing_md"])
        return "frontier mapped"

    monkeypatch.setattr("css.explore.api.get_or_explore", fake_explore)
    monkeypatch.setattr("css.l1gen._llm.complete_optimizer_json",
                        _fake_llm(_HAPPY_NEW))
    out = run_new_pipeline(_new_ctx(tmp_path))
    assert out.child is not None
    assert seen["group_key"] == "uncharted_frontier"
    assert seen["tasks"] == ["u1", "u2"]
    assert "Never-attempted tasks" in seen["briefing"]


def test_new_leads_ride_into_downstream_prompts(tmp_path, monkeypatch):
    gdir = tmp_path / "global" / "unsolved"
    gdir.mkdir(parents=True)
    (gdir / "groups.json").write_text(json.dumps(
        {"grp": {"task_ids": ["t1"], "summary": "s", "priority": 5.0}}))
    (gdir / "meta.json").write_text(json.dumps({"uncharted_task_ids": []}))
    record_lead(leads_path(str(tmp_path)), task_id="t1",
                behavior_prompt="probe all sources in parallel", n_pass=2, k=2)
    monkeypatch.setattr("css.explore.api.get_or_explore",
                        lambda group_key, **kw: "explored")
    seen_users: dict = {}
    monkeypatch.setattr("css.l1gen._llm.complete_optimizer_json",
                        _fake_llm(_HAPPY_NEW, seen_users))
    out = run_new_pipeline(_new_ctx(tmp_path))
    assert out.child is not None
    target_user = seen_users[_llm.STAGE_NEW_TARGET][0]
    assert "KNOWN LEADS" in target_user
    assert "probe all sources in parallel" in target_user


# ── findings cache: ledger-signature invalidation ────────────────────────────
def test_cached_findings_invalidate_on_ledger_state_flip(tmp_path, monkeypatch):
    out = str(tmp_path)
    runs = []

    def fake_session(**kw):
        runs.append(kw["group_key"])
        sd = kw["session_dir"]
        os.makedirs(sd, exist_ok=True)
        with open(os.path.join(sd, "report.md"), "w") as f:
            f.write("probe report %d" % len(runs))
        with open(os.path.join(sd, "session_meta.json"), "w") as f:
            json.dump({"group_key": kw["group_key"], "n_probes": 1}, f)
        return {"report_md": "r", "meta": {}}

    monkeypatch.setattr(explore_api, "run_director_session", fake_session)
    monkeypatch.setattr(explore_api, "build_findings",
                        lambda reports, **kw: "FINDINGS v%d" % len(runs))

    led = CoverageLedger(["t1"], min_attempts=1, path=coverage_path(out))
    led.record("n0", "t1", False)
    led.save()

    kwargs = dict(group_tasks=[{"task_id": "t1"}], neighbor_tasks=[],
                  briefing_md="b", mode="NEW", env=None, target_client=None,
                  optimizer_client=None, cfg=_cfg(), out_dir=out)
    f1 = explore_api.get_or_explore("g", decision_index=1, **kwargs)
    assert f1 == "FINDINGS v1" and runs == ["g"]

    # Same partition -> cache hit, no second session.
    f2 = explore_api.get_or_explore("g", decision_index=2, **kwargs)
    assert f2 == "FINDINGS v1" and len(runs) == 1

    # State flip (t1 solved) -> signature changes -> re-explore.
    led.record("n0", "t1", True)
    led.save()
    f3 = explore_api.get_or_explore("g", decision_index=3, **kwargs)
    assert len(runs) == 2 and f3 == "FINDINGS v2"


# ── REFINE: shortfall-first targeting with solver contrast ───────────────────
_PARENT = ("## Ground Every Claim\nRead before you write.\n\n"
           "## Decompose Before Committing\nBreak the task into parts.\n")

_REFINE_HAPPY = {
    _llm.STAGE_REFINE_CAUSE: {
        "has_target": True, "target_type": "A", "target_group": "g1",
        "cause": "missing contrast mechanism",
        "sections_implicated": ["Decompose Before Committing"],
        "keep_list": [], "no_target_reason": ""},
    _llm.STAGE_REFINE_PLAN: {"ops": [{
        "op": "replace_section", "section": "Decompose Before Committing",
        "content": "## Decompose Before Committing\nStage then commit.\n",
        "rationale": "addresses the cause"}]},
    _llm.STAGE_REFINE_CONFRONT: {"proceed": True, "reason": "scoped",
                                 "which_ops": []},
    _llm.STAGE_SCREEN: [{"index": 0, "verdict": "pass", "violated_criteria": [],
                         "feedback": "", "quoted_offense": ""}],
    _llm.STAGE_REFINE_COHERENCE: {"items": []},
    _llm.STAGE_REFINE_RATIONALE: {"target_problem": "p", "idea_sources": "s",
                                  "expected_behavior_changes": ["c"]},
    _llm.STAGE_REFINE_ALTITUDE: {"verdict": "pass"},
    _llm.STAGE_REFINE_INHERIT: [],
}


def _refine_ctx(tmp_path, env=None) -> SpawnContext:
    tree = SearchTree()
    root = TreeNode(node_id="n0000", branch_type="ROOT", strategy="", rules="")
    tree.add_root(root)
    parent = TreeNode(node_id="n0001", branch_type="NEW", strategy=_PARENT,
                      rules="", status="saturated")
    tree.add_child("n0000", parent)
    sib = TreeNode(node_id="n0002", branch_type="NEW",
                   strategy="## Probe Everything\nDispatch parallel probes.\n",
                   rules="", status="active")
    tree.add_child("n0000", sib)
    return SpawnContext(tree=tree, parent=parent, mode="REFINE",
                        new_node_id="n0003", cfg=_cfg(), env=env,
                        target_client=None, optimizer_client=None,
                        out_dir=str(tmp_path), decision_index=7, ledger=None)


def _seed_shortfall(out: str) -> None:
    """n0001 fails t7 (n0002 solves it) => shortfall; n0002 also solves t8."""
    led = CoverageLedger(["t7", "t8"], min_attempts=1, path=coverage_path(out))
    led.record("n0001", "t7", False)
    led.record("n0002", "t7", True)
    led.record("n0002", "t8", True)
    led.save()


def test_refine_targets_shortfall_with_solver_contrast(tmp_path, monkeypatch):
    _seed_shortfall(str(tmp_path))
    seen = {}

    def fake_explore(group_key, **kw):
        seen.update(group_key=group_key,
                    tasks=[t.get("task_id") for t in kw["group_tasks"]],
                    neighbors=[t.get("task_id") for t in kw["neighbor_tasks"]],
                    briefing=kw["briefing_md"])
        return "the solver dispatches parallel probes before bridging"

    monkeypatch.setattr("css.explore.api.get_or_explore", fake_explore)
    seen_users: dict = {}
    monkeypatch.setattr("css.l1gen._llm.complete_optimizer_json",
                        _fake_llm(_REFINE_HAPPY, seen_users))
    out = run_refine_pipeline(_refine_ctx(tmp_path))
    assert out.child is not None
    assert seen["group_key"] == "shortfall_n0001"
    assert seen["tasks"] == ["t7"]
    # Solver-solved contrast menu: n0002's other solved task, not the target.
    assert seen["neighbors"] == ["t8"]
    # Briefing names the solver and its strategy head.
    assert "solved by: n0002" in seen["briefing"]
    assert "Probe Everything" in seen["briefing"]
    # The shortfall findings feed the FIRST cause confirmation.
    cause_user = seen_users[_llm.STAGE_REFINE_CAUSE][0]
    assert "parallel probes before bridging" in cause_user
    cause = json.loads((tmp_path / "nodes" / "n0003" / "gen" /
                        "cause_confirmation.json").read_text())
    assert cause["_shortfall_tasks"] == ["t7"]


def test_refine_skips_exploration_without_shortfall_or_escalation(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("css.explore.api.get_or_explore",
                        lambda group_key, **kw: calls.append(group_key) or "f")
    monkeypatch.setattr("css.l1gen._llm.complete_optimizer_json",
                        _fake_llm(_REFINE_HAPPY))
    out = run_refine_pipeline(_refine_ctx(tmp_path))
    assert out.child is not None
    assert calls == [], "no shortfall + no escalated U group => no exploration"
