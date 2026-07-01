"""L0 Aggregate stage — section-level merger (Exploitation V2).

The Reflect stage (``css/optimizer/reflect.py``) produces one :class:`RawPatch`
per analysed minibatch (failures, successes, contrastive, each with
``source_tasks``).  The Merger consolidates those raw patches into a set of
section-level :class:`MergedEdit` objects — each targeting one ``###`` section
in ``rules.md`` — for independent ablation verification downstream.

Key design invariants (see ``docs/exploitation_v2_design.md`` section 5):
  * ONE edit per section — enables independent ablation.
  * Section-level targets, not line-level ops — deterministic apply.
  * Derivation audit trail — every merged edit traces back to raw edits.
  * History injection — past per-edit verification results inform merging.
"""
from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from css.data.edit import MergedEdit, RawPatch, Edit
from css.model.json_repair import complete_optimizer_json

if TYPE_CHECKING:
    from css.config import CSSConfig
    from css.data.step_buffer import StepBuffer
    from css.model.client import LLMClient

_log = logging.getLogger(__name__)

__all__ = ["merger"]

# Valid delta_type values for post-validation.
_VALID_DELTA_TYPES = frozenset({
    "new_section", "section_rewrite", "section_refinement", "delete_section",
    "point_edit", "point_add", "point_remove",
})


# ── Helpers ──────────────────────────────────────────────────────────────────

def _section_headings(rules: str) -> str:
    """A compact numbered list of the current ``###`` section headings."""
    if not rules:
        return ""
    heads = [ln.strip() for ln in rules.split("\n") if ln.strip().startswith("### ")]
    return "\n".join(f"  {i + 1}. {h}" for i, h in enumerate(heads))


# ── System prompt builder ────────────────────────────────────────────────────

_MERGER_PRINCIPLES_1_7 = """\
You are the MERGER in a rules.md optimization pipeline. You organize raw
edits into independently-verifiable edit units for ablation testing.

## Your core objective

Each output edit must be the SMALLEST independently-testable change. Every
output edit will be ablation-tested in isolation — applied alone to the
current rules.md, then evaluated via rollouts. If an edit bundles multiple
independent improvements together, the ablation test cannot determine which
improvement is effective. Improvements that could have been validated
independently get lost when bundled into a single edit that fails testing.

Therefore: ONE distinct improvement = ONE output edit. The number of output
edits should reflect the number of genuinely distinct improvements found in
the raw edits, NOT the number of sections in rules.md.

rules.md is a tactical playbook read by a SEPARATE task-executing agent.
Content must be DIRECT, ACTIONABLE instructions only (no rationale, source
tasks, or optimization metadata).

## Granularity decision — point edits are the DEFAULT

**Point edits** (point_edit, point_add, point_remove) are the DEFAULT output
type. Use them whenever a raw edit adds, modifies, or removes a localized
piece of text within an existing section — regardless of how many point edits
target the same section.

**Section-level edits** (section_rewrite, new_section) are the EXCEPTION.
Use section_rewrite ONLY when:
  - The raw edits collectively reorganize the internal structure of a section
    (reordering, splitting, merging its sub-parts), making localized point
    edits impractical.
  - A section is being created for the first time (new_section).

When raw edits propose localized operations (inserting a new rule, refining
the wording of a specific rule, removing a specific rule) within an existing
section, each such operation MUST be a separate point edit — even if many of
them target the same section. Do NOT collapse them into a section_rewrite.

## Output format — JSON only, no fences, no prose
{
  "reasoning": "<key consolidation decisions, 2-3 sentences>",
  "edits": [
    {
      "section_target": "### <heading>",
      "delta_type": "<operation type>",
      "after_section": "<for new_section only>",
      "content": "<see operation-specific rules>",
      "point_anchor": "<for point ops only>",
      "target_tasks": ["task_id_1", ...],
      "rationale": "<DETAILED — see Principle 5>",
      "derivation": "<DETAILED — see Principle 5>"
    }
  ]
}

## Operations

**point_edit** — replace specific text within a section:
  - section_target: the ### heading of the section containing the target
  - point_anchor: the text in the section to be replaced (verbatim from the
    current rules.md)
  - content: the replacement text

**point_add** — insert new text after a specific location:
  - section_target: the ### heading of the section
  - point_anchor: the text after which to insert (verbatim from the current
    rules.md — typically an existing rule or bullet)
  - content: the new text to insert

**point_remove** — delete specific text from a section:
  - section_target: the ### heading of the section
  - point_anchor: the text to remove (verbatim from rules.md)
  - content: (omit)

**section_rewrite** — replace an EXISTING ### section entirely:
  - section_target: the EXACT existing ### heading
  - content: the COMPLETE replacement section (### heading + full body)
  - Use ONLY when the section's internal structure must be reorganized.

**new_section** — create an entirely NEW ### section:
  - section_target: the NEW heading, matching the first line of content
  - after_section: "_end", "_start", or an EXISTING ### heading
  - content: the COMPLETE new section (### heading + full body)

**delete_section** — remove an entire ### section:
  - section_target: the EXACT existing ### heading to delete
  - content: (omit)
  - Use when a section is redundant, harmful, or its content has been
    consolidated into another section.

### Field rules for ALL operations:
  - content: Written FROM THE AGENT'S PERSPECTIVE — only actionable
    instructions. No "Rationale:", "Source Tasks:", or "Why:" blocks.
  - **NO NUMBERED HEADINGS.** "### Input Parsing", not "### 2. Input Parsing".
  - target_tasks: Union of source_tasks from contributing raw edits. Non-empty.
  - point_anchor: Must be copied VERBATIM from the current rules.md text
    shown above — not paraphrased, not abbreviated.

## Principles

1. MAXIMIZE INDEPENDENT VERIFIABILITY.
   - Each distinct improvement in the raw edits should become its own output
     edit whenever possible.
   - Raw edits adding/modifying/removing different rules within the same
     section → separate point edits, one per distinct change.
   - Only merge raw edits that modify the SAME text or that contradict each
     other. Everything else stays separate.
   - Multiple point edits targeting the same section is expected and correct.

   INDEPENDENCE REQUIREMENTS for same-section point edits:
   - point_edit and point_remove edits MUST each target DISTINCT text — two
     edits that modify or delete the same text are in conflict.
   - Multiple point_add edits MAY share the same anchor. Insertions do not
     modify or remove existing text, so they are independently verifiable
     even when anchored at the same location. Use the most semantically
     relevant existing rule as the anchor for each addition.
   - Do NOT modify text that another point edit's anchor depends on.
   - If unsure whether two point edits conflict, merge them into one.

2. CHOOSE THE RIGHT OPERATION.
   - If a raw edit proposes to IMPROVE or REFINE an existing rule's wording,
     scope, or precision → use point_edit (replace the existing rule text
     with the improved version). Do NOT add a near-duplicate rule via
     point_add while leaving the old, weaker version in place.
   - If a raw edit proposes a genuinely NEW rule that does not overlap with
     any existing rule → use point_add.
   - If a raw edit proposes removing an obsolete or harmful rule → use
     point_remove.

3. GROUP BY CONTENT, NOT SOURCE TYPE. Failure-driven and success-driven raw
   edits proposing the same improvement → merge into one edit.

4. GAP-ALIGN. If the topic is NOT covered by any existing section →
   new_section. If it IS covered → point edits (default) or section_rewrite
   (only when structural reorganization is needed).

5. RESOLVE CONTRADICTIONS. Conflicting raw edits → keep the version with
   more supporting patches. Explain in derivation.

6. DERIVATION TRANSPARENCY. rationale and derivation are detailed audit
   records:

   rationale: What insight was discovered? Which task IDs support it? What
   behavior change is expected?

   derivation: Which raw edit numbers contributed? What was kept vs dropped?
   How were overlaps resolved?

7. QUALITY OVER QUANTITY. Drop weak/low-support raw edits rather than
   outputting noise. But do NOT artificially reduce count by bundling
   independent high-confidence improvements into one edit."""

_PRINCIPLE_8_HISTORY = """

8. LEARN FROM HISTORY (when optimization history is provided below). The optimization history below shows recent edit
   verification results:
   - An edit that PASSED per-edit verification but the step was rejected at
     the final gate: the direction is sound but clashed with other edits.
     Consider re-proposing it.
   - An edit that FAILED per-edit verification: do not re-propose in the
     same form. Take a different approach.
   - A section with multiple consecutive failed edits may be near-optimal.
     Prioritize other sections."""


def _build_merger_system_prompt(has_history: bool) -> str:
    """Build the merger system prompt, conditionally including principle #8."""
    prompt = _MERGER_PRINCIPLES_1_7
    if has_history:
        prompt += _PRINCIPLE_8_HISTORY
    return prompt


# ── History formatter ────────────────────────────────────────────────────────

def _format_merger_history(step_buffer: "StepBuffer", window: int) -> str:
    """Full per-edit verification details from recent steps. No truncation.

    Shows: section_target, delta_type, content, rationale, derivation,
    per-task results. Includes all steps that have edit_verifications.
    """
    recent = step_buffer.recent(window)
    parts: list[str] = []

    for entry in recent:
        verifications = getattr(entry, "edit_verifications", None)
        if not verifications:
            continue

        step_header = (
            f"### Step {entry.step} (action={entry.action}, "
            f"score {entry.score_before:.3f} -> {entry.score_after:.3f})"
        )
        parts.append(step_header)

        for i, ev in enumerate(verifications):
            ev_section = getattr(ev, "section_target", "?")
            ev_delta = getattr(ev, "delta_type", "?")
            ev_passed = getattr(ev, "passed", False)
            ev_content = getattr(ev, "content", "")
            ev_rationale = getattr(ev, "rationale", "")
            ev_derivation = getattr(ev, "derivation", "")
            ev_task_results = getattr(ev, "task_results", {})

            lines = [
                f"  Edit {i}: section_target={ev_section}, "
                f"delta_type={ev_delta}, passed={ev_passed}",
            ]
            if ev_content:
                content_preview = ev_content.replace("\n", "\n    ")
                lines.append(f"    content:\n    {content_preview}")
            if ev_rationale:
                lines.append(f"    rationale: {ev_rationale}")
            if ev_derivation:
                lines.append(f"    derivation: {ev_derivation}")
            if ev_task_results and isinstance(ev_task_results, dict):
                lines.append("    task results:")
                for tid, tr in ev_task_results.items():
                    if isinstance(tr, dict):
                        status = tr.get("status", "?")
                        inc_pr = tr.get("inc_pr", 0.0)
                        cand_pr = tr.get("cand_pr", 0.0)
                        lines.append(
                            f"      {tid}: status={status}, "
                            f"inc_pr={inc_pr:.3f}, cand_pr={cand_pr:.3f}"
                        )
            parts.append("\n".join(lines))

    if not parts:
        return "(no edit verification history available)"
    return "\n\n".join(parts)


# ── Raw edit formatter ───────────────────────────────────────────────────────

def _format_raw_edits(raw_patches: list[RawPatch]) -> str:
    """Format all raw edits for the merger user prompt, with source_tasks."""
    entries: list[str] = []
    edit_idx = 0
    for rp_idx, rp in enumerate(raw_patches):
        if rp is None or rp.patch is None:
            continue
        source = rp.source_type or "failure"
        for e in rp.patch.edits:
            if not isinstance(e, Edit):
                continue
            edit_idx += 1
            source_tasks = getattr(e, "source_tasks", []) or []
            lines = [
                f"### Raw edit {edit_idx} (patch {rp_idx + 1}, source={source})",
                f"  op: {e.op}",
            ]
            if e.target:
                lines.append(f"  target: {e.target}")
            if e.content:
                lines.append(f"  content: {e.content}")
            if e.reason:
                lines.append(f"  rationale: {e.reason}")
            if source_tasks:
                lines.append(f"  source_tasks: {source_tasks}")
            entries.append("\n".join(lines))

    if not entries:
        return "(no raw edits)"
    return "\n\n".join(entries)


# ── User prompt builder ──────────────────────────────────────────────────────

def _build_merger_user_prompt(
    rules: str,
    raw_patches: list[RawPatch],
    step_buffer: "StepBuffer",
    cfg: "CSSConfig",
) -> str:
    """Build the user prompt for the merger LLM call.

    Sections in order:
      1. Current rules.md (always)
      2. Section index (always)
      3. Raw edits to merge (always, primary input)
      4. Optimization history (only when toggle ON and data exists)
      5. Budget (always, last — near output)
    """
    # Count total raw edits
    n_total = 0
    for rp in raw_patches:
        if rp is not None and rp.patch is not None:
            n_total += sum(1 for e in rp.patch.edits if isinstance(e, Edit))

    sections: list[str] = []

    # 1. Current rules.md
    sections.append(
        "## Current rules.md\n" + (rules.strip() if rules and rules.strip() else "(empty)")
    )

    # 2. Section index
    sections.append(
        "## Section index\n" + (_section_headings(rules) or "(no sections)")
    )

    # 3. Raw edits with source_tasks
    sections.append(
        f"## Raw edits to merge ({n_total} total)\n" + _format_raw_edits(raw_patches)
    )

    # 4. History (only when toggle ON and data exists)
    merger_inject_history = getattr(cfg, "merger_inject_history", True)
    has_history = _check_has_history(step_buffer) if merger_inject_history else False
    if has_history:
        window = getattr(cfg, "merger_history_window", 3)
        sections.append(
            "## Optimization history\n" + _format_merger_history(step_buffer, window)
        )

    # 5. Guidance
    sections.append(
        "## Edit count\n"
        "Many raw edits are redundant — multiple patches often propose the "
        "same improvement in different wording. First de-duplicate: identify "
        "the set of genuinely DISTINCT improvements across all raw edits. "
        "Then produce one output edit per distinct improvement.\n\n"
        "Do NOT artificially cap or inflate the count. Do NOT produce one "
        "edit per raw edit — de-duplicate first. The typical range after "
        "de-duplication is 5-25 edits depending on the diversity of the raw "
        "input. If the raw edits are highly redundant, fewer is correct; if "
        "they cover many independent topics, more is correct."
    )

    return "\n\n".join(sections)


# ── Parse helper ─────────────────────────────────────────────────────────────

def _parse_merger_output(text: str) -> list[dict] | None:
    """Extract the ``edits`` list from the merger's JSON response.

    Tries fenced JSON blocks first, then bare JSON objects.
    Returns the list of edit dicts, or None on parse failure.
    """
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
        if isinstance(obj, dict) and isinstance(obj.get("edits"), list):
            return [e for e in obj["edits"] if isinstance(e, dict)]
    return None


# ── Has-history check ────────────────────────────────────────────────────────

def _check_has_history(step_buffer: "StepBuffer") -> bool:
    """Check if step_buffer contains any edit_verifications data."""
    if step_buffer is None:
        return False
    for entry in step_buffer.entries:
        verifications = getattr(entry, "edit_verifications", None)
        if verifications:
            return True
    return False



# ── Post-validation ──────────────────────────────────────────────────────────

def _validate_merged_edits(raw_edits: list[dict], rules: str = "") -> list[MergedEdit]:
    """Validate and convert raw edit dicts into MergedEdit objects.

    Drops invalid edits with a log warning. Enforces:
      - ``content`` starts with ``### ``
      - ``delta_type`` is one of: new_section, section_rewrite, section_refinement
      - ``target_tasks`` is a non-empty list
      - No two edits target the same ``section_target``

    Auto-corrects: if delta_type is section_rewrite/refinement but the
    section_target heading does not exist in the current rules.md, silently
    converts to new_section with after_section="_end".
    """
    existing_headings: set[str] = set()
    if rules:
        for line in rules.split("\n"):
            stripped = line.strip()
            if stripped.startswith("### "):
                existing_headings.add(stripped)

    from css.data.edit import SECTION_DELTA_TYPES

    valid: list[MergedEdit] = []
    seen_section_rewrites: set[str] = set()

    for i, d in enumerate(raw_edits):
        delta_type = str(d.get("delta_type", ""))
        if delta_type not in _VALID_DELTA_TYPES:
            _log.warning(
                "merger: dropping edit %d — invalid delta_type %r", i, delta_type)
            continue

        is_point = delta_type not in SECTION_DELTA_TYPES
        section_target = str(d.get("section_target", ""))

        # Section-level (except delete): content must start with ###
        content = str(d.get("content", ""))
        if not is_point and delta_type != "delete_section" and not content.startswith("### "):
            _log.warning(
                "merger: dropping edit %d — section-level content does not "
                "start with '### ' (starts with %r)", i, content[:30])
            continue

        # Auto-correct: rewrite/refinement targeting a non-existent section
        if delta_type in ("section_rewrite", "section_refinement"):
            if section_target not in existing_headings:
                _log.info(
                    "merger: auto-correcting edit %d — delta_type %r but "
                    "section %r not in rules.md; converting to new_section",
                    i, delta_type, section_target)
                d["delta_type"] = "new_section"
                d["after_section"] = "_end"
                delta_type = "new_section"

        # target_tasks must be non-empty
        target_tasks = d.get("target_tasks", [])
        if not isinstance(target_tasks, list) or not target_tasks:
            _log.warning(
                "merger: dropping edit %d — target_tasks is empty or not a list", i)
            continue

        # Duplicate section_rewrite check (only for section-level ops)
        if not is_point:
            if section_target in seen_section_rewrites:
                _log.warning(
                    "merger: dropping edit %d — duplicate section-level "
                    "edit for %r", i, section_target)
                continue
            seen_section_rewrites.add(section_target)

        # Build MergedEdit
        try:
            merged = MergedEdit.from_dict(d)
            valid.append(merged)
        except Exception:
            _log.warning(
                "merger: dropping edit %d — MergedEdit.from_dict raised",
                i, exc_info=True)
            continue

    return valid


# ── Missing-required-field detector (schema-repair hook) ─────────────────────

def _merger_required_missing(edits: list[dict]) -> list[str]:
    """Hard-required fields whose absence makes ``_validate_merged_edits`` drop
    an edit (with no fallback).

    Used as the ``required=`` hook of :func:`complete_optimizer_json`: when the
    merger LLM produces an otherwise-valid edit but OMITS one of these, a
    feedback-driven repair is triggered instead of silently dropping the edit
    (the failure mode that collapsed a whole step to ``merged_edits == []``).

    Scope is deliberately narrow — ONLY fields whose absence/emptiness causes a
    drop with no fallback. Conditional/optional fields are excluded on purpose:
    ``after_section`` (only for new_section, and auto-defaulted), and
    ``reasoning``/``rationale``/``derivation`` (never validated). Presence /
    non-empty only — value-validity (delta_type enum, ``### `` prefix) stays a
    downstream drop, not a repair.
    """
    from css.data.edit import SECTION_DELTA_TYPES

    out: list[str] = []
    for i, d in enumerate(edits):
        if not isinstance(d, dict):
            continue
        sec = str(d.get("section_target") or "").strip()
        dt = str(d.get("delta_type") or "").strip()
        tag = f"edits[{i}]" + (f" (section {sec!r})" if sec else "")
        is_point = dt and dt not in SECTION_DELTA_TYPES

        if not sec:
            out.append(f"edits[{i}] is missing required 'section_target'")
        if not dt:
            out.append(f"{tag} is missing required 'delta_type'")

        # Section ops require content; point ops require content (except point_remove)
        content = str(d.get("content") or "").strip()
        if not is_point and not content:
            out.append(f"{tag} is missing required 'content'")
        if is_point and dt != "point_remove" and not content:
            out.append(f"{tag} ({dt}) is missing required 'content'")

        # Point ops require point_anchor
        if is_point and not str(d.get("point_anchor") or "").strip():
            out.append(
                f"{tag} ({dt}) is missing required 'point_anchor' — set it to "
                "the exact text in the section that this edit targets"
            )

        tt = d.get("target_tasks")
        if not (isinstance(tt, list) and len(tt) > 0):
            out.append(
                f"{tag} is missing required non-empty 'target_tasks' — set it to "
                "the union of the source_tasks of the raw edits this "
                "consolidates"
            )
    return out


# ── Public API ───────────────────────────────────────────────────────────────

def merger(
    client: "LLMClient",
    rules: str,
    raw_patches: list[RawPatch],
    step_buffer: "StepBuffer",
    cfg: "CSSConfig",
) -> list[MergedEdit]:
    """Consolidate raw patches into section-level MergedEdits.

    Single LLM call via complete_optimizer_json (with JSON repair).
    Returns list[MergedEdit]. On total failure returns [].
    """
    # Early exit: no raw patches or no edits at all
    patches = [rp for rp in raw_patches if rp is not None and rp.patch is not None]
    n_total = sum(
        sum(1 for e in rp.patch.edits if isinstance(e, Edit))
        for rp in patches
    )
    if not patches or n_total == 0:
        _log.info("merger: no raw edits to merge, returning []")
        return []

    # Determine if history should be injected
    merger_inject_history = getattr(cfg, "merger_inject_history", True)
    has_history = _check_has_history(step_buffer) if merger_inject_history else False

    # Build prompts
    system = _build_merger_system_prompt(has_history)
    user = _build_merger_user_prompt(rules, raw_patches, step_buffer, cfg)

    _log.info(
        "merger: consolidating %d raw edits from %d patches (history=%s)",
        n_total, len(patches), has_history,
    )

    # Single LLM call with JSON repair
    try:
        raw_edits = complete_optimizer_json(
            client,
            system,
            user,
            parse=_parse_merger_output,
            required=_merger_required_missing,
            max_tokens=16384,
            stage="merger",
        )
    except Exception:
        _log.warning("merger: LLM call failed, returning []", exc_info=True)
        return []

    if raw_edits is None:
        _log.warning("merger: LLM returned unparseable output, returning []")
        return []

    # Post-validate
    merged = _validate_merged_edits(raw_edits, rules=rules)

    _log.info(
        "merger: %d raw edits -> %d LLM outputs -> %d validated MergedEdits",
        n_total, len(raw_edits), len(merged),
    )
    return merged
