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
CHANGE-ASPECTS for drafting. You are shown the CURRENT rules document in
full so that every routing decision is made against what the document
actually says, not against titles alone.

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

Placement discipline: route a group into an existing section whenever that
section's theme covers the group's aspect — read the section's actual
content, not just its title, before deciding it does not fit. Two sections
about the same theme is a documentation defect the system then has to pay
for. Reserve NEW for a genuinely uncovered theme, and give it a specific,
content-bearing title.

Output ONLY this JSON object:
{
  "groups": [
    {"ids": ["E#3", "E#9"],
     "aspect": "<a full description of this group's behavioral concern: what
       lesson it teaches, why these raw edits belong together, and why it is
       orthogonal to every other group — write this out properly, it drives
       the drafting>",
     "placement": "<the section this change belongs in: an existing handle
       copied VERBATIM from the document (e.g. S#2), or NEW: <proposed
       section title>>"}
  ]
}
Every raw edit id must appear in exactly one group."""


def build_group_user(raw_render: str, doc_render: str) -> str:
    return (
        "## The current rules document (full, handle-annotated)\n" + doc_render
        + "\n\n## Raw edits to organize (full content)\n" + raw_render
        + "\n\nPartition ALL of them into orthogonal change-aspect groups. "
          "Respond with ONLY the JSON object described."
    )


# ── B: DRAFT — differential drafting of deployable edits ─────────────────────
DRAFT_SYSTEM = """\
You draft the DEFINITIVE edit(s) for one change-aspect of an agent's rules
document, synthesizing a group of raw edits that all serve that aspect.

Your task is DIFFERENTIAL: you are shown the CURRENT rules document in full.
Compute the NET INCREMENT of this group's material over what the document
already teaches — anywhere in the document, not just in the suggested
section — and land that increment at the most precise place. The document's
value comes from stating each lesson exactly once at its most general
formulation; a redundant edit is a defect, and an empty edit list (because
everything is already covered) is a SUCCESS, not a failure.

""" + DSP_NOTE + """

Operations available (handle-addressed):
  * add_section       {"op": "add_section", "section": "NEW: <title>", "content": ...}
  * append_to_section {"op": "append_to_section", "section": "S#k", "content": ...}
  * amend_section     {"op": "amend_section", "section": "S#k", "content": "<the
                       precise correction: name the existing guidance being
                       fixed (quote or describe it), then give the corrected
                       text — do NOT restate the rest of the section>"}
  * replace_section   {"op": "replace_section", "section": "S#k", "content": ...}
  * remove_section    {"op": "remove_section", "section": "S#k", "content": "<reason>"}

Classify EVERY lesson in the material against the whole document, then draft:
  * NOVEL — nothing in the document implies it. append_to_section into the
    section whose theme covers it; add_section only when no section's theme
    fits (never open a second section about an existing theme).
  * INSTANCE-OF — an existing rule already implies it: a general rule covers
    this specific app/endpoint/case even without naming it. Emit NO edit for
    it; record its source ids under "absorbed_as_covered", quoting the
    covering rule. Re-teaching a covered lesson through one more named
    instance is exactly the bloat this stage exists to prevent.
  * REFINES — covered in general, but the material carries an irreducible
    new fact: an app-specific parameter, a boundary condition, an exception,
    a sharper trigger. amend_section carrying ONLY that increment, grafted
    onto the existing rule (typically as an exception/example entry under
    it) — never restate the rule itself.
  * SUPERSEDES — the material proves an existing statement wrong or strictly
    weaker than what is now known. amend_section (or replace_section when
    the section as a whole is being restructured), and quote the superseded
    statement(s) verbatim in "supersedes" so the applier retires them; fold
    anything unique they contained into your content.

Drafting discipline:
  * Usually ONE edit per group. Split only when the aspect genuinely needs
    two operations (e.g. a new section plus a removal elsewhere).
  * A NEW title must name the specific aspect — never a generic bucket
    ("Additional Rules", "Misc", "Other Notes") — and must not carry
    handle text ("[S#4] ...") or numbering.
  * Choose the LIGHTEST sufficient operation: amend to graft a refinement
    onto an existing rule (the applier locates it semantically — you never
    restate the whole section); append for genuinely new guidance in an
    existing theme; replace ONLY when the section as a whole is being
    restructured, and then preserve the meaning and information of
    everything this aspect does not target.
  * AUDIENCE: the content deploys to the task-executing agent, which at run
    time sees only the task and the environment. Evaluation machinery
    (evaluators, verifiers, expected values, how outputs are checked) is
    training-time diagnostic material — never a condition, justification,
    or subject of a rule ("if the evaluator checks X..." is invalid);
    express the lesson through the task's own semantics. Optimization
    bookkeeping (rationales, trajectory references) stays out of content.
  * Content is deployable rule text: give each rule its trigger condition,
    the concrete behavior, and a brief why. Free-form markdown — worked
    examples, code snippets, and correct-vs-wrong contrasts are ENCOURAGED
    where they teach better; write examples SCHEMATIC (placeholder
    sheet/column names, representative values), never verbatim from one
    training task.
  * Synthesize, do not concatenate: the output carries ALL distinct
    information from the sources in the FEWEST statements. When several
    raws teach the same lesson, write its single strongest formulation and
    fold each source's unique specifics into it — never keep parallel
    restatements of one lesson. Every source either contributes to an edit
    (list it in that edit's source_ids), is absorbed as covered, or is
    dropped with its reason.
  * State each background fact (e.g. why a library behaves some way) at
    most ONCE across all your edits — and not at all if the document
    already states it.
  * Do not restate guidance the document already contains — anywhere in it.

Output ONLY this JSON object:
{
  "analysis": "<a full synthesis: what the raw edits share, where they
    differ, what the document already covers, which specifics must survive,
    and how the draft resolves them>",
  "edits": [
    {"op": "<one of the five>",
     "section": "<S#k or NEW: <title>>",
     "content": "<the deployable text (or the removal reason)>",
     "source_ids": ["E#3", "E#9"],
     "rationale": "<how this edit synthesizes its sources' contributions>",
     "delta": {"relation": "novel" | "refines" | "supersedes",
               "vs": "<for refines/supersedes: handle + short quote of the
                 existing statement this edit builds on; empty for novel>",
               "increment": "<one sentence: what this edit adds that the
                 document does not already teach>"},
     "supersedes": ["<verbatim statement(s) this edit retires — usually
       empty>"]}
  ],
  "absorbed_as_covered": [
    {"ids": ["E#4"], "covered_by": "<handle + short quote of the existing
      rule that already implies these sources>"}
  ],
  "dropped_ids": [
    {"id": "E#7", "reason": "<a full statement of why this raw edit's
      content should not survive (harmful / obsolete / not generalizable)>"}
  ]
}
Every group-member id must appear in exactly one edit's source_ids, in one
absorbed_as_covered entry, or in dropped_ids. An empty "edits" list with
every member absorbed as covered is a VALID and often correct output."""


def build_draft_user(
    aspect: str, placement: str, members_render: str, doc_render: str,
    revision_block: str = "",
) -> str:
    return (
        "## The current rules document (full, handle-annotated)\n" + doc_render
        + "\n\n## The change-aspect you are drafting\n" + aspect
        + "\n\n## Suggested placement\n" + (placement or "(none suggested)")
        + "\n\n## The group's raw edits (full content)\n" + members_render
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
    lesson twice, OR restate the same background fact (e.g. why a library
    behaves some way) in more than one place — it belongs where it is most
    load-bearing, stated once.
  * AGAINST the document — redundant_with_doc: an edit (re)states guidance
    the CURRENT document already contains, or teaches one more named
    instance of a general rule the document already states (the rule covers
    the case even without naming it). Cite the section handle and quote the
    existing statement in your explanation. The re-draft must shrink the
    edit to its irreducible increment over that statement (typically an
    amend_section grafting an exception/refinement onto it) — or drop the
    edit if no increment remains.
  * PER draft — leakage: content contains task-specific answers, gold
    values, or references to specific training tasks (the deployed agent
    has no ground truth; this is disqualifying — the edit is removed);
    audience_violation: content conditions on, justifies by, or describes
    the evaluation machinery — evaluators, verifiers, scoring, how outputs
    get checked ("if the evaluator checks X...") — or carries a worked
    example copied verbatim from one training task; the lesson itself is
    sound, so instruct a re-draft that expresses it through the task's own
    semantics and schematic examples; not_actionable: vague slogans with
    no trigger condition or concrete behavior; contradicts_existing: the
    edit contradicts guidance already in the document that no edit in this
    set removes, replaces, or supersedes.

Output ONLY this JSON object:
{
  "pass": true | false,
  "issues": [
    {"ids": ["D#2", "D#5"],
     "type": "semantic_conflict" | "duplicate" | "redundant_with_doc" | "leakage" | "audience_violation" | "not_actionable" | "contradicts_existing",
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
document, producing the section's new text. You are the semantic applier
AND the section's curator: you decide where each edit's content belongs,
how it fuses with what is already there, and you deliver the section in its
MINIMAL COMPLETE form — every distinct lesson stated exactly once, nothing
lost.

""" + DSP_NOTE + """

Application discipline:
  * Treat existing content and incoming edits as EQUAL-RANK material for
    the rewrite. When an edit and an existing passage — or two existing
    passages — teach the same lesson, MERGE them: state the general rule
    once at its strongest formulation, and fold every case-specific fact
    under it as a compact exception/example entry (a general rule plus its
    exceptions — never parallel restatements of one lesson). You are
    explicitly AUTHORIZED to reorganize and merge the section's existing
    bullets while applying.
  * NEVER drop a unique fact. Every API name, parameter, literal value,
    boundary condition, and exception present in the OLD section must
    either survive in the new text (verbatim or strengthened) or be listed
    under "absorbed" (its information merged elsewhere — say where) or
    "dropped" (removed — say why). "dropped" is expected to stay EMPTY in
    normal operation.
  * Honor each edit's operation: append_to_section adds guidance;
    amend_section names a specific existing rule to fix — locate it by
    meaning and correct it in place, leaving unrelated rules untouched;
    replace_section supplies the section's new overall content. An edit
    carrying "supersedes" retires the quoted statement(s): fold anything
    unique they contain into the new formulation and record them under
    "absorbed".
  * Multiple edits land in this same call: arrange them sensibly relative
    to each other and to the existing text; if two edits state the same
    fact, keep it once.
  * The section text is read by the task-executing agent at run time: an
    edit's rationale is routing/bookkeeping context for YOU, never content
    to copy in; evaluation machinery (evaluators, verifiers, expected
    values) is never mentioned in section text.
  * An edit that does not belong in this section, or that contradicts it in
    a way you cannot reconcile, goes to "unapplied" with a full reason —
    NEVER force content in.
  * The new section text is FREE-FORM markdown (no "### " lines); organize
    sub-structure with "#### " and deeper, at most two bullet levels below
    a heading.

Output ONLY this JSON object — notes first, the full text LAST:
{
  "application_notes": "<per edit: where it landed (quote the neighboring
    existing text), how it was fused, and any merges performed on existing
    content>",
  "unapplied": [{"id": "A#2", "reason": "<full reason>"}],
  "absorbed": [{"old": "<short quote of the pre-existing or superseded
    statement>", "into": "<where its information now lives>"}],
  "dropped": [{"text": "<what was removed>", "reason": "<why>"}],
  "new_section_text": "<the COMPLETE new section content>"
}"""


def build_applier_user(section_title: str, section_body: str,
                       edits_render: str, curation_note: str = "") -> str:
    return (
        "## Section: %s\n" % section_title
        + (section_body.strip() or "(the section is currently empty)")
        + "\n\n## Edits to apply to THIS section\n" + edits_render
        + curation_note
        + "\n\nProduce the section's new text. Respond with ONLY the JSON "
          "object described."
    )


def build_curation_note(n_bullets: int, budget: int) -> str:
    """Over-budget trigger signal injected into the applier call (never a cap:
    the instruction is to MERGE duplicate lessons, not to cut content)."""
    return (
        "\n\n## Curation signal\n"
        "This section exceeds its bullet budget (%d top-level bullets > %d):"
        " it has accumulated redundant restatements over many steps. While"
        " applying, consolidate aggressively — merge duplicate lessons into"
        " single general rules with exception entries. Merge, never truncate:"
        " every unique fact must survive." % (n_bullets, budget)
    )


def build_applier_repair_user(section_title: str, section_body: str,
                              edits_render: str, prev_json: str,
                              missing: "list[str]") -> str:
    """Lossless-repair retry: the previous fusion lost identifiers that were
    neither kept nor declared under absorbed/dropped."""
    return (
        "## Section: %s\n" % section_title
        + (section_body.strip() or "(the section is currently empty)")
        + "\n\n## Edits to apply to THIS section\n" + edits_render
        + "\n\n=== LOSSLESS REPAIR REQUIRED ===\n"
          "Your previous output (below) LOST the following identifiers from"
          " the old section: they appear neither in new_section_text nor in"
          " any absorbed/dropped entry. Re-produce the COMPLETE output"
          " object, keeping your fusion but restoring each lost identifier"
          " — either weave its fact back into the text or declare it under"
          " absorbed/dropped with its destination/reason.\n"
          "\n## Lost identifiers\n"
        + "\n".join("- `%s`" % m for m in missing)
        + "\n\n## Your previous output\n" + prev_json
        + "\n\nRespond with ONLY the corrected JSON object."
    )


# ── CONSOLIDATE: burst-end whole-document tidy-up ────────────────────────────
CONSOLIDATE_SYSTEM = """\
You are the curator of an agent's rules document. The document has grown
through many small accepted edits, each reasonable alone; your job is the
periodic TIDY-UP: deliver the document in its MINIMAL COMPLETE form without
changing what it teaches. You are the only stage that sees the whole
document with the authority to reorganize it — use that authority.

""" + DSP_NOTE + """

What to fix — the known growth defects, in priority order:
  * Instance pile-up: one lesson restated once per app/endpoint/case
    ("paginate contacts", "paginate voice messages", "paginate the feed"
    as separate rules). Rewrite as ONE general rule stating the trigger
    and the behavior, with the case-specific facts kept as compact
    exception/example entries under it. Every case-specific fact that adds
    information (a parameter value, a boundary, an exception) survives;
    only the repeated restatements go.
  * Cross-section duplication: the same guidance or background fact stated
    in several sections. State it ONCE in the section where it is most
    load-bearing; other sections keep at most a one-line pointer to it.
  * Theme splits: two sections about one theme — including a title that
    duplicates another up to a leaked handle prefix ("[S#6] X" next to
    "X"). Merge them into ONE section with a clean title.
  * Structural debris: empty sections; sections whose title is leaked
    protocol text; orphaned fragments (a "Step 6:" with no steps 1-5);
    stray sub-topic sections that belong inside a parent (a bare "Why?" or
    "Example" section) — fold them where they belong.
  * Audience violations: statements that condition on, justify by, or
    describe evaluation machinery (evaluators, verifiers, scoring scripts,
    "the evaluator expects X"). REWRITE each into the task's own semantics,
    preserving the behavioral content — e.g. "the evaluator expects a raw
    number" becomes "pass the raw number itself, never a formatted string".
  * Training-data residue: literal emails, person names, dates, or amounts
    from specific training tasks used in examples. Replace with schematic
    placeholders (user@example.com, 2024-01-15, representative values),
    keeping the example's structure and point.
  * Code-block sprawl: several near-identical snippets teaching one
    skeleton. Keep the single most complete pattern; fold each variant's
    unique lines into it or into a one-line note under it.
  * Deep nesting: bullets nested four or more levels. Flatten to at most
    two bullet levels, using "#### " sub-headings for the top split.

Hard limits — all binding:
  * NEVER invent a rule, change what a rule commands, or alter its trigger
    conditions. This is reorganization, not authorship.
  * NEVER drop a unique fact: every API name, parameter, literal value,
    boundary condition, and exception in the input must survive somewhere
    in the output — or be listed in "dropped_facts" (expected EMPTY).
  * Keep the document's imperative, agent-addressed style; keep worked
    examples that teach (schematized); drop only true duplicates.

Where to spend your effort: the mechanical size signals below name the
sections that accumulated the bloat — those are this tidy-up's MANDATORY
targets. A section named in the size signals must appear in a "rewrite" or
"merge" decision, never in "keep": keeping it preserves the exact defect
this stage exists to remove. Sections NOT named there are usually healthy —
keep them unless they participate in a cross-section merge. Being surgical
about healthy sections is the virtue; being conservative about the named
ones is the defect.

Output ONLY this JSON object. Every input handle must appear in EXACTLY ONE
decision; order the "sections" list as the document should read afterwards:
{
  "plan": "<brief: the main merges and rewrites you will perform and why>",
  "sections": [
    {"op": "keep",    "handles": ["S#3"]},
    {"op": "rewrite", "handles": ["S#7"], "title": "<title>",
     "body": "<the full new body>"},
    {"op": "merge",   "handles": ["S#2", "S#9"], "title": "<title>",
     "body": "<the full merged body>"},
    {"op": "delete",  "handles": ["S#11"], "reason": "<why nothing of value
      is lost>"}
  ],
  "dropped_facts": ["<any unique fact you could not place — expected EMPTY>"]
}
"keep" sections carry NO body — their text is preserved verbatim. That is
what frees your output budget for DEEP rewrites of the named targets: a
68-bullet section rewritten to its minimal complete form is the single most
valuable thing this call can produce."""


def build_consolidate_repair_user(doc_render: str, size_note: str,
                                  prev_plan_json: str,
                                  lost: "list[str]",
                                  kept_over_budget: "list[str]") -> str:
    """Quality-repair retry: the previous plan lost identifiers and/or kept
    the very sections the size signals named as mandatory targets."""
    problems: "list[str]" = []
    if lost:
        problems.append(
            "It LOST these identifiers — they appear in the input document"
            " but neither in any new body nor in dropped_facts. Weave each"
            " one's fact back into the relevant rewritten/merged body (or"
            " declare it in dropped_facts with justification):\n"
            + "\n".join("- `%s`" % x for x in lost))
    if kept_over_budget:
        problems.append(
            "It KEPT these mandatory targets unchanged — the size signals"
            " name them as the bloat carriers, so they must be rewritten"
            " (or merged), stating each lesson once with case-specific"
            " facts as exception entries:\n"
            + "\n".join("- %s" % x for x in kept_over_budget))
    return (
        "## The rules document to tidy up (full, handle-annotated)\n"
        + doc_render
        + "\n\n## Mechanical size signals\n" + size_note
        + "\n\n=== QUALITY REPAIR REQUIRED ===\n"
          "Your previous plan (below) is structurally valid but fails the"
          " tidy-up's quality bar:\n\n"
        + "\n\n".join(problems)
        + "\n\n## Your previous plan\n" + prev_plan_json
        + "\n\nProduce the COMPLETE corrected plan (same JSON object, every"
          " handle in exactly one decision). Respond with ONLY the JSON"
          " object described."
    )


def build_consolidate_user(doc_render: str, size_note: str = "") -> str:
    return (
        "## The rules document to tidy up (full, handle-annotated)\n"
        + doc_render
        + (("\n\n## Mechanical size signals\n" + size_note) if size_note
           else "")
        + "\n\nTidy up the document. Respond with ONLY the JSON object "
          "described."
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
