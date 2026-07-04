"""Replay of the REAL 2026-07-04 step1 merger accident through the v2
mechanical layer, plus perturbation gap tests.

The accident: the legacy merger LLM emitted 13 edits — 11 of them claiming
``section_target = "### Temporal Context Resolution"`` while their contents
were eight distinct new topics and three misplaced points. The legacy
mechanical validator killed 7 of 13 silently; every one of those carried a
distinct topic (three with 5-6 task support).

Acceptance for v2's deterministic layers on this exact input:
  * zero silent drops;
  * all 8 distinct topics still alive after gate + conflict detection;
  * the collision is DETECTED (violations), not resolved by deletion;
  * the deterministic fallback alone (no LLM at all) still delivers every
    topic's content into the candidate document, structurally valid.
"""
from __future__ import annotations

import json
import os

import pytest

from css.optimizer.editpipe.adjudicate import deterministic_fallback
from css.optimizer.editpipe.apply import apply_edits
from css.optimizer.editpipe.render import RulesDoc, assert_structure
from css.optimizer.editpipe.schema import (
    SectionEdit,
    detect_conflicts,
    syntax_gate,
)

FIX = os.path.join(os.path.dirname(__file__), "..", "..", "tests",
                   "fixtures", "edit_pipeline", "appworld_step1")


def _load():
    with open(os.path.join(FIX, "legacy_merger_response.json"),
              encoding="utf-8") as f:
        obj = json.load(f)
    with open(os.path.join(FIX, "base_rules.md"), encoding="utf-8") as f:
        base = f.read()
    edits = [SectionEdit.from_dict(d) for d in obj["edits"]]
    return base, edits


# Topics the legacy pipeline lost entirely (subject inferred from body since
# the accident wrote the wrong section_target on every one of them).
LOST_TOPIC_MARKERS = [
    "Queue and Stream State",
    "Venmo Transaction Direction",
    "Transaction Description Filtering",
    "Spotify Library Completeness",
    "Playlist Song Verification",
    "Contextual Identity Resolution",
    "Search and Sort Optimization",
]


def test_accident_edits_parse_via_legacy_field_names():
    base, edits = _load()
    assert len(edits) == 13
    # legacy delta_type / section_target / content / point_anchor all mapped:
    kinds = {e.kind for e in edits}
    assert kinds == {"new_section", "point_add"} or \
        kinds == {"add_section", "add_point"}  # pre/post gate normalization


def test_accident_zero_silent_drops_and_topics_survive():
    base, edits = _load()
    doc = RulesDoc.parse(base)
    kept, violations, audits = syntax_gate(edits, doc)

    # Nothing dropped except (possibly) byte-identical duplicates — and the
    # accident's 13 edits are all distinct:
    assert len(kept) == 13
    dropped = [a for a in audits if a.fate == "dropped"]
    assert dropped == []

    conflicts = detect_conflicts(kept, doc)
    vtypes = {v.vtype for v in conflicts}
    # The collision is detected (11 edits claim one subject: 8 adds collide,
    # 3 points sit under a whole-section pileup with them):
    assert "identity_collision" in vtypes

    # Every lost topic's content is still present among kept edits:
    all_bodies = "\n".join(e.body for e in kept)
    for marker in LOST_TOPIC_MARKERS:
        assert marker.split()[0] in all_bodies or marker in all_bodies, marker


def test_accident_pure_deterministic_recovery_no_llm():
    """Gate -> conflicts -> deterministic fallback -> apply. No LLM anywhere.
    All 13 edits' content must reach the candidate; structure must hold."""
    base, edits = _load()
    doc = RulesDoc.parse(base)
    kept, violations, audits = syntax_gate(edits, doc)
    conflicts = detect_conflicts(kept, doc)

    final, accepted = deterministic_fallback(kept, conflicts, audits)
    assert len(final) == 13                       # content count preserved

    res = apply_edits(base, final)
    assert res.assertion_failures == []

    # Body-level signal preservation in the actual candidate document:
    for marker in LOST_TOPIC_MARKERS:
        assert marker.split()[0] in res.text, f"lost topic: {marker}"
    # The original two sections are intact too:
    parsed = RulesDoc.parse(res.text)
    subjects = [s.subject for s in parsed.sections]
    assert "Pagination Discipline" in subjects
    assert "Temporal Context Resolution" in subjects


def test_accident_legacy_shell_candidate_would_fail_assertions():
    """Regression cross-check: the OLD pipeline's actual candidate (with the
    empty-shell duplicate headings) fails v2 structure assertions — i.e. the
    assertions would have caught the real damage."""
    with open(os.path.join(FIX, "candidate_rules.md"), encoding="utf-8") as f:
        legacy_candidate = f.read()
    problems = assert_structure(legacy_candidate)
    assert any("duplicate section heading" in p for p in problems)
    assert any("empty section body" in p for p in problems)


# ── perturbation gaps not covered elsewhere ─────────────────────────────────

def test_gate_handles_body_that_is_all_headings():
    doc = RulesDoc(preamble="", sections=[])
    e = SectionEdit.from_dict({
        "kind": "add_section", "subject": "Weird",
        "body": "### A\n## B\n# C", "target_tasks": ["t"]})
    kept, violations, _ = syntax_gate([e], doc)
    assert len(kept) == 1
    assert any(v.vtype == "body_contains_heading" for v in violations)
    res = apply_edits("", kept)
    assert res.assertion_failures == []          # demoted at apply time
    assert "**B**" in res.text


def test_gate_normalizes_heading_prefixed_subject_and_placement():
    doc = RulesDoc.parse("### Alpha\n- a\n")
    e = SectionEdit.from_dict({
        "kind": "add_section", "subject": "###   New   Topic",
        "body": "- x", "placement": "### Alpha", "target_tasks": ["t"]})
    kept, violations, _ = syntax_gate([e], doc)
    assert kept[0].subject == "New Topic"
    assert kept[0].placement == "Alpha"
    res = apply_edits("### Alpha\n- a\n", kept)
    parsed = RulesDoc.parse(res.text)
    assert [s.subject for s in parsed.sections] == ["Alpha", "New Topic"]


def test_fallback_add_exists_appends_not_clobbers():
    base = "### Alpha\n- original rule\n"
    doc = RulesDoc.parse(base)
    e = SectionEdit.from_dict({
        "kind": "add_section", "subject": "Alpha",
        "body": "- new rule", "target_tasks": ["t"]})
    kept, violations, audits = syntax_gate([e], doc)
    conflicts = detect_conflicts(kept, doc)
    assert any(v.vtype == "add_exists" for v in conflicts)
    final, _ = deterministic_fallback(kept, conflicts, audits)
    res = apply_edits(base, final)
    assert "- original rule" in res.text and "- new rule" in res.text
    assert res.assertion_failures == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
