"""SectionEdit schema, syntax gate, and the disjointness table.

The schema is orthogonal by construction:

  kind      -- what operation (add/rewrite/remove section, add/edit/remove point)
  subject   -- WHICH semantic unit (single source of identity; never a position)
  placement -- WHERE (add_section only; a hint, never identity; may be wrong)
  body      -- content WITHOUT any heading (headings are rendered from subject)
  anchor    -- point ops only: text to locate inside the subject section

The mechanical layer here has exactly three powers and no more:
  1. normalize representations (lossless),
  2. detect problems and emit violations (never resolve them),
  3. drop byte-identical duplicates (the only mechanical kill).
Everything else is adjudicated semantically (see adjudicate.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from css.optimizer.editpipe.render import (
    RulesDoc,
    find_heading_lines,
    normalize_subject,
    strip_redundant_heading,
    subject_key,
)

SECTION_KINDS: tuple[str, ...] = (
    "add_section", "rewrite_section", "remove_section")
POINT_KINDS: tuple[str, ...] = ("add_point", "edit_point", "remove_point")
EDIT_KINDS: tuple[str, ...] = SECTION_KINDS + POINT_KINDS

# Tolerant synonym map: legacy vocabulary and near-miss phrasings a model may
# emit. Mapping is mechanical-lossless (pure renaming); anything not covered
# becomes a violation, never a drop.
_KIND_SYNONYMS = {
    "new_section": "add_section",
    "section_rewrite": "rewrite_section",
    "section_refinement": "rewrite_section",
    "delete_section": "remove_section",
    "point_add": "add_point",
    "point_edit": "edit_point",
    "point_remove": "remove_point",
    "add": "add_section",
    "append": "add_section",
    "insert_section": "add_section",
    "rewrite": "rewrite_section",
    "replace_section": "rewrite_section",
    "remove": "remove_section",
    "insert_after": "add_point",
    "insert_point": "add_point",
    "replace": "edit_point",
    "delete": "remove_point",
}

_PLACEMENT_SPECIALS = ("", "start", "end")


@dataclass
class SectionEdit:
    """One consolidated edit unit (the merger's output vocabulary)."""

    kind: str
    subject: str
    body: str = ""
    placement: str = ""      # add_section only: "" == end | "start" | subject
    anchor: str = ""         # point ops only
    target_tasks: list[str] = field(default_factory=list)
    rationale: str = ""
    derivation: str = ""

    @property
    def key(self) -> str:
        return subject_key(self.subject)

    @property
    def is_point(self) -> bool:
        return self.kind in POINT_KINDS

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "kind": self.kind,
            "subject": self.subject,
            "body": self.body,
            "target_tasks": list(self.target_tasks),
            "rationale": self.rationale,
            "derivation": self.derivation,
        }
        if self.placement:
            d["placement"] = self.placement
        if self.anchor:
            d["anchor"] = self.anchor
        return d

    def to_merged(self):
        """Wire-format adapter for downstream stages that still speak
        MergedEdit (verification, gate, step_buffer, checkpoints).

        The MergedEdit is DERIVED, never authored: its ``content`` for
        section-level ops is mechanically rendered as ``### subject\\n body``
        (single source of identity preserved), and its ``delta_type`` maps
        onto the legacy vocabulary. ``SectionEdit.from_dict`` reads the
        legacy names back, so the round-trip is lossless.
        """
        from css.data.edit import MergedEdit

        kind_map = {
            "add_section": "new_section",
            "rewrite_section": "section_rewrite",
            "remove_section": "delete_section",
            "add_point": "point_add",
            "edit_point": "point_edit",
            "remove_point": "point_remove",
        }
        delta = kind_map.get(self.kind, self.kind)
        if self.kind in ("add_section", "rewrite_section"):
            content = f"### {self.subject}\n{self.body}".rstrip("\n")
        else:
            content = self.body
        after = self.placement
        if self.kind == "add_section":
            after = {"": "_end", "end": "_end", "start": "_start"}.get(
                after, f"### {after}")
        else:
            after = ""
        return MergedEdit(
            section_target=f"### {self.subject}",
            delta_type=delta,
            after_section=after,
            content=content,
            point_anchor=self.anchor,
            target_tasks=list(self.target_tasks),
            rationale=self.rationale,
            derivation=self.derivation,
        )

    @classmethod
    def from_dict(cls, d: dict) -> "SectionEdit":
        """Tolerant construction. Accepts the v2 field names and, for read
        compatibility with legacy MergedEdit data (old checkpoints, old
        merger responses), the legacy names: delta_type/section_target/
        content/point_anchor/after_section."""
        raw_tasks = d.get("target_tasks", [])
        if not isinstance(raw_tasks, list):
            raw_tasks = []
        placement = str(
            d.get("placement", "") or d.get("after_section", "")).strip()
        if placement in ("_end", "_start"):
            placement = placement.lstrip("_")
        return cls(
            kind=str(d.get("kind", "") or d.get("delta_type", "")).strip(),
            subject=normalize_subject(
                str(d.get("subject", "") or d.get("section_target", ""))),
            body=str(d.get("body", "") or d.get("content", "")),
            placement=normalize_subject(placement)
            if placement not in ("", "start", "end") else placement,
            anchor=str(d.get("anchor", "") or d.get("point_anchor", "")),
            target_tasks=[str(t) for t in raw_tasks if t],
            rationale=str(d.get("rationale", "")),
            derivation=str(d.get("derivation", "")),
        )


@dataclass
class EditAudit:
    """One line of the audit trail: what happened to one edit and why.

    ``actor`` says which layer decided: "gate" (mechanical, lossless or
    byte-dup only), "adjudicator" (LLM ruling, carries rationale),
    "fallback" (deterministic convergence fallback), "apply" (application
    stage degradations).
    """

    edit_id: str             # "E#3" style, stable across the adjudication loop
    subject: str
    fate: str                # kept | normalized | converted | merged_into |
                             # renamed | demoted | dropped | degraded
    reason: str
    actor: str

    def to_dict(self) -> dict:
        return {
            "edit_id": self.edit_id,
            "subject": self.subject,
            "fate": self.fate,
            "reason": self.reason,
            "actor": self.actor,
        }


@dataclass
class Violation:
    """A detected problem awaiting semantic adjudication."""

    vtype: str
    edit_ids: list[str]
    detail: str

    def to_dict(self) -> dict:
        return {
            "type": self.vtype,
            "edit_ids": list(self.edit_ids),
            "detail": self.detail,
        }


def _eid(i: int) -> str:
    return f"E#{i}"


def syntax_gate(
    edits: list[SectionEdit],
    doc: RulesDoc,
) -> tuple[list[SectionEdit], list[Violation], list[EditAudit]]:
    """Mechanical pass: normalize losslessly, detect, drop only byte-dups.

    Returns (edits_kept, violations, audits). ``edits_kept`` preserves input
    order and (except byte-identical duplicates) input membership — nothing
    else is removed here, whatever its problems; problems become violations
    for the adjudicator.
    """
    kept: list[SectionEdit] = []
    violations: list[Violation] = []
    audits: list[EditAudit] = []
    seen_bytes: dict[tuple, tuple[str, SectionEdit]] = {}

    for i, e in enumerate(edits):
        eid = _eid(i)

        # -- kind normalization (lossless rename) --------------------------
        k = e.kind.strip().lower()
        if k in _KIND_SYNONYMS:
            mapped = _KIND_SYNONYMS[k]
            if mapped != e.kind:
                audits.append(EditAudit(
                    eid, e.subject, "normalized",
                    f"kind {e.kind!r} -> {mapped!r}", "gate"))
            e.kind = mapped
        elif k in EDIT_KINDS:
            e.kind = k
        else:
            violations.append(Violation(
                "invalid_kind", [eid],
                f"{eid} has unrecognized kind {e.kind!r}; "
                f"valid kinds: {', '.join(EDIT_KINDS)}"))

        # -- required fields ------------------------------------------------
        if not e.subject:
            violations.append(Violation(
                "missing_subject", [eid],
                f"{eid} ({e.kind}) has an empty subject; every edit must name "
                "the section it defines or modifies"))
        if e.kind not in ("remove_section", "remove_point") and not e.body.strip():
            violations.append(Violation(
                "missing_body", [eid],
                f"{eid} ({e.kind}, subject {e.subject!r}) has an empty body"))
        if e.is_point and not e.anchor.strip():
            violations.append(Violation(
                "missing_anchor", [eid],
                f"{eid} ({e.kind}, subject {e.subject!r}) is a point edit "
                "without an anchor"))
        if not e.target_tasks:
            violations.append(Violation(
                "missing_target_tasks", [eid],
                f"{eid} (subject {e.subject!r}) has no target_tasks; set it "
                "to the union of source_tasks of the raw edits it "
                "consolidates"))

        # -- body heading hygiene (lossless strip; the rest is semantic) ----
        stripped = strip_redundant_heading(e.subject, e.body)
        if stripped != e.body:
            audits.append(EditAudit(
                eid, e.subject, "normalized",
                "removed redundant heading line duplicating the subject",
                "gate"))
            e.body = stripped
        headings_in_body = find_heading_lines(e.body)
        if headings_in_body:
            violations.append(Violation(
                "body_contains_heading", [eid],
                f"{eid} (subject {e.subject!r}) has markdown heading line(s) "
                f"inside its body (first: {headings_in_body[0][:60]!r}); a "
                "body must be heading-free (split into separate edits, or "
                "demote the heading to bold text)"))

        # -- placement sanity (hint only — degrade, never violate hard) -----
        if e.kind == "add_section" and e.placement not in _PLACEMENT_SPECIALS:
            if doc.find(e.placement) is None and not any(
                    subject_key(e.placement) == o.key
                    for o in edits if o.kind == "add_section" and o is not e):
                audits.append(EditAudit(
                    eid, e.subject, "degraded",
                    f"placement {e.placement!r} not found; will fall back to "
                    "document end", "gate"))
                e.placement = ""

        # -- executability of targets ---------------------------------------
        in_doc = doc.find(e.subject) is not None
        if e.kind == "rewrite_section" and not in_doc:
            # Lossless mechanical conversion (mirrors the legacy auto-correct).
            e.kind = "add_section"
            audits.append(EditAudit(
                eid, e.subject, "converted",
                "rewrite_section target missing from document; converted to "
                "add_section at end", "gate"))
        elif e.kind == "remove_section" and not in_doc:
            audits.append(EditAudit(
                eid, e.subject, "degraded",
                "remove_section target already absent — idempotent no-op "
                "(no content exists to preserve or delete)", "gate"))
            continue
        elif e.is_point and not in_doc:
            batch_adds = {o.key for o in edits if o.kind == "add_section"}
            if e.key in batch_adds:
                violations.append(Violation(
                    "dependency_on_new", [eid],
                    f"{eid} ({e.kind}) targets section {e.subject!r} that is "
                    "only being created by another edit in this batch; fold "
                    "the point into that edit's body instead"))
            else:
                violations.append(Violation(
                    "missing_target", [eid],
                    f"{eid} ({e.kind}) targets section {e.subject!r} which "
                    "does not exist in the document"))

        # -- byte-identical duplicate: the only mechanical kill --------------
        # (kill applies to CONTENT only; provenance is unioned into the
        # surviving twin so support counting and verification padding see
        # every contributing task)
        sig = (e.kind, e.key, e.body.strip(), e.anchor.strip())
        if sig in seen_bytes:
            keeper_eid, keeper = seen_bytes[sig]
            merged_tasks = [t for t in e.target_tasks
                            if t not in keeper.target_tasks]
            keeper.target_tasks.extend(merged_tasks)
            audits.append(EditAudit(
                eid, e.subject, "dropped",
                f"byte-identical duplicate of {keeper_eid}"
                + (f"; unioned {len(merged_tasks)} target_task(s) into it"
                   if merged_tasks else ""), "gate"))
            continue
        seen_bytes[sig] = (eid, e)

        kept.append(e)

    return kept, violations, audits


# ── Disjointness table ───────────────────────────────────────────────────────
#
# The per-edit ablation stage requires pairwise-disjoint action regions.
# Region of add_section(A)      = the NEW section A (nothing existing).
# Region of rewrite/remove(X)   = the WHOLE existing section X.
# Region of point(X, anchor)    = one location inside section X.
#
# Conflicts (same subject key):
#   add+add               -> identity_collision   (two claims to one identity)
#   add+existing section  -> add_exists           (ambiguous: rewrite? merge?)
#   rewrite/remove + any other on X -> section_conflict (whole-section op eats it)
#   point+point same anchor text    -> anchor_overlap
# NOT conflicts: add(A) + add(B) with A != B, whatever their placements;
# point(X)+point(Y); point+point same section different anchors (semantic
# overlap is the LLM validator's judgement, not ours).

def _norm_lines(text: str) -> set[str]:
    """Non-trivial body lines, whitespace-normalized (for restatement checks)."""
    out: set[str] = set()
    for line in (text or "").split("\n"):
        s = " ".join(line.split()).strip("-* ").casefold()
        if len(s) >= 12:  # ignore separators/short connectives
            out.add(s)
    return out


_RESTATE_MIN_LINES = 3
_RESTATE_RATIO = 0.5
_LINE_JACCARD = 0.7


def _lines_overlap(add_lines: set[str], other_lines: set[str]) -> float:
    """Fraction of ``add_lines`` matched in ``other_lines``.

    A line matches exactly or by token-Jaccard >= 0.7 — catching the
    light-paraphrase restatement ("Mandatory Loop" -> "Mandatory Pagination
    Loop" with an inserted example list) while staying conservative enough
    not to flag genuinely new rules that merely mention the same API names.
    """
    if not add_lines:
        return 0.0
    other_tokens = [set(o.split()) for o in other_lines]
    matched = 0
    for a in add_lines:
        if a in other_lines:
            matched += 1
            continue
        ta = set(a.split())
        if not ta:
            continue
        for tb in other_tokens:
            union = ta | tb
            if union and len(ta & tb) / len(union) >= _LINE_JACCARD:
                matched += 1
                break
    return matched / len(add_lines)


def detect_restatements(
    edits: list[SectionEdit],
    doc: RulesDoc,
) -> list[Violation]:
    """Mechanical detection of near-verbatim restatement.

    An add_section whose body substantially repeats an existing section (or
    an earlier add in the same batch) inflates the document with duplicate
    instructions — the observed failure mode when the merger re-emits
    context it was shown. Line-level overlap is mechanically decidable for
    the near-verbatim case; the semantic validator still owns paraphrase.
    """
    violations: list[Violation] = []
    doc_lines = [(sec.subject, _norm_lines(sec.body)) for sec in doc.sections]
    prior_adds: list[tuple[str, str, set[str]]] = []  # (eid, subject, lines)

    for i, e in enumerate(edits):
        if e.kind != "add_section":
            continue
        lines = _norm_lines(e.body)
        if len(lines) < _RESTATE_MIN_LINES:
            prior_adds.append((_eid(i), e.subject, lines))
            continue
        for subject, sec_lines in doc_lines:
            if not sec_lines:
                continue
            overlap = _lines_overlap(lines, sec_lines)
            if overlap >= _RESTATE_RATIO:
                shared = sorted(lines & sec_lines)[:3]
                evidence = "; ".join(s[:70] for s in shared) or \
                    "(paraphrase-level matches)"
                violations.append(Violation(
                    "restates_existing", [_eid(i)],
                    f"{_eid(i)} (add_section {e.subject!r}) overlaps "
                    f"{overlap:.0%} of the existing section {subject!r} "
                    f"(shared lines include: {evidence}). Judge whether "
                    "this is a restatement or a genuinely new theme that "
                    "merely shares setup steps. If restatement: keep ONLY "
                    f"the new delta as point edits inside {subject!r}. "
                    "Do NOT drop genuinely new material either way."))
                break
        else:
            for peid, psubject, plines in prior_adds:
                if not plines:
                    continue
                overlap = _lines_overlap(lines, plines)
                if overlap >= _RESTATE_RATIO:
                    violations.append(Violation(
                        "restates_sibling", [peid, _eid(i)],
                        f"{_eid(i)} (add_section {e.subject!r}) repeats "
                        f"{overlap:.0%} of {peid} ({psubject!r}) from this "
                        "same batch; merge them into one section"))
                    break
        prior_adds.append((_eid(i), e.subject, lines))

    return violations


def detect_conflicts(
    edits: list[SectionEdit],
    doc: RulesDoc,
) -> list[Violation]:
    violations: list[Violation] = []
    doc_keys = doc.keys()

    by_key: dict[str, list[tuple[str, SectionEdit]]] = {}
    for i, e in enumerate(edits):
        by_key.setdefault(e.key, []).append((_eid(i), e))

    for key, group in by_key.items():
        adds = [(eid, e) for eid, e in group if e.kind == "add_section"]
        wholes = [(eid, e) for eid, e in group
                  if e.kind in ("rewrite_section", "remove_section")]
        points = [(eid, e) for eid, e in group if e.is_point]

        if len(adds) > 1:
            ids = [eid for eid, _ in adds]
            violations.append(Violation(
                "identity_collision", ids,
                "multiple add_section edits claim the same subject "
                f"{adds[0][1].subject!r}; merge them into one section or "
                "rename the ones that are genuinely different topics"))
        if adds and key in doc_keys:
            ids = [eid for eid, _ in adds]
            violations.append(Violation(
                "add_exists", ids,
                f"add_section subject {adds[0][1].subject!r} already exists "
                "in the document; use rewrite_section (full replacement) or "
                "add_point (localized addition) instead"))
        if len(wholes) > 1:
            ids = [eid for eid, _ in wholes]
            violations.append(Violation(
                "section_conflict", ids,
                f"multiple whole-section edits target {wholes[0][1].subject!r}; "
                "at most one rewrite/remove per section"))
        if wholes and (points or adds):
            ids = [eid for eid, _ in wholes + points + adds]
            violations.append(Violation(
                "section_conflict", ids,
                f"section {wholes[0][1].subject!r} has a whole-section edit "
                "plus other edits; a rewrite/remove replaces the entire "
                "section, making the others inapplicable"))
        if len(points) > 1:
            seen_anchor: dict[str, str] = {}
            for eid, e in points:
                a = " ".join(e.anchor.split())
                if a and a in seen_anchor:
                    violations.append(Violation(
                        "anchor_overlap", [seen_anchor[a], eid],
                        f"two point edits on {e.subject!r} share the same "
                        f"anchor text {e.anchor[:60]!r}"))
                elif a:
                    seen_anchor[a] = eid

    return violations
