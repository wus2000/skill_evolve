# ScienceWorld — Integration Onboarding

Companion to `docs/env_prep/scienceworld_PREP.md` (the empirical audit). This doc
is the **actionable onboarding design**: protocol decision, split design, the
`TaskEnv` mapping spec, the concurrency/deployment architecture, real smoke
numbers, a proposed default-config list (every value marked **TO NEGOTIATE**),
a risk register, and open questions. All numbers below were measured on this Mac
(Java 21, Python 3.10, `scienceworld==1.2.3`, the live qwen endpoint) unless
marked "(from docs)". **Integration status (2026-07-04):** the design below was approved (with the
unified-simplification amendment) and IMPLEMENTED — `css/envs/scienceworld/`
(registered), `run_experiment_scienceworld_server.py`, the seed-deterministic
split generator `tools/make_scienceworld_split.py`, and tests
`css/tests/test_scienceworld_*.py`. The AgentBoard subset turned out to cover
**15** SW task types (not the ~20 estimated); all 90 instances were recovered to
exact (task, variation). Generated split: train 198 / val 72 / primary(AgentBoard)
90 / secondary 116. Live-validated on this Mac (oracle replay + real qwen rollouts
+ 8-way pool concurrency).

Artifacts produced alongside this doc (all under gitignored `env_candidates/`):
- `env_candidates/scienceworld_smoke/` — the smoke harness scripts + `results/*.json`
- `env_candidates/scienceworld_deploy/` — wheels + data + `SERVER_DEPLOY.md`
- `env_candidates/agentboard_scienceworld/` — AgentBoard code + the 90-instance subset

---

## 0. TL;DR recommendation

| Decision | Recommendation | Why (short) |
|---|---|---|
| Headline test protocol | **AgentBoard subset SR + PR (primary)** + original-protocol native score (secondary) | SR is the metric WorldEvolver / Evo-Memory / SkillGen report; unsaturated for open ~35B |
| Train signal | **Native ScienceWorld score (0–100)** on official train variations | subgoal annotations exist ONLY for the 90 test instances; native score covers all variations |
| Task universe | The **~20 task types present in the AgentBoard subset** (align train with the headline metric); report a 30-task original sample as a generalization check | maximizes comparability; keeps the rules doc's job coherent |
| Step budget | **30** for the AgentBoard test protocol (fidelity); **50** for train/val + original test (TO NEGOTIATE) | AgentBoard hardcodes 30; 50 matches our ALFWorld/AppWorld and covers most native gold paths |
| Simplifications | **`selfWateringFlowerPots,openContainers,openDoors,noElectricalAction`** (AgentBoard's set) | exact protocol fidelity on the primary test; removes brittle failure modes without teleport |
| Execution substrate | **In-process env pool** (N `ScienceWorldEnv`, each its own JVM), NOT a subprocess worker | the engine is ALREADY out-of-process (JVM); py3.8-importable; verified isolated at N=8 |
| GT firewall source | **live `generateGoldPath=True`** (primary) or `goldpaths-all.zip` (offline, 29/30 tasks) | live gen covers the one task the zip omits; gold stays optimizer-only |
| Valid-actions exposure | **generic action templates in the prompt + on-demand `check valid actions`** (mirror AgentBoard); NOT the full 400+ list every turn | comparability + realistic; the skill teaches strategy, not action enumeration |

---

## 1. Protocol decision analysis (work item 2)

Two established ScienceWorld protocols, plus a streaming variant. The choice sets
what our headline number is comparable to.

### (A) AgentBoard subset — Success Rate + Progress Rate
- **What it is:** a curated **90-instance** subset with **re-annotated subgoals**;
  metrics = **SR** (all subgoals achieved within budget) and **PR** (fraction of
  subgoals achieved). Step budget **30**. Reported split by difficulty (easy/hard).
- **Comparable to:** WorldEvolver (Gemma-4-26B, ~44→52 SR band), Evo-Memory/ReMem,
  SkillGen — i.e. the **exact papers this harness competes with**. SR is
  **unsaturated** for open ~35B models (room to show skill-doc lift).
- **Cost:** 90 instances × K rollouts × ≤30 steps. Cheap (§5 numbers).

### (B) Original protocol — native score (0–100)
- **What it is:** all 30 tasks, evaluate on held-out **test** variations, metric =
  **mean final score 0–100**; `envStepLimit≈100`, `easy` simplifications.
- **Comparable to:** SwiftSage (84.7), Reflexion (45.3), ReAct (36.4), OpenSkill.
  But frontier+skills already sit ~90 → **saturated / less room** to demonstrate lift.
- **Cost:** a stratified test SAMPLE (not all 1,819 test variations) keeps it cheap.

### (C) Streaming / continual (Evo-Memory style)
- Order test variations into a stream; accumulate skills across episodes; report a
  trajectory. Best exercises the CSS loop but hardest to compare head-to-head.
  **Defer** to a follow-up; not the first headline.

### Recommendation — dual-track, AgentBoard-primary
- **TRAIN** on official **train** variations (native score reward — the only signal
  available for non-test variations), with **our own val carve** for the gate.
- **REPORT** two test splits via the `eval_splits()` hook:
  1. **PRIMARY = AgentBoard 90-instance subset** → SR (mechanism `test_score`) + PR
     (`extra_metrics`). This is the headline, comparable to WorldEvolver et al.
  2. **SECONDARY = a stratified original-protocol test sample** → native 0–100 score
     (`extra_metrics`), comparable to the SwiftSage lineage and a generalization check.
- **Metric-mismatch caveat (flag):** the train reward (native score) is not identical
  to the primary test metric (subgoal SR); both measure task progress and are strongly
  correlated (score==100 ≈ all subgoals), but they are not the same function. This is
  inherent — AgentBoard subgoals exist only for the 90 test instances. Acceptable and
  worth a sentence in the paper's protocol section.

---

## 2. Split design (work item 3)

All counts below are **authoritative** — read live from the JAR
(`get_variations_{train,dev,test}()`) and cross-checked against the gold-path
archive. The JAR split is a deterministic **50/25/25** per task type; dev/test
variations use substances/objects unseen in train.

### 2a. Per-task-type folds (JAR authority; grand total 7,207)

| # | task | maxvar | train | dev | test | gold med / p90 / max steps |
|--:|---|--:|--:|--:|--:|---|
| 1-1 | boil | 30 | 14 | 7 | 9 | 82 / 165 / 177 |
| 1-2 | melt | 30 | 14 | 7 | 9 | 46 / 121 / 145 |
| 1-3 | freeze | 30 | 14 | 7 | 9 | 56 / 127 / 138 |
| 1-4 | change-the-state-of-matter-of | 30 | 14 | 7 | 9 | 38 / 121 / 127 |
| 2-1 | use-thermometer | 540 | 270 | 135 | 135 | 21 / 25 / 25 |
| 2-2 | measure-melting-point-known-substance | 436 | 218 | 109 | 109 | 32 / 73 / 75 |
| 2-3 | measure-melting-point-unknown-substance | 300 | 150 | 75 | 75 | (no offline gold — live only) |
| 3-1 | power-component | 20 | 10 | 5 | 5 | 14 / 16 / 16 |
| 3-2 | power-component-renewable-vs-nonrenewable | 20 | 10 | 5 | 5 | 27 / 31 / 31 |
| 3-3 | test-conductivity | 900 | 450 | 225 | 225 | 32 / 46 / 50 |
| 3-4 | test-conductivity-of-unknown-substances | 600 | 300 | 150 | 150 | 24 / 32 / 34 |
| 4-1 | find-living-thing | 300 | 150 | 75 | 75 | 13 / 17 / 17 |
| 4-2 | find-non-living-thing | 300 | 150 | 75 | 75 | 8 / 10 / 12 |
| 4-3 | find-plant | 300 | 150 | 75 | 75 | 13 / 15 / 17 |
| 4-4 | find-animal | 300 | 150 | 75 | 75 | 13 / 17 / 17 |
| 5-1 | grow-plant | 126 | 62 | 31 | 33 | 67 / 76 / 78 |
| 5-2 | grow-fruit | 126 | 62 | 31 | 33 | 82 / 90 / 96 |
| 6-1 | chemistry-mix | 32 | 16 | 8 | 8 | 23 / 57 / 61 |
| 6-2 | chemistry-mix-paint-secondary-color | 36 | 18 | 9 | 9 | 16 / 30 / 32 |
| 6-3 | chemistry-mix-paint-tertiary-color | 36 | 18 | 9 | 9 | 23 / 40 / 42 |
| 7-1 | lifespan-longest-lived | 125 | 62 | 31 | 32 | 7 / 9 / 9 |
| 7-2 | lifespan-shortest-lived | 125 | 62 | 31 | 32 | 7 / 9 / 9 |
| 7-3 | lifespan-longest-then-shortest-lived | 125 | 62 | 31 | 32 | 8 / 10 / 10 |
| 8-1 | identify-life-stages-1 | 14 | 6 | 3 | 5 | 41 / 43 / 43 |
| 8-2 | identify-life-stages-2 | 10 | 4 | 2 | 4 | 17 / 18 / 18 |
| 9-1 | inclined-plane-determine-angle | 168 | 84 | 42 | 42 | 85 / 163 / 194 |
| 9-2 | inclined-plane-friction-named-surfaces | 1386 | 692 | 346 | 348 | 73 / 339 / 798 |
| 9-3 | inclined-plane-friction-unnamed-surfaces | 162 | 80 | 40 | 42 | 122 / 205 / 232 |
| 10-1 | mendelian-genetics-known-plant | 120 | 60 | 30 | 30 | 131 / 135 / 151 |
| 10-2 | mendelian-genetics-unknown-plant | 480 | 240 | 120 | 120 | 132 / 135 / 149 |
| | **TOTAL** | **7207** | **3592** | **1796** | **1819** | overall med 32 / p90 131 / max 798 |

### 2b. Tiny-task leakage — EMPIRICALLY A NON-ISSUE
The PREP warned of `maxvar==1`/`==2` train/test leakage. **Verified false for all 30
tasks**: the smallest is `identify-life-stages-2` (maxvar=10 → 4/2/4) and **every
task has zero train∩test overlap** (measured). No task needs exclusion or special
handling. Keep a one-line defensive assertion (`assert not set(train)&set(test)`),
but there is nothing to guard against in practice. → **PREP correction (§10).**

### 2c. Variation imbalance (10 .. 1,386) — stratified caps
The train pool is sampled by the harness in batches; a raw union would be dominated
by `inclined-plane-friction-named` (692 train) and `test-conductivity` (450). Propose
**per-task-type stratified caps** so no task type dominates the rules doc's evidence.

### 2d. Concrete pool proposal (AgentBoard-aligned universe)
Primary test = the 90 AgentBoard instances, which cover ~20 of 30 task types and
**exclude** the long-horizon / electrical families (mendelian, inclined-plane,
grow-*, power-component, test-conductivity). To align training with the headline
metric, build train/val over the **AgentBoard task set** (exact membership pinned
from the canonical subset — §6), and report the original-protocol secondary on the
SAME task set for an apples-to-apples native-score check.

| pool | source fold | sizing rule | approx size | note |
|---|---|---|---|---|
| **train** | official **train** variations, AB task set | cap ~15/task, stratified | **~250–300** | the L0/L1 optimization pool |
| **val** | official **train** (held out from train pool) or **dev** | cap ~4–5/task, stratified | **~90** | the paired-gate carve |
| **test primary** | AgentBoard subset (fixed) | fixed | **90** | SR + PR, budget 30 |
| **test secondary** | official **test** variations, AB task set | cap ~6–8/task, stratified | **~150** | native 0–100, generalization check |

(If we instead want a *full-benchmark* secondary, add a 30-task original sample,
~5/task ≈ 150, budget 100. TO NEGOTIATE.)

Split files are pre-shuffled `items.json` per split with a **stable unique `id`**
per item — e.g. `"sw::<task_name>::v<variation>::<protocol>"` — so grouping, caches,
and the paired-gate ledger key cleanly. Items carry `task_name`, `variation`,
`protocol` (`"original"` | `"agentboard"`), and (AgentBoard items) `goal`,
`subgoals`, `difficulty`.

---

## 3. TaskEnv interface mapping spec (work item 4)

Mapping ScienceWorld onto the six-method `TaskEnv` surface
(`docs/env_integration_guide.md`). No mechanism code changes.

### 3a. Item identity & splits
- `train_items()/val_items()/test_items()` read pre-built `items.json` per split
  (deterministic order), sliced by `n_train/n_val/n_test` via `common.slice_split`.
- **Interface gotcha (verified):** `get_variations_{train,dev,test}()` operate on the
  **currently-loaded task** and throw `size=0 ... must be positive` if no task is
  loaded. The split-builder tool must `load(task, 0, ...)` once per task before
  querying its folds. (This bit the first smoke rollout; documented so the split
  tool and any live-variation code call `load()` first.)
- `eval_splits()` → `[("agentboard", <90>), ("original_test", <~150>)]`; first is
  primary (its `task_hard` = mechanism `test_score` = SR). Reporting-only.
- `extra_metrics(results)` → for the AB split `{ "PR": mean soft, "SR_easy":…,
  "SR_hard":…, "grounding":… }`; for the original split `{ "avg_score": mean soft×100 }`.
  Dispatch on the results' `protocol`/`task_type` tag.

### 3b. Rollout loop (`run_one`)
```
item -> (task_name, variation, protocol, [goal, subgoals, difficulty])
env = pool.acquire()                      # an in-process ScienceWorldEnv (its own JVM)
budget, simpl = protocol_settings(protocol)   # agentboard: 30 / AB-simpl ; original: 50 / easy
env.load(task_name, variation, simpl, generateGoldPath=False)   # agent NEVER sees gold
obs, info = env.reset()
goal = item["goal"] if protocol=="agentboard" else env.get_task_description()
for step in range(budget):
    action = agent_turn(target_client, goal, action_templates, history, obs, inv)
    obs, reward, done, info = env.step(action)          # native score in info["score"]
    if protocol=="agentboard": update_subgoal_matcher(obs)   # regex latch (see §6)
    if terminated(protocol, info, subgoal_matcher): break
score = subgoal_SR_PR(...) if protocol=="agentboard" else info["score"]
pool.release(env)                          # or recycle every M episodes
append eval_annotation_message(outcome, ground_truth=gold_ref, detail=...)   # LAST msg
```
- **Flatten** the transcript to canonical `[{role, content}]` (ReAct text: the agent
  is single-turn text, so this is direct — `"Action: <cmd>"` + observation text).
- **`run_one` never raises** for a task-level failure: a py4j error (crashed JVM) →
  return `hard=0, fail_reason="jvm_error"` with the partial trajectory; recycle the env.

### 3c. Scoring predicates (pin these)
- **original protocol:** `soft = clamp(info["score"],0,100)/100`; `hard = 1 if
  info["score"] >= 100 else 0` (score is an int 0–100 = completion fraction; 100 = solved).
  Key `hard` off **score**, NOT `isCompleted` — `isCompleted` is also True on step-limit
  and on negative-score auto-termination (a FAILURE), so it is not a success signal.
- **agentboard protocol:** `subgoals` = list of regex patterns; after each step, for
  each pattern `re.search(pattern, observation)` latches that subgoal to done
  (monotonic). `PR = matched/len(subgoals)` (final == max, by latching);
  `soft = PR`; `SR = hard = 1 if all subgoals matched within 30 steps else 0`.
  Matching is on the **step observation string**, NOT the native goal-progress string.

### 3d. GT firewall
- Source: **live `env.load(..., generateGoldPath=True)` → `get_gold_action_sequence()`**
  (optimizer/annotation only), or the offline `goldpaths-all.zip` (29/30 tasks;
  omits `measure-melting-point-unknown-substance` → that task needs live gen).
- During rollout, `generateGoldPath=False` and gold NEVER enters an agent-visible
  message. After scoring, the eval annotation (role `"evaluation"`, LAST message)
  carries the gold action sequence + the native goal-progress checklist as the
  optimizer's reference. Central `GROUND_TRUTH_FIREWALL` injection keeps optimizer
  OUTPUTS from depending on it (harness-wide invariant).

### 3e. `action_space_description()` (L1 paradigm context)
Return a faithful description of the ReAct loop + the ~15 generic action templates
(open/close/pick up/put down/move/pour/dunk/mix; look around/look at/look in/read;
activate/deactivate/use; go to/teleport to; focus on; wait; task; inventory;
`check valid actions`), the `{OBJ}`/`{LOC}` placeholder convention, one-action-per-turn,
and the step budget. (Draft text: `env_candidates/scienceworld_smoke/action_space_description.txt`.)

### 3f. Env knobs (`cfg.extra["scienceworld_*"]`) — see §7.

---

## 4. Concurrency & deployment architecture (work item 5)

### 4a. The substrate — a NEW menu entry: in-process RPC-handle pool
`css/envs/common/__init__.py` lists substrates (a) in-thread, (b) per-episode
worker, (c) persistent worker pool, (d) shared server. **ScienceWorld fits none
cleanly** — it is **(e) an external process-per-slot engine driven by a thin
in-process RPC handle.** Each `ScienceWorldEnv.__init__` `launch_gateway()`s a
**dedicated JVM subprocess** and holds a per-instance py4j client + a daemon
callback-server thread. The heavy engine is ALREADY isolated in the JVM; the Python
handle is thin and thread-independent.

Consequence: the two reasons the `subprocess_worker` layer exists — *wrong
interpreter* (AppWorld py3.11) and *not-thread-safe in-process engine* (ALFWorld) —
**do not apply.** `scienceworld` imports fine under the harness py3.8, and isolation
is provided by the JVM boundary. So the recommended architecture is an **in-process
env pool**, not a subprocess worker.

### 4b. Verified at N=8 (concurrency_probe.json)
8 `ScienceWorldEnv` created from 8 threads in ONE process:
- **8 distinct JVM PIDs**; each thread replayed ITS task's gold path to **score 100
  independently** under concurrent stepping (a `threading.Barrier` forced max
  contention) → **state isolation holds, no py4j cross-talk**.
- **mean 265 MB RSS / JVM**, aggregate **2,122 MB** for 8. Concurrent spawn ~1.3–1.5 s each.

### 4c. Sizing for 128 concurrent
- **128 × 265 MB ≈ 34 GB RAM** — comfortable in the server's ~131 GB free.
- Each JVM is single-core-busy only DURING `step()`; between turns it idles waiting
  on the LLM (1–3 s), so 128 JVMs do not saturate 80 cores. Concurrency is LLM-bound.
- **Pool design:** spawn N envs once at harness init (N = rollout concurrency); each
  rollout slot `acquire()`s an env, `load(task, var)`s its episode, `release()`s it.
  **Recycle** an env (`close()` + respawn) every ~M episodes to bound slow RSS creep.
  Cap heap tail with `JAVA_TOOL_OPTIONS="-Xmx256m"`.
- **Wedge safety (the one gap vs subprocess workers):** an in-process py4j `.step()`
  that hangs (wedged JVM) blocks its harness thread — py4j's default socket read has
  no timeout. Mitigation: run each episode under a watchdog; on deadline, `close()`
  the env from another thread (unblocks the socket) and respawn a fresh one for the
  pool. This preserves the "harness thread never blocks unboundedly" invariant that
  `subprocess_worker`'s `select()`-bounded reads give the other envs. **If the
  watchdog proves fiddly, fall back to substrate (c)** — a persistent py-worker pool,
  each holding one `ScienceWorldEnv`, reusing `subprocess_worker` for group-kill +
  bounded reads at the cost of one extra process per slot. (Recommend (e) first;
  (c) documented as the escape hatch.)

### 4d. Server deployment
Full numbered steps + the shippable package in
`env_candidates/scienceworld_deploy/SERVER_DEPLOY.md`. Essentials:
- **Ship ≈16 MB**: `scienceworld-1.2.3-py3-none-any.whl` (bundles the 7.8 MB JAR) +
  `py4j` wheel + `goldpaths-all.zip` (8.7 MB, optional) + the AgentBoard subset jsonl.
- **Install into the CSS harness env** (no separate worker interpreter): offline
  `pip install --no-index --find-links wheels/ scienceworld==1.2.3 py4j`, or Tsinghua
  mirror. Server **Java 17** runs the Java-8-bytecode JAR (verified 8..21 range).
- **Verify:** `ScienceWorldEnv().get_task_names()` → 30; the boil gold-replay smoke → 100/True.

---

## 5. Smoke test results (work item 6) — REAL numbers

Scripts in `env_candidates/scienceworld_smoke/`, results JSON alongside. Java 21,
Python 3.10, `scienceworld==1.2.3`, live qwen `qwen3.6-35b-a3b` endpoint.

### 5a. Oracle gold-path replay (oracle_and_splits.json) — PASS
Loaded 5 diverse tasks with `generateGoldPath=True`, replayed the gold sequence:
**all reached score 100 / isCompleted=True.** boil(39), use-thermometer(22),
chemistry-mix(24), find-living-thing(10), inclined-plane-determine-angle(55) actions.
JVM boot 1.2 s, RSS ~265 MB. Split enumeration over all 30 tasks matches §2a exactly.

### 5b. Long-horizon / gold-coverage probe (gold_length_probe.json)
- `measure-melting-point-unknown-substance` (absent from the offline zip) **IS
  live-gold-generable** → 32 steps, score 100. GT firewall is fine via live gen.
- `mendelian-genetics-known-plant`: live gold **143** steps (no-simpl) / **129** (easy)
  — **> 100**, so `envStepLimit=100` makes even the ORACLE fail mendelian. (Excluded
  from the AgentBoard subset anyway; a step-budget consideration for the original
  protocol.)
- `easy` simplification barely shortens gold paths (boil 39→39, mendelian 143→129):
  its value is removing brittle failure modes (watering, doors) + shrinking the
  action space, **not** shortening solutions.

### 5c. Concurrency + isolation + RSS (concurrency_probe.json) — PASS
See §4b: 8 concurrent JVMs, isolated, 265 MB each.

### 5d. Real LLM ReAct rollout (llm_rollout.json)
Minimal, **memoryless** free-form ReAct (agent sees the current observation +
inventory + the generic action templates each turn, but NOT its own history — a
deliberately weak smoke agent), temperature 0.4, `enable_thinking=false`, ≤40 steps,
`easy` simplification, dev-fold variations, 5 diverse task types.

Per-task result (score trajectory → final / max, all K=1):

| task (dev var) | final | max | steps | done | invalid | pain point |
|---|--:|--:|--:|:--:|--:|---|
| boil (v14) | 38 | 38 | 40 | no | 0 | plateaued at 38 — memoryless agent repeats |
| use-thermometer (v270) | **−100** | 12 | 16 | yes | 0 | drove score to −100 → auto-terminated |
| find-living-thing (v150) | **−100** | −100 | 1 | yes | 0 | ONE wrong `focus on` → −100 on step 1 |
| chemistry-mix-paint-secondary (v18) | 30 | 30 | 40 | no | 12 | stuck at 30; 12 invalid mix commands |
| lifespan-longest-lived (v62) | **100** | 100 | 3 | yes | 0 | **solved in 3 steps** |

Summary: 1 clean solve, 2 partial (38, 30), 2 catastrophic (−100). RSS 243–308 MB/JVM.
Tokens: 43 K prompt / 8 K completion total. **The two `isCompleted` values on the
−100 rows are FAILURES** (negative-score auto-termination), not successes — real
success = 1/5.

**Pipeline: fully validated.** env load/step/score, LLM connect/return, action
parsing, invalid-action feedback, and token/latency/RSS accounting all exercised
end-to-end. **0 parse failures across all 5 tasks** — the model reliably emits
`Action: <cmd>` and the lenient last-line fallback covers the rest.

**Protocol pain points (real, load-bearing for the agent + skill design):**
- **Negative-score auto-termination is brutal.** 2/5 tasks hit −100 (find-living-thing
  on step 1 from a wrong `focus on`; use-thermometer after 16 steps). A single
  premature/incorrect `focus on` can tank an episode instantly. → the real agent's
  prompt + the skill doc must teach *focus discipline*; and it empirically confirms
  keying `hard` off the SCORE (clamped to [0,1] for soft), never off `isCompleted`.
- **Memoryless agent plateaus** (boil 38, chemistry 30). The real agent MUST carry a
  rolling action/observation history (as AgentBoard's VanillaAgent does) — the single
  biggest headroom lever, NOT reflected in these scores.
- **Invalid actions are a soft channel** — ScienceWorld returns `No known action ...` /
  ambiguity strings (no exception); chemistry-mix ate 12 of them without a crash.
- **Wall time is 100% LLM-bound.** Per-call latency ranged 0.5–50 s (shared endpoint
  under load); the 778 s wall on boil was all LLM wait — the env `step()` is sub-ms.
- **Headroom feel:** a proper history-carrying agent should lift the partial-progress
  tasks; consistent with the literature's "open models have real room" (ReAct ~36).

---

## 6. AgentBoard protocol deep-dive (work item 1) — how to reimplement faithfully

Acquired: `git clone hkust-nlp/AgentBoard` (code) + the 90-instance subset from HF
(`hkust-nlp/agentboard`). Saved under `env_candidates/agentboard_scienceworld/`.
**Licenses: code Apache-2.0, DATA GPL-2.0** (use for eval; do not re-license/vendor).

### 6a. The subset (data/scienceworld/test.jsonl)
- **90 instances**, **34 easy / 56 hard**. **2–10 subgoals** each (mode 2 and 5).
- Covers ~20 of 30 task types — Matter (boil/freeze/change-state), Measurement
  (use-thermometer, measure-melting known/unknown), Chemistry (mix, paint sec/tert),
  Classification (find living/non-living/plant/animal), Lifespan (×3), Life-stages
  (×2). **Excludes** the long-horizon/electrical families (mendelian, inclined-plane,
  grow-*, power-component, test-conductivity) — coherent with a 30-step budget.
- Exact per-instance `(task_name, variation)` membership is pinned from the canonical
  `data.tar.gz` schema (`additional_info.env_name/var`); see §6d on the two schemas.

### 6b. Scoring — quote-accurate (agentboard/environment/scienceworld_env.py)
```python
def _check_temperature_string(self, s, selected_obs):   # s = the step observation
    for i, pattern in enumerate(selected_obs):
        if re.search(pattern, s): self.finished_sub_goal[i] = 1.   # latch
def get_reward(self):                                    # == Progress Rate
    return sum(self.finished_sub_goal) / len(self.finished_sub_goal)
def _check_is_done(self, selected_obs):                  # == Success (all subgoals)
    return sum(self.finished_sub_goal) >= len(selected_obs)
```
- **PR** = fraction of subgoal regexes that have matched an observation so far
  (monotonic latch → final == max-so-far). **SR** = 1 iff ALL subgoals matched within
  the budget (AgentBoard's own `done`, NOT ScienceWorld `isCompleted`).
- **Grounding rate** (secondary): fraction of steps whose action is in the valid-action
  set (`get_action_space(abstract=False)`), reported alongside.

### 6c. Episode loop (agentboard/tasks/scienceworld.py + eval_configs)
- **Budget = 30** (`max_num_steps: 30` AND `envStepLimit: 30`, verified in
  `main_results_all_tasks.yaml`). Agent = **VanillaAgent** (direct action, temp 0,
  stop `"\n"`), **1-shot** (one full boil trajectory example) + a command-reference
  instruction + `system_msg`; the agent is shown the **re-annotated `goal`**, not the
  native task description.
- **Simplifications:** `selfWateringFlowerPots,openContainers,openDoors,noElectricalAction`
  (built in `build_simplification_str`) — note this **includes `openContainers`** and
  **excludes `teleportAction`**, i.e. it is NOT the `easy` preset. Faithful
  reproduction MUST use exactly this string.
- **Valid actions:** on-demand only — the meta-action `check valid actions` returns
  the generic templates; the prompt does not enumerate valid actions every turn.

### 6d. Two data schemas (a gotcha)
- **Canonical (`data.tar.gz`, what the code reads):** each line has
  `additional_info.{env_name,var}`, `goal`, `subgoals` (a **list** of regex patterns),
  `difficulty`.
- **HF-unpacked (`data/scienceworld/test.jsonl`, 53 KB, what "Dataset Viewer"
  serves):** `{id, goal, difficulty, subgoals}` where `subgoals` is a single
  `"\nSubgoal N: …"`-joined **string** and `additional_info` is dropped. The README
  warns this copy "is not complete." Splitting the string on `Subgoal N:` recovers the
  regex list, but the **variation index is only in the canonical tar** — needed to load
  the exact world the subgoals reference. Our integration reads the canonical tar's
  `env_name/var`.
- **Acquisition status (this Mac):** the 53 KB unpacked subset was downloaded and
  parsed (90 instances — the analysis in §6a). The **canonical `data.tar.gz` (1.4 GB)
  is throttled by the HF CDN from this location to <1 MB/s across every method tried**
  (curl stream, `hf_transfer`, and a 16-way parallel range download all plateaued near
  ~13 MB) — impractical here. It is NOT a hard block: fetch it (a) on the **server**
  (different network — but the server blocks HF, so via `hf-mirror.com` or scp a copy
  downloaded elsewhere), or (b) on any faster link, then extract the single
  `data/scienceworld/test.jsonl`. **Fallback if the tar stays unreachable:** the `var`
  index is recoverable at integration time by loading each candidate variation of the
  inferred task and matching the instance's subgoal regexes against the world's
  observations/object tree — deterministic but O(variations) work. The design in this
  doc does not depend on `var`; only the eventual `css/envs/scienceworld/` loader does.

### 6e. Reimplementation cost — LOW
We port three small pieces into `css/envs/scienceworld/`: (1) the subgoal **regex
matcher** (~15 lines, verbatim semantics above); (2) the **instance loader** (read the
canonical jsonl → items with `task_name/var/goal/subgoals/difficulty`); (3) the
**prompt** (command reference + the 1-shot example + the on-demand `check valid
actions`). The env, scoring, and step loop are otherwise our standard `run_one`.
**No AgentBoard runtime dependency** — we replay their subset + scoring inside our harness.

---

## 7. Proposed default config list — EVERY value TO NEGOTIATE

Mirrors the `run_experiment_appworld_server.py` convention: the launcher is the
carrier; each value carries its justification; agreed values must not change silently.
**None of these are locked** — this is the negotiation input.

### 7a. Splits & universe
| knob | proposed | justification | status |
|---|---|---|---|
| `n_train` | ~250–300 | stratified train pool over AB task set (~15/task) | **TO NEGOTIATE** |
| `n_val` | ~90 | paired-gate carve, stratified (~4–5/task) | **TO NEGOTIATE** |
| `n_test` (primary) | 90 | the fixed AgentBoard subset | **TO NEGOTIATE** |
| secondary test size | ~150 | original-protocol native-score sample, AB task set | **TO NEGOTIATE** |
| task universe | AB task set (~20) | align train with the SR headline; report generalization | **TO NEGOTIATE** |

### 7b. Runtime
| knob | proposed | justification | status |
|---|---|---|---|
| `max_api_workers` | 128 | RAM ~34 GB @265 MB/JVM; LLM-bound | **TO NEGOTIATE** |
| `concurrency_limit` | 1 | per-slot env; matches ALFWorld/AppWorld | **TO NEGOTIATE** |
| `k_rollouts` | 3 | harness standard; deterministic env → temp gives diversity | **TO NEGOTIATE** |
| `task_timeout_s` | 600 | ≤50 steps × worst-case LLM latency; env step is ms | **TO NEGOTIATE** |
| `max_turns` | 50 | generic mirror of the original-protocol step budget | **TO NEGOTIATE** |

### 7c. Env knobs (`extra["scienceworld_*"]`)
| knob | proposed | justification | status |
|---|---|---|---|
| `scienceworld_step_limit_original` | 50 | covers most native gold paths; matches our other envs (mendelian/inclined excluded from AB) | **TO NEGOTIATE** |
| `scienceworld_step_limit_agentboard` | 30 | EXACT AgentBoard budget (fidelity) | **TO NEGOTIATE** |
| `scienceworld_simpl_original` | `easy` | community/SwiftSage baseline for the native-score track | **TO NEGOTIATE** |
| `scienceworld_simpl_agentboard` | `selfWateringFlowerPots,openContainers,openDoors,noElectricalAction` | EXACT AgentBoard string (NOT `easy`) | **TO NEGOTIATE** |
| `scienceworld_temperature` | 0.4 | ALFWorld/AppWorld precedent (train+eval same temp) | **TO NEGOTIATE** |
| `scienceworld_max_tokens` | 512 | one action + short thought; bounds runaway generations | **TO NEGOTIATE** |
| `scienceworld_valid_actions` | `on_demand` | mirror AgentBoard; avoid 400+-entry context bloat | **TO NEGOTIATE** |
| `scienceworld_history` | rolling full | agent MUST see its action/obs history (headroom lever) | **TO NEGOTIATE** |
| `scienceworld_pool_recycle_episodes` | 200 | bound RSS creep; cheap respawn | **TO NEGOTIATE** |
| `scienceworld_gt_mode` | `live` | live gold gen (covers all 30); `offline_zip`/`none` as ablation | **TO NEGOTIATE** |
| `scienceworld_agentboard_label` | `$DATA/scienceworld/agentboard_test.jsonl` | canonical subset path | **TO NEGOTIATE** |
| `JAVA_TOOL_OPTIONS` | `-Xmx256m` | heap tail cap; ~130 MB non-heap floor stays | **TO NEGOTIATE** |

### 7d. L0/L1 (inherit the AppWorld/ALFWorld V3.4 defaults unless negotiated)
`gate_mode="paired"`, `gate_screen_k=1`, `gate_escalation_k=3` (val ~90 is small →
escalation buys power); `batch_size` ~ half the train pool; `min_l0_epochs=0`,
`max_l0_steps=20`, `l0_stall_steps=8`; `l1_diagnostic_tasks`/`l1_regression_tasks`
sized to fit the pool. All **TO NEGOTIATE**.

---

## 8. Risk register

| # | risk | severity | mitigation |
|--:|---|---|---|
| 1 | **Wedged in-process JVM blocks a harness thread** (py4j no read-timeout) | med | per-episode watchdog → `close()`+respawn; fallback to persistent worker pool (§4c) |
| 2 | **Variation imbalance** (10..1,386) skews the train pool | med | per-task-type stratified caps (§2c/2d) |
| 3 | **Train reward ≠ primary test metric** (native score vs subgoal SR) | med | strongly correlated; flag in the protocol section; optionally add native-score secondary on same tasks |
| 4 | **AgentBoard subgoal fidelity** — regex-on-observation is brittle to obs wording across SW versions | med | pin `scienceworld==1.2.3`; validate the matcher reproduces AgentBoard baseline PR on a few instances before trusting |
| 5 | **Canonical `var` only in `data.tar.gz`** (1.4 GB; HF CDN throttles this Mac to <1 MB/s) | low | fetch tar on a faster link / server-side; or recover `var` by subgoal-pattern matching per variation (§6d). Design does not depend on it |
| 6 | **`measure-melting-unknown` has no offline gold** | low | live `generateGoldPath=True` verified working for it |
| 7 | **JVM RSS creep over many episodes** | low | recycle every M episodes; `-Xmx` cap (both verified harmless) |
| 8 | **Step-budget over-caps long tasks** (mendelian gold 129–143) | low | those tasks are excluded from the AB primary; only affects a full-30-task original secondary — size budget per track |
| 9 | **GPL-2.0 data license** on AgentBoard subset | low | eval-only use; do not vendor into `css/` or re-license |
| 10 | **`get_variations_*` throws before `load()`** | low | documented; split tool loads first (§3a) |

---

## 9. Open questions for the user

1. **Primary metric:** AgentBoard SR (unsaturated, WorldEvolver-comparable) as the
   headline — confirm? And is PR the co-headline or a secondary?
2. **Task universe:** train over the ~20 AgentBoard task types (aligns with SR), or
   over all 30 (general skill, but dilutes the headline)?
3. **Step budgets:** 30 for the AB test track is fixed by fidelity; what budget for
   train/val and the original secondary — 50 (our convention) or 100 (SwiftSage)?
4. **Secondary test:** original-protocol native-score sample on the AB task set only,
   or a full 30-task sample for whole-benchmark reporting?
5. **Valid-actions exposure:** on-demand `check valid actions` (AgentBoard-faithful),
   or always-shown templates, or full valid list? (Recommendation: on-demand.)
6. **Execution substrate:** in-process env pool (recommended) vs a persistent
   subprocess worker pool (more robust to wedges, one extra process/slot)?
7. **GT richness** (`scienceworld_gt_mode`): live gold sequence in the eval annotation
   (default), or the native goal-progress checklist only (a path-free ablation)?

---

## 10. PREP corrections (`scienceworld_PREP.md`)

- **§3 tiny-task leakage:** the PREP says guard `identify-life-stages-*` for
  train/test leakage and that `maxvar==1`/`==2` cause overlap. **Empirically, all 30
  tasks split with ZERO train∩test overlap** (min maxvar=10 → 4/2/4). No task is
  affected; the guard is defensive only. (The `maxvar==1`/`==2` math is correct in
  principle but no real task triggers it.)
- **§4 gold-path archive:** `goldpaths-all.zip` covers **29/30** tasks — it **omits
  `measure-melting-point-unknown-substance` entirely** (300 variations; the "missing
  index 5" in the archive filename). Live `generateGoldPath=True` covers it (verified).
  Grand offline gold total = 3,442/1,721/1,744 = **6,907** (vs the JAR's 7,207).
- **§3 split approximation:** the PREP's "~3,592 / ~1,796 / ~1,819" is **exact** (not
  approximate) — confirmed per-task via `get_variations_*`.
- Everything else in the PREP verified accurate (JVM-per-env, ~265 MB RSS, boot cost,
  py4j model, `easy`≠paper Appendix B.5, scoring semantics).
