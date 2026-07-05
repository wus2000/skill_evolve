"""Named-section engine for L1 strategy documents (design §4.2).

A strategy document is organized as a sequence of ``## <behavioral mechanism>``
sections, optionally preceded by a short preamble before the first ``##``. This
module is the single authority for parsing that structure and for applying
CONTROLLED edits to it.

The load-bearing guarantee: any section an edit plan does NOT name is reproduced
BYTE-FOR-BYTE in the output. A parent-vs-child diff is therefore exactly the
intervention — the design's controlled-experiment property (§4.2: "diff ==
intervention"), which lets lineage analysis read off which mechanism change moved
the basin. A full-document rewrite op deliberately does NOT exist in the
vocabulary: a whole-paradigm change is the root's NEW job, not a REFINE.

Byte-identity is achieved by never touching the original substring of an
untouched section. We slice the document into the preamble plus one raw substring
per section (heading through the byte just before the next heading); an edit only
replaces / drops / inserts whole blocks, so every surviving block is the exact
original bytes.

The fenced-code handling (``## `` inside a code fence is code, not a heading) is
ported from :mod:`css.markdown_utils` (``_fenced_spans``); we keep a
self-contained copy here because this engine's byte-exactness is a hard contract
and must not silently drift with that module.
"""
from __future__ import annotations

import re
from typing import Any, List, NamedTuple, Optional, Sequence, Tuple

# Level-2 ATX heading at a line start: "## <name>". Exactly two '#' followed by
# whitespace — "###"/"####" (more hashes then no space) and "##x" (no space) do
# not match, so only true behavioral-mechanism sections are picked up.
_H2_LINE_RE = re.compile(r"^##[ \t]+(.*?)[ \t]*$", re.MULTILINE)

# Opening of a fenced code block: >= 3 backticks or tildes, optionally indented.
_FENCE_OPEN_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})([^\n]*)$")

_OP_TYPES = frozenset(
    {"replace_section", "add_section", "remove_section", "rewrite_section_for_adherence"}
)
# Ops that carry a full replacement/new section body.
_CONTENT_OPS = frozenset({"replace_section", "add_section", "rewrite_section_for_adherence"})


class Section(NamedTuple):
    """One ``## `` section of a strategy document.

    ``raw_span`` is the (start, end) char offset of the WHOLE section including its
    heading line, running up to (but not including) the next section's heading (or
    end-of-document). ``body`` is a stripped view of the content below the heading
    line — for display / membership reasoning only; reconstruction always uses the
    raw span, never ``body``.
    """

    name: str
    body: str
    raw_span: Tuple[int, int]


class SectionEditError(ValueError):
    """Raised when an edit plan or coherence diff cannot be applied safely.

    Covers unknown section targets, ambiguous duplicate section names, duplicate
    targets within one plan, malformed op content, and coherence edits that touch
    an unmodified section. The message always lists the valid section names when a
    name lookup is the cause, so the caller can surface an actionable error.
    """


# ── Fenced-code spans (ported from css.markdown_utils._fenced_spans) ──────────
def _fenced_spans(text: str) -> "List[Tuple[int, int]]":
    spans: "List[Tuple[int, int]]" = []
    open_char: "Optional[str]" = None
    open_len = 0
    start_off = 0
    offset = 0
    for line in text.splitlines(keepends=True):
        if open_char is None:
            m = _FENCE_OPEN_RE.match(line)
            if m:
                fence = m.group(1)
                open_char = fence[0]
                open_len = len(fence)
                start_off = offset
        else:
            stripped = line.strip()
            if stripped and set(stripped) == {open_char} and len(stripped) >= open_len:
                spans.append((start_off, offset + len(line)))
                open_char = None
        offset += len(line)
    if open_char is not None:  # unterminated fence runs to EOF
        spans.append((start_off, offset))
    return spans


def _in_spans(pos: int, spans: "Sequence[Tuple[int, int]]") -> bool:
    return any(s <= pos < e for s, e in spans)


def _norm(name: str) -> str:
    """Identity used to match a section name across edit ops and the document.

    Case- and whitespace-insensitive so trivial drift in an op's ``section`` field
    still resolves; only ever used for MATCHING, never for output bytes.
    """
    return re.sub(r"\s+", " ", name or "").strip().lower()


# ── Parsing ───────────────────────────────────────────────────────────────────
def parse_sections(text: str) -> "List[Section]":
    """Parse the ``## `` sections of a strategy document, in document order.

    Headings inside fenced code blocks are ignored. Text before the first heading
    (the preamble) is not returned here — use :func:`split_document`.
    """
    text = text or ""
    spans = _fenced_spans(text)
    heads = [m for m in _H2_LINE_RE.finditer(text) if not _in_spans(m.start(), spans)]
    out: "List[Section]" = []
    for i, m in enumerate(heads):
        start = m.start()
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        body = text[m.end():end].strip("\n")
        out.append(Section(name=m.group(1).strip(), body=body, raw_span=(start, end)))
    return out


def split_document(text: str) -> "Tuple[str, List[Section]]":
    """Return ``(preamble, sections)`` — preamble is everything before the first ``## ``."""
    text = text or ""
    secs = parse_sections(text)
    if not secs:
        return text, []
    return text[: secs[0].raw_span[0]], secs


def section_names(text: str) -> "List[str]":
    return [s.name for s in parse_sections(text)]


def raw_section_text(text: str, name: str) -> "Optional[str]":
    """Exact bytes of the named section (heading through the next heading), or None."""
    nm = _norm(name)
    for s in parse_sections(text):
        if _norm(s.name) == nm:
            return (text or "")[s.raw_span[0]: s.raw_span[1]]
    return None


def _normalize_block(content: str) -> str:
    """Normalize a new/replacement section body to end with one blank-line separator.

    Guarantees the next section's heading is never glued onto this block. Applied
    ONLY to blocks the plan writes; untouched blocks keep their exact bytes.
    """
    return (content or "").rstrip("\n") + "\n\n"


def _validate_content_section(content: str, expect_norm: str, op: str, valid: "List[str]") -> None:
    """A content-carrying op must supply exactly one ``## `` section named as targeted."""
    if not (content or "").strip():
        raise SectionEditError("op '%s' section '%s' has empty content" % (op, expect_norm))
    secs = parse_sections(content)
    if len(secs) != 1:
        raise SectionEditError(
            "op '%s' content must be exactly one '## ' section (found %d); it must "
            "start with a '## <name>' heading and contain no other section headings"
            % (op, len(secs))
        )
    if _norm(secs[0].name) != expect_norm:
        raise SectionEditError(
            "op '%s' content heading '%s' does not match the targeted section "
            "(rename via remove_section + add_section, not %s); valid sections: %s"
            % (op, secs[0].name, op, ", ".join(valid) or "(none)")
        )


# ── Apply an edit plan ────────────────────────────────────────────────────────
def apply_edit_plan(text: str, ops: "Sequence[dict]") -> str:
    """Apply a controlled edit plan; untouched sections stay byte-identical.

    ``ops`` are dicts with an ``op`` in {replace_section, add_section,
    remove_section, rewrite_section_for_adherence}; a content-carrying op supplies
    ``content`` = the full new section text (heading included) and, for
    add_section, an optional ``after`` anchor section name. Every op is fully
    validated BEFORE anything is applied, so a bad plan leaves ``text`` untouched
    (no partial application). Unknown section names, an ambiguous duplicate name,
    or two ops targeting the same section raise :class:`SectionEditError` listing
    the valid names.
    """
    text = text or ""
    preamble, sections = split_document(text)
    valid_names = [s.name for s in sections]

    by_norm: "dict[str, List[int]]" = {}
    for idx, s in enumerate(sections):
        by_norm.setdefault(_norm(s.name), []).append(idx)

    # Mutable per-section blocks: untouched blocks keep their EXACT substring.
    blocks: "List[Optional[str]]" = [text[s.raw_span[0]: s.raw_span[1]] for s in sections]
    adds_after: "dict[int, List[str]]" = {}
    end_adds: "List[str]" = []
    targeted: "set[str]" = set()   # normalized names an op has claimed (dedupe)

    def _resolve_existing(name: str, op: str) -> int:
        nm = _norm(name)
        idxs = by_norm.get(nm)
        if not idxs:
            raise SectionEditError(
                "op '%s' names unknown section '%s'; valid sections: %s"
                % (op, name, ", ".join(valid_names) or "(none)")
            )
        if len(idxs) > 1:
            raise SectionEditError(
                "op '%s' names ambiguous section '%s' (appears %d times)"
                % (op, name, len(idxs))
            )
        if nm in targeted:
            raise SectionEditError("duplicate edit target for section '%s'" % name)
        targeted.add(nm)
        return idxs[0]

    # Pass 1 — validate everything and stage the operations.
    staged: "List[tuple]" = []
    for op in ops or []:
        if not isinstance(op, dict):
            raise SectionEditError("edit op must be an object, got %r" % type(op).__name__)
        kind = str(op.get("op", "")).strip()
        if kind not in _OP_TYPES:
            raise SectionEditError(
                "unknown op '%s'; valid ops: %s" % (kind, ", ".join(sorted(_OP_TYPES)))
            )
        name = str(op.get("section", "")).strip()
        if not name:
            raise SectionEditError("op '%s' is missing a 'section' name" % kind)

        if kind == "add_section":
            nm = _norm(name)
            if nm in by_norm or nm in targeted:
                raise SectionEditError(
                    "add_section '%s' already exists / already added" % name
                )
            _validate_content_section(str(op.get("content", "")), nm, kind, valid_names)
            targeted.add(nm)
            anchor_idx: "Optional[int]" = None
            after = str(op.get("after", "") or "").strip()
            if after:
                anm = _norm(after)
                idxs = by_norm.get(anm)
                if not idxs:
                    raise SectionEditError(
                        "add_section '%s' anchor 'after=%s' is unknown; valid sections: %s"
                        % (name, after, ", ".join(valid_names) or "(none)")
                    )
                anchor_idx = idxs[-1]
            staged.append(("add", anchor_idx, _normalize_block(str(op.get("content", "")))))
        elif kind == "remove_section":
            idx = _resolve_existing(name, kind)
            staged.append(("remove", idx, None))
        else:  # replace_section / rewrite_section_for_adherence
            idx = _resolve_existing(name, kind)
            _validate_content_section(str(op.get("content", "")), _norm(name), kind, valid_names)
            staged.append(("replace", idx, _normalize_block(str(op.get("content", "")))))

    # Pass 2 — apply (validation already guarantees safety).
    for kind, idx, payload in staged:
        if kind == "replace":
            blocks[idx] = payload
        elif kind == "remove":
            blocks[idx] = None
        else:  # add
            if idx is None:
                end_adds.append(payload)
            else:
                adds_after.setdefault(idx, []).append(payload)

    out: "List[str]" = []
    for idx in range(len(blocks)):
        if blocks[idx] is not None:
            out.append(blocks[idx])   # type: ignore[arg-type]
        out.extend(adds_after.get(idx, []))
    return preamble + "".join(out) + "".join(end_adds)


# ── Section membership + coherence diff ───────────────────────────────────────
def _section_norm_at(text: str, pos: int) -> "Optional[str]":
    """Normalized name of the section containing char ``pos``, or None (preamble)."""
    for s in parse_sections(text):
        lo, hi = s.raw_span
        if lo <= pos < hi:
            return _norm(s.name)
    return None


def apply_coherence_diff(
    text: str,
    items: "Sequence[dict]",
    modified_sections: "Optional[Sequence[str]]" = None,
) -> str:
    """Apply exact-substring coherence fixes, restricted to modified sections.

    Each item is ``{quoted_old, new, reason}`` applied by EXACT substring match:
    ``quoted_old`` must occur EXACTLY ONCE in the current document and is replaced
    by ``new``. When ``modified_sections`` is given, an item may only touch text
    that lies wholly within one of those sections — an edit reaching into a section
    the edit plan did not modify (or the preamble) raises
    :class:`SectionEditError`, preserving the controlled-experiment guarantee even
    through the coherence pass. Items are applied in order against the running
    text; each is re-checked so a replacement cannot smuggle in a second match.
    """
    cur = text or ""
    allowed = None if modified_sections is None else {_norm(n) for n in modified_sections}
    for it in items or []:
        if not isinstance(it, dict):
            raise SectionEditError("coherence item must be an object")
        old = str(it.get("quoted_old", "") or "")
        new = str(it.get("new", "") or "")
        if not old:
            raise SectionEditError("coherence item is missing 'quoted_old'")
        n = cur.count(old)
        if n != 1:
            raise SectionEditError(
                "coherence 'quoted_old' must occur exactly once (found %d): %r"
                % (n, old[:80])
            )
        pos = cur.find(old)
        if allowed is not None:
            start_sec = _section_norm_at(cur, pos)
            end_sec = _section_norm_at(cur, pos + len(old) - 1)
            if start_sec is None or start_sec not in allowed:
                raise SectionEditError(
                    "coherence edit touches unmodified section %r; only modified "
                    "sections (%s) may be adjusted"
                    % (start_sec, ", ".join(sorted(allowed)) or "(none)")
                )
            if end_sec != start_sec:
                raise SectionEditError("coherence edit spans a section boundary")
        cur = cur[:pos] + new + cur[pos + len(old):]
    return cur


def sections_byte_identical(
    parent_text: str, child_text: str, names: "Sequence[str]"
) -> "Tuple[bool, List[str]]":
    """Mechanical keep-list check: are the named sections byte-identical in both docs?

    Returns ``(ok, offending)`` where ``offending`` lists names whose raw section
    bytes differ between parent and child (or are missing from either). Used to
    enforce the REFINE keep-list guarantee independently of any LLM self-report.
    """
    offending: "List[str]" = []
    for name in names or []:
        p = raw_section_text(parent_text, name)
        c = raw_section_text(child_text, name)
        if p is None or c is None or p != c:
            offending.append(name)
    return (not offending), offending
