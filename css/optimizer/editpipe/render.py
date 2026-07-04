"""Document model for rules.md: parse, render, normalize, assert.

The document is a preamble plus an ordered list of ``### `` sections. Section
identity lives in ``DocSection.subject`` (no ``### `` prefix); the heading
line is *rendered*, never stored, so a section cannot disagree with its own
heading. All functions are deterministic and total (no LLM, no exceptions on
malformed input — malformations are normalized or reported by
``assert_structure``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_WS_RE = re.compile(r"\s+")


def normalize_subject(raw: str) -> str:
    """Canonical display form of a section subject.

    Strips leading markdown heading markers, surrounding whitespace, and
    collapses internal runs of whitespace. Keeps case and punctuation (display
    form). ``"###  Pagination   Discipline "`` -> ``"Pagination Discipline"``.
    """
    s = (raw or "").strip()
    m = _HEADING_RE.match(s)
    if m:
        s = m.group(2).strip()
    return _WS_RE.sub(" ", s)


def subject_key(raw: str) -> str:
    """Equality key for subjects: normalized + casefolded.

    Used for every identity/conflict decision so that trivial drift
    (case, spacing, a stray ``###``) never manufactures a distinct identity.
    """
    return normalize_subject(raw).casefold()


@dataclass
class DocSection:
    """One ``### `` section: identity + body (heading is rendered, not stored)."""

    subject: str
    body: str = ""

    @property
    def key(self) -> str:
        return subject_key(self.subject)

    def render(self) -> str:
        body = self.body.rstrip("\n")
        if body:
            return f"### {self.subject}\n{body}\n"
        return f"### {self.subject}\n"


@dataclass
class RulesDoc:
    """Parsed rules.md: preamble + ordered sections."""

    preamble: str = ""
    sections: list[DocSection] = field(default_factory=list)

    # ── Construction ────────────────────────────────────────────────────────
    @classmethod
    def parse(cls, text: str) -> "RulesDoc":
        """Split on ``### `` heading lines. Deeper/shallower headings inside a
        body stay in that body (only ``###`` delimits sections, matching the
        established rules.md convention)."""
        preamble_lines: list[str] = []
        sections: list[DocSection] = []
        cur: list[str] | None = None
        cur_subject = ""
        for line in (text or "").split("\n"):
            if line.startswith("### "):
                if cur is not None:
                    sections.append(
                        DocSection(cur_subject, "\n".join(cur).strip("\n")))
                cur_subject = normalize_subject(line)
                cur = []
            elif cur is None:
                preamble_lines.append(line)
            else:
                cur.append(line)
        if cur is not None:
            sections.append(DocSection(cur_subject, "\n".join(cur).strip("\n")))
        return cls("\n".join(preamble_lines).strip("\n"), sections)

    # ── Rendering ───────────────────────────────────────────────────────────
    def render(self) -> str:
        parts: list[str] = []
        if self.preamble.strip():
            parts.append(self.preamble.strip("\n") + "\n")
        for sec in self.sections:
            parts.append(sec.render())
        return "\n".join(parts).strip("\n") + "\n" if parts else ""

    # ── Queries ─────────────────────────────────────────────────────────────
    def find(self, subject: str) -> DocSection | None:
        k = subject_key(subject)
        for sec in self.sections:
            if sec.key == k:
                return sec
        return None

    def index_of(self, subject: str) -> int:
        k = subject_key(subject)
        for i, sec in enumerate(self.sections):
            if sec.key == k:
                return i
        return -1

    def subjects(self) -> list[str]:
        return [sec.subject for sec in self.sections]

    def keys(self) -> set[str]:
        return {sec.key for sec in self.sections}

    # ── Normalization ───────────────────────────────────────────────────────
    def normalize(self) -> list[str]:
        """Repair structural damage in place; return audit notes.

        - drop empty-body sections that duplicate a non-empty same-key section
          (the "empty shell heading" artifact);
        - merge duplicate same-key sections (later body appended to first);
        - collapse 3+ blank lines inside bodies.
        Content is never deleted: only empty shells and literal duplication go.
        """
        notes: list[str] = []
        seen: dict[str, int] = {}
        kept: list[DocSection] = []
        for sec in self.sections:
            sec.body = re.sub(r"\n{3,}", "\n\n", sec.body).strip("\n")
            if sec.key in seen:
                first = kept[seen[sec.key]]
                if not sec.body.strip():
                    notes.append(
                        f"dropped empty duplicate heading {sec.subject!r}")
                    continue
                if not first.body.strip():
                    first.body = sec.body
                    notes.append(
                        f"filled empty shell {first.subject!r} from duplicate")
                    continue
                if sec.body.strip() == first.body.strip():
                    notes.append(
                        f"dropped byte-identical duplicate section {sec.subject!r}")
                    continue
                first.body = first.body.rstrip("\n") + "\n" + sec.body
                notes.append(
                    f"merged duplicate section {sec.subject!r} into first occurrence")
                continue
            seen[sec.key] = len(kept)
            kept.append(sec)
        self.sections = kept
        return notes


def find_heading_lines(body: str) -> list[str]:
    """Return the markdown heading lines present in a body (should be none)."""
    return [line for line in (body or "").split("\n") if _HEADING_RE.match(line)]


def demote_headings(body: str) -> tuple[str, int]:
    """Convert any markdown heading lines inside a body to bold lines.

    A section body containing ``### X`` would fracture the document on the
    next parse. Demotion preserves the text while removing the structural
    claim. Returns (new_body, n_demoted).
    """
    out: list[str] = []
    n = 0
    for line in (body or "").split("\n"):
        m = _HEADING_RE.match(line)
        if m:
            out.append(f"**{m.group(2).strip()}**")
            n += 1
        else:
            out.append(line)
    return "\n".join(out), n


def strip_redundant_heading(subject: str, body: str) -> str:
    """If the body's first non-blank line is a heading equivalent to
    ``subject``, remove it (pure redundancy, zero information)."""
    lines = (body or "").split("\n")
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        m = _HEADING_RE.match(line)
        if m and subject_key(m.group(2)) == subject_key(subject):
            return "\n".join(lines[:i] + lines[i + 1:]).strip("\n")
        break
    return body


def assert_structure(
    text: str,
    expected_subjects: list[str] | None = None,
) -> list[str]:
    """Deterministic document invariants; returns a list of violations
    (empty == healthy). Never raises.

    - no duplicate section keys;
    - no empty section bodies;
    - no heading lines inside section bodies;
    - if ``expected_subjects`` is given, every one must be present.
    """
    problems: list[str] = []
    doc = RulesDoc.parse(text)
    seen: set[str] = set()
    for sec in doc.sections:
        if sec.key in seen:
            problems.append(f"duplicate section heading: {sec.subject!r}")
        seen.add(sec.key)
        if not sec.body.strip():
            problems.append(f"empty section body: {sec.subject!r}")
        for line in sec.body.split("\n"):
            if _HEADING_RE.match(line):
                problems.append(
                    f"heading line inside body of {sec.subject!r}: {line[:60]!r}")
                break
    if expected_subjects:
        for subj in expected_subjects:
            if subject_key(subj) not in seen:
                problems.append(f"expected section missing: {subj!r}")
    return problems
