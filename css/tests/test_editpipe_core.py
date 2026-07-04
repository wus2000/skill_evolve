"""Deterministic-core tests for editpipe: render, schema gate, conflicts, apply.

Uses the real AppWorld fixtures where possible so the tests encode the actual
accident modes (step1's identity collision, the empty-shell candidate).
"""
from __future__ import annotations

import json
import os

import pytest

from css.optimizer.editpipe.apply import apply_edits
from css.optimizer.editpipe.render import (
    RulesDoc,
    assert_structure,
    demote_headings,
    normalize_subject,
    strip_redundant_heading,
    subject_key,
)
from css.optimizer.editpipe.schema import (
    SectionEdit,
    detect_conflicts,
    syntax_gate,
)

FIX = os.path.join(os.path.dirname(__file__), "..", "..", "tests",
                   "fixtures", "edit_pipeline")


def _fixture(case: str, name: str) -> str:
    return os.path.join(FIX, case, name)


def _load_rules(case: str) -> str:
    with open(_fixture(case, "base_rules.md"), encoding="utf-8") as f:
        return f.read()


# ── render ───────────────────────────────────────────────────────────────────

def test_subject_normalization():
    assert normalize_subject("###  Pagination   Discipline ") == \
        "Pagination Discipline"
    assert subject_key("### Temporal Context Resolution") == \
        subject_key("temporal   context resolution")


def test_parse_render_roundtrip_real_base():
    text = _load_rules("appworld_step1")
    doc = RulesDoc.parse(text)
    assert [s.subject for s in doc.sections] == \
        ["Pagination Discipline", "Temporal Context Resolution"]
    again = RulesDoc.parse(doc.render())
    assert [s.subject for s in again.sections] == \
        [s.subject for s in doc.sections]
    assert [s.body.strip() for s in again.sections] == \
        [s.body.strip() for s in doc.sections]


def test_normalize_repairs_empty_shell_artifact():
    """The real step1 accident: empty shell heading + real body twin."""
    with open(_fixture("appworld_step1", "candidate_rules.md"),
              encoding="utf-8") as f:
        broken = f.read()
    doc = RulesDoc.parse(broken)
    problems_before = assert_structure(broken)
    assert any("duplicate section heading" in p for p in problems_before)
    notes = doc.normalize()
    healed = doc.render()
    assert assert_structure(healed) == []
    assert any("empty" in n for n in notes)
    # No content lost: every non-empty body survived.
    assert "Explicit Authentication Parameters" in healed
    assert "Playback API Resolution" in healed


def test_demote_and_strip_helpers():
    body = "### Sub Heading\n- rule one\n## Deep\n- rule two"
    demoted, n = demote_headings(body)
    assert n == 2 and "###" not in demoted and "- rule one" in demoted
    assert strip_redundant_heading(
        "My Topic", "### My Topic\n- a") == "- a"
    assert strip_redundant_heading(
        "My Topic", "### Other\n- a") == "### Other\n- a"


# ── syntax gate ──────────────────────────────────────────────────────────────

def _edit(**kw) -> SectionEdit:
    base = dict(kind="add_section", subject="New Topic", body="- rule",
                target_tasks=["t1"])
    base.update(kw)
    return SectionEdit.from_dict(base)


def test_gate_never_drops_for_semantic_reasons():
    doc = RulesDoc.parse(_load_rules("appworld_step1"))
    edits = [
        _edit(subject="Same Name"),
        _edit(subject="Same Name", body="- different rule"),
        _edit(kind="bogus_kind", subject="X"),
        _edit(kind="rewrite_section", subject="Nonexistent Section"),
        _edit(kind="add_point", subject="Pagination Discipline",
              body="- p", anchor="- **Mandatory Loop**"),
    ]
    kept, violations, audits = syntax_gate(edits, doc)
    # Nothing semantically questionable was dropped:
    assert len(kept) == 5
    vtypes = {v.vtype for v in violations}
    assert "invalid_kind" in vtypes
    # rewrite of missing section was mechanically converted (lossless):
    assert any(a.fate == "converted" for a in audits)
    assert kept[3].kind == "add_section"


def test_gate_drops_only_byte_identical():
    doc = RulesDoc(preamble="", sections=[])
    e1 = _edit(subject="Topic A", body="- same")
    e2 = _edit(subject="Topic A", body="- same")
    e3 = _edit(subject="Topic A", body="- DIFFERENT")
    kept, violations, audits = syntax_gate([e1, e2, e3], doc)
    assert len(kept) == 2
    dropped = [a for a in audits if a.fate == "dropped"]
    assert len(dropped) == 1 and "byte-identical" in dropped[0].reason


def test_gate_strips_redundant_heading_and_flags_foreign():
    doc = RulesDoc(preamble="", sections=[])
    e1 = _edit(subject="Topic A", body="### Topic A\n- rule")
    e2 = _edit(subject="Topic B", body="### Something Else\n- rule")
    kept, violations, audits = syntax_gate([e1, e2], doc)
    assert kept[0].body == "- rule"
    assert any(v.vtype == "body_contains_heading" and v.edit_ids == ["E#1"]
               for v in violations)


def test_gate_missing_fields_are_violations_not_drops():
    doc = RulesDoc(preamble="", sections=[])
    e = SectionEdit(kind="add_section", subject="", body="", target_tasks=[])
    kept, violations, _ = syntax_gate([e], doc)
    assert len(kept) == 1
    vtypes = [v.vtype for v in violations]
    assert "missing_subject" in vtypes and "missing_body" in vtypes \
        and "missing_target_tasks" in vtypes


# ── disjointness / conflicts ────────────────────────────────────────────────

def test_step1_collision_is_detected_not_fatal():
    """Replays the real accident shape: many adds claiming one subject."""
    doc = RulesDoc.parse(_load_rules("appworld_step1"))
    edits = [
        _edit(subject="Temporal Context Resolution",
              body=f"- topic {i}") for i in range(3)
    ]
    kept, _, _ = syntax_gate(edits, doc)
    violations = detect_conflicts(kept, doc)
    vtypes = {v.vtype for v in violations}
    assert "identity_collision" in vtypes
    assert "add_exists" in vtypes          # subject already in the document
    assert len(kept) == 3                  # nothing was killed mechanically


def test_distinct_adds_never_conflict():
    doc = RulesDoc.parse(_load_rules("appworld_step1"))
    edits = [_edit(subject=f"Distinct Topic {i}", body=f"- r{i}",
                   placement="Temporal Context Resolution")
             for i in range(8)]
    kept, violations, _ = syntax_gate(edits, doc)
    assert len(kept) == 8
    assert detect_conflicts(kept, doc) == []


def test_whole_section_op_conflicts_with_points():
    doc = RulesDoc.parse(_load_rules("appworld_step1"))
    edits = [
        _edit(kind="rewrite_section", subject="Pagination Discipline",
              body="- full rewrite"),
        _edit(kind="add_point", subject="Pagination Discipline",
              body="- p", anchor="- **Mandatory Loop**"),
    ]
    kept, _, _ = syntax_gate(edits, doc)
    violations = detect_conflicts(kept, doc)
    assert any(v.vtype == "section_conflict" for v in violations)


# ── apply ────────────────────────────────────────────────────────────────────

def test_apply_full_batch_deterministic():
    base = _load_rules("appworld_step1")
    edits = [
        _edit(kind="add_point", subject="Pagination Discipline",
              body="- **Explicit Page Limit**: set page_limit high.",
              anchor="- **Accumulate Results**: Store results from all pages"),
        _edit(subject="Authentication and Action Discipline",
              body="- pass access_token explicitly."),
        _edit(subject="Playback API Resolution",
              body="- try apis.spotify.play_music directly.",
              placement="Temporal Context Resolution"),
    ]
    res = apply_edits(base, edits)
    assert res.assertion_failures == []
    doc = RulesDoc.parse(res.text)
    subjects = [s.subject for s in doc.sections]
    assert subjects == [
        "Pagination Discipline",
        "Temporal Context Resolution",
        "Playback API Resolution",
        "Authentication and Action Discipline",
    ]
    assert "Explicit Page Limit" in doc.sections[0].body


def test_apply_anchor_miss_appends_never_noop():
    base = _load_rules("appworld_step1")
    edits = [
        _edit(kind="add_point", subject="Pagination Discipline",
              body="- new bullet.", anchor="THIS ANCHOR DOES NOT EXIST"),
    ]
    res = apply_edits(base, edits)
    assert "- new bullet." in res.text          # content preserved
    assert any(a.fate == "degraded" and "appended content" in a.reason
               for a in res.audits)
    assert res.assertion_failures == []


def test_apply_anchor_whitespace_normalized_match():
    base = "### S\n- alpha   beta\n- gamma\n"
    edits = [_edit(kind="edit_point", subject="S",
                   body="- ALPHA BETA", anchor="- alpha beta")]
    res = apply_edits(base, edits)
    assert "- ALPHA BETA" in res.text and "alpha   beta" not in res.text


def test_apply_resolver_scoped_to_section():
    base = _load_rules("appworld_step1")
    calls = []

    def resolver(subject, body, edit, feedback=""):
        calls.append(subject)
        return body + "\n" + edit.body      # contract-abiding insertion

    edits = [_edit(kind="add_point", subject="Pagination Discipline",
                   body="- resolver added this.", anchor="NO SUCH ANCHOR")]
    res = apply_edits(base, edits, resolver=resolver)
    assert calls == ["Pagination Discipline"]
    assert "- resolver added this." in res.text
    assert res.assertion_failures == []


def test_apply_resolver_hallucination_rejected():
    """A resolver returning unrelated text (dropping the section's existing
    lines / omitting the edit body) must be rejected by the sanity guard,
    degrading to a content-preserving append."""
    base = _load_rules("appworld_step1")

    feedbacks = []

    def bad_resolver(subject, body, edit, feedback=""):
        feedbacks.append(feedback)
        return "completely unrelated hallucinated text"

    edits = [_edit(kind="add_point", subject="Pagination Discipline",
                   body="- the real new rule.", anchor="NO SUCH ANCHOR")]
    res = apply_edits(base, edits, resolver=bad_resolver)
    assert "hallucinated" not in res.text          # rejected
    assert "- the real new rule." in res.text      # content preserved
    assert "**Mandatory Loop**" in res.text        # original body intact
    # The LLM got a second chance WITH the rejection reason before rule
    # fallback took over:
    assert len(feedbacks) == 2 and feedbacks[0] == "" \
        and "rejected" in feedbacks[1]
    assert any(a.fate == "degraded" and "twice" in a.reason
               for a in res.audits)
    assert res.assertion_failures == []


def test_apply_remove_point_idempotent():
    base = "### S\n- keep\n- kill me\n"
    ok = apply_edits(base, [_edit(kind="remove_point", subject="S",
                                  body="", anchor="- kill me")])
    assert "- kill me" not in ok.text and "- keep" in ok.text
    again = apply_edits(ok.text, [_edit(kind="remove_point", subject="S",
                                        body="", anchor="- kill me")])
    assert again.text == ok.text
    assert any(a.fate == "degraded" for a in again.audits)


def test_apply_heading_in_body_demoted():
    base = ""
    edits = [_edit(subject="Topic",
                   body="- a\n### Sneaky Sub Heading\n- b")]
    res = apply_edits(base, edits)
    assert res.assertion_failures == []
    assert "**Sneaky Sub Heading**" in res.text


def test_apply_point_groups_run_concurrently_and_match_sequential():
    """Cross-section point groups with a resolver run concurrently (observed
    via overlapping in-flight windows) and produce the same document as the
    sequential path; within-section order is preserved."""
    import threading
    import time

    base = "\n\n".join(f"### Sec{i}\n- base {i}" for i in range(4)) + "\n"
    in_flight = []
    lock = threading.Lock()
    overlap_seen = []

    def slow_resolver(subject, body, edit, feedback=""):
        with lock:
            in_flight.append(subject)
            if len(in_flight) > 1:
                overlap_seen.append(tuple(in_flight))
        time.sleep(0.05)
        with lock:
            in_flight.remove(subject)
        return body + "\n" + edit.body

    def mk_edits():
        return [
            _edit(kind="add_point", subject=f"Sec{i}",
                  body=f"- resolved {i}", anchor="NO MATCH")
            for i in range(4)
        ]

    t0 = time.time()
    res = apply_edits(base, mk_edits(), resolver=slow_resolver)
    dur = time.time() - t0
    assert res.assertion_failures == []
    for i in range(4):
        assert f"- resolved {i}" in res.text
    assert overlap_seen, "resolver calls never overlapped — not concurrent"
    assert dur < 0.05 * 4, "took as long as sequential"

    seq = apply_edits(base, mk_edits(),
                      resolver=lambda s, b, e, f="": b + "\n" + e.body)
    assert seq.text == res.text          # concurrent == sequential result


def test_apply_same_section_points_stay_ordered_under_resolver():
    base = "### S\n- base\n\n### Other\n- o\n"
    calls = []

    def resolver(subject, body, edit, feedback=""):
        calls.append(edit.body)
        return body + "\n" + edit.body

    edits = [
        _edit(kind="add_point", subject="S", body="- first", anchor="NOPE"),
        _edit(kind="add_point", subject="S", body="- second", anchor="NOPE"),
        _edit(kind="add_point", subject="Other", body="- other", anchor="NOPE"),
    ]
    res = apply_edits(base, edits, resolver=resolver)
    assert res.text.index("- first") < res.text.index("- second")
    assert calls.index("- first") < calls.index("- second")
    assert res.assertion_failures == []


def test_apply_add_when_subject_exists_appends():
    base = "### S\n- old\n"
    res = apply_edits(base, [_edit(subject="S", body="- new")])
    assert res.assertion_failures == []
    assert "- old" in res.text and "- new" in res.text
    doc = RulesDoc.parse(res.text)
    assert len(doc.sections) == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
