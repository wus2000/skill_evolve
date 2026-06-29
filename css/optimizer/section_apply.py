"""Deterministic section-level parse/apply/assemble for rules.md (V2).

Applies :class:`~css.data.edit.MergedEdit` objects produced by the merger to
``rules.md`` by treating ``###`` headings as section boundaries. Each MergedEdit
carries the COMPLETE target content for one section (heading + body), so apply is
a pure string replacement with no LLM involvement.

Operations:
  * ``section_rewrite`` / ``section_refinement`` -- replace the matching section
    (by exact heading match) with the edit's ``content``.
  * ``new_section`` -- insert ``content`` after the section identified by
    ``after_section`` (``_end`` for document end, ``_start`` for before all
    sections, or an exact ``###`` heading).

Preamble (text before the first ``###``) is NOT editable by the merger.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from css.data.edit import MergedEdit

_log = logging.getLogger("css")

# Matches ### headings (but not #### or deeper) at the start of a line.
_H3_RE = re.compile(r"^###[ \t]+(.*)$", re.MULTILINE)


@dataclass
class RulesSection:
    """One ``###`` section of rules.md."""
    heading: str    # "### Data Loading" or "_preamble"
    content: str    # Full text including ### heading line + body


def _in_fenced_block(text: str, pos: int) -> bool:
    """Check if position ``pos`` is inside a fenced code block."""
    fence_re = re.compile(r"^```", re.MULTILINE)
    count = 0
    for m in fence_re.finditer(text):
        if m.start() >= pos:
            break
        count += 1
    return count % 2 == 1


def parse_rules_into_sections(rules_md: str) -> list[RulesSection]:
    """Parse rules.md into ordered sections.

    - Splits on ``###`` headings (respects fenced code blocks).
    - Text before first ``###`` -> ``RulesSection(heading="_preamble", ...)``.
    - ``####`` and deeper headings are part of their parent ``###`` section.
    - Returns sections in document order.
    """
    if not rules_md:
        return [RulesSection(heading="_preamble", content="")]

    sections: list[RulesSection] = []
    # Find all ### heading positions (not inside fenced blocks)
    heading_positions: list[tuple[int, str]] = []
    for m in _H3_RE.finditer(rules_md):
        if not _in_fenced_block(rules_md, m.start()):
            heading_positions.append((m.start(), m.group(0).strip()))

    if not heading_positions:
        return [RulesSection(heading="_preamble", content=rules_md)]

    # Preamble: everything before the first ### heading
    first_pos = heading_positions[0][0]
    preamble = rules_md[:first_pos]
    if preamble.strip():
        sections.append(RulesSection(heading="_preamble", content=preamble))

    # Each heading's section runs from its position to the next heading (or EOF)
    for i, (pos, heading) in enumerate(heading_positions):
        if i + 1 < len(heading_positions):
            end = heading_positions[i + 1][0]
        else:
            end = len(rules_md)
        sections.append(RulesSection(heading=heading, content=rules_md[pos:end]))

    return sections


def apply_section_edit(rules_md: str, edit: "MergedEdit") -> str:
    """Apply ONE MergedEdit to rules.md. Deterministic, no LLM.

    - ``new_section``: insert after ``edit.after_section`` (``_end`` / ``_start``
      / exact heading).
    - ``section_rewrite`` / ``section_refinement``: replace matching section by
      heading.
    - Heading match is exact string ``==``. Mismatch -> skip + log warning.
    - Returns the updated rules.md text.
    """
    if not edit.content:
        _log.warning("section_apply: edit for %r has empty content, skipping",
                      edit.section_target)
        return rules_md

    sections = parse_rules_into_sections(rules_md)

    if edit.delta_type == "new_section":
        return _insert_new_section(sections, edit)
    elif edit.delta_type in ("section_rewrite", "section_refinement"):
        return _replace_section(sections, edit)
    else:
        _log.warning("section_apply: unknown delta_type %r, skipping",
                      edit.delta_type)
        return rules_md


def _insert_new_section(
    sections: list[RulesSection], edit: "MergedEdit",
) -> str:
    """Insert a new section after the specified anchor."""
    new_section = RulesSection(heading=edit.section_target, content=edit.content)
    after = edit.after_section

    if after == "_start":
        # Insert after preamble (or at very start if no preamble)
        insert_idx = 0
        for i, s in enumerate(sections):
            if s.heading == "_preamble":
                insert_idx = i + 1
                break
        sections.insert(insert_idx, new_section)
    elif after == "_end" or not after:
        sections.append(new_section)
    else:
        # Find the section with matching heading
        found = False
        for i, s in enumerate(sections):
            if s.heading == after:
                sections.insert(i + 1, new_section)
                found = True
                break
        if not found:
            _log.warning(
                "section_apply: after_section %r not found, appending to end",
                after,
            )
            sections.append(new_section)

    return _assemble_sections(sections)


def _replace_section(
    sections: list[RulesSection], edit: "MergedEdit",
) -> str:
    """Replace a section matching the edit's section_target heading."""
    target = edit.section_target
    for i, s in enumerate(sections):
        if s.heading == target:
            sections[i] = RulesSection(heading=target, content=edit.content)
            return _assemble_sections(sections)

    _log.warning(
        "section_apply: section %r not found for %s, skipping",
        target, edit.delta_type,
    )
    return _assemble_sections(sections)


def _assemble_sections(sections: list[RulesSection]) -> str:
    """Reassemble sections into a single rules.md string."""
    parts: list[str] = []
    for s in sections:
        text = s.content.rstrip("\n")
        if text:
            parts.append(text)
    result = "\n\n".join(parts)
    if result and not result.endswith("\n"):
        result += "\n"
    return result


def apply_all_section_edits(rules_md: str, edits: list["MergedEdit"]) -> str:
    """Apply multiple MergedEdits targeting different sections. Order-independent."""
    for edit in edits:
        rules_md = apply_section_edit(rules_md, edit)
    return rules_md


def size_guard(rules_md: str, max_chars: int = 40_000) -> str:
    """Log warning if rules.md exceeds soft cap. Never truncates. Returns input unchanged."""
    if len(rules_md) > max_chars:
        _log.warning(
            "rules.md size %d chars exceeds soft cap %d chars (%.1f%% over)",
            len(rules_md), max_chars,
            100.0 * (len(rules_md) - max_chars) / max_chars,
        )
    return rules_md


# ── Atomic JSON write helper (shared with exploitation checkpoint saves) ─────

def _save_json(path: str, obj: object) -> None:
    """Atomic JSON write (tmp + os.replace)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
