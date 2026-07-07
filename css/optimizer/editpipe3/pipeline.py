"""editpipe v3 orchestration: GROUP -> DRAFT -> REVIEW(-> revision) -> APPLY.

Design: docs/editpipe_v3_design.md. Single-direction flow, no adversarial
loop. Every LLM call has a single decision dimension and a structurally
bounded output; rule code touches only its own handles and arithmetic; every
protocol violation gets one machine-worded repair attempt and a predefined
LOSSLESS degradation. Raw edits are drafting MATERIAL (v2 schema accepted
verbatim); the drafted edits are DSP operations addressed by section handle.
"""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Optional

from css.model.json_repair import complete_optimizer_json
from css.optimizer.editpipe3 import prompts
from css.optimizer.editpipe3.docmodel import RulesDocV3

_log = logging.getLogger("css.optimizer.editpipe3")

_GROUP_MAX_TOKENS = 8192
_DRAFT_MAX_TOKENS = 16384
_REVIEW_MAX_TOKENS = 8192
_APPLIER_MAX_TOKENS = 16384
_REPAIR_MAX_TOKENS = 16384
_MAX_GROUP_SIZE = 6

_OPS = frozenset({"add_section", "append_to_section", "replace_section",
                  "remove_section"})


@dataclass
class DraftEdit:
    op: str
    section: str                    # "S#k" or "NEW: <title>"
    content: str
    source_ids: "list[str]" = field(default_factory=list)
    rationale: str = ""

    def to_dict(self) -> dict:
        return {"op": self.op, "section": self.section, "content": self.content,
                "source_ids": self.source_ids, "rationale": self.rationale}


@dataclass
class AspectGroup:
    gid: str
    member_ids: "list[str]"
    aspect: str = ""
    placement: str = ""
    edits: "list[DraftEdit]" = field(default_factory=list)
    dropped: "list[dict]" = field(default_factory=list)
    target_tasks: "list[str]" = field(default_factory=list)
    revision_note: str = ""

    def to_dict(self) -> dict:
        return {"gid": self.gid, "member_ids": self.member_ids,
                "aspect": self.aspect, "placement": self.placement,
                "edits": [e.to_dict() for e in self.edits],
                "dropped": self.dropped, "target_tasks": self.target_tasks,
                "revision_note": self.revision_note}


@dataclass
class ConsolidationResult:
    groups: "list[AspectGroup]" = field(default_factory=list)
    deferred_groups: "list[AspectGroup]" = field(default_factory=list)
    audit: "list[dict]" = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"groups": [g.to_dict() for g in self.groups],
                "deferred_groups": [g.to_dict() for g in self.deferred_groups],
                "audit": self.audit}


# ── shared helpers ────────────────────────────────────────────────────────────
def _parse_obj(text: str) -> Any:
    if not text:
        return None
    import re
    for cand in ([text.strip()] + re.findall(r"\{.*\}", text, re.DOTALL)):
        try:
            obj = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _call_json(client: Any, system: str, user: str, *, ok, stage: str,
               max_tokens: int) -> Any:
    return complete_optimizer_json(
        client, system, user, parse=_parse_obj, ok=ok,
        max_tokens=max_tokens, stage=stage)


def _protocol_repair(client: Any, task_summary: str, protocol_text: str,
                     raw_obj: Any, violations: "list[str]", *, ok,
                     stage: str) -> Any:
    """One machine-worded repair attempt (structure/references only)."""
    user = prompts.build_protocol_repair_user(
        task_summary, protocol_text,
        json.dumps(raw_obj, ensure_ascii=False, indent=1), violations)
    return _call_json(client, prompts.PROTOCOL_REPAIR_SYSTEM, user,
                      ok=ok, stage=stage + "_protocol_repair",
                      max_tokens=_REPAIR_MAX_TOKENS)


def _render_raw(rid: str, raw: dict) -> str:
    """Full-content rendering of one raw edit (rich, never truncated)."""
    tasks = ", ".join(str(t) for t in (raw.get("target_tasks") or []))
    return (
        "[%s] section_hint=%s | kind=%s | tasks=%s | src=%s\n"
        "rationale: %s\n"
        "content:\n%s"
        % (rid, raw.get("section_target", "(none)"), raw.get("kind", "?"),
           tasks or "(none)", raw.get("src", "?"),
           str(raw.get("rationale", "") or "(none)"),
           str(raw.get("body", "") or "(empty)"))
    )


# ── Stage A: GROUP ────────────────────────────────────────────────────────────
def _check_partition(obj: Any, all_ids: "list[str]") -> "list[str]":
    violations: "list[str]" = []
    if not isinstance(obj, dict) or not isinstance(obj.get("groups"), list):
        return ["the output must be an object with a \"groups\" list"]
    seen: "dict[str, int]" = {}
    known = set(all_ids)
    for gi, g in enumerate(obj["groups"]):
        if not isinstance(g, dict):
            violations.append("groups[%d] is not an object" % gi)
            continue
        ids = g.get("ids")
        if not isinstance(ids, list) or not ids:
            violations.append("groups[%d] has no non-empty \"ids\" list" % gi)
            continue
        for rid in ids:
            rid = str(rid)
            if rid not in known:
                violations.append(
                    "groups[%d] references unknown id %s (known: %s..%s)"
                    % (gi, rid, all_ids[0], all_ids[-1]))
            seen[rid] = seen.get(rid, 0) + 1
    dupes = sorted(r for r, n in seen.items() if n > 1)
    if dupes:
        violations.append(
            "ids appear in MORE than one group (the partition must cover "
            "every id exactly once): %s" % ", ".join(dupes))
    missing = sorted(set(all_ids) - set(seen))
    if missing:
        violations.append(
            "ids appear in NO group (the partition must cover every id "
            "exactly once): %s" % ", ".join(missing))
    return violations


def _stage_a_group(client: Any, doc: RulesDocV3, raws: "dict[str, dict]",
                   audit: "list[dict]") -> "list[AspectGroup]":
    all_ids = sorted(raws, key=lambda r: int(r.split("#")[1]))
    raw_render = "\n\n".join(_render_raw(rid, raws[rid]) for rid in all_ids)
    user = prompts.build_group_user(raw_render, doc.catalog())
    obj = _call_json(client, prompts.GROUP_SYSTEM, user,
                     ok=lambda r: isinstance(r, dict) and "groups" in r,
                     stage="ep3_group", max_tokens=_GROUP_MAX_TOKENS)
    violations = _check_partition(obj, all_ids)
    if violations:
        repaired = _protocol_repair(
            client, "Partition raw edits into orthogonal change-aspect groups.",
            "groups: [{ids, aspect, placement}]; every id exactly once.",
            obj, violations,
            ok=lambda r: isinstance(r, dict) and "groups" in r,
            stage="ep3_group")
        if repaired is not None and not _check_partition(repaired, all_ids):
            obj = repaired
            audit.append({"stage": "A", "action": "protocol_repaired",
                          "violations": violations})
        else:
            audit.append({"stage": "A", "action": "degraded_all_singletons",
                          "violations": violations})
            return [AspectGroup(gid="G#%d" % (i + 1), member_ids=[rid])
                    for i, rid in enumerate(all_ids)]

    groups: "list[AspectGroup]" = []
    catalog = doc.handle_map()
    assigned: set = set()
    for g in obj["groups"]:
        ids = [str(r) for r in g.get("ids", []) if str(r) in raws
               and str(r) not in assigned]
        if not ids:
            continue
        assigned.update(ids)
        placement = str(g.get("placement", "") or "").strip()
        if placement and not placement.upper().startswith("NEW") \
                and placement not in catalog:
            audit.append({"stage": "A", "action": "placement_echo_failed",
                          "placement": placement, "ids": ids})
            placement = ""            # B decides
        if len(ids) > _MAX_GROUP_SIZE:
            audit.append({"stage": "A", "action": "oversize_group_split",
                          "ids": ids})
            for rid in ids:
                groups.append(AspectGroup(gid="", member_ids=[rid]))
            continue
        groups.append(AspectGroup(
            gid="", member_ids=ids,
            aspect=str(g.get("aspect", "") or ""), placement=placement))
    for rid in all_ids:                      # arithmetic completeness backstop
        if rid not in assigned:
            groups.append(AspectGroup(gid="", member_ids=[rid]))
    for i, g in enumerate(groups):
        g.gid = "G#%d" % (i + 1)
    return groups


# ── Stage B: DRAFT ────────────────────────────────────────────────────────────
def _check_draft(obj: Any, member_ids: "list[str]",
                 catalog: "dict[str, int]") -> "list[str]":
    violations: "list[str]" = []
    if not isinstance(obj, dict) or not isinstance(obj.get("edits"), list):
        return ["the output must be an object with an \"edits\" list"]
    accounted: set = set()
    members = set(member_ids)
    for ei, e in enumerate(obj["edits"]):
        if not isinstance(e, dict):
            violations.append("edits[%d] is not an object" % ei)
            continue
        op = str(e.get("op", "")).strip()
        if op not in _OPS:
            violations.append(
                "edits[%d].op %r is not one of %s"
                % (ei, op, ", ".join(sorted(_OPS))))
        section = str(e.get("section", "") or "").strip()
        if not (section.upper().startswith("NEW") or section in catalog):
            violations.append(
                "edits[%d].section %r is neither a valid handle from the "
                "catalog nor 'NEW: <title>'" % (ei, section))
        if not str(e.get("content", "") or "").strip():
            violations.append("edits[%d].content is empty" % ei)
        srcs = [str(s) for s in (e.get("source_ids") or [])]
        bad = [s for s in srcs if s not in members]
        if bad:
            violations.append(
                "edits[%d].source_ids reference non-member id(s): %s"
                % (ei, ", ".join(bad)))
        accounted.update(s for s in srcs if s in members)
    if len(obj["edits"]) > len(member_ids):
        violations.append(
            "%d edits exceed the group's %d member(s) — draft at most one "
            "edit per member" % (len(obj["edits"]), len(member_ids)))
    for d in obj.get("dropped_ids") or []:
        if isinstance(d, dict) and str(d.get("id", "")) in members:
            accounted.add(str(d["id"]))
    unaccounted = sorted(members - accounted)
    if unaccounted:
        violations.append(
            "member id(s) accounted for by NO edit's source_ids and NO "
            "dropped_ids entry: %s — absorb each into an edit or drop it "
            "with a reason" % ", ".join(unaccounted))
    return violations


def _fallback_edits(group: AspectGroup, raws: "dict[str, dict]",
                    doc: RulesDocV3) -> "list[DraftEdit]":
    """Lossless degradation: each member raw becomes its own minimal edit."""
    title_to_handle = {s.title.strip().lstrip("# ").strip(): doc.handle(i)
                       for i, s in enumerate(doc.sections)}
    out: "list[DraftEdit]" = []
    for rid in group.member_ids:
        raw = raws[rid]
        hint = str(raw.get("section_target", "") or "").lstrip("# ").strip()
        handle = title_to_handle.get(hint)
        section = handle if handle else "NEW: %s" % (hint or "Additional Rules")
        out.append(DraftEdit(
            op="append_to_section" if handle else "add_section",
            section=section,
            content=str(raw.get("body", "") or ""),
            source_ids=[rid],
            rationale="fallback pass-through of the raw edit (draft stage "
                      "degraded)"))
    return [e for e in out if e.content.strip()]


def _adopt_draft(group: AspectGroup, obj: dict, raws: "dict[str, dict]") -> None:
    group.edits = []
    for e in obj.get("edits", []):
        if not isinstance(e, dict):
            continue
        srcs = [str(s) for s in (e.get("source_ids") or [])
                if str(s) in set(group.member_ids)]
        group.edits.append(DraftEdit(
            op=str(e.get("op", "")).strip(),
            section=str(e.get("section", "") or "").strip(),
            content=str(e.get("content", "") or ""),
            source_ids=srcs,
            rationale=str(e.get("rationale", "") or "")))
    group.dropped = [d for d in (obj.get("dropped_ids") or [])
                     if isinstance(d, dict)]
    group.revision_note = str(obj.get("revision_note", "") or "")
    # target_tasks: RULE-SIDE union of the sources' tasks (LLM never writes it).
    tasks: "list[str]" = []
    for rid in group.member_ids:
        for t in raws[rid].get("target_tasks") or []:
            if str(t) not in tasks:
                tasks.append(str(t))
    group.target_tasks = tasks


def _stage_b_draft_one(client: Any, doc: RulesDocV3, group: AspectGroup,
                       raws: "dict[str, dict]", audit: "list[dict]",
                       revision_block: str = "") -> None:
    members_render = "\n\n".join(_render_raw(rid, raws[rid])
                                 for rid in group.member_ids)
    idx = doc.resolve(group.placement) if group.placement else None
    if idx is not None:
        section_render = "### [%s] %s\n%s" % (
            doc.handle(idx), doc.sections[idx].title, doc.sections[idx].body)
    else:
        section_render = doc.render()
    user = prompts.build_draft_user(
        group.aspect or "(single raw edit — polish it into deployable form)",
        group.placement, members_render, section_render, revision_block)
    obj = _call_json(client, prompts.DRAFT_SYSTEM, user,
                     ok=lambda r: isinstance(r, dict) and "edits" in r,
                     stage="ep3_draft", max_tokens=_DRAFT_MAX_TOKENS)
    catalog = doc.handle_map()
    violations = _check_draft(obj, group.member_ids, catalog)
    if violations:
        repaired = _protocol_repair(
            client, "Draft the definitive edit(s) for one change-aspect.",
            "edits: [{op, section, content, source_ids, rationale}] + "
            "dropped_ids; union(source_ids)+dropped = group members.",
            obj, violations,
            ok=lambda r: isinstance(r, dict) and "edits" in r,
            stage="ep3_draft")
        if repaired is not None and not _check_draft(
                repaired, group.member_ids, catalog):
            obj = repaired
            audit.append({"stage": "B", "gid": group.gid,
                          "action": "protocol_repaired",
                          "violations": violations})
        else:
            audit.append({"stage": "B", "gid": group.gid,
                          "action": "degraded_raw_passthrough",
                          "violations": violations})
            group.edits = _fallback_edits(group, raws, doc)
            tasks: "list[str]" = []
            for rid in group.member_ids:
                for t in raws[rid].get("target_tasks") or []:
                    if str(t) not in tasks:
                        tasks.append(str(t))
            group.target_tasks = tasks
            return
    _adopt_draft(group, obj, raws)


# ── Stage C: REVIEW + revision routing ────────────────────────────────────────
def _render_drafts(groups: "list[AspectGroup]") -> "tuple[str, dict]":
    """Render all drafted edits with D#n handles; returns (text, D# -> (gi, ei))."""
    lines: "list[str]" = []
    index: "dict[str, tuple]" = {}
    n = 0
    for gi, g in enumerate(groups):
        for ei, e in enumerate(g.edits):
            n += 1
            did = "D#%d" % n
            index[did] = (gi, ei)
            lines.append(
                "[%s] (aspect group %s) op=%s section=%s\nrationale: %s\n"
                "content:\n%s"
                % (did, g.gid, e.op, e.section, e.rationale or "(none)",
                   e.content))
    return "\n\n".join(lines), index


def _stage_c_review(client: Any, doc: RulesDocV3,
                    groups: "list[AspectGroup]",
                    raws: "dict[str, dict]",
                    audit: "list[dict]") -> "list[AspectGroup]":
    drafts_render, index = _render_drafts(groups)
    if not index:
        return []
    user = prompts.build_review_user(drafts_render, doc.render())
    obj = _call_json(client, prompts.REVIEW_SYSTEM, user,
                     ok=lambda r: isinstance(r, dict) and "pass" in r,
                     stage="ep3_review", max_tokens=_REVIEW_MAX_TOKENS)
    if not isinstance(obj, dict):
        audit.append({"stage": "C", "action": "review_unparseable_pass"})
        return []
    issues = [i for i in (obj.get("issues") or []) if isinstance(i, dict)]
    if not issues:
        return []

    deferred: "list[AspectGroup]" = []
    revision_batches: "dict[tuple, list[dict]]" = {}
    for issue in issues:
        itype = str(issue.get("type", "")).strip()
        gids = sorted({index[str(d)][0] for d in (issue.get("ids") or [])
                       if str(d) in index})
        if not gids:
            audit.append({"stage": "C", "action": "issue_ids_unresolvable",
                          "issue": issue})
            continue
        if itype == "leakage":
            # No downstream backstop: the offending edits are REMOVED (the
            # raw material survives and re-proposes next step).
            for did in issue.get("ids") or []:
                if str(did) in index:
                    gi, ei = index[str(did)]
                    if ei < len(groups[gi].edits):
                        groups[gi].edits[ei] = None  # type: ignore[call-overload]
            audit.append({"stage": "C", "action": "leakage_edit_rejected",
                          "issue": issue})
            continue
        revision_batches.setdefault(tuple(gids), []).append(issue)

    for g in groups:
        g.edits = [e for e in g.edits if e is not None]

    # Revision x1: the involved groups re-draft together with full context.
    for gids, batch in revision_batches.items():
        involved = [groups[gi] for gi in gids]
        merged = AspectGroup(
            gid="+".join(g.gid for g in involved),
            member_ids=[rid for g in involved for rid in g.member_ids],
            aspect="\n\n".join(g.aspect for g in involved if g.aspect),
            placement=involved[0].placement)
        issue_text = "\n\n".join(
            "[%s] %s\nINSTRUCTION: %s"
            % (i.get("type"), i.get("explanation", ""),
               i.get("instruction", "")) for i in batch)
        own = json.dumps([g.to_dict() for g in involved],
                         ensure_ascii=False, indent=1)
        block = prompts.build_revision_block(own, issue_text, "")
        _stage_b_draft_one(client, doc, merged, raws, audit,
                           revision_block=block)
        if merged.edits:
            for gi in gids:
                groups[gi].edits = []
            involved[0].edits = merged.edits
            involved[0].dropped += merged.dropped
            involved[0].target_tasks = merged.target_tasks
            involved[0].revision_note = merged.revision_note
            involved[0].member_ids = merged.member_ids
            for gi in gids[1:]:
                groups[gi].member_ids = []
            audit.append({"stage": "C", "action": "revised",
                          "gids": [groups[gi].gid for gi in gids],
                          "issues": batch})
        else:
            audit.append({"stage": "C", "action": "revision_failed_kept",
                          "gids": [groups[gi].gid for gi in gids]})
    return deferred


# ── APPLY: per-section semantic fusion ────────────────────────────────────────
def apply_groups(client: Any, rules_md: str, groups: "list[AspectGroup]",
                 audit: "Optional[list]" = None) -> "tuple[str, list[dict]]":
    """Apply the groups' edits to ``rules_md``; returns (new_text, deferred).

    Rule code owns structure: NEW sections and removals are mechanical; every
    touched EXISTING section gets ONE Section Applier call (LLM fusion; its
    output is adopted as-is — no apply-side guards, per user ruling). Edits
    the applier returns as ``unapplied`` are deferred with its reason.
    """
    audit = audit if audit is not None else []
    doc = RulesDocV3.parse(rules_md)
    catalog = doc.handle_map()
    deferred: "list[dict]" = []

    by_section: "dict[int, list[tuple[str, DraftEdit]]]" = {}
    removals: "dict[int, DraftEdit]" = {}
    n = 0
    for g in groups:
        for e in g.edits:
            n += 1
            aid = "A#%d" % n
            if e.op == "add_section" or e.section.upper().startswith("NEW"):
                title = e.section.split(":", 1)[1].strip() \
                    if ":" in e.section else "Additional Rules"
                doc.add_section(title, e.content)
                audit.append({"apply": aid, "action": "add_section",
                              "title": title, "gid": g.gid})
                continue
            idx = doc.resolve(e.section) if e.section in catalog else None
            if idx is None:
                deferred.append({"id": aid, "gid": g.gid,
                                 "edit": e.to_dict(),
                                 "reason": "section handle no longer resolves"})
                continue
            if e.op == "remove_section":
                removals[idx] = e
            else:
                by_section.setdefault(idx, []).append((aid, e))

    # Removals are mechanical, but a removal conflicting with same-section
    # edits defers (deleting content others are building on needs a ruling).
    for idx in sorted(removals, reverse=True):
        if idx in by_section:
            deferred.append({"id": "remove:%d" % idx,
                             "edit": removals[idx].to_dict(),
                             "reason": "removal conflicts with same-section "
                                       "edits this step; deferred"})
            continue
        audit.append({"apply": "remove_section", "title":
                      doc.sections[idx].title})
        doc.remove_section(idx)
        by_section = {(i - 1 if i > idx else i): v
                      for i, v in by_section.items()}

    def _fuse(idx: int, entries: "list[tuple[str, DraftEdit]]"):
        section = doc.sections[idx]
        edits_render = "\n\n".join(
            "[%s] op=%s\nintended content:\n%s\n(rationale: %s)"
            % (aid, e.op, e.content, e.rationale or "none")
            for aid, e in entries)
        user = prompts.build_applier_user(section.title, section.body,
                                          edits_render)
        obj = _call_json(client, prompts.APPLIER_SYSTEM, user,
                         ok=lambda r: isinstance(r, dict)
                         and "new_section_text" in r,
                         stage="ep3_applier", max_tokens=_APPLIER_MAX_TOKENS)
        return idx, entries, obj

    results = []
    if by_section:
        with ThreadPoolExecutor(max_workers=min(8, len(by_section))) as pool:
            futs = [pool.submit(_fuse, idx, entries)
                    for idx, entries in by_section.items()]
            for fut in as_completed(futs):
                results.append(fut.result())

    for idx, entries, obj in results:
        if not isinstance(obj, dict) or not str(
                obj.get("new_section_text", "") or "").strip():
            for aid, e in entries:
                deferred.append({"id": aid, "edit": e.to_dict(),
                                 "reason": "applier produced no section text"})
            audit.append({"apply": "section_fusion_failed",
                          "section": doc.sections[idx].title})
            continue
        unapplied_ids = {str(u.get("id", "")) for u in
                         (obj.get("unapplied") or []) if isinstance(u, dict)}
        for aid, e in entries:
            if aid in unapplied_ids:
                reason = next((str(u.get("reason", "")) for u in
                               obj.get("unapplied", [])
                               if isinstance(u, dict)
                               and str(u.get("id", "")) == aid),
                              "unapplied by the applier")
                deferred.append({"id": aid, "edit": e.to_dict(),
                                 "reason": reason})
        doc.replace_body(idx, str(obj["new_section_text"]))
        audit.append({"apply": "section_fused",
                      "section": doc.sections[idx].title,
                      "edits": [aid for aid, _ in entries],
                      "unapplied": sorted(unapplied_ids),
                      "notes": str(obj.get("application_notes", ""))})

    return doc.serialize(), deferred


# ── entry: consolidate ────────────────────────────────────────────────────────
def consolidate(client: Any, rules_md: str, raw_edits: "list[dict]",
                cfg: Any = None, max_workers: int = 8) -> ConsolidationResult:
    """GROUP -> DRAFT (parallel) -> REVIEW (-> revision x1).

    ``raw_edits``: v2-schema material dicts ({section_target, kind, body,
    rationale, target_tasks, src}). Returns aspect groups whose edits are
    DSP operations, each group carrying its rule-side target_tasks union.
    """
    res = ConsolidationResult()
    raws = {"E#%d" % (i + 1): dict(r) for i, r in enumerate(raw_edits)}
    if not raws:
        return res
    doc = RulesDocV3.parse(rules_md)

    groups = _stage_a_group(client, doc, raws, res.audit)

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        futs = {pool.submit(_stage_b_draft_one, client, doc, g, raws,
                            res.audit): g for g in groups}
        for fut in as_completed(futs):
            g = futs[fut]
            try:
                fut.result()
            except Exception:  # noqa: BLE001 — degrade that group, keep going
                _log.exception("editpipe3: draft failed for %s", g.gid)
                g.edits = _fallback_edits(g, raws, doc)

    _stage_c_review(client, doc, groups, raws, res.audit)

    res.groups = [g for g in groups if g.edits]
    n_edits = sum(len(g.edits) for g in res.groups)
    _log.info("editpipe3: %d raw -> %d aspect group(s) -> %d drafted edit(s)",
              len(raws), len(res.groups), n_edits)
    return res
