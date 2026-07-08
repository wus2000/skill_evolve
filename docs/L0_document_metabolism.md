# L0 Document Metabolism — differential drafting + burst-end consolidation

Status: implemented 2026-07-08 (branch `feat/l1-actions-redesign`).
UPDATE (2026-07-08 evening, user ruling): the burst-end consolidation (§4)
is DISABLED in both launchers after three real-document smoke rounds — the
single tidy-up call could not yet deliver deep lossless compression (best:
-37% with 97 lost-identifier flags, mostly checker false positives — see
§7a; SS rewrite bodies were re-split by the ### self-heal). The code stays
(config-gated off) for later revisiting; bloat control rests on the
upstream differential chain (§3), which passed its real-data smoke
(14 groups fully absorbed, amend ops emitted, 7/7 signal coverage).
Companion to `docs/editpipe_v3_design.md` (the v3 pipeline this extends).

## 1. Problem (measured live, 2026-07-08)

Two live v3 runs grew their rules.md ~6x in ~11 steps with ZERO score gain
past an early best:

| run | growth | best reached at | after that |
|-----|--------|-----------------|------------|
| AppWorld  | 15.0KB/13 sec -> 88.1KB/22 sec | step 2 (33KB, 0.860) | 9 steps, 5 accepts, never above 0.860 |
| Spreadsheet | 16.9KB/9 sec -> 112.1KB/15 sec | step 4 (56KB, 0.717) | 6 steps, 3 tie-accepts, never above |

Eight defect classes were audited in the grown documents: (1) instance
pile-up ("iterate ALL pages" x7 variants in one section, x34 document-wide);
(2) cross-section duplication (`access_token` rules x52 across 9 sections);
(3) theme splits (two sections sharing one theme, incl. `[S#6] X` next to
`X`); (4) structural debris (empty shells, leaked handle titles, orphaned
"Step 6:", zombie `Why?`/`Example` sections); (5) training-data residue
(literal emails/dates/amounts); (6) evaluation-machinery phrasing re-
accumulating in the backlog (SS 35 hits); (7) near-identical code-block
sprawl (27% of bytes); (8) nesting depth 4+.

Root causes — three structural asymmetries plus one pressure:
* **Vision**: writing stages saw one section; no call ever read the whole
  document. (REVIEW did see it but had no dedup mandate and its attention
  was diluted by the very bloat it should have caught.)
* **Authorization**: every stage could only ADD; appending was the applier's
  rational strategy.
* **Time**: REVIEW/verify/gate audit the per-step increment only; backlog
  content is never re-reviewed (early leaks live forever).
* **Emit pressure**: no stage had a legal "emit nothing" outcome, so mature
  documents kept being force-fed instances (reflect's "empty list if
  covered" failed because coverage was judged literally, not by
  entailment).

## 2. User rulings (2026-07-08)

1. Fix the SOURCE flow, not only a downstream tidy-up ("上游做得好,下游质量
   也会更高").
2. DRAFT sees the FULL document (a summary map cannot support dedup
   judgments — it only supports routing).
3. NO mechanical echo-hint layer feeding the LLM (rejected): literal-dup
   noise that slips through is the burst-end consolidation's job. Offline
   duplicate-phrase statistics live in validation tooling only, never in
   the pipeline.
4. Consolidation runs ONCE PER BURST as ONE whole-document LLM call;
   acceptance = val gate score not lower than before / no per-topic
   regression, with noise tolerated by design (=> two-layer
   non-inferiority, margin 1.5pp; a plain ">= before" point rule would kill
   ~half of truly lossless reorganizations).
5. Empty output is a legal success state for REFLECT and DRAFT.
6. Subtraction ladder: merge freely (with audit), supersede only with an
   explicit declaration, delete only inside consolidation and only for
   structural debris; rule retirement on accumulated evidence = future work.
7. Honest bookkeeping: an accepted consolidation re-bases best_score to its
   fresh measured mean (small within-margin retreats accepted; the old
   (score, rules) pair is archived in the audit).
8. Order: consolidation clears the backlog FIRST (smoke on the real bloated
   documents doubles as the production starting point), then the upstream
   changes operate on a slim document.
9. Standing rulings that bind the implementation: LLM-canonicalize-not-gate
   (mechanical layer = trigger signals + lossless backstops + repair chains,
   never blocking validation); rich semantic content (no word caps — merge,
   never truncate); judge-only screens with revision routed back through the
   original generator; English-only prompts; token-based accounting.

## 3. Upstream (U1–U5)

| stage | change | implements |
|-------|--------|-----------|
| U1 REFLECT | COVERAGE TEST by logical entailment; refinement-tier preference (carry ONLY the increment); `vs_doc` self-claim (novel / refines / instance-of) as advisory signal; empty list = correct output | `reflect.py::_EDIT_SPEC`, `Edit.vs_doc` |
| U2 GROUP | full handle-annotated document replaces the title catalog; placement discipline (second section about an existing theme = defect) | `pipeline.py::_stage_a_group`, `prompts.GROUP_SYSTEM` |
| U3 DRAFT | re-defined as the DIFFERENTIAL drafter: classify every lesson NOVEL / INSTANCE-OF / REFINES / SUPERSEDES against the whole document; edits carry `delta` {relation, vs, increment} and `supersedes`; sources may be `absorbed_as_covered` (accounting: sources ∪ absorbed ∪ dropped = members); `edits: []` legal and audited (`group_fully_absorbed`) | `prompts.DRAFT_SYSTEM`, `pipeline.py::_check_draft/_adopt_draft/_stage_b_draft_one` |
| U4 REVIEW | new issue type `redundant_with_doc` (cite handle + quote; REVISION route: shrink to the irreducible increment) — zero input cost, REVIEW already saw the document | `prompts.REVIEW_SYSTEM` |
| U5 APPLIER | re-defined as the section's curator: "minimal complete form", equal-rank old/new, general-rule-plus-exceptions, explicit merge authorization; outputs `absorbed`/`dropped`; lossless backstop: backtick identifiers of the old body must survive or be declared -> ONE repair call -> degrade to conservative append (audited); bullet-budget curation note (trigger signal, "merge, never truncate"); NEW titles stripped of leaked `[S#k]` text at draft-check AND apply time | `prompts.APPLIER_SYSTEM/build_curation_note/build_applier_repair_user`, `pipeline.py::apply_groups/_lost_identifiers/_strip_handle_title` |

Prompt layout puts the document FIRST in every draft call so all draft calls
of a step share one cacheable prefix (vLLM APC; infra note, not mechanism).

## 4. Downstream (D): burst-end consolidation

`css/optimizer/editpipe3/metabolism.py::run_burst_consolidation`, hooked in
`tree_search.py::run_burst` AFTER the burst reward is recorded (its neutral
wobble must not enter L1 selection signals) and BEFORE the skill snapshot
(the tree inherits the tidied document).

* Object: `node.best_rules` (what spawn/MERGE consume, tree_search.py:377);
  on accept `node.rules` and `best_rules` both become the tidied document.
* One whole-document call -> per-section decisions
  `keep | rewrite | merge | delete`; "keep" carries NO body (output budget
  spent only on changes). Deterministic execution in plan order.
* Mechanical completeness: every input handle in EXACTLY one decision
  (protocol repair; then a half-split retry — cross-half merges lost, the
  tidy-up survives output-budget truncation).
* Lossless backstop: backtick identifiers of the input must survive in the
  output or be declared in `dropped_facts`; the audit archive of deleted
  originals is OUR backup and never satisfies the check. Violation ->
  ABANDON (document untouched, next burst retries).
* Acceptance: `paired_gate.run_noninferiority_gate` — symmetric FRESH
  screens on both sides (no ledger reads; the incumbent here may differ
  from node.rules whose measurements the ledger carries), discordance ->
  one symmetric escalation, then Layer 1 `n_lost <= n_gained` AND Layer 2
  `cand_mean >= inc_mean - consolidation_margin`. Sign-flip permutation p
  stays telemetry-only. On accept the val ledger resets to the candidate's
  fresh measurements.
* Reject/failed: document untouched, audited, NO retry this burst.
* Idempotent: `gate.json` checkpoint replays node-field updates on resume.
* Artifacts: `consolidation/{input_rules.md, output_rules.md, plan.json,
  audit.json, gate.json}` under the burst directory.

## 5. Config (per-env launchers opt in explicitly)

```
consolidation_enabled = True       # AW + SS launchers, rationale inline
consolidation_margin = 0.015       # ~1 sigma of the val mean
l0_section_bullet_budget = 15      # curation TRIGGER, never a cap
```

## 6. Metrics (token-based, persisted per step in gate_v3.json["metrics"])

`candidate_tokens` (count_tokens), `net_delta_chars`, `n_sections`,
`n_top_bullets`, `op_counts` (a rising amend share = the document maturing —
the observable form of the long-term "sentence-level refinement of a mature
rules.md" goal), `n_sources_absorbed_as_covered`, `n_groups_fully_absorbed`
(the empty-output health signal), `n_redundant_with_doc_issues`. Per burst:
the consolidation outcome (chars/sections before/after, gate numbers).

Health targets: net delta per step falling from ~8-10KB toward <3KB; amend
share rising with step index; consolidation change-rate falling per burst.

## 7. Validation & rollout

* Stub tests: `css/tests/test_document_metabolism.py` (15) + existing
  `test_editpipe_v3.py` (25) — full suite green.
* Real-data smoke (same discipline that caught six v3 defects):
  (a) `python tools/replay_editpipe.py consolidate <real rules.md>` against
  the live AW step11 (88K) and SS step10 (112K) documents — human review of
  the tidied output, plan ops, lost-identifier check, compression;
  (b) upstream A/B replay on recorded raw_patches (v3 runner) — net delta,
  op mix, zero silent loss;
  (c) one end-to-end step.
* Rollout: sync to the server, resume both experiments at a burst boundary;
  the first consolidation clears the backlog (the smoke-accepted tidied
  document may be adopted directly — "smoke as production").
* The pre-change steps of both live runs stand as the no-metabolism
  control arm for the paper narrative.

## 7a. Smoke findings archive (2026-07-08, for the revisit)

Round 1 (conservative prompt): model kept every fat section, -6%; 11 lost
identifiers correctly abandoned. Round 2 (mandatory targets): rewrite
appeared but the fattest section escaped the bullet-only trigger (68
bullets nested, 13 top-level) — token budget added. Round 3 (full chain:
token budgets, sliced planning, tolerant scope filter, scoped quality
repair): AW -37%, mandatory targets all executed, BUT 97 lost-identifier
flags — inspection shows three false-positive classes: (a) backtick-quoted
prose fragments counted as identifiers; (b) schematisation replacements
(real emails/phones/tickers the prompt itself orders replaced) flagged as
losses; (c) full call signatures rewritten into merged phrasing. SS: -26%,
15->35 sections (rewrite bodies carried ### lines; needs the STRUCTURE
normalization pass on consolidation output). Raw I/O of one full run:
consolidation_io_samples/aw_step11/. Open design questions parked with
these findings; upstream chain is the active bloat-control path.
