"""Tree-search loop: three-state machine, cross-burst stall, spawn atomicity,
degree/decline terminality, checkpoint resume, and burst-unit UCB.

The loop is exercised with an injected fake burst function and spawner (no env,
no LLM). The fake burst mirrors the real contract: appends ``burst_steps``
step-buffer entries, advances val on a scripted gain, increments ``n_bursts``.
"""
from __future__ import annotations

import pytest

import css.tree_search as ts
from css.config import CSSConfig
from css.data.step_buffer import StepBufferEntry
from css.data.tree import SearchTree, TreeNode
from css.tree.select import ucb1_score
from css.tree_search import BurstResult, SpawnOutcome, node_stalled, run_css_tree


def _cfg(**kw) -> CSSConfig:
    base = dict(
        n_train=4, n_val=2, n_test=2,
        burst_steps=5, l0_stall_steps=8, N=5,
        node_degree=3, max_decisions=8,
        test_eval_on_new_best=False,
        alpha=0.5, beta=0.5, W=10,
    )
    base.update(kw)
    return CSSConfig(**base)


def _fake_measure(baselines: dict):
    """Monkeypatch stand-in for measure_initial_val (no rollouts)."""
    def fn(node, env, target_client, *, cfg, out_dir, val_items=None):
        score = baselines.get(node.node_id, 0.5)
        node.baseline_val_score = score
        node.val_score = score
        return out_dir
    return fn


def _fake_burst(plans: dict):
    """Scripted burst: ``plans[node_id][burst_index]`` = val gain (>0 => one
    accept_new_best first step; 0 => five rejects)."""
    calls: list[tuple[str, int]] = []

    def fn(tree, node, env, tc, oc, *, cfg, out_dir, decision_index, ledger=None):
        assert node.status == "active" or node.n_bursts == 0, (
            "burst must only run on ACTIVE nodes (or a freshly spawned child)")
        idx = node.n_bursts
        calls.append((node.node_id, idx))
        gain = plans.get(node.node_id, [])
        gain = gain[idx] if idx < len(gain) else 0.0
        for s in range(cfg.burst_steps):
            action = "accept_new_best" if (s == 0 and gain > 0) else "reject"
            node.step_buffer.append(StepBufferEntry(
                step=node.n_steps, action=action,
                score_before=node.val_score, score_after=node.val_score))
        before = node.val_score
        node.val_score = before + max(0.0, gain)
        node.best_rules = node.rules or node.best_rules
        node.n_bursts += 1
        node.burst_rewards.append(node.val_score - before)
        return BurstResult(
            node_id=node.node_id, burst_index=idx, decision_index=decision_index,
            steps=cfg.burst_steps, n_accepted=1 if gain > 0 else 0,
            val_before=before, val_after=node.val_score,
            reward=node.val_score - before,
            stall_after=node.step_buffer.steps_since_new_best())

    fn.calls = calls
    return fn


def _spawner(strategy="## Mechanism A\ndo things differently", decline_ids=()):
    def fn(ctx):
        if ctx.parent.node_id in decline_ids:
            return SpawnOutcome(child=None, mode=ctx.mode, decline=True,
                                reason="no defensible A/B target")
        child = TreeNode(
            node_id=ctx.new_node_id, strategy=strategy,
            rules="inherited: parent tactic" if ctx.mode == "REFINE" else "should-be-cleared",
        )
        return SpawnOutcome(child=child, mode=ctx.mode)
    return fn


# ── cross-burst stall semantics ─────────────────────────────────────────────
def test_single_dry_burst_does_not_saturate():
    cfg = _cfg()
    node = TreeNode(node_id="n0000")
    for _ in range(5):
        node.step_buffer.append(StepBufferEntry(
            step=0, action="reject", score_before=0, score_after=0))
    assert node.step_buffer.steps_since_new_best() == 5
    assert not node_stalled(node, cfg)   # 5 < 8: one dry burst is NOT saturation
    # legacy criterion would have fired here — deliberately unused:
    assert node.step_buffer.consecutive_rejects() >= cfg.N


def test_stall_counter_persists_across_bursts(tmp_path, monkeypatch):
    cfg = _cfg(max_decisions=3)
    monkeypatch.setattr(ts, "measure_initial_val", _fake_measure({"n0000": 0.5}))
    burst = _fake_burst({"n0000": [0.1, 0.0]})  # gain burst, then dry burst
    result = run_css_tree(None, None, None, cfg=cfg, out_dir=str(tmp_path),
                          spawner=_spawner(), burst_fn=burst)
    root = result.tree.get("n0000")
    # burst 0: accept at step 0 then 4 rejects (stall 4, active);
    # burst 1: 5 more rejects (stall 9 >= 8 -> saturated);
    # decision 2: saturated root spawns.
    assert [c for c in burst.calls if c[0] == "n0000"] == [("n0000", 0), ("n0000", 1)]
    assert root.status in ("saturated", "terminal") or root.children_ids


# ── spawn atomicity + zero inheritance ──────────────────────────────────────
def test_spawn_and_first_burst_atomic_no_unvisited_child(tmp_path, monkeypatch):
    cfg = _cfg(max_decisions=4)
    monkeypatch.setattr(ts, "measure_initial_val",
                        _fake_measure({"n0000": 0.5, "n0001": 0.55}))
    burst = _fake_burst({"n0000": [0.0], "n0001": [0.2]})
    result = run_css_tree(None, None, None, cfg=cfg, out_dir=str(tmp_path),
                          spawner=_spawner(), burst_fn=burst)
    tree = result.tree
    child = tree.get("n0001")
    assert child is not None, "saturated root must spawn a NEW child"
    assert child.branch_type == "NEW"
    assert child.n_bursts >= 1, "spawn+first-burst must be atomic (no n=0 in pool)"
    assert child.rules == "", "NEW children start with ZERO rules inheritance"
    assert (tmp_path / "decisions.jsonl").exists()


def test_refine_child_keeps_adjudicated_rules(tmp_path, monkeypatch):
    cfg = _cfg(max_decisions=8)
    # n0001 born higher than root so the saturated n0001 wins selection and
    # spawns the REFINE (UCB ties would otherwise keep going to the root).
    monkeypatch.setattr(ts, "measure_initial_val",
                        _fake_measure({"n0000": 0.5, "n0001": 0.7}))
    burst = _fake_burst({"n0000": [0.0, 0.0], "n0001": [0.0, 0.0]})
    result = run_css_tree(None, None, None, cfg=cfg, out_dir=str(tmp_path),
                          spawner=_spawner(), burst_fn=burst)
    tree = result.tree
    refined = [n for n in tree.nodes.values() if n.branch_type == "REFINE"]
    assert refined, "a saturated strategy node must spawn a REFINE child"
    assert all(n.rules.startswith("inherited") for n in refined), (
        "REFINE children carry the adjudicated inherited rules untouched by the loop")


# ── terminality ─────────────────────────────────────────────────────────────
def test_degree_exhaustion_makes_strategy_node_terminal_root_never(tmp_path, monkeypatch):
    cfg = _cfg(max_decisions=30, node_degree=1)
    # Root born lowest: saturated children outscore it, so spawning flows
    # down the lineage and quotas actually get spent.
    monkeypatch.setattr(ts, "measure_initial_val", _fake_measure({"n0000": 0.4}))
    burst = _fake_burst({})     # every burst dry -> everything saturates fast
    result = run_css_tree(None, None, None, cfg=cfg, out_dir=str(tmp_path),
                          spawner=_spawner(), burst_fn=burst)
    tree = result.tree
    root = tree.get(tree.root_id)
    assert root.status != "terminal", "root is never terminal"
    exhausted = [n for n in tree.nodes.values()
                 if not n.is_root and len(n.children_ids) >= 1]
    assert exhausted, "with degree=1 some strategy node must exhaust its quota"
    assert all(n.status == "terminal" for n in exhausted)


def test_refine_decline_terminal(tmp_path, monkeypatch):
    cfg = _cfg(max_decisions=8)
    monkeypatch.setattr(ts, "measure_initial_val", _fake_measure({"n0000": 0.4}))
    burst = _fake_burst({})
    # n0001 (the first NEW child) declines its REFINE -> must go terminal.
    result = run_css_tree(None, None, None, cfg=cfg, out_dir=str(tmp_path),
                          spawner=_spawner(decline_ids=("n0001",)), burst_fn=burst)
    child = result.tree.get("n0001")
    assert child is not None
    assert child.status == "terminal", "REFINE decline => node TERMINAL (no junk child)"
    assert not child.children_ids


# ── selection: burst units, no inf ──────────────────────────────────────────
def test_ucb_no_inf_and_burst_units():
    fresh = TreeNode(node_id="a", val_score=0.6)          # n_bursts == 0
    veteran = TreeNode(node_id="b", val_score=0.6, n_bursts=9)
    s_fresh = ucb1_score(fresh, total_bursts=10, alpha=0.0, beta=0.5, window=10)
    s_vet = ucb1_score(veteran, total_bursts=10, alpha=0.0, beta=0.5, window=10)
    assert s_fresh != float("inf")
    assert s_fresh > s_vet, "fewer bursts => bigger exploration bonus"


# ── checkpoint / resume ─────────────────────────────────────────────────────
def test_checkpoint_resume_continues_and_guards_fingerprint(tmp_path, monkeypatch):
    cfg = _cfg(max_decisions=2)
    monkeypatch.setattr(ts, "measure_initial_val", _fake_measure({"n0000": 0.5}))
    burst = _fake_burst({"n0000": [0.1, 0.0, 0.0]})
    r1 = run_css_tree(None, None, None, cfg=cfg, out_dir=str(tmp_path),
                      spawner=_spawner(), burst_fn=burst)
    assert len(r1.rounds) == 2

    # Resume with a larger budget: continues from decision 2 (budget is NOT
    # fingerprinted), prior decisions retained, global best preserved.
    cfg2 = _cfg(max_decisions=4)
    burst2 = _fake_burst({"n0000": [0.1, 0.0, 0.0]})
    r2 = run_css_tree(None, None, None, cfg=cfg2, out_dir=str(tmp_path),
                      resume=True, spawner=_spawner(), burst_fn=burst2)
    assert len(r2.rounds) == 4
    # Run 1 executed root bursts 0-1 (then saturated); the resumed run must
    # not replay them — its work is the spawn + child bursts.
    assert burst2.calls, "resume must continue with new decisions"
    assert ("n0000", 0) not in burst2.calls and ("n0000", 1) not in burst2.calls, (
        "resume must not replay completed bursts")
    assert r2.tree.get("n0000").val_score >= 0.6
    assert r2.tree.get("n0000").status == "saturated"

    # Structural knob change => fingerprint mismatch => refuse.
    cfg3 = _cfg(max_decisions=6, burst_steps=3)
    with pytest.raises(ValueError, match="fingerprint"):
        run_css_tree(None, None, None, cfg=cfg3, out_dir=str(tmp_path),
                     resume=True, spawner=_spawner(), burst_fn=_fake_burst({}))


# ── global best decoupled from selection ────────────────────────────────────
def test_global_best_snapshot_survives_node_regression(tmp_path, monkeypatch):
    cfg = _cfg(max_decisions=3)
    monkeypatch.setattr(ts, "measure_initial_val", _fake_measure({"n0000": 0.5}))
    burst = _fake_burst({"n0000": [0.3, 0.0, 0.0]})
    run_css_tree(None, None, None, cfg=cfg, out_dir=str(tmp_path),
                 spawner=_spawner(), burst_fn=burst)
    import json
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["global_best"]["val"] == pytest.approx(0.8)
    assert (tmp_path / "global_best" / "strategy.md").exists()
    assert (tmp_path / "global_best" / "rules.md").exists()
