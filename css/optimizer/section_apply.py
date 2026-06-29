"""Deterministic section-level parse / apply / assemble for rules.md.

rules.md is structured as a sequence of ### sections. This module provides
the mechanical operations for applying MergedEdit units (produced by the
merger) to rules.md without any LLM involvement.
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

from css.markdown_utils import _fenced_spans, _in_spans

_log = logging.getLogger(__name__)

# Matches a level-3 ATX heading line: "### ...".
_H3_RE = re.compile(r"^###[ \t]+(.*)$", re.MULTILINE)


@dataclass
class RulesSection:
    """One ### section (or preamble) of rules.md."""

    heading: str  # "### Data Loading" or "_preamble"
    content: str  # Full text including ### heading line + body


def parse_rules_into_sections(rules_md: str) -> list[RulesSection]:
    """Parse rules.md into ordered sections.

    - Splits on ### headings (must respect fenced code blocks -- ### inside
      fenced code blocks are NOT section boundaries).
    - Text before first ### -> RulesSection(heading="_preamble", ...).
    - #### and deeper headings are part of their parent ### section.
    - Empty input -> [RulesSection(heading="_preamble", content="")].
    """
    if not rules_md:
        return [RulesSection(heading="_preamble", content="")]

    # 1. Find all fenced code block spans
    fenced = _fenced_spans(rules_md)

    # 2. Find all ### heading matches at line start, filtering out those
    #    inside fenced code blocks
    heading_matches = [
        m for m in _H3_RE.finditer(rules_md)
        if not _in_spans(m.start(), fenced)
    ]

    sections: list[RulesSection] = []

    if not heading_matches:
        # No ### headings found -- entire document is preamble
        return [RulesSection(heading="_preamble", content=rules_md)]

    # 3. Everything before the first heading is the preamble
    first_pos = heading_matches[0].start()
    preamble_text = rules_md[:first_pos]
    sections.append(RulesSection(heading="_preamble", content=preamble_text))

    # 4. Each heading + content until next heading = one section
    for i, m in enumerate(heading_matches):
        section_start = m.start()
        if i + 1 < len(heading_matches):
            section_end = heading_matches[i + 1].start()
        else:
            section_end = len(rules_md)

        # The full heading line including "### " prefix
        heading_line = m.group(0).strip()
        section_content = rules_md[section_start:section_end]

        sections.append(RulesSection(heading=heading_line, content=section_content))

    return sections


def apply_section_edit(rules_md: str, edit: "MergedEdit") -> str:
    """Apply ONE MergedEdit to rules.md. Deterministic, no LLM.

    - new_section: insert edit.content after edit.after_section.
      "_end" = append at document end.
      "_start" = insert after preamble (before first ### section).
      Otherwise: insert after the section whose heading matches exactly.
    - section_rewrite / section_refinement: find the section whose heading
      matches edit.section_target exactly, replace it with edit.content.
    - Heading match is EXACT string ==.
    - Mismatch (heading not found) -> log warning, return rules_md unchanged.
    """
    if edit.delta_type == "new_section":
        return _apply_new_section(rules_md, edit)
    elif edit.delta_type in ("section_rewrite", "section_refinement"):
        return _apply_section_replace(rules_md, edit)
    else:
        _log.warning(
            "Unknown delta_type %r for section_target %r; skipping",
            edit.delta_type,
            edit.section_target,
        )
        return rules_md


def _apply_new_section(rules_md: str, edit: "MergedEdit") -> str:
    """Insert a new section into rules_md based on edit.after_section."""
    sections = parse_rules_into_sections(rules_md)
    after = edit.after_section

    # Ensure content ends with a newline for clean concatenation
    new_content = edit.content
    if new_content and not new_content.endswith("\n"):
        new_content += "\n"

    if after == "_end":
        # Append at document end
        separator = "\n" if rules_md and not rules_md.endswith("\n") else ""
        return rules_md + separator + new_content

    if after == "_start":
        # Insert after preamble (before first ### section)
        if len(sections) <= 1:
            # Only preamble exists -- just append
            preamble = sections[0].content
            separator = "\n" if preamble and not preamble.endswith("\n") else ""
            return preamble + separator + new_content
        else:
            preamble = sections[0].content
            rest_start = len(preamble)
            rest = rules_md[rest_start:]
            separator = "\n" if preamble and not preamble.endswith("\n") else ""
            return preamble + separator + new_content + rest

    # Insert after a specific section by exact heading match
    target_idx = None
    for i, sec in enumerate(sections):
        if sec.heading == after:
            target_idx = i
            break

    if target_idx is None:
        _log.warning(
            "after_section heading %r not found for new_section %r; "
            "returning rules_md unchanged",
            after,
            edit.section_target,
        )
        return rules_md

    # Reconstruct: everything up to and including target section,
    # then new content, then remaining sections
    parts_before: list[str] = []
    parts_after: list[str] = []
    for i, sec in enumerate(sections):
        if i <= target_idx:
            parts_before.append(sec.content)
        else:
            parts_after.append(sec.content)

    before_text = "".join(parts_before)
    after_text = "".join(parts_after)

    separator = "\n" if before_text and not before_text.endswith("\n") else ""
    return before_text + separator + new_content + after_text


def _apply_section_replace(rules_md: str, edit: "MergedEdit") -> str:
    """Replace an existing section whose heading matches edit.section_target."""
    sections = parse_rules_into_sections(rules_md)

    target_idx = None
    for i, sec in enumerate(sections):
        if sec.heading == edit.section_target:
            target_idx = i
            break

    if target_idx is None:
        _log.warning(
            "section_target heading %r not found for %s; "
            "returning rules_md unchanged",
            edit.section_target,
            edit.delta_type,
        )
        return rules_md

    # Ensure replacement content ends with newline for clean joins
    new_content = edit.content
    if new_content and not new_content.endswith("\n"):
        new_content += "\n"

    # Reconstruct document with the target section replaced
    parts: list[str] = []
    for i, sec in enumerate(sections):
        if i == target_idx:
            parts.append(new_content)
        else:
            parts.append(sec.content)

    return "".join(parts)


def apply_all_section_edits(rules_md: str, edits: list["MergedEdit"]) -> str:
    """Apply multiple MergedEdits targeting different sections.

    Since edits target different sections, order is irrelevant.
    Applies sequentially for simplicity.
    """
    result = rules_md
    for edit in edits:
        result = apply_section_edit(result, edit)
    return result


def size_guard(rules_md: str, max_chars: int = 40_000) -> str:
    """Log warning if rules.md exceeds soft cap. NEVER truncates.

    Returns input unchanged.
    """
    n = len(rules_md)
    if n > max_chars:
        _log.warning(
            "rules.md size %d chars exceeds soft cap %d (%.1f%% over)",
            n,
            max_chars,
            ((n - max_chars) / max_chars) * 100,
        )
    return rules_md


def _save_json(path: str, obj) -> None:
    """Atomic JSON write: write to .tmp then os.replace for crash safety."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
