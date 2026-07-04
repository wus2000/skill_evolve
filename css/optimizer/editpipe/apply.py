"""Deterministic application of SectionEdits to rules.md.

Section-level operations are pure text surgery on the parsed document model —
no LLM ever rewrites the whole document. Point-level operations resolve their
anchor through a degradation chain in which every step preserves content:

  1. exact anchor match inside the subject section;
  2. whitespace-normalized match;
  3. an injected ``resolver`` callback (the pipeline passes a section-scoped
     LLM: input = one section + the edit, output = that section's new body —
     blast radius is one section by construction);
  4. content-preserving fallback: add/edit append their body at the section
     end, remove becomes an audited idempotent no-op.

After all edits are applied the document is normalized and checked against
structure assertions; the caller receives everything (text, audits,
assertion failures) and decides what to do — apply never hides an outcome.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

from css.optimizer.editpipe.render import (
    DocSection,
    RulesDoc,
    assert_structure,
    demote_headings,
)
from css.optimizer.editpipe.schema import EditAudit, SectionEdit

_log = logging.getLogger(__name__)

# resolver(section_subject, section_body, edit) -> new body for that section,
# or None if it could not resolve. Injected by the pipeline (LLM-backed) or
# left None for fully-deterministic operation (tests, degraded mode).
Resolver = Callable[[str, str, SectionEdit], Optional[str]]


@dataclass
class ApplyResult:
    text: str
    audits: list[EditAudit] = field(default_factory=list)
    assertion_failures: list[str] = field(default_factory=list)
    normalize_notes: list[str] = field(default_factory=list)


def _norm_ws(s: str) -> str:
    return " ".join((s or "").split())


def _find_anchor_span(body: str, anchor: str) -> tuple[int, int] | None:
    """Locate anchor in body: exact first, then whitespace-normalized."""
    if not (anchor or "").strip():
        return None
    idx = body.find(anchor)
    if idx >= 0:
        return idx, idx + len(anchor)
    # Whitespace-normalized scan, line-window based: try to find a run of
    # lines whose normalized join equals the normalized anchor.
    target = _norm_ws(anchor)
    if not target:
        return None
    lines = body.split("\n")
    # Precompute char offsets of each line start.
    offsets: list[int] = []
    pos = 0
    for ln in lines:
        offsets.append(pos)
        pos += len(ln) + 1
    n = len(lines)
    for i in range(n):
        acc = ""
        for j in range(i, min(n, i + 12)):
            acc = _norm_ws(acc + " " + lines[j]) if acc else _norm_ws(lines[j])
            if acc == target:
                start = offsets[i]
                end = offsets[j] + len(lines[j])
                return start, end
            if len(acc) > len(target) + 16:
                break
    return None


def _sanitize_body(edit_id: str, subject: str, body: str,
                   audits: list[EditAudit]) -> str:
    """Bodies must be heading-free at apply time. If any heading line slipped
    through adjudication, demote it to bold text (content preserved)."""
    new_body, n = demote_headings(body)
    if n:
        audits.append(EditAudit(
            edit_id, subject, "degraded",
            f"demoted {n} heading line(s) inside body to bold text", "apply"))
    return new_body


def apply_edits(
    rules_md: str,
    edits: list[SectionEdit],
    resolver: Resolver | None = None,
) -> ApplyResult:
    """Apply all edits deterministically. Order: whole-section removals,
    rewrites, section additions, then point edits — so a point edit whose
    section is created by the same batch lands on the created section, and
    placements can reference both pre-existing and same-batch sections."""
    doc = RulesDoc.parse(rules_md)
    audits: list[EditAudit] = []

    ids = {id(e): f"E#{i}" for i, e in enumerate(edits)}

    def eid(e: SectionEdit) -> str:
        return ids[id(e)]

    order = {"remove_section": 0, "rewrite_section": 1, "add_section": 2,
             "edit_point": 3, "remove_point": 3, "add_point": 3}
    for e in sorted(edits, key=lambda x: order.get(x.kind, 2)):
        if e.kind == "remove_section":
            idx = doc.index_of(e.subject)
            if idx < 0:
                audits.append(EditAudit(
                    eid(e), e.subject, "degraded",
                    "remove_section target absent at apply time (idempotent "
                    "no-op)", "apply"))
                continue
            del doc.sections[idx]
            audits.append(EditAudit(
                eid(e), e.subject, "kept", "section removed", "apply"))

        elif e.kind == "rewrite_section":
            body = _sanitize_body(eid(e), e.subject, e.body, audits)
            sec = doc.find(e.subject)
            if sec is None:
                doc.sections.append(DocSection(e.subject, body))
                audits.append(EditAudit(
                    eid(e), e.subject, "degraded",
                    "rewrite target absent at apply time; added as new "
                    "section at end", "apply"))
            else:
                sec.body = body
                audits.append(EditAudit(
                    eid(e), e.subject, "kept", "section rewritten", "apply"))

        elif e.kind == "add_section":
            body = _sanitize_body(eid(e), e.subject, e.body, audits)
            existing = doc.find(e.subject)
            if existing is not None:
                # Adjudication should have resolved add_exists; preserve
                # content rather than clobber or drop.
                existing.body = existing.body.rstrip("\n") + "\n" + body \
                    if existing.body.strip() else body
                audits.append(EditAudit(
                    eid(e), e.subject, "degraded",
                    "add_section subject already present at apply time; "
                    "appended body to the existing section", "apply"))
                continue
            new_sec = DocSection(e.subject, body)
            if e.placement == "start":
                doc.sections.insert(0, new_sec)
            elif e.placement in ("", "end"):
                doc.sections.append(new_sec)
            else:
                anchor_idx = doc.index_of(e.placement)
                if anchor_idx < 0:
                    doc.sections.append(new_sec)
                    audits.append(EditAudit(
                        eid(e), e.subject, "degraded",
                        f"placement {e.placement!r} absent at apply time; "
                        "appended at end", "apply"))
                else:
                    doc.sections.insert(anchor_idx + 1, new_sec)
            audits.append(EditAudit(
                eid(e), e.subject, "kept", "section added", "apply"))

        else:  # point ops
            _apply_point(doc, e, eid(e), audits, resolver)

    notes = doc.normalize()
    text = doc.render()
    failures = assert_structure(text)
    return ApplyResult(text, audits, failures, notes)


def _apply_point(
    doc: RulesDoc,
    e: SectionEdit,
    edit_id: str,
    audits: list[EditAudit],
    resolver: Resolver | None,
) -> None:
    sec = doc.find(e.subject)
    if sec is None:
        # Gate/adjudication normally prevents this; preserve content anyway.
        if e.kind in ("add_point", "edit_point"):
            body = _sanitize_body(edit_id, e.subject, e.body, audits)
            doc.sections.append(DocSection(e.subject, body))
            audits.append(EditAudit(
                edit_id, e.subject, "degraded",
                f"{e.kind} target section absent at apply time; created the "
                "section with the edit body", "apply"))
        else:
            audits.append(EditAudit(
                edit_id, e.subject, "degraded",
                "remove_point target section absent (idempotent no-op)",
                "apply"))
        return

    body = sec.body
    span = _find_anchor_span(body, e.anchor)
    content = _sanitize_body(edit_id, e.subject, e.body, audits) \
        if e.kind != "remove_point" else ""

    if span is None and resolver is not None:
        resolved = resolver(sec.subject, body, e)
        if resolved is not None and resolved.strip():
            new_body, n = demote_headings(resolved)
            if n:
                audits.append(EditAudit(
                    edit_id, e.subject, "degraded",
                    f"resolver output contained {n} heading line(s); demoted",
                    "apply"))
            sec.body = new_body.strip("\n")
            audits.append(EditAudit(
                edit_id, e.subject, "kept",
                "anchor resolved semantically within the section", "apply"))
            return
        audits.append(EditAudit(
            edit_id, e.subject, "degraded",
            "semantic resolver could not locate the anchor", "apply"))

    if span is None:
        # Content-preserving fallbacks.
        if e.kind == "remove_point":
            audits.append(EditAudit(
                edit_id, e.subject, "degraded",
                f"anchor {e.anchor[:60]!r} not found; removal treated as "
                "already satisfied (idempotent no-op)", "apply"))
            return
        sec.body = (body.rstrip("\n") + "\n" + content).strip("\n")
        audits.append(EditAudit(
            edit_id, e.subject, "degraded",
            f"anchor {e.anchor[:60]!r} not found; appended content at "
            "section end instead", "apply"))
        return

    start, end = span
    if e.kind == "add_point":
        sec.body = body[:end] + "\n" + content + body[end:]
    elif e.kind == "edit_point":
        sec.body = body[:start] + content + body[end:]
    else:  # remove_point
        sec.body = (body[:start] + body[end:])
    sec.body = sec.body.strip("\n")
    audits.append(EditAudit(
        edit_id, e.subject, "kept", f"{e.kind} applied at anchor", "apply"))
