"""Golden tests for the byte-exact named-section engine (css.l1gen.sections).

The load-bearing contract: any section an edit plan does not name is reproduced
BYTE-FOR-BYTE — this is what makes a REFINE diff equal to its intervention.
"""
from __future__ import annotations

import pytest

from css.l1gen.sections import (
    SectionEditError, apply_coherence_diff, apply_edit_plan, parse_sections,
    raw_section_text, sections_byte_identical, section_names, split_document,
)

_DOC = (
    "Preamble framing before any section.\n\n"
    "## Ground Every Claim\n"
    "Read before you write. Verify each value against a real read.\n\n"
    "## Decompose Before Committing\n"
    "Break the task into parts, then commit to each in turn.\n\n"
    "## Keep A Ledger\n"
    "Track what you have already done so you never redo it.\n"
)


def test_parse_and_roundtrip_byte_identity():
    assert section_names(_DOC) == [
        "Ground Every Claim", "Decompose Before Committing", "Keep A Ledger"]
    preamble, secs = split_document(_DOC)
    assert preamble == "Preamble framing before any section.\n\n"
    recon = preamble + "".join(_DOC[s.raw_span[0]: s.raw_span[1]] for s in secs)
    assert recon == _DOC   # exact byte round-trip


def test_fenced_hash_is_not_a_heading():
    doc = "## Real\nbody\n\n```\n## not a heading inside a fence\n```\nmore body\n"
    assert section_names(doc) == ["Real"]


@pytest.mark.parametrize("untouched", ["Ground Every Claim", "Keep A Ledger"])
def test_replace_section_keeps_others_byte_identical(untouched):
    ops = [{"op": "replace_section", "section": "Decompose Before Committing",
            "content": "## Decompose Before Committing\nA wholly new mechanism body.\n",
            "rationale": "A-cause"}]
    out = apply_edit_plan(_DOC, ops)
    assert raw_section_text(out, untouched) == raw_section_text(_DOC, untouched)
    assert "A wholly new mechanism body" in out
    assert out.startswith("Preamble framing before any section.")


def test_rewrite_for_adherence_keeps_others_byte_identical():
    ops = [{"op": "rewrite_section_for_adherence", "section": "Ground Every Claim",
            "content": "## Ground Every Claim\nRe-expressed so the agent actually does it.\n",
            "rationale": "B-cause"}]
    out = apply_edit_plan(_DOC, ops)
    ok, offending = sections_byte_identical(
        _DOC, out, ["Decompose Before Committing", "Keep A Ledger"])
    assert ok, offending
    assert raw_section_text(out, "Ground Every Claim") != raw_section_text(_DOC, "Ground Every Claim")


def test_add_section_after_anchor_keeps_all_existing_byte_identical():
    ops = [{"op": "add_section", "section": "Escalate On Repeat Failure",
            "content": "## Escalate On Repeat Failure\nWhen stuck twice, change approach.\n",
            "after": "Decompose Before Committing", "rationale": "A-add"}]
    out = apply_edit_plan(_DOC, ops)
    ok, offending = sections_byte_identical(_DOC, out, section_names(_DOC))
    assert ok, offending
    names = section_names(out)
    assert names == ["Ground Every Claim", "Decompose Before Committing",
                     "Escalate On Repeat Failure", "Keep A Ledger"]


def test_remove_section_keeps_survivors_byte_identical():
    ops = [{"op": "remove_section", "section": "Decompose Before Committing"}]
    out = apply_edit_plan(_DOC, ops)
    assert section_names(out) == ["Ground Every Claim", "Keep A Ledger"]
    ok, offending = sections_byte_identical(_DOC, out, ["Ground Every Claim", "Keep A Ledger"])
    assert ok, offending


def test_unknown_target_hard_error_lists_valid_names():
    with pytest.raises(SectionEditError) as ei:
        apply_edit_plan(_DOC, [{"op": "replace_section", "section": "No Such Section",
                                "content": "## No Such Section\nx"}])
    msg = str(ei.value)
    assert "Ground Every Claim" in msg and "Keep A Ledger" in msg


def test_duplicate_target_hard_error():
    ops = [
        {"op": "replace_section", "section": "Keep A Ledger", "content": "## Keep A Ledger\nA\n"},
        {"op": "rewrite_section_for_adherence", "section": "Keep A Ledger",
         "content": "## Keep A Ledger\nB\n"},
    ]
    with pytest.raises(SectionEditError, match="duplicate edit target"):
        apply_edit_plan(_DOC, ops)


def test_add_existing_name_hard_error():
    with pytest.raises(SectionEditError, match="already exists"):
        apply_edit_plan(_DOC, [{"op": "add_section", "section": "Keep A Ledger",
                                "content": "## Keep A Ledger\nx\n"}])


def test_content_heading_must_match_target():
    with pytest.raises(SectionEditError, match="does not match"):
        apply_edit_plan(_DOC, [{"op": "replace_section", "section": "Keep A Ledger",
                                "content": "## Renamed Ledger\nx\n"}])


def test_no_rewrite_all_op_in_vocabulary():
    with pytest.raises(SectionEditError, match="unknown op"):
        apply_edit_plan(_DOC, [{"op": "rewrite_all", "section": "x", "content": "y"}])


def test_bad_plan_does_not_partially_apply():
    # Second op is invalid -> the whole plan is rejected, first op is NOT applied.
    ops = [
        {"op": "replace_section", "section": "Keep A Ledger", "content": "## Keep A Ledger\nNEW\n"},
        {"op": "replace_section", "section": "Nonexistent", "content": "## Nonexistent\nx"},
    ]
    with pytest.raises(SectionEditError):
        apply_edit_plan(_DOC, ops)


# ── coherence diff ───────────────────────────────────────────────────────────
def test_coherence_diff_applies_inside_modified_section():
    edited = apply_edit_plan(_DOC, [{
        "op": "replace_section", "section": "Decompose Before Committing",
        "content": "## Decompose Before Committing\nSplit, per Ground Every Claim, then commit.\n"}])
    out = apply_coherence_diff(
        edited,
        [{"quoted_old": "per Ground Every Claim", "new": "per the grounding mechanism",
          "reason": "avoid a brittle cross-reference"}],
        modified_sections=["Decompose Before Committing"])
    assert "per the grounding mechanism" in out
    # untouched sections still byte-identical after the coherence pass
    ok, offending = sections_byte_identical(_DOC, out, ["Ground Every Claim", "Keep A Ledger"])
    assert ok, offending


def test_coherence_diff_rejects_touching_unmodified_section():
    with pytest.raises(SectionEditError, match="unmodified"):
        apply_coherence_diff(
            _DOC,
            [{"quoted_old": "Read before you write", "new": "Read first", "reason": "x"}],
            modified_sections=["Decompose Before Committing"])


def test_coherence_diff_requires_unique_match():
    doc = "## A\nrepeat token repeat token\n"
    with pytest.raises(SectionEditError, match="exactly once"):
        apply_coherence_diff(doc, [{"quoted_old": "repeat token", "new": "x"}],
                             modified_sections=["A"])
