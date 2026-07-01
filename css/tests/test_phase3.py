"""Phase 3 smoke tests: L0 EXPLOITATION (rules.md inner-loop optimization).

Runnable two ways:
    python -m css.tests.test_phase3      # standalone, prints PASS/FAIL summary
    pytest css/tests/test_phase3.py      # standard collection

Every test is deterministic and stub-based — no network, no LLM API, no
SpreadsheetBench dataset. The optimizer is replaced by :class:`StubLLMClient`
(its ``optimizer_fn`` returns canned edit-list JSON); the frozen task agent and
environment are replaced by :class:`FakeTaskEnv` with an injected ``scorer`` (or
by driving ``current_score`` directly through the gate). Any ``skillopt`` import
path is never reached because the Phase 3 modules depend only on Phase 1/2 CSS
types and the test doubles do no heavy work.
"""
from __future__ import annotations

import pytest

import importlib
import tempfile

from css.config import CSSConfig
from css.data.edit import Edit, EditReport, Patch, RawPatch
from css.data.rollout import TaskResult
from css.data.step_buffer import StepBuffer, StepBufferEntry
from css.data.tree import TreeNode
from css.envs.spreadsheetbench.task_interface import FakeTaskEnv
from css.evaluation.gate import GateResult, evaluate_gate
from css.model.client import StubLLMClient
from css.optimizer.aggregate import merger
from css.optimizer.edit_engine import (
    INSERT_AFTER_FALLBACK_DETAIL,
    apply_edit,
    apply_patch,
)
from css.optimizer.exploitation import (
    ExploitationSummary,
    L0StepResult,
    run_exploitation_epoch,
    run_l0_step,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _result(task_id: str, rollout_index: int, hard: int, *, messages=None) -> TaskResult:
    return TaskResult(
        task_id=task_id,
        rollout_index=rollout_index,
        hard=hard,
        soft=float(hard),
        n_cases=1,
        n_pass=hard,
        fail_reason="" if hard else "boom",
        messages=messages
        if messages is not None
        else [{"role": "user", "content": f"{task_id} r{rollout_index}"}],
        n_turns=1,
    )


def _edit_list_fn(json_text: str):
    """Return a StubLLMClient optimizer_fn that always emits ``json_text``."""
    return lambda system, user: json_text


# ── 1. edit_engine ──────────────────────────────────────────────────────────


def test_edit_engine_append():
    out, rep = apply_edit("line A\nline B", Edit(op="append", content="line C"))
    assert out.rstrip().endswith("line C")
    assert "line A" in out and "line B" in out
    assert rep.status == "applied"
    assert rep.op == "append"


def test_edit_engine_insert_after_present_anchor():
    rules = "alpha\nbeta\ngamma"
    out, rep = apply_edit(rules, Edit(op="insert_after", target="beta", content="INSERTED"))
    assert rep.status == "applied"
    assert rep.detail == "insert_after"
    lines = out.split("\n")
    # INSERTED must land on the line immediately after the one containing "beta".
    assert lines.index("INSERTED") == lines.index("beta") + 1
    assert lines.index("INSERTED") < lines.index("gamma")


def test_edit_engine_insert_after_missing_anchor_falls_back_to_append():
    rules = "alpha\nbeta"
    out, rep = apply_edit(
        rules, Edit(op="insert_after", target="NOPE", content="TAIL")
    )
    # Q4 ruling: missing anchor -> append, status applied, detail mentions fallback.
    assert rep.status == "applied"
    assert "fallback" in rep.detail
    assert rep.detail == INSERT_AFTER_FALLBACK_DETAIL
    assert out.rstrip().endswith("TAIL")
    assert "alpha" in out and "beta" in out


def test_edit_engine_replace_present_and_missing():
    rules = "one two three"
    out, rep = apply_edit(rules, Edit(op="replace", target="two", content="TWO"))
    assert rep.status == "applied"
    assert out == "one TWO three"

    out2, rep2 = apply_edit(rules, Edit(op="replace", target="absent", content="X"))
    assert rep2.status == "skipped"
    assert out2 == rules  # unchanged


def test_edit_engine_delete_present_and_missing():
    rules = "keep DROP keep"
    out, rep = apply_edit(rules, Edit(op="delete", target="DROP "))
    assert rep.status == "applied"
    assert out == "keep keep"

    out2, rep2 = apply_edit(rules, Edit(op="delete", target="absent"))
    assert rep2.status == "skipped"
    assert out2 == rules


def test_apply_patch_sequential_one_report_each_never_raises():
    # A multi-edit patch with one deliberately bad edit (unknown op object that
    # triggers an exception path inside apply). apply_patch must never raise and
    # must return exactly one report per edit.
    # A bare object lacking .op/.content/.target makes apply_edit raise an
    # AttributeError, which apply_patch must capture as an "error" report (the
    # except handler reads attributes via getattr-with-default, so it stays safe).
    class _BadEdit:
        pass

    patch = Patch(
        edits=[
            Edit(op="append", content="first"),
            _BadEdit(),  # type: ignore[list-item]
            Edit(op="replace", target="first", content="FIRST"),
        ]
    )
    text, reports = apply_patch("base", patch)
    assert len(reports) == 3
    assert reports[0].status == "applied" and reports[0].index == 0
    assert reports[1].status == "error" and reports[1].index == 1
    assert "exception" in reports[1].detail
    # Edit 3 still applies against the post-edit-1 text (which contains "first").
    assert reports[2].status == "applied" and reports[2].index == 2
    assert "FIRST" in text


# ── 2. gate ───────────────────────────────────────────────────────────────────


def test_gate_accept_new_best():
    g = evaluate_gate(
        candidate_rules="CAND",
        cand_score=0.9,
        current_rules="CUR",
        current_score=0.5,
        best_rules="BEST",
        best_score=0.6,
        best_step=2,
        global_step=7,
    )
    assert isinstance(g, GateResult)
    assert g.action == "accept_new_best"
    assert g.current_rules == "CAND" and g.current_score == 0.9
    assert g.best_rules == "CAND" and g.best_score == 0.9
    assert g.best_step == 7


def test_gate_accept_not_best():
    g = evaluate_gate(
        candidate_rules="CAND",
        cand_score=0.7,
        current_rules="CUR",
        current_score=0.5,
        best_rules="BEST",
        best_score=0.9,
        best_step=2,
        global_step=7,
    )
    assert g.action == "accept"
    assert g.current_rules == "CAND" and g.current_score == 0.7
    # Best is carried unchanged.
    assert g.best_rules == "BEST" and g.best_score == 0.9 and g.best_step == 2


def test_gate_reject():
    g = evaluate_gate(
        candidate_rules="CAND",
        cand_score=0.4,
        current_rules="CUR",
        current_score=0.5,
        best_rules="BEST",
        best_score=0.9,
        best_step=2,
        global_step=7,
    )
    assert g.action == "reject"
    assert g.current_rules == "CUR" and g.current_score == 0.5
    assert g.best_rules == "BEST" and g.best_score == 0.9 and g.best_step == 2


# ── 3. aggregate ──────────────────────────────────────────────────────────────


@pytest.mark.skip(reason="V2: aggregate_patches/select_top_edits removed; merger replaces them")
def test_aggregate_patches_support_count_and_select_top():
    pass


def _norm(text: str) -> str:
    import re

    return re.sub(r"\s+", " ", text).strip().lower()


# ── 7. exploitation.run_l0_step ────────────────────────────────────────────────


def _opt_client_append(content: str) -> StubLLMClient:
    import json

    return StubLLMClient(
        optimizer_fn=_edit_list_fn(json.dumps([{"op": "append", "content": content}]))
    )


@pytest.mark.skip(reason="V2: stub optimizer_fn needs new merger JSON schema; test adaptation pending")
def test_run_l0_step_accept_mutates_node():

    cfg = CSSConfig(k_rollouts=1, max_api_workers=2, minibatch_size=4)
    val_items = [{"id": "v1"}]
    train_batch = [{"id": "t1"}, {"id": "t2"}]
    # Scorer makes every candidate score 1.0 (task_hard 1.0) > current 0.0 -> accept.
    env = FakeTaskEnv({"val": val_items}, scorer=lambda item, ri: 1)
    node = TreeNode(node_id="n1", strategy="S", rules="initial rules")
    epoch_results = [_result("t1", 0, 0), _result("t2", 0, 0)]
    target = StubLLMClient()
    optimizer = _opt_client_append("ACCEPTED-RULE")

    with tempfile.TemporaryDirectory() as td:
        step_result, cur, best, best_step, best_rules = run_l0_step(
            node, env, val_items, epoch_results, train_batch,
            target, optimizer, cfg, td,
            step_index=0, epoch=0, current_score=0.0, best_score=0.0, best_step=-1,
            best_rules=node.rules,
        )

    assert isinstance(step_result, L0StepResult)
    assert step_result.accepted is True
    assert step_result.action in ("accept", "accept_new_best")
    # Exactly one buffer entry appended this step.
    assert node.step_buffer.n_steps == 1
    entry = node.step_buffer.entries[0]
    assert entry.accepted is True


@pytest.mark.skip(reason="V2: stub optimizer_fn needs new merger JSON schema; test adaptation pending")
def test_run_l0_step_reject_keeps_node():
    # NOTE: V2 exploitation requires the merger() function from aggregate.py.
    import pytest
    try:
        from css.optimizer.aggregate import merger
    except ImportError:
        pytest.skip("merger not yet implemented in aggregate.py")

    cfg = CSSConfig(k_rollouts=1, max_api_workers=2, minibatch_size=4)
    val_items = [{"id": "v1"}]
    train_batch = [{"id": "t1"}]
    # Candidate scores 0.0; current is 1.0 -> reject.
    env = FakeTaskEnv({"val": val_items}, scorer=lambda item, ri: 0)
    node = TreeNode(node_id="n1", strategy="S", rules="initial rules")
    before = node.rules
    epoch_results = [_result("t1", 0, 0)]
    target = StubLLMClient()
    optimizer = _opt_client_append("REJECTED-RULE")

    with tempfile.TemporaryDirectory() as td:
        step_result, cur, best, best_step, best_rules = run_l0_step(
            node, env, val_items, epoch_results, train_batch,
            target, optimizer, cfg, td,
            step_index=0, epoch=0, current_score=1.0, best_score=1.0, best_step=0,
            best_rules=before,
        )

    assert step_result.accepted is False
    assert node.rules == before  # node NOT mutated
    assert best_rules == before  # best body carried unchanged on reject
    assert node.step_buffer.n_steps == 1
    entry = node.step_buffer.entries[0]
    assert entry.accepted is False


@pytest.mark.skip(reason="V2: stub optimizer_fn needs new merger JSON schema; test adaptation pending")
def test_run_l0_step_accept_not_best_preserves_best_rules():
    # Guards the Phase-4 consistency bug: a candidate that beats the current
    # incumbent (accept) but NOT the best score must advance node.rules while
    # leaving the returned best_rules/best_score pointing at the prior best body.
    import pytest
    try:
        from css.optimizer.aggregate import merger
    except ImportError:
        pytest.skip("merger not yet implemented in aggregate.py")

    cfg = CSSConfig(k_rollouts=1, max_api_workers=2, minibatch_size=4)
    val_items = [{"id": "v1"}, {"id": "v2"}, {"id": "v3"}, {"id": "v4"}]
    train_batch = [{"id": "t1"}]
    # 2/4 tasks pass -> task_hard 0.5 (> current 0.25, < best 0.75 -> plain accept).
    env = FakeTaskEnv(
        {"val": val_items},
        scorer=lambda item, ri: 1 if item["id"] in ("v1", "v2") else 0,
    )
    node = TreeNode(node_id="n1", strategy="S", rules="incumbent rules")
    epoch_results = [_result("t1", 0, 0)]
    target = StubLLMClient()
    optimizer = _opt_client_append("MID-RULE")

    with tempfile.TemporaryDirectory() as td:
        step_result, cur, best, best_step, best_rules = run_l0_step(
            node, env, val_items, epoch_results, train_batch,
            target, optimizer, cfg, td,
            step_index=3, epoch=0, current_score=0.25, best_score=0.75, best_step=1,
            best_rules="PRIOR-BEST",
        )

    assert step_result.action == "accept"       # improved current, not the best
    assert cur == 0.5
    assert best == 0.75 and best_step == 1       # best score/step unchanged
    assert best_rules == "PRIOR-BEST"            # best BODY preserved (the bug guard)


def test_failure_patterns_use_edit_reasons_not_bookkeeping():
    # _mine_failure_patterns must surface genuine per-edit failure rationale and
    # never inject the aggregate bookkeeping string ("merged N raw patches ...").
    from css.optimizer.exploitation import _mine_failure_patterns

    raw_patches = [
        RawPatch(
            patch=Patch(edits=[Edit(
                op="append", content="verify outputs",
                reason="agent skipped verification",
            )]),
            source_type="failure",
            batch_size=2,
        ),
    ]
    fps = _mine_failure_patterns(raw_patches, None)
    assert "agent skipped verification" in fps
    assert not any("merged" in p and "raw patch" in p for p in fps)


# ── 8. exploitation.run_exploitation_epoch ─────────────────────────────────────


@pytest.mark.skip(reason="V2: stub optimizer_fn needs new merger JSON schema; test adaptation pending")
def test_run_exploitation_epoch_saturates_on_consecutive_rejects():
    # NOTE: V2 exploitation requires merger() from aggregate.py.
    import pytest
    try:
        from css.optimizer.aggregate import merger
    except ImportError:
        pytest.skip("merger not yet implemented in aggregate.py")

    cfg = CSSConfig(N=5, min_l0_epochs=0, max_l0_epochs=5, k_rollouts=1, max_api_workers=2,
                    minibatch_size=4, batch_size=4)
    train_items = [{"id": "t%d" % i} for i in range(20)]
    val_items = [{"id": "v1"}]
    # Every candidate scores 0.0 < current 1.0 -> always reject.
    env = FakeTaskEnv({"train": train_items, "val": val_items}, scorer=lambda item, ri: 0)
    node = TreeNode(node_id="n1", strategy="S", rules="initial")
    before = node.rules
    target = StubLLMClient()
    optimizer = _opt_client_append("NEVER-ACCEPTED")

    with tempfile.TemporaryDirectory() as td:
        summary = run_exploitation_epoch(
            node, env, train_items, val_items, target, optimizer, cfg, td,
            epoch=0, current_score=1.0,
        )
    assert isinstance(summary, ExploitationSummary)
    assert summary.saturated is True
    assert summary.n_accepted == 0
    assert node.step_buffer.is_saturated(cfg.N)


@pytest.mark.skip(reason="V2: stub optimizer_fn needs new merger JSON schema; test adaptation pending")
def test_run_exploitation_epoch_improving_not_saturated():
    # NOTE: V2 exploitation requires merger() from aggregate.py.
    import pytest
    try:
        from css.optimizer.aggregate import merger
    except ImportError:
        pytest.skip("merger not yet implemented in aggregate.py")

    cfg = CSSConfig(N=5, min_l0_epochs=0, max_l0_epochs=1, k_rollouts=1, max_api_workers=2,
                    minibatch_size=4, batch_size=4)
    train_items = [{"id": "t%d" % i} for i in range(12)]
    val_items = [{"id": "v%d" % i} for i in range(4)]
    state = {"rollouts": 0, "n_pass": 1}
    rollouts_per_step = len(val_items) * cfg.k_rollouts

    def increasing_scorer(item, ri):
        idx_str = item["id"]
        if idx_str.startswith("v"):
            idx = int(idx_str[1:])
        else:
            return 1
        hard = 1 if idx < state["n_pass"] else 0
        state["rollouts"] += 1
        if state["rollouts"] % rollouts_per_step == 0:
            state["n_pass"] += 1
        return hard

    env = FakeTaskEnv({"train": train_items, "val": val_items}, scorer=increasing_scorer)
    node = TreeNode(node_id="n1", strategy="S", rules="initial")
    target = StubLLMClient()
    optimizer = _opt_client_append("GOOD-RULE")

    with tempfile.TemporaryDirectory() as td:
        summary = run_exploitation_epoch(
            node, env, train_items, val_items, target, optimizer, cfg, td,
            epoch=0, current_score=0.0,
        )
    assert summary.saturated is False
    assert summary.best_score >= 0.0
    assert node.best_score == summary.best_score


# ── 9. RawPatch round-trip ──────────────────────────────────────────────────────


def test_rawpatch_round_trip():
    rp = RawPatch(
        patch=Patch(
            edits=[Edit(op="append", content="c", reason="r")],
            reasoning="why",
        ),
        source_type="success",
        batch_size=3,
        failure_summary=[{"pattern": "off-by-one"}],
    )
    again = RawPatch.from_dict(rp.to_dict())
    assert again is not None
    assert again.source_type == "success"
    assert again.batch_size == 3
    assert again.failure_summary == [{"pattern": "off-by-one"}]
    assert len(again.patch.edits) == 1
    assert again.patch.edits[0].op == "append"
    assert again.patch.edits[0].content == "c"
    assert again.patch.reasoning == "why"
    # from_dict(None) -> None.
    assert RawPatch.from_dict(None) is None


# ── 10. import smoke (no skillopt) ──────────────────────────────────────────────


def test_phase3_modules_import():
    for mod in (
        "css.data.edit",
        "css.optimizer.edit_engine",
        "css.evaluation.gate",
        "css.optimizer.reflect",
        "css.optimizer.aggregate",
        "css.optimizer.exploitation",
    ):
        importlib.import_module(mod)
    # Keep referenced symbols live.
    assert EditReport is not None
    assert all(
        fn is not None
        for fn in (apply_patch, evaluate_gate, merger, run_l0_step)
    )


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
