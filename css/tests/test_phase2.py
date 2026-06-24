"""Phase 2 smoke tests: task execution & rollout infrastructure.

Runnable two ways:
    python -m css.tests.test_phase2      # standalone, prints PASS/FAIL summary
    pytest css/tests/test_phase2.py      # standard collection

These tests exercise the rollout stack with deterministic stubs only — they
never touch the network, an LLM API, or the SpreadsheetBench dataset. The frozen
task agent is replaced by :class:`StubLLMClient`; the SpreadsheetBench env is
replaced by :class:`FakeTaskEnv`. Real ``skillopt`` / ``openpyxl`` imports inside
the concrete env are lazy, so importing every Phase 2 module succeeds without
those packages installed (test 9 guards exactly that contract).
"""
from __future__ import annotations

import importlib
import tempfile

from css.config import CSSConfig
from css.data.rollout import (
    TaskResult,
    TaskRolloutGroup,
    aggregate_scores,
    group_rollouts,
)
from css.data.tree import TreeNode
from css.envs.spreadsheetbench.task_interface import FakeTaskEnv
from css.model.client import (
    OptimizerOnlyClient,
    RouterLLMClient,
    StubLLMClient,
    build_clients,
)
from css.rollout.batch import batch_rollout, grouped_batch_rollout
from css.rollout.contrastive import (
    ContrastiveDivergence,
    extract_contrastive_pairs,
    persistent_fail_tasks,
)
from css.rollout.selection_eval import evaluate_candidate, selection_set_rollout
from css.skill_document import SkillDocument
from css.trajectory import truncate_tool_results


# ── Helpers ───────────────────────────────────────────────────────────────────


def _result(task_id: str, rollout_index: int, hard: int, *, soft: float | None = None) -> TaskResult:
    """Hand-build a TaskResult with a non-empty trajectory."""
    return TaskResult(
        task_id=task_id,
        rollout_index=rollout_index,
        hard=hard,
        soft=soft if soft is not None else float(hard),
        n_cases=1,
        n_pass=hard,
        fail_reason="" if hard else "fail",
        messages=[{"role": "user", "content": f"{task_id} r{rollout_index}"}],
        n_turns=1,
    )


# ── 1. First Law boundary ─────────────────────────────────────────────────────
def test_build_clients_enforces_first_law():
    cfg = CSSConfig()
    target_view, optimizer_view = build_clients(cfg)

    # The target view must be structurally incapable of calling the optimizer.
    for call in (
        lambda: target_view.complete_optimizer("s", "u"),
        lambda: target_view.complete_optimizer_messages([{"role": "user", "content": "u"}]),
    ):
        raised = False
        try:
            call()
        except RuntimeError:
            raised = True
        assert raised, "target view must raise on optimizer calls"

    # The optimizer view must be structurally incapable of calling the target.
    raised = False
    try:
        optimizer_view.complete_target("s", "u")
    except RuntimeError:
        raised = True
    assert raised, "optimizer view must raise on target calls"


# ── 2. batch_rollout counts / stamping / grouping ─────────────────────────────
def test_batch_rollout_counts_and_grouping():
    items = [{"id": "t1"}, {"id": "t2"}, {"id": "t3"}]
    env = FakeTaskEnv(items)
    client, _ = build_clients(CSSConfig())  # target view; FakeTaskEnv ignores it
    k = 3
    with tempfile.TemporaryDirectory() as td:
        results = batch_rollout(
            env,
            items,
            "skill",
            client,
            k_rollouts=k,
            out_dir=td,
            max_workers=4,
            epoch=7,
            node_id="nodeX",
        )

    assert len(results) == k * len(items)
    for r in results:
        assert 0 <= r.rollout_index < k
        assert r.epoch == 7
        assert r.node_id == "nodeX"

    # group_rollouts buckets the flat list back to one group per task. The batch
    # runs concurrently, so the *completion* order (hence first-seen task order)
    # is nondeterministic; group_rollouts only guarantees first-seen order OF THE
    # LIST IT IS GIVEN. So assert the bucketing (count + membership + per-group k),
    # and separately assert group_rollouts preserves first-seen order on a fixed
    # input list rather than over-specifying the concurrent batch ordering.
    groups = group_rollouts(results)
    assert len(groups) == len(items)
    assert {g.task_id for g in groups} == {"t1", "t2", "t3"}
    for g in groups:
        assert len(g.rollouts) == k

    # Deterministic first-seen-order contract on a hand-ordered list.
    ordered = group_rollouts(
        [_result("t3", 0, 1), _result("t1", 0, 1), _result("t3", 1, 0), _result("t1", 1, 1)]
    )
    assert [g.task_id for g in ordered] == ["t3", "t1"]


# ── 3. FakeTaskEnv hydration contract ─────────────────────────────────────────
def test_fake_env_run_one_nonempty_messages():
    env = FakeTaskEnv([{"id": "t1", "instruction": "do x"}])
    client = StubLLMClient()
    with tempfile.TemporaryDirectory() as td:
        res = env.run_one({"id": "t1", "instruction": "do x"}, "skill", client, td, rollout_index=2)
    assert isinstance(res, TaskResult)
    assert res.task_id == "t1"
    assert res.rollout_index == 2
    assert len(res.messages) >= 1, "FakeTaskEnv must yield a non-empty trajectory"


# ── 4. aggregate_scores ────────────────────────────────────────────────────────
def test_aggregate_scores_cases():
    # 2 pass / 1 fail of the SAME task -> majority pass -> task_hard 1.0, hard 2/3.
    group = [_result("t1", 0, 1), _result("t1", 1, 1), _result("t1", 2, 0)]
    scores = aggregate_scores(group)
    assert scores["task_hard"] == 1.0
    assert abs(scores["hard"] - (2 / 3)) < 1e-9
    assert scores["n_rollouts"] == 3 and scores["n_tasks"] == 1

    # All-fail -> task_hard 0.0.
    allfail = [_result("t1", i, 0) for i in range(3)]
    assert aggregate_scores(allfail)["task_hard"] == 0.0

    # Empty -> all-zero dict, n_rollouts == 0.
    empty = aggregate_scores([])
    assert empty["n_rollouts"] == 0
    assert empty["hard"] == 0.0 and empty["soft"] == 0.0 and empty["task_hard"] == 0.0


# ── 5. contrastive extraction / persistent fail ───────────────────────────────
def test_contrastive_pairs_and_persistent_fail():
    # 1 success + 2 failures -> exactly 2 pairs.
    mixed = TaskRolloutGroup.from_results(
        "t1", [_result("t1", 0, 1), _result("t1", 1, 0), _result("t1", 2, 0)]
    )
    all_pass = TaskRolloutGroup.from_results("t2", [_result("t2", i, 1) for i in range(3)])
    all_fail = TaskRolloutGroup.from_results("t3", [_result("t3", i, 0) for i in range(3)])

    assert len(extract_contrastive_pairs([mixed])) == 2
    assert len(extract_contrastive_pairs([all_pass])) == 0
    assert len(extract_contrastive_pairs([all_fail])) == 0

    # persistent_fail_tasks picks only the all-fail group.
    persistent = persistent_fail_tasks([mixed, all_pass, all_fail])
    assert [g.task_id for g in persistent] == ["t3"]


# ── 6. truncation policy (D7) ──────────────────────────────────────────────────
def test_truncate_only_oversized_tool_result():
    big = "X" * 20000
    short = "short observation"
    messages = [
        {"role": "user", "content": short},
        {"role": "tool", "content": big},
        {"role": "assistant", "content": "reasoning text never clipped"},
    ]
    out = truncate_tool_results(messages, 8000)

    assert len(out) == 3
    # Short and reasoning messages are byte-identical.
    assert out[0]["content"] == short
    assert out[2]["content"] == "reasoning text never clipped"
    # Only the >= 8000 message is elided.
    assert out[1]["content"] != big
    assert "...[truncated" in out[1]["content"]
    assert len(out[1]["content"]) < len(big)
    # Head + tail preserved.
    assert out[1]["content"].startswith("X" * 100)
    assert out[1]["content"].endswith("X" * 100)


# ── 7. selection_set_rollout == aggregate task_hard, deterministic ─────────────
def test_selection_set_rollout_returns_task_hard():
    val_items = [{"id": "v1"}, {"id": "v2"}]
    # Deterministic scorer: v1 always passes, v2 always fails.
    def scorer(item, rollout_index):
        return 1 if item["id"] == "v1" else 0

    env = FakeTaskEnv({"val": val_items}, scorer=scorer)
    client = StubLLMClient()
    doc = SkillDocument(skill_dir="/unused", strategy="S body", rules="R body")
    with tempfile.TemporaryDirectory() as td:
        score = selection_set_rollout(
            env, doc, val_items, client, k_rollouts=3, out_dir=td, max_workers=4
        )
    # v1 passes (majority pass), v2 fails -> 1 of 2 tasks -> 0.5.
    assert isinstance(score, float)
    assert 0.0 <= score <= 1.0
    assert score == 0.5

    # evaluate_candidate composes a SkillDocument from a node + rules and agrees.
    node = TreeNode(node_id="n1", strategy="S body")
    cfg = CSSConfig(k_rollouts=3, max_api_workers=4, task_timeout_s=600)
    with tempfile.TemporaryDirectory() as td:
        score2 = evaluate_candidate(env, node, "R body", val_items, client, cfg, td)
    assert score2 == 0.5


# ── 8. ContrastiveDivergence round-trip ────────────────────────────────────────
def test_contrastive_divergence_round_trip():
    cd = ContrastiveDivergence(
        task_id="t1",
        success_rollout_index=0,
        failure_rollout_index=2,
        divergence_point="step 3: read range before write",
        cognitive_difference="success verified the sheet name first",
        is_systematic=True,
    )
    again = ContrastiveDivergence.from_dict(cd.to_dict())
    assert again == cd


# ── 9. import smoke (no skillopt / openpyxl) ───────────────────────────────────
def test_phase2_modules_import_without_skillopt():
    for mod in (
        "css.model.client",
        "css.trajectory",
        "css.envs.spreadsheetbench.task_interface",
        "css.rollout.batch",
        "css.rollout.selection_eval",
        "css.rollout.contrastive",
    ):
        importlib.import_module(mod)
    # grouped_batch_rollout / OptimizerOnlyClient are referenced to keep imports live.
    assert grouped_batch_rollout is not None
    assert OptimizerOnlyClient is not None


def test_router_client_resolves_backend_and_calls_through():
    """Covers the real RouterLLMClient path (the gap that hid the backend blocker).

    Stub-based tests never exercise _resolve_backend; this asserts (a) the
    backend-name mapping resolves to a concrete module and rejects unknowns, and
    (b) complete_target/optimizer set the right deployment and forward args.
    """
    # (a) mapping: 'claude' resolves to the claude_backend module; unknown raises.
    rc = RouterLLMClient(target_model="m-target", optimizer_model="m-opt", backend="claude")
    mod = rc._resolve_backend()
    assert mod.__name__.endswith("claude_backend")
    bad = RouterLLMClient("a", "b", backend="no-such-backend")
    raised = False
    try:
        bad._resolve_backend()
    except ValueError:
        raised = True
    assert raised, "unknown backend must raise ValueError"

    # (b) call-through with a fake backend module (no network).
    calls: dict = {}

    class _FakeBackend:
        def set_target_deployment(self, d):
            calls["target_dep"] = d

        def set_optimizer_deployment(self, d):
            calls["opt_dep"] = d

        def chat_target(self, *, system, user, max_completion_tokens, stage):
            calls["target"] = (system, user, max_completion_tokens, stage)
            return "T-OUT", {"total_tokens": 1}

        def chat_optimizer(self, *, system, user, max_completion_tokens, stage):
            calls["opt"] = (system, user, max_completion_tokens, stage)
            return "O-OUT", {"total_tokens": 2}

        def chat_optimizer_messages(self, *, messages, max_completion_tokens, stage):
            calls["opt_msgs"] = (messages, max_completion_tokens, stage)
            return "OM-OUT", {"total_tokens": 3}

    fake = _FakeBackend()
    rc._resolve_backend = lambda: fake  # type: ignore[method-assign]

    assert rc.complete_target("sys", "usr", max_tokens=128) == "T-OUT"
    assert calls["target_dep"] == "m-target"
    assert calls["target"] == ("sys", "usr", 128, "target")

    txt, usage = rc.complete_optimizer("s2", "u2", max_tokens=64)
    assert txt == "O-OUT" and calls["opt_dep"] == "m-opt" and usage["total_tokens"] == 2

    txt2, _ = rc.complete_optimizer_messages([{"role": "user", "content": "hi"}], max_tokens=32)
    assert txt2 == "OM-OUT" and calls["opt_msgs"][2] == "optimizer"


# ── Standalone runner ─────────────────────────────────────────────────────────
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
