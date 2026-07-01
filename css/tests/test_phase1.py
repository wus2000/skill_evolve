"""Phase 1 smoke tests: config, core data structures, skill document I/O.

Runnable two ways:
    python -m css.tests.test_phase1      # standalone, prints PASS/FAIL summary
    pytest css/tests/test_phase1.py      # standard collection

The tests assert behavior of the deterministic, code-enforced pieces of Phase 1
(saturation detection, L1-signal predicate, REFINE diff gate, JSON round-trips),
since those are exactly the parts the design says must NOT depend on an LLM.
"""
from __future__ import annotations

import tempfile

from css.config import CSSConfig
from css.data import (
    Edit,
    LearningCurvePoint,
    NegativeArchive,
    NegativeArchiveEntry,
    Observation,
    OccurrencePoint,
    Patch,
    PatternLibrary,
    PatternRecord,
    SearchTree,
    StepBuffer,
    StepBufferEntry,
    TaskResult,
    TreeNode,
    aggregate_scores,
    group_rollouts,
)
from css.data.rollout import TaskRolloutGroup
from css.markdown_utils import (
    check_refine_diff,
    diff_subsections,
    parse_subsections,
    split_strategy,
)
from css.skill_document import SkillDocument, SkillDocumentMetadata


# ── Config ───────────────────────────────────────────────────────────────────
def test_config_defaults_and_validation():
    cfg = CSSConfig()
    cfg.validate()  # default values must be self-consistent
    assert cfg.N == 5 and cfg.W == 10 and cfg.K == 3
    assert cfg.effective_context_threshold == int(256_000 * 0.80)


def test_config_invalid_raises():
    bad = CSSConfig(W=2, N=5)  # W < N
    raised = False
    try:
        bad.validate()
    except ValueError:
        raised = True
    assert raised, "expected W<N to fail validation"


def test_config_roundtrip_with_extra():
    cfg = CSSConfig(alpha=0.7, extra={})
    cfg.extra["custom_knob"] = 123
    d = cfg.to_dict()
    cfg2 = CSSConfig.from_dict(d)
    assert cfg2.alpha == 0.7
    # unknown top-level keys are absorbed into extra
    d2 = dict(d)
    d2["totally_unknown"] = "x"
    cfg3 = CSSConfig.from_dict(d2)
    assert cfg3.extra.get("totally_unknown") == "x"


def test_config_json_file_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        p = f"{td}/cfg.json"
        CSSConfig(beta=0.9).to_json_file(p)
        cfg = CSSConfig.from_json_file(p)
        assert cfg.beta == 0.9


# ── Edit / Patch ───────────────────────────────────────────────────────────--
def test_edit_patch_roundtrip():
    patch = Patch(
        edits=[
            Edit(op="append", content="rule A", source_type="failure", support_count=3),
            Edit(op="replace", target="old", content="new"),
            Edit(op="delete", target="stale"),
        ],
        reasoning="why",
    )
    d = patch.to_dict()
    patch2 = Patch.from_dict(d)
    assert len(patch2) == 3
    assert patch2.edits[0].support_count == 3
    assert patch2.edits[1].target == "old"
    assert patch2.reasoning == "why"


# ── Rollout grouping & contrastive pairs ─────────────────────────────────────
def test_rollout_grouping_and_contrastive_pairs():
    results = [
        TaskResult(task_id="t1", rollout_index=0, hard=1, soft=1.0),
        TaskResult(task_id="t1", rollout_index=1, hard=0, soft=0.5),
        TaskResult(task_id="t1", rollout_index=2, hard=0, soft=0.0),
        TaskResult(task_id="t2", rollout_index=0, hard=0, soft=0.0),
    ]
    groups = group_rollouts(results)
    assert len(groups) == 2
    g1 = next(g for g in groups if g.task_id == "t1")
    assert len(g1.successes) == 1 and len(g1.failures) == 2
    # 1 success x 2 failures = 2 contrastive pairs
    assert len(g1.contrastive_pairs()) == 2
    g2 = next(g for g in groups if g.task_id == "t2")
    assert g2.is_persistent_fail()
    assert not g1.is_persistent_fail()


def test_aggregate_scores():
    results = [
        TaskResult(task_id="t1", hard=1, soft=1.0),
        TaskResult(task_id="t1", hard=1, soft=1.0),
        TaskResult(task_id="t2", hard=0, soft=0.5),
        TaskResult(task_id="t2", hard=0, soft=0.0),
    ]
    s = aggregate_scores(results)
    assert s["hard"] == 0.5         # 2/4 rollouts all-pass
    assert s["n_tasks"] == 2.0
    assert s["task_hard"] == 0.5    # t1 majority-pass, t2 not


def test_rollout_roundtrip_absorbs_unknown_keys():
    d = {"task_id": "t9", "hard": 1, "soft": 1.0, "weird_field": [1, 2, 3]}
    r = TaskResult.from_dict(d)
    assert r.task_id == "t9" and r.passed
    assert r.extras.get("weird_field") == [1, 2, 3]


def test_rollout_token_usage_and_aliases():
    # Q5: token_usage is first-class; is_correct/score alias passed/soft.
    r = TaskResult(task_id="t1", hard=1, soft=0.8,
                   token_usage={"total_tokens": 1234})
    assert r.is_correct is True and r.score == 0.8
    r2 = TaskResult.from_dict(r.to_dict())
    assert r2.token_usage == {"total_tokens": 1234}
    assert r2.is_correct == r.is_correct and r2.score == r.score


def test_rollout_adapts_skillopt_result_dict():
    # Shape mirrors SkillOpt process_one(): id + conversation (not messages).
    skillopt_like = {
        "id": "1234", "hard": 1, "soft": 1.0, "n_cases": 3, "n_pass": 3,
        "n_turns": 5, "fail_reason": "", "task_type": "cell_level",
        "conversation": [{"role": "user", "content": "do it"},
                         {"role": "assistant", "content": "done"}],
    }
    r = TaskResult.from_dict(skillopt_like)
    assert r.task_id == "1234" and r.passed
    # The trajectory must land in `messages`, not be lost into `extras`.
    assert len(r.messages) == 2 and r.messages[0]["content"] == "do it"
    assert "conversation" not in r.extras


# ── Step buffer: saturation, accept rate, slope ──────────────────────────────
def _mk_step(step, accepted):
    return StepBufferEntry(
        step=step,
        action="accept" if accepted else "reject",
        score_before=0.0,
        score_after=0.1 if accepted else 0.0,
    )


def test_step_buffer_saturation():
    buf = StepBuffer()
    for i in range(3):
        buf.append(_mk_step(i, accepted=True))
    for i in range(3, 8):
        buf.append(_mk_step(i, accepted=False))  # 5 consecutive rejects
    assert buf.consecutive_rejects() == 5
    assert buf.is_saturated(5)
    assert not buf.is_saturated(6)
    # an accept resets the streak
    buf.append(_mk_step(8, accepted=True))
    assert buf.consecutive_rejects() == 0
    assert not buf.is_saturated(5)


def test_step_buffer_accept_rate_and_slope():
    buf = StepBuffer()
    # accepts early, rejects late → negative slope, but accept_rate still 0.5
    for i in range(5):
        buf.append(_mk_step(i, accepted=True))
    for i in range(5, 10):
        buf.append(_mk_step(i, accepted=False))
    assert abs(buf.accept_rate() - 0.5) < 1e-9
    assert buf.accept_slope(10) < 0     # declining learning
    # all-accept window → positive-or-zero slope, rate 1.0
    buf2 = StepBuffer()
    for i in range(4):
        buf2.append(_mk_step(i, accepted=True))
    assert buf2.accept_rate() == 1.0
    assert abs(buf2.accept_slope(4)) < 1e-9  # constant 1s → zero slope


def test_step_buffer_roundtrip():
    buf = StepBuffer()
    buf.append(StepBufferEntry(
        step=0, action="reject", score_before=0.3, score_after=0.3,
        rejected_edits=[Edit(op="append", content="bad rule")],
        failure_patterns=["over-eager execution"],
    ))
    buf2 = StepBuffer.from_dict(buf.to_dict())
    assert buf2.entries[0].rejected_edits[0].content == "bad rule"
    assert buf2.recent_failure_patterns(1) == ["over-eager execution"]


# ── Pattern library: L1 signal predicate ─────────────────────────────────────
def test_pattern_l1_signal_predicate():
    lib = PatternLibrary()
    pid = lib.new_pattern_id()
    # A failure pattern that persists (flat occurrence) and resisted remedies.
    persistent = PatternRecord(
        pattern_id=pid, name="premature commitment", polarity="failure",
        occurrence_history=[
            OccurrencePoint(epoch=0, occurrence_rate=0.4, support_count=10),
            OccurrencePoint(epoch=1, occurrence_rate=0.42, support_count=11),
            OccurrencePoint(epoch=2, occurrence_rate=0.41, support_count=10),
        ],
        remedy_resistance=3,
    )
    lib.add(persistent)
    sigs = lib.l1_signals(
        trend_window=3, l0_saturated=True, min_task_fraction=0.15,
    )
    assert len(sigs) == 1 and sigs[0].pattern_id == pid

    # A decaying pattern is NOT an L1 signal (L0 is fixing it).
    pid2 = lib.new_pattern_id()
    decaying = PatternRecord(
        pattern_id=pid2, name="missing verification", polarity="failure",
        occurrence_history=[
            OccurrencePoint(epoch=0, occurrence_rate=0.5, support_count=12),
            OccurrencePoint(epoch=1, occurrence_rate=0.3, support_count=7),
            OccurrencePoint(epoch=2, occurrence_rate=0.1, support_count=2),
        ],
        remedy_resistance=3,
    )
    lib.add(decaying)
    assert decaying.occurrence_trend(3) < 0
    sigs2 = lib.l1_signals(
        trend_window=3, l0_saturated=True, min_task_fraction=0.15,
    )
    assert all(s.pattern_id != pid2 for s in sigs2)

    # If L0 is NOT saturated, even a persistent pattern is not yet an L1 signal.
    sigs3 = lib.l1_signals(
        trend_window=3, l0_saturated=False, min_task_fraction=0.15,
    )
    assert sigs3 == []


def test_pattern_l1_signal_saturation_is_the_remedy_evidence():
    """Authoritative doc: L0 saturation IS the remedy_resistance evidence.

    A persistent, saturated, widespread pattern must qualify even with
    remedy_resistance == 0 under the default (require_remedy_count=False).
    Flipping require_remedy_count enforces the stricter v6 D4 counter gate.
    """
    lib = PatternLibrary()
    pid = lib.new_pattern_id()
    rec = PatternRecord(
        pattern_id=pid, name="anchoring", polarity="failure", remedy_resistance=0,
        occurrence_history=[
            OccurrencePoint(epoch=0, occurrence_rate=0.3, support_count=8),
            OccurrencePoint(epoch=1, occurrence_rate=0.31, support_count=8),
        ],
    )
    lib.add(rec)
    # Default: fires on saturation alone.
    assert lib.is_l1_signal(rec, trend_window=2, l0_saturated=True, min_task_fraction=0.15)
    # Stricter v6 D4 reading: needs remedy_resistance >= threshold.
    assert not lib.is_l1_signal(
        rec, trend_window=2, l0_saturated=True, min_task_fraction=0.15,
        remedy_threshold=3, require_remedy_count=True,
    )


def test_pattern_library_roundtrip():
    lib = PatternLibrary()
    pid = lib.new_pattern_id()
    rec = PatternRecord(
        pattern_id=pid, name="x", polarity="success",
        observations=[Observation(
            obs_id="o0", task_id="t1", rollout_index=0, node_id="n0", epoch=0,
            what="checked output", cognitive_aspect="verification habit",
            evidence="...", consequence="caught error", significance="critical",
            polarity="success",
        )],
    )
    lib.add(rec)
    lib2 = PatternLibrary.from_dict(lib.to_dict())
    assert lib2.get(pid).observations[0].cognitive_aspect == "verification habit"
    assert lib2._next_id == lib._next_id


# ── Negative archive recall ───────────────────────────────────────────────────
def test_negative_archive_recall():
    arch = NegativeArchive()
    e1 = NegativeArchiveEntry(
        entry_id=arch.new_entry_id(), strategy_snapshot="A", origin="pruned_node",
        embedding=[1.0, 0.0],
    )
    e2 = NegativeArchiveEntry(
        entry_id=arch.new_entry_id(), strategy_snapshot="B",
        origin="proposal_failed_rollout", embedding=[0.0, 1.0],
    )
    arch.add(e1)
    arch.add(e2)

    def dot(a, b):
        return sum(x * y for x, y in zip(a, b))

    top = arch.recall([0.9, 0.1], top_k=1, similarity_fn=dot)
    assert len(top) == 1 and top[0][0].strategy_snapshot == "A"
    arch2 = NegativeArchive.from_dict(arch.to_dict(include_embeddings=True))
    assert len(arch2) == 2 and arch2._next_id == 2


# ── Search tree topology ──────────────────────────────────────────────────────
def test_search_tree_topology_and_roundtrip():
    tree = SearchTree()
    root = TreeNode(node_id=tree.new_node_id(), strategy="root strat")
    tree.add_root(root)
    c1 = TreeNode(node_id=tree.new_node_id(), branch_type="REFINE")
    c2 = TreeNode(node_id=tree.new_node_id(), branch_type="PROPOSAL")
    tree.add_child(root.node_id, c1)
    tree.add_child(root.node_id, c2)

    assert tree.root_id == root.node_id
    assert {n.node_id for n in tree.children(root.node_id)} == {c1.node_id, c2.node_id}
    assert [n.node_id for n in tree.siblings(c1.node_id)] == [c2.node_id]
    path = tree.path_to_root(c1.node_id)
    assert [n.node_id for n in path] == [c1.node_id, root.node_id]

    c1.val_score = 0.8
    root.val_score = 0.5
    assert tree.best_node("val_score").node_id == c1.node_id

    tree2 = SearchTree.from_dict(tree.to_dict())
    assert len(tree2) == 3 and tree2.root_id == root.node_id
    assert tree2._next_id == tree._next_id


def test_tree_node_uses_step_buffer_saturation():
    node = TreeNode(node_id="n0")
    for i in range(5):
        node.step_buffer.append(_mk_step(i, accepted=False))
    assert node.is_saturated(5)
    assert node.n_steps == 5


# ── Markdown subsection parsing & REFINE diff gate ────────────────────────────
STRATEGY_A = """## Hypothesis-Driven Execution

## Strategy Body

### 1. Task Understanding
Spend time analyzing structure before acting.
Probe with the simplest input first.

### 2. Execution as Hypotheses
Each step is a verifiable hypothesis.

### 3. Error Recovery
On failure, re-examine assumptions.
"""


def test_parse_subsections():
    subs = parse_subsections(STRATEGY_A)
    assert len(subs) == 3
    assert subs[0].heading == "1. Task Understanding"
    assert "simplest input" in subs[0].body
    name, preamble, subs2 = split_strategy(STRATEGY_A)
    assert name == "Hypothesis-Driven Execution"
    assert "Strategy Body" in preamble
    assert len(subs2) == 3


def test_refine_diff_gate_accepts_local_change():
    # Change only subsection 3's body.
    child = STRATEGY_A.replace(
        "On failure, re-examine assumptions.",
        "On failure, re-examine assumptions AND log the divergence point.",
    )
    ok, reason = check_refine_diff(STRATEGY_A, child, max_changed=2)
    assert ok, reason
    diff = diff_subsections(STRATEGY_A, child)
    assert diff.n_changed == 1 and diff.unchanged_match


def test_refine_diff_gate_rejects_empty_change():
    ok, reason = check_refine_diff(STRATEGY_A, STRATEGY_A, max_changed=2)
    assert not ok and reason == "no_subsection_changed:escalate_to_proposal"


def test_refine_diff_gate_rejects_too_many_changes():
    child = STRATEGY_A
    child = child.replace("Spend time analyzing structure before acting.", "X1")
    child = child.replace("Each step is a verifiable hypothesis.", "X2")
    child = child.replace("On failure, re-examine assumptions.", "X3")
    ok, reason = check_refine_diff(child_text=child, parent_text=STRATEGY_A, max_changed=2)
    assert not ok and reason.startswith("too_many_changed")


def test_refine_diff_gate_rejects_removed_subsection():
    # Drop subsection 3 entirely → structural change → escalate to PROPOSAL.
    child = STRATEGY_A.split("### 3. Error Recovery")[0]
    ok, reason = check_refine_diff(STRATEGY_A, child, max_changed=2)
    assert not ok
    assert reason.startswith("structure_changed") and "escalate_to_proposal" in reason
    assert "removed=3. error recovery" in reason


def test_refine_diff_gate_escalates_on_added_subsection():
    # Add a brand-new subsection → structural change → escalate to PROPOSAL.
    child = STRATEGY_A + "\n### 4. New Dimension\nSomething new.\n"
    ok, reason = check_refine_diff(STRATEGY_A, child, max_changed=2)
    assert not ok
    assert reason.startswith("structure_changed") and "escalate_to_proposal" in reason


def test_refine_diff_gate_no_subsections_at_all():
    # A strategy with no headings at all cannot be REFINEd.
    flat = "Just prose, no subsections at all.\n"
    ok, reason = check_refine_diff(flat, flat + "more prose\n", max_changed=2)
    assert not ok and reason == "no_subsection_changed:escalate_to_proposal"


def test_refine_diff_gate_flat_h2_strategy():
    # A D6-style strategy with flat ## headings IS now refinable.
    parent = "## Section One\nContent one.\n\n## Section Two\nContent two.\n"
    child = parent.replace("Content one.", "Refined content one.")
    ok, reason = check_refine_diff(parent, child, max_changed=2)
    assert ok and reason == "ok:1_changed"


def test_refine_diff_duplicate_headings_aligned_by_ordinal():
    # Two subsections share the heading "Step". Editing the FIRST one's body
    # must be detected (a naive {key: sub} map would keep only the last and
    # report "no change").
    parent = (
        "## S\n\n"
        "### Step\nbody-zero\n"
        "### Step\nbody-one\n"
    )
    child = parent.replace("body-zero", "body-zero-edited")
    diff = diff_subsections(parent, child)
    assert diff.n_changed == 1 and diff.unchanged_match
    ok, reason = check_refine_diff(parent, child, max_changed=2)
    assert ok, reason


def test_parse_subsections_ignores_4backtick_fence():
    # A 4-backtick fence (used to display a literal triple-backtick) must still
    # be recognized so inner ### lines do not leak as headings.
    strat = (
        "## S\n\n"
        "### 1. Real\n"
        "Example showing a fence:\n"
        "````\n"
        "```\n"
        "### not a heading\n"
        "```\n"
        "````\n"
        "### 2. Also Real\n"
        "body\n"
    )
    subs = parse_subsections(strat)
    assert [s.heading for s in subs] == ["1. Real", "2. Also Real"]
    assert "not a heading" in subs[0].body


def test_parse_subsections_ignores_code_fences():
    # A ### line inside a fenced code block is code, not a heading.
    strat = (
        "## S\n\n"
        "### 1. Real Subsection\n"
        "Body with an example:\n"
        "```python\n"
        "### this is a comment, not a heading\n"
        "x = 1\n"
        "```\n"
        "### 2. Second Real Subsection\n"
        "More body.\n"
    )
    subs = parse_subsections(strat)
    assert [s.heading for s in subs] == ["1. Real Subsection", "2. Second Real Subsection"]
    # The fenced ### must live inside subsection 1's body, not split it.
    assert "this is a comment" in subs[0].body


# ── Skill document I/O ────────────────────────────────────────────────────────
def test_skill_document_roundtrip_and_views():
    with tempfile.TemporaryDirectory() as td:
        skill_dir = f"{td}/n0"
        meta = SkillDocumentMetadata(node_id="n0")
        meta.learning_curve.append(LearningCurvePoint(epoch=0, n_steps=3, val_score=0.4))
        doc = SkillDocument(
            skill_dir=skill_dir, strategy=STRATEGY_A,
            rules="- Always set INPUT_PATH/OUTPUT_PATH at top.\n", metadata=meta,
        )
        doc.save()

        loaded = SkillDocument.load(skill_dir)
        assert loaded.strategy == STRATEGY_A
        assert "INPUT_PATH" in loaded.rules
        assert loaded.metadata.node_id == "n0"
        assert loaded.metadata.learning_curve[0].val_score == 0.4
        assert len(loaded.subsections()) == 3

        combined = loaded.combined_skill_text()
        assert "# Task-Solving Approach" in combined and "# Tactical Rules" in combined

        # REFINE gate via the document method.
        child = STRATEGY_A.replace(
            "Each step is a verifiable hypothesis.",
            "Each step is a verifiable hypothesis; assert the expected cell value.",
        )
        ok, reason = loaded.refine_diff_ok(child)
        assert ok, reason


def test_skill_document_empty_rules_clean_combined():
    with tempfile.TemporaryDirectory() as td:
        doc = SkillDocument(skill_dir=f"{td}/n1", strategy="## S\n\n### 1. X\nbody\n", rules="")
        doc.save()
        combined = SkillDocument.load(doc.skill_dir).combined_skill_text()
        assert "# Tactical Rules" not in combined  # empty rules omitted
        assert "# Task-Solving Approach" in combined


# ── Reflect mode dispatch tests ──────────────────────────────────────────────

def _make_results_mixed():
    """Create rollouts covering all three categories: pure_pass, pure_fail, mixed."""
    results = []
    # Task A: pure pass (2 rollouts, all pass)
    results.append(TaskResult(task_id="A", rollout_index=0, hard=1, soft=1.0,
                              n_cases=1, n_pass=1, messages=[{"role": "user", "content": "do A"}],
                              task_description="task A"))
    results.append(TaskResult(task_id="A", rollout_index=1, hard=1, soft=1.0,
                              n_cases=1, n_pass=1, messages=[{"role": "user", "content": "do A"}],
                              task_description="task A"))
    # Task B: pure fail (2 rollouts, all fail)
    results.append(TaskResult(task_id="B", rollout_index=0, hard=0, soft=0.0,
                              n_cases=1, n_pass=0, fail_reason="wrong",
                              messages=[{"role": "user", "content": "do B"}],
                              task_description="task B"))
    results.append(TaskResult(task_id="B", rollout_index=1, hard=0, soft=0.0,
                              n_cases=1, n_pass=0, fail_reason="wrong",
                              messages=[{"role": "user", "content": "do B"}],
                              task_description="task B"))
    # Task C: mixed (1 pass, 1 fail)
    results.append(TaskResult(task_id="C", rollout_index=0, hard=1, soft=1.0,
                              n_cases=1, n_pass=1, messages=[{"role": "user", "content": "do C"}],
                              task_description="task C"))
    results.append(TaskResult(task_id="C", rollout_index=1, hard=0, soft=0.0,
                              n_cases=1, n_pass=0, fail_reason="partial",
                              messages=[{"role": "user", "content": "do C"}],
                              task_description="task C"))
    return results


def test_reflect_mode_dispatch_legacy():
    from css.model.client import StubLLMClient
    from css.optimizer.reflect import reflect_epoch

    calls = []
    def _track(system, user):
        calls.append(system[:40])
        return '[{"op": "append", "content": "test rule", "reason": "test"}]'

    client = StubLLMClient(optimizer_fn=_track)
    cfg = CSSConfig(reflect_mode="legacy", minibatch_size=10)
    results = _make_results_mixed()
    sb = StepBuffer()
    patches = reflect_epoch(client, "# Strategy", "# Rules", results, sb, cfg=cfg)
    assert len(patches) >= 1
    assert all(p.patch is not None for p in patches)


def test_reflect_mode_dispatch_plan_a():
    from css.model.client import StubLLMClient
    from css.optimizer.reflect import reflect_epoch

    call_systems = []
    def _track(system, user):
        call_systems.append(system[:80])
        # plan_a v2: every unit is a per-minibatch *proposer* that directly emits
        # an edit list. Return one minimal edit so each minibatch yields a patch.
        return '{"diagnosis": "d", "edits": [{"op": "add_section", "content": "### T\\nplan_a rule", "rationale": "x"}]}'

    client = StubLLMClient(optimizer_fn=_track)
    cfg = CSSConfig(reflect_mode="plan_a", minibatch_size=10)
    results = _make_results_mixed()
    sb = StepBuffer()
    patches = reflect_epoch(client, "# Strategy", "# Rules", results, sb, cfg=cfg)
    assert len(patches) >= 1  # plan_a v2 returns one patch per minibatch / contrastive unit
    # Per-minibatch source types (no more single "synthesized" generator stage).
    assert all(p.source_type in ("failure", "success", "contrastive") for p in patches)
    # Each proposer call was made (at least one unit produced an edit).
    assert len(call_systems) >= 1
    assert any(p.patch.edits for p in patches)


def test_reflect_mode_dispatch_plan_b():
    from css.model.client import StubLLMClient
    from css.optimizer.reflect import reflect_epoch

    call_systems = []
    def _track(system, user):
        call_systems.append(system[:80])
        if "success analyst" in system.lower():
            return '{"rule_attributions": [], "success_patterns": [], "robustness_warnings": []}'
        return '[{"op": "append", "content": "plan_b rule", "reason": "context-injected"}]'

    client = StubLLMClient(optimizer_fn=_track)
    cfg = CSSConfig(reflect_mode="plan_b", minibatch_size=10)
    results = _make_results_mixed()
    sb = StepBuffer()
    patches = reflect_epoch(client, "# Strategy", "# Rules", results, sb, cfg=cfg)
    # plan_b: one patch per fail-batch + one per mixed group = 2 patches
    assert len(patches) >= 2
    source_types = {p.source_type for p in patches}
    assert "contrastive" in source_types or "failure" in source_types


def test_triage_groups():
    from css.optimizer.reflect import _group_by_task, _triage_groups
    results = _make_results_mixed()
    groups = _group_by_task(results)
    assert len(groups) == 3
    pure_pass, pure_fail, mixed = _triage_groups(groups)
    assert len(pure_pass) == 1 and pure_pass[0].task_id == "A"
    assert len(pure_fail) == 1 and pure_fail[0].task_id == "B"
    assert len(mixed) == 1 and mixed[0].task_id == "C"


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
