"""Adjudication-loop and merger tests driven by StubLLMClient — deterministic,
no network. Perturbation cases mirror real observed LLM failure modes."""
from __future__ import annotations

import json
import os

import pytest

from css.data.edit import RawPatch
from css.model.client import StubLLMClient
from css.optimizer.editpipe.adjudicate import adjudicate
from css.optimizer.editpipe.merger import (
    parse_merger_output,
    required_fields_missing,
    run_merger,
)
from css.optimizer.editpipe.pipeline import run_pipeline
from css.optimizer.editpipe.render import RulesDoc
from css.optimizer.editpipe.schema import SectionEdit

FIX = os.path.join(os.path.dirname(__file__), "..", "..", "tests",
                   "fixtures", "edit_pipeline")

BASE = "### Alpha\n- a1\n- a2\n\n### Beta\n- b1\n"


def _edit(**kw) -> SectionEdit:
    base = dict(kind="add_section", subject="New", body="- r",
                target_tasks=["t"])
    base.update(kw)
    return SectionEdit.from_dict(base)


def _valid_response():
    return json.dumps({"valid": True, "violations": []})


# ── adjudication loop ────────────────────────────────────────────────────────

def test_adjudicate_clean_set_converges_round1():
    calls = []

    def opt(system, user):
        calls.append(system[:40])
        return _valid_response()

    client = StubLLMClient(optimizer_fn=opt)
    edits = [_edit(subject="Topic A"), _edit(subject="Topic B", body="- x")]
    res = adjudicate(client, BASE, edits)
    assert res.converged and res.rounds == 1
    assert len(res.edits) == 2
    assert len(calls) == 1  # one validator call, no repair


def test_adjudicate_collision_repaired_by_llm():
    """identity_collision -> repair renames one edit -> converges."""
    state = {"n": 0}

    def opt(system, user):
        if "edit validator" in system:
            return _valid_response()
        # repair call: rename E#1
        state["n"] += 1
        return json.dumps({
            "reasoning": "rename the second claimant",
            "operations": [{
                "op": "replace", "id": "E#1",
                "edit": {"kind": "add_section", "subject": "Distinct Topic",
                         "body": "- different rule",
                         "target_tasks": ["t2"]},
            }],
        })

    client = StubLLMClient(optimizer_fn=opt)
    edits = [_edit(subject="Same Topic", body="- one"),
             _edit(subject="Same Topic", body="- two")]
    res = adjudicate(client, BASE, edits)
    assert res.converged
    assert sorted(e.subject for e in res.edits) == \
        ["Distinct Topic", "Same Topic"]
    assert state["n"] == 1


def test_adjudicate_repair_drop_is_audited():
    def opt(system, user):
        if "edit validator" in system:
            return _valid_response()
        return json.dumps({
            "reasoning": "duplicate",
            "operations": [{"op": "drop", "id": "E#1",
                            "reason": "verbatim duplicate of E#0"}],
        })

    client = StubLLMClient(optimizer_fn=opt)
    edits = [_edit(subject="Same Topic", body="- one"),
             _edit(subject="Same Topic", body="- one bis")]
    res = adjudicate(client, BASE, edits)
    assert res.converged and len(res.edits) == 1
    drops = [a for a in res.audits if a.fate == "dropped"]
    assert len(drops) == 1
    assert drops[0].actor == "adjudicator"
    assert "duplicate" in drops[0].reason


def test_adjudicate_nonconvergence_falls_back_content_preserving():
    """Repair never fixes anything -> deterministic fallback demotes, keeps
    all content, blocks nothing."""

    def opt(system, user):
        if "edit validator" in system:
            return _valid_response()
        return json.dumps({"reasoning": "no ops", "operations": []})

    client = StubLLMClient(optimizer_fn=opt)
    edits = [
        _edit(subject="Same Topic", body="- one", target_tasks=["a", "b"]),
        _edit(subject="Same Topic", body="- two", target_tasks=["c"]),
        _edit(subject="Same Topic", body="- three",
              target_tasks=["d", "e", "f"]),
    ]
    res = adjudicate(client, BASE, edits, max_rounds=2)
    assert not res.converged
    assert len(res.edits) == 3                      # nothing deleted
    adds = [e for e in res.edits if e.kind == "add_section"]
    demoted = [e for e in res.edits if e.kind == "add_point"]
    assert len(adds) == 1 and len(demoted) == 2
    assert adds[0].target_tasks == ["d", "e", "f"]  # max support kept role
    assert all(a.actor == "fallback" for a in res.audits
               if a.fate == "demoted")


def test_adjudicate_scope_guard_rejects_out_of_allowlist_ops():
    """Repair tries to drop an edit not implicated in any violation — the op
    must be rejected and the edit survive."""

    def opt(system, user):
        if "edit validator" in system:
            return _valid_response()
        return json.dumps({
            "reasoning": "sneaky",
            "operations": [
                {"op": "drop", "id": "E#2", "reason": "I just felt like it"},
                {"op": "replace", "id": "E#0",
                 "edit": {"kind": "add_section", "subject": "Renamed",
                          "body": "- one", "target_tasks": ["t"]}},
            ],
        })

    client = StubLLMClient(optimizer_fn=opt)
    edits = [_edit(subject="Same Topic", body="- one"),
             _edit(subject="Same Topic", body="- two"),
             _edit(subject="Innocent Bystander", body="- three")]
    res = adjudicate(client, BASE, edits, max_rounds=3)
    subjects = {e.subject for e in res.edits}
    assert "Innocent Bystander" in subjects        # survived the sneaky drop


def test_adjudicate_semantic_purity_flag_reaches_repair():
    state = {"validator_calls": 0}

    def opt(system, user):
        if "edit validator" in system:
            state["validator_calls"] += 1
            if state["validator_calls"] == 1:
                return json.dumps({"valid": False, "violations": [{
                    "type": "content_purity", "edit_indices": [0],
                    "detail": "body references task IDs",
                    "suggestion": "strip provenance"}]})
            return _valid_response()
        return json.dumps({
            "reasoning": "strip provenance",
            "operations": [{"op": "replace", "id": "E#0",
                            "edit": {"kind": "add_section",
                                     "subject": "Topic A",
                                     "body": "- clean rule",
                                     "target_tasks": ["t"]}}],
        })

    client = StubLLMClient(optimizer_fn=opt)
    res = adjudicate(client, BASE, [_edit(subject="Topic A",
                                          body="- rule (from task 42)")])
    assert res.converged
    assert res.edits[0].body == "- clean rule"


def test_adjudicate_llm_failure_is_optimistic_not_fatal():
    def opt(system, user):
        raise RuntimeError("network down")

    client = StubLLMClient(optimizer_fn=opt)
    edits = [_edit(subject="Topic A")]
    res = adjudicate(client, BASE, edits)
    assert res.converged and len(res.edits) == 1


# ── merger with stubbed LLM ──────────────────────────────────────────────────

def _load_patches(case: str) -> list[RawPatch]:
    with open(os.path.join(FIX, case, "raw_patches.json"),
              encoding="utf-8") as f:
        raw = json.load(f)
    return [RawPatch.from_dict(d) for d in raw]


def test_run_merger_step1_shape_with_stub():
    """Feed the REAL step1 raw patches; stub returns a well-formed v2 output.
    Verifies prompt assembly + parse + gate wiring end to end (no network)."""
    patches = _load_patches("appworld_step1")
    with open(os.path.join(FIX, "appworld_step1", "base_rules.md"),
              encoding="utf-8") as f:
        base = f.read()

    def opt(system, user):
        assert "## Raw edits to merge (59 total)" in user
        assert "position hint" in user       # legacy target explained
        return json.dumps({"reasoning": "ok", "edits": [
            {"kind": "add_section", "subject": "Spotify Library Completeness",
             "body": "- fetch songs, albums, playlists.",
             "target_tasks": ["a", "b"]},
            {"kind": "add_point", "subject": "Pagination Discipline",
             "anchor": "- **Mandatory Loop**",
             "body": "- set page_limit high.", "target_tasks": ["c"]},
        ]})

    client = StubLLMClient(optimizer_fn=opt)
    edits, violations, audits, stats = run_merger(client, base, patches)
    assert stats["parsed_edits"] == 2
    assert len(edits) == 2
    assert violations == []


def test_run_merger_coherence_round_fixes_collisions():
    """Perturbation: stub merger reproduces the REAL accident (many adds all
    claiming one subject); the coherence round returns corrected subjects."""
    patches = _load_patches("appworld_step1")
    with open(os.path.join(FIX, "appworld_step1", "base_rules.md"),
              encoding="utf-8") as f:
        base = f.read()
    state = {"calls": 0}

    def opt(system, user):
        state["calls"] += 1
        if state["calls"] == 1:
            # accident shape: distinct bodies, one subject claimed 3x
            return json.dumps({"reasoning": "bad", "edits": [
                {"kind": "add_section", "subject": "Temporal Context Resolution",
                 "body": f"- distinct topic {i}", "target_tasks": [f"t{i}"]}
                for i in range(3)
            ]})
        # repair prompt (coherence): return corrected full set
        return json.dumps({"edits": [
            {"kind": "add_section", "subject": f"Distinct Topic {i}",
             "body": f"- distinct topic {i}", "target_tasks": [f"t{i}"]}
            for i in range(3)
        ]})

    client = StubLLMClient(optimizer_fn=opt)
    edits, violations, audits, stats = run_merger(client, base, patches)
    assert stats["coherence_rounds"] == 1
    assert len(edits) == 3
    assert len({e.subject for e in edits}) == 3
    assert not [v for v in violations if v.vtype == "identity_collision"]


def test_sb_accident_shape_batch_target_tasks_omission_recovers():
    """The recorded SpreadsheetBench failure mode: the merger omits
    target_tasks on EVERY edit (which under the legacy validator collapsed a
    whole step to merged_edits == []). The required-field hook must recover
    it with one feedback repair call."""
    patches = _load_patches("spreadsheetbench_step2")
    with open(os.path.join(FIX, "spreadsheetbench_step2", "base_rules.md"),
              encoding="utf-8") as f:
        base = f.read()
    state = {"n": 0}

    def opt(system, user):
        state["n"] += 1
        if state["n"] == 1:
            return json.dumps({"reasoning": "x", "edits": [
                {"kind": "add_section", "subject": "Computed Values Refinement",
                 "body": "- compute in python."},
                {"kind": "add_point", "subject": "Sheet and Range Fidelity",
                 "anchor": "**Answer Position Alignment**",
                 "body": "- align."},
            ]})
        assert "target_tasks" in user      # repair feedback names the field
        return json.dumps({"edits": [
            {"kind": "add_section", "subject": "Computed Values Refinement",
             "body": "- compute in python.", "target_tasks": ["49801"]},
            {"kind": "add_point", "subject": "Sheet and Range Fidelity",
             "anchor": "**Answer Position Alignment**", "body": "- align.",
             "target_tasks": ["168-17"]},
        ]})

    client = StubLLMClient(optimizer_fn=opt)
    edits, violations, audits, stats = run_merger(client, base, patches)
    assert len(edits) == 2
    assert all(e.target_tasks for e in edits)
    assert not [v for v in violations if v.vtype == "missing_target_tasks"]
    assert state["n"] == 2                 # merger + exactly one repair


def test_required_fields_hook_matches_gate_blockers():
    edits = [{"kind": "add_point", "subject": "S"}]  # no anchor, body, tasks
    missing = required_fields_missing(edits)
    assert any("anchor" in m for m in missing)
    assert any("body" in m for m in missing)
    assert any("target_tasks" in m for m in missing)


def test_parse_merger_output_variants():
    obj = {"edits": [{"kind": "add_section", "subject": "S", "body": "- x"}]}
    assert parse_merger_output(json.dumps(obj))
    assert parse_merger_output("```json\n" + json.dumps(obj) + "\n```")
    assert parse_merger_output("noise " + json.dumps(obj) + " trailing")
    assert parse_merger_output("no json here") is None
    assert parse_merger_output("") is None


# ── full pipeline with stub ──────────────────────────────────────────────────

def test_run_pipeline_end_to_end_stub():
    patches = _load_patches("appworld_step1")
    with open(os.path.join(FIX, "appworld_step1", "base_rules.md"),
              encoding="utf-8") as f:
        base = f.read()

    def opt(system, user):
        if "You are the MERGER" in system:
            return json.dumps({"reasoning": "ok", "edits": [
                {"kind": "add_section", "subject": "Authentication Discipline",
                 "body": "- pass access_token explicitly.",
                 "target_tasks": ["a"]},
                {"kind": "add_point", "subject": "Pagination Discipline",
                 "anchor": "NO SUCH ANCHOR IN DOC",
                 "body": "- page_limit high.", "target_tasks": ["b"]},
            ]})
        if "edit validator" in system:
            return _valid_response()
        if "precise text editor" in system:
            # Contract-abiding resolver: keep the section body, insert the
            # edit content (extract both from the user prompt).
            sec_body = user.split("## Section:", 1)[1].split("\n", 1)[1] \
                .split("\n\n## Edit", 1)[0]
            edit_body = user.rsplit("body:\n", 1)[1]
            return json.dumps(
                {"body": sec_body + "\n" + edit_body + " (resolver-was-here)"})
        return _valid_response()

    client = StubLLMClient(optimizer_fn=opt)
    result = run_pipeline(client, base, patches)
    assert result.consolidation.converged
    assert result.apply_result.assertion_failures == []
    doc = RulesDoc.parse(result.candidate_text)
    assert "Authentication Discipline" in [s.subject for s in doc.sections]
    # resolver was engaged for the unfindable anchor:
    assert "resolver-was-here" in result.candidate_text


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
