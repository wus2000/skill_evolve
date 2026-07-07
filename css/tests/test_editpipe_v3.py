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

# ── live-replay regressions (2026-07-07 smoke on real fixtures) ───────────────
def test_resolve_placement_tolerant_forms():
    from css.optimizer.editpipe3.pipeline import _resolve_placement
    doc = RulesDocV3.parse(BASE)
    assert _resolve_placement("S#2", doc) == "S#2"
    assert _resolve_placement("[S#2] Error Handling", doc) == "S#2"
    assert _resolve_placement("Error Handling", doc) == "S#2"
    assert _resolve_placement("error handling", doc) == "S#2"
    assert _resolve_placement("NEW: Fresh Aspect", doc) == "NEW: Fresh Aspect"
    assert _resolve_placement("Nonexistent Section", doc) == ""
    assert _resolve_placement("", doc) == ""


def test_check_draft_rejects_untitled_new():
    from css.optimizer.editpipe3.pipeline import _check_draft
    catalog = RulesDocV3.parse(BASE).handle_map()
    untitled = {"edits": [{"op": "add_section", "section": "NEW",
                           "content": "x", "source_ids": ["E#1"]}],
                "dropped_ids": []}
    v = _check_draft(untitled, ["E#1"], catalog)
    assert any("NEW: <specific title>" in s for s in v)
    titled = {"edits": [{"op": "add_section", "section": "NEW: Titled",
                         "content": "x", "source_ids": ["E#1"]}],
              "dropped_ids": []}
    assert _check_draft(titled, ["E#1"], catalog) == []


def test_apply_new_same_title_fuses_not_stacks():
    from css.optimizer.editpipe3.pipeline import AspectGroup, DraftEdit
    groups = [
        AspectGroup(gid="G#1", member_ids=["E#1"], edits=[
            DraftEdit(op="add_section", section="NEW: Output Discipline",
                      content="Emit plain values.", source_ids=["E#1"])]),
        AspectGroup(gid="G#2", member_ids=["E#2"], edits=[
            DraftEdit(op="add_section", section="NEW: output discipline",
                      content="Prefer terse output.", source_ids=["E#2"])]),
    ]
    applier = {"application_notes": "fused", "unapplied": [],
               "new_section_text": "Emit plain values.\nPrefer terse output."}
    client = _happy_client(applier_obj=applier)
    new_text, deferred = apply_groups(client, BASE, groups)
    assert deferred == []
    doc = RulesDocV3.parse(new_text)
    matches = [s for s in doc.sections
               if s.title.casefold() == "output discipline"]
    assert len(matches) == 1, "same-titled NEW must fuse, not stack"
    assert "Prefer terse output" in matches[0].body


def test_apply_new_naming_existing_title_routes_to_fusion():
    from css.optimizer.editpipe3.pipeline import AspectGroup, DraftEdit
    groups = [AspectGroup(gid="G#1", member_ids=["E#1"], edits=[
        DraftEdit(op="add_section", section="NEW: Error Handling",
                  content="Also retry on 502.", source_ids=["E#1"])])]
    applier = {"application_notes": "fused into the existing section",
               "unapplied": [],
               "new_section_text": "Retry once on transient failures.\n"
                                   "Also retry on 502."}
    new_text, deferred = apply_groups(
        _happy_client(applier_obj=applier), BASE, groups)
    assert deferred == []
    doc = RulesDocV3.parse(new_text)
    titles = [s.title for s in doc.sections]
    assert titles.count("Error Handling") == 1
    assert "502" in doc.sections[titles.index("Error Handling")].body


def test_apply_new_without_title_defers():
    from css.optimizer.editpipe3.pipeline import AspectGroup, DraftEdit
    groups = [AspectGroup(gid="G#1", member_ids=["E#1"], edits=[
        DraftEdit(op="add_section", section="NEW", content="orphan text",
                  source_ids=["E#1"])])]
    new_text, deferred = apply_groups(
        _happy_client(applier_obj=_APPLIER_OK), BASE, groups)
    assert len(deferred) == 1 and "without a title" in deferred[0]["reason"]
    assert RulesDocV3.parse(new_text).serialize() == \
        RulesDocV3.parse(BASE).serialize()


def test_flatten_raw_edits_maps_edit_fields():
    from css.optimizer.exploitation import _flatten_raw_edits
    from css.data.edit import Edit, Patch, RawPatch
    e = Edit(op="append", content="- do X", target="### Data Retrieval",
             subject="Data Retrieval", reason="learned",
             source_tasks=["t1", "t2"])
    rp = RawPatch(patch=Patch(edits=[e]), source_type="failure", batch_size=4)
    (m,) = _flatten_raw_edits([rp])
    assert m["section_target"] == "Data Retrieval", "v2 subject field wins"
    assert m["kind"] == "append"
    assert m["body"] == "- do X"
    assert m["rationale"] == "learned"
    assert m["target_tasks"] == ["t1", "t2"], \
        "source_tasks is the material's task provenance"
    legacy = Edit(op="add_section", content="c", target="### Legacy Anchor")
    (m2,) = _flatten_raw_edits(
        [RawPatch(patch=Patch(edits=[legacy]), source_type="failure",
                  batch_size=1)])
    assert m2["section_target"] == "### Legacy Anchor", "legacy target fallback"


def test_oversize_split_keeps_aspect_and_placement():
    import re as _re

    ids = ["E#%d" % i for i in range(1, 8)]          # 7 > _MAX_GROUP_SIZE (6)
    group_obj = {"groups": [
        {"ids": ids, "aspect": "shared aspect", "placement": "S#1"}]}

    def draft_for(user):
        mid = _re.findall(r"\[(E#\d+)\]", user)[0]
        return {"analysis": "a",
                "edits": [{"op": "append_to_section", "section": "S#1",
                           "content": "- from %s" % mid,
                           "source_ids": [mid], "rationale": "r"}],
                "dropped_ids": []}

    client = _happy_client(group_obj, draft_for, _REVIEW_PASS)
    res = consolidate(client, BASE, [_raw(body="- r%d" % i) for i in range(7)])
    assert len(res.groups) == 7
    assert all(g.aspect == "shared aspect" for g in res.groups), \
        "splinter singletons keep the group's aspect"
    assert all(g.placement == "S#1" for g in res.groups)
