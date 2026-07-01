"""Section-level parse / apply / assemble for rules.md.

rules.md is structured as a sequence of ### sections. This module provides:
  * Deterministic parse/assemble utilities.
  * LLM-based faithful apply — the primary path for applying MergedEdits.
  * Deterministic apply — kept as fallback / reference.
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
    from css.model.client import LLMClient

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

    Section ops:
    - new_section: insert edit.content after edit.after_section.
    - section_rewrite / section_refinement: replace matching section.

    Point ops (deterministic fallback — exact string match):
    - point_edit: replace edit.point_anchor with edit.content.
    - point_add: insert edit.content after edit.point_anchor.
    - point_remove: delete edit.point_anchor.
    """
    if edit.delta_type == "new_section":
        return _apply_new_section(rules_md, edit)
    elif edit.delta_type == "delete_section":
        return _apply_section_delete(rules_md, edit)
    elif edit.delta_type in ("section_rewrite", "section_refinement"):
        return _apply_section_replace(rules_md, edit)
    elif edit.delta_type in ("point_edit", "point_add", "point_remove"):
        return _apply_point_edit(rules_md, edit)
    else:
        _log.warning(
            "Unknown delta_type %r for section_target %r; skipping",
            edit.delta_type,
            edit.section_target,
        )
        return rules_md


def _apply_point_edit(rules_md: str, edit: "MergedEdit") -> str:
    """Apply a point-level edit via exact string matching (deterministic fallback)."""
    anchor = edit.point_anchor
    if not anchor or anchor not in rules_md:
        _log.warning(
            "point_anchor %r not found in rules.md for %s on %s; returning unchanged",
            anchor[:80] if anchor else "(empty)",
            edit.delta_type,
            edit.section_target,
        )
        return rules_md

    if edit.delta_type == "point_edit":
        return rules_md.replace(anchor, edit.content, 1)
    elif edit.delta_type == "point_add":
        return rules_md.replace(anchor, anchor + "\n" + edit.content, 1)
    elif edit.delta_type == "point_remove":
        result = rules_md.replace(anchor, "", 1)
        return re.sub(r"\n{3,}", "\n\n", result)
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
            "falling back to append at end",
            after,
            edit.section_target,
        )
        separator = "\n" if rules_md and not rules_md.endswith("\n") else ""
        return rules_md + separator + new_content

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


def _apply_section_delete(rules_md: str, edit: "MergedEdit") -> str:
    """Remove an entire ### section (heading + body) from rules_md."""
    sections = parse_rules_into_sections(rules_md)
    target_idx = None
    for i, sec in enumerate(sections):
        if sec.heading == edit.section_target:
            target_idx = i
            break
    if target_idx is None:
        _log.warning(
            "section_target heading %r not found for delete_section; "
            "returning rules_md unchanged",
            edit.section_target,
        )
        return rules_md
    parts = [sec.content for i, sec in enumerate(sections) if i != target_idx]
    result = "".join(parts)
    return re.sub(r"\n{3,}", "\n\n", result)


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


# ---------------------------------------------------------------------------
# LLM-based apply — primary path
# ---------------------------------------------------------------------------

_LLM_APPLY_SYSTEM = """\
You are a document editor. Your ONLY job is to apply the specified edit(s) to \
the given rules.md document FAITHFULLY, then call write_rules_md with the result.

rules.md is a tactical playbook read by a task-executing agent. Your job is \
purely mechanical — apply each edit precisely, then output the complete document.

## Edit types

**Section-level edits:**
- "new_section": insert the new ### section at a logical position in the \
document (after thematically related sections, or at the end if unsure).
- "section_rewrite" / "section_refinement": find the matching ### section by \
its heading and replace it entirely with the edit's content field.
- "delete_section": find the matching ### section by its heading and remove \
it entirely (heading + all content under it).

**Point-level edits** (localized changes within a section):
- "point_edit": locate the point_anchor text within the target section and \
replace it with the content field.
- "point_add": locate the point_anchor text and insert the content field \
immediately after it.
- "point_remove": locate the point_anchor text and remove it.

For point edits, the point_anchor may not be an exact string match — use \
semantic understanding to locate the intended text in the target section.

## Rules
1. Reproduce ALL edit content WORD-FOR-WORD — do NOT rephrase, summarize, \
add to, or omit any part of the edit content.
2. PRESERVE all existing rules.md content that is NOT being modified by an edit.
3. Do NOT add any commentary, rationale, source tasks, or meta-information \
that is not in the edit content."""

_WRITE_RULES_TOOL = {
    "type": "function",
    "function": {
        "name": "write_rules_md",
        "description": (
            "Write the complete updated rules.md content. "
            "rules.md is read by a task-executing agent as its tactical playbook. "
            "Content must contain ONLY actionable rules — no rationale, no source "
            "task lists, no optimization metadata."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The complete rules.md text after applying all edits",
                }
            },
            "required": ["content"],
        },
    },
}


def _format_edit_for_apply(edit: "MergedEdit") -> str:
    """Render one MergedEdit as a text block for the LLM apply prompt."""
    lines = [
        f"Type: {edit.delta_type}",
        f"Section: {edit.section_target}",
    ]
    if edit.point_anchor:
        lines.append(f"Anchor: {edit.point_anchor}")
    if edit.content:
        lines.append("Content:")
        lines.append(edit.content)
    return "\n".join(lines)



def llm_apply_edit(
    client: "LLMClient",
    rules_md: str,
    edit: "MergedEdit",
    *,
    max_tokens: int = 32768,
) -> str:
    """Apply ONE MergedEdit to rules.md via LLM. For per-edit ablation.

    Returns the complete updated rules.md text.
    Falls back to deterministic apply_section_edit on LLM failure.
    """
    user = (
        "## Current rules.md\n"
        + (rules_md.strip() if rules_md and rules_md.strip() else "(empty)")
        + "\n\n## Edit to apply\n"
        + _format_edit_for_apply(edit)
        + "\n\nApply this edit and output the complete updated rules.md."
    )

    try:
        result = client.complete_tool_call(
            _LLM_APPLY_SYSTEM, user, _WRITE_RULES_TOOL, max_tokens=max_tokens
        )
        content = result.get("content", "")
    except Exception:
        _log.exception("LLM apply tool call failed for edit %r; falling back to deterministic apply",
                        edit.section_target)
        return apply_section_edit(rules_md, edit)

    if not content or not content.strip():
        _log.warning("LLM apply returned empty content for edit %r; falling back", edit.section_target)
        return apply_section_edit(rules_md, edit)

    return content.strip()


def llm_apply_edits(
    client: "LLMClient",
    rules_md: str,
    edits: list["MergedEdit"],
    *,
    max_tokens: int = 32768,
) -> str:
    """Apply multiple MergedEdits to rules.md via LLM. For collective apply.

    All edits are applied in a single LLM call to produce a coherent document.
    Falls back to sequential deterministic apply on LLM failure.
    """
    if not edits:
        return rules_md

    edit_blocks = []
    for i, edit in enumerate(edits):
        edit_blocks.append(f"### Edit {i + 1}\n{_format_edit_for_apply(edit)}")

    user = (
        "## Current rules.md\n"
        + (rules_md.strip() if rules_md and rules_md.strip() else "(empty)")
        + f"\n\n## Edits to apply ({len(edits)} total)\n"
        + "\n\n".join(edit_blocks)
        + "\n\nApply ALL edits and output the complete updated rules.md."
    )

    try:
        result = client.complete_tool_call(
            _LLM_APPLY_SYSTEM, user, _WRITE_RULES_TOOL, max_tokens=max_tokens
        )
        content = result.get("content", "")
    except Exception:
        _log.exception("LLM collective apply tool call failed; falling back to deterministic apply")
        return apply_all_section_edits(rules_md, edits)

    if not content or not content.strip():
        _log.warning("LLM collective apply returned empty content; falling back")
        return apply_all_section_edits(rules_md, edits)

    return content.strip()


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
