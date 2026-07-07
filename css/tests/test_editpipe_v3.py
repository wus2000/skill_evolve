"""editpipe v3: DSP docmodel, consolidation stages, protocol repair, apply.

Stub-driven (no network). The load-bearing invariants: roundtrip
parse/serialize, empty-document bootstrap, partition arithmetic with repair
and lossless degradation, rule-side target_tasks inheritance, review routing
(leakage rejects the edit; issues re-draft with counterpart context), and the
Section Applier's adopted-as-is output with the honest unapplied channel.
"""
from __future__ import annotations

import json

from css.model.client import StubLLMClient
from css.optimizer.editpipe3.docmodel import RulesDocV3
from css.optimizer.editpipe3.pipeline import apply_groups, consolidate

BASE = ("### Data Retrieval\nAlways page through listing APIs.\n\n"
        "### Error Handling\nRetry once on transient failures.\n")


def _raw(section="### Data Retrieval", body="- new rule", tasks=("t1",),
         src="P#1", rationale="learned from failures"):
    return {"section_target": section, "kind": "add_point", "body": body,
            "rationale": rationale, "target_tasks": list(tasks), "src": src}


# ── DSP docmodel ─────────────────────────────────────────────────────────────
def test_roundtrip_and_self_healing():
    doc = RulesDocV3.parse(BASE)
    assert [s.title for s in doc.sections] == ["Data Retrieval", "Error Handling"]
    assert RulesDocV3.parse(doc.serialize()).serialize() == doc.serialize()
    # No invalid documents: a stray heading merely splits a section.
    weird = "free preamble\n### A\nbody\n### odd\nmore"
    doc2 = RulesDocV3.parse(weird)
    assert RulesDocV3.parse(doc2.serialize()).serialize() == doc2.serialize()
    # Fenced headings are NOT boundaries.
    fenced = "### A\n```\n### not a heading\n```\ntail"
    doc3 = RulesDocV3.parse(fenced)
    assert len(doc3.sections) == 1
    assert "### not a heading" in doc3.sections[0].body


def test_empty_document_bootstrap():
    doc = RulesDocV3.parse("")
    assert doc.sections == [] and "EMPTY" in doc.render()
    doc.add_section("First Section", "- rule one")
    text = doc.serialize()
    again = RulesDocV3.parse(text)
    assert again.sections[0].title == "First Section"
    assert again.serialize() == text, "first output is already canonical form"


def test_handles_resolve_and_render():
    doc = RulesDocV3.parse(BASE)
    assert doc.resolve("S#1") == 0 and doc.resolve("S#2") == 1
    assert doc.resolve("S#9") is None and doc.resolve("garbage") is None
    assert "[S#1] Data Retrieval" in doc.render()
    assert "[S#2] Error Handling" in doc.catalog()


# ── consolidation: A/B protocol + arithmetic + degradation ───────────────────
def _happy_client(group_obj=None, draft_obj=None, review_obj=None,
                  applier_obj=None, log=None):
    def opt(system, user):
        if log is not None:
            log.append(system.split("\n", 1)[0])
        if "organize a batch of RAW EDITS" in system:
            return json.dumps(group_obj(user) if callable(group_obj) else group_obj)
        if "draft the DEFINITIVE edit" in system:
            return json.dumps(draft_obj(user) if callable(draft_obj) else draft_obj)
        if "final reviewer" in system:
            return json.dumps(review_obj(user) if callable(review_obj) else review_obj)
        if "semantic applier" in system:
            return json.dumps(applier_obj(user) if callable(applier_obj) else applier_obj)
        if "violates its output protocol" in system:
            return "{}"
        return "{}"
    return StubLLMClient(optimizer_fn=opt)


_GROUP_OK = {"groups": [
    {"ids": ["E#1", "E#2"], "aspect": "pagination discipline for listings",
     "placement": "S#1"}]}
_DRAFT_OK = {"analysis": "both raws teach exhaustive pagination",
             "edits": [{"op": "append_to_section", "section": "S#1",
                        "content": "When a listing returns exactly page-size "
                                   "items, request the next page before "
                                   "aggregating.",
                        "source_ids": ["E#1", "E#2"],
                        "rationale": "synthesized both raws"}],
             "dropped_ids": []}
_REVIEW_PASS = {"pass": True, "issues": []}
_APPLIER_OK = {"application_notes": "appended after the paging rule",
               "unapplied": [],
               "new_section_text": "Always page through listing APIs.\n"
                                   "When a listing returns exactly page-size "
                                   "items, request the next page."}


def test_consolidate_happy_path_and_rule_side_target_tasks():
    client = _happy_client(_GROUP_OK, _DRAFT_OK, _REVIEW_PASS)
    raws = [_raw(tasks=("t1", "t2")), _raw(body="- also page", tasks=("t2", "t3"))]
    res = consolidate(client, BASE, raws)
    assert len(res.groups) == 1
    g = res.groups[0]
    assert [e.section for e in g.edits] == ["S#1"]
    # target_tasks = RULE-SIDE union of the sources' tasks, order-preserving.
    assert g.target_tasks == ["t1", "t2", "t3"]
    assert g.edits[0].source_ids == ["E#1", "E#2"]


def test_partition_violation_repaired_then_adopted():
    calls = {"n": 0}

    def group_obj(user):
        calls["n"] += 1
        return {"groups": [{"ids": ["E#1"], "aspect": "a", "placement": "S#1"}]}
        # E#2 missing -> partition violation

    def opt(system, user):
        if "organize a batch of RAW EDITS" in system:
            return json.dumps(group_obj(user))
        if "violates its output protocol" in system:
            assert "E#2" in user, "machine-worded violation names the id"
            return json.dumps({"groups": [
                {"ids": ["E#1"], "aspect": "a", "placement": "S#1"},
                {"ids": ["E#2"], "aspect": "b", "placement": "S#2"}]})
        if "draft the DEFINITIVE edit" in system:
            return json.dumps(_DRAFT_OK)
        if "final reviewer" in system:
            return json.dumps(_REVIEW_PASS)
        return "{}"

    client = StubLLMClient(optimizer_fn=opt)
    res = consolidate(client, BASE, [_raw(), _raw(body="- other")])
    assert len(res.groups) == 2
    assert any(a.get("action") == "protocol_repaired" for a in res.audit)


def test_partition_unrepaired_degrades_to_singletons():
    def opt(system, user):
        if "organize a batch of RAW EDITS" in system:
            return json.dumps({"groups": [{"ids": ["E#99"], "aspect": "x"}]})
        if "violates its output protocol" in system:
            return json.dumps({"groups": [{"ids": ["E#99"], "aspect": "x"}]})
        if "draft the DEFINITIVE edit" in system:
            return json.dumps(_DRAFT_OK)
        if "final reviewer" in system:
            return json.dumps(_REVIEW_PASS)
        return "{}"

    client = StubLLMClient(optimizer_fn=opt)
    res = consolidate(client, BASE, [_raw(), _raw(body="- b2")])
    # Lossless: every raw survives as its own group (fallback drafting may
    # still produce real edits via the DRAFT stage).
    assert sum(len(g.member_ids) for g in res.groups) == 2
    assert any(a.get("action") == "degraded_all_singletons" for a in res.audit)


def test_draft_degradation_is_lossless_raw_passthrough():
    def opt(system, user):
        if "organize a batch of RAW EDITS" in system:
            return json.dumps(_GROUP_OK)
        if "draft the DEFINITIVE edit" in system:
            return json.dumps({"edits": []})       # violates: E#1/E#2 unaccounted
        if "violates its output protocol" in system:
            return json.dumps({"edits": []})       # repair also fails
        if "final reviewer" in system:
            return json.dumps(_REVIEW_PASS)
        return "{}"

    client = StubLLMClient(optimizer_fn=opt)
    res = consolidate(client, BASE, [_raw(), _raw(body="- second lesson")])
    g = res.groups[0]
    assert len(g.edits) == 2, "each member raw survives as a minimal edit"
    assert {e.op for e in g.edits} == {"append_to_section"}
    assert g.target_tasks == ["t1"]


# ── review routing ───────────────────────────────────────────────────────────
def test_leakage_rejects_the_edit_without_revision():
    review = {"pass": False, "issues": [
        {"ids": ["D#1"], "type": "leakage",
         "explanation": "cites task t1's gold value",
         "instruction": "remove it"}]}
    client = _happy_client(_GROUP_OK, _DRAFT_OK, review)
    res = consolidate(client, BASE, [_raw(), _raw(body="- b")])
    assert res.groups == [], "the leaking edit is removed outright"


def test_issue_routes_a_revision_with_counterpart_context():
    group2 = {"groups": [
        {"ids": ["E#1"], "aspect": "paging", "placement": "S#1"},
        {"ids": ["E#2"], "aspect": "retry", "placement": "S#2"}]}
    drafts = {"n": 0}

    def draft_obj(user):
        drafts["n"] += 1
        if "REVISION REQUIRED" in user:
            assert "duplicate" in user and "previous draft" in user.lower()
            return {"analysis": "merged after review",
                    "edits": [{"op": "append_to_section", "section": "S#1",
                               "content": "unified rule", "source_ids":
                               ["E#1", "E#2"], "rationale": "merged"}],
                    "dropped_ids": [], "revision_note": "merged the duplicates"}
        return {"analysis": "a", "edits": [
            {"op": "append_to_section",
             "section": "S#1" if "E#1" in user else "S#2",
             "content": "rule text", "source_ids":
             ["E#1"] if "E#1" in user else ["E#2"], "rationale": "r"}],
            "dropped_ids": []}

    review = {"pass": False, "issues": [
        {"ids": ["D#1", "D#2"], "type": "duplicate",
         "explanation": "both teach the same paging lesson",
         "instruction": "merge them into one rule"}]}
    client = _happy_client(group2, draft_obj, review)
    res = consolidate(client, BASE, [_raw(), _raw(section="### Error Handling",
                                                  body="- retry", tasks=("t9",))])
    all_edits = [e for g in res.groups for e in g.edits]
    assert len(all_edits) == 1 and all_edits[0].content == "unified rule"
    merged_group = [g for g in res.groups if g.edits][0]
    assert merged_group.revision_note == "merged the duplicates"
    assert set(merged_group.member_ids) == {"E#1", "E#2"}
    assert merged_group.target_tasks == ["t1", "t9"]


# ── apply ────────────────────────────────────────────────────────────────────
def test_apply_fuses_per_section_and_adds_new_sections():
    from css.optimizer.editpipe3.pipeline import AspectGroup, DraftEdit
    groups = [
        AspectGroup(gid="G#1", member_ids=["E#1"], edits=[
            DraftEdit(op="append_to_section", section="S#1",
                      content="- page fully", source_ids=["E#1"])]),
        AspectGroup(gid="G#2", member_ids=["E#2"], edits=[
            DraftEdit(op="add_section", section="NEW: Output Discipline",
                      content="Emit final answers as plain values.",
                      source_ids=["E#2"])]),
    ]
    client = _happy_client(applier_obj=_APPLIER_OK)
    new_text, deferred = apply_groups(client, BASE, groups)
    assert deferred == []
    doc = RulesDocV3.parse(new_text)
    titles = [s.title for s in doc.sections]
    assert "Output Discipline" in titles, "NEW sections are mechanical"
    fused = doc.sections[titles.index("Data Retrieval")].body
    assert "request the next page" in fused, "applier output adopted as-is"
    # Untouched section never passed through any LLM.
    assert doc.sections[titles.index("Error Handling")].body == \
        "Retry once on transient failures."


def test_applier_unapplied_channel_defers_honestly():
    from css.optimizer.editpipe3.pipeline import AspectGroup, DraftEdit
    groups = [AspectGroup(gid="G#1", member_ids=["E#1"], edits=[
        DraftEdit(op="append_to_section", section="S#1",
                  content="- misrouted rule", source_ids=["E#1"])])]
    applier = {"application_notes": "does not belong here",
               "unapplied": [{"id": "A#1", "reason": "belongs to Error "
                                                     "Handling, not here"}],
               "new_section_text": "Always page through listing APIs."}
    client = _happy_client(applier_obj=applier)
    new_text, deferred = apply_groups(client, BASE, groups)
    assert len(deferred) == 1 and "belongs to Error Handling" in \
        deferred[0]["reason"]


def test_remove_conflicting_with_same_section_edits_defers():
    from css.optimizer.editpipe3.pipeline import AspectGroup, DraftEdit
    groups = [
        AspectGroup(gid="G#1", member_ids=["E#1"], edits=[
            DraftEdit(op="remove_section", section="S#1",
                      content="obsolete", source_ids=["E#1"])]),
        AspectGroup(gid="G#2", member_ids=["E#2"], edits=[
            DraftEdit(op="append_to_section", section="S#1",
                      content="- new rule", source_ids=["E#2"])]),
    ]
    client = _happy_client(applier_obj=_APPLIER_OK)
    new_text, deferred = apply_groups(client, BASE, groups)
    assert any("removal conflicts" in d["reason"] for d in deferred)
    assert "Data Retrieval" in new_text, "the section survives the deferral"
