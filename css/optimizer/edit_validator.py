"""Independence validator for MergedEdits — structural + LLM semantic checks.

After the merger produces a set of MergedEdits, this module verifies that they
can be independently ablation-tested and collectively applied without conflicts.

Two validation layers:
  * **Structural** (programmatic, zero cost): catches unambiguous type conflicts
    (e.g. section_rewrite + point_edit on the same section).
  * **Semantic** (one LLM call, conditional): checks anchor locatability,
    anchor overlap, paraphrase duplicates, and causal dependencies among point
    edits within the same section.  Only triggered when same-section point edits
    exist — no overhead otherwise.

On violation, returns structured feedback suitable for a merger repair loop.
"""
from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from css.data.edit import SECTION_DELTA_TYPES, MergedEdit

if TYPE_CHECKING:
    from css.model.client import LLMClient

_log = logging.getLogger(__name__)

__all__ = ["validate_edits", "ValidationResult"]


# ── Result type ──────────────────────────────────────────────────────────────

class ValidationResult:
    """Outcome of an independence validation pass."""

    __slots__ = ("valid", "violations", "conflict_pairs")

    def __init__(
        self,
        valid: bool = True,
        violations: list[dict] | None = None,
        conflict_pairs: list[tuple[int, int]] | None = None,
    ):
        self.valid = valid
        self.violations = violations or []
        self.conflict_pairs = conflict_pairs or []

    def feedback_text(self) -> str:
        """Format violations as human-readable feedback for the merger repair."""
        if not self.violations:
            return ""
        parts: list[str] = [
            "INDEPENDENCE VIOLATIONS FOUND — fix and re-output all edits.\n"
        ]
        for i, v in enumerate(self.violations):
            tag = v.get("type", "UNKNOWN").upper()
            indices = v.get("edit_indices", [])
            detail = v.get("detail", "")
            suggestion = v.get("suggestion", "")
            parts.append(
                f"Violation {i + 1} [{tag}] (edits {indices}):\n"
                f"  Detail: {detail}\n"
                f"  Suggested fix: {suggestion}"
            )
        return "\n\n".join(parts)


# ── Layer A: structural checks (programmatic) ───────────────────────────────

def _structural_check(edits: list[MergedEdit]) -> list[dict]:
    """Detect unambiguous structural conflicts. Zero LLM cost."""
    violations: list[dict] = []

    section_types: dict[str, list[tuple[int, str]]] = {}
    for i, e in enumerate(edits):
        section_types.setdefault(e.section_target, []).append((i, e.delta_type))

    for section, entries in section_types.items():
        has_section_op = [
            (i, dt) for i, dt in entries if dt in SECTION_DELTA_TYPES
        ]
        has_point_op = [
            (i, dt) for i, dt in entries if dt not in SECTION_DELTA_TYPES
        ]

        # section_rewrite + point_edit on same section
        if has_section_op and has_point_op:
            sec_indices = [i for i, _ in has_section_op]
            pt_indices = [i for i, _ in has_point_op]
            violations.append({
                "type": "structural_conflict",
                "edit_indices": sec_indices + pt_indices,
                "detail": (
                    f"Section {section!r} has both section-level ops "
                    f"(edits {sec_indices}) and point-level ops "
                    f"(edits {pt_indices}). A section_rewrite replaces the "
                    f"entire section, making point edits inapplicable."
                ),
                "suggestion": (
                    "Either absorb the point edits into the section_rewrite "
                    "content, or convert the section_rewrite into point_edits."
                ),
            })

        # multiple section_rewrite on same section
        sec_rewrites = [
            (i, dt) for i, dt in has_section_op
            if dt in ("section_rewrite", "section_refinement")
        ]
        if len(sec_rewrites) > 1:
            indices = [i for i, _ in sec_rewrites]
            violations.append({
                "type": "structural_conflict",
                "edit_indices": indices,
                "detail": (
                    f"Section {section!r} has {len(sec_rewrites)} "
                    f"section_rewrite/refinement edits (edits {indices}). "
                    f"Only one section-level replacement is allowed per section."
                ),
                "suggestion": (
                    "Merge them into one section_rewrite, or convert the "
                    "less important ones into point_edits."
                ),
            })

    # point edits missing point_anchor
    for i, e in enumerate(edits):
        if e.is_point and not e.point_anchor.strip():
            violations.append({
                "type": "missing_anchor",
                "edit_indices": [i],
                "detail": (
                    f"Edit {i} is a {e.delta_type} but has an empty "
                    f"point_anchor. Point edits require an anchor to locate "
                    f"the target text in the section."
                ),
                "suggestion": (
                    "Set point_anchor to the exact text in the section that "
                    "this edit targets."
                ),
            })

    return violations


# ── Layer B: LLM semantic check ──────────────────────────────────────────────

_VALIDATOR_SYSTEM = """\
You are an edit independence validator. You check whether a set of proposed \
point-level edits to a rules.md document can be applied independently and \
simultaneously without conflicts.

## Fundamental Independence Principle

Two edits are INDEPENDENT if and only if:
1. Each edit targets a distinct, non-overlapping region of the document text.
2. Applying one edit does NOT change, remove, or alter the meaning of the \
text that the other edit targets.
3. The combined effect of applying both edits equals the sum of their \
individual effects — no edit's contribution is lost, overwritten, or \
semantically changed by the other.

Any violation of these conditions means the edits CONFLICT and cannot be \
independently ablation-tested.

## Key rule: point_add with shared anchors is NOT a conflict

Multiple point_add edits that share the same point_anchor are ALLOWED and \
should NOT be flagged. Insertions do not modify or remove existing text — \
each point_add can be independently applied to the original document and \
independently verified via ablation testing. Do NOT flag these as violations.

## Conflict patterns (non-exhaustive — flag ANY independence violation)

**Modification overlap**: Two point_edit or point_remove edits target the \
same text or overlapping text regions (even if anchor strings differ slightly \
due to paraphrasing). This is a real conflict because both attempt to \
modify/delete the same content.

**Causal dependency**: Applying edit A (point_edit or point_remove) changes \
or removes text that edit B's point_anchor references. After A is applied, \
B cannot locate its anchor or its modification becomes semantically invalid.

**Anchor not found**: An edit's point_anchor cannot be located in the target \
section — the text does not exist there (hallucinated or imprecise anchor).

**Ambiguous anchor**: An edit's point_anchor matches multiple locations in \
the target section, making the intended edit location unclear. (This does \
NOT apply to point_add — multiple insertions at the same anchor are fine.)

## Checking procedure

For each point edit:
  1. Verify the point_anchor can be unambiguously located in the target section \
(for point_add, verify the anchor exists; ambiguity is acceptable).

For each pair of point_edit/point_remove edits targeting the SAME section:
  2. Verify their anchors target distinct, non-overlapping text regions.
  3. Verify neither edit's modification would invalidate the other's anchor.

## Output format — JSON only, no fences, no prose
{
  "valid": true/false,
  "violations": [
    {
      "type": "anchor_overlap | paraphrase_duplicate | causal_dependency | \
anchor_not_found | ambiguous_anchor",
      "edit_indices": [i, j],
      "detail": "<describe the specific conflict>",
      "suggestion": "<how the merger should fix this>"
    }
  ]
}

If all edits are independent, return {"valid": true, "violations": []}.
Report ONLY genuine violations — do not flag edits that truly target \
different, non-overlapping text."""


def _needs_semantic_check(edits: list[MergedEdit]) -> bool:
    """Return True if any section has multiple point edits (the only case
    where semantic independence checking is needed)."""
    point_sections: dict[str, int] = {}
    for e in edits:
        if e.is_point:
            point_sections[e.section_target] = (
                point_sections.get(e.section_target, 0) + 1
            )
    return any(count > 1 for count in point_sections.values())


def _build_validator_user(
    rules_md: str, edits: list[MergedEdit]
) -> str:
    """Build the user prompt for the semantic validator LLM call."""
    from css.optimizer.section_apply import parse_rules_into_sections

    sections = parse_rules_into_sections(rules_md)

    # Only include sections that have point edits targeting them
    point_sections = set()
    for e in edits:
        if e.is_point:
            point_sections.add(e.section_target)

    parts: list[str] = ["## Relevant sections from rules.md\n"]
    for sec in sections:
        if sec.heading in point_sections:
            parts.append(f"--- {sec.heading} ---\n{sec.content}\n")
    if not any(sec.heading in point_sections for sec in sections):
        parts.append("(no matching sections found)\n")

    parts.append(f"\n## Point edits to validate ({len(edits)} total)\n")
    for i, e in enumerate(edits):
        if not e.is_point:
            continue
        parts.append(
            f"Edit {i}: delta_type={e.delta_type}, "
            f"section_target={e.section_target!r}\n"
            f"  point_anchor: {e.point_anchor!r}\n"
            f"  content: {e.content!r}\n"
        )

    return "\n".join(parts)


def _parse_validator_output(text: str) -> dict | None:
    """Extract the validator's JSON response."""
    if not text:
        return None
    for pattern in (
        re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL),
        re.compile(r"\{.*\}", re.DOTALL),
    ):
        m = pattern.search(text)
        if not m:
            continue
        try:
            raw = m.group(1) if "```" in pattern.pattern else m.group(0)
            obj = json.loads(raw)
        except (json.JSONDecodeError, ValueError, IndexError):
            continue
        if isinstance(obj, dict) and "valid" in obj:
            return obj
    return None


def _semantic_check(
    client: "LLMClient",
    rules_md: str,
    edits: list[MergedEdit],
) -> list[dict]:
    """Run LLM-based semantic independence check.

    Returns a list of violation dicts. On LLM failure, returns [] (optimistic
    fallback — proceed without checking).
    """
    user = _build_validator_user(rules_md, edits)
    n_edits = len(edits)

    try:
        text, _usage = client.complete_optimizer(
            _VALIDATOR_SYSTEM, user, max_tokens=4096
        )
    except Exception:
        _log.warning(
            "edit_validator: semantic check LLM call failed; optimistic fallback",
            exc_info=True,
        )
        return []

    result = _parse_validator_output(text)
    if result is None:
        _log.warning(
            "edit_validator: unparseable validator output; optimistic fallback"
        )
        return []

    raw_violations = result.get("violations", [])
    if not isinstance(raw_violations, list):
        return []

    # Post-validate: filter out violations with invalid edit_indices and
    # false-positive anchor_overlap on point_add-only groups.
    validated: list[dict] = []
    for v in raw_violations:
        if not isinstance(v, dict):
            continue
        indices = v.get("edit_indices", [])
        if not isinstance(indices, list):
            continue
        clean_indices = [i for i in indices if isinstance(i, int) and 0 <= i < n_edits]
        if not clean_indices:
            continue
        v["edit_indices"] = clean_indices

        # point_add + point_add sharing an anchor is NOT a conflict —
        # insertions are independently verifiable. Filter these out even if
        # the LLM validator flagged them.
        vtype = v.get("type", "")
        if vtype in ("anchor_overlap", "paraphrase_duplicate"):
            if all(edits[i].delta_type == "point_add" for i in clean_indices):
                _log.info(
                    "edit_validator: dropping false-positive %s on point_add "
                    "edits %s (insertions with shared anchor are allowed)",
                    vtype, clean_indices,
                )
                continue

        validated.append(v)

    return validated


# ── Conflict-pair extraction ─────────────────────────────────────────────────

def _extract_conflict_pairs(
    violations: list[dict],
) -> list[tuple[int, int]]:
    """Extract pairwise conflict relationships from violations."""
    pairs: set[tuple[int, int]] = set()
    for v in violations:
        indices = v.get("edit_indices", [])
        if len(indices) < 2:
            continue
        for a in range(len(indices)):
            for b in range(a + 1, len(indices)):
                pair = (min(indices[a], indices[b]), max(indices[a], indices[b]))
                pairs.add(pair)
    return sorted(pairs)


# ── Public API ───────────────────────────────────────────────────────────────

def validate_edits(
    edits: list[MergedEdit],
    rules_md: str,
    client: "LLMClient | None" = None,
) -> ValidationResult:
    """Run independence validation on merged edits.

    Layer A (structural) always runs.  Layer B (semantic) runs only when
    same-section point edits exist AND a client is provided.

    On any LLM failure, returns optimistic result (valid=True) so the
    pipeline proceeds — the downstream ablation verification and val gate
    still provide safety nets.
    """
    # Layer A: structural
    structural = _structural_check(edits)
    if structural:
        return ValidationResult(
            valid=False,
            violations=structural,
            conflict_pairs=_extract_conflict_pairs(structural),
        )

    # Layer B: semantic (conditional)
    if client is not None and _needs_semantic_check(edits):
        semantic = _semantic_check(client, rules_md, edits)
        if semantic:
            return ValidationResult(
                valid=False,
                violations=semantic,
                conflict_pairs=_extract_conflict_pairs(semantic),
            )

    return ValidationResult(valid=True)
