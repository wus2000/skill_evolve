"""MERGE pipeline (docs/L1_actions_redesign.md §6) + root three-way dispatch.

The load-bearing assertions: the fused child's rules are assembled VERBATIM
from source sections (select-and-prune only — byte-identity is the fidelity
constraint the user approved), the matrix includes dead nodes' coverage, a
degenerate matrix DECLINES (root blocks without a strike), and the tree loop
keeps a MERGE child's rules (only NEW zeroes them).
"""
from __future__ import annotations

import json

import css.tree_search as ts
from css.config import CSSConfig
from css.coverage import CoverageLedger, coverage_path
from css.data.step_buffer import StepBufferEntry
from css.data.tree import SearchTree, TreeNode
from css.l1gen import _llm
from css.l1gen.merge_pipeline import (
    build_merge_matrix,
    merge_coverage_check,
    run_merge_pipeline,
)
from css.tree_search import BurstResult, SpawnContext, SpawnOutcome, root_spawn_mode, run_css_tree


def _cfg(**kw) -> CSSConfig:
    base = dict(n_train=4, n_val=2, n_test=2, burst_steps=5, l0_stall_steps=8,
                N=5, node_degree=3, max_decisions=8, ledger_min_attempts=1)
    base.update(kw)
    return CSSConfig(**base)


_RULES_A = "### Alpha Retrieval\nalways page through results\n  keep indentation\n"
_RULES_B = "### Beta Bridging\nresolve the entity bridge first\n"

_STRAT_A = "## Probe Everything\nDispatch parallel probes to all sources.\n"
_STRAT_B = "## Bridge First\nConstruct the entity bridge before retrieval.\n"


def _full_coverage_ledger(out: str) -> CoverageLedger:
    """nA exclusively solves t1, nB exclusively solves t2; both attempted both.
    Full coverage: no global_unsolved, no uncharted."""
    led = CoverageLedger(["t1", "t2"], min_attempts=1, path=coverage_path(out))
    led.record("nA", "t1", True)
    led.record("nA", "t2", False)
    led.record("nB", "t1", False)
    led.record("nB", "t2", True)
    led.save()
    return led


def _merge_tree() -> SearchTree:
    tree = SearchTree()
    root = TreeNode(node_id="n0000", branch_type="ROOT", strategy="", rules="",
                    status="saturated")
    tree.add_root(root)
    nA = TreeNode(node_id="nA", branch_type="NEW", strategy=_STRAT_A,
                  rules=_RULES_A, best_rules=_RULES_A, status="active")
    nB = TreeNode(node_id="nB", branch_type="NEW", strategy=_STRAT_B,
                  rules=_RULES_B, best_rules=_RULES_B, status="pruned")
    tree.add_child("n0000", nA)
    tree.add_child("n0000", nB)
    return tree


def _merge_ctx(tmp_path, tree=None) -> SpawnContext:
    tree = tree or _merge_tree()
    return SpawnContext(tree=tree, parent=tree.get("n0000"), mode="MERGE",
                        new_node_id="n0003", cfg=_cfg(), env=None,
                        target_client=None, optimizer_client=None,
                        out_dir=str(tmp_path), decision_index=9, ledger=None)


def _fake_llm(script, seen_users=None):
    def fn(client, system, user, *, parse=None, ok=None, required=None,
           max_tokens=4096, repair_max_tokens=16384, stage=""):
        if seen_users is not None:
            seen_users.setdefault(stage, []).append(user)
        v = script[stage]
        return v(system, user) if callable(v) else v
    return fn


def _select_by_source(system, user):
    if "SOURCE NODE: nA" in user:
        return [{"section": "### Alpha Retrieval", "verdict": "keep", "reason": "r"}]
    return [{"section": "### Beta Bridging", "verdict": "keep", "reason": "r"}]


_MERGE_HAPPY = {
    _llm.STAGE_MERGE_CONCEPT: {
        "base_node": "nA",
        "contributions": [{"source": "nB", "sections": ["Bridge First"],
                           "adaptation": "route by external-key presence"}],
        "conflict_resolutions": [{"between": ["nA", "nB"],
                                  "resolution": "conditional routing"}],
        "expected_coverage": ["t1", "t2"]},
    _llm.STAGE_MERGE_DRAFT: {
        "strategy_md": ("## Route By Task Feature\nWith an external key, probe "
                        "in parallel; otherwise bridge first.\n"),
        "rationale": {"target_problem": "union coverage",
                      "idea_sources": "probe-validated routing",
                      "expected_behavior_changes": ["routes instead of committing"]}},
    _llm.STAGE_MERGE_ALTITUDE: {"verdict": "pass"},
    _llm.STAGE_MERGE_RULES_SELECT: _select_by_source,
    _llm.STAGE_MERGE_RULES_CONSOLIDATE: {
        "sections": [{"source": "nA", "section": "### Alpha Retrieval"},
                     {"source": "nB", "section": "### Beta Bridging"}],
        "dropped": []},
}


# ── root three-way dispatch ──────────────────────────────────────────────────
def test_root_spawn_mode_three_way(tmp_path):
    out = str(tmp_path)
    assert root_spawn_mode(None, _cfg()) == "NEW"

    led = CoverageLedger(["t1", "t2"], min_attempts=1, path=coverage_path(out))
    led.record("nA", "t1", False)              # unsolved frontier -> NEW
    assert root_spawn_mode(led, _cfg()) == "NEW"

    led.record("nA", "t1", True)               # t1 solved, t2 uncharted -> NEW
    assert root_spawn_mode(led, _cfg()) == "NEW"

    led.record("nB", "t2", True)               # true full coverage -> MERGE
    assert root_spawn_mode(led, _cfg()) == "MERGE"

    led.record("nB", "t9", False)              # regression re-opens -> NEW again
    assert root_spawn_mode(led, _cfg()) == "NEW"


# ── matrix ───────────────────────────────────────────────────────────────────
def test_matrix_includes_dead_nodes_and_detects_degenerate(tmp_path):
    led = _full_coverage_ledger(str(tmp_path))
    tree = _merge_tree()                       # nB is PRUNED
    m = build_merge_matrix(led, tree)
    assert m["exclusive_coverage"] == {"nA": ["t1"], "nB": ["t2"]}
    assert m["node_status"]["nB"] == "pruned", "dead coverage evidence stays usable"
    assert not m["degenerate"]

    # Everyone solves everything -> nothing exclusive -> degenerate.
    led2 = CoverageLedger(["t1"], min_attempts=1)
    led2.record("nA", "t1", True)
    led2.record("nB", "t1", True)
    assert build_merge_matrix(led2, tree)["degenerate"]


def test_degenerate_matrix_declines(tmp_path):
    led = CoverageLedger(["t1"], min_attempts=1,
                         path=coverage_path(str(tmp_path)))
    led.record("nA", "t1", True)
    led.record("nB", "t1", True)
    led.save()
    out = run_merge_pipeline(_merge_ctx(tmp_path))
    assert out.child is None and out.decline is True
    assert "degenerate" in out.reason


# ── happy path ───────────────────────────────────────────────────────────────
def test_merge_happy_path_verbatim_rules_and_provenance(tmp_path, monkeypatch):
    _full_coverage_ledger(str(tmp_path))
    monkeypatch.setattr("css.explore.api.get_or_explore",
                        lambda group_key, **kw: "routing held on both sides")
    monkeypatch.setattr("css.l1gen._llm.complete_optimizer_json",
                        _fake_llm(_MERGE_HAPPY))
    out = run_merge_pipeline(_merge_ctx(tmp_path))
    assert out.child is not None and out.mode == "MERGE"
    child = out.child
    assert child.branch_type == "MERGE"
    assert "Route By Task Feature" in child.strategy

    # FIDELITY: every carried section is byte-identical to its source (only
    # trailing whitespace at the join may differ) — the no-rewrite constraint
    # holds by construction.
    assert _RULES_A.rstrip() in child.rules
    assert _RULES_B.rstrip() in child.rules
    assert child.best_rules == child.rules

    prov = json.loads((tmp_path / "nodes" / "n0003" / "dossier" /
                       "rules_provenance.json").read_text())
    assert prov == [{"source": "nA", "section": "### Alpha Retrieval"},
                    {"source": "nB", "section": "### Beta Bridging"}]
    # First MERGE: novelty short-circuits (no prior fusion to duplicate).
    nv = json.loads((tmp_path / "nodes" / "n0003" / "gen" /
                     "novelty_verdict.json").read_text())
    assert nv["novel"] is True and "first MERGE" in nv.get("note", "")
    # Blueprint persisted with the coverage expectation for step 7.
    concept = json.loads((tmp_path / "nodes" / "n0003" / "gen" /
                          "conception.json").read_text())
    assert concept["expected_coverage"] == ["t1", "t2"]


def test_merge_novelty_rejects_duplicate_fusion(tmp_path, monkeypatch):
    _full_coverage_ledger(str(tmp_path))
    tree = _merge_tree()
    prior = TreeNode(node_id="n0002", branch_type="MERGE",
                     strategy="## Route By Task Feature\nSame idea already.\n",
                     rules="", status="active")
    tree.add_child("n0000", prior)
    script = dict(_MERGE_HAPPY)
    script[_llm.STAGE_MERGE_NOVELTY] = {"novel": False, "duplicates": "n0002",
                                        "reason": "same routing fusion"}
    monkeypatch.setattr("css.explore.api.get_or_explore",
                        lambda group_key, **kw: "f")
    monkeypatch.setattr("css.l1gen._llm.complete_optimizer_json",
                        _fake_llm(script))
    out = run_merge_pipeline(_merge_ctx(tmp_path, tree=tree))
    assert out.child is None and out.decline is False
    assert "duplicate of prior fusion n0002" in out.reason


def test_merge_conception_failure_is_a_plain_fail(tmp_path, monkeypatch):
    _full_coverage_ledger(str(tmp_path))
    script = dict(_MERGE_HAPPY)
    script[_llm.STAGE_MERGE_CONCEPT] = {}
    monkeypatch.setattr("css.explore.api.get_or_explore",
                        lambda group_key, **kw: "f")
    monkeypatch.setattr("css.l1gen._llm.complete_optimizer_json",
                        _fake_llm(script))
    out = run_merge_pipeline(_merge_ctx(tmp_path))
    assert out.child is None and out.decline is False
    assert "conception" in out.reason


def test_merge_all_rules_dropped_still_spawns(tmp_path, monkeypatch):
    _full_coverage_ledger(str(tmp_path))
    script = dict(_MERGE_HAPPY)
    script[_llm.STAGE_MERGE_RULES_SELECT] = [
        {"section": "### whatever", "verdict": "drop", "reason": "conflicts"}]
    monkeypatch.setattr("css.explore.api.get_or_explore",
                        lambda group_key, **kw: "f")
    monkeypatch.setattr("css.l1gen._llm.complete_optimizer_json",
                        _fake_llm(script))
    out = run_merge_pipeline(_merge_ctx(tmp_path))
    assert out.child is not None
    assert out.child.rules == "", "an empty verified-rules start is legal; L0 grows them"


# ── step 7: coverage-preservation check ──────────────────────────────────────
def test_merge_coverage_check_kept_lost_unattempted(tmp_path):
    out = str(tmp_path)
    gd = tmp_path / "nodes" / "n0003" / "gen"
    gd.mkdir(parents=True)
    (gd / "conception.json").write_text(json.dumps(
        {"expected_coverage": ["t1", "t2", "t3"]}))
    led = CoverageLedger(["t1", "t2", "t3"], min_attempts=1,
                         path=coverage_path(out))
    led.record("n0003", "t1", True)
    led.record("n0003", "t2", False)
    led.record("n0003", "t4", True)
    led.save()
    rec = merge_coverage_check(out, "n0003", _cfg())
    assert rec == {"expected": ["t1", "t2", "t3"], "kept": ["t1"],
                   "lost": ["t2"], "unattempted": ["t3"], "extra": ["t4"]}
    on_disk = json.loads((tmp_path / "nodes" / "n0003" / "dossier" /
                          "merge_coverage_check.json").read_text())
    assert on_disk == rec


# ── tree-loop integration: dispatch + rules preservation ────────────────────
def test_tree_loop_dispatches_merge_and_keeps_child_rules(tmp_path, monkeypatch):
    out = str(tmp_path)
    # Pre-seed TRUE full coverage before the run starts.
    led = CoverageLedger(["t1"], min_attempts=1, path=coverage_path(out))
    led.record("n0000", "t1", True)
    led.save()

    monkeypatch.setattr(ts, "measure_initial_val",
                        _fake_measure({"n0000": 0.6, "n0001": 0.7}))
    seen_modes = []

    def spawner(ctx):
        seen_modes.append(ctx.mode)
        child = TreeNode(node_id=ctx.new_node_id, branch_type="MERGE",
                         strategy="## Fused\nroute.\n",
                         rules="### Carried\nverbatim section\n",
                         best_rules="### Carried\nverbatim section\n")
        return SpawnOutcome(child=child, mode=ctx.mode)

    burst = _fake_burst({"n0000": [0.1, 0.0, 0.0], "n0001": [0.05]})
    result = run_css_tree(None, None, None, cfg=_cfg(max_decisions=5),
                          out_dir=out, spawner=spawner, burst_fn=burst)
    assert seen_modes and seen_modes[0] == "MERGE"
    child = result.tree.get("n0001")
    assert child is not None
    assert child.branch_type == "MERGE"
    assert child.rules == "### Carried\nverbatim section\n", (
        "the loop must NOT zero a MERGE child's rules (only NEW starts blank)")


def _fake_measure(baselines: dict):
    def fn(node, env, target_client, *, cfg, out_dir, val_items=None):
        score = baselines.get(node.node_id, 0.5)
        node.baseline_val_score = score
        node.val_score = score
        return out_dir
    return fn


def _fake_burst(plans: dict):
    def fn(tree, node, env, tc, oc, *, cfg, out_dir, decision_index, ledger=None,
           coverage=None):
        idx = node.n_bursts
        gain = plans.get(node.node_id, [])
        gain = gain[idx] if idx < len(gain) else 0.0
        for s in range(cfg.burst_steps):
            action = "accept_new_best" if (s == 0 and gain > 0) else "reject"
            node.step_buffer.append(StepBufferEntry(
                step=node.n_steps, action=action,
                score_before=node.val_score, score_after=node.val_score))
        before = node.val_score
        node.val_score = before + max(0.0, gain)
        node.n_bursts += 1
        node.burst_rewards.append(node.val_score - before)
        node.burst_accepts.append(1 if gain > 0 else 0)
        return BurstResult(
            node_id=node.node_id, burst_index=idx, decision_index=decision_index,
            steps=cfg.burst_steps, n_accepted=1 if gain > 0 else 0,
            val_before=before, val_after=node.val_score,
            reward=node.val_score - before,
            stall_after=node.step_buffer.steps_since_new_best())
    return fn
