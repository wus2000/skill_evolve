"""Deterministic edit engine for ``rules.md`` (Phase 3, Module 2).

Applies :class:`~css.data.edit.Edit` / :class:`~css.data.edit.Patch` objects to
free-form ``rules.md`` text and returns per-edit
:class:`~css.data.edit.EditReport` observability records.

V2 note: ``llm_apply_patch`` and ``normalize_rules_structure`` have been removed.
The V2 exploitation flow uses deterministic section-level apply via
``css.optimizer.section_apply`` instead. This module retains the deterministic
``apply_edit`` / ``apply_patch`` functions used by ``css.proposal.inheritance``
and tests.

Operation semantics (on ``rules.md`` text)
-------------------------------------------
* ``append``        -- add ``content`` at the end of the document.
* ``insert_after``  -- insert ``content`` on the line *after* the first line
  containing ``target``. If ``target`` is missing (or empty), fall back to
  ``append`` with status ``applied`` and ``detail="fallback_append:target_not_found"``
  (Q4 ruling) so the step_buffer can tell intended-location from fallback edits.
* ``replace``       -- replace the first occurrence of ``target`` with
  ``content``. Missing ``target`` -> status ``skipped``.
* ``delete``        -- remove the first occurrence of ``target``. Missing
  ``target`` -> status ``skipped``.

``apply_patch`` applies edits sequentially, returns one report per edit, and
never raises: any unexpected exception becomes an ``error`` report.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from css.data.edit import Edit, EditReport, Patch

if TYPE_CHECKING:
    pass

# Detail string mandated by the lead for the insert_after->append fallback, so
# the step_buffer can distinguish "applied at intended location" from "applied
# at wrong location via fallback" when feeding the optimizer's next-step context.
INSERT_AFTER_FALLBACK_DETAIL = "fallback_append:target_not_found"

_PREVIEW_LEN = 200

_SECTION_HEADING = re.compile(r"^###\s+", re.MULTILINE)


def _preview(text: str) -> str:
    text = text or ""
    return text[:_PREVIEW_LEN]


def _find_section_span(text: str, heading_target: str) -> tuple[int, int] | None:
    """Find the byte span of a ``###`` section whose heading contains *heading_target*.

    Returns ``(start, end)`` covering the heading line through the last line
    before the next ``###`` heading (or EOF).  Returns ``None`` when no matching
    heading is found.
    """
    lines = text.split("\n")
    start_line: int | None = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("### ") and heading_target in stripped:
            start_line = i
            break
    if start_line is None:
        return None
    end_line = len(lines)
    for j in range(start_line + 1, len(lines)):
        if lines[j].strip().startswith("### "):
            end_line = j
            break
    start_offset = sum(len(lines[k]) + 1 for k in range(start_line))
    end_offset = sum(len(lines[k]) + 1 for k in range(end_line))
    return start_offset, end_offset


def _replace_section(text: str, heading_target: str, replacement: str) -> str | None:
    """Replace a ``###`` section (heading through next heading) with *replacement*.

    Returns the modified text, or ``None`` if no matching section was found.
    Empty *replacement* deletes the section.
    """
    span = _find_section_span(text, heading_target)
    if span is None:
        return None
    start, end = span
    before = text[:start].rstrip("\n")
    after = text[end:].lstrip("\n")
    parts = [p for p in (before, replacement.strip(), after) if p]
    result = "\n\n".join(parts)
    if result and not result.endswith("\n"):
        result += "\n"
    return result


def _append(rules_text: str, content: str) -> str:
    """Append ``content`` to the end of the document with a separating newline."""
    if not rules_text:
        return content if content.endswith("\n") else content + "\n"
    base = rules_text.rstrip("\n")
    return base + "\n\n" + content.rstrip("\n") + "\n"


def apply_edit(rules_text: str, edit: "Edit") -> tuple[str, "EditReport"]:
    """Apply a single edit to ``rules_text``.

    Returns the (possibly unchanged) new text and an :class:`EditReport`. This
    function does not raise for *expected* conditions (missing target, unknown
    op); ``apply_patch`` additionally guards against unexpected exceptions.
    """
    op = edit.op
    content = (edit.content or "").strip("\n")
    target = edit.target or ""

    report = EditReport(
        index=0,
        op=op,
        status="error",
        target_preview=_preview(target),
        content_preview=_preview(content),
    )

    if op == "append":
        report.status = "applied"
        report.detail = "append"
        return _append(rules_text, content), report

    if op == "insert_after":
        # Missing or absent anchor -> fall back to append (Q4 ruling).
        if not target or target not in rules_text:
            report.status = "applied"
            report.detail = INSERT_AFTER_FALLBACK_DETAIL
            return _append(rules_text, content), report
        lines = rules_text.split("\n")
        for i, line in enumerate(lines):
            if target in line:
                lines.insert(i + 1, content)
                report.status = "applied"
                report.detail = "insert_after"
                return "\n".join(lines), report
        # Defensive: target was in the text but not on any single line
        # (e.g. spans a newline). Fall back to append per the same ruling.
        report.status = "applied"
        report.detail = INSERT_AFTER_FALLBACK_DETAIL
        return _append(rules_text, content), report

    if op == "replace":
        if not target:
            report.status = "skipped"
            report.detail = "replace_missing_target"
            return rules_text, report
        if target not in rules_text:
            report.status = "skipped"
            report.detail = "replace_target_not_found"
            return rules_text, report
        report.status = "applied"
        report.detail = "replace"
        return rules_text.replace(target, content, 1), report

    if op == "delete":
        if not target:
            report.status = "skipped"
            report.detail = "delete_missing_target"
            return rules_text, report
        if target not in rules_text:
            report.status = "skipped"
            report.detail = "delete_target_not_found"
            return rules_text, report
        report.status = "applied"
        report.detail = "delete"
        return rules_text.replace(target, "", 1), report

    # ── Section-level operations (### heading granularity) ──────────────
    if op == "add_section":
        report.status = "applied"
        report.detail = "add_section"
        return _append(rules_text, content), report

    if op == "rewrite_section":
        if not target:
            report.status = "skipped"
            report.detail = "rewrite_section_missing_target"
            return rules_text, report
        new_text = _replace_section(rules_text, target, content)
        if new_text is None:
            report.status = "applied"
            report.detail = "rewrite_section_fallback_append"
            return _append(rules_text, content), report
        report.status = "applied"
        report.detail = "rewrite_section"
        return new_text, report

    if op == "delete_section":
        if not target:
            report.status = "skipped"
            report.detail = "delete_section_missing_target"
            return rules_text, report
        new_text = _replace_section(rules_text, target, "")
        if new_text is None:
            report.status = "skipped"
            report.detail = "delete_section_target_not_found"
            return rules_text, report
        report.status = "applied"
        report.detail = "delete_section"
        return new_text, report

    report.status = "skipped"
    report.detail = f"unknown_op:{op}"
    return rules_text, report


def apply_patch(rules_text: str, patch: "Patch") -> tuple[str, list["EditReport"]]:
    """Apply all edits in ``patch`` sequentially.

    Returns the final text and one :class:`EditReport` per edit (indexed in
    application order). Never raises: an unexpected exception while applying an
    edit is captured as an ``error`` report and the remaining edits continue
    against the last good text.
    """
    text = rules_text
    reports: list[EditReport] = []
    for i, edit in enumerate(patch.edits):
        try:
            text, report = apply_edit(text, edit)
        except Exception as exc:  # never raise to the caller
            op = getattr(edit, "op", "")
            report = EditReport(
                index=i,
                op=op,
                status="error",
                detail=f"exception:{type(exc).__name__}",
                target_preview=_preview(getattr(edit, "target", "")),
                content_preview=_preview(getattr(edit, "content", "")),
                error=str(exc),
            )
        else:
            report.index = i
        reports.append(report)
    return text, reports

