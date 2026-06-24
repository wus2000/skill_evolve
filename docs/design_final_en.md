# CSS: Cognitive Strategy Search — Searching for Optimal Skill Documents in Text Space for Frozen-Weight Agents

## Complete Mechanism Design Document (Final Version)

---

## 1. System Overview

### 1.1 Core Contribution

We propose **Cognitive Strategy Search (CSS)**: a two-level skill document optimization framework. The outer level searches over cognitive strategies (HOW to think) to discover qualitatively different thinking approaches. The inner level performs SkillOpt-style tactical rule optimization (WHAT to do) within each cognitive strategy. This breaks the scaling ceiling of existing systems (e.g., SkillOpt) that are trapped in local optimization under a single, fixed cognitive strategy.

### 1.2 LLM-Code Division Principle

A foundational principle governing all design decisions:

```
LLM handles (semantic synthesis — irreplaceable):
  - Observing and describing cognitive process features from trajectories
  - Identifying cross-trajectory cognitive patterns
  - Root cause attribution (why the agent thinks this way)
  - Strategy derivation (root cause → logical consequence for strategy change)

Code handles (constraint enforcement — LLM unreliable):
  - Statistical validation (pattern occurrence rates, trend detection, significance tests)
  - Physical isolation (L0/L1 file separation)
  - Output format gates (structural compliance checks)
  - Operation scheduling (EXPLOITATION/REFINE/PROPOSAL switching)
  - Fitness gates (best-score accept/reject)
  - Evaluation fairness (same-period comparison, held-out management)
```

Source: v5 survey "First Law" — strong wording ≠ load-bearing; only code architecture is truly load-bearing.

---

## 2. Skill Document Structure

### 2.1 Physically Separated File Organization

```
skill_document/            ← per tree node
  strategy.md   ← L1 cognitive strategy (HOW to think)
  rules.md      ← L0 tactical rules (WHAT to do)
  metadata.json  ← Learning curve + pattern library (per-node)

search_tree/               ← tree-global (not per-node)
  negative_archive.json  ← disproven strategy directions (meta-auxiliary, D12)
```

Note: the negative archive is **tree-global** — it records strategies pruned or
failed at any node and is read at any node's Layer 5, so it lives at the search
tree level, not inside a node's metadata.json.

### 2.2 strategy.md Format

```markdown
## Strategy Name
[Concise name, e.g., "Hypothesis-Driven Iterative Execution"]

## Strategy Body

### 1. [Cognitive Process Dimension]
[Level 2-3 granularity: concrete cognitive procedures]
[Describes METHOD not GOAL]
[Includes WHEN-to-switch decision logic]

### 2. [Cognitive Process Dimension]
...

### 3. [Cognitive Process Dimension]
...
```

Requirements:
- Organized as multiple `###` numbered subsections, each describing one cognitive dimension
- Level 2-3 granularity: concrete cognitive procedures (not platitudes, not tactical rules)
- Describes methods, not goals
- Includes decision logic: under what conditions to switch behaviors

### 2.3 rules.md Format

Free-form markdown text. No enforced numbering — the LLM freely determines the expression format (paragraphs, lists, conditionals) to maximize trajectory experience utilization.

### 2.4 Multi-Consumer Views

```
Task Agent sees:
  strategy.md (read-only reference) + rules.md (read-only reference)

L0 Optimizer (EXPLOITATION) sees:
  strategy.md (read-only context, immutable)
  rules.md (editable: add/modify/delete)
  Trajectory data + step_buffer

L1 Optimizer (REFINE) sees:
  strategy.md (can modify 1-2 ### subsections)
  rules.md (can modify/delete conflicting rules, cannot add)
  Layer 1-4 analysis outputs + metadata

L1 Optimizer (PROPOSAL) sees:
  strategy.md (full rewrite)
  rules.md (semantic judgment: keep compatible, drop incompatible)
  Layer 1-5 analysis outputs + metadata + negative archive
```

---

## 3. Search Topology: Tree-Based Branching

### 3.1 Tree Node Definition

```
TreeNode = {
  strategy:        L1 cognitive strategy text (strategy.md)
  rules:           Accumulated L0 tactical rules (rules.md)
  step_buffer:     L0 optimization history (accept/reject + failure patterns + rejected edits)
  val_score:       Latest validation set score
  pattern_records: Cognitive pattern library discovered under this strategy
  parent:          Parent node reference
  branch_type:     "PROPOSAL" | "REFINE" | "ROOT"
  refine_count:    Number of REFINE attempts from this node
  status:          "active" | "pruned" | "saturated"
}
```

### 3.2 Three Operations and Branching

```
EXPLOITATION (no branching): L0 optimization within current node
  → Default operation, runs every epoch

REFINE branch: Create child node (local strategy modification)
  Trigger: L0 saturated + persistent_fail exists + refine_count < K

PROPOSAL branch: Create child node (entirely new strategy)
  Trigger: L0 saturated + persistent_fail exists + REFINE exhausted (refine_count ≥ K)
```

### 3.3 Core Management Signal

A cognitive strategy's true quality is indirectly revealed through the L0 inner-loop learning curve:

```
Good strategy: L0 accept rate remains high (rules accumulate effectively)
Bad strategy:  L0 accept rate drops quickly (edits are consistently rejected)
```

This signal simultaneously drives: SELECT (which node to invest), BRANCH (when to branch), PRUNE (when to prune).

---

## 4. Detailed Design of Three Operations

### 4.1 EXPLOITATION (L0 Optimization)

Default operation each epoch. Adapted from SkillOpt's core mechanism for the two-level architecture.

**Data Consumption Principle**:
- Complete trajectories fed directly to L0 optimizer, no lossy truncation
- Only truncation: individual tool call results ≥ 8K tokens → truncate (preserve head/tail summary)
- Global context threshold = configurable parameter (default 256K) × 80%
- No Epoch-Level Review (Layer 3 + L1 already cover cross-epoch perspective)

**L0 Step Flow Within an Epoch**:

```
Epoch start: rollout all training tasks → collect trajectories
Split trajectories into minibatches (M items/batch, failures/successes separately)

Each L0 Step:
  (1) Per-minibatch LLM analysis → raw patches (edit proposals)
  (2) Hierarchical merge → merged edit set
  (3) Selection/ranking → top edits
  (4) Apply edits to rules.md → candidate rules
      Edit operations (adapted from SkillOpt skill.py:48-108):
        append / insert_after / replace / delete
  (5) Selection set rollout → candidate_score
  (6) Accept/reject gate (adapted from SkillOpt gate.py:31-73):
      candidate_score > current_score → accept
      candidate_score > best_score → accept_new_best
      else → reject
  (7) Update step_buffer:
      {step, action, score_before, score_after, failure_patterns, rejected_edits}

Run until saturated (N consecutive rejects) or epoch trajectories fully utilized.
Even if saturation triggers mid-epoch, complete the epoch (ensure Layer 1-3 gets full data).
```

**Saturation Detection** (step-level granularity):
```
N consecutive rejected steps (N initial value 5) → L0 saturated
Saturated + Layer 3 reports persistent_fail → trigger REFINE or PROPOSAL
```

### 4.2 REFINE (Local Strategy Modification) ⚠️ Requires Experimental Observation

REFINE serves as the middle layer: larger than EXPLOITATION (modifies strategy components), smaller than PROPOSAL (doesn't replace the entire strategy).

> **Experimental observation flag**: REFINE's modification scope control, boundary with PROPOSAL, and escalation threshold K are the most uncertain mechanism points — must be empirically validated.

**Pipeline Sharing with PROPOSAL**:
```
Layer 1-4: Fully shared (continuous analysis pipeline)
Layer 5: Fork — two response amplitudes from the same diagnosis
```

**REFINE Layer 5**:
```
Input: Layer 4 attribution → identified problematic strategy components (1-2 ### subsections)
Operations:
  strategy.md: rewrite only identified subsections, keep rest unchanged
  rules.md: clean up conflicting rules (modify/delete OK, no additions)
Validation: 5b retrospective + negative archive check + 5c rollout

Code gate (diff check):
  (a) Must modify at least 1 ### subsection (reject empty operations)
  (b) Unmodified subsections must match parent exactly (else escalate to PROPOSAL)
```

**Knowledge Inheritance**:
```
Strategy: Parent's locally modified version
L0 rules: Fully inherited + REFINE cleans conflicting parts
Pattern library: Inherited (tracking sequence continues)
Learning curve: Restart from 0
```

**Termination**:
```
REFINE attempted ≥ K times (K=3) from same parent, none significantly better
→ "REFINE-exhausted" → escalate to PROPOSAL
```

### 4.3 PROPOSAL (New Strategy Derivation) — Five-Layer Trajectory-Driven Progressive Analysis

PROPOSAL is not "LLM creative generation" but "layered data analysis where L1 signals emerge naturally and strategy changes are logical consequences of the analysis."

#### Layer 1: Open-Ended Cognitive Process Analysis (Per-Trajectory)

**Key Design**: No predefined cognitive analysis dimensions. Structured output format, but analysis content is emergent.

```
LLM guidance:
  Focus on "how the agent thinks" not "what it does"
  Name observed cognitive aspects in LLM's own language
  Each observation must cite trajectory evidence
  Distinguish critical (significant impact) vs notable (noteworthy but uncertain)

Output format:
  observations: [
    { what, cognitive_aspect (LLM-named), evidence, consequence, significance }
    ... (variable count)
  ]

Contrastive pairs (same-task success/failure pairs) analyzed separately:
  Focus on "cognitive process differences that led to different outcomes"
  Output: divergence_point + cognitive_difference + is_systematic
```

#### Layer 2: Cross-Trajectory Cognitive Pattern Clustering

```
Step 2a (Code): Embedding pre-grouping
  Embed each observation's text (Qwen3-Embedding-0.6B)
  DBSCAN clustering (adaptive eps, min_samples=2)
  Unit = observation, not trajectory

Step 2b (LLM): Per-cluster refinement
  Unify naming / split different patterns / merge similar ones

Step 2c (LLM): Cross-cluster operations
  Failure vs success pattern pairing → "counterpart patterns"
  Success patterns directly suggest "what to do" → key input for Layer 5

Incremental update:
  New epoch observations matched to existing patterns via embedding retrieval
  Unmatched → create new / merge old patterns
  Pattern library grows incrementally, not rebuilt from scratch
```

#### Layer 3: Longitudinal Tracking + L0/L1 Signal Separation

**Core Innovation**: Using L0 optimization history to objectively separate L1 signals.

```
Track each cognitive pattern's occurrence rate across epochs:
  Pattern decays after L0 optimization → L0 problem (rules can fix it)
  Pattern persists after multiple L0 rounds → L1 signal (need to change thinking approach)

L1 signal criteria (Code-computed):
  (a) No significant declining trend in occurrence rate over W recent steps
  (b) L0 saturated (N consecutive rejects = remedy_resistance evidence)
  (c) Still affects a significant proportion of tasks

Cross-epoch pattern matching:
  Each pattern has a stable pattern_id
  New epoch observations first matched via embedding retrieval + LLM judgment
  Periodic global review: independently created patterns that are actually identical → merge
```

#### Layer 4: Root Cause Attribution

```
Four-level progressive inquiry (LLM, each level requires evidence citation via Code gate):

  Level 1 (Behavioral): What exactly did the agent do in L1 patterns? Pure factual summary
  Level 2 (Process): Why did it do that? Find reasons from THOUGHT text
  Level 3 (Strategy): What strategy design caused this process? Point to specific strategy paragraphs
  Level 4 (Assumption): What hidden assumption underlies the strategy? What is unlocked by breaking it?

Cross-pattern attribution: Multiple L1 patterns → same strategy-level root cause → high-leverage point
L0 failure explanation: Cite remedy_history to explain why L0 rules couldn't fix this
```

#### Layer 5: Strategy Derivation + Retrospective Validation

```
5a Strategy derivation (LLM):
  Keyword "derive" not "generate" — strategy change is a logical consequence of root cause
  Root cause → strategy components to change → direction → concrete mechanism
  Paired with counterpart success patterns: correct behaviors the agent exhibited incidentally
    = what the new strategy should systematize
  Compared with negative archive: embedding recall top-K → LLM explains differences

5b Retrospective validation (cheap pre-rollout check):
  Check 1: Positive evidence in success trajectories
  Check 2: Counterfactual reasoning on failure trajectories
  Check 3: Coverage estimate (what % of persistent_fail can this solve)
  Coverage < 20% → reconsider root cause / Coverage > 30% → proceed to rollout

5c Quick rollout validation (Code-driven):
  Targeted verification on persistent_fail task subset
  Core metric: does target L1 pattern occurrence rate significantly decrease
  Pass → create new tree node
  Fail → enter negative archive, try next root cause
```

**PROPOSAL Knowledge Inheritance**:
```
Strategy: Entirely new
L0 rules: L1 optimizer semantic judgment — keep compatible, drop incompatible
Pattern library: Not inherited
Learning curve: Start from 0
```

---

## 5. Tree Management

### 5.1 SELECT: UCB1 Variant

```
SELECT(node) = val_score + α × accept_slope + β × sqrt(ln(T) / n_i)

val_score     = Latest validation set score (normalized to [0,1])
accept_slope  = Linear trend of accept rate over recent W steps
T             = Global total invested steps
n_i           = Steps invested in this node
α, β          = Hyperparameters
```

In concurrent mode, SELECT_BATCH selects top-K nodes for parallel investment.

### 5.2 PRUNE: Paired Bootstrap

```
Conditions (all must be met):
  (a) Invested ≥ min_steps (initial value 10)
  (b) L0 saturated
  (c) val_score significantly lower than best sibling under same parent

Significance test:
  Paired bootstrap on validation set, 1000 resamples
  Score difference 95% CI lower bound > 0 → prune

After pruning:
  Status → "pruned", no further investment
  Trajectories and pattern library retained (for Layer 5b retrospective retrieval)
```

### 5.3 Negative Archive

Meta-auxiliary information for the strategy search tree. Prevents blind repetition of disproven directions.

```
Entry = {
  strategy_snapshot, origin, root_cause, failure_evidence, created_at
}

Write: on PRUNE / PROPOSAL failure / REFINE failure
Read: at PROPOSAL/REFINE Layer 5 → embedding recall top-K → LLM judgment
Principle: reminder not prohibition — if differences can be articulated, proceed; if not, block
```

---

## 6. Cold Start

```
Phase 0a: Bare rollout
  No strategy/rules, 80 tasks × K=3 → baseline_score + trajectories_0

Phase 0b: Initial Layer 1-3 analysis → initial pattern library

Phase 0c: First PROPOSAL (Layer 4-5)
  Attribute to systemic cognitive weaknesses of bare LLM → strategy_0.md
  rules_0.md = empty

Phase 0d: Create root node
```

---

## 7. Complete System Flow (Concurrent Round-Based)

```
while not terminated:

  ── SCHEDULE ──
  SELECT_BATCH: Choose top-K active nodes for parallel investment
  K = min(active_node_count, concurrency_limit)

  ── PARALLEL EXECUTE (K nodes independently) ──
  Each node runs one full epoch in parallel:

    ① Epoch Rollout: 80 tasks × K=3 → epoch_trajectories
    
    ② L0 EXPLOITATION:
       Minibatch analysis → hierarchical merge → edit → evaluate → accept/reject
       Multiple steps, until saturated or epoch complete
       (complete epoch even if mid-epoch saturation)
    
    ③ Layer 1-3 analysis (full epoch data)
    
    ④ Validation set evaluation (periodic)

  ── SYNC POINT ──
  
    ⑤ Global update: global_best + PRUNE check
    
    ⑥ Saturated node branching (parallelizable):
       REFINE or PROPOSAL → new node / negative archive
    
    ⑦ Return to SCHEDULE

Termination: All active nodes saturated with no new PROPOSAL available / manual stop
No max_total_steps budget — run fully to observe mechanism behavior.
```

---

## 8. Technical Stack

### 8.1 Embedding

```
Model: Qwen3-Embedding-0.6B
  0.6B params | 1024 dims | MRL support 32~1024
  C-MTEB clustering 68.74 (far exceeds BGE-M3 ~55)
  Strong Chinese-English bilingual | CPU-runnable | sentence-transformers compatible

Upgrade path: Qwen3-Embedding-4B (C-MTEB clustering 77.89)
```

### 8.2 Vector Index

```
faiss-cpu (IndexFlatIP)
  ~300-5000 vectors → exact search sufficient
  L2 normalize + inner product = cosine similarity
```

### 8.3 Clustering

```
DBSCAN / HDBSCAN
  eps: adaptive (k-distance plot elbow)
  min_samples = 2 (capture low-frequency patterns)
```

---

## 9. Data Usage

```
Training set (80 tasks): Main loop — trajectory collection, L0 optimization, pattern analysis
Validation set (40 tasks): Tree node evaluation — fair comparison of strategies
Test set (20 tasks): Final reporting only, never used in optimization

K=3 rollouts per task:
  (a) Reduce LLM randomness (majority vote)
  (b) Generate natural contrastive pairs
  (c) Provide pattern occurrence rate confidence

Fair node comparison: Same-period comparison (validation performance at equal step count)
Held-out rotation: Re-randomize train/val split every T epochs to prevent overfitting
```

---

## 10. Global Parameter Table

| Parameter | Meaning | Initial Value | Rationale |
|-----------|---------|---------------|-----------|
| N | Consecutive reject saturation threshold | 5 | SkillOpt-class convergence speed |
| W | Accept rate trend window | 2N=10 | Cover twice the saturation length |
| K | REFINE→PROPOSAL escalation | 3 | ≈ number of strategy subsections |
| min_steps | PRUNE minimum investment | 10 | ≈ 2 epochs of steps |
| α | SELECT trend weight | TBD | Pilot grid search |
| β | SELECT exploration weight | TBD | Pilot grid search |
| context_cap | Global context limit | 256K × 80% | LLM context window |
| tool_trunc | Tool result truncation | 8K | Only truncate tool call returns |
| eps_dbscan | DBSCAN distance threshold | Adaptive | k-distance plot elbow |
| min_samples | DBSCAN minimum cluster size | 2 | Capture low-frequency patterns |

All initial values based on reasonable inference; final values calibrated via pilot experiments.

---

## 11. Ablation & Baselines (To Be Expanded)

```
Core Ablations:
  A1 Remove L1 layer    A2 Remove tree structure    A3 Simplify PROPOSAL
  A4 Remove signal sep. A5 Remove neg. archive      A6 Predefined Layer 1

Baselines:
  B1 SkillOpt vanilla   B2 SkillOpt extended    B3 Bare LLM    B4 Manual skill

Priority: Must-do A1,A3,B1,B2,B3 | Should-do A2,A4,A6 | Nice-to-have A5,B4
```

---

## Appendix A: Operation Permission Matrix

| | strategy.md | rules.md |
|---|---|---|
| EXPLOITATION | Read-only context | Add/Modify/Delete (free edit) |
| REFINE | Modify 1-2 ### subsections | Modify/Delete conflicts (no additions) |
| PROPOSAL | Full rewrite | Semantic judgment: keep/drop |

## Appendix B: Design Document Evolution

| Version | Content | Role |
|---------|---------|------|
| v2 | Original 8-codebase analysis | Survey |
| v3 | Unified design (identified as "missing Level 1") | Concept |
| v4 | Two-level two-generator framework | Conceptual framework |
| v5 | 8-codebase survey conclusions + guidance + trajectory + First Law | Survey & initial design |
| v6 | Negotiated complete design (D0-D16) | Mechanism design |
| This doc | Implementation-ready final integration | Implementation reference |
