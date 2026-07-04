# Edit Pipeline v2 (editpipe) — design & validation

Status: implemented on `feat/edit-pipeline-v2`; validated by unit replay of
the real accident plus live-LLM replays against real step fixtures.
Scope: raw-edit production (reflector vocabulary) -> merger -> validation ->
repair -> apply. Per-edit verification and the gate are unchanged consumers.

## 1. The accident that motivated this (2026-07-04, AppWorld step1)

Real run `appworld_20260704_113715`, exploit step1, 59 raw edits in:

- The merger LLM emitted 13 edits; **11 carried
  `section_target = "### Temporal Context Resolution"`** while their bodies
  were eight distinct new topics plus three misplaced points. The model had
  copied the raw edits' *position hints* into what the schema treated as
  *identity*.
- `_validate_merged_edits` deduplicates section-level edits **by field
  equality**, so it silently killed 7 of 13 — including three topics with
  5-6 supporting tasks each (Spotify Library Completeness, Contextual
  Identity Resolution, Search and Sort Optimization).
- The ID-diff repair loop then did good semantic work on the 6 survivors —
  but it never saw the 7 casualties.
- The whole-document LLM apply left **empty-shell duplicate headings** in
  the candidate; that candidate passed the gate and became best.

A replay of the same input through the legacy pipeline (see §5) reproduced
the same catastrophe with 19+ silent drops — this is systematic model
behavior under this input shape, not a one-off.

Four structural causes, not four bugs:

1. **Identity had no single source of truth** — it lived in a field AND in
   the content's first heading line; the two could disagree, and did.
2. **Position and identity shared one field** (`target` in raw edits,
   `section_target` in merged edits), with different readings per stage.
3. **The mechanical layer executed semantic judgements** (field-string
   dedup enforcing "one edit per section") **with drop as the failure
   mode** — unappealable, unaudited.
4. **An LLM transcribed the whole document** at apply time, with no
   structure assertions afterward.

## 2. Design axioms

1. **Single source of identity.** An edit's identity is its `subject`;
   bodies never contain headings (headings are rendered from subjects at
   assembly). The field-vs-content disagreement is unrepresentable.
2. **Mechanical layer = syntax gate.** Deterministic code may normalize
   losslessly, detect problems, and drop *byte-identical duplicates* — and
   nothing else. Detection output is a violation, not a deletion.
3. **Semantic rulings belong to the LLM,** in the adjudication loop
   (validator + ID-diff repair). Every kill carries a reason into the
   permanent audit trail. Nothing is silently discarded.
4. **Constraints are enforced by purpose, not form.** Ablation needs
   pairwise-disjoint action regions; the disjointness table encodes exactly
   that. Two *different* new sections never conflict over a field value.
   Near-verbatim restatement (mechanically decidable via token-Jaccard line
   overlap) is detected mechanically; paraphrase stays with the validator.
5. **LLMs never do mechanical transcription.** Section surgery is
   deterministic; the only apply-time LLM is a *section-scoped* anchor
   resolver (input: one section + one edit; blast radius: that section).
   Structure assertions run after every assembly.
6. **Failure degrades position, never content.** Wrong placement -> end;
   unresolvable anchor -> section-end append; adjudication non-convergence
   -> keep-max-support, demote the rest to point-adds; remove-ops are
   idempotent no-ops when the target is already gone. Every degradation is
   audited. The pipeline's objective backstop (per-edit verification + the
   paired gate) is what finally kills weak content — with measurements, not
   string comparisons.

## 3. Vocabulary (all stages speak it)

```
kind:      add_section | rewrite_section | remove_section |
           add_point   | edit_point      | remove_point
subject:   WHICH semantic unit (identity; add_section invents its own name)
placement: add_section only — "end" | "start" | existing subject (hint only)
anchor:    point ops — text located inside the subject section
body:      content, heading-free
```

Legacy vocabulary (`new_section`/`section_rewrite`/`point_add`/...,
`append`/`insert_after`/..., `section_target`/`content`/`point_anchor`/
`after_section`) is accepted on read everywhere (old checkpoints, old run
data) and mapped losslessly.

`MergedEdit` remains the downstream wire format: `SectionEdit.to_merged()`
derives it mechanically (`content = "### subject\n" + body`), so downstream
consumers (verification, step buffer, checkpoints) are untouched and the
round-trip is lossless.

## 4. Pipeline

```
reflector (v2 vocabulary, per-minibatch raw edits)
  └─ merger LLM call            [complete_optimizer_json: parse + missing-
     │                           required-field repair]
  └─ syntax gate                [lossless normalize; detect; byte-dup only]
  └─ coherence feedback          [<=1 in-band repair at the source when the
     │                           output is structurally incoherent:
     │                           identity_collision / add_exists /
     │                           restates_existing / heading-in-body / ...]
  └─ adjudication loop (<=3)    [each round: disjointness table +
     │                           restatement detection + LLM validator;
     │                           ID-diff repair ops (replace/drop/merge/add,
     │                           allowlist-scoped, drops carry reasons);
     │                           apply-degradable violations pass through]
  └─ deterministic fallback     [on non-convergence: keep-max-support,
     │                           demote rest to point-adds; content intact]
  └─ deterministic apply        [remove -> rewrite -> add -> points;
                                 exact anchor -> normalized anchor ->
                                 section-scoped LLM resolver -> append;
                                 normalize; structure assertions]
```

Artifacts per step: `merged_edits.json` (wire format, unchanged) plus
`edit_audit.json` — the fate of every edit (kept / normalized / converted /
merged_into / demoted / dropped / degraded) with actor and reason, the
accepted risks, and convergence stats.

## 5. Validation evidence

Fixtures: real step data under `tests/fixtures/edit_pipeline/`
(appworld_step0: 51 raw edits, empty base; appworld_step1: 59 raw edits +
the actual 13-edit accident response; spreadsheetbench_step2: 38 raw edits,
3-section base).

**Unit replay of the real accident** (`test_editpipe_accident.py`): the 13
accident edits pass the v2 mechanical layer with zero silent drops; the
collision is detected as violations; the *pure deterministic path* (no LLM
at all) still delivers all 13 bodies into a structurally valid candidate.
The legacy pipeline's actual empty-shell candidate fails v2 structure
assertions — they would have caught the damage.

**Live qwen replays** (`temperature 0.7`, real fleet):

| metric | legacy (independent replay) | v2 (4 samples incl. injection) |
|---|---|---|
| silent drops | 19 (field-dedup massacre reproduced) | 0 / 0 / 0 / 0 |
| lost-signal coverage (7 probes) | 5/7 | 7/7, 6/7, 7/7, 6/7 |
| convergence | repair exhausted, 3 unresolved conflict pairs delivered | converged every run (1-3 rounds) |
| structure assertions on candidate | shells/duplicates in the original accident run | clean every run |
| audit trail | none | every drop carries actor + reason |
| optimizer LLM calls / wall | 8 / 1326s | 2-6 / 185-523s |

Raw metric JSONs: `docs/editpipe_v2_replays/`. The final replay (with
restatement detection + the "Delta only" guidance) converged in ONE round
with TWO LLM calls total: the merger emitted point edits against existing
sections instead of near-duplicate new sections, unprompted by any repair.

Iteration driven by the first live replay: the merger tended to restate
existing sections as near-duplicate new sections ("Pagination Protocol"
duplicating "Pagination Discipline" at 67% line overlap). Countermeasure:
mechanical restatement detection (token-Jaccard >= 0.7 per line, >= 50% of
body lines) feeding the coherence/adjudication rounds + a "Delta only"
merger guidance block. Verified on the very output that exposed it (both
inflating sections flagged; 18 genuinely-new sections untouched).

**Live accident injection** (the strongest end-to-end perturbation): the
REAL 13-edit accident response was injected verbatim as the merger's first
output, with every subsequent call hitting real qwen. Observed defense
sequence — (1) the missing-required-field hook fired on the legacy-shaped
payload and one schema-repair call recovered `subject` for all eight
mislabeled sections *from their own content*; (2) the syntax gate stripped
the now-redundant heading lines (8 audited normalizations); (3) the
adjudication loop merged the two misplaced points into their host, dropped
one semantically-misplaced duplicate WITH a written reason, and converged
in 3 rounds; (4) apply assertions green. Net: 13 injected -> 10 final
edits, all 7 previously-massacred topics alive under their correct names,
0 silent drops, 6 LLM calls, 185s.

**Perturbation suite** (stub-driven, `test_editpipe_loop.py`): colliding
subjects, missing fields, heading-polluted bodies, dangling placements,
repair returning empty/out-of-scope/sneaky operations, validator garbage,
LLM failures at every stage — the loop converges or falls back, never
blocks, never silently loses content.

## 6. Integration & migration

- `CSSConfig.edit_pipeline: "v2" | "legacy"` (default v2). Three switch
  points in `exploitation.py`: merger stage, per-edit verification apply,
  collective candidate apply. The legacy path is preserved for ablation.
- Resume compatibility: `merged_edits.json` written by old runs parses
  through `SectionEdit.from_dict` (legacy field names accepted); running
  servers pick the new path up at the next step boundary after a code sync.
- `EditVerification` now carries `after_section`/`point_anchor` so
  surviving edits keep their positions through the collective apply (fixes
  a pre-existing information-loss bug independent of v2).

## 7. Legacy silent-path inventory and v2 dispositions

Exhaustive accounting of every silent-drop / silent-no-op / silent-mutation
path in the legacy chain, and what v2 does instead. "unchanged" means the
behavior was already correct (parse failures and hard LLM failures have no
content to preserve — they are retried by the JSON-repair layer and logged).

| legacy path | v2 disposition |
|---|---|
| reflector: unparseable proposer output -> `[]` | unchanged (repair retry + log) |
| reflector: bad op / locator-less refinement edit dropped | locator-less edit WITH content demotes to add_point (content survives); only content-less AND locator-less is junk |
| reflector: `raw_edits[:budget]` truncation | kept (deliberate budget) but now logged with counts |
| merger: `_validate_merged_edits` drops — invalid delta_type / content not `### ` / empty target_tasks / from_dict raises | all become violations or lossless normalizations; missing fields go through the required-field repair hook; **no mechanical drops** |
| merger: duplicate section_target drop (the massacre) | identity_collision violation -> semantic adjudication; byte-identical duplicates are the only mechanical kill |
| merger: silent auto-convert rewrite-of-missing -> new_section | kept (mechanically lossless) but audited as `converted` |
| validator: optimistic `[]` on LLM failure | unchanged by design (mechanical detections still stand; apply degrades safely) — now with structural JSON repair first |
| repair: out-of-scope / malformed ops rejected | unchanged (scope guard) + every accepted drop now REQUIRES a written reason -> audit |
| salvage: phantom point_edit/point_remove dropped | dependency_on_new violation -> adjudication; fallback creates the section or treats removal as idempotent — content never dropped |
| repair exhaustion: proceed with unresolved conflict_pairs -> downstream best-pick drops losers | deterministic fallback: keep-max-support + demote rest; nothing dropped; final-round repair that clears violations counts as convergence |
| apply: anchor not found -> silent no-op | degradation chain: normalized match -> section-scoped LLM resolver -> content-preserving append; remove treated as idempotent; all audited |
| apply: whole-document LLM rewrite, no assertions | deterministic section surgery + post-assembly structure assertions + normalize |
| collective apply loses anchor/placement (EditVerification gap) | EditVerification now carries after_section/point_anchor end to end |
| purity_telemetry (log-only) | wired identically in the v2 path (monitoring continuity) |

## 8. Cost profile

Converged path: 1 merger call + (0-1) coherence repair + 1 validator call
per adjudication round (+1 repair call per non-converged round) + 0 apply
calls (deterministic) + section-scoped resolver calls only on anchor
misses. Comparable to legacy (which spent 1 merger + 1 validator + up to 3
repair + 1-per-edit whole-document apply calls), while the apply stage is
now free and assertion-guarded.
