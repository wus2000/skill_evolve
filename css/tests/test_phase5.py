"""Phase 5 smoke tests: PROPOSAL & REFINE (the L1 cognitive-strategy operations).

Runnable two ways:
    python -m css.tests.test_phase5      # standalone, prints PASS/FAIL summary
    pytest css/tests/test_phase5.py      # standard collection

Every test is deterministic and stub-based — no network, no LLM API, no
SpreadsheetBench dataset, no faiss, and no sentence-transformers model download.

  * The optimizer LLM is replaced by :class:`StubLLMClient` whose ``optimizer_fn``
    is a ROUTER (:func:`_router`) that dispatches a canned JSON response per
    Phase-5 prompt, keyed off each system prompt's unambiguous opening sentence
    (the prompts cross-reference each other — e.g. the derive prompt mentions a
    "root-cause analyst" — so substring matching is unsafe; we anchor on
    ``startswith`` of the distinctive first sentence).
  * The negative-archive gate recalls by Jaccard word-overlap on strategy text
    (no embeddings): a candidate whose text equals an archived snapshot scores
    Jaccard 1.0, driving the difference-articulation gate.
  * Layer 5c is a fake ``rollout_validate_fn`` returning a controlled
    ``(occurrence_before, occurrence_after)`` — no rollout / analysis stack.
"""
from __future__ import annotations

import json

from css.config import CSSConfig
from css.data.edit import Edit, Patch
from css.data.negative_archive import NegativeArchive, NegativeArchiveEntry
from css.data.pattern import (
    Observation,
    OccurrencePoint,
    PatternLibrary,
    PatternRecord,
)
from css.data.rollout import TaskResult, TaskRolloutGroup
from css.data.tree import TreeNode
from css.model.client import StubLLMClient
from css.proposal.derivation import (
    StrategyProposal,
    ValidationResult,
    check_negative_archive,
    derive_strategy,
    retrospective_validate,
)
from css.proposal.inheritance import proposal_inherit_rules, refine_apply_cleanup
from css.proposal.proposal import ProposalOutcome, run_proposal
from css.proposal.refine import RefineProposal, derive_refine, is_cleanup_only
from css.proposal.root_cause import RootCause, attribute_root_cause


# ── Canned LLM responses (per Phase-5 prompt) ───────────────────────────────────

_CANNED_ROOT_CAUSE = json.dumps(
    [
        {
            "pattern_ids": ["p0001"],
            "behavioral": "committed to the first column mapping and never re-checked it",
            "process": "treated the first plausible reading as settled and moved to execution",
            "strategy": "the 'Plan then execute' subsection tells it to lock a plan early",
            "assumption": "the first reading of an ambiguous input is usually right",
            "leverage": "",
            "l0_explanation": "a rule cannot force a re-read because the agent never "
            "perceives the input as ambiguous in the first place",
            "evidence": {
                "behavioral": "THOUGHT: 'column B is the date, proceeding'",
                "process": "THOUGHT: 'the mapping is clear, no need to verify'",
                "strategy": "### Plan then execute: lock a plan and follow it",
                "assumption": "first reading is treated as ground truth",
            },
        }
    ]
)

# Two failure patterns tracing back to the SAME strategy-level cause → must MERGE
# into one high-leverage RootCause covering both ids.
_CANNED_ROOT_CAUSE_CROSS = json.dumps(
    [
        {
            "pattern_ids": ["p0001", "p0003"],
            "behavioral": "committed early on two distinct task families",
            "process": "treated the first plausible reading as settled in both",
            "strategy": "the 'Plan then execute' subsection forces an early lock",
            "assumption": "the first reading of an ambiguous input is usually right",
            "leverage": "one strategy change (force a cheap re-read) suppresses both patterns",
            "l0_explanation": "two separate rules each patched one family but neither "
            "addressed the shared premature-commitment process",
            "evidence": {
                "behavioral": "THOUGHT (both): 'proceeding with first reading'",
                "process": "THOUGHT (both): 'no need to verify'",
                "strategy": "### Plan then execute",
                "assumption": "first reading is ground truth",
            },
        }
    ]
)

# A RootCause whose 'process' level has NO evidence → must be dropped by CODE GATE.
_CANNED_ROOT_CAUSE_NO_EVIDENCE = json.dumps(
    [
        {
            "pattern_ids": ["p0001"],
            "behavioral": "committed early",
            "process": "treated first reading as settled",
            "strategy": "the 'Plan then execute' subsection",
            "assumption": "first reading is right",
            "evidence": {
                "behavioral": "THOUGHT: proceeding",
                # no "process" evidence
                "strategy": "### Plan then execute",
                "assumption": "first reading is ground truth",
            },
        }
    ]
)

_CANNED_STRATEGY = json.dumps(
    {
        "strategy_text": (
            "## Cognitive Strategy\n"
            "### Re-read before committing\n"
            "Before locking any interpretation of an ambiguous input, perform one "
            "cheap re-read against the source and confirm the mapping.\n"
            "### Plan then execute\n"
            "Once the interpretation is confirmed, plan and follow it."
        ),
        "rationale": "Breaking the 'first reading is right' assumption forces a cheap "
        "re-read, systematizing the success counterpart's verify-then-commit behavior.",
        "targeted_pattern_ids": ["p0001"],
    }
)

_CANNED_NEG_PROCEED = json.dumps(
    {"proceed": True, "difference": "different trigger: re-read fires on detected ambiguity, "
     "not on every step like the abandoned direction"}
)
_CANNED_NEG_BLOCK = json.dumps({"proceed": False, "difference": ""})

_CANNED_RETRO_HIGH = json.dumps(
    {
        "positive_evidence": ["the passing rollouts already re-read before committing"],
        "counterfactuals": ["task t1 would likely have flipped: it failed on a premature lock"],
        "coverage": 0.5,
    }
)
_CANNED_RETRO_LOW = json.dumps(
    {
        "positive_evidence": [],
        "counterfactuals": ["unclear the change touches these failures"],
        "coverage": 0.1,
    }
)

_CANNED_INHERIT = json.dumps(
    {
        "kept_rules": ["Always validate the final output range before saving."],
        "dropped": [
            {"rule": "Lock the column mapping on first read.",
             "reason": "contradicts the new re-read-before-committing strategy"}
        ],
    }
)

# A REFINE: rewrites exactly ONE ### subsection of the parent; cleanup deletes a
# conflicting rule AND (illegally) tries to append — the append must be dropped.
_PARENT_STRATEGY = (
    "## Cognitive Strategy\n"
    "### Planning\n"
    "Lock the interpretation early and follow it.\n"
    "### Execution\n"
    "Run the locked plan to completion."
)
_PARENT_RULES = (
    "Lock the column mapping on first read.\n"
    "Always validate the final output range before saving."
)
_REFINE_CHILD_OK = (
    "## Cognitive Strategy\n"
    "### Planning\n"
    "Re-read the input once to confirm the interpretation, then lock it and follow it.\n"
    "### Execution\n"
    "Run the locked plan to completion."
)
_CANNED_REFINE_OK = json.dumps(
    {
        "strategy_text": _REFINE_CHILD_OK,
        "changed_subsections": ["Planning"],
        "rationale": "Breaking the early-lock assumption: a cheap re-read before locking.",
        "rules_cleanup": {
            "reasoning": "the first-read lock rule now contradicts the re-read step",
            "edits": [
                {"op": "delete", "target": "Lock the column mapping on first read."},
                {"op": "append", "content": "ADDED RULE THAT MUST BE DROPPED"},
            ],
        },
        "escalate": "",
    }
)

# A REFINE that touches THREE subsections → over-broad, must be gated out.
_PARENT_STRATEGY_3 = "## S\n### A\nalpha\n### B\nbeta\n### C\ngamma"
_REFINE_CHILD_3 = "## S\n### A\nAAA\n### B\nBBB\n### C\nCCC"
_CANNED_REFINE_TOO_MANY = json.dumps(
    {
        "strategy_text": _REFINE_CHILD_3,
        "changed_subsections": ["A", "B", "C"],
        "rationale": "r",
        "rules_cleanup": {"edits": []},
        "escalate": "",
    }
)


def _router(system: str, user: str) -> str:
    """Dispatch a canned response keyed off each system prompt's first sentence.

    The Phase-5 prompts reference each other (the derive prompt mentions a
    "root-cause analyst", the inherit/neg prompts mention strategies, etc.), so a
    naive substring match is ambiguous. We anchor on the distinctive opening
    sentence of each prompt with ``startswith``.
    """
    s = system.lstrip()
    if s.startswith("You are a root-cause analyst"):
        return _router.root_cause
    if s.startswith("You are a COGNITIVE-STRATEGY designer"):
        return _router.strategy
    if s.startswith("You are guarding against"):
        return _router.neg
    if s.startswith("You are running a CHEAP"):
        return _router.retro
    if s.startswith("You are deciding which low-level"):
        return _router.inherit
    if s.startswith("You are refining"):
        return _router.refine
    return "{}"


def _fresh_router(
    *,
    root_cause: str = _CANNED_ROOT_CAUSE,
    strategy: str = _CANNED_STRATEGY,
    neg: str = _CANNED_NEG_PROCEED,
    retro: str = _CANNED_RETRO_HIGH,
    inherit: str = _CANNED_INHERIT,
    refine: str = _CANNED_REFINE_OK,
):
    """Build a StubLLMClient whose optimizer_fn routes to the given canned blobs."""

    def fn(system: str, user: str) -> str:
        s = system.lstrip()
        if s.startswith("You are a root-cause analyst"):
            return root_cause
        if s.startswith("You are a COGNITIVE-STRATEGY designer"):
            return strategy
        if s.startswith("You are guarding against"):
            return neg
        if s.startswith("You are running a CHEAP"):
            return retro
        if s.startswith("You are deciding which low-level"):
            return inherit
        if s.startswith("You are refining"):
            return refine
        return "{}"

    return StubLLMClient(optimizer_fn=fn)


# ── Fixtures (plain helpers; no pytest fixtures so the __main__ runner works) ───


def _obs(obs_id: str, what: str, polarity: str, evidence: str) -> Observation:
    return Observation(
        obs_id=obs_id,
        task_id="t1",
        rollout_index=0,
        node_id="n0",
        epoch=0,
        what=what,
        cognitive_aspect="planning",
        evidence=evidence,
        consequence="affected outcome",
        significance="critical",
        polarity=polarity,
    )


def _library() -> tuple[PatternLibrary, PatternRecord]:
    """A library with a failure pattern p0001 paired to success counterpart p0002."""
    lib = PatternLibrary()
    fail = PatternRecord(
        pattern_id="p0001",
        name="premature commitment",
        description="locks the first interpretation without re-checking",
        cognitive_aspect="planning",
        polarity="failure",
        remedy_resistance=2,
        counterpart_id="p0002",
        observations=[_obs("o1", "committed to first mapping", "failure", "THOUGHT: proceeding")],
        occurrence_history=[OccurrencePoint(epoch=0, occurrence_rate=0.5, support_count=3, n_tasks=6)],
    )
    succ = PatternRecord(
        pattern_id="p0002",
        name="verify before commit",
        description="re-reads the input before locking an interpretation",
        cognitive_aspect="planning",
        polarity="success",
        counterpart_id="p0001",
        observations=[_obs("o2", "re-read header before mapping", "success", "THOUGHT: verifying")],
    )
    lib.add(fail)
    lib.add(succ)
    return lib, fail


def _strategy_proposal() -> StrategyProposal:
    rc = RootCause(
        pattern_ids=["p0001"],
        behavioral="b",
        process="p",
        strategy="s",
        assumption="a",
        evidence={"behavioral": "x", "process": "x", "strategy": "x", "assumption": "x"},
    )
    return StrategyProposal(
        strategy_text="## Strategy\n### Re-read\nRe-read before commit.",
        rationale="systematize verify-before-commit",
        root_cause=rc,
        targeted_pattern_ids=["p0001"],
    )


def _fail_group() -> TaskRolloutGroup:
    r = TaskResult(task_id="t1", rollout_index=0, hard=0, soft=0.0, fail_reason="premature lock")
    return TaskRolloutGroup(task_id="t1", rollouts=[r])


# ── 1. attribute_root_cause: four-level gate + cross-pattern merge ──────────────


def test_attribute_root_cause_four_levels_and_evidence():
    lib, fail = _library()
    client = StubLLMClient(optimizer_fn=lambda s, u: _CANNED_ROOT_CAUSE)
    cfg = CSSConfig()
    causes = attribute_root_cause(client, [fail], lib, remedy_history=["always recheck"], cfg=cfg)
    assert len(causes) == 1
    rc = causes[0]
    assert rc.pattern_ids == ["p0001"]
    # All four levels are non-empty with cited evidence.
    for level in RootCause.LEVELS:
        assert getattr(rc, level).strip()
        assert str(rc.evidence.get(level, "")).strip()
    assert rc.is_complete
    assert rc.missing_levels() == []
    assert rc.l0_explanation.strip()


def test_attribute_root_cause_drops_level_missing_evidence():
    lib, fail = _library()
    client = StubLLMClient(optimizer_fn=lambda s, u: _CANNED_ROOT_CAUSE_NO_EVIDENCE)
    causes = attribute_root_cause(
        client, [fail], lib, remedy_history=[], cfg=CSSConfig()
    )
    # The CODE GATE drops the only candidate (process level has no evidence).
    assert causes == []


def test_attribute_root_cause_cross_pattern_merge_high_leverage():
    lib, fail = _library()
    # Add a second failure pattern p0003 that the canned response merges with p0001.
    fail2 = PatternRecord(
        pattern_id="p0003",
        name="premature commitment (other family)",
        cognitive_aspect="planning",
        polarity="failure",
        remedy_resistance=1,
        observations=[_obs("o3", "committed early elsewhere", "failure", "THOUGHT: proceeding")],
        occurrence_history=[OccurrencePoint(epoch=0, occurrence_rate=0.4, support_count=2, n_tasks=5)],
    )
    lib.add(fail2)
    client = StubLLMClient(optimizer_fn=lambda s, u: _CANNED_ROOT_CAUSE_CROSS)
    causes = attribute_root_cause(
        client, [fail, fail2], lib, remedy_history=["r1", "r2"], cfg=CSSConfig()
    )
    assert len(causes) == 1
    rc = causes[0]
    # A single high-leverage RootCause covers BOTH patterns.
    assert set(rc.pattern_ids) == {"p0001", "p0003"}
    assert rc.leverage.strip()
    assert rc.is_complete


def test_attribute_root_cause_empty_signals_returns_empty():
    lib, _ = _library()
    client = StubLLMClient(optimizer_fn=lambda s, u: _CANNED_ROOT_CAUSE)
    assert attribute_root_cause(client, [], lib, remedy_history=[], cfg=CSSConfig()) == []


# ── 2. derive_strategy ──────────────────────────────────────────────────────────


def test_derive_strategy_subsections_and_targets():
    rc = RootCause(
        pattern_ids=["p0001"],
        behavioral="committed early",
        process="treated first reading as settled",
        strategy="plan then execute",
        assumption="first reading is right",
        evidence={"behavioral": "x", "process": "x", "strategy": "x", "assumption": "x"},
    )
    lib, _ = _library()
    counterparts = [lib.get("p0002")]
    client = StubLLMClient(optimizer_fn=lambda s, u: _CANNED_STRATEGY)
    proposal = derive_strategy(client, rc, counterparts, cfg=CSSConfig())
    assert proposal.strategy_text.strip()
    # Organized in ### subsections.
    assert "### " in proposal.strategy_text
    assert proposal.targeted_pattern_ids == ["p0001"]
    assert proposal.root_cause is rc
    assert proposal.rationale.strip()


def test_derive_strategy_defaults_targets_to_root_cause():
    rc = RootCause(
        pattern_ids=["p0001"],
        behavioral="b", process="p", strategy="s", assumption="a",
        evidence={"behavioral": "x", "process": "x", "strategy": "x", "assumption": "x"},
    )
    # LLM omits targeted_pattern_ids -> default to the root cause's patterns.
    resp = json.dumps({"strategy_text": "## S\n### X\nbody", "rationale": "r"})
    client = StubLLMClient(optimizer_fn=lambda s, u: resp)
    proposal = derive_strategy(client, rc, [], cfg=CSSConfig())
    assert proposal.targeted_pattern_ids == ["p0001"]


# ── 3. check_negative_archive ───────────────────────────────────────────────────


def _seed_archive(snapshot: str) -> NegativeArchive:
    arch = NegativeArchive()
    arch.add(
        NegativeArchiveEntry(
            entry_id=arch.new_entry_id(),
            strategy_snapshot=snapshot,
            origin="proposal_failed_rollout",
            root_cause="patterns[p0001] strategy: lock early",
            failure_evidence="occurrence 0.5 -> 0.5",
        )
    )
    return arch


def test_check_negative_archive_blocks_when_no_difference():
    strat = "## Strategy\n### Re-read\nRe-read before commit."
    arch = _seed_archive(strat)  # identical text -> Jaccard 1.0 -> ask LLM
    client = StubLLMClient(optimizer_fn=lambda s, u: _CANNED_NEG_BLOCK)
    proceed, reason = check_negative_archive(client, strat, arch)
    assert proceed is False
    assert "too similar" in reason


def test_check_negative_archive_proceeds_with_clear_difference():
    strat = "## Strategy\n### Re-read\nRe-read before commit."
    arch = _seed_archive(strat)
    client = StubLLMClient(optimizer_fn=lambda s, u: _CANNED_NEG_PROCEED)
    proceed, reason = check_negative_archive(client, strat, arch)
    assert proceed is True
    assert "meaningfully different" in reason


def test_check_negative_archive_empty_archive_proceeds():
    client = StubLLMClient(optimizer_fn=lambda s, u: _CANNED_NEG_BLOCK)
    proceed, reason = check_negative_archive(
        client, "## S\n### X\nbody", NegativeArchive()
    )
    assert proceed is True
    assert "empty" in reason


# ── 4. retrospective_validate: coverage gate ────────────────────────────────────


def test_retrospective_validate_low_coverage_reconsider():
    client = StubLLMClient(optimizer_fn=lambda s, u: _CANNED_RETRO_LOW)
    res = retrospective_validate(
        client, _strategy_proposal(), [], [_fail_group()], cfg=CSSConfig()
    )
    assert res.coverage == 0.1
    assert res.verdict == "reconsider"


def test_retrospective_validate_band_reconsiders():
    # Coverage in the [coverage_low, coverage_high) band reconsiders: the gate
    # keys on coverage_high (the literal ">high -> proceed"), not coverage_low.
    cfg = CSSConfig()  # coverage_low=0.20, coverage_high=0.30
    band = json.dumps({"coverage": 0.25, "positive_evidence": ["x"], "counterfactuals": ["y"]})
    client = StubLLMClient(optimizer_fn=lambda s, u: band)
    res = retrospective_validate(client, _strategy_proposal(), [], [_fail_group()], cfg=cfg)
    assert res.coverage == 0.25
    assert res.verdict == "reconsider"  # 0.25 < coverage_high(0.30)


def test_retrospective_validate_high_coverage_proceed():
    client = StubLLMClient(optimizer_fn=lambda s, u: _CANNED_RETRO_HIGH)
    res = retrospective_validate(
        client, _strategy_proposal(), [], [_fail_group()], cfg=CSSConfig()
    )
    assert res.coverage == 0.5
    assert res.verdict == "proceed"


# ── 5. derive_refine + is_cleanup_only ──────────────────────────────────────────


def _refine_root_cause() -> RootCause:
    return RootCause(
        pattern_ids=["p0001"],
        behavioral="committed early",
        process="treated first reading as settled",
        strategy="the Planning subsection locks early",
        assumption="first reading is right",
        evidence={"behavioral": "x", "process": "x", "strategy": "x", "assumption": "x"},
    )


def test_derive_refine_one_subsection_passes_gate():
    client = StubLLMClient(optimizer_fn=lambda s, u: _CANNED_REFINE_OK)
    proposal, reason = derive_refine(
        client, _refine_root_cause(), _PARENT_STRATEGY, _PARENT_RULES, cfg=CSSConfig()
    )
    assert proposal is not None
    # changed_subsections comes from the DETERMINISTIC diff (heading key, lowered).
    assert proposal.changed_subsections == ["planning"]
    # The illegal 'append' was dropped; only the delete survives, cleanup-only.
    assert is_cleanup_only(proposal.rules_cleanup)
    assert all(e.op in ("replace", "delete") for e in proposal.rules_cleanup.edits)
    assert len(proposal.rules_cleanup.edits) == 1
    assert reason.startswith("ok:")


def test_derive_refine_too_many_subsections_escalates():
    client = StubLLMClient(optimizer_fn=lambda s, u: _CANNED_REFINE_TOO_MANY)
    proposal, reason = derive_refine(
        client, _refine_root_cause(), _PARENT_STRATEGY_3, "", cfg=CSSConfig()
    )
    assert proposal is None
    assert "too_many" in reason and "escalate" in reason


def test_derive_refine_structure_change_escalates():
    # Child REMOVES a subsection (Execution) -> structural change, must escalate.
    child = "## Cognitive Strategy\n### Planning\nRe-read then lock."
    resp = json.dumps(
        {"strategy_text": child, "changed_subsections": ["Planning"],
         "rationale": "r", "rules_cleanup": {"edits": []}, "escalate": ""}
    )
    client = StubLLMClient(optimizer_fn=lambda s, u: resp)
    proposal, reason = derive_refine(
        client, _refine_root_cause(), _PARENT_STRATEGY, "", cfg=CSSConfig()
    )
    assert proposal is None
    assert "structure" in reason and "escalate" in reason


def test_derive_refine_llm_escalate_signal():
    resp = json.dumps(
        {"strategy_text": _REFINE_CHILD_OK, "changed_subsections": ["Planning"],
         "rationale": "r", "rules_cleanup": {"edits": []},
         "escalate": "a correct fix needs the whole document rewritten"}
    )
    client = StubLLMClient(optimizer_fn=lambda s, u: resp)
    proposal, reason = derive_refine(
        client, _refine_root_cause(), _PARENT_STRATEGY, "", cfg=CSSConfig()
    )
    assert proposal is None
    assert "escalate" in reason


def test_is_cleanup_only_predicate():
    assert is_cleanup_only(Patch(edits=[]))  # empty is vacuously cleanup-only
    assert is_cleanup_only(Patch(edits=[Edit(op="replace", target="a", content="b")]))
    assert is_cleanup_only(Patch(edits=[Edit(op="delete", target="a")]))
    assert not is_cleanup_only(Patch(edits=[Edit(op="append", content="x")]))
    assert not is_cleanup_only(
        Patch(edits=[Edit(op="delete", target="a"), Edit(op="insert_after", target="b", content="c")])
    )


# ── 6. inheritance ──────────────────────────────────────────────────────────────


def test_proposal_inherit_rules_keep_drop():
    client = StubLLMClient(optimizer_fn=lambda s, u: _CANNED_INHERIT)
    kept = proposal_inherit_rules(
        client,
        "## Strategy\n### Re-read\nRe-read before commit.",
        _PARENT_RULES,
        cfg=CSSConfig(),
    )
    assert "Always validate the final output range before saving." in kept
    # The contradicting rule was dropped.
    assert "Lock the column mapping on first read." not in kept


def test_proposal_inherit_rules_empty_parent_returns_empty():
    client = StubLLMClient(optimizer_fn=lambda s, u: _CANNED_INHERIT)
    assert proposal_inherit_rules(client, "new strategy", "", cfg=CSSConfig()) == ""


def test_refine_apply_cleanup_replace_delete():
    cleanup = Patch(
        edits=[Edit(op="delete", target="Lock the column mapping on first read.")],
        reasoning="conflicts with re-read",
    )
    new_rules, reports = refine_apply_cleanup(_PARENT_RULES, cleanup)
    assert "Lock the column mapping on first read." not in new_rules
    # Full inherit: the unrelated rule survives.
    assert "Always validate the final output range before saving." in new_rules
    assert reports and reports[0].status == "applied"


def test_refine_apply_cleanup_rejects_append():
    cleanup = Patch(edits=[Edit(op="append", content="a new rule")])
    try:
        refine_apply_cleanup(_PARENT_RULES, cleanup)
    except ValueError:
        pass
    else:
        raise AssertionError("refine_apply_cleanup must reject an append op")


# ── 7. run_proposal end-to-end ──────────────────────────────────────────────────


def _proposal_node() -> TreeNode:
    return TreeNode(
        node_id="n0",
        strategy=_PARENT_STRATEGY,
        rules=_PARENT_RULES,
        refine_count=1,
    )


import pytest

# Tests below test the OLD single-shot run_proposal interface (pre-v3 L1 cycle).
# The v3 L1 cycle replaces them with run_l1_cycle (tested separately).
# These tests are skipped until they are rewritten for the new interface.
_V2_SKIP = pytest.mark.skip(reason="run_proposal v3: old interface tests, pending rewrite")


@_V2_SKIP
def test_run_proposal_success_builds_node_with_inherited_rules():
    lib, fail = _library()
    client = _fresh_router()
    arch = NegativeArchive()
    node = _proposal_node()
    out = run_proposal(
        node, [fail], lib, arch, client,
        cfg=CSSConfig(), new_node_id="n0001", epoch=2,
        success_results=[], persistent_fail_groups=[_fail_group()],
        rollout_validate_fn=lambda strat, rules, pids: (0.5, 0.1),  # occurrence DROPS
    )
    assert out.success is True
    assert out.operation == "PROPOSAL"
    assert out.new_node is not None
    assert out.new_node.branch_type == "PROPOSAL"
    assert out.new_node.parent_id == "n0"
    assert out.new_node.created_epoch == 2
    # Rules are the SEMANTIC keep/drop inheritance (kept rule present, dropped gone).
    assert "Always validate the final output range before saving." in out.new_node.rules
    assert "Lock the column mapping on first read." not in out.new_node.rules
    assert out.occurrence_before == 0.5 and out.occurrence_after == 0.1
    assert arch.entries == []  # nothing archived on success


@_V2_SKIP
def test_run_proposal_rollout_fail_archives():
    lib, fail = _library()
    client = _fresh_router()
    arch = NegativeArchive()
    node = _proposal_node()
    out = run_proposal(
        node, [fail], lib, arch, client,
        cfg=CSSConfig(), new_node_id="n0001", epoch=2,
        success_results=[], persistent_fail_groups=[_fail_group()],
        rollout_validate_fn=lambda strat, rules, pids: (0.5, 0.5),  # NO drop
    )
    assert out.success is False
    assert out.new_node is None
    assert out.archived is not None
    assert out.archived.origin == "proposal_failed_rollout"
    assert out.archived.strategy_snapshot  # text preserved for Jaccard recall
    assert len(arch.entries) == 1


@_V2_SKIP
def test_run_proposal_does_not_mutate_shared_cfg():
    # The major fix: the current strategy is threaded explicitly, NOT stashed on
    # shared cfg — so concurrent Phase-6 nodes cannot clobber each other.
    lib, fail = _library()
    client = _fresh_router()
    arch = NegativeArchive()
    node = _proposal_node()
    cfg = CSSConfig()
    run_proposal(
        node, [fail], lib, arch, client,
        cfg=cfg, new_node_id="n0001", epoch=2,
        success_results=[], persistent_fail_groups=[_fail_group()],
        rollout_validate_fn=lambda strat, rules, pids: (0.5, 0.1),
    )
    assert "current_strategy" not in cfg.extra


@_V2_SKIP
def test_run_proposal_rollout_error_archives_distinctly():
    # An infrastructure failure in the 5c callback must NOT look like a real
    # measured no-decrease: it archives with a distinct "could not be measured"
    # evidence and a rollout_error reason.
    lib, fail = _library()
    client = _fresh_router()
    arch = NegativeArchive()
    node = _proposal_node()

    def _boom(strat, rules, pids):
        raise RuntimeError("env exploded")

    out = run_proposal(
        node, [fail], lib, arch, client,
        cfg=CSSConfig(), new_node_id="n0001", epoch=2,
        success_results=[], persistent_fail_groups=[_fail_group()],
        rollout_validate_fn=_boom,
    )
    assert out.success is False and out.archived is not None
    assert out.reason.startswith("rollout_error")
    assert "could not be measured" in out.archived.failure_evidence


@_V2_SKIP
def test_run_proposal_neg_archive_block_skips_rollout():
    lib, fail = _library()
    # Archive contains the EXACT derived strategy; LLM articulates NO difference.
    derived_text = json.loads(_CANNED_STRATEGY)["strategy_text"]
    arch = _seed_archive(derived_text)
    client = _fresh_router(neg=_CANNED_NEG_BLOCK)

    rollout_calls = []

    def rollout_fn(strat, rules, pids):
        rollout_calls.append(1)
        return (0.5, 0.1)

    out = run_proposal(
        _proposal_node(), [fail], lib, arch, client,
        cfg=CSSConfig(), new_node_id="n0001", epoch=2,
        success_results=[], persistent_fail_groups=[_fail_group()],
        rollout_validate_fn=rollout_fn,
    )
    assert out.success is False
    assert out.reason.startswith("neg_archive_block")
    assert rollout_calls == []  # rollout never ran
    assert out.archived is not None  # the disproven direction is re-recorded


@_V2_SKIP
def test_run_proposal_low_coverage_kills_cheaply():
    lib, fail = _library()
    client = _fresh_router(retro=_CANNED_RETRO_LOW)
    rollout_calls = []
    out = run_proposal(
        _proposal_node(), [fail], lib, NegativeArchive(), client,
        cfg=CSSConfig(), new_node_id="n0001", epoch=2,
        success_results=[], persistent_fail_groups=[_fail_group()],
        rollout_validate_fn=lambda *a: rollout_calls.append(1) or (0.5, 0.1),
    )
    assert out.success is False
    assert out.reason.startswith("low_coverage")
    assert rollout_calls == []  # no rollout budget spent


# ── 8. dataclass from_dict/to_dict round-trips ──────────────────────────────────


def test_root_cause_round_trip():
    rc = RootCause(
        pattern_ids=["p0001", "p0003"],
        behavioral="b", process="p", strategy="s", assumption="a",
        leverage="lev", l0_explanation="l0",
        evidence={"behavioral": "x", "process": "y", "strategy": "z", "assumption": "w"},
    )
    rc2 = RootCause.from_dict(rc.to_dict())
    assert rc2.to_dict() == rc.to_dict()
    assert rc2.pattern_ids == rc.pattern_ids
    assert rc2.is_complete


def test_strategy_proposal_round_trip():
    sp = _strategy_proposal()
    sp2 = StrategyProposal.from_dict(sp.to_dict())
    assert sp2.to_dict() == sp.to_dict()
    assert sp2.targeted_pattern_ids == sp.targeted_pattern_ids
    assert isinstance(sp2.root_cause, RootCause)


def test_validation_result_round_trip():
    vr = ValidationResult(
        coverage=0.42, verdict="proceed",
        positive_evidence=["pe1"], counterfactuals=["cf1", "cf2"],
    )
    vr2 = ValidationResult.from_dict(vr.to_dict())
    assert vr2.to_dict() == vr.to_dict()
    # Bad verdict is sanitized to "reconsider"; coverage clamped to [0,1].
    bad = ValidationResult.from_dict({"coverage": 5.0, "verdict": "nonsense"})
    assert bad.verdict == "reconsider"
    assert bad.coverage == 1.0


def test_refine_proposal_round_trip():
    rc = _refine_root_cause()
    rp = RefineProposal(
        strategy_text=_REFINE_CHILD_OK,
        changed_subsections=["planning"],
        root_cause=rc,
        rules_cleanup=Patch(edits=[Edit(op="delete", target="x")], reasoning="r"),
    )
    rp2 = RefineProposal.from_dict(rp.to_dict())
    assert rp2.to_dict() == rp.to_dict()
    assert rp2.changed_subsections == ["planning"]
    assert is_cleanup_only(rp2.rules_cleanup)


def test_proposal_outcome_to_dict():
    out = ProposalOutcome(success=False, operation="PROPOSAL", reason="x")
    d = out.to_dict()
    assert d["success"] is False
    assert d["operation"] == "PROPOSAL"
    assert d["new_node"] is None and d["archived"] is None


# ── 9. import smoke for all css.proposal.* modules ──────────────────────────────


def test_import_smoke_no_heavy_backends():
    import importlib
    import sys

    faiss_before = "faiss" in sys.modules
    for mod in (
        "css.proposal.root_cause",
        "css.proposal.derivation",
        "css.proposal.refine",
        "css.proposal.inheritance",
        "css.proposal.proposal",
    ):
        importlib.import_module(mod)
    # Importing Phase 5 must not NEWLY pull faiss.
    if not faiss_before:
        assert "faiss" not in sys.modules
    # Keep referenced symbols live.
    assert all(
        fn is not None
        for fn in (attribute_root_cause, derive_strategy, derive_refine,
                   proposal_inherit_rules, run_proposal)
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
