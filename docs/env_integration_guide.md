# Task Environment Integration Guide

How to onboard a new benchmark into CSS **without reading the mechanism**.
The mechanism (rollout, exploitation, gates, analysis, L1 proposal,
orchestrator) interacts with a task environment through one narrow, frozen
surface; implement that surface faithfully and everything else works
untouched.

Companion artifacts (keep the four in sync — the contract test enforces it):

| artifact | role |
|---|---|
| `css/envs/base.py` | the `TaskEnv` protocol (the formal surface) |
| `css/envs/common.py` | single-sourced helpers for every convention the protocol signature can't express |
| `css/envs/template/` | copy-me scaffold (runnable toy env, every `TODO(env)` marked) |
| `css/tests/test_env_template.py` | contract test — green = your env honors the full surface |
| `run_experiment_template.py` | launcher scaffold |

---

## 1. The complete mechanism→env surface

The mechanism calls EXACTLY six things — nothing else, ever:

| method | required? | callers | when |
|---|---|---|---|
| `train_items() -> list[dict]` | yes | orchestrator (round batching, analysis sampling), coldstart (bare train), tree/branching, L1 proposal | throughout |
| `val_items() -> list[dict]` | yes | orchestrator (val subset, baselines, round eval) → threaded into exploitation / paired gate | throughout |
| `test_items() -> list[dict]` | yes | coldstart (bare test baseline), orchestrator (per-round test eval) | per round |
| `run_one(item, skill_text, target_client, out_dir, *, rollout_index, epoch, node_id) -> TaskResult` | yes | `css/rollout/batch.py` — the ONLY execution entry point; everything that rolls tasks funnels through it | every (task, rollout) |
| `load_cached_result(item, out_dir, *, rollout_index, skill_hash) -> TaskResult \| None` | optional (duck-typed) | `css/rollout/batch.py` before each `run_one` | resume + prediction reuse |
| `action_space_description() -> str` | optional (duck-typed, default "") | orchestrator/coldstart → L1 paradigm-design prompts | cold start + each L1 cycle |

Construction goes through `css/envs/registry.py::build_env(cfg)` — the
mechanism and launchers never import a concrete env class.

## 2. Invariants (violating any breaks runs, usually silently)

1. **Stable unique ids.** Every item dict carries a unique `"id"` (fallback
   key `"task_id"`). Grouping, caches, difficulty ledgers, the paired-gate
   item ledger, and analysis labels all key off it. An empty id silently
   corrupts grouping.
2. **Deterministic splits.** Split accessors return the same items in the
   same order across calls AND process restarts. Batch shuffles are seeded
   over that order; resume and caches assume it.
3. **`run_one` never raises for task-level failure.** Return a failed
   `TaskResult` (`hard=0`, `fail_reason` set) with the trajectory intact.
   If you raise, the batch layer substitutes a counted-but-empty failure —
   denominators stay correct but the trajectory (analysis material) is lost.
4. **Exact-count discipline** is the batch layer's job, not yours — but it
   relies on (3).
5. **Canonical trajectory shape** (`css/trajectory.py`):
   `TaskResult.messages` is a flat `[{"role": str, "content": str}, ...]`
   transcript. If your agent runs on a richer transport (function calling,
   `role:"tool"`), FLATTEN it in your env (`"Action: <name>\n<args>"` +
   observation text). Reference implementation: `css/envs/bird/agent.py`
   (dual native/canonical message lists kept in lockstep).
6. **Eval annotation LAST.** After scoring, append exactly one
   `eval_annotation_message(outcome=..., ground_truth=..., detail=...)`
   (role `"evaluation"`) as the final message. This is how the optimizer's
   analysis learns the outcome and the gold reference. Do NOT use a
   `system`-role message for this (a legacy SpreadsheetBench shape —
   superseded).
7. **Ground-truth firewall** (two-sided):
   - During rollout: gold NEVER enters any agent-visible prompt or message.
   - After rollout: gold IS exposed to analysis via the eval annotation.
   - The invariant "optimizer outputs never depend on gold" is enforced
     centrally (the `GROUND_TRUTH_FIREWALL` clause is injected into every
     optimizer prompt at the client layer) — your env only guarantees the
     first bullet.
8. **Persistence + cache key.** `run_one` persists `result.json` (including
   the conversation) under `<out_dir>/predictions/<task_id>/r<i>/`, stamped
   with `skill_hash` (`css.envs.common.skill_hash` — MUST stay byte-identical
   to `css.rollout.batch._hash_skill`). `load_cached_result` reuses it only
   on a hash match. This powers crash-resume and the orchestrator's
   prediction-reuse optimizations (val-gate/round-eval reuse). Corollary the
   mechanism already honors: callers evaluating DIFFERENT candidates must
   pass different `out_dir`s (`css/rollout/selection_eval.py`).
9. **Config hygiene.** Env-private knobs live in `cfg.extra` under a
   `"<env>_*"` namespace (e.g. `bird_max_turns`). `CSSConfig` itself stays
   env-agnostic. Generic fields you may read: `data_root`, `split_dir`,
   `n_train/n_val/n_test` (slice convention: 0 = whole split), `seed`,
   `max_turns`, timeouts.

## 3. `TaskResult`: which fields are load-bearing

Produced via `TaskResult.from_dict(result_dict)` — the dict may use `id` /
`conversation` aliases; unknown keys are absorbed into `extras`.

| field | consumed by | notes |
|---|---|---|
| `hard` (0/1) | every score aggregate, gates, verification G/L, pass_rate | THE headline metric |
| `soft` [0,1] | aggregates, analysis displays | == hard for binary tasks |
| `passed` (property) | success/failure splits everywhere | `bool(hard)` |
| `messages` | all trajectory analysis (layer1, reflect, contrastive, proposal, coldstart) | canonical shape, eval annotation last |
| `task_id` | grouping, ledgers, dedup, labels | from item `id` |
| `fail_reason` | analysis prompts, failure mining | short diagnostic |
| `rollout_index`/`epoch`/`node_id` | provenance, dedup, longitudinal analysis | stamped by `common.persist_result` |
| `task_type`/`task_description` | analysis sampling + prompt labels | description = agent-visible task text, NEVER gold |
| `n_turns`/`token_usage` | logging/budget | |
| `extras` | nobody (audit only) | put predicted/gold/diagnostics here |

## 4. The agent's client surface

`run_one` receives a **target-only** client (First Law: the optimizer never
executes tasks; envs never see the optimizer client). Available calls:

- `complete_target(system, user) -> str` — single shot
- `complete_target_messages(messages) -> str` — multi-turn text ReAct
- `complete_target_tools(messages, tools) -> dict` — native function calling
  (returns `{"content", "tool_calls"}`; see `css/envs/bird/agent.py`)

## 5. Onboarding checklist

1. `cp -r css/envs/template css/envs/<env>`; rename `TemplateEnv`.
2. Data: build `split_dir/{train,val,test}/items.json` (pre-shuffled, unique
   `id` per item, everything `run_one` needs embedded or path-referenced via
   `data_root`). Implement `_load_split` if your layout differs.
3. Agent: rewrite `agent.py` around your task's real action space (keep the
   RETURN CONTRACT dict). Multi-turn/function-calling reference:
   `css/envs/bird/agent.py`; bash/code-execution reference:
   `css/envs/spreadsheetbench/`.
4. Scoring: implement your evaluator; call it AFTER the agent loop; feed the
   outcome + gold into the eval annotation.
5. `action_space_description()`: describe the REAL action set faithfully —
   L1 designs behavioral paradigms around exactly this text.
6. Register: one alias + one branch in `css/envs/registry.py`.
7. Contract test: copy `css/tests/test_env_template.py`, adapt the stub
   client + items to your env, make all 8 tests green.
8. Launcher: copy `run_experiment_template.py`; set split sizes, batch_size
   (steps/epoch = ceil(n_train/batch_size)), timeouts, `gate_screen_k`
   (val ≥ ~1000 → 1; small val → 3), env knobs under `extra["<env>_*"]`.
9. Smoke: tiny sliced run (`n_train=8, n_val=4, n_test=4, k_rollouts=1`)
   end-to-end before a real launch.

## 6. Known asymmetries in the two original envs

Bird and SpreadsheetBench predate `css/envs/common.py` and carry local copies
of its helpers (behaviorally identical; left untouched for run stability).
SpreadsheetBench additionally uses the legacy `system`-role
`[POST-EXECUTION VERIFICATION]` annotation and a split
`result.json`/`conversation.json` persistence. New envs should follow the
template (helper-based, `evaluation`-role annotation, single `result.json`).
