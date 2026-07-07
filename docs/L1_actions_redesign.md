# L1 Actions Redesign — Coverage Ledger, Selection Debiasing, NEW/REFINE/MERGE

Status: **APPROVED** (user sign-off 2026-07-07). Implementation branch:
`feat/l1-actions-redesign`.

This document is the implementation blueprint for the L1-layer redesign agreed
after the AppWorld run `appworld_20260706_010423` post-mortem. Scope: the L1
data layer, selection, action dispatch, and exploration supply lines. Out of
scope (unchanged): the L0 burst internals (editpipe / verify / gate), the
saturation predicate (`node_stalled`), the GT firewall, and the val-boundary
role of the paired gate.

---

## 0. Post-mortem findings this design answers

Evidence run: `runs/appworld_20260706_010423` (13 decisions, ~33 h, global
best frozen at the root's first burst 0.877).

1. **Exploration never fired.** `global/unsolved/groups.json` was computed as
   the INTERSECTION of per-node residual sets, where each residual was the
   failure set of that burst's sampled analysis batch (support drifts per
   burst; root logged residuals `[4, 0, 0]` while val sat at 0.877). One node
   emptied the intersection at decision 1; every NEW child was conceived with
   `findings=""`. The 4-8 paradigm-sensitive tasks per synthesis — the actual
   gold — were computed, written to `meta.json`, and discarded.
2. **Root monopolized selection (8/13 decisions; 4 spawned null).** Three
   accounting defects, all measured live:
   - the `alpha*accept_slope` term scored a newborn's inevitable
     early-accept-then-settle pattern as the maximum negative trend
     (n0001: `[1,1,1,0,0]` -> slope −0.300) while scoring the root's
     basin-top churn accepts as positive (+0.061) — the same signal
     `node_stalled`'s docstring rules meaningless;
   - spawns did not increment the selection count: root sat at `n_bursts=3`
     across 8 selections while its children's bursts RAISED the global T,
     inflating root's own exploration bonus (1.294 at d10 -> 1.324 at d12);
   - spawn decisions booked `child.val_after − empty-rules-baseline`
     (+0.43..+0.59) as reward — 20x a burst reward, ~90% cold-start recovery.
3. **REFINE starved upstream.** Children got 1-2 bursts each, never reached
   saturation, so the REFINE path never had a chance to fire. The trigger
   itself is sound.

## 1. Data layer: two books

### 1.1 Node coverage ledger — the sole authority for solved/unsolved

File: `global/coverage/ledger.json` (atomic writes). Model:

```
(node_id, task_id) -> {attempts, passes, kinds, last_decision}
```

plus the registered full train task-id list (needed for `uncharted`).

**What is recorded** (the precise definition of "the node's real
configuration"): the L0 ON-POLICY train rollout — each step's minibatch
under the node's CURRENT deployed rules (including mid-burst rules_s
states, which are the deployed configuration at that step). This is the
node's SOLE solved-evidence stream.

**What is NOT recorded**:
- probe rollouts (user ruling: a behavior prompt is not the node's actual
  strategy; probe reachability must never flip a task's solved status —
  otherwise a lucky probe can empty `global_unsolved`, trigger MERGE over
  strategies none of which solve the task, and permanently orphan it);
- verify rollouts — REVISED 2026-07-07 (code-review ruling): every verify
  rollout, target AND control alike, runs the CANDIDATE rules, and a
  gate-rejected candidate is a discarded, never-deployed configuration.
  Booking its passes as solved re-creates the probe deadlock through a
  different door (a task leaves `global_unsolved` although no deployed
  strategy solves it). Same principle as probes: reachability means the
  ACTUAL deployed configuration;
- any val/test rollout (val-boundary invariant: val crosses the boundary
  only as aggregate statistics — gate verdicts, node `val_score`; val task
  identities never reach generation-side inputs).

Recording is EXPLICIT: call sites invoke `ledger.record_groups(node_id,
groups, kind, decision_index)` after each train rollout batch. Explicit
beats a hook inside the rollout layer: which rollouts count is auditable at
the call site, and the probe path simply never calls it.

**States** (m = `cfg.ledger_min_attempts`, default **1** — user ruling
2026-07-07, revisit after live observation):

- `solved(n,t)`: passes >= 1
- `unsolved(n,t)`: attempts >= m and passes == 0
- `unattempted(n,t)`: attempts < m

**Derived global sets** (recomputed on ledger change; the ledger partition
hash is the signature that gates downstream recomputation):

- `global_unsolved` = tasks attempted (>= m) by >= 1 node and solved by NO
  node — union-of-evidence semantics: a task leaves this set only by being
  actually solved somewhere; sampling drift cannot empty it (monotone
  except for genuine first-solves);
- `paradigm_sensitive` = tasks solved at >= 1 node AND unsolved at >= 1
  node; indexed per node as `shortfall(n)` = tasks n has unsolved that some
  other node solved;
- `uncharted` = tasks no node has attempted >= m times. Kept SEPARATE from
  `global_unsolved`: no failure evidence, no difficulty claim.

**Relation to existing materials**: `residual_history.jsonl` and the
frontier narratives stay (failure-mode reading). The intersection
computation inside `materials/global_unsolved.synthesize_global` is
REMOVED; the task set feeding the LLM grouping + per-group narrative +
priority comes from the ledger's `global_unsolved`. The grouping layer
itself is kept — "which unsolved tasks share a failure family" remains an
LLM question.

**Backfill**: `tools/rebuild_coverage_ledger.py` reconstructs the ledger
from an existing run directory (so the live AppWorld run can resume under
the new mechanism with a full book).

### 1.2 Probe leads — signal only, never state

File: `global/exploration/leads.json`:

```
task_id -> [{behavior_prompt, n_pass, k, session_ref, decision_index}]
```

top-`cfg.leads_per_task` (default 3) entries per task by pass rate then
recency. Written by `dispatch_probe` whenever a probe passes on a task.

Consumed at three points: (a) NEW/MERGE conception inputs ("known lead:
behavior X passed 2/2 on t"); (b) exploration briefings; (c) target
ordering — an unsolved task WITH a lead ranks first for NEW (the idea has
been found; no strategy has absorbed it yet).

Leads never mutate solved/unsolved status (deadlock regression test
required).

## 2. Selection layer

Formula (slope term DELETED):

```
score = val_score + beta * sqrt(ln(max(T, 2)) / n_selections)
```

- **Why delete rather than fix the slope term**: the behavior it wants to
  reward ("still improving") is already expressed by val rising between
  selections; the behavior it wants to punish ("stalled") is already owned
  by the saturation state machine. It was redundant in intent and inverted
  in practice. `cfg.alpha` becomes deprecated (field kept for old-config
  compatibility; unused). `cfg.W` STAYS — it is the general trend window
  with non-SELECT consumers (reflect's recent-rejected window, longitudinal
  analytics).
- **`n_selections` (new TreeNode field)**: incremented EVERY time the node
  is selected — burst, successful spawn, failed spawn alike (each consumed
  a decision). `n_bursts` keeps its original semantics (saturation windows,
  materials, and the spawn-failure cooldown, which must stay burst-based:
  a cooldown unlocks on new EVIDENCE, i.e. a burst landing somewhere, not
  on another decision being burned). `T = total_selections(tree)`.
  Children are born with `n_selections = 1` (spawn + first burst is atomic;
  the pool never contains an inf-UCB node).
- **Checkpoint compatibility**: old checkpoints lack the field;
  `from_dict` defaults `n_selections` to `n_bursts` (closest historical
  approximation).
- **Spawn reward rebased**: the decisions ledger records
  `reward = child.val − parent.val` for spawn decisions (n0007's book entry
  becomes −0.07, not +0.59). This is honest bookkeeping for every ledger
  consumer (human or LLM); the selection-side brake is `n_selections`, not
  this number. `child_baseline` and `val_after` stay in the record; the
  child's own first-burst reward keeps its meaning inside
  `child.burst_rewards`.
- **Root spawn-failure policy**: root is the only NEW/MERGE entry point, so
  a 3-strike terminal on root would seal off the phase transition. Root at
  `spawn_fail_count >= 3` gets a LONG block instead of terminal:
  `spawn_block_T = total_bursts + 3 * 2^(fail−3)`, with a WARNING event
  (`root_spawn_blocked`). Strategy nodes keep the 3-strike terminal.
  A root DECLINE (world-state says the action is pointless, e.g. degenerate
  MERGE matrix — not a pipeline failure) blocks without incrementing
  `spawn_fail_count`.
- **Progressive widening**: NOT implemented now. Observation item — revisit
  if the root still over-spawns after the accounting fixes.

## 3. Action dispatch (root three-way)

When a SATURATED node is selected:

| Node | Condition (from ledger) | Action |
|---|---|---|
| root | `global_unsolved != {}` or `uncharted != {}` | **NEW** |
| root | both empty and complementarity matrix non-degenerate | **MERGE** |
| root | both empty and matrix degenerate (no node has exclusive coverage) | **decline + block** (train signal exhausted; tree continues on REFINE depth or ends all_terminal) |
| strategy node | — | **REFINE** |

The `uncharted != {}` clause prevents declaring "full coverage" while tasks
were simply never sampled. The transition is REVERSIBLE: if a MERGE child
regresses and re-opens unsolved tasks, the next root spawn is NEW again.

## 4. NEW pipeline (revisions only)

- **Step 0 exploration**: target = highest-priority `global_unsolved` group
  (LLM grouping kept, data source switched) + `uncharted` tasks added to
  the menu tagged "never attempted" + leads injected into the briefing.
  Session artifacts cached under
  `global/exploration/sessions/<group_key>_<ledger_sig>/`; an unchanged
  signature reuses the cached findings instead of burning probes again.
- Step 1 target selection: the `global_unsolved` block is rendered from the
  ledger (per-task: how many nodes tried, how many attempts).
- Steps 2-5 (conception + novelty vs ALL prior strategies, draft,
  altitude/purity) unchanged. Child `rules = ""` (zero inheritance —
  standing user ruling) unchanged.

## 5. REFINE pipeline (revisions only)

- Trigger unchanged (saturated strategy node selected).
- **Targeting priority**: `shortfall(n)` (this node unsolved, someone else
  solved) FIRST; the existing escalate-U-group path becomes secondary.
- **Exploration supply**: for each shortfall task the briefing carries the
  solver's identity, its strategy head, and (when available in the solver's
  analysis artifacts) the success-trajectory narrative. The
  `neighbor_tasks` menu — built for contrast and never fed until now — is
  populated with the solver's solved tasks, so the director can run
  same-task contrast probes (this node's behavior vs the solving sibling's
  behavior). When a node has neither shortfall nor escalated U groups,
  exploration is SKIPPED (revision proceeds from its own failure
  narratives).
- Output: MPO-style section replacement of the strategy; **rules inherited
  from the parent** (the revision targets the strategy layer; verified
  rules are assets). No tree-wide novelty for REFINE (a revision naturally
  resembles its parent); the substantive-difference check vs the parent
  stays.

## 6. MERGE pipeline (new; seven steps)

Persisted under `nodes/<child>/gen/` step-by-step like NEW (step-granular
resume). `branch_type = "MERGE"`. Spawn + first burst atomic; enters the
normal pool; can be REFINEd later; reuses the spawn-failure cooldown.

- **Step 0 — complementarity matrix** (mechanical, zero LLM):
  `gen/merge_matrix.json` from the ledger — per-node exclusive coverage
  (solved here, solved nowhere else), pairwise complementarity, common
  set. **Includes pruned/terminal nodes**: a node's death does not kill its
  coverage evidence; MERGE may resurrect a pruned branch's exclusive
  capability. Degenerate matrix (no exclusive coverage anywhere) -> root
  decline (see §3).
- **Step 1 — conflict-detection exploration** (mode `MERGE`): briefing =
  matrix + full source strategies + dossier digests + leads. The director's
  job: find coverage-complementary but behaviorally-CONFLICTING strategy
  pairs (live example from the run: n0002 "Construct the Entity Bridge
  Before Data Retrieval" vs n0007 "Abandon ... Bridge-First"), propose a
  reconciliation hypothesis (e.g. conditional routing by task features),
  write it as a draft fused behavior prompt, and probe it on representative
  tasks from BOTH sides. Irreconcilable pairs are reported as "keep the
  division of labor; do not fuse this pair". The report is an
  evidence-backed fusion blueprint.
- **Step 2 — MERGE conception**: inputs = matrix + exploration report;
  output = `{base_node, contributions: [{source, sections, adaptation}],
  conflict_resolutions, expected_coverage: [task_ids]}`.
- **Step 3 — novelty, scope narrowed**: confronted ONLY against prior MERGE
  strategies (a fusion necessarily resembles its parents; tree-wide novelty
  would false-kill every fusion; repeat-fusion must still be caught).
  Retry-with-critique loop (``_confront_merge_novelty``, mirroring NEW): a
  duplicate verdict re-conceives with the critique appended, up to
  ``gen_novelty_retries``; the first MERGE short-circuits novel.
- **Steps 4-5 — draft + altitude/purity**: same machinery as NEW.
- **Step 6 — selective rules integration** (user ruling: LLM judges
  against the FUSED strategy; never wholesale copy):
  1. *Per-source selection* — one LLM call per source node: input = fused
     strategy (the yardstick) + that source's rules split into sections +
     that source's exclusive-coverage task list; verdict per section:
     `keep` (compatible with the fused strategy and supporting that
     source's contributed coverage) or `drop` (conflicting/redundant), with
     reasons. Per-source calls also keep each call within context budget.
  2. *Consolidation* — one LLM call over all kept sections: deduplicate
     (delete copies, keep one), resolve direct conflicts (keep the section
     from the higher-val / stronger-coverage source), organize.
  - **Fidelity constraint (approved)**: selection and pruning ONLY — the
    integrator never rewrites section bodies. Rules are L0-gate-verified
    assets; a rewrite is unverified content. Wording mismatches with the
    fused strategy are left to the first burst's L0 — the controlled,
    verified rewriting machine.
  - Outputs: `child.rules` + `dossier/rules_provenance.json` (per-section
    source attribution).
- **Step 7 — coverage-preservation check** (MERGE-specific acceptance
  signal): after the first burst lands in the ledger, compare against
  `expected_coverage`; write `dossier/merge_coverage_check.json`
  (kept/lost). Record-only — no automatic action; the lost-list feeds the
  next MERGE/REFINE.

## 7. Exploration subsystem — unified view

| Mode | Target source | Typical probe | Report answers |
|---|---|---|---|
| NEW | top `global_unsolved` group + uncharted + leads | assault: new behavior hypothesis x unsolved task | what behavior cracks the frontier |
| REFINE | `shortfall(n)` + escalated U groups | contrast: same task, this node's vs solver's behavior | where the shortfall diverges |
| MERGE | matrix conflict pairs | adjudication: fused draft on tasks from both sides | which reconciliation holds |

Shared: director loop, silent 48-probe guardrail, `k_max=2`, full session
archive, probe passes -> leads (never the ledger). Cost note: one session
is bounded by ~48 probes ≈ one val evaluation; the signature cache means a
session re-runs only after the ledger actually changes.

## 8. Config & compatibility

- New keys: `ledger_min_attempts` (default 1), `leads_per_task` (default 3).
- Deprecated (kept, unused): `alpha`. (`W` stays: general trend window with
  non-SELECT consumers.)
- MERGE reuses existing knobs (`gen_novelty_retries`, draft token caps,
  cooldown constants).
- Old checkpoints: `n_selections` defaults to `n_bursts`; a missing ledger
  is rebuilt by the backfill tool; running experiments are untouched (new
  mechanism activates on the next agreed launch/resume — per-env config
  covenant applies).

## 9. Decision log (user rulings, 2026-07-07)

1. Exploration targets move from intersection to union-of-evidence
   residual, computed on TRAIN only (val stays aggregate-only). Approved.
2. Slope term deleted outright. Approved.
3. Selection charged per selection (`n_selections`); spawn reward rebased
   to parent val. Approved.
4. Probe passes are leads, never solved-state. Approved (user-initiated).
5. MERGE as the third root action on true full coverage; exploration does
   conflict adjudication. Approved (user-initiated).
6. MERGE rules integration: LLM selective integration judged against the
   fused strategy; select-and-prune only, no rewriting; wording adaptation
   deferred to first-burst L0. Approved.
7. Root spawn-fail 3-strike -> long block, never terminal. Approved.
8. MERGE trigger requires `global_unsolved` AND `uncharted` both empty;
   degenerate matrix -> decline. Approved.
9. `ledger_min_attempts = 1` for now; revisit after live observation.
   User-set.
10. MERGE matrix includes pruned/terminal nodes. Approved.
11. Progressive widening deferred to an observation item. Approved.
12. Verify rollouts (candidate configurations, gate-rejected included) are
    EXCLUDED from the coverage ledger — the L0 on-policy rollout is the sole
    solved-evidence stream. Code-review ruling 2026-07-07, extending ruling
    4's principle (reachability = deployed configuration) from probes to
    rejected candidates.
13. Post-review hardening (2026-07-07): whole-section trim keeps rules text
    and provenance 1:1; ``uncharted`` subtracts solved; root never declares
    full coverage over an unregistered universe (loud registration + a
    ``registered_count`` guard); MERGE self-checks its full-coverage
    precondition and declines otherwise; ledger readers/writers share an
    RLock and ``save()`` deep-copies under it; the exploration-findings
    signature uses the run's ``ledger_min_attempts``; leads recording lives
    at the ``dispatch_probe`` boundary (§1.2 as written).
