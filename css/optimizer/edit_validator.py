"""Independence validator for MergedEdits — structural + LLM semantic checks.

After the merger produces a set of MergedEdits, this module verifies that they
can be independently ablation-tested and collectively applied without conflicts.

Two validation layers:
  * **Structural** (programmatic, zero cost): catches unambiguous type conflicts
    (e.g. section_rewrite + point_edit on the same section) and phantom-section
    point ops (point ops targeting a ``###`` section that does not exist in the
    current rules.md — anchors cannot resolve there).
  * **Semantic** (one LLM call, conditional): checks anchor locatability,
    anchor overlap, paraphrase duplicates, and causal dependencies among point
    edits within the same section.  Only triggered when same-section point edits
    exist — no overhead otherwise.

On violation, returns structured feedback suitable for a merger repair loop.
:func:`salvage_phantom_point_ops` is the deterministic last-resort fallback
applied when the repair loop exhausts its retries with phantom-section
violations still present.
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

__all__ = [
    "validate_edits", "ValidationResult", "salvage_phantom_point_ops",
    "purity_telemetry",
]


def _existing_headings(rules_md: str) -> set[str]:
    """Exact ``###`` headings present in the current rules.md (fence-aware)."""
    if not rules_md or not rules_md.strip():
        return set()
    from css.optimizer.section_apply import parse_rules_into_sections
    return {
        sec.heading for sec in parse_rules_into_sections(rules_md)
        if sec.heading != "_preamble"
    }


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
        """Format violations for the repair protocol (edits referenced as E#n)."""
        if not self.violations:
            return ""
        parts: list[str] = ["VIOLATIONS FOUND:\n"]
        for i, v in enumerate(self.violations):
            tag = v.get("type", "UNKNOWN").upper()
            ids = ", ".join(f"E#{j}" for j in v.get("edit_indices", []))
            detail = v.get("detail", "")
            suggestion = v.get("suggestion", "")
            parts.append(
                f"Violation {i + 1} [{tag}] (edits: {ids}):\n"
                f"  Detail: {detail}\n"
                f"  Suggested fix: {suggestion}"
            )
        return "\n\n".join(parts)

    def implicated_indices(self) -> set[int]:
        """Union of all edit indices named by the violations (repair allowlist)."""
        out: set[int] = set()
        for v in self.violations:
            for i in v.get("edit_indices", []):
                if isinstance(i, int):
                    out.add(i)
        return out


# ── Layer A: structural checks (programmatic) ───────────────────────────────

def _structural_check(edits: list[MergedEdit], rules_md: str = "") -> list[dict]:
    """Detect unambiguous structural conflicts. Zero LLM cost."""
    violations: list[dict] = []

    # Phantom-section point ops: a point op can only modify a section that
    # EXISTS in the current rules.md — otherwise its anchor cannot resolve
    # and independent ablation apply would have nothing to attach to.
    existing = _existing_headings(rules_md)
    # Co-emitted new_section hosts (heading -> index), for fold suggestions.
    hosts: dict[str, int] = {
        e.section_target.strip(): i for i, e in enumerate(edits)
        if e.delta_type == "new_section"
    }
    phantom: dict[str, list[int]] = {}
    for i, e in enumerate(edits):
        if e.is_point and e.section_target not in existing:
            phantom.setdefault(e.section_target, []).append(i)
    for section, indices in phantom.items():
        empty_note = (
            " (the current rules.md has NO sections at all)"
            if not existing else ""
        )
        host_idx = hosts.get(section.strip())
        ids = [f"E#{j}" for j in indices]
        if host_idx is not None:
            # The point ops depend on a new_section co-emitted in this same
            # output — an independence violation with a known host: name the
            # host so the repair can fold via a targeted operation.
            violations.append({
                "type": "phantom_section",
                "edit_indices": indices + [host_idx],
                "detail": (
                    f"Point op(s) {ids} target section {section!r} which does "
                    f"NOT exist in the current rules.md — it is being CREATED "
                    f"by edit E#{host_idx} in this same output. Under "
                    f"independent ablation each edit is applied alone, so "
                    f"these point ops would have no section to attach to."
                ),
                "suggestion": (
                    f"Fold the point ops' content directly into E#{host_idx}'s "
                    f"section body (a merge operation replacing E#{host_idx}), "
                    f"and drop the separate point ops."
                ),
            })
        else:
            violations.append({
                "type": "phantom_section",
                "edit_indices": indices,
                "detail": (
                    f"Point op(s) {ids} target section {section!r} which does "
                    f"NOT exist in the current rules.md{empty_note}. Point ops "
                    f"can only modify EXISTING sections — their anchors cannot "
                    f"resolve in a section that is not there. Historical or "
                    f"proposed content is NOT part of the current rules.md."
                ),
                "suggestion": (
                    "Re-emit this material as a new_section edit (compose the "
                    "fragments into one complete section: ### heading + full "
                    "body), or re-anchor to a section that actually exists in "
                    "the section index."
                ),
            })

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
You are an edit validator for a rules.md optimization pipeline. You perform \
two checks on a set of proposed edits: INDEPENDENCE (can they be applied \
independently and simultaneously without conflicts?) and CONTENT PURITY \
(is every edit's content pure agent-facing instruction text?).

## Check 1 — Independence (applies to point-level edits)

Two edits are INDEPENDENT if and only if:
1. Each edit targets a distinct, non-overlapping region of the document text.
2. Applying one edit does NOT change, remove, or alter the meaning of the \
text that the other edit targets.
3. The combined effect of applying both edits equals the sum of their \
individual effects — no edit's contribution is lost, overwritten, or \
semantically changed by the other.

Key rule: multiple point_add edits sharing the same point_anchor are ALLOWED \
— insertions do not modify existing text and remain independently testable. \
Do NOT flag them.

Conflict patterns (non-exhaustive — flag ANY independence violation):
- **Modification overlap**: two point_edit/point_remove edits target the same \
or overlapping text (even with slightly different anchor wording).
- **Causal dependency**: applying edit A changes/removes text that edit B's \
anchor references.
- **Anchor not found**: a point_anchor does not exist in its target section.
- **Ambiguous anchor**: a point_anchor matches multiple locations (does NOT \
apply to point_add).

## Check 2 — Content purity (applies to EVERY edit's content field)

The content field is the text a SEPARATE task-executing agent will read as \
its operational rules. It must contain ONLY domain instructions the agent \
can act on. Flag content that contains optimization-process material:
- provenance or justification prose (rationale/derivation-style explanations \
of why the rule was added, which analyses support it)
- references to specific training tasks or task identifiers
- protocol bookkeeping (edit IDs, verification outcomes, gate/score talk)
- meta commentary about the optimization, editing, or merging process

Judge by SEMANTICS, not keywords: a domain rule legitimately using words \
like "derivation" or cell references like "E3" is FINE. Flag only text whose \
communicative purpose is optimizer-to-optimizer, not optimizer-to-agent. \
When uncertain, do NOT flag.

## Output format — JSON only, no fences, no prose
{
  "valid": true/false,
  "violations": [
    {
      "type": "anchor_overlap | paraphrase_duplicate | causal_dependency | \
anchor_not_found | ambiguous_anchor | content_purity",
      "edit_indices": [i, j],
      "detail": "<describe the specific problem>",
      "suggestion": "<how to fix it>"
    }
  ]
}

If everything passes, return {"valid": true, "violations": []}.
Report ONLY genuine violations."""


def _needs_semantic_check(edits: list[MergedEdit]) -> bool:
    """Layer B runs whenever there are edits to check.

    Independence needs it when same-section point edits exist; content purity
    applies to every edit. One LLM call covers both.
    """
    return bool(edits)


def _build_validator_user(
    rules_md: str, edits: list[MergedEdit]
) -> str:
    """Build the user prompt for the semantic validator LLM call."""
    from css.optimizer.section_apply import parse_rules_into_sections

    sections = parse_rules_into_sections(rules_md)

    # Include sections that have point edits targeting them (independence ctx)
    point_sections = set()
    for e in edits:
        if e.is_point:
            point_sections.add(e.section_target)

    parts: list[str] = []
    if point_sections:
        parts.append("## Relevant sections from rules.md\n")
        matched = False
        for sec in sections:
            if sec.heading in point_sections:
                parts.append(f"--- {sec.heading} ---\n{sec.content}\n")
                matched = True
        if not matched:
            parts.append("(no matching sections found)\n")

    parts.append(f"\n## Edits to validate ({len(edits)} total)\n")
    for i, e in enumerate(edits):
        lines = [
            f"Edit {i}: delta_type={e.delta_type}, "
            f"section_target={e.section_target!r}"
        ]
        if e.point_anchor:
            lines.append(f"  point_anchor: {e.point_anchor!r}")
        lines.append(f"  content: {e.content!r}")
        parts.append("\n".join(lines) + "\n")

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
    """Run independence + content-purity validation on merged edits.

    Layer A (structural) always runs.  Layer B (one LLM call — independence
    for point edits + content purity for every edit) runs whenever edits
    exist AND a client is provided.

    On any LLM failure, returns optimistic result (valid=True) so the
    pipeline proceeds — the downstream ablation verification and val gate
    still provide safety nets.
    """
    # Layer A: structural
    structural = _structural_check(edits, rules_md)
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


# ── Purity telemetry (LOG-ONLY — never enforces) ─────────────────────────────

_TELEMETRY_MARKERS = (
    "target_tasks", "Rationale:", "Source Tasks:", "derivation:",
    "GAINED", "LOST", "verdict",
)


def purity_telemetry(edits: list[MergedEdit], run_task_ids: "set[str] | None" = None) -> None:
    """Deterministic metadata-marker scan over edit contents — telemetry ONLY.

    Logs WARNING lines for observability; NEVER enforces, mutates, or feeds
    the repair loop (deterministic string rules on LLM prose are false-positive
    prone — e.g. a domain rule legitimately containing "derivation" or a
    task-id-like token). Enforcement is the semantic validator's job; this
    scan exists to collect real-world leak data so any future hard rule is
    driven by observed patterns, not imagination.
    """
    import re as _re

    for i, e in enumerate(edits):
        content = e.content or ""
        if not content:
            continue
        hits: list[str] = []
        for marker in _TELEMETRY_MARKERS:
            if marker in content:
                hits.append(marker)
        if _re.search(r"\bE#\d+\b", content):
            hits.append("protocol-id(E#n)")
        if run_task_ids:
            for tid in run_task_ids:
                if tid and tid in content:
                    hits.append(f"task-id({tid})")
                    break
        if hits:
            _log.warning(
                "purity-telemetry: edit %d (%s) content contains marker(s) %s "
                "— log-only, not enforced",
                i, e.section_target, hits,
            )


# ── Deterministic fallback: phantom-section salvage ──────────────────────────

def salvage_phantom_point_ops(
    edits: list[MergedEdit],
    rules_md: str,
) -> tuple[list[MergedEdit], bool]:
    """Last-resort structural salvage after the repair loop is exhausted.

    Purely programmatic — no LLM. For point ops whose ``section_target`` does
    not exist in the current rules.md:

      * ``point_add`` — content is self-contained new material. All phantom
        point_adds for the same section are merged into ONE ``new_section``
        edit (heading = section_target, body = contents joined in order,
        target_tasks = union). If the edit list already contains a
        ``new_section`` for that heading, the content is folded into it
        instead of creating a duplicate.
      * ``point_edit`` / ``point_remove`` — the text they want to modify or
        delete does not exist; nothing salvageable. Dropped with a log.

    Returns ``(new_edits, changed)``.
    """
    existing = _existing_headings(rules_md)

    out: list[MergedEdit] = []
    new_section_idx: dict[str, int] = {}  # heading -> index in `out`
    phantom_adds: dict[str, list[MergedEdit]] = {}
    changed = False

    for e in edits:
        if not e.is_point or e.section_target in existing:
            out.append(e)
            if e.delta_type == "new_section":
                heading = e.section_target.strip()
                new_section_idx[heading] = len(out) - 1
            continue
        changed = True
        if e.delta_type == "point_add":
            phantom_adds.setdefault(e.section_target, []).append(e)
        else:
            _log.warning(
                "salvage: dropping %s on phantom section %r — target text "
                "does not exist in current rules.md",
                e.delta_type, e.section_target,
            )

    for section, adds in phantom_adds.items():
        heading = section.strip()
        if not heading.startswith("### "):
            heading = "### " + heading.lstrip("# ").strip()
        body = "\n".join(a.content.strip() for a in adds if a.content.strip())
        tasks: list[str] = []
        for a in adds:
            for t in a.target_tasks:
                if t not in tasks:
                    tasks.append(t)
        rationale = " | ".join(a.rationale for a in adds if a.rationale)
        derivation = (
            "salvaged: %d phantom point_add(s) merged into one new_section"
            % len(adds)
        )

        if heading in new_section_idx:
            # Fold into the existing new_section edit for this heading.
            host = out[new_section_idx[heading]]
            host.content = host.content.rstrip("\n") + "\n" + body + "\n"
            for t in tasks:
                if t not in host.target_tasks:
                    host.target_tasks.append(t)
            _log.info(
                "salvage: folded %d phantom point_add(s) into existing "
                "new_section %r", len(adds), heading,
            )
        else:
            out.append(MergedEdit(
                section_target=heading,
                delta_type="new_section",
                after_section="_end",
                content=heading + "\n\n" + body + "\n",
                target_tasks=tasks,
                rationale=rationale,
                derivation=derivation,
            ))
            _log.info(
                "salvage: merged %d phantom point_add(s) into new_section %r",
                len(adds), heading,
            )

    return out, changed
