# Cognitive Strategy Search (CSS): Methodology

## 1. System Overview

Cognitive Strategy Search (CSS) is a Monte Carlo Tree Search (MCTS)-based
optimization framework that searches for the optimal *skill document* — a pair
of markdown files `strategy.md` (cognitive strategy) and `rules.md` (tactical
rules) — for a frozen, task-executing LLM agent. The skill document is injected
into the agent's system prompt as read-only context; the agent itself is never
fine-tuned. CSS discovers *what to tell the agent* to maximize task-solving
performance, searching both the space of high-level thinking strategies and the
space of low-level tactical rules.

### 1.1 Two-Layer Skill Architecture

The skill document is physically separated into two layers with distinct
optimization loops, enforced by code rather than by prompt convention
(`css/skill_document.py`):

| Layer | File           | Content                                      | Optimizer | Mutability during L0 |
|-------|----------------|----------------------------------------------|-----------|----------------------|
| L1    | `strategy.md`  | Cognitive strategy — HOW to think             | L1 Proposal Cycle | Read-only |
| L0    | `rules.md`     | Tactical rules — WHAT to do (checklists, gotchas, procedures) | L0 Exploitation | Read-write |

The frozen task agent sees a single combined text rendered by
`SkillDocument.combined_skill_text()` (`css/skill_document.py:155-166`):

```
# Cognitive Strategy
<strategy.md content>

# Tactical Rules
<rules.md content>
```

The physical separation is load-bearing: the L0 optimizer structurally cannot
mutate the strategy because it is only ever handed `rules.md` as an edit target.

### 1.2 Search Tree

CSS maintains a search tree where each node represents a `(strategy, rules)`
pair. The root node is seeded at cold start (typically with an initial strategy
and empty rules). Branching produces child nodes with new strategies; L0
exploitation refines the rules within a node. Nodes are selected via UCB1,
pruned by paired-bootstrap sibling dominance, and the best node's skill document
is the output of the search.

### 1.3 Round-Based Loop

The system operates in rounds, each consisting of three phases
(`css/orchestrator.py`):

```
                     ┌──────────────────────────────────────────────────────────┐
                     │                   CSS MAIN LOOP                         │
                     │                                                          │
  Cold Start ──────> │   ┌─────────────────────────────────────────┐            │
  (root node)        │   │          ROUND  r = 1, 2, ...           │            │
                     │   │                                          │            │
                     │   │  1. SELECT (UCB1 top-K active nodes)     │            │
                     │   │          |                                │            │
                     │   │          v                                │            │
                     │   │  2. PER-NODE EPOCH (for each selected):  │            │
                     │   │     a) On-policy train rollout            │            │
                     │   │     b) L0 EXPLOITATION (multi-step)       │            │
                     │   │     c) Analysis (pattern mining)          │            │
                     │   │     d) Validation eval -> val_score       │            │
                     │   │          |                                │            │
                     │   │          v                                │            │
                     │   │  3. SYNC POINT:                          │            │
                     │   │     a) PRUNE (paired bootstrap)           │            │
                     │   │     b) BRANCH decision:                   │            │
                     │   │        - not saturated -> EXPLOITATION    │            │
                     │   │        - saturated -> L1 PROPOSAL         │            │
                     │   │           -> new child node if effective  │            │
                     │   │                                          │            │
                     │   └─────────────┬───────────────────────────┘            │
                     │                 |                                         │
                     │                 v                                         │
                     │   Terminate when: no progress + all saturated,           │
                     │                   or max_rounds reached                  │
                     │                                                          │
                     │   Output: best node's (strategy.md, rules.md)           │
                     └──────────────────────────────────────────────────────────┘
```


## 2. Overall Architecture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                         SEARCH TREE (MCTS)                                   │
│                                                                              │
│   Root ──┬── Node A (strategy_a, rules_a)  [val=0.72, active]               │
│          │      └── Node A1 (strategy_a1, rules_a1)  [val=0.68, pruned]     │
│          └── Node B (strategy_b, rules_b)  [val=0.78, active, BEST]         │
│                 └── Node B1 (strategy_b1, rules_b1)  [val=0.75, active]     │
│                                                                              │
│   Per Node:                                                                  │
│   ┌──────────────────────────────────────────────────────────────────┐       │
│   │  strategy.md  (L1, read-only during L0)                         │       │
│   │  rules.md     (L0, mutated by exploitation)                     │       │
│   │  step_buffer   (L0 optimization history)                        │       │
│   │  val_score     (task_hard on validation set)                    │       │
│   │  best_score / best_rules  (best-ever snapshot)                  │       │
│   └──────────────────────────────────────────────────────────────────┘       │
│                                                                              │
│   UCB1 SELECT ──> Per-Node Epoch ──> SYNC (PRUNE + BRANCH)                  │
│                        |                       |                             │
│                   L0 EXPLOITATION         L1 PROPOSAL                        │
│                   (inner loop)           (if saturated)                       │
│                        |                       |                             │
│                   refine rules.md         new child node                     │
│                                          (new strategy,                      │
│                                           empty rules)                       │
└──────────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────────┐
│                     GROUND TRUTH FIREWALL                                    │
│                                                                              │
│   TargetOnlyClient ─── frozen agent (rollouts, scoring)                     │
│         |                                                                    │
│   [cannot call optimizer]                                                    │
│                                                                              │
│   OptimizerOnlyClient ─── edit proposals, analysis, strategy design         │
│         |                                                                    │
│   [cannot call target; GT firewall injected into EVERY optimizer prompt]     │
│                                                                              │
│   Invariant: optimizer may REASON from ground truth but its OUTPUT           │
│              must NEVER depend on ground-truth values.                       │
└──────────────────────────────────────────────────────────────────────────────┘
```


## 3. L0 Exploitation (V2) — The Inner Optimization Loop

L0 Exploitation is the core optimization loop that iteratively improves
`rules.md` within a fixed cognitive strategy. It is orchestrated by
`run_exploitation_epoch` and `run_l0_step` in `css/optimizer/exploitation.py`.

### 3.1 Epoch Structure

An exploitation epoch (`run_exploitation_epoch`, line 722) runs multiple steps
until one of two stopping conditions is met:

1. **Saturation**: `N` consecutive gate rejections (configurable, default 5).
2. **Step cap**: `max_l0_steps_per_epoch` steps consumed (default 20).

Each step operates on a fresh batch of training tasks. The epoch shuffles
`train_items` with a deterministic seed (`cfg.seed + epoch + batch_round *
10000`), partitions them into batches of `cfg.batch_size` (default 40), and
processes batches sequentially. Each batch gets its own on-policy rollout under
the node's *current* (evolving) rules, so steps after the first do not re-see
stale trajectories.

### 3.2 Single Step Flow

Each L0 step (`run_l0_step`, line 419) proceeds through a five-stage pipeline.
Every stage is checkpointed to disk for crash recovery.

```
┌───────────────────────────────────────────────────────────────────────────────┐
│ STEP 0: ON-POLICY SAMPLING  (run_exploitation_epoch, line 812-826)           │
│                                                                               │
│  Input:  train batch (batch_size tasks)                                      │
│  Action: rollout with node's CURRENT rules, k_rollouts per task              │
│  Output: batch_results: list[TaskResult] — fresh on-policy trajectories      │
│                                                                               │
│  Design: on-policy ensures the optimizer sees failures/successes              │
│          under the CURRENT rules, not stale trajectories from                 │
│          before earlier edits were applied.                                   │
└──────────────────────────────┬────────────────────────────────────────────────┘
                               │
                               v
┌───────────────────────────────────────────────────────────────────────────────┐
│ STEP 1: REFLECT  (css/optimizer/reflect.py:reflect_epoch)                    │
│                                                                               │
│  Input:  on-policy trajectories, strategy (read-only), rules (edit target),  │
│          step_buffer (rejected edits, failure patterns)                       │
│  Action: split trajectories by outcome → concurrent minibatch analysis       │
│  Output: list[RawPatch] — raw edit proposals with source_tasks attribution   │
│                                                                               │
│  Three reflect modes (cfg.reflect_mode):                                     │
│    "plan_a" (default) — per-task grouping → three-way fused proposers:       │
│      - pure-fail minibatches → FAILURE_PROPOSER                              │
│      - pure-pass minibatches → SUCCESS_PROPOSER                              │
│      - mixed-outcome tasks  → CONTRASTIVE_PROPOSER (per task)                │
│    "plan_b" — success analyst → inject insights into fail/contrastive        │
│    "legacy" — flat fail/success split, no per-task grouping                  │
│                                                                               │
│  Each proposer emits at most L=l0_edit_budget (default 3) small,             │
│  single-theme edits. Source_tasks are tracked per edit for ablation.          │
│  Checkpoint: raw_patches.json                                                │
└──────────────────────────────┬────────────────────────────────────────────────┘
                               │
                               v
┌───────────────────────────────────────────────────────────────────────────────┐
│ STEP 2: MERGER  (css/optimizer/aggregate.py:merger)                           │
│                                                                               │
│  Input:  rules.md, list[RawPatch], step_buffer history                       │
│  Action: single LLM call consolidates raw patches into section-level edits   │
│  Output: list[MergedEdit] — one per ### section, complete target content     │
│                                                                               │
│  Key invariants:                                                             │
│    - ONE edit per section (enables independent ablation)                      │
│    - Section-level targets (### headings), not line-level ops                │
│    - Derivation audit trail (raw edit indices, kept/dropped decisions)        │
│    - History injection (past edit verification results inform merging;        │
│      configurable via merger_inject_history, window=merger_history_window)    │
│                                                                               │
│  Seven merger principles guide the LLM:                                      │
│    1. One edit per section (independence)                                     │
│    2. Group by content, not source type                                      │
│    3. Gap-align (new_section vs rewrite vs refinement)                       │
│    4. Preserve existing content                                              │
│    5. Resolve contradictions                                                 │
│    6. Derivation transparency (detailed rationale + derivation fields)        │
│    7. Quality over quantity                                                   │
│    8. Learn from history (conditional on history availability)                │
│                                                                               │
│  Post-validation (_validate_merged_edits) enforces:                          │
│    - content starts with "### "                                              │
│    - delta_type in {new_section, section_rewrite, section_refinement}        │
│    - target_tasks is non-empty                                               │
│    - no duplicate section_target                                             │
│    - auto-correct: rewrite/refinement of non-existent section → new_section  │
│                                                                               │
│  Checkpoint: merged_edits.json                                               │
└──────────────────────────────┬────────────────────────────────────────────────┘
                               │
                               v
┌───────────────────────────────────────────────────────────────────────────────┐
│ STEP 3: PER-EDIT ABLATION VERIFICATION  (parallel)                           │
│         (_run_parallel_edit_verification, line 298)                           │
│                                                                               │
│  Input:  merged edits, current rules, incumbent baselines                    │
│  Action: for EACH edit independently:                                        │
│    a) LLM-apply the single edit to current rules (llm_apply_edit)            │
│    b) Rollout candidate on that edit's target_tasks (k_rollouts each)        │
│    c) Apply binary solvability criterion (_evaluate_edit_criterion)           │
│  Output: list[EditVerification] — per-edit pass/fail + task_results          │
│                                                                               │
│  Edits run in parallel via ThreadPoolExecutor(max_api_workers).              │
│  Checkpoint: edit_verifications.json                                         │
└──────────────────────────────┬────────────────────────────────────────────────┘
                               │
                               v
┌───────────────────────────────────────────────────────────────────────────────┐
│ STEP 4: COLLECTIVE APPLY + SIZE GUARD  (line 608-624)                        │
│                                                                               │
│  Input:  surviving edits (passed ablation), current rules                    │
│  Action: apply ALL surviving edits via LLM (llm_apply_edits, tool call)      │
│  Output: candidate_rules — the proposed new rules.md                         │
│                                                                               │
│  The LLM apply uses function calling (write_rules_md tool) for structured    │
│  extraction. Falls back to deterministic apply_all_section_edits on failure.  │
│  Size guard logs a warning if rules exceed rules_max_chars (default 60K)     │
│  but NEVER truncates.                                                        │
│  Checkpoint: candidate_rules.md                                              │
└──────────────────────────────┬────────────────────────────────────────────────┘
                               │
                               v
┌───────────────────────────────────────────────────────────────────────────────┐
│ STEP 5: FINAL VALIDATION GATE  (css/evaluation/gate.py:evaluate_gate)        │
│                                                                               │
│  Input:  candidate_rules, current_score, best_score                          │
│  Action:                                                                     │
│    a) Score candidate on FULL validation set (evaluate_candidate,            │
│       k_override=exploitation_val_k)                                         │
│    b) Pure decision: cand > current → accept; cand > best → accept_new_best │
│  Output: GateResult {action, current_rules, current_score, best_*}           │
│                                                                               │
│  If accepted: node.rules = candidate_rules                                   │
│  The step buffer records the full edit_verifications regardless of gate       │
│  outcome, so future merger calls can learn from history.                      │
│  Checkpoint: gate_decision.json                                              │
└───────────────────────────────────────────────────────────────────────────────┘
```

### 3.3 LLM Apply Mechanism

The apply stage (`css/optimizer/section_apply.py`) offers two paths:

1. **LLM-based apply** (primary, `llm_apply_edit` / `llm_apply_edits`): A
   system prompt instructs the LLM to mechanically place edit content into the
   document, using a `write_rules_md` function-calling tool for structured
   extraction. Rules: reproduce edit content word-for-word, preserve unmodified
   content, no commentary or meta-information.

2. **Deterministic apply** (fallback, `apply_section_edit`): Parses `rules.md`
   into `RulesSection` objects by `###` headings (respecting fenced code blocks),
   then performs structural insert/replace operations. Used automatically when
   LLM apply returns empty or fails.

### 3.4 Step Buffer and History

Each step appends a `StepBufferEntry` (`css/data/step_buffer.py`) recording:
- Step index, epoch, action (accept/reject), score before/after
- Edit verifications (full content, task_results, passed/failed)
- Failure patterns mined from raw patches
- Number of surviving edits

The buffer serves three roles:
1. **Saturation detection**: `consecutive_rejects() >= N` triggers L1.
2. **SELECT signal**: `accept_slope(W)` feeds the UCB1 formula.
3. **Optimizer context**: recent failure patterns and rejected edits are
   injected into future reflect/merger prompts to prevent dead-end revisitation.

Cross-epoch saturation is prevented by `reset_saturation()`, which inserts a
synthetic `epoch_reset` sentinel that breaks the consecutive-reject streak.


## 4. Binary Solvability Criterion

The per-edit verification uses a two-layer decision function
(`_evaluate_edit_criterion`, `css/optimizer/exploitation.py:194-220`):

### 4.1 Layer 1: Binary Solvability (Pass@K)

Each target task is categorized by comparing incumbent and candidate solvability
(pass rate > 0):

| Incumbent Solvable | Candidate Solvable | Status           |
|--------------------|--------------------|------------------|
| No                 | Yes                | **GAINED**       |
| Yes                | No                 | **LOST**         |
| Yes                | Yes                | RETAINED         |
| No                 | No                 | STILL_UNSOLVED   |

Decision rules:
- `n_lost > 0` and `n_gained == 0` → **REJECT**
- `n_gained > 0` and `n_lost == 0` → **ACCEPT**
- `n_gained > n_lost` → **ACCEPT**
- `n_gained <= n_lost` (both > 0) → **REJECT**

### 4.2 Layer 2: Continuous Tiebreaker

When `n_gained == 0` and `n_lost == 0` (all tasks retain their solvability
status):

```
continuous_delta = sum(cand_pass_rate - inc_pass_rate for all tasks)
ACCEPT if continuous_delta > 0, else REJECT
```

This ensures edits that improve pass rates without flipping solvability
boundaries are still accepted.


## 5. L1 Proposal Cycle

When L0 exploitation saturates at a node (N consecutive rejections), the system
escalates to the L1 Proposal Cycle (`css/proposal/proposal.py:run_l1_cycle`),
which searches for a better cognitive strategy. The cycle is a *candidate
generator*, not a gatekeeper — the tree's validation/test scoring is the final
judge.

### 5.1 Design Principles

- **Altitude separation**: L1 searches cognitive strategies (HOW to think); L0
  handles tactical rules (WHAT to do). A strategy polluted with tactical
  content is a failed strategy even if it passes.
- **Empty-rules testing**: Candidates are tested with empty `rules.md`,
  handicapping them deliberately. Any lift under handicap is a conservative
  signal that survives once L0 restores rules.
- **Objective selection**: All categorization is by pass@K comparison (no LLM);
  the LLM is used only for rich diagnosis to steer the search.

### 5.2 Cycle Flow

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                     L1 PROPOSAL CYCLE                                        │
│                                                                              │
│  Fixed test set selection (computed ONCE):                                   │
│    residual  : up to 24 tasks the baseline CANNOT solve (0/K)               │
│    regression: up to 12 tasks the baseline ALWAYS solves (K/K)              │
│    baseline_map: task_id -> baseline TaskRolloutGroup                        │
│                                                                              │
│  Step 1: GROUNDING (computed ONCE, cached):                                 │
│    1a: L0 ceiling analysis (why L0 optimization stalled)                    │
│    1b: Failure trajectory deep analysis (3-5 representative failures)       │
│    1c: L0 contrastive limitation review                                     │
│    1d: Synthesis → recommended directions (re-run per hypothesis with       │
│        growing ledger)                                                       │
│    [all 1a/1b/1c run in parallel]                                           │
│                                                                              │
│  LOOP (up to max_l1_iterations rounds, each a DISTINCT philosophy):         │
│                                                                              │
│    Step 2: STRATEGY GENERATION (one LLM call)                               │
│      Mode = NEW:    genuinely different cognitive mechanism                  │
│      Mode = REFINE: same core idea, better operationalization               │
│      Input: grounding + cycle ledger (all prior rounds' results)            │
│      Output: {philosophy, mechanism_difference, strategy_text}              │
│                                                                              │
│    Step 3: CANDIDATE TESTING                                                │
│      Rollout: (new strategy, EMPTY rules) x K on the fixed test set        │
│      Group results by task → candidate_groups                               │
│                                                                              │
│    OBJECTIVE CATEGORIZATION (no LLM):                                       │
│      cracked     = baseline fails, candidate passes  → +lift               │
│      still_failed = both fail                                               │
│      regressed   = baseline passes, candidate fails  → +regression         │
│      maintained  = both pass                                                │
│                                                                              │
│    Step 4: CONTRASTIVE DIAGNOSIS (two layers):                              │
│      Layer 1 (parallel, one task each):                                     │
│        cracked  → contrast (cand SUCCESS x baseline FAILURE) →             │
│                   active_ingredient                                         │
│        regressed → contrast (baseline SUCCESS x cand FAILURE) →            │
│                    handicap | harm classification                           │
│        still_failed → single failure analysis → residual nature            │
│      Layer 2 (aggregate synthesis):                                         │
│        → {active_ingredient, harm, residual, next_direction_hint,          │
│           next_action: propose_new | refine_current}                        │
│                                                                              │
│    KEEP-BEST: effective = lift > 0 AND net_lift >= 0                        │
│      Rank: max (net_lift, deploy_net, lift, -regression)                    │
│      deploy_net = lift - harm_regressions (post-exploitation lower bound)   │
│                                                                              │
│    MODE DECISION for next round:                                            │
│      effective (lift>0) → bank it, next = NEW (diverse exploration)         │
│      ineffective + was already a refine → abandon, next = NEW              │
│      ineffective + first attempt → diagnosis.next_action decides           │
│                                                                              │
│  EXIT: collected l1_target_effective (3) effective strategies → pick best   │
│  EXIT: exhausted max_l1_iterations (8) with no effective → archive          │
│                                                                              │
│  SUCCESS → new child node (best strategy, EMPTY rules) added to tree       │
│  FAILURE → last direction archived to negative archive                      │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 5.3 Cross-Round Ledger

The iteration context (`_IterationContext`) maintains a ledger of all prior
rounds' results, rendered as text for prompt injection via `render_ledger()`.
Each round records: the philosophy tried, its mechanism difference, objective
lift/regression counts, the diagnosis (active ingredient, harm, residual
nature), and cracked/regressed/still-failed task IDs. This prevents the search
from re-proposing spent directions and accumulates knowledge across rounds.

### 5.4 Handicap-vs-Harm Classification

Regressed tasks are classified by per-task analyzers into:
- **Handicap**: The candidate pursued a sound approach but tripped on a tactical
  detail the baseline's rules supplied. Expected and recoverable once L0 restores
  rules. Not the strategy's fault.
- **Harm**: The strategy's cognitive frame actively misled the agent. This is the
  strategy's fault and must be corrected.

The objective `deploy_net = lift - harm_regressions` is the post-exploitation
net lower bound and is used for tie-breaking among equal-`net_lift` candidates.


## 6. Tree Management

### 6.1 UCB1 Selection

Node selection (`css/tree/select.py:ucb1_score`) uses the formula:

```
ucb1(i) = val_score(i) + alpha * accept_slope(i, W) + beta * sqrt(ln(T) / n_i)
```

Where:
- `val_score(i)`: the node's validation score (task_hard metric)
- `accept_slope(i, W)`: linear-regression slope of accept indicators over the
  last W steps (positive = still learning, ~0 = plateaued)
- `T`: global total invested L0 steps across all nodes
- `n_i`: this node's L0 steps
- `alpha` (default 0.5): weight for the accept-slope term
- `beta` (default 0.5): weight for the exploration bonus

An unvisited node (`n_i = 0`) scores `+inf` to force initial exploration.
`select_batch` returns the top-K nodes by UCB1 for concurrent processing.

### 6.2 Branching Decision

The branching decision (`css/tree/branching.py:decide_branch`) is deterministic
and depends solely on L0 saturation:

- Not saturated → `"EXPLOITATION"` (continue L0 optimization)
- Saturated → `"PROPOSAL"` (launch L1 cycle)

The old REFINE operation has been unified into PROPOSAL. The branching decision
does not consult L1 signals or pattern-level statistical gates.

### 6.3 Pruning

Pruning (`css/tree/prune.py:should_prune`) requires ALL three conditions:

1. **Minimum investment**: `n_steps >= cfg.min_steps` (default 10)
2. **L0 saturated**: `node.is_saturated(cfg.N)`
3. **Sibling dominance**: paired-bootstrap CI lower bound > 0

The paired bootstrap (`paired_bootstrap_diff_ci`) resamples per-task pass/fail
indicators on the shared validation set. The same task indices are drawn for
both node and sibling in each resample, preserving pairing. The CI (default
95%) tests `mean(sibling) - mean(node)`. If the lower bound is strictly > 0,
the sibling is significantly better and the node is marked `status='pruned'`.

The bootstrap is seeded from `cfg.seed` for determinism, with default 1000
resamples.


## 7. Infrastructure

### 7.1 Ground Truth Firewall

The system maintains a strict separation between the frozen task agent and the
optimizer via narrow client wrappers (`css/model/client.py`):

- **`TargetOnlyClient`**: Wraps the inner LLM client, exposing only
  `complete_target` / `complete_target_messages`. Calling `complete_optimizer`
  raises `RuntimeError`. Used by all rollout/scoring code.

- **`OptimizerOnlyClient`**: Wraps the inner LLM client, exposing only
  `complete_optimizer` / `complete_optimizer_messages` / `complete_tool_call`.
  Calling `complete_target` raises `RuntimeError`. Critically, it is the single
  enforcement point for the ground-truth firewall: every optimizer call routes
  through this class, which injects a `GROUND_TRUTH_FIREWALL` instruction into
  every system prompt. This guarantees the optimizer may reason FROM ground
  truth present in trajectories but can never produce output that DEPENDS on
  ground-truth values.

### 7.2 Checkpoint / Resume System

Every expensive computation is checkpointed to disk using atomic JSON writes
(write to `.tmp`, then `os.replace` for crash safety). The checkpoint hierarchy:

```
<out_root>/
  <node_id>/
    round_<NNNN>/
      analysis/             # pattern mining artifacts
    l1_cycle/
      grounding/            # Step 1 cached analysis
      round_<NNNN>/
        step2/              # strategy proposal
        step3/              # candidate rollouts
        step4/              # diagnosis
        iteration_context.json  # cross-round ledger
    step<N>/                # L0 exploitation steps
      raw_patches.json      # Reflect output
      merged_edits.json     # Merger output
      edit_verifications.json  # Per-edit ablation results
      candidate_rules.md    # Collective apply output
      gate_decision.json    # Val gate result
      step_summary.json     # Compact step record
      rollout/              # On-policy rollout artifacts
      verify/edit_<i>/      # Per-edit verification rollouts
```

Each stage checks for its checkpoint file before executing. If present, the
stage loads from checkpoint and skips re-computation. This enables resume after
crashes at any granularity from individual L0 steps to L1 cycle rounds.

### 7.3 Step Buffer

The `StepBuffer` (`css/data/step_buffer.py`) is a per-node ordered history of
L0 optimization steps. Each `StepBufferEntry` records the step index, gate
action, scores, edit verifications, and failure patterns.

Key analytics methods:
- `consecutive_rejects()`: tail count of rejects (resets on any accept)
- `is_saturated(N)`: True when consecutive rejects >= N
- `accept_slope(W)`: least-squares slope of the accept indicator over W steps
- `recent_failure_patterns(W)`: de-duplicated failure pattern strings
- `recent_rejected_edits(W)`: rejected edits for prompt injection
- `reset_saturation()`: inserts a synthetic `epoch_reset` to break the
  consecutive-reject streak across epochs

### 7.4 Data Splits

Training data is split into three disjoint sets:

| Split | Default Size | Purpose |
|-------|-------------|---------|
| Train | 80 tasks    | On-policy rollout for L0 exploitation; L1 baseline + test set source |
| Val   | 40 tasks    | Selection-set scoring (val gate, UCB1 val_score, pruning) |
| Test  | 20 tasks    | Final evaluation of the best node (not used during search) |

Splits are shuffled deterministically from `cfg.seed`. Optional periodic
rotation (`held_out_rotation_T`) re-splits every T epochs (default 0 = never).

### 7.5 Scoring Metric

The primary metric is `task_hard` (`css/data/rollout.py:aggregate_scores`): the
fraction of *tasks* whose majority of K rollouts passed. This weights every
task equally regardless of per-rollout noise, providing a stable comparison
signal for the gate and UCB1 selection.


## 8. Configuration Parameters

All parameters are defined in `css/config.py:CSSConfig`:

### 8.1 Saturation and Branching

| Parameter           | Default | Description |
|---------------------|---------|-------------|
| `N`                 | 5       | Consecutive-reject saturation threshold |
| `W`                 | 10      | Accept-rate trend window (should be >= N) |
| `K`                 | 3       | REFINE→PROPOSAL escalation count (deprecated) |
| `remedy_threshold`  | 3       | Remedy resistance needed for L1 signal (deprecated) |

### 8.2 Tree Management

| Parameter                   | Default | Description |
|-----------------------------|---------|-------------|
| `min_steps`                 | 10      | Minimum L0 investment before pruning |
| `alpha`                     | 0.5     | UCB1 accept_slope weight |
| `beta`                      | 0.5     | UCB1 exploration bonus weight |
| `prune_bootstrap_resamples` | 1000    | Paired bootstrap resamples for pruning |
| `prune_ci`                  | 0.95    | Confidence level for sibling dominance |

### 8.3 Data Usage

| Parameter             | Default | Description |
|-----------------------|---------|-------------|
| `k_rollouts`          | 3       | Rollouts per task (K for pass@K) |
| `n_train`             | 80      | Training set size |
| `n_val`               | 40      | Validation set size |
| `n_test`              | 20      | Test set size |
| `held_out_rotation_T` | 0       | Re-split period (0 = never) |

### 8.4 L0 Exploitation

| Parameter               | Default | Description |
|--------------------------|---------|-------------|
| `minibatch_size`         | 8       | Trajectories per reflect minibatch |
| `max_l0_steps_per_epoch` | 20      | Safety cap on L0 steps per epoch |
| `batch_size`             | 40      | Tasks per train batch |
| `l0_edit_budget`         | 3       | Max edits per minibatch proposer (L) |
| `max_edits_per_step`     | 6       | Max merged edits per step |
| `exploitation_val_k`     | 1       | K for val gate (1 = fast; train K stays at k_rollouts) |
| `merger_inject_history`  | True    | Inject per-edit verification history into merger |
| `merger_history_window`  | 3       | Recent steps of edit verification history shown to merger |
| `rules_max_chars`        | 60,000  | Soft cap for rules.md size (warning only) |
| `reflect_mode`           | "plan_a"| Reflect architecture: "legacy", "plan_a", or "plan_b" |

### 8.5 L1 Strategy Cycle

| Parameter                   | Default | Description |
|-----------------------------|---------|-------------|
| `max_l1_iterations`         | 8       | Max diverse-iterate rounds (hard cap) |
| `l1_target_effective`       | 3       | Stop once this many effective strategies collected |
| `l1_diagnostic_tasks`       | 24      | Residual tasks for L1 testing (baseline 0/K) |
| `l1_regression_tasks`       | 12      | Regression guard tasks (baseline K/K) |
| `l1_diagnosis_per_category` | 5       | Max per-task analyses per category |

### 8.6 Context and Truncation

| Parameter          | Default  | Description |
|--------------------|----------|-------------|
| `context_cap`      | 256,000  | Global context window (tokens) |
| `context_use_frac` | 0.80     | Effective threshold = cap * frac |
| `tool_trunc`       | 8,000    | Truncate single tool result >= this (chars) |

### 8.7 Runtime

| Parameter          | Default             | Description |
|--------------------|---------------------|-------------|
| `seed`             | 42                  | Global random seed |
| `concurrency_limit`| 4                   | Parallel tree nodes per round |
| `max_api_workers`  | 32                  | Parallel rollout / verification workers |
| `task_timeout_s`   | 600                 | Per-rollout wall-clock timeout (seconds) |
| `bash_timeout_s`   | 180                 | Per bash command timeout (seconds) |
| `max_turns`        | 30                  | Multi-turn conversation limit per rollout |
| `target_model`     | "claude-sonnet-4-6" | Frozen task agent model |
| `optimizer_model`  | "claude-sonnet-4-6" | Optimizer / analyst model |


## 9. Data Model Summary

### 9.1 Edit Types

```
Edit (css/data/edit.py)
  ├── op: append | insert_after | replace | delete |
  │       add_section | rewrite_section | delete_section
  ├── content, target, reason
  └── source_tasks: list[str]     # task-level provenance

RawPatch
  ├── patch: Patch (list of Edits)
  ├── source_type: failure | success | contrastive
  └── batch_size: int

MergedEdit
  ├── section_target: "### Exact Heading"
  ├── delta_type: new_section | section_rewrite | section_refinement
  ├── content: complete section text (### heading + body)
  ├── target_tasks: list[str]     # for ablation verification
  ├── rationale                    # why this edit is needed
  └── derivation                   # audit trail from raw edits

EditVerification (css/data/step_buffer.py)
  ├── section_target, delta_type, content
  ├── passed: bool
  ├── task_results: {task_id: {status, inc_pr, cand_pr, ...}}
  └── rationale, derivation
```

### 9.2 Gate Decision

```
GateResult (css/evaluation/gate.py)
  ├── action: accept_new_best | accept | reject
  ├── current_rules, current_score
  └── best_rules, best_score, best_step
```

### 9.3 L1 Categorization

```
_categorize() output:
  ├── cracked: list[task_id]        # baseline fails, candidate passes → +lift
  ├── still_failed: list[task_id]   # both fail
  ├── regressed: list[task_id]      # baseline passes, candidate fails → +regression
  ├── maintained: list[task_id]     # both pass
  ├── lift, regression, net_lift: int
  └── n_residual, n_regression: int
```
