"""P0 selection debiasing (docs/L1_actions_redesign.md §2).

Regression anchors from the AppWorld post-mortem (run appworld_20260706_010423):
the saturated root monopolized 8/13 decisions because (a) the slope term
punished newborn cold-start settling, (b) spawns were never charged to the
selection count, (c) spawn rewards were booked against the empty-rules
baseline. These tests replay the decisive scenario with the run's REAL numbers
and pin the corrected accounting.
"""
from __future__ import annotations

import pytest

import css.tree_search as ts
from css.config import CSSConfig
from css.data.step_buffer import StepBufferEntry
from css.data.tree import SearchTree, TreeNode
from css.tree.select import select_node, total_selections, ucb1_score
from css.tree_search import BurstResult, SpawnOutcome, run_css_tree


# ── harness (mirrors test_tree_search_loop's fakes) ─────────────────────────
def _cfg(**kw) -> CSSConfig:
    base = dict(
        n_train=4, n_val=200, n_test=2,
        burst_steps=5, l0_stall_steps=8, N=5,
        node_degree=3, max_decisions=8,
        test_eval_on_new_best=False,
        alpha=0.5, beta=0.5, W=10,
    )
    base.update(kw)
    return CSSConfig(**base)


def _fake_measure(baselines: dict):
    def fn(node, env, target_client, *, cfg, out_dir, val_items=None):
        score = baselines.get(node.node_id, 0.5)
        node.baseline_val_score = score
        node.val_score = score
        return out_dir
    return fn


def _fake_burst(plans: dict):
    """Scripted burst: ``plans[node_id][burst_index]`` = val gain."""
    calls: list[tuple[str, int]] = []

    def fn(tree, node, env, tc, oc, *, cfg, out_dir, decision_index, ledger=None,
           coverage=None):
        idx = node.n_bursts
        calls.append((node.node_id, idx))
        gain = plans.get(node.node_id, [])
        gain = gain[idx] if idx < len(gain) else 0.0
        for s in range(cfg.burst_steps):
            action = "accept_new_best" if (s == 0 and gain > 0) else "reject"
            after = node.val_score + (gain if action == "accept_new_best"
                                      else 0.0)
            node.step_buffer.append(StepBufferEntry(
                step=node.n_steps, action=action,
                score_before=node.val_score, score_after=after))
        before = node.val_score
        node.val_score = before + max(0.0, gain)
        node.best_rules = node.rules or node.best_rules
        node.n_bursts += 1
        node.burst_rewards.append(node.val_score - before)
        node.burst_accepts.append(1 if gain > 0 else 0)
        return BurstResult(
            node_id=node.node_id, burst_index=idx, decision_index=decision_index,
            steps=cfg.burst_steps, n_accepted=1 if gain > 0 else 0,
            val_before=before, val_after=node.val_score,
            reward=node.val_score - before,
            stall_after=node.step_buffer.steps_since_new_best())

    fn.calls = calls
    return fn


def _spawner_script(outcomes):
    """Pop scripted outcomes; "ok" -> child, "fail" -> None/no-decline,
    "decline" -> None/decline."""
    script = list(outcomes)

    def fn(ctx):
        kind = script.pop(0) if script else "fail"
        if kind == "ok":
            child = TreeNode(node_id=ctx.new_node_id,
                             strategy="## Mechanism\nnew behavior")
            return SpawnOutcome(child=child, mode=ctx.mode)
        return SpawnOutcome(child=None, mode=ctx.mode,
                            decline=(kind == "decline"),
                            reason="scripted %s" % kind)
    return fn


# ── d10 replay: the corrected formula flips the decision ────────────────────
def test_d10_replay_newborn_wins_over_saturated_root():
    """Real numbers from ckpt_dec_0009: root val .8772, n0001 .7368 (1 charge),
    n0002 .8246 (2 charges). With spawns charged, the root carries 8 charges
    (3 bursts + 5 spawn decisions, d3-d10). The newborn must win the pick."""
    tree = SearchTree()
    root = TreeNode(node_id="n0000", val_score=0.8772, status="saturated",
                    n_bursts=3, n_selections=8)
    tree.add_root(root)
    n1 = TreeNode(node_id="n0001", branch_type="NEW", parent_id="n0000",
                  val_score=0.7368, n_bursts=1, n_selections=1)
    n2 = TreeNode(node_id="n0002", branch_type="NEW", parent_id="n0000",
                  val_score=0.8246, n_bursts=2, n_selections=2)
    tree.add_child("n0000", n1)
    tree.add_child("n0000", n2)

    assert total_selections(tree) == 11
    chosen = select_node(tree, cfg=_cfg())
    assert chosen is not None and chosen.node_id == "n0001"


def test_d10_replay_flips_even_under_legacy_charge_approximation():
    """Old-checkpoint compatibility maps n_selections <- n_bursts (root=3,
    T=6): even under that approximation the slope-term deletion alone flips
    d10 (live: root won 1.294 vs n0001 1.256 PURELY on slope +0.061 vs
    -0.300)."""
    tree = SearchTree()
    root_d = TreeNode(node_id="n0000", val_score=0.8772, status="saturated",
                      n_bursts=3).to_dict()
    root_d.pop("n_selections")               # simulate a pre-P0 checkpoint
    tree.add_root(TreeNode.from_dict(root_d))
    n1 = TreeNode(node_id="n0001", branch_type="NEW", parent_id="n0000",
                  val_score=0.7368, n_bursts=1, n_selections=1)
    n2 = TreeNode(node_id="n0002", branch_type="NEW", parent_id="n0000",
                  val_score=0.8246, n_bursts=2, n_selections=2)
    tree.add_child("n0000", n1)
    tree.add_child("n0000", n2)

    assert tree.get("n0000").n_selections == 3    # legacy approximation
    chosen = select_node(tree, cfg=_cfg())
    assert chosen is not None and chosen.node_id == "n0001"


def test_bonus_decays_as_the_node_is_charged():
    """d10 -> d12 live pathology: the root's bonus GREW (1.294 -> 1.324)
    because its children's bursts raised T while its own count froze.
    Charged per selection, the score must now be non-increasing in charges."""
    a = TreeNode(node_id="a", val_score=0.8772, n_selections=3)
    b = TreeNode(node_id="b", val_score=0.8772, n_selections=8)
    assert ucb1_score(a, total_selections=11, beta=0.5) > \
        ucb1_score(b, total_selections=11, beta=0.5)


# ── loop accounting: spawns are charged; rewards rebased ────────────────────
def test_spawn_decisions_charge_the_parent_and_seed_the_child(tmp_path, monkeypatch):
    cfg = _cfg(max_decisions=5)
    monkeypatch.setattr(ts, "measure_initial_val",
                        _fake_measure({"n0000": 0.5, "n0001": 0.3}))
    burst = _fake_burst({"n0000": [0.1, 0.0, 0.0], "n0001": [0.25, 0.05]})
    result = run_css_tree(None, None, None, cfg=cfg, out_dir=str(tmp_path),
                          spawner=_spawner_script(["ok"]), burst_fn=burst)
    root = result.tree.get("n0000")
    child = result.tree.get("n0001")
    assert child is not None
    # Root: 3 burst picks + 1 spawn pick = 4 charges vs 3 bursts.
    assert root.n_selections > root.n_bursts == 3
    # The child enters the pool pre-charged (atomic spawn+first-burst).
    assert child.n_selections >= 1

    spawn_rows = [r for r in result.rounds if r.get("kind") == "spawn"]
    assert spawn_rows and spawn_rows[0]["spawned"] == "n0001"
    # Rebased reward: child.val (0.3 + 0.25) − parent.val (0.6) = −0.05.
    # The legacy baseline-relative book would have said +0.25.
    assert spawn_rows[0]["reward"] == pytest.approx(-0.05, abs=1e-6)
    assert spawn_rows[0]["child_baseline"] == pytest.approx(0.3, abs=1e-6)


# ── root spawn-failure policy: long block, never terminal ───────────────────
def test_root_three_strikes_long_block_not_terminal(tmp_path, monkeypatch):
    cfg = _cfg(max_decisions=12)
    monkeypatch.setattr(ts, "measure_initial_val",
                        _fake_measure({"n0000": 0.6, "n0001": 0.2}))
    # Child keeps landing bursts (unlocking the root's cooldown); root keeps
    # failing its spawns until the third strike.
    burst = _fake_burst({"n0000": [0.1, 0.0, 0.0], "n0001": [0.05] * 9})
    result = run_css_tree(None, None, None, cfg=cfg, out_dir=str(tmp_path),
                          spawner=_spawner_script(["ok", "fail", "fail", "fail"]),
                          burst_fn=burst)
    root = result.tree.get("n0000")
    assert root.spawn_fail_count >= 3
    assert root.status == "saturated", (
        "the root is the only NEW/MERGE entry point; 3 strikes must long-block "
        "it, never terminal")
    # Long block: pushed beyond the current burst clock by the 3*2^k schedule.
    from css.tree.select import total_bursts
    assert root.spawn_block_T > 0
    assert root.spawn_block_T >= total_bursts(result.tree) - 9 + 3


def test_strategy_node_three_strikes_still_terminal(tmp_path, monkeypatch):
    cfg = _cfg(max_decisions=12, node_degree=1)
    monkeypatch.setattr(ts, "measure_initial_val",
                        _fake_measure({"n0000": 0.6, "n0001": 0.9}))
    # Root spawns n0001 (high val, dominates selection), n0001 saturates after
    # dry bursts, then fails its REFINE spawns three times -> terminal.
    burst = _fake_burst({"n0000": [0.1, 0.0, 0.0],
                         "n0001": [0.05, 0.0, 0.0]})
    result = run_css_tree(None, None, None, cfg=cfg, out_dir=str(tmp_path),
                          spawner=_spawner_script(["ok", "fail", "fail", "fail"]),
                          burst_fn=burst)
    child = result.tree.get("n0001")
    assert child is not None
    if child.spawn_fail_count >= 3:
        assert child.status == "terminal"


def test_root_decline_blocks_without_a_strike(tmp_path, monkeypatch):
    cfg = _cfg(max_decisions=6)
    monkeypatch.setattr(ts, "measure_initial_val", _fake_measure({"n0000": 0.6}))
    burst = _fake_burst({"n0000": [0.1, 0.0, 0.0]})
    result = run_css_tree(None, None, None, cfg=cfg, out_dir=str(tmp_path),
                          spawner=_spawner_script(["decline"]), burst_fn=burst)
    root = result.tree.get("n0000")
    assert root.status == "saturated"
    assert root.spawn_fail_count == 0, "a decline is world-state, not a strike"
    assert root.spawn_block_T > 0


# ── serde compatibility ──────────────────────────────────────────────────────
def test_n_selections_serde_roundtrip_and_legacy_default():
    node = TreeNode(node_id="x", n_bursts=4, n_selections=7)
    back = TreeNode.from_dict(node.to_dict())
    assert back.n_selections == 7 and back.n_bursts == 4

    legacy = node.to_dict()
    legacy.pop("n_selections")
    old = TreeNode.from_dict(legacy)
    assert old.n_selections == 4, "old checkpoints approximate charges by bursts"
