"""Phase 6 smoke tests: Tree Management & Orchestration (the CSS capstone).

Runnable two ways:
    python -m css.tests.test_phase6      # standalone, prints PASS/FAIL summary
    pytest css/tests/test_phase6.py      # standard collection

Every test is deterministic and stub-based — no network, no LLM API, no
SpreadsheetBench dataset, no faiss, and no sentence-transformers model download.

  * Task rollouts come from :class:`FakeTaskEnv` (a canned, scorer-driven double).
  * The optimizer LLM is replaced by :class:`StubLLMClient` whose ``optimizer_fn``
    is a single ROUTER (:func:`_optimizer_router`) that dispatches a canned JSON
    response for every Phase-4 (Layer 1/2) and Phase-5 (root-cause / strategy /
    neg-archive / retro / inherit / refine) prompt the orchestrator drives. This
    lets cold_start and run_css run the FULL Phase-1..5 stack end-to-end.
  * Embeddings come from :class:`StubEmbedder` (hash-seeded, L2-normalized).
  * SELECT / PRUNE / branching are pure: their tests assert exact behavior; the
    orchestrator tests exercise the wiring (types / ranges / determinism), not
    exact scores.
"""
from __future__ import annotations

import json
import os
import tempfile

from css.config import CSSConfig
from css.data.pattern import Observation, OccurrencePoint, PatternLibrary, PatternRecord
from css.data.rollout import TaskResult, TaskRolloutGroup
from css.data.step_buffer import StepBufferEntry
from css.data.tree import SearchTree, TreeNode
from css.envs.spreadsheetbench.task_interface import FakeTaskEnv
from css.model.client import StubLLMClient

from css.tree.select import select_batch, select_node, ucb1_score
from css.tree.prune import paired_bootstrap_diff_ci, prune_node, should_prune
from css.orchestrator import RoundResult, RunResult
from css.logging_viz import format_tree, tree_snapshot, write_run_artifacts


# ── Canned optimizer responses (per prompt) ─────────────────────────────────────

_OBS_JSON = json.dumps(
    [
        {
            "what": "committed to the first interpretation without re-reading",
            "cognitive_aspect": "premature-commitment",
            "evidence": "stated 'this must be column B' before inspecting the sheet",
            "consequence": "computed against the wrong column",
            "significance": "critical",
        }
    ]
)

_DIVERGENCE_JSON = json.dumps(
    {
        "divergence_point": "after reading the prompt, before the first action",
        "cognitive_difference": "success enumerated assumptions; failure committed early",
        "is_systematic": True,
    }
)

_UNIFY_JSON = json.dumps(
    {
        "name": "premature commitment",
        "description": "commits to one reading before gathering evidence",
        "cognitive_aspect": "premature-commitment",
        "polarity": "failure",
    }
)

_COUNTERPART_JSON = json.dumps({"pairs": []})

_ROOT_CAUSE_JSON = json.dumps(
    [
        {
            "pattern_ids": ["p0000"],
            "behavioral": "committed to the first column mapping and never re-checked",
            "process": "treated the first plausible reading as settled",
            "strategy": "the existing plan locks an interpretation early",
            "assumption": "the first reading of an ambiguous input is usually right",
            "leverage": "",
            "l0_explanation": "a rule cannot force a re-read it never perceives as needed",
            "evidence": {
                "behavioral": "THOUGHT: 'column B is the date, proceeding'",
                "process": "THOUGHT: 'the mapping is clear, no need to verify'",
                "strategy": "### Plan then execute: lock a plan and follow it",
                "assumption": "first reading is treated as ground truth",
            },
        }
    ]
)

_STRATEGY_JSON = json.dumps(
    {
        "strategy_text": (
            "## Cognitive Strategy\n"
            "### Re-read before committing\n"
            "Before locking any interpretation of an ambiguous input, perform one "
            "cheap re-read against the source and confirm the mapping.\n"
            "### Plan then execute\n"
            "Once the interpretation is confirmed, plan and follow it."
        ),
        "rationale": "Breaking 'first reading is right' forces a cheap re-read.",
        "targeted_pattern_ids": ["p0000"],
    }
)

_NEG_PROCEED_JSON = json.dumps(
    {"proceed": True, "difference": "different trigger: fires on detected ambiguity"}
)

_RETRO_HIGH_JSON = json.dumps(
    {
        "positive_evidence": ["passing rollouts already re-read before committing"],
        "counterfactuals": ["task t1 would likely have flipped"],
        "coverage": 0.5,
    }
)

_INHERIT_JSON = json.dumps({"kept_rules": [], "dropped": []})

_REFINE_OK_JSON = json.dumps(
    {
        "strategy_text": (
            "## Cognitive Strategy\n"
            "### Re-read before committing\n"
            "Re-read the input once to confirm the interpretation, then lock it.\n"
            "### Plan then execute\n"
            "Once the interpretation is confirmed, plan and follow it."
        ),
        "changed_subsections": ["Re-read before committing"],
        "rationale": "A cheap re-read before locking the interpretation.",
        "rules_cleanup": {"reasoning": "no rules to clean", "edits": []},
        "escalate": "",
    }
)


def _optimizer_router(system: str, user: str) -> str:
    """Dispatch a canned response for every Phase-4 / Phase-5 optimizer prompt.

    Layer-1 prompts are distinguished by content (a contrastive pair prompt
    carries both SUCCESS and FAILURE markers); the Phase-5 prompts anchor on the
    distinctive opening sentence of each system prompt (matching test_phase5).
    """
    s = system.lstrip()
    # Phase 5 prompts (anchored on the system prompt's first sentence).
    if s.startswith("You are a root-cause analyst"):
        return _ROOT_CAUSE_JSON
    if s.startswith("You are a COGNITIVE-STRATEGY designer"):
        return _STRATEGY_JSON
    if s.startswith("You are guarding against"):
        return _NEG_PROCEED_JSON
    if s.startswith("You are running a CHEAP"):
        return _RETRO_HIGH_JSON
    if s.startswith("You are deciding which low-level"):
        return _INHERIT_JSON
    if s.startswith("You are refining"):
        return _REFINE_OK_JSON
    # Phase 4 Layer 1/2 prompts (distinguished by content).
    if "SUCCESS" in user and "FAILURE" in user:
        return _DIVERGENCE_JSON
    low = system.lower()
    if "unify" in low or "unify" in user.lower():
        return _UNIFY_JSON
    if "pair each failure" in low or "counterpart" in low:
        return _COUNTERPART_JSON
    return _OBS_JSON


def _stub_client() -> StubLLMClient:
    return StubLLMClient(optimizer_fn=_optimizer_router)


# ── Fixtures (plain helpers; no pytest fixtures so the __main__ runner works) ────


def _node(node_id: str, *, val: float = 0.0, n_steps: int = 0, slope: float = 0.0,
          status: str = "active", parent: str | None = None,
          branch: str = "ROOT") -> TreeNode:
    """A TreeNode with a hand-stuffed step buffer to control n_steps / slope.

    accept_slope is driven by the StepBuffer's recorded accepts; rather than
    reconstruct the exact internal step records we stub ``accept_slope`` and
    ``n_steps`` via a tiny subclass-free shim on the instance.
    """
    nd = TreeNode(node_id=node_id, branch_type=branch, parent_id=parent,
                  val_score=val, status=status)
    # n_steps is derived from the step buffer; append accepted entries to set it.
    for i in range(n_steps):
        nd.step_buffer.append(
            StepBufferEntry(step=i, action="accept", score_before=val, score_after=val)
        )
    # Shadow accept_slope with the desired controlled value (instance attribute
    # overrides the method for this test double).
    object.__setattr__(nd, "accept_slope", lambda window, _s=slope: _s)
    return nd


def _push(buf, *, accepted: bool, n: int = 1) -> None:
    """Append ``n`` accept/reject step entries to a StepBuffer."""
    base = len(buf)
    for i in range(n):
        buf.append(
            StepBufferEntry(
                step=base + i,
                action="accept" if accepted else "reject",
                score_before=0.0,
                score_after=0.0,
            )
        )


def _item(task_id: str, instruction: str = "do it") -> dict:
    return {"id": task_id, "instruction": instruction, "instruction_type": "cell"}


def _fail_scorer(item, rollout_index):
    """Every rollout fails -> persistent-fail groups + saturation pressure."""
    return 0


# ── 1. SELECT (ucb1_score / select_node / select_batch) ─────────────────────────


def test_ucb1_unvisited_is_finite():
    # Tree-search mechanism: spawn + first-burst is atomic, so n_bursts == 0
    # only ever occurs for the run-opening root; the legacy `inf` branch (the
    # chain-degeneration mechanism: fresh nodes unconditionally selected) is
    # deliberately GONE. n_bursts is clamped to >= 1 in the formula.
    nd = _node("a", val=0.5, n_steps=0, slope=0.0)
    assert nd.n_bursts == 0
    score = ucb1_score(nd, total_bursts=100, alpha=0.5, beta=0.5, window=10)
    assert score != float("inf")
    one = _node("b", val=0.5, n_steps=0, slope=0.0)
    one.n_bursts = 1
    assert score == ucb1_score(one, total_bursts=100, alpha=0.5, beta=0.5, window=10)


def test_ucb1_visited_ranks_by_value_and_slope():
    good = _node("g", val=0.8, n_steps=10, slope=0.2)
    bad = _node("b", val=0.2, n_steps=10, slope=-0.1)
    good.n_bursts = 2
    bad.n_bursts = 2
    sg = ucb1_score(good, total_bursts=100, alpha=0.5, beta=0.5, window=10)
    sb = ucb1_score(bad, total_bursts=100, alpha=0.5, beta=0.5, window=10)
    assert sg > sb
    # Same n_bursts/total -> same exploration term; the win is exploitation+slope.


def test_ucb1_fewer_bursts_bigger_bonus():
    a = _node("a", val=0.5, n_steps=10, slope=0.0)
    b = _node("b", val=0.5, n_steps=10, slope=0.0)
    a.n_bursts = 1
    b.n_bursts = 8
    sa = ucb1_score(a, total_bursts=9, alpha=0.5, beta=0.5, window=10)
    sb = ucb1_score(b, total_bursts=9, alpha=0.5, beta=0.5, window=10)
    assert sa > sb


def test_select_node_argmax():
    tree = SearchTree()
    tree.add_root(_node("n0", val=0.3, n_steps=10))
    tree.add_child("n0", _node("n1", val=0.9, n_steps=10, parent="n0", branch="PROPOSAL"))
    chosen = select_node(tree, cfg=CSSConfig())
    assert chosen is not None and chosen.node_id == "n1"


def test_select_node_none_when_no_active():
    tree = SearchTree()
    tree.add_root(_node("n0", val=0.3, n_steps=10, status="pruned"))
    assert select_node(tree, cfg=CSSConfig()) is None


def test_select_batch_size_and_order_and_excludes_pruned():
    cfg = CSSConfig(concurrency_limit=2)
    tree = SearchTree()
    tree.add_root(_node("n0", val=0.1, n_steps=10))
    tree.add_child("n0", _node("n1", val=0.9, n_steps=10, parent="n0", branch="PROPOSAL"))
    tree.add_child("n0", _node("n2", val=0.5, n_steps=10, parent="n0", branch="PROPOSAL"))
    tree.add_child("n0", _node("n3", val=0.99, n_steps=10, parent="n0",
                               branch="PROPOSAL", status="pruned"))
    batch = select_batch(tree, cfg=cfg)
    # K = min(active=3, concurrency_limit=2) = 2; highest-scored first; pruned excluded.
    assert len(batch) == 2
    assert batch[0].node_id == "n1"  # highest val among visited active
    assert all(n.status == "active" for n in batch)
    assert "n3" not in {n.node_id for n in batch}


# ── 2. PRUNE (paired bootstrap + should_prune gate) ─────────────────────────────


def test_paired_bootstrap_deterministic():
    a = [1, 0, 1, 1, 0, 1]
    b = [0, 0, 1, 0, 0, 1]
    ci1 = paired_bootstrap_diff_ci(a, b, resamples=500, ci=0.95, seed=42)
    ci2 = paired_bootstrap_diff_ci(a, b, resamples=500, ci=0.95, seed=42)
    assert ci1 == ci2  # deterministic under a fixed seed


def test_paired_bootstrap_better_sibling_ci_positive():
    node_passes = [0, 0, 0, 1]
    sibling_passes = [1, 1, 1, 1]
    lo, hi = paired_bootstrap_diff_ci(
        node_passes, sibling_passes, resamples=1000, ci=0.95, seed=42
    )
    assert lo > 0  # sibling clearly better -> CI lower bound > 0
    assert hi >= lo


def test_paired_bootstrap_equal_ci_contains_zero():
    v = [1, 0, 1, 0, 1, 0]
    lo, hi = paired_bootstrap_diff_ci(v, list(v), resamples=1000, ci=0.95, seed=42)
    assert lo <= 0.0 <= hi


def test_should_prune_requires_all_conditions():
    cfg = CSSConfig(N=3, min_steps=4, prune_bootstrap_resamples=500)
    # A saturated node with enough steps and a clearly-better sibling -> prune.
    node = TreeNode(node_id="n1", parent_id="n0", branch_type="PROPOSAL")
    # min_steps total steps, with the last N being consecutive rejects -> saturated.
    _push(node.step_buffer, accepted=True, n=cfg.min_steps - cfg.N)
    _push(node.step_buffer, accepted=False, n=cfg.N)
    assert node.n_steps >= cfg.min_steps
    assert node.is_saturated(cfg.N)
    sibling = TreeNode(node_id="n2", parent_id="n0", branch_type="PROPOSAL", val_score=0.9)
    node_passes = [0, 0, 0, 1, 0]
    sib_passes = [1, 1, 1, 1, 1]

    prune, reason = should_prune(
        node, sibling, cfg=cfg, node_passes=node_passes, sibling_passes=sib_passes
    )
    assert prune is True
    assert "significantly better" in reason

    # Flip OFF saturation: a fresh node with the same passes but not saturated.
    fresh = TreeNode(node_id="n3", parent_id="n0", branch_type="PROPOSAL")
    _push(fresh.step_buffer, accepted=True, n=cfg.min_steps)  # accepts -> not saturated
    assert not fresh.is_saturated(cfg.N)
    pr2, _ = should_prune(
        fresh, sibling, cfg=cfg, node_passes=node_passes, sibling_passes=sib_passes
    )
    assert pr2 is False

    # Flip OFF min_steps: too few steps.
    cfg_hi = CSSConfig(N=3, min_steps=100, prune_bootstrap_resamples=500)
    pr3, _ = should_prune(
        node, sibling, cfg=cfg_hi, node_passes=node_passes, sibling_passes=sib_passes
    )
    assert pr3 is False

    # Flip OFF CI>0: equal pass vectors -> not significantly better.
    pr4, _ = should_prune(
        node, sibling, cfg=cfg, node_passes=node_passes, sibling_passes=list(node_passes)
    )
    assert pr4 is False


def test_prune_node_sets_status():
    tree = SearchTree()
    tree.add_root(_node("n0", n_steps=10))
    prune_node(tree, "n0")
    assert tree.get("n0").status == "pruned"


# ── 8. logging_viz.tree_snapshot / format_tree ──────────────────────────────────


def test_tree_snapshot_and_format_tree():
    tree = SearchTree()
    root = TreeNode(node_id="n0", branch_type="ROOT", val_score=0.4)
    root.record_learning_point  # attribute exists
    tree.add_root(root)
    child = TreeNode(node_id="n1", branch_type="PROPOSAL", parent_id="n0", val_score=0.6)
    tree.add_child("n0", child)

    snap = tree_snapshot(tree)
    assert snap["root_id"] == "n0"
    assert len(snap["nodes"]) == 2
    fields = {"node_id", "parent_id", "branch_type", "status", "val_score",
              "best_score", "n_steps", "refine_count"}
    for nd in snap["nodes"]:
        assert fields.issubset(nd.keys())
        assert "learning_curve" in nd

    ascii_tree = format_tree(tree)
    assert isinstance(ascii_tree, str) and ascii_tree.strip()
    assert "n0" in ascii_tree and "n1" in ascii_tree


# ── 9. import smoke (no faiss / model / network) ────────────────────────────────


def test_import_smoke_no_heavy_backends():
    import importlib
    import sys

    faiss_before = "faiss" in sys.modules
    for mod in (
        "css.tree.select",
        "css.tree.prune",
        "css.tree_search",
        "css.orchestrator",
        "css.logging_viz",
    ):
        importlib.import_module(mod)
    # Importing Phase 6 must not NEWLY pull faiss at import time.
    if not faiss_before:
        assert "faiss" not in sys.modules
    # Keep referenced symbols live.
    assert all(
        fn is not None
        for fn in (ucb1_score, should_prune, tree_snapshot, format_tree)
    )


# ── Standalone runner ──────────────────────────────────────────────────────────


def _run_all():
    import inspect

    fns = [
        obj for name, obj in sorted(globals().items())
        if name.startswith("test_") and inspect.isfunction(obj)
    ]
    passed = 0
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            import traceback
            print(f"  FAIL  {fn.__name__}: {exc}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed (of {len(fns)})")
    return failed == 0


if __name__ == "__main__":
    import sys

    sys.exit(0 if _run_all() else 1)
