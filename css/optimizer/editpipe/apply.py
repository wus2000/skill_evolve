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

# resolver(section_subject, section_body, edit, feedback) -> new body for
# that section, or None if it could not resolve. ``feedback`` is non-empty on
# a retry and explains why the previous attempt was rejected. Injected by the
# pipeline (LLM-backed) or left None for fully-deterministic operation.
Resolver = Callable[[str, str, SectionEdit, str], Optional[str]]


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


def _resolver_output_sane(e: SectionEdit, old_body: str, new_body: str) -> bool:
    """Mechanical guard against resolver hallucination: the output must
    actually reflect the edit's intent on THIS section.

    - add_point/edit_point: the edit's content must appear in the output
      (whitespace-normalized substring), and the output must retain at least
      half of the original body's lines (an unrelated rewrite fails both).
    - remove_point: the output must be a subset-ish of the original (no new
      material) — removal never introduces text.
    """
    norm = _norm_ws
    if e.kind in ("add_point", "edit_point"):
        if e.body.strip() and norm(e.body) not in norm(new_body):
            return False
        old_lines = [ln for ln in
                     (norm(x) for x in old_body.split("\n")) if ln]
        if old_lines:
            new_norm = norm(new_body)
            retained = sum(1 for ln in old_lines if ln in new_norm)
            if e.kind == "add_point" and retained < len(old_lines):
                return False       # an insertion must not lose existing text
            if e.kind == "edit_point":
                # A point edit may replace roughly the anchored text and
                # nothing else: allow at most anchor-size + 1 lines to
                # change, never a wholesale rewrite of the section.
                anchor_lines = max(
                    1, len([ln for ln in e.anchor.split("\n") if ln.strip()]))
                if retained < len(old_lines) - anchor_lines - 1:
                    return False
        return True
    if e.kind == "remove_point":
        new_lines = [ln for ln in
                     (norm(x) for x in new_body.split("\n")) if ln]
        old_norm = norm(old_body)
        return all(ln in old_norm for ln in new_lines)
    return True


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
    """Apply all edits deterministically. Order: rewrites, section
    additions, point edits, then whole-section removals — a point edit whose
    section is created by the same batch lands on the created section,
    placements can reference both pre-existing and same-batch sections, and
    a removal is always the LAST word: if a removal and other edits on the
    same section ever reach apply together (adjudication normally prevents
    it), the outcome is a clean removal — never a deleted section
    resurrected as an empty shell holding only a point edit's delta."""
    doc = RulesDoc.parse(rules_md)
    audits: list[EditAudit] = []

    ids = {id(e): f"E#{i}" for i, e in enumerate(edits)}

    def eid(e: SectionEdit) -> str:
        return ids[id(e)]

    order = {"rewrite_section": 0, "add_section": 1,
             "edit_point": 2, "remove_point": 2, "add_point": 2,
             "remove_section": 3}
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
        # The LLM gets a second attempt WITH the rejection reason before any
        # rule-based degradation — exhaust the decision maker first.
        feedback = ""
        for attempt in (1, 2):
            resolved = resolver(sec.subject, body, e, feedback)
            if resolved is None or not resolved.strip():
                feedback = (
                    "Your previous attempt returned no usable body. Output "
                    "the COMPLETE updated section body as JSON.")
                continue
            if not _resolver_output_sane(e, body, resolved):
                feedback = (
                    "Your previous attempt was rejected by a mechanical "
                    "check: the output must contain the edit's content "
                    "verbatim (for add/edit), must keep the section's "
                    "existing lines (all for add, the majority for edit), "
                    "and must not introduce new material on remove. Apply "
                    "ONLY the requested change to the section body.")
                continue
            new_body, n = demote_headings(resolved)
            if n:
                audits.append(EditAudit(
                    edit_id, e.subject, "degraded",
                    f"resolver output contained {n} heading line(s); demoted",
                    "apply"))
            sec.body = new_body.strip("\n")
            audits.append(EditAudit(
                edit_id, e.subject, "kept",
                "anchor resolved semantically within the section"
                + (" (attempt 2, after feedback)" if attempt == 2 else ""),
                "apply"))
            return
        audits.append(EditAudit(
            edit_id, e.subject, "degraded",
            "semantic resolver failed twice (with feedback on retry); "
            "falling back", "apply"))

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
        # Extend a mid-line anchor match to the end of its line so the
        # insertion never splits a line in two.
        nl = body.find("\n", end)
        end = len(body) if nl < 0 else nl
        sec.body = body[:end] + "\n" + content + body[end:]
    elif e.kind == "edit_point":
        sec.body = body[:start] + content + body[end:]
    else:  # remove_point
        sec.body = (body[:start] + body[end:])
    sec.body = sec.body.strip("\n")
    audits.append(EditAudit(
        edit_id, e.subject, "kept", f"{e.kind} applied at anchor", "apply"))
