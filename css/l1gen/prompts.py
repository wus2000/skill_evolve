"""English deployment prompts for the NEW and REFINE generation pipelines.

Mission-first style throughout. Two altitude/purity blocks are PORTED from the
retiring ``css.proposal`` package (noted inline):

  * the "what a strategy document IS (and is NOT)" firewall — from
    ``css.proposal.derivation._DERIVE_SYSTEM`` — adapted here to the design's
    ``## <behavioral mechanism>`` section format (the operable unit REFINE edits);
  * the four-criteria Altitude Screen — from design §2.3 — reused as the per-op
    screen in REFINE confrontation and the single-document final check.

Nothing is imported from ``css.proposal``; the useful text is reproduced so this
package stands alone once that package is retired.
"""
from __future__ import annotations

# ── Altitude firewall (ported from css.proposal.derivation._DERIVE_SYSTEM) ────
# Adapted: the old firewall mandated a "## Name + ### Details" shape; the tree
# design mandates flat ``## <behavioral mechanism>`` sections instead, because
# those sections are the operable unit a later REFINE edits under byte-identity.
_STRATEGY_ALTITUDE_FIREWALL = """\
CRITICAL — what a strategy document IS (and is NOT). This is an L1 COGNITIVE \
STRATEGY: it describes an overall WAY OF BEHAVING an executor agent should adopt \
— how it approaches, frames, and works through a task — NOT a checklist of \
low-level tactical rules and NOT step-by-step task instructions. Get the ALTITUDE \
right or the document is worthless.

Structure the document as flat ``## <behavioral mechanism>`` sections:
  * Each ``## `` heading names one behavioral mechanism — a coherent way of \
    behaving (e.g. "## Ground Every Claim in a Verified Read", "## Decompose \
    Before Committing"). The heading is the mechanism's stable identity: a later \
    controlled edit operates on whole named sections, so name each for what it \
    DOES behaviorally.
  * Under each heading, write deployable guidance in full paragraphs: what this \
    way of behaving is, when it engages, and how it shapes the agent's choices. \
    A short preamble before the first ``## `` is allowed for framing.

Hard constraints:
  * Every instruction describes a METHOD (how to behave / think), never a GOAL \
    (what score to hit) and never a bare prohibition — prohibitions are L0 \
    tactical rules, not strategy.
  * No task-specific content: no concrete task ids, no gold answers, no \
    dataset-specific values, no "on task X do Y". The document must generalize \
    across the whole domain.
  * Deployable voice only: it is read by an executor agent AS ITS INSTRUCTIONS. \
    No meta-commentary about the design process, no "this strategy was chosen \
    because…", no references to prior strategies, experiments, or this pipeline."""


# ══════════════════════════════════════════════════════════════════════════════
# NEW pipeline
# ══════════════════════════════════════════════════════════════════════════════

NEW_TARGET_SYSTEM = """\
=== YOUR MISSION ===
A new behavioral strategy is about to be designed for this task domain from a \
blank page. It must succeed where every existing strategy has failed. Your job in \
this step is to choose WHAT it should target and to explain, as verified \
understanding, why every paradigm tried so far fails there.

You are given the complete state of the search: every strategy node's own \
documents (its strategy text, why it was created, its behavior profile, and its \
frontier analysis of where it still fails), the cross-strategy synthesis of \
still-unsolved task groups, and any exploration findings gathered ahead of this \
design. Read it as a designer reads prior art: absorb what has been tried, locate \
the task group whose resistance is both important (large / high-leverage) and \
genuinely unexplained by the current paradigms.

Think at strategy altitude: you are choosing a class of behavior to invent, not a \
task to hand-fix. Do not propose fixes here — only name the target and diagnose \
the collective failure.

Output ONLY a JSON object:
  {
    "target_group": "<the task group / failure family the new strategy should \
crack, named and briefly characterized>",
    "why_all_paradigms_fail": "<several paragraphs: for each existing paradigm, \
the behavioral reason it cannot solve this group — grounded in the profiles and \
frontier analyses you were given, and in exploration findings where present. \
State it as understanding, not speculation.>",
    "leverage": "<one paragraph: why cracking this group is worth a new paradigm \
(size, transfer, what it unlocks)>"
  }
No prose outside the JSON."""

NEW_TARGET_USER = """\
ALL STRATEGY NODES (full dossiers; '(not yet available)' means the document does \
not exist yet):
-------------------------------------------------------------
{node_dossiers}
-------------------------------------------------------------

GLOBAL UNSOLVED SYNTHESIS (cross-strategy; task groups no paradigm solves):
-------------------------------------------------------------
{global_unsolved}
-------------------------------------------------------------

EXPLORATION FINDINGS (probe experiments run ahead of this design; may be empty):
-------------------------------------------------------------
{findings}
-------------------------------------------------------------

Choose the target group and diagnose why every current paradigm fails there. \
Respond with ONLY the JSON object described in the instructions."""


NEW_CONCEPT_SYSTEM = """\
=== YOUR MISSION ===
You are designing the CORE of a new behavioral strategy for the task domain. A \
target group and a diagnosis of why all current paradigms fail there have already \
been established. Conceive the new way of behaving that would actually break that \
failure — a whole behavioral paradigm, not a patch on an existing one.

Requirements for the conception:
  * It must be a genuinely different WAY OF BEHAVING, not a re-description of an \
    existing strategy. Name the single core behavioral commitment that defines it.
  * For each existing strategy, state concretely how this differs in behavioral \
    substance — what the agent would visibly do differently.
  * Explain the mechanism: why this way of behaving would resolve the diagnosed \
    failure, step by step at the behavioral level.

If the exploration findings feature a probe that achieved a first-ever pass on an \
unsolved task, treat its behavior prompt as a PROTOTYPE SEED: generalize the \
behavioral element that made the difference into the core commitment, rather than \
inventing from scratch.

Stay at strategy altitude — describe ways of behaving, not step-level task fixes.

Output ONLY a JSON object:
  {
    "core_behavioral_commitment": "<the one defining way of behaving, stated \
crisply>",
    "how_it_differs_from_each_prior": [
      {"node_id": "<id>", "difference": "<the behavioral-substance difference>"}
    ],
    "expected_mechanism": "<paragraphs: why this behavior resolves the diagnosed \
failure, at the behavioral level; cite the prototype seed probe if you used one>"
  }
No prose outside the JSON."""

NEW_CONCEPT_USER = """\
TARGET GROUP AND FAILURE DIAGNOSIS (from the previous step):
-------------------------------------------------------------
{target_selection}
-------------------------------------------------------------

EXISTING STRATEGIES (differentiate from each in behavioral substance):
-------------------------------------------------------------
{prior_strategies}
-------------------------------------------------------------

EXPLORATION FINDINGS (prototype-seed source if a probe cracked an unsolved task):
-------------------------------------------------------------
{findings}
-------------------------------------------------------------
{critique}
Conceive the new behavioral paradigm. Respond with ONLY the JSON object described."""


NOVELTY_SYSTEM = """\
You are an INDEPENDENT critic guarding against reinventing a paradigm the search \
already has. You are given a NEW conception and EVERY historical strategy's text. \
Compare on BEHAVIORAL SUBSTANCE — the actual way of behaving the agent would \
adopt — not on surface wording. Two conceptions that read differently but induce \
the same behavior are duplicates; two that read alike but differ in the core \
behavioral commitment, its trigger, or its mechanism are distinct.

Decide whether the conception is behaviorally novel against ALL of them.
  * novel: no historical strategy commits the agent to the same core behavior for \
    the same reason.
  * duplicate: it is, in substance, one of them — name which node, and why.

Output ONLY a JSON object:
  {"novel": true|false, "duplicates": "<node_id or ''>", "reason": "<the \
behavioral-substance comparison that justifies the verdict>"}
No prose outside the JSON."""

NOVELTY_USER = """\
NEW CONCEPTION:
-------------------------------------------------------------
{conception}
-------------------------------------------------------------

HISTORICAL STRATEGIES (compare against every one):
-------------------------------------------------------------
{prior_strategies}
-------------------------------------------------------------

Is the conception behaviorally novel against every historical strategy? Respond \
with ONLY the JSON object described."""


DRAFTING_SYSTEM = """\
You are writing the deployable strategy document for a newly conceived behavioral \
paradigm. The document is read by an executor agent AS ITS INSTRUCTIONS while it \
solves tasks — it is not a design memo. Turn the conception into thorough, \
deployable guidance.

{firewall}

Also produce a rationale (a SEPARATE record, never shown to the executor) that \
ties the document back to its purpose.

Output ONLY a JSON object:
  {{
    "strategy_md": "<the full strategy document: flat '## <behavioral mechanism>' \
sections (an optional short preamble before the first '##'), deployable voice, no \
meta-commentary>",
    "rationale": {{
      "target_problem": "<the target group / failure this paradigm attacks>",
      "idea_sources": "<where the design came from — the diagnosis, and any \
exploration probe or finding it generalizes (reference them)>",
      "expected_behavior_changes": ["<a concrete behavioral change this document \
should produce in the agent, versus the failing paradigms>", "..."]
    }}
  }}
No prose outside the JSON."""

DRAFTING_USER = """\
CONCEPTION (the paradigm to write up):
-------------------------------------------------------------
{conception}
-------------------------------------------------------------

TARGET AND DIAGNOSIS (what it must solve):
-------------------------------------------------------------
{target_selection}
-------------------------------------------------------------

EXPLORATION FINDINGS (reference any prototype-seed probe you build on):
-------------------------------------------------------------
{findings}
-------------------------------------------------------------

Write the deployable strategy document and its rationale. Respond with ONLY the \
JSON object described."""


# ══════════════════════════════════════════════════════════════════════════════
# Shared altitude / purity screens (design §2.3)
# ══════════════════════════════════════════════════════════════════════════════

ALTITUDE_SCREEN_SYSTEM = """\
You are the ALTITUDE SCREEN — a reusable judge that keeps generated content at \
strategy altitude and free of task-specific leakage. You are given one or more \
numbered pieces of candidate strategy content. Judge EACH against four criteria:
  1. SUBJECT: it describes a WAY OF BEHAVING / an approach, not a specific action \
     at a specific step.
  2. GENERALITY: its conclusions generalize across the domain, not bound to one \
     task's incidental details.
  3. SUFFICIENCY: it explains the behavioral mechanism (why this way of behaving \
     works), rather than merely labelling it.
  4. NO TACTICAL PRESCRIPTION: it contains no "at step X do Y" execution recipe \
     and no task-specific ids / gold answers / dataset values.

For each piece output a verdict:
  * "pass"   — meets all four.
  * "revise" — fixable at altitude; say specifically what to change.
  * "reject" — fundamentally tactical / task-bound.

Output ONLY a JSON array, one object per input piece, IN ORDER:
  [{"index": <int>, "verdict": "pass"|"revise"|"reject", "violated_criteria": \
[<int>, ...], "feedback": "<one paragraph, specific to a sentence>", \
"quoted_offense": "<the offending substring, or ''>"}]
No prose outside the JSON array."""

ALTITUDE_SCREEN_USER = """\
CANDIDATE PIECES (judge each by its index):
-------------------------------------------------------------
{pieces}
-------------------------------------------------------------

Screen every piece at strategy altitude. Respond with ONLY the JSON array described."""


ALTITUDE_PURITY_SYSTEM = """\
You are the final ALTITUDE + PURITY gate on a complete strategy document before \
it is deployed to an executor agent. Judge the WHOLE document against four \
criteria:
  1. ALTITUDE: it is a way-of-behaving strategy, not a checklist of tactical \
     rules or step-by-step task instructions.
  2. GENERALITY: nothing is bound to a single task's incidental details.
  3. PURITY: no task-specific ids, no gold/reference answers, no dataset-specific \
     values leaked into the deployable text.
  4. DEPLOYABLE VOICE: it reads as instructions to the executor — no \
     meta-commentary about the design process or prior strategies.

Output ONLY a JSON object:
  {"verdict": "pass"|"revise"|"reject", "violated_criteria": [<int>, ...], \
"feedback": "<specific, sentence-level guidance for a single repair pass>", \
"quoted_offense": "<the worst offending substring, or ''>"}
No prose outside the JSON object."""

ALTITUDE_PURITY_USER = """\
STRATEGY DOCUMENT:
-------------------------------------------------------------
{strategy}
-------------------------------------------------------------

Apply the altitude + purity gate to the whole document. Respond with ONLY the \
JSON object described."""


ALTITUDE_REPAIR_SYSTEM = """\
You are repairing a strategy document that failed the altitude + purity gate. You \
are given the document and the gate's specific feedback. Rewrite ONLY what the \
feedback flags: lift tactical/task-bound passages to strategy altitude, remove any \
task-specific ids / gold answers / dataset values, and strip meta-commentary — \
while preserving the document's behavioral paradigm and its ``## <behavioral \
mechanism>`` section structure. Do not weaken or redesign the strategy; only fix \
the altitude/purity defects.

{firewall}

Output ONLY a JSON object:
  {{"strategy_md": "<the repaired full strategy document, same paradigm, same \
section structure, defects removed>"}}
No prose outside the JSON object."""

ALTITUDE_REPAIR_USER = """\
STRATEGY DOCUMENT (repair in place):
-------------------------------------------------------------
{strategy}
-------------------------------------------------------------

GATE FEEDBACK (fix exactly this):
-------------------------------------------------------------
{feedback}
-------------------------------------------------------------

Return the repaired document. Respond with ONLY the JSON object described."""


# ══════════════════════════════════════════════════════════════════════════════
# REFINE pipeline
# ══════════════════════════════════════════════════════════════════════════════

REFINE_CAUSE_SYSTEM = """\
=== YOUR MISSION ===
You are deciding whether — and how — to improve an existing strategy by a \
CONTROLLED edit at strategy altitude. The strategy succeeds broadly but fails on \
some task groups. You are given this node's full dossier, a machine attribution of \
each failing group, the strategy's "expectation fulfillment" read from its \
behavior profile, and the dossiers of its sibling strategies (the parent's other \
children — their fates).

Each failing group is attributed as:
  * A — a MISSING behavioral mechanism: the strategy does not provide a way of \
    behaving the group needs. This is a legitimate target for a mechanism-level \
    edit.
  * B — an EXPRESSION failure: the strategy says the right thing but the agent \
    does not carry it out (check it against the adherence read). The target is a \
    re-expression of the SAME mechanism, not a new one.
  * U — no defensible strategy-level cause established: the approach itself looks \
    sound and the failure is local execution, or the evidence is insufficient to \
    name an A/B cause. U groups are NOT edit targets.

Pick ONE defensible A or B target if one exists. Also produce a KEEP-LIST: the \
names of the strategy's ``## `` sections that the behavior profile credits with \
its strengths — these must stay byte-identical, so they must NOT be edited.

Be strict: do NOT dress a local execution detail up as a missing mechanism just \
to have something to edit. If there is no defensible A or B target, say so.

Output ONLY a JSON object:
  {
    "has_target": true|false,
    "target_type": "A"|"B"|"",
    "target_group": "<the failing group you are targeting, or ''>",
    "cause": "<the behavioral cause, argued from the failure narrative: for A, the \
missing mechanism; for B, what is written vs what the agent actually does>",
    "sections_implicated": ["<existing '## ' section name to edit, or a proposed \
new mechanism name for an A-type add>", "..."],
    "keep_list": ["<'## ' section name credited with a strength — do not edit>", \
"..."],
    "no_target_reason": "<if has_target is false, why no defensible A/B target \
exists>"
  }
No prose outside the JSON."""

REFINE_CAUSE_USER = """\
THIS NODE'S DOSSIER (strategy + rationale + behavior profile + frontier analysis):
-------------------------------------------------------------
{node_dossier}
-------------------------------------------------------------

FRONTIER ATTRIBUTION (per failing group: A/B/U + task ids + summary):
-------------------------------------------------------------
{frontier_attribution}
-------------------------------------------------------------

SIBLING DOSSIERS (the parent's other children — avoid repeating a dead sibling):
-------------------------------------------------------------
{sibling_dossiers}
-------------------------------------------------------------

EXPLORATION FINDINGS (present only on the retry after exploring U groups; may be empty):
-------------------------------------------------------------
{findings}
-------------------------------------------------------------

Select a defensible A or B target, or declare none. Respond with ONLY the JSON \
object described."""


REFINE_PLAN_SYSTEM = """\
You are producing a CONTROLLED EDIT PLAN over a strategy document organized as \
flat ``## <behavioral mechanism>`` sections. A cause has been confirmed (an A-type \
missing mechanism, or a B-type expression failure) and a keep-list of sections \
that must NOT be touched. You edit whole named sections only; every section you do \
not name stays byte-identical.

The edit vocabulary (there is deliberately NO whole-document rewrite — a whole \
paradigm change is a different operation, not a refine):
  * replace_section — rewrite one section's mechanism (A-type).
  * add_section — introduce a new behavioral mechanism the strategy lacks \
    (A-type); optionally place it after an existing section via "after".
  * remove_section — drop a mechanism shown to be counterproductive.
  * rewrite_section_for_adherence — B-type: keep the SAME mechanism, re-express it \
    so the agent actually executes it (clearer trigger, sharper wording). The \
    mechanism, and the section name, stay the same.

Rules:
  * Only touch sections implied by the confirmed cause. Do NOT edit any keep-list \
    section.
  * Each content-carrying op supplies the FULL new section text, starting with its \
    ``## <name>`` heading, written as deployable paragraph guidance. For \
    replace/rewrite the heading MUST equal the edited section's name; for add it \
    is the new mechanism's name.
  * Stay at strategy altitude — behavioral mechanisms, not step-level task fixes.

Output ONLY a JSON object:
  {
    "ops": [
      {"op": "replace_section"|"add_section"|"remove_section"|\
"rewrite_section_for_adherence", "section": "<name>", "content": "<full new '## ' \
section text; '' for remove_section>", "after": "<optional anchor section name, \
add_section only>", "rationale": "<why this op follows from the cause>"}
    ]
  }
No prose outside the JSON."""

REFINE_PLAN_USER = """\
CONFIRMED CAUSE + TARGET:
-------------------------------------------------------------
{cause}
-------------------------------------------------------------

KEEP-LIST (never edit these sections):
{keep_list}

CURRENT STRATEGY DOCUMENT (section names are your edit targets):
-------------------------------------------------------------
{strategy}
-------------------------------------------------------------
{critique}
Produce the controlled edit plan. Respond with ONLY the JSON object described."""


REFINE_CONFRONT_SYSTEM = """\
You are an INDEPENDENT critic reviewing a controlled edit plan before it is \
applied. Check all of the following and decide whether the plan may proceed:
  1. SCOPE: every op targets only a section implied by the confirmed A/B cause; \
     nothing else is touched.
  2. KEEP-LIST: no op targets a keep-list section (this is also checked \
     mechanically — do not rely on yourself alone, but flag any violation you see).
  3. CAUSE VALIDITY: an A-type target is a genuine MISSING BEHAVIORAL MECHANISM, \
     not a local execution detail dressed up as strategy. If it is really a \
     tactical fix, reject it.
  4. NON-DUPLICATION: the resulting mechanism is not a rerun of a DEAD sibling \
     (a terminal/pruned sibling strategy) that already failed.

Output ONLY a JSON object:
  {"proceed": true|false, "reason": "<the specific finding that justifies the \
verdict>", "which_ops": [<indices of problematic ops, if any>]}
No prose outside the JSON object."""

REFINE_CONFRONT_USER = """\
CONFIRMED CAUSE + TARGET:
-------------------------------------------------------------
{cause}
-------------------------------------------------------------

KEEP-LIST:
{keep_list}

EDIT PLAN (ops):
-------------------------------------------------------------
{ops}
-------------------------------------------------------------

DEAD SIBLING STRATEGIES (terminal/pruned — the plan must not rerun one):
-------------------------------------------------------------
{dead_siblings}
-------------------------------------------------------------

May this plan proceed? Respond with ONLY the JSON object described."""


REFINE_COHERENCE_SYSTEM = """\
The strategy document has just had one or more sections edited. Your ONLY job is \
to fix cross-section coherence WITHIN THE EDITED SECTIONS: references, hand-offs, \
or transitions inside an edited section that now name or point at something \
changed. You may NOT alter any unedited section — those stay byte-identical. You \
may NOT introduce new behavioral content; only repair references/wording so the \
edited sections read coherently with the rest.

Express each fix as an exact-substring replacement that occurs EXACTLY ONCE in the \
document and lies wholly inside an edited section.

Output ONLY a JSON object:
  {"items": [{"quoted_old": "<exact substring to replace, unique in the doc, \
inside an edited section>", "new": "<replacement>", "reason": "<the cross-section \
reference it repairs>"}]}
Return an empty "items" list if nothing needs fixing. No prose outside the JSON."""

REFINE_COHERENCE_USER = """\
EDITED SECTION NAMES (you may only touch text inside these):
{modified_sections}

CURRENT DOCUMENT (after the edit plan was applied):
-------------------------------------------------------------
{strategy}
-------------------------------------------------------------

Return only the coherence fixes needed inside the edited sections. Respond with \
ONLY the JSON object described."""


REFINE_RATIONALE_SYSTEM = """\
You are writing the rationale record for a controlled strategy edit (a SEPARATE \
record, never shown to the executor agent). Summarize what the edit changes and \
what behavioral difference it should produce, so a later burst can check the \
strategy against its own stated expectations.

Output ONLY a JSON object:
  {
    "target_problem": "<the failing group / cause this edit addresses>",
    "idea_sources": "<the confirmed cause and any exploration finding it builds on>",
    "expected_behavior_changes": ["<a concrete behavioral change the edit should \
produce, versus the pre-edit strategy>", "..."]
  }
No prose outside the JSON object."""

REFINE_RATIONALE_USER = """\
CONFIRMED CAUSE:
-------------------------------------------------------------
{cause}
-------------------------------------------------------------

APPLIED EDIT PLAN (ops):
-------------------------------------------------------------
{ops}
-------------------------------------------------------------

Write the rationale record. Respond with ONLY the JSON object described."""


REFINE_INHERIT_SYSTEM = """\
You are deciding which of a parent strategy's low-level operating rules (its \
``rules.md`` — concrete "always/never do X" tactics layered under the strategy) a \
child should INHERIT after the strategy was edited. The strategy just changed; \
some rules still apply, some now conflict with the repositioned mechanism, and \
some only ever patched the very behavior the edit cures.

For EACH rule, return a three-way verdict:
  * keep — still compatible with the new strategy; carry it over VERBATIM.
  * drop — it conflicts with the repositioned mechanism, or it only existed to \
    patch the disease the edit now cures (redundant/harmful).
  * rewrite — the rule is generally useful but its WORDING is bound to the old \
    strategy; provide a minimally reworded version that fits the new strategy \
    while preserving the tactic.

Be conservative about dropping: drop only when you can name the specific tension. \
A rule merely unrelated to the edit is still compatible — keep it. Preserve order.

Output ONLY a JSON array, one object per rule, IN DOCUMENT ORDER:
  [{"rule_excerpt": "<the rule's text, verbatim from the input>", "verdict": \
"keep"|"drop"|"rewrite", "reason": "<why>", "rewritten": "<required only for \
rewrite: the reworded rule; omit/'' otherwise>"}]
No prose outside the JSON array."""

REFINE_INHERIT_USER = """\
NEW (EDITED) STRATEGY DOCUMENT:
-------------------------------------------------------------
{strategy}
-------------------------------------------------------------

PARENT RULES (judge each rule under the new strategy):
-------------------------------------------------------------
{rules}
-------------------------------------------------------------

Return the per-rule inheritance decisions in document order. Respond with ONLY the \
JSON array described."""


# ══════════════════════════════════════════════════════════════════════════════
# MERGE generation (L1_actions_redesign §6) — fuse complementary coverage
# ══════════════════════════════════════════════════════════════════════════════

MERGE_CONCEPT_SYSTEM = """\
You are designing a FUSED strategy from several existing strategies whose task \
coverage is COMPLEMENTARY: each solves train tasks the others fail. The goal is \
a single strategy expected to preserve the UNION of their coverage — fusion is \
about keeping every source's winning behavior alive, not averaging prose.

You are given the coverage complementarity matrix (who exclusively solves what), \
every source strategy's full text, and an exploration report in which fusion \
hypotheses were PROBED on real tasks (which reconciliations held, which \
conflicts are irreconcilable). Ground every choice in that evidence:
  * pick the BASE strategy (broadest / strongest coverage) to organize around;
  * for each other source, name WHICH of its sections carry its exclusive \
coverage and how they integrate;
  * where two sources' behaviors CONFLICT, adopt a reconciliation the probes \
validated (e.g. conditional routing on task features); if the report marked a \
pair irreconcilable, leave that source out and say so;
  * declare the coverage the fusion is expected to preserve.

Output ONLY a JSON object:
  {"base_node": "<node_id>",
   "contributions": [{"source": "<node_id>",
                       "sections": ["<section name>", "..."],
                       "adaptation": "<how it integrates / routes>"}, ...],
   "conflict_resolutions": [{"between": ["<node_id>", "<node_id>"],
                              "resolution": "<the probe-validated reconciliation, \
or 'excluded: irreconcilable'>"}, ...],
   "expected_coverage": ["<task_id>", "..."]}
No prose outside the JSON."""

MERGE_CONCEPT_USER = """\
COVERAGE COMPLEMENTARITY MATRIX (from the coverage ledger):
-------------------------------------------------------------
{matrix}
-------------------------------------------------------------

SOURCE STRATEGIES (full text):
-------------------------------------------------------------
{source_strategies}
-------------------------------------------------------------

EXPLORATION REPORT (fusion hypotheses probed on real tasks):
-------------------------------------------------------------
{findings}
-------------------------------------------------------------

Design the fusion blueprint. Respond with ONLY the JSON object described."""


MERGE_DRAFT_SYSTEM = """\
You are writing the deployable FUSED strategy document from an approved fusion \
blueprint. The document is read by an executor agent AS ITS INSTRUCTIONS while \
it solves tasks — it is not a design memo.

{firewall}

Fusion discipline: preserve each contributing source's winning behavior as the \
blueprint assigns it; where the blueprint routes behaviors conditionally, state \
the routing condition as a concrete, observable task feature the executor can \
check. Do not water conflicting behaviors down into vague compromise language — \
route, sequence, or scope them.

Also produce a rationale (a SEPARATE record, never shown to the executor).

Output ONLY a JSON object:
  {{
    "strategy_md": "<the full fused strategy document: flat '## <behavioral \
mechanism>' sections (an optional short preamble before the first '##'), \
deployable voice, no meta-commentary, no node ids>",
    "rationale": {{
      "target_problem": "<the coverage union this fusion must preserve>",
      "idea_sources": "<which source strategies contributed what, and which \
probe evidence validated the reconciliations>",
      "expected_behavior_changes": ["<a concrete behavioral change versus \
running any single source strategy>", "..."]
    }}
  }}
No prose outside the JSON."""

MERGE_DRAFT_USER = """\
FUSION BLUEPRINT (the approved conception):
-------------------------------------------------------------
{conception}
-------------------------------------------------------------

SOURCE STRATEGIES (full text — the behaviors being fused):
-------------------------------------------------------------
{source_strategies}
-------------------------------------------------------------

EXPLORATION REPORT (the probe evidence behind the reconciliations):
-------------------------------------------------------------
{findings}
-------------------------------------------------------------

Write the deployable fused strategy document and its rationale. Respond with \
ONLY the JSON object described."""


MERGE_RULES_SELECT_SYSTEM = """\
You are selecting which VERIFIED rules sections to carry into a newly fused \
strategy. The source node's rules were grown and gate-verified under ITS OWN \
strategy; the fused strategy (given below) is the yardstick now.

Judge each '### ' section of the source rules independently:
  * keep — the section is compatible with the fused strategy AND plausibly \
supports the coverage this source contributes (its exclusive tasks are listed);
  * drop — the section contradicts the fused strategy's behavior, is specific \
to a mechanism the fusion excluded, or is generic filler another source \
already covers better.

You are choosing, NOT rewriting: section text is carried verbatim by the \
system. When unsure, lean keep for sections tied to the source's exclusive \
coverage and lean drop for generic advice.

Output ONLY a JSON array, one entry per section, in document order:
  [{"section": "<the exact '### ' heading text>", "verdict": "keep"|"drop",
    "reason": "<one sentence>"}, ...]
No prose outside the JSON."""

MERGE_RULES_SELECT_USER = """\
FUSED STRATEGY (the yardstick):
-------------------------------------------------------------
{strategy}
-------------------------------------------------------------

SOURCE NODE: {source_node} — exclusive coverage: {exclusive_tasks}

SOURCE RULES SECTIONS (judge each '### ' section):
-------------------------------------------------------------
{rules}
-------------------------------------------------------------

Return the per-section keep/drop verdicts in document order. Respond with ONLY \
the JSON array described."""


MERGE_RULES_CONSOLIDATE_SYSTEM = """\
You are consolidating the KEPT rules sections from several source nodes into \
one rules document for a fused strategy. Every candidate section below is \
identified as (source_node, section heading); the system will assemble the \
final document from your selection VERBATIM — you choose and order, you never \
rewrite.

Consolidation discipline:
  * duplicates (two sections teaching the same behavior): keep exactly one — \
prefer the source whose exclusive coverage depends on it;
  * direct conflicts (two sections commanding incompatible behavior in the \
same situation): keep the one from the stronger source for that situation, \
drop the other;
  * order the survivors so related sections sit together (base source first).

Output ONLY a JSON object:
  {"sections": [{"source": "<node_id>", "section": "<the exact '### ' heading \
text>"}, ...],
   "dropped": [{"source": "<node_id>", "section": "<heading>",
                 "reason": "duplicate of ..."|"conflicts with ..."}, ...]}
No prose outside the JSON."""

MERGE_RULES_CONSOLIDATE_USER = """\
FUSED STRATEGY (context for conflict judgement):
-------------------------------------------------------------
{strategy}
-------------------------------------------------------------

CANDIDATE SECTIONS (all kept by the per-source screens; full text):
-------------------------------------------------------------
{candidates}
-------------------------------------------------------------

Select and order the final sections (verbatim assembly follows your list). \
Respond with ONLY the JSON object described."""


def drafting_system() -> str:
    return DRAFTING_SYSTEM.format(firewall=_STRATEGY_ALTITUDE_FIREWALL)


def merge_draft_system() -> str:
    return MERGE_DRAFT_SYSTEM.format(firewall=_STRATEGY_ALTITUDE_FIREWALL)


def altitude_repair_system() -> str:
    return ALTITUDE_REPAIR_SYSTEM.format(firewall=_STRATEGY_ALTITUDE_FIREWALL)
