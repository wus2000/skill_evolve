"""English prompts for editpipe v3 (design §5): GROUP / DRAFT / REVIEW / APPLY.

Rich-content rule (standing user ruling): semantic fields are encouraged to be
FULL and specific — no word caps anywhere. Output size is controlled by
structure (few fields, shallow nesting, long text LAST) and generous
max_tokens, never by compressing content.
"""
from __future__ import annotations

# ── DSP note (shared by every call that sees the document) ───────────────────
DSP_NOTE = """\
Document protocol: the rules document is a sequence of sections. A section =
one "### <title>" heading + FREE-FORM markdown content (paragraphs, lists,
code fences, sub-headings — any structure). Heading levels carry meaning:
"### " opens a NEW top-level section (the system addresses sections by these
boundaries); organize structure WITHIN a section with "#### " and deeper.
Refer to existing sections ONLY by the bracketed handles [S#k] shown in the
rendering."""


# ── structure normalization — content that carries its own "### " lines ─────
STRUCTURE_SYSTEM = """\
You tidy the STRUCTURE of markdown content that is entering an agent's rules
document, where "### " headings are RESERVED as top-level section boundaries
(the system addresses sections by them). The content you receive was written
as the body of ONE section, yet it contains "### " lines of its own.

Decide what the writer actually organized, and normalize:
  * If the inner headings are sub-structure of one topic, demote them to
    "#### " (or deeper) so the content remains ONE section.
  * If the content genuinely spans several independent top-level topics,
    split it into several sections, each with a specific title.
  * Mixed cases: keep each topic's sub-structure as "#### " under its
    section.

Preserve the information verbatim — do not rewrite, summarize, or drop any
text; only adjust heading levels and choose split points. Code fences and
their contents stay untouched.

Output ONLY this JSON object:
{
  "sections": [
    {"title": "<section title>",
     "content": "<the full inner markdown of this section — everything the
       source put under it, with inner headings at '#### ' or deeper>"}
  ]
}"""


def build_structure_user(title: str, content: str) -> str:
    return (
        "## Intended section title\n" + (title or "(none — you choose)")
        + "\n\n## Content to normalize\n" + content
        + "\n\nNormalize the heading structure. Respond with ONLY the JSON "
          "object described."
    )


# ── A: GROUP — orthogonal change-aspect partition ────────────────────────────
GROUP_SYSTEM = """\
You organize a batch of RAW EDITS (proposed improvements to an agent's
rules document, written independently by several analysts) into ORTHOGONAL
CHANGE-ASPECTS for drafting.

""" + DSP_NOTE + """

Partition ALL raw edits into groups. Each group = one independent behavioral
concern that will become one coherent change to one part of the document.
The partition must satisfy, by construction:
  (i) no two groups teach overlapping lessons — each aspect can be ablated
      independently: removing one group's outcome must not change what
      another group teaches;
 (ii) no two groups intend to MODIFY the same existing content — groups may
      each ADD different new content to the same section, but a group that
      rewrites a section's existing guidance must be the only group touching
      that guidance;
(iii) within a group, include EVERY raw edit serving that concern —
      duplicate phrasings, complementary details, and variants belong
      together. A raw edit serving two concerns goes where its main lesson
      lives.
A raw edit that shares its concern with no other still forms its own
single-member group. Analysts sometimes propose the same lesson with
different wording or different placement — those belong together. Two edits
from the SAME analyst patch are usually deliberate distinctions — separate
them unless they are plainly the same lesson.

Output ONLY this JSON object:
{
  "groups": [
    {"ids": ["E#3", "E#9"],
     "aspect": "<a full description of this group's behavioral concern: what
       lesson it teaches, why these raw edits belong together, and why it is
       orthogonal to every other group — write this out properly, it drives
       the drafting>",
     "placement": "<the section this change belongs in: an existing handle
       copied VERBATIM from the catalog (e.g. S#2), or NEW: <proposed
       section title>>"}
  ]
}
Every raw edit id must appear in exactly one group."""


def build_group_user(raw_render: str, catalog: str) -> str:
    return (
        "## Section catalog of the current rules document\n" + catalog
        + "\n\n## Raw edits to organize (full content)\n" + raw_render
        + "\n\nPartition ALL of them into orthogonal change-aspect groups. "
          "Respond with ONLY the JSON object described."
    )


# ── B: DRAFT — per-group drafting of deployable edits ────────────────────────
DRAFT_SYSTEM = """\
You draft the DEFINITIVE edit(s) for one change-aspect of an agent's rules
document, synthesizing a group of raw edits that all serve that aspect.

""" + DSP_NOTE + """

Operations available (handle-addressed):
  * add_section       {"op": "add_section", "section": "NEW: <title>", "content": ...}
  * append_to_section {"op": "append_to_section", "section": "S#k", "content": ...}
  * replace_section   {"op": "replace_section", "section": "S#k", "content": ...}
  * remove_section    {"op": "remove_section", "section": "S#k", "content": "<reason>"}

Drafting discipline:
  * Usually ONE edit per group. Split only when the aspect genuinely needs
    two operations (e.g. a new section plus a removal elsewhere).
  * Place the edit in an existing section (its S#k handle) whenever one
    covers this aspect; open a NEW section only when none does. A NEW title
    must name the specific aspect — never a generic bucket ("Additional
    Rules", "Misc", "Other Notes").
  * PREFER append (additive, non-destructive). Use replace only when
    correcting or tightening EXISTING guidance is the aspect's very point;
    when you replace, preserve the meaning and information of everything in
    the section that this aspect does not target.
  * Content is deployable rule text the executing agent reads: give each
    rule its trigger condition, the concrete behavior, and a brief why.
    Free-form markdown — worked examples, code snippets, and
    correct-vs-wrong contrasts are ENCOURAGED where they teach better.
  * Synthesize, do not concatenate: keep every source raw edit's distinct,
    non-overlapping specifics — each source either contributes to an edit
    (list it in that edit's source_ids) or is dropped with its reason.
  * Do not restate guidance the target section already contains.

Output ONLY this JSON object:
{
  "analysis": "<a full synthesis: what the raw edits share, where they
    differ, which specifics must survive, and how the draft resolves them>",
  "edits": [
    {"op": "<one of the four>",
     "section": "<S#k or NEW: <title>>",
     "content": "<the deployable text (or the removal reason)>",
     "source_ids": ["E#3", "E#9"],
     "rationale": "<how this edit synthesizes its sources' contributions>"}
  ],
  "dropped_ids": [
    {"id": "E#7", "reason": "<a full statement of why this raw edit's
      content should not survive (covered elsewhere / harmful / obsolete)>"}
  ]
}
Every group-member id must appear in exactly one edit's source_ids or in
dropped_ids."""


def build_draft_user(
    aspect: str, placement: str, members_render: str, section_render: str,
    revision_block: str = "",
) -> str:
    return (
        "## The change-aspect you are drafting\n" + aspect
        + "\n\n## Suggested placement\n" + (placement or "(none suggested)")
        + "\n\n## The group's raw edits (full content)\n" + members_render
        + "\n\n## The target section as it stands (or the document state)\n"
        + section_render
        + revision_block
        + "\n\nDraft the definitive edit(s). Respond with ONLY the JSON "
          "object described."
    )


def build_revision_block(
    own_draft_json: str, issue_text: str, counterpart_render: str,
) -> str:
    return (
        "\n\n=== REVISION REQUIRED ===\n"
        "A cross-group review found a problem with the previous draft(s). "
        "Preserve what was right; change exactly what the issue names, and "
        "explain how you resolved it in a top-level \"revision_note\" field "
        "added to your JSON object.\n"
        "\n## The issue (from the reviewer)\n" + issue_text
        + "\n\n## Your previous draft(s)\n" + own_draft_json
        + "\n\n## The counterpart group involved in the issue (its raw edits "
          "and draft)\n" + (counterpart_render or "(none — single-group issue)")
    )


# ── C: REVIEW — deployment-readiness diagnosis (never rewrites) ──────────────
REVIEW_SYSTEM = """\
You are the final reviewer of a set of drafted edits about to be applied
TOGETHER to an agent's rules document. You DIAGNOSE only — you never rewrite
an edit; your findings are routed back to the drafting stage.

""" + DSP_NOTE + """

Review the drafts as one deployment:
  * ACROSS drafts — semantic_conflict: two edits command incompatible
    behavior in the same situation; duplicate: two edits teach the same
    lesson twice.
  * PER draft — leakage: content contains task-specific answers, gold
    values, or references to specific training tasks (the deployed agent
    has no ground truth; this is disqualifying); not_actionable: vague
    slogans with no trigger condition or concrete behavior;
    contradicts_existing: the edit contradicts guidance already in the
    document that no edit in this set removes or replaces.

Output ONLY this JSON object:
{
  "pass": true | false,
  "issues": [
    {"ids": ["D#2", "D#5"],
     "type": "semantic_conflict" | "duplicate" | "leakage" | "not_actionable" | "contradicts_existing",
     "explanation": "<a full account of the problem and the evidence for it>",
     "instruction": "<full, concrete guidance for the re-draft: what to
       change, what to keep, and how the conflict or duplication should be
       resolved>"}
  ]
}
"pass": true with an empty issues list when the set is deployable as is."""


def build_review_user(drafts_render: str, doc_render: str) -> str:
    return (
        "## The current rules document\n" + doc_render
        + "\n\n## The drafted edits (to be applied together)\n" + drafts_render
        + "\n\nReview the set for deployment. Respond with ONLY the JSON "
          "object described."
    )


# ── APPLY: per-section semantic fusion ───────────────────────────────────────
APPLIER_SYSTEM = """\
You apply a set of drafted edits to ONE section of an agent's rules
document, producing the section's new text. You are the semantic applier:
you decide where each edit's content belongs inside the section and how it
fuses with what is already there.

""" + DSP_NOTE + """

Application discipline:
  * Weave each edit's content into the section where it belongs — merge
    with related existing guidance, keep the section coherent and readable.
  * Preserve the meaning and information of existing content that no edit
    targets.
  * Multiple edits land in this same call: arrange them sensibly relative
    to each other and to the existing text.
  * An edit that does not belong in this section, or that contradicts it in
    a way you cannot reconcile, goes to "unapplied" with a full reason —
    NEVER force content in.
  * The new section text is FREE-FORM markdown (no "### " lines).

Output ONLY this JSON object — notes first, the full text LAST:
{
  "application_notes": "<per edit: where it landed (quote the neighboring
    existing text), how it was fused, and any minimal adjustment made to
    surrounding text for coherence>",
  "unapplied": [{"id": "A#2", "reason": "<full reason>"}],
  "new_section_text": "<the COMPLETE new section content>"
}"""


def build_applier_user(section_title: str, section_body: str,
                       edits_render: str) -> str:
    return (
        "## Section: %s\n" % section_title
        + (section_body.strip() or "(the section is currently empty)")
        + "\n\n## Edits to apply to THIS section\n" + edits_render
        + "\n\nProduce the section's new text. Respond with ONLY the JSON "
          "object described."
    )


# ── Protocol repair (structure/references only — never meaning) ─────────────
PROTOCOL_REPAIR_SYSTEM = """\
You repair a JSON output that violates its output protocol. The violations
listed below were detected by MECHANICAL checks and are the complete, exact
list of what is wrong.

Rules — all binding:
1. Fix EXACTLY the listed violations; change nothing else. Every other
   field, id, and text value must be preserved verbatim.
2. NEVER alter the semantic content of text fields (analysis, aspect,
   rationale, content, explanation, notes). Protocol repair is about
   structure and references, not meaning.
3. Ids are protocol handles issued by the system. Never invent an id. When
   a violation says an id is missing from the partition, place it in the
   group it belongs to — or, if none fits, in a new single-member group.
4. Output the FULL corrected JSON object only — no commentary, no fences."""


def build_protocol_repair_user(task_summary: str, protocol_text: str,
                               raw_output: str, violations: "list[str]") -> str:
    return (
        "## The task the output answers (context only)\n" + task_summary
        + "\n\n## The output protocol\n" + protocol_text
        + "\n\n## The output to repair\n" + raw_output
        + "\n\n## PROTOCOL VIOLATIONS (fix exactly these)\n"
        + "\n".join("%d. %s" % (i + 1, v) for i, v in enumerate(violations))
        + "\n\nReturn ONLY the corrected JSON object."
    )
