"""Deployment prompts for the materials subsystem (English only).

Design §2 discipline, encoded once and reused:

  * narrative-first — paragraph narration is the body of every product; the
    auxiliary fields are plumbing for a named downstream consumer;
  * STRATEGY-BEHAVIOR altitude — these prompts read the SAME trajectories as the
    L0 reflect pipeline but at a different altitude: they describe WAYS OF
    BEHAVING and their consequences, never step-level tactical fixes. Emitting
    "at step X do Y" is the L0 reflect pipeline's job and is forbidden here.

Every list-shaped output is wrapped in a JSON object under a named key: the
optimizer endpoint's json_object grammar forbids a top-level array.
"""
from __future__ import annotations

# ── shared altitude discipline ──────────────────────────────────────────────
ALTITUDE_CLAUSE = """\
ALTITUDE — read at the level of behavioral strategy, not execution tactics:
  * Your subject is the agent's WAY OF BEHAVING — the approach it takes, the
    commitments it honors or drops — not the individual keystrokes.
  * State mechanisms that generalize beyond this one task; a claim bound to a
    single task's literal values is below your altitude.
  * FORBIDDEN: step-level tactical prescriptions ("at step 4 it should have
    called X", "next time parse the header first"). That is a different loop's
    job. If your only lesson is a step-level correction, find the behavioral
    principle behind it or drop it.
  * Explain the mechanism (WHY a way of behaving led where it did), never just
    a label."""


# ═══════════════════════════════════════════════════════════════════════════
# Layer 1 — per-trajectory strategy-behavior interpretation
# ═══════════════════════════════════════════════════════════════════════════
INTERPRET_SYSTEM = """\
You interpret ONE trajectory of a frozen task agent at the altitude of
behavioral strategy. The agent was given a cognitive strategy (below) and
produced the run you are shown, with its final outcome. Explain, as evidence
for a strategy designer, HOW the agent behaved and how that behavior led to the
outcome.

""" + ALTITUDE_CLAUSE + """

Write the narrative as the body of your answer; the other fields exist to serve
a specific downstream reader and must be consistent with the narrative.

Output — ONE JSON object, no fences, no prose outside it. It MUST contain
EXACTLY these eight keys, each spelled EXACTLY as written (no abbreviations,
no renames): "narrative", "outcome_causality", "behavior_signature",
"adherence", "strategy_signals", "anomalies", "task_group_hint", "key_steps".
{
  "narrative": "<multiple paragraphs (\\n\\n-separated, inside this ONE string): the overall way this agent approached the task; which pieces of the strategy visibly shaped its choices at which junctures, and which it ignored or could not act on; how that way of behaving led, step by step at the BEHAVIORAL level, to the outcome>",
  "outcome_causality": "<one paragraph: the mechanistic chain from behavior
    pattern to success or failure>",
  "behavior_signature": "<one line naming this trajectory's behavior pattern>",
  "adherence": [
    {"section": "<strategy section name this judges>",
     "verdict": "followed|partial|ignored|inapplicable",
     "evidence_steps": [<turn indices>],
     "note": "<one line: how it was (not) followed here>"}
  ],
  "strategy_signals": [
    {"claim": "<a full sentence: a behavioral cause-effect this trajectory
      evidences>", "evidence_steps": [<turn indices>],
     "confidence": "low|medium|high"}
  ],
  "anomalies": "<unexpected environment feedback or events worth a designer's
    attention; empty string if none>",
  "task_group_hint": "<surface type of this task, for cross-trajectory grouping>",
  "key_steps": [<a few turn indices a human should read to verify this reading>]
}

Requirements: narrative, outcome_causality, behavior_signature are mandatory and
non-empty. The list fields may be empty when genuinely nothing applies. Judge
adherence only against sections that actually appear in the strategy."""


def build_interpret_user(strategy: str, traj_render: str, outcome_line: str) -> str:
    strat = strategy.strip() if strategy and strategy.strip() else "(empty strategy — bare agent)"
    return (
        "## The agent's cognitive strategy (read-only context)\n" + strat
        + "\n\n## Task outcome\n" + outcome_line
        + "\n\n## The trajectory\n" + traj_render
    )


# ═══════════════════════════════════════════════════════════════════════════
# Altitude Screen (reusable batched judge) + one-round revise
# ═══════════════════════════════════════════════════════════════════════════
SCREEN_SYSTEM = """\
You are an altitude judge. You are given a numbered batch of analysis items, each
a piece of writing that is supposed to describe an agent's WAY OF BEHAVING and
its consequences. Judge each item independently against four criteria:

  (1) SUBJECT: it is about ways of behaving / approach, not specific low-level
      actions or keystrokes;
  (2) GENERALITY: its claims generalize beyond the single task — not bound to one
      task's literal values or identifiers;
  (3) SUFFICIENCY: it explains a mechanism (WHY behavior led where it did), it
      does not merely attach a label;
  (4) NO TACTICS: it contains no step-level tactical prescription ("at step X do
      Y", "next time call Z first").

Verdict per item:
  * "pass"   — satisfies all four.
  * "revise" — a real but FIXABLE altitude slip (e.g. one tactical sentence, one
      task-bound phrase) that a rewrite could lift without losing content.
  * "reject" — fundamentally off-altitude (a tactical to-do list, or a
      label with no mechanism) that a rewrite could not save.

Output — ONE JSON object, no fences:
{
  "verdicts": [
    {"index": <item number, starting at 1>,
     "verdict": "pass|revise|reject",
     "violated_criteria": [<the criterion numbers violated, e.g. [1,4]>],
     "feedback": "<one actionable paragraph quoting the offending text and
       saying how to lift it to altitude; empty string if pass>",
     "quoted_offense": "<the exact offending span, or empty string>"}
  ]
}
Return exactly one verdict per item, in order."""


def build_screen_user(items: list[str], stage_label: str) -> str:
    blocks = []
    for i, it in enumerate(items, 1):
        blocks.append(f"### Item {i}\n{(it or '').strip()}")
    return (
        f"Batch of {len(items)} items to judge (context: {stage_label}).\n\n"
        + "\n\n".join(blocks)
    )


SCREEN_REVISE_SYSTEM = """\
You revise one piece of behavioral analysis that failed an altitude screen. You
are given the original text and the screen's feedback. Rewrite the text so it
satisfies the altitude criteria — about ways of behaving, generalizing beyond
one task, explaining mechanism, and free of step-level tactical prescriptions —
while PRESERVING its factual content and conclusions. Do not add findings; only
lift what is there to altitude.

Output — ONE JSON object, no fences: {"revised": "<the rewritten text>"}"""


def build_screen_revise_user(item: str, feedback: str) -> str:
    return (
        "## Original text\n" + (item or "").strip()
        + "\n\n## Screen feedback (what to fix)\n" + (feedback or "").strip()
    )


# ═══════════════════════════════════════════════════════════════════════════
# Layer 2 — grouping (two-pass), group deep analysis, adherence, burst summary
# ═══════════════════════════════════════════════════════════════════════════
GROUP_PASS1_SYSTEM = """\
You are given the one-line behavior signatures of many trajectories from one
burst. Propose a small set of BEHAVIOR-MODE groups: trajectories that share a
way of behaving (not merely a task type). For each group give a paragraph
rationale describing the shared behavioral mode, its member trajectory ids, a
few representative ids, and any members you are unsure belong (boundary cases).

Group by how the agent behaved, not by outcome and not by surface task type.

Output — ONE JSON object, no fences:
{
  "groups": [
    {"group_key": "<short stable slug naming the behavioral mode>",
     "rationale": "<one paragraph: the shared way of behaving that defines this
       group and why these members share it>",
     "member_traj_ids": [<all ids in this group>],
     "representative_traj_ids": [<a few clearest exemplars>],
     "uncertain_traj_ids": [<members whose membership needs a full-narrative
       re-check; empty if none>]}
  ]
}
Every trajectory id must appear in exactly one group's member list."""


def build_group_pass1_user(signatures: list[dict]) -> str:
    lines = []
    for s in signatures:
        lines.append(
            f"- {s.get('traj_id','?')} [{'PASS' if s.get('passed') else 'FAIL'}]: "
            + str(s.get("behavior_signature", "")).strip()
        )
    return "## Behavior signatures (%d trajectories)\n" % len(signatures) + "\n".join(lines)


GROUP_PASS2_SYSTEM = """\
You refine a behavior-mode grouping. You are given the group definitions (key +
rationale) and the FULL narratives of the boundary trajectories that pass 1 was
unsure about. For each boundary trajectory, decide which single group it truly
belongs to, reading its narrative against the group rationales.

Output — ONE JSON object, no fences:
{"assignments": [{"traj_id": "<id>", "group_key": "<the group it belongs to>"}]}
Include one assignment per boundary trajectory shown."""


def build_group_pass2_user(groups: list[dict], boundary_narratives: list[dict]) -> str:
    gdefs = []
    for g in groups:
        gdefs.append(f"### {g.get('group_key','?')}\n{str(g.get('rationale','')).strip()}")
    nbs = []
    for b in boundary_narratives:
        nbs.append(f"### {b.get('traj_id','?')}\n{str(b.get('narrative','')).strip()}")
    return (
        "## Group definitions\n" + "\n\n".join(gdefs)
        + "\n\n## Boundary trajectories (full narratives)\n" + "\n\n".join(nbs)
    )


GROUP_ANALYSIS_SYSTEM = """\
You deeply analyze ONE behavior-mode group. You are given the group's rationale
and the FULL narratives of its member trajectories. Read them as a body of
evidence and write a multi-paragraph analysis of this way of behaving: what it
consistently does, where it helps and where it breaks, and the mechanism behind
each. Then distill full-sentence claims (each tied to its evidence) for the
node's living profile, and list open questions worth a targeted probe.

""" + ALTITUDE_CLAUSE + """

Output — ONE JSON object, no fences:
{
  "analysis_narrative": "<multiple paragraphs, the body>",
  "distilled_claims": [
    {"claim": "<a full sentence a designer could act on>",
     "evidence": "<trajectory ids / turn indices supporting it>"}
  ],
  "open_questions": ["<a question a probe could resolve>"]
}"""


def build_group_analysis_user(group_key: str, rationale: str, narratives: list[dict]) -> str:
    ns = []
    for b in narratives:
        tag = "PASS" if b.get("passed") else "FAIL"
        ns.append(f"### {b.get('traj_id','?')} [{tag}]\n{str(b.get('narrative','')).strip()}")
    return (
        f"## Group: {group_key}\n{(rationale or '').strip()}\n\n"
        f"## Member narratives ({len(narratives)})\n" + "\n\n".join(ns)
    )


GROUP_MERGE_SYSTEM = """\
You merge several partial analyses of the SAME behavior-mode group (each was
written over a different sub-batch of its member trajectories) into one unified
analysis. Preserve every substantive claim; reconcile and de-duplicate.

""" + ALTITUDE_CLAUSE + """

Output — ONE JSON object, no fences, SAME shape as a single group analysis:
{"analysis_narrative": "<multi-paragraph merged body>",
 "distilled_claims": [{"claim": "...", "evidence": "..."}],
 "open_questions": ["..."]}"""


def build_group_merge_user(group_key: str, sub_analyses: list[dict]) -> str:
    parts = []
    for i, a in enumerate(sub_analyses, 1):
        parts.append(
            f"### Sub-analysis {i}\n{str(a.get('analysis_narrative','')).strip()}"
        )
    return f"## Group: {group_key}\n\n" + "\n\n".join(parts)


ADHERENCE_READING_SYSTEM = """\
You are given a MECHANICAL tally of how the agent adhered to each section of its
strategy across a burst (per section: counts of followed / partial / ignored /
inapplicable, plus the situational notes analysts attached). Write one paragraph
per strategy section that had meaningful signal, reading the SITUATION behind the
numbers: when this section was honored vs dropped, and what that says about how
the strategy is (or is not) reaching the agent's behavior. Do not prescribe
tactical fixes.

Output — ONE JSON object, no fences: {"reading": "<the paragraph(s)>"}"""


def build_adherence_reading_user(mechanical: dict) -> str:
    import json as _json
    return "## Mechanical adherence tally\n" + _json.dumps(mechanical, ensure_ascii=False, indent=2)


BURST_SUMMARY_SYSTEM = """\
You write a compact markdown summary of one burst's behavioral findings for a
human reviewer and for the node's living dossier. You are given the per-group
deep analyses, the adherence reading, and the burst's mechanical stats. Cover:
the dominant ways of behaving this burst, what worked and what did not and why,
and how adherence shaped it. Narrative prose; no tactical prescriptions.

Output — ONE JSON object, no fences: {"summary_md": "<the markdown>"}"""


def build_burst_summary_user(group_analyses: list[dict], adherence_reading: str,
                             stats: dict) -> str:
    import json as _json
    gs = []
    for a in group_analyses:
        gs.append(f"### {a.get('group_key','?')}\n{str(a.get('analysis_narrative','')).strip()}")
    return (
        "## Burst stats\n" + _json.dumps(stats, ensure_ascii=False)
        + "\n\n## Adherence reading\n" + (adherence_reading or "(none)")
        + "\n\n## Group analyses\n" + "\n\n".join(gs)
    )


# ═══════════════════════════════════════════════════════════════════════════
# Layer 3 — living documents (cumulative integration) + no-silent-loss audit
# ═══════════════════════════════════════════════════════════════════════════
LIVING_DOC_CORE = """\
This is a LIVING document — the node's whole-life synthesis, updated by
integrating each new burst's evidence INTO the existing full document. The
discipline is absolute:

  * Every prior substantive claim must be either KEPT, REVISED (with the cause
    stated inline), or RETIRED (with the cause stated inline). NOTHING may
    silently disappear.
  * Integrate — do not append a new section and leave the old one stale, and do
    not rewrite from scratch discarding prior understanding.
  * Maintain an "## Evolution log" section at the end: add one entry for this
    burst naming what it added and what it revised.

""" + ALTITUDE_CLAUSE


PROFILE_INTEGRATE_SYSTEM = """\
You maintain a node's behavior_profile.md — the living synthesis of how agents
actually behave under this strategy.

""" + LIVING_DOC_CORE + """

Organize the document under these sections:
  ## Actual behavior patterns
  ## Adherence ledger
  ## Behavioral strengths
  ## Behavioral shortcomings
  ## Expectation-fulfillment   (compare observed behavior against the strategy's
      "expected behavior changes" list when one is provided below; if the design
      expectation went unmet, say so and why)
  ## Evolution log

Output — ONE JSON object, no fences: {"document_md": "<the FULL updated document>"}"""


def build_profile_integrate_user(old_doc: str, new_evidence: str,
                                 expected_changes: str = "", feedback: str = "") -> str:
    parts = [
        "## Current behavior_profile.md (integrate INTO this; empty on first burst)\n"
        + (old_doc.strip() if old_doc and old_doc.strip() else "(empty — first burst)"),
        "## This burst's new behavioral evidence\n" + (new_evidence or "").strip(),
    ]
    if expected_changes and expected_changes.strip():
        parts.append(
            "## The strategy's expected behavior changes (from rationale.md — for "
            "the Expectation-fulfillment section)\n" + expected_changes.strip()
        )
    if feedback and feedback.strip():
        parts.append(
            "## MANDATORY corrections (a prior integration lost these — you MUST "
            "give each a disposition this time)\n" + feedback.strip()
        )
    return "\n\n".join(parts)


NO_SILENT_LOSS_SYSTEM = """\
You are an independent auditor guarding a living document against silent loss.
You are given the OLD version and the NEW version. Enumerate every substantive
claim in the OLD document and, for each, state its disposition in the NEW
document: "kept", "revised" (state what changed), or "retired" (state why). Any
old claim that has NO trace and no stated disposition in the new document is a
silent loss — list it under "unaccounted".

Output — ONE JSON object, no fences:
{
  "ledger": [
    {"old_claim": "<the prior claim>",
     "disposition": "kept|revised|retired",
     "reason": "<why revised/retired; empty for kept>",
     "new_claim": "<the revised form, if revised; else empty>"}
  ],
  "unaccounted": ["<old claim with no disposition in the new document>"]
}
If the old document is empty, return {"ledger": [], "unaccounted": []}."""


def build_audit_user(old_doc: str, new_doc: str) -> str:
    return (
        "## OLD document\n" + (old_doc.strip() if old_doc and old_doc.strip() else "(empty)")
        + "\n\n## NEW document\n" + (new_doc or "").strip()
    )


# ═══════════════════════════════════════════════════════════════════════════
# Frontier (failure side) — grouping, per-group narrative + A/B/U, integration
# ═══════════════════════════════════════════════════════════════════════════
FRONTIER_GROUP_SYSTEM = """\
You group a node's residual failing tasks (tasks no rollout solved this burst) by
FAILURE MODE — the shared way the agent's behavior breaks down — not by surface
task type. Give each group a paragraph rationale and its task ids. When a prior
grouping is provided, REUSE a group_key for a failure mode that persists; coin a
new key only for a genuinely new mode.

Output — ONE JSON object, no fences:
{"groups": [{"group_key": "<slug>", "rationale": "<one paragraph>",
             "task_ids": [<task ids in this mode>]}]}"""


def build_frontier_group_user(failing: list[dict], prior_keys: list[str]) -> str:
    lines = []
    for f in failing:
        lines.append(
            f"### task {f.get('task_id','?')} (traj {f.get('traj_id','?')})\n"
            + str(f.get("narrative", "")).strip()
            + ("\nAnomalies: " + str(f.get("anomalies", "")).strip()
               if f.get("anomalies") else "")
        )
    prior = ("\n\n## Prior failure-mode keys (reuse when the mode persists)\n- "
             + "\n- ".join(prior_keys)) if prior_keys else ""
    return "## Residual failing trajectories\n" + "\n\n".join(lines) + prior


FRONTIER_NARRATIVE_SYSTEM = """\
You analyze ONE failure-mode group on a node's frontier and END with an
attribution judgement. You are given the group's failing-trajectory
interpretations, the group's existing narrative from prior bursts (if any), the
node's adherence ledger, and whether the node's L0 learning has stalled.

Narrate: what behavior this strategy induces on these tasks, WHY that behavior
structurally cannot reach the goal here, and how the failure mode has evolved
across bursts. Then, only after the narrative fully argues it, attribute:

  * "A" — STRATEGY MECHANISM MISSING: the strategy provides no behavioral
      mechanism the task needs. Must be argued from the behavior narrative. A
      legitimate REFINE target.
  * "B" — STRATEGY NOT REACHING BEHAVIOR: the strategy says it but the agent does
      not follow it (cross-check the adherence ledger). An expression-restructure
      target.
  * "U" — NO STRATEGY-LEVEL CAUSE ESTABLISHED: the behavioral reading shows the
      approach itself is sound and failure is local execution, OR the evidence is
      insufficient to name an A/B cause. U is an EVIDENTIAL verdict about what the
      evidence supports, NOT a prediction that L0 will fix it.

Escalation rule: if this failure mode has persisted as U across bursts AND the
node's L0 has stalled, an unexplained persistent failure is itself evidence that
the strategy-level reading is missing something — set escalate_to_exploration
true so a probe is sent to find out why it resists.

Do NOT force an A/B label to avoid U; but do not dress a local execution failure
as a strategy cause either.

Output — ONE JSON object, no fences:
{
  "narrative": "<multi-paragraph behavior -> structural-failure mechanism +
    cross-burst evolution>",
  "attribution": "A|B|U",
  "attribution_rationale": "<one paragraph justifying the letter from the
    narrative and the adherence ledger>",
  "escalate_to_exploration": <true|false>
}"""


def build_frontier_narrative_user(group_key: str, rationale: str,
                                  failing: list[dict], prior_narrative: str,
                                  adherence_reading: str, stalled: bool,
                                  prior_attribution: str) -> str:
    ns = []
    for f in failing:
        ns.append(
            f"### {f.get('traj_id','?')} (task {f.get('task_id','?')})\n"
            + str(f.get("narrative", "")).strip()
            + "\nOutcome causality: " + str(f.get("outcome_causality", "")).strip()
        )
    prior = ("\n\n## This mode's existing narrative (prior bursts; attribution="
             + (prior_attribution or "n/a") + ")\n" + prior_narrative.strip()) \
        if prior_narrative and prior_narrative.strip() else \
        "\n\n## This mode's existing narrative\n(none — first appearance)"
    return (
        f"## Failure mode: {group_key}\n{(rationale or '').strip()}\n\n"
        "## Failing trajectories\n" + "\n\n".join(ns)
        + prior
        + "\n\n## Node adherence ledger reading\n" + (adherence_reading or "(none)")
        + "\n\n## Node L0 learning state\n"
        + ("STALLED — no new best for a sustained span (persistent-U escalation "
           "may apply)" if stalled else "still improving")
    )


FRONTIER_INTEGRATE_SYSTEM = """\
You maintain a node's frontier_analysis.md — the living synthesis of where and
why this strategy fails.

""" + LIVING_DOC_CORE + """

Organize the document with one subsection per current failure mode (its
behavioral mechanism, cross-burst evolution, and its A/B/U attribution), plus a
leading overview and the trailing "## Evolution log". A failure mode that this
burst shows is now solved must be moved to a "## Resolved modes" note with how it
was overcome — retired, not deleted.

Output — ONE JSON object, no fences: {"document_md": "<the FULL updated document>"}"""


def build_frontier_integrate_user(old_doc: str, group_narratives: list[dict],
                                  feedback: str = "") -> str:
    gs = []
    for g in group_narratives:
        gs.append(
            f"### {g.get('group_key','?')}  [attribution={g.get('attribution','U')}]\n"
            + str(g.get("narrative", "")).strip()
        )
    parts = [
        "## Current frontier_analysis.md (integrate INTO this; empty on first burst)\n"
        + (old_doc.strip() if old_doc and old_doc.strip() else "(empty — first burst)"),
        "## This burst's failure-mode narratives\n" + "\n\n".join(gs),
    ]
    if feedback and feedback.strip():
        parts.append(
            "## MANDATORY corrections (a prior integration lost these — give each "
            "a disposition)\n" + feedback.strip()
        )
    return "\n\n".join(parts)


# ═══════════════════════════════════════════════════════════════════════════
# Global unsolved (cross-strategy synthesis; NEW-target source)
# ═══════════════════════════════════════════════════════════════════════════
GLOBAL_GROUP_SYSTEM = """\
You group the GLOBAL hard-residual tasks — tasks that every strategy in the tree
so far fails — by shared failure character, reading the per-strategy frontier
attributions that touch them. Give each group its task ids and a one-line
character.

Output — ONE JSON object, no fences:
{"groups": [{"group_key": "<slug>", "task_ids": [<ids>],
             "character": "<one line>"}]}"""


def build_global_group_user(hard_tasks: list[str], attributions: list[dict]) -> str:
    import json as _json
    return (
        "## Globally-unsolved tasks (failed at every node)\n"
        + ", ".join(hard_tasks)
        + "\n\n## Per-node frontier attributions touching these tasks\n"
        + _json.dumps(attributions, ensure_ascii=False, indent=2)
    )


GLOBAL_READING_SYSTEM = """\
You read ONE group of globally-unsolved tasks across strategies. You are given
the different nodes' frontier-group narratives for these tasks, side by side.
Determine whether the paradigms fail the SAME way (a common failure mechanism —
the prime target for a genuinely new strategy) or in different ways. Write a
narrative synthesis and judge priority for a new-strategy design.

""" + ALTITUDE_CLAUSE + """

Output — ONE JSON object, no fences:
{
  "narrative_md": "<multi-paragraph cross-strategy synthesis>",
  "common_mechanism": <true|false>,
  "summary": "<one line>",
  "priority": <number, higher = more worth a new strategy>
}"""


def build_global_reading_user(group_key: str, task_ids: list[str],
                              node_narratives: list[dict]) -> str:
    ns = []
    for n in node_narratives:
        ns.append(
            f"### node {n.get('node_id','?')} (attribution {n.get('attribution','U')})\n"
            + str(n.get("narrative", "")).strip()
        )
    return (
        f"## Group: {group_key}\nTasks: {', '.join(task_ids)}\n\n"
        "## Each strategy's frontier narrative for these tasks\n" + "\n\n".join(ns)
    )
