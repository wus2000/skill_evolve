# editpipe v3 — Plan / Draft / Review / Apply (LLM decides, rules execute)

Status: **APPROVED** (user sign-off 2026-07-07). Replaces the v2
merge-then-adjudicate loop; v2 remains available as `edit_pipeline: "v2"`
fallback during the transition.

## 0. Why v3 (evidence)

The v2 adjudication loop failed structurally, not incidentally (live log,
AW step 22): the validator judged ALL dimensions in one call whose output
grows O(violations × detail) — truncated at 8192 twice (after already being
raised from 4096), leaving a silently incomplete violation list; pairwise
counting exploded 16 edits into 85 "violations"; repair/detect oscillated
under judge noise (1 -> 2 blocking on an UNCHANGED edit set); the exit path
evaporated findings (F2) and mislabeled convergence (F3). Root cause: an
adversarial detect-repair loop where detector and repairer are the same weak
model, and rule code consumed LLM text (violation type names, anchor
"adjacency" with no sound definition).

## 1. The essence

Consolidate N raw edits into a set of ORTHOGONAL merged edits — each
independently ablatable, all simultaneously applicable. Sub-requirements:

| | requirement | nature | on error |
|---|---|---|---|
| R1a | apply-commutativity | structural | accident |
| R1b | ablation independence | semantic | verify power loss |
| R2 | no redundancy | semantic | wasted verify budget |
| R3 | per-edit quality (purity, actionability) | generative + judge | leakage has NO downstream backstop |
| R4 | information preservation | audit | learning-signal loss |

## 2. Division of responsibility (the axiom)

**LLM decides everything semantic** (what groups with what; how to write;
where content belongs; how it fuses into a section; what to retreat from).
**Rule code touches ONLY its own protocol handles and arithmetic**: handle
regex + set operations, closed-enum matching, verbatim-echo equality,
counting. It never parses, compares, or transforms LLM text (the sole
verified-reliable boundary in v2 was the E#n allowlist).
**verify + gate judge all effect** (group-level ablation; the only quality
authority). No defensive middle layer: an LLM's semantic error is caught by
the next LLM stage or measured out by verify.

## 3. Document structure protocol (DSP)

Structure stops at `### section` (user ruling): a section = one `###` line
+ FREE-FORM markdown (paragraphs, code fences, ####, lists). The only
reserved token is a fence-outside line starting `### `. There is NO invalid
document — any text parses into a legal section list (self-healing).

- `RulesDocV3` (rule code) owns parse / handle issue / render / apply /
  serialize. Handles `S#k` are per-run-of-the-pipeline (reissued each step).
- Rendering: `### [S#k] <title>` + verbatim body. Empty document renders as
  an explicit bootstrap notice (only `add_section` possible) — first-step
  output is serialized into canonical form, so the parser thereafter only
  ever reads its own serializer's output (`parse(serialize(d)) == d` is a
  test anchor).
- Draft-edit operations (all handle-addressed; text anchors retired):
  `add_section {title, content}` · `append_to_section {target, content}` ·
  `replace_section {target, content}` · `remove_section {target, reason}`.
- Structural conflict = handle-set arithmetic: append+append same section
  coexist (deterministic order); replace/remove conflict with any same-
  section op; add_section same title conflicts. (Semantic conflict is C's.)

## 4. Raw edits are MATERIAL, not operations (scope decision)

The reflect layer is UNCHANGED: v2 RawPatch/Edit objects (7-op, text-anchor
schema) feed v3 as drafting MATERIAL. A raw edit's kind/anchor/body are
reference input to stage B, which writes fresh DraftEdits in the DSP
protocol — no translation layer, no reflect rework, and v2/v3 coexist on
one reflect. (Supersedes the earlier "handleize from reflect onward" note:
B never translates anchors, it re-drafts.)

## 5. The pipeline (three reads + apply)

```
raw edits (v2 schema, material)
 -> A GROUP    1 call: partition into orthogonal change-aspects + placement
 -> B DRAFT    per group (parallel): fresh DraftEdits; source_ids per edit;
               target_tasks inherited by RULE CODE as the union of the
               source raws' target_tasks (LLM never writes the field)
 -> C REVIEW   1 call, diagnosis only: semantic conflicts/duplicates across
               groups + per-edit checklist (leakage / not_actionable /
               contradicts_existing) -> issues route back to B as a merged
               REVISION (counterpart group's raws + drafts + issue text) x1
 -> APPLY      rule code buckets by section handle; ONE Section Applier call
               per touched section (LLM semantic fusion; unapplied honest-
               return channel); output adopted as-is; untouched sections
               never pass through any LLM (SCOPE by construction)
 -> VERIFY     group-level ablation (aspect granularity) -> paired GATE
```

- **A GROUP** output: `{"groups":[{"ids":[...], "aspect": <rich>,
  "placement": "S#k | NEW: <title>"}]}` — uniform group list (a singleton is
  a one-id group). Arithmetic checks: id partition completeness (missing ->
  auto-singleton; phantom -> dropped, logged); group size <= 6 (else one
  re-ask, then degrade to singletons). Placement echo-checked against the
  catalog; a failed echo voids only that field (B decides placement).
- **B DRAFT** output: `{"analysis": <rich>, "edits":[{op, target/section,
  content: <rich, examples encouraged>, source_ids, rationale: <rich>}],
  "dropped_ids":[{id, reason: <rich>}]}`. Arithmetic: union(source_ids) ∪
  dropped = group members; content non-empty; count <= member count.
  REVISION mode adds: both groups' raws + first drafts + full issue text +
  counterpart context + `revision_note`.
- **C REVIEW** output: `{"pass": bool, "issues":[{ids, type, explanation:
  <rich>, instruction: <rich>}]}`. Residuals after one revision: leakage ->
  reject that edit (no backstop downstream); semantic_conflict /
  contradicts -> defer to next step; duplicate / not_actionable -> deliver,
  verify decides.
- **Section Applier** output: `{"application_notes": <rich, per edit>,
  "unapplied":[{id, reason}], "new_section_text": <full section>}` — notes
  first, long text LAST (autoregressive: truncation can only wound the
  tail, never the decisions). Adopted as-is; unapplied edits defer with
  audit. Per user ruling: NO apply-side guards (no size heuristics, no
  sanitization gates — the ### grammar is self-healing).

## 6. Failure chain (uniform per call)

L1 syntax: JSON unparseable -> json_repair (syntax-only franchise).
L2 protocol: arithmetic/handle/echo violations -> machine-generated
violation list -> ONE protocol-repair call (template below) -> recheck ->
predefined lossless degradation (A: all-singletons; B: members pass through
as their own singleton groups via raw fallback; C: pass; Applier: section's
edits defer, old text stands).

Protocol-repair SYSTEM (fix references/structure, NEVER meaning):
```
You repair a JSON output that violates its output protocol. The violations
listed below were detected by MECHANICAL checks and are the complete list.
1. Fix EXACTLY the listed violations; change nothing else.
2. NEVER alter the semantic content of text fields.
3. Ids are protocol handles issued by the system; never invent one.
4. Output the FULL corrected JSON only.
```
Repair (protocol, no meaning change) and REVISION (meaning change on a
critique) are separate channels with separate templates — decision log #14
at the call layer.

## 7. Verify granularity decoupling

Apply granularity (fine: one edit may change one sentence via semantic
fusion) is DECOUPLED from verify granularity (coarse: one aspect-group is
one ablation unit — the funnel audit measured 79-83% null verdicts at
per-edit granularity; group-level restores statistical power). A group's
verify candidate = Applier(incumbent rules, that group's edits); the
collective candidate = Applier(incumbent rules, all surviving groups'
edits). Group target_tasks = rule-side union of members' target_tasks.

## 8. Coexistence & migration

`edit_pipeline: "v3"` selects the new path inside run_l0_step; "v2" remains
the fallback (with the F2/F3 stopgaps). Reflect is shared. Shadow
comparison on recorded raw-edit sets from live runs precedes any per-env
default switch (per-env config covenant).
