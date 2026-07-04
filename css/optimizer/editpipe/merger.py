"""v2 merger: consolidate raw patches into SectionEdits (orthogonal schema).

One LLM call through ``complete_optimizer_json`` (structural + missing-field
repair), followed by at most one in-band COHERENCE feedback round fixing
output-internal structure problems (identity collisions, headings inside
bodies) at the interface where they were produced — the cheapest point to
repair, with the full generation context still in front of the model.

The mechanical layer after parsing is the syntax gate (schema.py): lossless
normalization + violation detection. Nothing is dropped here except
byte-identical duplicates; remaining violations travel with the edits into
the adjudication loop (adjudicate.py).
"""
from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

from css.data.edit import Edit, RawPatch
from css.model.json_repair import complete_optimizer_json, repair_json_via_llm
from css.optimizer.editpipe.render import RulesDoc
from css.optimizer.editpipe.schema import (
    EditAudit,
    SectionEdit,
    Violation,
    detect_conflicts,
    detect_restatements,
    syntax_gate,
)

if TYPE_CHECKING:
    from css.data.step_buffer import StepBuffer

_log = logging.getLogger(__name__)

# ── System prompt ────────────────────────────────────────────────────────────
# Derived from the proven point-granularity merger prompt; vocabulary is the
# orthogonal schema. The load-bearing changes vs the legacy prompt:
#   * subject = IDENTITY (a name), placement = POSITION (a hint) — separated;
#   * body carries NO heading line (headings are rendered from subject);
#   * raw-edit `target` fields are explained as position hints, never homes.

MERGER_SYSTEM = """\
You are the MERGER in a rules.md optimization pipeline. You organize raw
edits into independently-verifiable edit units for ablation testing.

## Your core objective

Each output edit must be the SMALLEST independently-testable change. Every
output edit will be ablation-tested in isolation — applied alone to the
current rules.md, then evaluated via rollouts. If an edit bundles multiple
independent improvements together, the ablation test cannot determine which
improvement is effective.

Therefore: ONE distinct improvement = ONE output edit. The number of output
edits should reflect the number of genuinely distinct improvements found in
the raw edits, NOT the number of sections in rules.md.

rules.md is a tactical playbook read by a SEPARATE task-executing agent.
Bodies must be DIRECT, ACTIONABLE instructions only (no rationale, source
tasks, or optimization metadata).

## The edit model — identity vs position

Every edit names its SUBJECT: the section it defines or modifies. The
subject is an IDENTITY, never a position.

- For add_section, subject is the NEW section's name — you invent it to
  describe the edit's own theme. Two edits with different themes MUST have
  different subjects. Never reuse another section's name as the subject of
  unrelated content.
- For every other kind, subject is the EXACT name of an existing section
  (copy it from the Section index; do not include the ### marker).
- WHERE a new section is inserted is a separate field, `placement` — "end",
  "start", or the name of an existing section to insert after. If unsure,
  use "end". Placement is only a hint; it never affects identity.

Raw edits below may carry an `op`/`target` in a legacy vocabulary. Their
`target` is usually a POSITION hint (e.g. "insert after section X") — it does
NOT mean the content belongs to section X. Decide each output edit's subject
from its CONTENT's theme, and use the raw target at most as a placement hint.

## Output format — JSON only, no fences, no prose
{
  "reasoning": "<key consolidation decisions, 2-3 sentences>",
  "edits": [
    {
      "kind": "<operation, see below>",
      "subject": "<section name, no ### marker>",
      "placement": "<add_section only: end | start | existing section name>",
      "anchor": "<point ops only: verbatim text from the section>",
      "body": "<the content; NEVER include any markdown heading line>",
      "target_tasks": ["task_id_1", ...],
      "rationale": "<DETAILED — see Principle 5>",
      "derivation": "<DETAILED — see Principle 5>"
    }
  ]
}

## Operations

**add_point** — insert a new rule into an existing section:
  - subject: the existing section's name;  anchor: the text after which to
    insert (verbatim from the current rules.md);  body: the new text.

**edit_point** — replace specific text within an existing section:
  - subject: the existing section's name;  anchor: the exact text being
    replaced (verbatim from the current rules.md);  body: the replacement.

**remove_point** — delete specific text from an existing section:
  - subject: the existing section's name;  anchor: the text to remove;
    body: omit.

**add_section** — create an entirely NEW section:
  - subject: the NEW section's name (invent it; descriptive, no numbering);
  - placement: "end" | "start" | an existing section's name;
  - body: the complete section content WITHOUT the heading line — the
    heading is generated from subject automatically.

**rewrite_section** — replace an EXISTING section's entire body:
  - subject: the EXACT existing section name;  body: the complete new
    content (again WITHOUT a heading line). Use ONLY when the section's
    internal structure must be reorganized.

**remove_section** — delete an entire existing section:
  - subject: the EXACT existing section name;  body: omit.

### Field rules for ALL operations
  - body: from the task agent's perspective; actionable instructions only.
  - body: NO markdown heading lines of any level. Use **bold** lead-ins for
    sub-grouping inside a section.
  - subject: plain section name — "Input Parsing", never "### Input Parsing"
    and never "2. Input Parsing".
  - target_tasks: union of source_tasks from contributing raw edits;
    non-empty.
  - anchor: copied VERBATIM from the current rules.md text shown below —
    not paraphrased, not abbreviated.
  - Point ops require an EXISTING section (in the Section index). Text seen
    in raw edits or history is NOT part of the current rules.md. To
    introduce material whose section does not exist yet, use add_section.
    Never emit point ops against a section another edit in this same output
    is creating.

## Principles

1. MAXIMIZE INDEPENDENT VERIFIABILITY.
   - Each distinct improvement becomes its own output edit.
   - Raw edits adding/modifying/removing different rules within the same
     section → separate point edits, one per distinct change.
   - Only merge raw edits that modify the SAME text or contradict each
     other. Everything else stays separate.
   - Multiple point edits on the same section is expected and correct.
   - edit_point / remove_point edits must target DISTINCT text; multiple
     add_point edits MAY share an anchor.

2. CHOOSE THE RIGHT OPERATION. Improving an existing rule's wording or
   scope → edit_point (do NOT add a near-duplicate next to the old rule).
   A genuinely new rule → add_point. An obsolete/harmful rule →
   remove_point. Internal reorganization → rewrite_section. A theme not
   specifically covered by any existing section → add_section (a normal,
   first-class outcome at ANY step).

3. GROUP BY CONTENT, NOT SOURCE TYPE. Failure-driven and success-driven raw
   edits proposing the same improvement → merge into one edit.

4. SEMANTIC HOME discipline. Place material by THEME. Do NOT absorb a
   foreign theme into an existing section merely because that section
   exists and is broadly named; give it its own add_section instead.

5. DERIVATION TRANSPARENCY. rationale and derivation are detailed audit
   records. rationale: what insight was discovered, which task IDs support
   it, what behavior change is expected. derivation: which raw edit numbers
   contributed, what was kept vs dropped, how overlaps were resolved.

6. RESOLVE CONTRADICTIONS. Conflicting raw edits → keep the version with
   more supporting patches; explain in derivation.

7. QUALITY OVER QUANTITY. Drop weak/low-support raw edits rather than
   outputting noise — and record every such drop in the derivation of the
   nearest surviving edit (or the reasoning field if none survives), so the
   decision is auditable. Do NOT artificially reduce count by bundling
   independent high-confidence improvements into one edit.

TRAIN/TEST FIREWALL (non-negotiable). You are operating during TRAINING on
the training split, where ground-truth / "expected" / golden answers may be
visible to you (in evaluation notes, fail reasons, or trajectories). It is
fine to LOOK at them to understand what truly went wrong. But everything you
PRODUCE — the analysis, the patterns you mine, and above all the rules.md
that results — is DEPLOYED at TEST time, where the agent has NO ground truth
and sees only the task instruction and the input. Therefore nothing you
output may reference, depend on, compare against, validate with, or instruct
the agent to use expected / ground-truth / golden values. Reason FROM ground
truth privately if it helps your diagnosis, but the agent can NEVER access
it at run time. State your findings in terms of what the agent can observe
from the task and input alone."""

_PRINCIPLE_HISTORY = """

8. LEARN FROM HISTORY. The optimization history below shows recent per-edit
   verification results:
   - An edit that PASSED per-edit verification but whose step was rejected
     at the final gate: the direction is sound but clashed with other edits.
     Consider re-proposing it.
   - An edit that FAILED per-edit verification: do not re-propose in the
     same form. Take a different approach.
   - A section with multiple consecutive failed edits may be near-optimal.
     Prioritize other sections."""


# ── Parsing ──────────────────────────────────────────────────────────────────

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_BARE_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_merger_output(text: str) -> list[dict] | None:
    """Extract the ``edits`` list from the merger's JSON response."""
    if not text:
        return None
    for pattern in (_FENCE_RE, _BARE_RE):
        m = pattern.search(text)
        if not m:
            continue
        try:
            raw = m.group(1) if pattern is _FENCE_RE else m.group(0)
            obj = json.loads(raw)
        except (json.JSONDecodeError, ValueError, IndexError):
            continue
        if isinstance(obj, dict) and isinstance(obj.get("edits"), list):
            return [e for e in obj["edits"] if isinstance(e, dict)]
    return None


def required_fields_missing(edits: list[dict]) -> list[str]:
    """Missing-required-field detector for ``complete_optimizer_json``.

    Only fields whose absence blocks downstream stages outright. Everything
    else (bad kinds, colliding subjects, headings in bodies) is either
    losslessly normalized or adjudicated — not a repair trigger here.
    """
    out: list[str] = []
    for i, d in enumerate(edits or []):
        if not isinstance(d, dict):
            continue
        subject = str(d.get("subject") or "").strip()
        kind = str(d.get("kind") or "").strip()
        tag = f"edits[{i}]" + (f" (subject {subject!r})" if subject else "")
        if not subject:
            out.append(f"edits[{i}] is missing required 'subject'")
        if not kind:
            out.append(f"{tag} is missing required 'kind'")
        if kind not in ("remove_section", "remove_point") \
                and not str(d.get("body") or "").strip():
            out.append(f"{tag} is missing required 'body'")
        if kind in ("add_point", "edit_point", "remove_point") \
                and not str(d.get("anchor") or "").strip():
            out.append(
                f"{tag} ({kind}) is missing required 'anchor' — set it to "
                "the exact text in the section this edit targets")
        tt = d.get("target_tasks")
        if not (isinstance(tt, list) and len(tt) > 0):
            out.append(
                f"{tag} is missing required non-empty 'target_tasks' — the "
                "union of the source_tasks of the raw edits it consolidates")
    return out


# ── Prompt assembly (reuses the proven legacy user-prompt skeleton) ─────────

def _section_index(rules: str) -> str:
    doc = RulesDoc.parse(rules)
    if not doc.sections:
        return "(no sections)"
    return "\n".join(
        f"  {i + 1}. {s.subject}" for i, s in enumerate(doc.sections))


def _format_raw_edits(raw_patches: list[RawPatch]) -> str:
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
            subject = getattr(e, "subject", "") or ""
            anchor = getattr(e, "anchor", "") or ""
            if subject:
                lines.append(f"  subject: {subject}")
            if anchor:
                lines.append(f"  anchor: {anchor}")
            if e.target and not subject:
                lines.append(f"  target (position hint): {e.target}")
            if e.content:
                lines.append(f"  content: {e.content}")
            if e.reason:
                lines.append(f"  rationale: {e.reason}")
            if source_tasks:
                lines.append(f"  source_tasks: {source_tasks}")
            entries.append("\n".join(lines))
    return "\n\n".join(entries) if entries else "(no raw edits)"


def build_merger_user_prompt(
    rules: str,
    raw_patches: list[RawPatch],
    history_text: str = "",
) -> str:
    n_total = sum(
        sum(1 for e in rp.patch.edits if isinstance(e, Edit))
        for rp in raw_patches if rp is not None and rp.patch is not None)

    sections = [
        "## Current rules.md\n"
        + (rules.strip() if rules and rules.strip() else "(empty)"),
        "## Section index\n" + _section_index(rules),
        f"## Raw edits to merge ({n_total} total)\n"
        + _format_raw_edits(raw_patches),
    ]
    if history_text:
        sections.append("## Optimization history\n" + history_text)

    guidance = (
        "## Edit count\n"
        "Many raw edits are redundant — multiple patches often propose the "
        "same improvement in different wording. First de-duplicate: identify "
        "the set of genuinely DISTINCT improvements across all raw edits. "
        "Then produce one output edit per distinct improvement. Do NOT "
        "artificially cap or inflate the count.\n\n"
        "## Delta only\n"
        "Never restate rules that already exist in the current rules.md — "
        "output only the CHANGE. If a theme already has a section (check the "
        "Section index), improvements to it are point edits inside that "
        "section, not a new near-duplicate section under a slightly "
        "different name."
    )
    doc = RulesDoc.parse(rules)
    if len(doc.sections) < 2:
        guidance += (
            "\n\nThe document currently has little or no structure. When "
            "establishing the initial structure, organize the material into "
            "a SMALL number of THEMATIC sections: a section owns a theme, "
            "and related rules become bullets WITHIN it — never one section "
            "per individual rule."
        )
    sections.append(guidance)
    return "\n\n".join(sections)


# ── Coherence feedback (one in-band repair round at the source) ─────────────

_COHERENCE_TYPES = frozenset({
    "identity_collision", "add_exists", "body_contains_heading",
    "dependency_on_new", "invalid_kind",
    "restates_existing", "restates_sibling",
})


def _coherence_feedback(violations: list[Violation]) -> str:
    lines = [
        "Your edit set is valid JSON but structurally incoherent. Fix the "
        "following by re-emitting the COMPLETE corrected JSON object "
        "(same format, all edits — corrected ones and untouched ones):",
    ]
    for v in violations:
        if v.vtype in _COHERENCE_TYPES:
            lines.append(f"- [{v.vtype}] {v.detail}")
    lines.append(
        "Remember: subject is an IDENTITY (add_section invents its own "
        "name from its content's theme); placement carries position; bodies "
        "never contain heading lines.")
    return "\n".join(lines)


# ── Public API ───────────────────────────────────────────────────────────────

def run_merger(
    client: Any,
    rules: str,
    raw_patches: list[RawPatch],
    history_text: str = "",
    *,
    max_tokens: int = 16384,
) -> tuple[list[SectionEdit], list[Violation], list[EditAudit], dict]:
    """Merger call + syntax gate + at most one coherence feedback round.

    Returns (edits, open_violations, audits, stats). Never raises on LLM
    failure; a total failure returns ``([], [], audits, stats)``.
    """
    stats = {"llm_calls": 0, "coherence_rounds": 0, "parsed_edits": 0}
    patches = [rp for rp in raw_patches if rp is not None and rp.patch is not None]
    n_total = sum(
        sum(1 for e in rp.patch.edits if isinstance(e, Edit)) for rp in patches)
    if not patches or n_total == 0:
        _log.info("editpipe.merger: no raw edits to merge")
        return [], [], [], stats

    system = MERGER_SYSTEM + (_PRINCIPLE_HISTORY if history_text else "")
    user = build_merger_user_prompt(rules, raw_patches, history_text)

    _log.info(
        "editpipe.merger: consolidating %d raw edits from %d patches",
        n_total, len(patches))

    try:
        raw_edits = complete_optimizer_json(
            client, system, user,
            parse=parse_merger_output,
            required=required_fields_missing,
            max_tokens=max_tokens,
            stage="merger_v2",
        )
        stats["llm_calls"] += 1  # (+ any internal repair calls, not counted)
    except Exception:
        _log.warning("editpipe.merger: LLM call failed", exc_info=True)
        return [], [], [], stats

    if not raw_edits:
        _log.warning("editpipe.merger: unparseable output")
        return [], [], [], stats
    stats["parsed_edits"] = len(raw_edits)

    doc = RulesDoc.parse(rules)
    edits = [SectionEdit.from_dict(d) for d in raw_edits]
    kept, violations, audits = syntax_gate(edits, doc)
    violations += detect_conflicts(kept, doc)
    violations += detect_restatements(kept, doc)

    # One coherence round, at the interface that produced the problem.
    coherence = [v for v in violations if v.vtype in _COHERENCE_TYPES]
    if coherence:
        stats["coherence_rounds"] = 1
        _log.info(
            "editpipe.merger: %d coherence violation(s); one in-band repair "
            "round", len(coherence))
        repaired_text = repair_json_via_llm(
            client, system, user,
            json.dumps({"edits": [e.to_dict() for e in kept]},
                       ensure_ascii=False),
            max_tokens=max_tokens,
            stage="merger_v2_coherence",
            feedback=_coherence_feedback(coherence),
        )
        repaired = parse_merger_output(repaired_text) if repaired_text else None
        if repaired:
            new_edits = [SectionEdit.from_dict(d) for d in repaired]
            new_kept, new_violations, new_audits = syntax_gate(new_edits, doc)
            new_violations += detect_conflicts(new_kept, doc)
            new_violations += detect_restatements(new_kept, doc)
            # Adopt only if strictly better (fewer coherence violations) and
            # not lossy (comparable or larger edit count).
            old_n = len([v for v in violations if v.vtype in _COHERENCE_TYPES])
            new_n = len([v for v in new_violations
                         if v.vtype in _COHERENCE_TYPES])
            if new_n < old_n and len(new_kept) >= len(kept) - 1:
                audits.append(EditAudit(
                    "*", "*", "normalized",
                    f"coherence repair adopted: {old_n} -> {new_n} coherence "
                    f"violations, {len(kept)} -> {len(new_kept)} edits",
                    "gate"))
                kept, violations = new_kept, new_violations
                audits += new_audits
            else:
                audits.append(EditAudit(
                    "*", "*", "kept",
                    f"coherence repair NOT adopted (violations {old_n} -> "
                    f"{new_n}, edits {len(kept)} -> {len(new_kept)}); "
                    "remaining issues go to adjudication", "gate"))

    _log.info(
        "editpipe.merger: %d raw -> %d parsed -> %d gated edits, "
        "%d open violation(s)",
        n_total, stats["parsed_edits"], len(kept), len(violations))
    return kept, violations, audits, stats
