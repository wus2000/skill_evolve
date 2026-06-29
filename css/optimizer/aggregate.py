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
_VALID_DELTA_TYPES = frozenset({"new_section", "section_rewrite", "section_refinement"})


# ── Helpers ──────────────────────────────────────────────────────────────────

def _section_headings(rules: str) -> str:
    """A compact numbered list of the current ``###`` section headings."""
    if not rules:
        return ""
    heads = [ln.strip() for ln in rules.split("\n") if ln.strip().startswith("### ")]
    return "\n".join(f"  {i + 1}. {h}" for i, h in enumerate(heads))


# ── System prompt builder ────────────────────────────────────────────────────

_MERGER_PRINCIPLES_1_7 = """\
You are the MERGER in a rules.md optimization pipeline. You consolidate
independently-proposed raw edits into section-level edit units for ablation
testing.

## What you produce

Each output edit is the COMPLETE TARGET CONTENT of one ### section in rules.md.
rules.md is a tactical playbook that a SEPARATE task-executing agent reads as
its operational instructions. The task agent has NO access to raw edits,
optimization history, or any context from this pipeline — it only sees the
final rules.md text. Therefore, every piece of content you write must be a
DIRECT, ACTIONABLE instruction that helps the agent perform tasks correctly.
Do not include any information that is only meaningful to the optimization
process (rationale, source tasks, failure statistics, "why" explanations).

## Output format — JSON only, no fences, no prose
{
  "reasoning": "<key consolidation decisions, 2-3 sentences>",
  "edits": [
    {
      "section_target": "### <exact heading>",
      "delta_type": "new_section | section_rewrite | section_refinement",
      "after_section": "### <heading of preceding section>",
      "content": "### <heading>\\n<well-structured markdown body>",
      "target_tasks": ["task_id_1", ...],
      "rationale": "<DETAILED — see Principle 6>",
      "derivation": "<DETAILED — see Principle 6>"
    }
  ]
}

Field rules:
- section_target: for rewrite/refinement, the EXACT existing heading;
  for new_section, the heading you are creating.
- after_section: REQUIRED for new_section — must be "_end" (document end),
  "_start" (before all sections), or an EXISTING ### heading from the
  section index. Do NOT reference a section created by another edit in
  this output. For section_rewrite/section_refinement, omit this field.
- content: COMPLETE section (### heading + body). This is what the task agent
  will read as its rules — write it FROM THE AGENT'S PERSPECTIVE. Include
  only what the agent needs to know to act correctly: procedures, patterns,
  checks, constraints. Do NOT include anything the agent cannot act on:
  no "**Rationale**:", no "**Source Tasks**:", no "**Why**:", no failure
  statistics, no edit provenance. Those belong in rationale/derivation fields.
  For rewrite/refinement, include ALL existing content that should be KEPT
  plus your changes.
- target_tasks: Union of source_tasks from all contributing raw edits.

## Principles

1. ONE EDIT PER SECTION. Multiple raw edits touching the same section MUST
   be merged into one edit. Non-negotiable (enables independent ablation).

2. GROUP BY CONTENT, NOT SOURCE TYPE. Failure-driven and success-driven raw
   edits proposing the same improvement → merge them. Cross-source agreement
   is high confidence; note it in derivation.

3. GAP-ALIGN. Choose delta_type based on the section index above:
   - If the section index is empty or the topic is NOT covered by any
     existing section → use "new_section" with after_section="_end".
   - If the topic IS covered by an existing ### section → use
     "section_rewrite" or "section_refinement", NEVER a duplicate new_section.

4. PRESERVE EXISTING CONTENT. For rewrite/refinement, output the COMPLETE
   section — existing bullets that should be kept + changes. You are writing
   the replacement.

5. RESOLVE CONTRADICTIONS. Conflicting raw edits → keep the version with
   more supporting patches. Explain in derivation.

6. DERIVATION TRANSPARENCY. The rationale and derivation fields carry
   critical diagnostic value — they are NOT summaries, they are detailed
   audit records. Write each thoroughly:

   rationale must answer:
   - What valuable INSIGHT was discovered from the trajectories? This may
     be a recurring failure pattern (e.g. "agent uses pd.read_excel()
     without data_only=True, causing TypeError on formula cells"), a
     success pattern worth codifying (e.g. "passing rollouts consistently
     verify sheet names match before writing — this practice prevents
     silent data loss"), or a contrastive finding (e.g. "the key
     difference between pass/fail on task X was checking file extension
     before choosing the read method").
   - Which task IDs and how many independent patches support this insight?
     (cross-patch consensus = high confidence)
   - What concrete behavior change is expected after applying this edit?

   derivation must answer:
   - Which raw edit numbers contributed? (list ALL by index, e.g. "Raw
     edits 1, 2, 5, 6, 9")
   - For each contributing raw edit, what did it propose and what was kept
     vs refined? (e.g. "Edit 2 proposed data_only=True with engine param
     — kept its more precise formulation over edit 1's simpler version")
   - Were any raw edits DROPPED? Which ones and why? (e.g. "Dropped edit
     11 which proposed 'avoid formulas entirely' — conflicts with tasks
     requiring formula output")
   - How were overlapping proposals resolved? (e.g. "Edits 5, 6, 9 all
     proposed type-checking rules — merged into one consolidated bullet")

7. QUALITY OVER QUANTITY. Fewer high-confidence edits beat many speculative
   ones. Drop weak/low-support raw edits rather than outputting noise."""

_PRINCIPLE_8_HISTORY = """

8. LEARN FROM HISTORY. The optimization history below shows recent edit
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
        window = getattr(cfg, "W", 10)
        sections.append(
            "## Optimization history\n" + _format_merger_history(step_buffer, window)
        )

    # 5. Budget
    max_edits = getattr(cfg, "max_edits_per_step", 6)
    sections.append(f"## Budget\nProduce at most {max_edits} edit units.")

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

    valid: list[MergedEdit] = []
    seen_targets: set[str] = set()

    for i, d in enumerate(raw_edits):
        # Validate content starts with ###
        content = str(d.get("content", ""))
        if not content.startswith("### "):
            _log.warning(
                "merger: dropping edit %d — content does not start with '### ' "
                "(starts with %r)",
                i, content[:30],
            )
            continue


        # Validate delta_type
        delta_type = str(d.get("delta_type", ""))
        if delta_type not in _VALID_DELTA_TYPES:
            _log.warning(
                "merger: dropping edit %d — invalid delta_type %r "
                "(expected one of %s)",
                i, delta_type, _VALID_DELTA_TYPES,
            )
            continue

        # Auto-correct: rewrite/refinement targeting a non-existent section → new_section
        section_target = str(d.get("section_target", ""))
        if delta_type in ("section_rewrite", "section_refinement"):
            if section_target not in existing_headings:
                _log.info(
                    "merger: auto-correcting edit %d — delta_type %r but "
                    "section %r not in rules.md; converting to new_section",
                    i, delta_type, section_target,
                )
                d["delta_type"] = "new_section"
                d["after_section"] = "_end"
                delta_type = "new_section"

        # Validate target_tasks is a non-empty list
        target_tasks = d.get("target_tasks", [])
        if not isinstance(target_tasks, list) or not target_tasks:
            _log.warning(
                "merger: dropping edit %d — target_tasks is empty or not a list",
                i,
            )
            continue

        # Validate no duplicate section_target
        section_target = str(d.get("section_target", ""))
        if section_target in seen_targets:
            _log.warning(
                "merger: dropping edit %d — duplicate section_target %r",
                i, section_target,
            )
            continue
        seen_targets.add(section_target)

        # Build MergedEdit via from_dict
        try:
            merged = MergedEdit.from_dict(d)
            valid.append(merged)
        except Exception:
            _log.warning(
                "merger: dropping edit %d — MergedEdit.from_dict raised an error",
                i,
                exc_info=True,
            )
            continue

    return valid


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
            max_tokens=8192,
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
