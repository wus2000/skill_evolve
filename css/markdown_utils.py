"""Markdown parsing utilities for strategy.md structure.

The strategy document is organized as numbered ``###`` subsections, each
describing one cognitive-process dimension (design D6 / §2.2). Two consumers
depend on parsing this structure deterministically:

  * REFINE (Phase 5.5): modify 1-2 ``###`` subsections; a code gate verifies
    that exactly the intended subsections changed and the rest match the parent
    byte-for-byte. This file provides that diff check — it must NOT rely on LLM
    judgment of "how big" the change was (design D10).
  * Multi-consumer views: the strategy name + body are split for prompt
    construction.

A "subsection" is the text from a ``###`` heading up to (but not including) the
next ``###`` heading at the same or higher level. Content before the first
``###`` (e.g. ``## Strategy Body`` preamble) is captured as the ``preamble``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Matches a level-3 ATX heading line: "### ...".
_H3_RE = re.compile(r"^###[ \t]+(.*)$", re.MULTILINE)

# Matches the first level-2 heading used as the strategy name: "## Name".
_H2_RE = re.compile(r"^##[ \t]+(.*)$", re.MULTILINE)


def _detect_subsection_re(text: str) -> re.Pattern:
    """Auto-detect the heading level used for strategy subsections.

    If the document has ``###`` headings (outside fenced code), use those.
    Otherwise fall back to ``##`` headings.  This handles both the D6 flat
    ``##`` format and the nested ``###`` format without hardcoding either.
    """
    spans = _fenced_spans(text)
    if any(
        not _in_spans(m.start(), spans)
        for m in _H3_RE.finditer(text)
    ):
        return _H3_RE
    return _H2_RE

# Opening of a fenced code block: a run of >= 3 backticks or >= 3 tildes,
# optionally indented, optionally followed by an info string. ``###`` lines
# inside a fence are code, not subsection headings.
_FENCE_OPEN_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})([^\n]*)$")


def _fenced_spans(text: str) -> list[tuple[int, int]]:
    """Return (start, end) char spans of fenced code blocks in ``text``.

    CommonMark-style: a fence opens with >= 3 of the same char (`` ` `` or
    ``~``); the closing fence must use the same char and be at least as long as
    the opener, with nothing else on the line. This correctly handles 4+-char
    fences (used to display a literal triple-backtick) so inner ``###`` lines do
    not leak as headings. An unterminated fence extends to end-of-text.
    """
    spans: list[tuple[int, int]] = []
    open_char: str | None = None
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
            # A valid closer: only fence chars, same char, length >= opener.
            if stripped and set(stripped) == {open_char} and len(stripped) >= open_len:
                spans.append((start_off, offset + len(line)))
                open_char = None
        offset += len(line)
    if open_char is not None:  # unterminated fence runs to EOF
        spans.append((start_off, offset))
    return spans


def _in_spans(pos: int, spans: list[tuple[int, int]]) -> bool:
    return any(s <= pos < e for s, e in spans)


@dataclass(frozen=True)
class Subsection:
    """One ``###`` subsection of the strategy body."""

    index: int                         # 0-based order in the document
    heading: str                       # heading text after "### "
    body: str                          # content below the heading (verbatim)

    @property
    def normalized_body(self) -> str:
        """Body with trailing whitespace on each line and around the block
        stripped — used for change detection that ignores cosmetic whitespace.
        """
        return "\n".join(line.rstrip() for line in self.body.splitlines()).strip()

    def key(self) -> str:
        """Identity used to align subsections across parent/child versions.

        We align by *heading text* (case- and whitespace-insensitive). REFINE
        keeps headings stable and edits bodies, so heading alignment is robust;
        if a heading itself is rewritten the gate will report it as changed.
        """
        return re.sub(r"\s+", " ", self.heading).strip().lower()


def parse_subsections(text: str) -> list[Subsection]:
    """Parse the subsections of a strategy document.

    Auto-detects whether the document uses ``###`` or ``##`` headings for its
    subsections and parses accordingly. Returns them in document order. Text
    before the first subsection heading is not returned here (use
    :func:`split_strategy` for the preamble).
    """
    spans = _fenced_spans(text)
    sub_re = _detect_subsection_re(text)
    matches = [m for m in sub_re.finditer(text) if not _in_spans(m.start(), spans)]
    subsections: list[Subsection] = []
    for i, m in enumerate(matches):
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        # Strip leading and trailing blank lines uniformly so the last
        # subsection's body is normalized the same way as the others.
        body = text[body_start:body_end].strip("\n")
        subsections.append(
            Subsection(index=i, heading=m.group(1).strip(), body=body)
        )
    return subsections


def split_strategy(text: str) -> tuple[str, str, list[Subsection]]:
    """Split a strategy document into (name, preamble, subsections).

    ``name``        — text of the first ``## `` heading (the strategy name), or
                      "" if absent.
    ``preamble``    — everything before the first subsection heading.
    ``subsections`` — the subsections in order (auto-detected ``##`` or ``###``).
    """
    spans = _fenced_spans(text)
    sub_re = _detect_subsection_re(text)
    name = ""
    # Only extract a ## name if subsections use ### (otherwise ## IS subsections)
    if sub_re is _H3_RE:
        name_match = next(
            (m for m in _H2_RE.finditer(text) if not _in_spans(m.start(), spans)), None
        )
        name = name_match.group(1).strip() if name_match else ""

    first_sub = next(
        (m for m in sub_re.finditer(text) if not _in_spans(m.start(), spans)), None
    )
    preamble = text[: first_sub.start()] if first_sub else text
    subsections = parse_subsections(text)
    return name, preamble, subsections


@dataclass(frozen=True)
class SubsectionDiff:
    """Result of comparing a child strategy against its parent.

    ``changed_keys``  — subsection keys whose body differs (or that are new).
    ``removed_keys``  — keys present in parent but absent in child.
    ``unchanged_match`` — True iff every subsection NOT in ``changed_keys`` and
                          NOT removed has a byte-identical (whitespace-normalized)
                          body in both versions.
    """

    changed_keys: tuple[str, ...]
    removed_keys: tuple[str, ...]
    added_keys: tuple[str, ...]
    unchanged_match: bool
    n_changed: int


def diff_subsections(parent_text: str, child_text: str) -> SubsectionDiff:
    """Compare two strategy documents at ``###`` subsection granularity.

    Whitespace-only differences in unchanged subsections are tolerated (we
    compare ``normalized_body``). Heading set differences are surfaced via
    ``added_keys`` / ``removed_keys``.
    """
    _, _, parent_subs = split_strategy(parent_text)
    _, _, child_subs = split_strategy(child_text)

    # Align by (heading_key, ordinal) so DUPLICATE headings do not collapse: a
    # plain ``{key: sub}`` dict would keep only the last subsection of each
    # repeated heading, causing an edit to an earlier same-named subsection to
    # be silently missed (false "no change") or misattributed. The ordinal is
    # the occurrence index of that heading within its own document.
    def _aligned(subs: list[Subsection]) -> dict[str, Subsection]:
        seen: dict[str, int] = {}
        out: dict[str, Subsection] = {}
        for s in subs:
            k = s.key()
            idx = seen.get(k, 0)
            seen[k] = idx + 1
            out[f"{k}#{idx}"] = s
        return out

    parent_map = _aligned(parent_subs)
    child_map = _aligned(child_subs)

    changed: list[str] = []          # display heading keys (un-suffixed)
    added: list[str] = []
    removed: list[str] = []
    changed_aug: set[str] = set()    # augmented keys, for the unchanged check

    for ak, csub in child_map.items():
        if ak not in parent_map:
            added.append(csub.key())
        elif csub.normalized_body != parent_map[ak].normalized_body:
            changed.append(csub.key())
            changed_aug.add(ak)

    for ak, psub in parent_map.items():
        if ak not in child_map:
            removed.append(psub.key())

    # unchanged_match: every subsection present in BOTH and not in `changed`
    # has an identical (whitespace-normalized) body.
    shared_unchanged_ok = all(
        child_map[ak].normalized_body == parent_map[ak].normalized_body
        for ak in child_map
        if ak in parent_map and ak not in changed_aug
    )

    return SubsectionDiff(
        changed_keys=tuple(changed),
        removed_keys=tuple(removed),
        added_keys=tuple(added),
        unchanged_match=shared_unchanged_ok,
        n_changed=len(changed) + len(added),
    )


def check_refine_diff(
    parent_text: str,
    child_text: str,
    *,
    max_changed: int = 2,
) -> tuple[bool, str]:
    """REFINE code gate (design D10 (a)+(b)).

    Returns ``(ok, reason)``. The diff is valid iff:
      (a) at least one ``###`` subsection changed (reject empty operations);
      (b) at most ``max_changed`` subsections changed (REFINE is local — more
          than this should escalate to PROPOSAL);
      (c) unmodified subsections are byte-identical to the parent (else this is
          not a controlled local edit and must escalate to PROPOSAL).

    This is fully deterministic: no LLM judgment of modification magnitude.
    """
    diff = diff_subsections(parent_text, child_text)

    # Structural changes — adding or removing a ``###`` subsection — are NOT a
    # controlled local body edit. REFINE keeps the subsection set fixed and only
    # rewrites 1-2 bodies; a structural change means the strategy's shape moved,
    # which is PROPOSAL territory. We surface this distinctly (and before the
    # empty-op test, since a pure add/remove can leave body-`n_changed` at 0) so
    # Phase 5 can route to PROPOSAL rather than retry REFINE.
    if diff.added_keys or diff.removed_keys:
        detail = (
            f"added={','.join(diff.added_keys) or '-'};"
            f"removed={','.join(diff.removed_keys) or '-'}"
        )
        return False, f"structure_changed:{detail}:escalate_to_proposal"
    if diff.n_changed == 0:
        return False, "no_subsection_changed:escalate_to_proposal"
    if not diff.unchanged_match:
        return False, "unmodified_subsections_differ:escalate_to_proposal"
    if diff.n_changed > max_changed:
        return False, f"too_many_changed:{diff.n_changed}>{max_changed}:escalate_to_proposal"
    return True, f"ok:{diff.n_changed}_changed"
