"""Deterministic edit engine for ``rules.md`` (Phase 3, Module 2).

Applies :class:`~css.data.edit.Edit` / :class:`~css.data.edit.Patch` objects to
free-form ``rules.md`` text and returns per-edit
:class:`~css.data.edit.EditReport` observability records.

Adapted from SkillOpt ``skillopt/optimizer/skill.py:48-108`` (``append`` /
``insert_after`` / ``replace`` / ``delete``), but with **all SLOW_UPDATE marker
handling dropped**: CSS physically separates ``strategy.md`` (L1, read-only at
L0) from ``rules.md`` (L0, editable), so there is no protected region to parse
and no marker to anchor an append before.

Operation semantics (on ``rules.md`` text)
-------------------------------------------
* ``append``        — add ``content`` at the end of the document.
* ``insert_after``  — insert ``content`` on the line *after* the first line
  containing ``target``. If ``target`` is missing (or empty), fall back to
  ``append`` with status ``applied`` and ``detail="fallback_append:target_not_found"``
  (Q4 ruling) so the step_buffer can tell intended-location from fallback edits.
* ``replace``       — replace the first occurrence of ``target`` with
  ``content``. Missing ``target`` -> status ``skipped``.
* ``delete``        — remove the first occurrence of ``target``. Missing
  ``target`` -> status ``skipped``.

``apply_patch`` applies edits sequentially, returns one report per edit, and
never raises: any unexpected exception becomes an ``error`` report.
"""
from __future__ import annotations

from css.data.edit import Edit, EditReport, Patch

# Detail string mandated by the lead for the insert_after->append fallback, so
# the step_buffer can distinguish "applied at intended location" from "applied
# at wrong location via fallback" when feeding the optimizer's next-step context.
INSERT_AFTER_FALLBACK_DETAIL = "fallback_append:target_not_found"

_PREVIEW_LEN = 200


def _preview(text: str) -> str:
    text = text or ""
    return text[:_PREVIEW_LEN]


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
