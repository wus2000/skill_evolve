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
from css.optimizer.aggregate import aggregate_patches, select_top_edits
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
from css.optimizer.reflect import (
    build_l0_prompt,
    run_minibatch_analyst,
    split_minibatches,
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


# ── 3. reflect.split_minibatches ───────────────────────────────────────────────


def test_split_minibatches_separates_and_chunks():
    results = (
        [_result("f", i, 0) for i in range(5)]
        + [_result("s", i, 1) for i in range(3)]
    )
    fail_batches, succ_batches = split_minibatches(results, minibatch_size=2)

    # 5 failures -> chunks of 2 -> [2, 2, 1]; 3 successes -> [2, 1].
    assert [len(b) for b in fail_batches] == [2, 2, 1]
    assert [len(b) for b in succ_batches] == [2, 1]
    # Class purity.
    assert all(not r.passed for b in fail_batches for r in b)
    assert all(r.passed for b in succ_batches for r in b)


# ── 4. reflect.build_l0_prompt ─────────────────────────────────────────────────


def test_build_l0_prompt_contains_context_and_truncates():
    strategy = "STRATEGY-MARKER decompose the problem first"
    rules = "RULES-MARKER always read the sheet name"
    big = "Z" * 20000
    minibatch = [
        _result(
            "t1",
            0,
            0,
            messages=[
                {"role": "user", "content": "short user text VERBATIM-SHORT"},
                {"role": "tool", "content": big},
            ],
        )
    ]
    # Seed the step buffer with a rejected edit so it must appear in the prompt.
    sb = StepBuffer()
    sb.append(
        StepBufferEntry(
            step=0,
            action="reject",
            score_before=0.5,
            score_after=0.4,
            rejected_edits=[
                Edit(op="append", content="REJECTED-EDIT-CONTENT", reason="bad")
            ],
            failure_patterns=["FAILPATTERN-OFF-BY-ONE"],
        )
    )

    system, user = build_l0_prompt(
        strategy, rules, minibatch, sb, source_type="failure", tool_trunc=8000
    )

    # strategy injected and marked read-only.
    assert "STRATEGY-MARKER" in user
    assert "READ-ONLY" in user
    # rules injected as the edit target.
    assert "RULES-MARKER" in user
    # formatted trajectory text present.
    assert "VERBATIM-SHORT" in user
    # rejected-edit content injected so repeats are discouraged.
    assert "REJECTED-EDIT-CONTENT" in user
    assert "FAILPATTERN-OFF-BY-ONE" in user
    # System role tells the optimizer it edits rules.md only.
    assert "rules.md" in system

    # The 20000-char tool message is truncated in the prompt; short text verbatim.
    assert big not in user
    assert "...[truncated" in user
    # Short content (well under tool_trunc) is preserved exactly.
    assert "short user text VERBATIM-SHORT" in user


# ── 5. reflect.run_minibatch_analyst ───────────────────────────────────────────


def test_run_minibatch_analyst_parses_edit_list():
    cfg = CSSConfig()
    client = StubLLMClient(optimizer_fn=_edit_list_fn('[{"op":"append","content":"new rule"}]'))
    minibatch = [_result("t1", 0, 0)]
    rp = run_minibatch_analyst(
        client, "strat", "rules", minibatch, StepBuffer(),
        source_type="failure", cfg=cfg,
    )
    assert isinstance(rp, RawPatch)
    assert rp.source_type == "failure"
    assert rp.batch_size == 1
    assert len(rp.patch.edits) == 1
    e = rp.patch.edits[0]
    assert e.op == "append"
    assert e.content == "new rule"
    assert e.source_type == "failure"


def test_run_minibatch_analyst_parses_fenced_edit_list():
    cfg = CSSConfig()
    fenced = "```json\n[{\"op\":\"append\",\"content\":\"fenced rule\"}]\n```"
    client = StubLLMClient(optimizer_fn=_edit_list_fn(fenced))
    rp = run_minibatch_analyst(
        client, "strat", "rules", [_result("t1", 0, 1)], StepBuffer(),
        source_type="success", cfg=cfg,
    )
    assert len(rp.patch.edits) == 1
    assert rp.patch.edits[0].content == "fenced rule"
    assert rp.patch.edits[0].source_type == "success"


# ── 6. aggregate ──────────────────────────────────────────────────────────────


def test_aggregate_patches_support_count_and_select_top():
    # Two raw patches independently propose the SAME append (+ differing whitespace).
    rp1 = RawPatch(
        patch=Patch(edits=[Edit(op="append", content="Do verify the sheet")]),
        source_type="failure",
    )
    rp2 = RawPatch(
        patch=Patch(edits=[Edit(op="append", content="do   verify the   sheet")]),
        source_type="failure",
    )
    # A third, distinct, success-driven edit with support 1.
    rp3 = RawPatch(
        patch=Patch(edits=[Edit(op="append", content="Only-once rule")]),
        source_type="success",
    )

    merged = aggregate_patches([rp1, rp2, rp3])
    # The first two collapse into one edit with support_count == 2.
    by_content = {_norm(e.content): e for e in merged.edits}
    shared = by_content["do verify the sheet"]
    assert shared.support_count == 2
    assert shared.source_type == "failure"
    once = by_content["only-once rule"]
    assert once.support_count == 1

    # select_top_edits: cap and order by support desc (then failure-first).
    top = select_top_edits(merged, max_edits=1)
    assert len(top.edits) == 1
    assert top.edits[0].support_count == 2  # the high-support edit wins the cap

    # No cap shrink: support ordering puts the 2-support edit first.
    ordered = select_top_edits(merged, max_edits=10)
    assert ordered.edits[0].support_count == 2


def _norm(text: str) -> str:
    import re

    return re.sub(r"\s+", " ", text).strip().lower()


# ── 7. exploitation.run_l0_step ────────────────────────────────────────────────


def _opt_client_append(content: str) -> StubLLMClient:
    import json

    return StubLLMClient(
        optimizer_fn=_edit_list_fn(json.dumps([{"op": "append", "content": content}]))
    )


def test_run_l0_step_accept_mutates_node():
    cfg = CSSConfig(k_rollouts=1, max_api_workers=2, minibatch_size=4)
    val_items = [{"id": "v1"}]
    # Scorer makes every candidate score 1.0 (task_hard 1.0) > current 0.0 -> accept.
    env = FakeTaskEnv({"val": val_items}, scorer=lambda item, ri: 1)
    node = TreeNode(node_id="n1", strategy="S", rules="initial rules")
    epoch_results = [_result("t1", 0, 0), _result("t2", 0, 0)]
    target = StubLLMClient()
    optimizer = _opt_client_append("ACCEPTED-RULE")

    with tempfile.TemporaryDirectory() as td:
        step_result, cur, best, best_step, best_rules = run_l0_step(
            node, env, val_items, epoch_results, target, optimizer, cfg, td,
            step_index=0, epoch=0, current_score=0.0, best_score=0.0, best_step=-1,
            best_rules=node.rules,
        )

    assert isinstance(step_result, L0StepResult)
    assert step_result.accepted is True
    assert step_result.action in ("accept", "accept_new_best")
    assert cur == 1.0
    # Candidate became node.rules; the appended rule is present.
    assert "ACCEPTED-RULE" in node.rules
    # Exactly one buffer entry appended this step.
    assert node.step_buffer.n_steps == 1
    entry = node.step_buffer.entries[0]
    assert entry.accepted is True
    assert entry.applied_edits and not entry.rejected_edits
    # New best registered, and the best rules body is threaded back.
    assert step_result.action == "accept_new_best"
    assert best == 1.0 and best_step == 0
    assert "ACCEPTED-RULE" in best_rules


def test_run_l0_step_reject_keeps_node():
    cfg = CSSConfig(k_rollouts=1, max_api_workers=2, minibatch_size=4)
    val_items = [{"id": "v1"}]
    # Candidate scores 0.0; current is 1.0 -> reject.
    env = FakeTaskEnv({"val": val_items}, scorer=lambda item, ri: 0)
    node = TreeNode(node_id="n1", strategy="S", rules="initial rules")
    before = node.rules
    epoch_results = [_result("t1", 0, 0)]
    target = StubLLMClient()
    optimizer = _opt_client_append("REJECTED-RULE")

    with tempfile.TemporaryDirectory() as td:
        step_result, cur, best, best_step, best_rules = run_l0_step(
            node, env, val_items, epoch_results, target, optimizer, cfg, td,
            step_index=0, epoch=0, current_score=1.0, best_score=1.0, best_step=0,
            best_rules=before,
        )

    assert step_result.accepted is False
    assert step_result.action == "reject"
    assert cur == 1.0  # current carried unchanged
    assert node.rules == before  # node NOT mutated
    assert best_rules == before  # best body carried unchanged on reject
    assert node.step_buffer.n_steps == 1
    entry = node.step_buffer.entries[0]
    assert entry.accepted is False
    # Rejected edits recorded so the next prompt avoids them.
    assert entry.rejected_edits and not entry.applied_edits
    assert any("REJECTED-RULE" in (e.content or "") for e in entry.rejected_edits)


def test_run_l0_step_accept_not_best_preserves_best_rules():
    # Guards the Phase-4 consistency bug: a candidate that beats the current
    # incumbent (accept) but NOT the best score must advance node.rules while
    # leaving the returned best_rules/best_score pointing at the prior best body.
    cfg = CSSConfig(k_rollouts=1, max_api_workers=2, minibatch_size=4)
    val_items = [{"id": "v1"}, {"id": "v2"}, {"id": "v3"}, {"id": "v4"}]
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
            node, env, val_items, epoch_results, target, optimizer, cfg, td,
            step_index=3, epoch=0, current_score=0.25, best_score=0.75, best_step=1,
            best_rules="PRIOR-BEST",
        )

    assert step_result.action == "accept"       # improved current, not the best
    assert "MID-RULE" in node.rules              # node.rules advanced off the best
    assert cur == 0.5
    assert best == 0.75 and best_step == 1       # best score/step unchanged
    assert best_rules == "PRIOR-BEST"            # best BODY preserved (the bug guard)


def test_failure_patterns_use_edit_reasons_not_bookkeeping():
    # _mine_failure_patterns must surface genuine per-edit failure rationale and
    # never inject the aggregate bookkeeping string ("merged N raw patches ...").
    import json
    cfg = CSSConfig(k_rollouts=1, max_api_workers=2, minibatch_size=4)
    val_items = [{"id": "v1"}]
    env = FakeTaskEnv({"val": val_items}, scorer=lambda item, ri: 1)
    node = TreeNode(node_id="n1", strategy="S", rules="r")
    epoch_results = [_result("t1", 0, 0), _result("t2", 0, 0)]  # failures
    target = StubLLMClient()
    optimizer = StubLLMClient(optimizer_fn=_edit_list_fn(json.dumps(
        [{"op": "append", "content": "verify outputs", "reason": "agent skipped verification"}]
    )))

    with tempfile.TemporaryDirectory() as td:
        run_l0_step(
            node, env, val_items, epoch_results, target, optimizer, cfg, td,
            step_index=0, epoch=0, current_score=0.0, best_score=0.0, best_step=-1,
            best_rules=node.rules,
        )

    fps = node.step_buffer.entries[0].failure_patterns
    assert "agent skipped verification" in fps
    assert not any("merged" in p and "raw patch" in p for p in fps)


# ── 8. exploitation.run_exploitation_epoch ─────────────────────────────────────


def test_run_exploitation_epoch_saturates_on_consecutive_rejects():
    cfg = CSSConfig(N=5, max_l0_steps_per_epoch=20, k_rollouts=1, max_api_workers=2,
                    minibatch_size=4, batch_size=4)
    train_items = [{"id": f"t{i}"} for i in range(20)]
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
    # Stops at exactly N consecutive rejects (well under the step cap).
    assert summary.n_steps == cfg.N
    assert summary.n_accepted == 0
    assert node.rules == before  # never mutated
    assert node.step_buffer.is_saturated(cfg.N)


def test_run_exploitation_epoch_improving_not_saturated():
    cfg = CSSConfig(N=5, max_l0_steps_per_epoch=3, k_rollouts=1, max_api_workers=2,
                    minibatch_size=4, batch_size=4)
    train_items = [{"id": f"t{i}"} for i in range(12)]
    # task_hard is binary per task, so to get a strictly increasing candidate
    # score across steps we use 4 selection tasks and let one MORE of them pass
    # on each step (step 0: 1/4=0.25, step 1: 2/4=0.5, step 2: 3/4=0.75). Each
    # step strictly beats the incumbent -> every step accepts; never saturates.
    val_items = [{"id": f"v{i}"} for i in range(4)]
    state = {"rollouts": 0, "n_pass": 1}
    rollouts_per_step = len(val_items) * cfg.k_rollouts

    def increasing_scorer(item, ri):
        idx_str = item["id"]
        if idx_str.startswith("v"):
            idx = int(idx_str[1:])
        else:
            return 1
        # Pass the first ``n_pass`` tasks this step.
        hard = 1 if idx < state["n_pass"] else 0
        state["rollouts"] += 1
        if state["rollouts"] % rollouts_per_step == 0:
            state["n_pass"] += 1  # let one more task pass next step
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
    # Hits the per-epoch cap, not saturation; accepts every step.
    assert summary.saturated is False
    assert summary.n_steps == cfg.max_l0_steps_per_epoch
    assert summary.n_accepted == cfg.max_l0_steps_per_epoch
    assert summary.best_score > 0.0
    assert node.best_score == summary.best_score
    assert "GOOD-RULE" in node.rules


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
        for fn in (apply_patch, evaluate_gate, aggregate_patches, run_l0_step)
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
