# ScienceWorld — Integration Prep

Prepared for the skills_evolve research harness (frozen-agent, test-time skill/memory evolution).
All findings below were **verified empirically** on this machine (macOS, Java 21, Python 3.10)
by cloning the repo and `pip install scienceworld` into a clean venv, unless marked "(from docs)".

- Repo: https://github.com/allenai/ScienceWorld — cloned to `env_candidates/scienceworld` (commit `f6d8f5e`)
- Paper: Wang et al., *ScienceWorld: Is your Agent Smarter than a 5th Grader?* EMNLP 2022 (arXiv:2203.07540)
- Installed/verified version: **1.2.3** (PyPI). License: **Apache-2.0**.

---

## 1. TL;DR decision summary

| Dimension | Finding |
|---|---|
| Runtime engine | Scala 2.12.9 simulator compiled to a single JAR, driven from Python over a **py4j** JVM bridge |
| Java requirement | **JDK/JRE 8+** (JAR bytecode is Java 8 / major-version 52). Verified running on Java 21 → forward-compatible |
| Data bundling | **JAR (7.78 MB) ships inside the pip wheel** (`package_data`). No GitHub, no network at runtime. Pure `py3-none-any` wheel. Sole dependency: `py4j` |
| Concurrency model | **1 separate JVM OS-process per `ScienceWorldEnv`** (py4j `launch_gateway`, dynamic port). Fully isolated; never thread-share one env |
| Memory / instance | ~127 MB idle floor → **~200–300 MB RSS active** per JVM. 100 concurrent ≈ **20–30 GB RAM** |
| Tasks | **30 task types**, 10 task-ID groups, ~8 science domains, **7,207 total variations**, split 50/25/25 train/dev/test |
| Scoring | Continuous **0–100 progress score** + boolean completion flag; step reward = Δscore |
| Ground truth | `get_gold_action_sequence()` (oracle path) available for the optimizer — keep it away from the agent |
| Eval protocols | (a) Original: all 30 tasks, held-out test variations, avg 0–100 score; (b) AgentBoard subset: success-rate + re-annotated progress-rate (used by WorldEvolver et al.) |
| Integration effort | **Low.** `pip install scienceworld` + a JDK. Thin Gym-like API already present |

---

## 2. Package, versions, Java requirement (verified)

- `pip install scienceworld` → installs **scienceworld 1.2.3** + **py4j 0.10.9.9** and nothing else.
- The wheel is **pure Python** (`py3-none-any`) and **contains the compiled JAR**:
  `site-packages/scienceworld/scienceworld.jar` (7,784,058 bytes) — confirmed present after install.
  Also bundled: `tasks.json`, `object_type_ids.tsv`. → **No GitHub access needed at runtime.**
- Python: `setup.py` says `>=3.7`; README recommends **3.8+**; CI (`tox.ini`) tests py38–py312.
  Server's system Python 3.8 is fine.
- **Java: 1.8+ required** (from README, and confirmed by JAR bytecode inspection: 3,559 class files,
  max major version **52 = Java 8**; Scala runtime 2.12.9 vendored inside). It ran cleanly under **Java 21**,
  so any JDK/JRE ≥ 8 works. The JVM is located by py4j via the `java` on `PATH` (or `JAVA_HOME`).
- No native/OS-specific components beyond the JVM → the same wheel works on the Linux server.

### Deployment on the China server (no GitHub, conda available, JVM unknown)
1. **Python package** — install from a China PyPI mirror (pure-python wheel, mirror-friendly):
   `pip install -i https://pypi.tuna.tsinghua.edu.cn/simple scienceworld==1.2.3`
   *Fallback if the mirror lacks it:* the git-bundled repo already contains the JAR, so
   `pip install .` (or `pip install ./scienceworld`) from the bundle works offline.
2. **JVM (this resolves the "JVM availability unknown" risk)** — the server has conda, so a JDK can be
   installed **without root, into the env**:
   `conda install -c conda-forge openjdk=21` (8/11/17/21 all available; conda-forge currently ships up to 25).
   Then `java -version` must succeed on `PATH`. No `apt`/root needed.
3. Verify: `python -c "from scienceworld import ScienceWorldEnv; e=ScienceWorldEnv(); print(len(e.get_task_names()))"`
   → prints `30`.

---

## 3. Task structure (verified against README table + live API)

- **30 task types**, addressed by paper ID (`"1-1"`…`"10-2"`) or by task name (`"boil"`, `"grow-plant"`, …).
  Legacy integer index `--task-num` (0–29) into `get_task_names()` is also accepted by the example agents.
- Grouped into **10 task-ID groups** ≈ 8 science domains (topic labels in `tasks.json`:
  Matter, Measurement, Electricity, Classification, Biology, Chemistry, Forces):

  | Group | Domain | Tasks (IDs) |
  |---|---|---|
  | 1 | Matter / thermodynamics | boil, melt, freeze, change-state (1-1..1-4) |
  | 2 | Measurement | use-thermometer, measure-melting-point known/unknown (2-1..2-3) |
  | 3 | Electricity | power-component, renewable-vs-non, test-conductivity known/unknown (3-1..3-4) |
  | 4 | Classification | find living / non-living / plant / animal (4-1..4-4) |
  | 5 | Biology (growth) | grow-plant, grow-fruit (5-1..5-2) |
  | 6 | Chemistry | chemistry-mix, mix-paint secondary/tertiary (6-1..6-3) |
  | 7 | Biology (lifespan) | longest / shortest / longest-then-shortest lived (7-1..7-3) |
  | 8 | Biology (life stages) | plant, animal (8-1..8-2) |
  | 9 | Forces | inclined-plane angle, friction named/unnamed (9-1..9-3) |
  | 10 | Genetics | mendelian known / unknown plant (10-1..10-2) |

- **Variations per task**: range from 10 (`identify-life-stages-2`) to **1,386** (`inclined-plane-friction-named-surfaces`).
  **Total across all 30 tasks = 7,207 variations.** Each variation randomizes substances/objects/layout so agents
  must generalize rather than memorize.
- **Train/dev/test split** is computed **deterministically inside the JAR**, not stored as files
  (`PythonInterface.getSets()`): **50% / 25% / 25%**. Dev/test variations deliberately contain
  substances/animals/plants **not seen in train** (paper §; verified: `boil` maxvar=30 → train=14 [idx 0–13],
  dev=7 [14–20], test=9 [21–29]). Corpus-wide (**exact**, measured per task via `get_variations_*`):
  **3,592 train / 1,796 dev / 1,819 test** (= 7,207).
  ⚠️ The `maxvar==1`/`==2` overlap math holds in principle, BUT — **correction (2026-07-04,
  verified live): NO real task triggers it.** The smallest task is `identify-life-stages-2`
  (maxvar=10 → 4/2/4) and **all 30 tasks split with ZERO train∩test overlap.** The leakage
  guard is defensive only; no task needs exclusion. See `scienceworld_ONBOARDING.md` §2b/§10.
- **Episode length**: bounded by `envStepLimit` (constructor arg, **default 100**). Oracle/gold trajectories run
  ~15–50+ actions; AgentBoard buckets tasks as short (<37 steps) vs long (>37 steps). Papers commonly use 100;
  some long-horizon setups use higher limits.

### Scoring (verified)
- `step()` returns `(observation, reward, isCompleted, info)`.
- **Score = continuous 0–100** = task completion fraction (Scala returns 0.0–1.0; the Python wrapper multiplies by 100
  and rounds to int). E.g. final score 60 = 60% of the task's subgoals achieved.
- **`reward` = Δscore** since the previous step (can be negative).
- **`isCompleted`** becomes `True` on success, on exceeding `envStepLimit`, or when **score < 0**
  (some tasks penalize wrong actions to a negative score → auto-terminate).
- `get_goal_progress()` returns a human-readable **subgoal checklist** string (which sequential subgoals are done).
- `info` dict keys: `moves, score, reward, look, inv, taskDesc, valid, variationIdx, taskName, simplificationStr`.

### Simplification flags (6; verified)
`teleportAction`, `selfWateringFlowerPots`, `openContainers`, `openDoors`, `noElectricalAction`, and
`easy` (= all of the above **except `openContainers`** in code). Passed as a comma-separated string (no spaces)
to `load(..., simplificationStr=...)`.
- **Community standard**: the paper's main results use the **`easy` preset**
  (`teleportAction,openDoors,selfWateringFlowerPots,noElectricalAction` for non-electrical tasks).
  ⚠️ README warns the code `easy` preset **differs from the paper's Appendix B.5** — it omits `openContainers`;
  add it manually to match the paper exactly. For a hard/realistic setting, pass `""` (no simplifications).
  `noElectricalAction` is auto-rejected for electrical tasks (`power-component*`, `*conductivity*`).

---

## 4. Programmatic API (Gym-like; snippet verified end-to-end)

```python
from scienceworld import ScienceWorldEnv

# One env == one JVM subprocess. Reuse it across episodes; do NOT share across threads.
env = ScienceWorldEnv(taskName="", serverPath=None, envStepLimit=100)  # serverPath=None -> bundled JAR

env.get_task_names()                 # -> 30 task names
env.get_max_variations("boil")       # -> 30
tr = env.get_variations_train()      # -> [0..13]   (dev/test analogous)
dv = env.get_variations_dev()
te = env.get_variations_test()

# Load a task variation. generateGoldPath=True computes the oracle path (optimizer-only!).
env.load("boil", variationIdx=0, simplificationStr="", generateGoldPath=True)

obs, info = env.reset()              # obs = 'look around' output; info dict as above
env.get_task_description()           # natural-language goal
env.get_goal_progress()             # subgoal checklist string  (== Scala getGoalProgressStr)

# Action space at current state:
env.get_valid_action_object_combinations()               # list[str] of valid "action obj" combos (417 at boil start)
env.get_valid_action_object_combinations_with_templates()# same, with template_id + obj_ids
env.get_possible_actions()           # action templates only
env.get_possible_objects()           # referable objects

# ----- GROUND TRUTH for the optimizer (keep OUT of the agent's context) -----
gold = env.get_gold_action_sequence()   # list[str]; requires load(generateGoldPath=True)

# Rollout loop
obs, reward, done, info = env.step("open door to kitchen")
score = info["score"]                # 0..100
# ... executing the gold path reaches score==100, done==True (verified: 36 steps for boil/var0)

env.close()                          # shuts down the JVM subprocess (also called on __del__)
```

Notes:
- Method names are **snake_case**; camelCase aliases exist but emit deprecation warnings.
- **Gold path is the GT source** for the frozen-agent optimizer. Aligns with the project's GT-firewall principle
  (optimizer may read gold; the agent's context must never contain it). A precomputed
  `goldpaths/goldpaths-all.zip` (8.7 MB, tagged by fold) also ships in the repo if you
  prefer offline GT over live generation. Live generation adds a "Generating Gold Path" pass at `load()` time.
  ⚠️ **Correction (2026-07-04, verified):** the zip covers **29/30 tasks / 6,907 variations**
  (3,442 train / 1,721 dev / 1,744 test) — it **omits `measure-melting-point-unknown-substance`
  entirely** (300 variations; the "missing index 5" in the archive filename). That task's GT
  must come from **live `generateGoldPath=True`** (verified working — 32-step gold, score 100).
- `reset()` re-runs `load()` with the same task/variation (regenerates gold if it was enabled).

---

## 5. Concurrency & memory (verified — the load-bearing constraint)

**Model: process-parallel, one JVM per env.** Each `ScienceWorldEnv.__init__` calls py4j `launch_gateway`,
which **spawns a dedicated `java` subprocess** on a dynamic port with a private Python callback server
(`die_on_exit=True`). Verified: 5 concurrent envs → 5 distinct JVM PIDs, fully independent state
(stepping one env does not affect the others).

- **Thread-safety**: a single env holds **mutable per-instance state** in the JVM (`score`, `isComplete`,
  `currentHistory`). **Never share one `ScienceWorldEnv` across threads.** The correct pattern for the
  100+-concurrent-rollout harness: **one `ScienceWorldEnv` per worker thread/rollout slot**, reused across
  episodes via `load()`/`reset()`. This is exactly the DRRN/paper baseline pattern.
- **Boot cost**: ~0.6–0.7 s to spawn+connect one JVM (verified). 100 serial spawns ≈ 60–70 s; spawn a pool
  once at startup and reuse.
- **Memory per JVM (verified, default heap):** idle ~127 MB → after `load` ~167 MB → after `reset` ~186 MB →
  climbs to ~200–300 MB RSS during an episode. 5 concurrent averaged ~198 MB each.
  RSS is dominated by JVM non-heap (metaspace/threads/code-cache), not the Java heap.
- **Heap capping**: you can inject JVM flags via the **`JAVA_TOOL_OPTIONS`** env var (the wrapper does not expose
  `javaopts`). Verified `JAVA_TOOL_OPTIONS="-Xmx128m"` still runs correctly; it trims heap growth but the ~130 MB
  non-heap floor remains. Useful to bound tail growth on long runs.
- **Budget rule of thumb**: plan **~250–300 MB/instance**. **100 concurrent JVMs ≈ 25–30 GB RAM** (+ CPU for 100
  JVMs). If RAM-bound, either cap concurrency, use `-Xmx`, or shard across machines. Each JVM is ~single-core busy
  only during `step()`; otherwise idle.
- **Leak watch**: RSS grows within an episode; reusing one JVM across *many* `load()`/`reset()` cycles should be
  monitored for slow growth. Mitigation: periodically `close()` + respawn a worker's env every N episodes.
- `SCIENCEWORLD_DEBUG=1` enables JVM stdout + a JDWP debug port (do **not** set in production).

---

## 6. Evaluation protocols & SOTA lineage (from docs/papers)

Two conventions dominate; pick per comparability target.

**(A) Original ScienceWorld / SwiftSage protocol** — all 30 tasks; evaluate on the **held-out test variations**
(the 25% unseen-substance split); metric = **average final score (0–100)** over test episodes, `envStepLimit≈100`.
SOTA lineage (avg score, all-30-task mean):

| Agent | Avg score |
|---|---|
| SayCan | 33.8 |
| ReAct | 36.4 |
| Reflexion | 45.3 |
| **SwiftSage** | **84.7** |

**(B) AgentBoard subset protocol** — ScienceWorld under AgentBoard's "Embodied AI" category, using a **curated
subset** with AgentBoard's **re-annotated subgoals** (native ScienceWorld subgoals are sparse/unevenly weighted).
Metrics: **success rate** (goal fully achieved) and **progress rate** (fraction of subgoals). Tasks bucketed
short (<37 steps) / long (>37 steps). This is the protocol used by the recent test-time-evolution papers most
relevant to skills_evolve:
- **WorldEvolver** (*Self-Evolving World Models for LLM Agent Planning*, arXiv:2606.30639): frozen agent +
  frozen weights, revises **deployment-time context/memory** (episodic + semantic memory, selective foresight);
  evaluated on ALFWorld + ScienceWorld via AgentBoard success rate; reports e.g. **+3.33** avg success on
  ScienceWorld with a **Gemma-4-26B-A4B** (~26B open) backbone. ← closest paradigm match to our harness.
- **Evo-Memory** (arXiv:2511.20857): streaming benchmark; ScienceWorld as a multi-turn goal-oriented task stream
  where memory is accumulated/updated across a **sequence** of variations (test-time learning). Proposes ReMem
  (action-think-memory refine).

**(C) Streaming / continual protocol** (Evo-Memory style) — order the test variations into a stream and let the
optimizer accumulate skills/memory across episodes; report score trajectory / cumulative improvement rather than
i.i.d. average. This best exercises the skills_evolve loop.

Open ~30B-class results are still sparse/scattered in the literature; the WorldEvolver Gemma-4-26B numbers are the
cleanest recent open-model reference point on the AgentBoard ScienceWorld subset. For our own runs on the local
qwen endpoint, protocol (A) on the 25% test split gives the most directly citable "avg score" number; protocol
(C) best demonstrates test-time skill evolution.

---

## 7. Recommended harness integration

- **Split usage**: optimize skills on `get_variations_train()`, model-select on `get_variations_dev()`, report on
  `get_variations_test()`. Never touch test during optimization. Watch the tiny-task leakage edge cases (§3).
- **GT firewall**: use `get_gold_action_sequence()` (or `goldpaths-all.zip`) only inside the optimizer/reflector;
  scrub it from any context the frozen agent sees.
- **Rollout worker**: pool of N `ScienceWorldEnv` (N = concurrency), each its own JVM; `load(task, var, simpl)` per
  episode; cap `envStepLimit` (100 default) to bound cost; recycle env every M episodes to bound RSS.
- **Simplifications**: default to `easy` for baselines to match published numbers; use `""` for the hard setting.
- **Determinism**: environment seeds off `variationIdx` (`Random.setSeed(variationIdx)` in `doLoad`), so a
  (task, variation, simplification) triple is reproducible; agent/LLM stochasticity is the only remaining variance.

## 8. License
**Apache License 2.0** (`LICENSE` in repo; also declared in `setup.py` classifiers). Permissive — fine to vendor
the JAR/wheel into our harness and redistribute within the project, with attribution + license notice retained.
