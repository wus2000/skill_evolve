# ALFWorld — Integration Prep (text-only mode)

Prepared for integration into the skill-search research harness (vLLM-served qwen,
thread-parallel rollouts). All facts below were **empirically verified** on this Mac
(Python 3.10, clang) unless marked "from source/web". Deployment target is a Linux
server in China: system Python 3.8, conda maybe available, **no GitHub**, PyPI via China
mirror, HF via hf-mirror.com.

- Repo: https://github.com/alfworld/alfworld  (cloned to `env_candidates/alfworld`)
- Cloned commit: `aaba687` (2026-02-08, "Merge PR #122 fix_121")
- Paper: Shridhar et al., *ALFWorld*, ICLR 2021 (arXiv:2010.03768)
- Package version: `alfworld 0.5.0`, pulls `textworld 1.7.0`
- License: ALFWorld **MIT**, TextWorld **MIT**, Fast Downward **GPL v3** (see §10)

---

## 1. TL;DR decision summary

ALFWorld text mode is a clean, stable, lightweight (no GPU/Unity/torch) benchmark and a
strong fit for an in-context / skill-learning method: well-established baselines
(ReAct/Reflexion/ExpeL) and large headroom for prompting agents. The three real
integration frictions are all **deployment-side**, not API-side:

1. **Python ≥3.9 required** (server has 3.8) → must create a conda env.
2. **`jericho` (C) and `fast-downward-textworld` (C++) have no wheels** → compile from
   sdist at install; needs a build toolchain, or ship prebuilt wheels in the bundle.
3. **The env is NOT thread-safe** → incompatible with the harness's 320-thread single-process
   model as-is; use a subprocess env pool (or a global lock for small-scale eval).

Everything else (data, API, success detection, license) is straightforward. Full details
and a copy-paste deployment recipe are in §11.

---

## 2. What ALFWorld text mode is

Text (TextWorld) reimplementation of the ALFRED household tasks: the agent reads a room
description + a goal ("put a clean apple on the diningtable"), and issues text commands
(`go to fridge 1`, `open fridge 1`, `take apple 1 from countertop 1`, `clean apple 1 with
sinkbasin 1`, ...). The world is a **PDDL** simulation (no pixels, no physics engine). 6
task types:

| id | task_type | valid_unseen | valid_seen |
|----|-----------|:---:|:---:|
| 1 | pick_and_place_simple | 24 | 35 |
| 2 | look_at_obj_in_light | 18 | 13 |
| 3 | pick_clean_then_place_in_recep | 31 | 27 |
| 4 | pick_heat_then_place_in_recep | 23 | 16 |
| 5 | pick_cool_then_place_in_recep | 21 | 25 |
| 6 | pick_two_obj_and_place | 17 | 24 |
| | **total** | **134** | **140** |

---

## 3. Python & dependencies (text-only)

**Python**: TextWorld 1.7.0 declares `requires_python >=3.9` (verified on PyPI). The server's
system Python 3.8 will NOT install it. Options:
- **Recommended**: `conda create -n alfworld python=3.9` (or 3.10/3.11).
- Fallback if no conda: pin `textworld==1.6.1` (it declares `>=3.8`, sdist-only, still needs
  compilation). ALFWorld requires `textworld>=1.6.1` so 1.6.1 satisfies it, but this is
  older/less tested — prefer conda.

**Text-only dependency set** is tiny (from `alfworld/requirements.txt`):
```
textworld[pddl]>=1.6.1
pyyaml
```
`pip install alfworld` (WITHOUT `[full]`/`[vis]`) installs ONLY this — **no torch, no
ai2thor, no opencv**. Transitive deps actually installed (verified): `numpy, networkx,
tatsu==5.8.3, jericho, fast-downward-textworld, spacy, prompt_toolkit, tqdm, termcolor,
hashids, mementos, more_itertools` (+ spacy's chain). Total install ~46 s on this Mac.

**Compilation risk (the important one).** Two deps ship **sdist only — no wheels**:
- `jericho` (Frotz-based Z-machine interpreter, C) — small, quick to build.
- `fast-downward-textworld 20.6.4` (Fast Downward planner, C++) — builds a
  `libdownward.so` (verified present after install) + a Python `translate/translate.py`.
  Its build **needs cmake**; `pip install cmake` (a wheel) satisfies it.

TextWorld 1.7.0 itself ships a `manylinux2014_x86_64` wheel → no compile for the core.
On this Mac the whole `textworld[pddl]` build (jericho + fast-downward) finished in ~46 s
with Apple clang. On the offline Linux server the sdists come from the PyPI mirror, but
**compilation must succeed there** → needs `gcc, g++, make, cmake, python3.9-dev`. If the
server lacks a toolchain, **prebuild wheels** on a matching Linux/py3.9 box and ship them
(see §11).

> Note: `fast-downward-textworld` is **not optional** for text mode — TextWorld invokes its
> PDDL grounder (translate.py) on **every game load** (verified: FD translator output prints
> at each `reset`). It is a hard runtime dependency, and it is GPL v3 (see §10).

---

## 4. Data: files, sources, sizes, layout

`alfworld-download` pulls 3 zips (text mode) + 1 detector (vision only), all from **GitHub
release assets** (verified sizes via HTTP HEAD, and downloaded+unpacked successfully):

| asset | release | compressed | contents | needed for text? |
|-------|---------|:---:|----------|:---:|
| `json_2.1.1_json.zip` | 0.2.2 | 68.6 MB | `traj_data.json` (goals, gold trajs) | ✅ |
| `json_2.1.1_pddl.zip` | 0.2.2 | 33.2 MB | `initial_state.pddl` | ✅ |
| `json_2.1.3_tw-pddl.zip` | 0.4.2 | 34.8 MB | `game.tw-pddl` (pre-built games) | ✅ |
| `mrcnn_alfred_objects_sep13_004.pth` | 0.2.2 | 169.6 MB | Mask-RCNN detector | ❌ vision only |
| `alfred.pddl` + `alfred.twl2` | (ships in pip pkg) | ~30 KB | domain + grammar | ✅ copied to `$ALFWORLD_DATA/logic/` |

- **Text-mode total: ~137 MB compressed → ~2.0 GB unpacked** (verified `du -sh`).
- **No GitHub on the server** → download the 3 zips on the Mac and `scp` them (or scp the
  unpacked tree). The MRCNN detector (170 MB) is **not** needed.
- The two logic files ship inside the pip package (`alfworld/data/`); the download script
  just copies them into `$ALFWORLD_DATA/logic/`. Reproduce with a 3-line Python snippet
  (in §11) — no network needed.

**On-disk layout** (default `$ALFWORLD_DATA = ~/.cache/alfworld`):
```
$ALFWORLD_DATA/
  logic/{alfred.pddl, alfred.twl2}
  json_2.1.1/
    train/        <task_type>-<obj>-...-<sceneid>/trial_T*/  → 3553 game.tw-pddl
    valid_seen/   (same structure)                           →  140 game.tw-pddl
    valid_unseen/ (same structure)                           →  134 game.tw-pddl   ← standard eval
    valid_train/  (same structure)                           →  200 game.tw-pddl
```
Each leaf `trial_*/` holds `traj_data.json` (goal + gold action seq), `initial_state.pddl`,
and `game.tw-pddl` (self-contained playable game = domain+grammar+problem, all solvable-tagged).
Game counts verified by `find ... -name game.tw-pddl | wc -l`. (There are more
`traj_data.json`/`initial_state.pddl` than games because of extra annotations/variants; the
authoritative game count is the `game.tw-pddl` files, which the loader filters to solvable.)

---

## 5. Text vs THOR mode — text avoids Unity/THOR entirely (confirmed)

Text mode = `AlfredTWEnv`. It imports only `textworld`; nothing pulls in ai2thor, Unity, an
X server, or a GPU. Verified: the text-only install has no `ai2thor`, and full episodes run
headless with no display. Vision mode (`AlfredThorEnv`) is the one that needs
`ai2thor==2.1.0` + Unity + the 170 MB MRCNN detector + an X server (`startx`) — **not
required for our use**. `env_type` is selected via config (`env.type: AlfredTWEnv`).

---

## 6. Programmatic API (text game)

Two entry points. For a custom agent/eval loop, **use the single-game API** — it's the
cleanest and is exactly what `scripts/alfworld-play-tw` uses.

### 6a. Minimal single-game (recommended)
```python
import textworld, textworld.gym
from alfworld.agents.environment.alfred_tw_env import AlfredDemangler

def make_env(gamefile, max_steps=50):
    infos = textworld.EnvInfos(won=True, admissible_commands=True)   # request what we need
    env_id = textworld.gym.register_game(
        gamefile, infos, max_episode_steps=max_steps,
        wrappers=[AlfredDemangler()])                                # demangler → readable object names
    return textworld.gym.make(env_id)

env = make_env(".../valid_unseen/.../game.tw-pddl")
obs, infos = env.reset()                     # obs: str (room + "Your task is to: ...")
#   infos["won"]  -> False
#   infos["admissible_commands"] -> list[str], e.g. ["go to bed 1", "go to desk 1", ...]
obs, score, done, infos = env.step("go to desk 1")
#   done == True when infos["won"] becomes True OR max_steps reached
```
- The pre-built `game.tw-pddl` files can be loaded **directly** (no rebuild). Just glob them:
  `glob(".../valid_unseen/**/game.tw-pddl", recursive=True)`.
- Real sample `reset()` obs:
  > `-= Welcome to TextWorld, ALFRED! =-` … `you see a bed 1, a desk 2, ... a shelf 1.`
  > `Your task is to: examine the alarmclock with the desklamp.`

### 6b. Config/batch API (the README path)
```python
import alfworld.agents.modules.generic as generic
from alfworld.agents.environment import get_environment
config = generic.load_config()                       # reads a YAML given on argv, e.g. base_config.yaml
env = get_environment(config['env']['type'])(config, train_eval='eval_out_of_distribution')
env = env.init_env(batch_size=N)                      # batched; asynchronous=True → multiprocessing
obs, infos = env.reset()                              # obs is a list[str] of length N
obs, scores, dones, infos = env.step([cmd_0, ..., cmd_{N-1}])
```
`train_eval` ∈ `{"train", "eval_in_distribution"(valid_seen), "eval_out_of_distribution"(valid_unseen)}`.
`num_eval_games`/`task_types` in the YAML subset the set. This path is heavier (needs the
full config) and its batch env uses multiprocessing — see §8.

### 6c. Oracle expert (optional, for gold trajectories / imitation)
Add the handcoded expert wrapper to get the oracle next action in `infos`:
```python
from alfworld.agents.environment.alfred_tw_env import AlfredExpert, AlfredExpertType
infos = textworld.EnvInfos(won=True, admissible_commands=True, extras=["expert_plan"])
env_id = textworld.gym.register_game(gamefile, infos, max_episode_steps=50,
            wrappers=[AlfredDemangler(), AlfredExpert(expert_type=AlfredExpertType.HANDCODED)])
# infos["extra.expert_plan"] -> [next_gold_action]
```
Verified: following `expert_plan` solves a game with `won=True` in 12 steps. (There is also
a `PLANNER` expert that shells out to Fast Downward — much slower, avoid for real-time.)

### 6d. Success detection & max steps
- **Success = `infos["won"] is True`** (bool). `score` goes 0 → 1 on completion; `done` is
  True on win **or** when `max_episode_steps` is hit (then `won` stays False).
- **Max-steps convention = 50** (`base_config.yaml: max_nb_steps_per_episode: 50`, for both
  dqn/dagger). ReAct/Reflexion-style papers typically also cap ~30–50 steps.
- **Eval metric**: success rate = (#games with `won`) / 134 over `valid_unseen`. The repo's
  `evaluate_dagger.py` loops episodes in batches, runs up to `max_nb_steps_per_episode`, and
  aggregates `won`.

---

## 7. A minimal eval loop for our harness
```python
games = sorted(glob(f"{DATA}/json_2.1.1/valid_unseen/**/game.tw-pddl", recursive=True))  # 134
wins = 0
for g in games:                       # NB: see §8 — parallelize across PROCESSES, not threads
    env = make_env(g, max_steps=50)
    obs, infos = env.reset()
    for _ in range(50):
        action = agent(obs, infos["admissible_commands"])   # your vLLM/qwen call
        obs, score, done, infos = env.step(action)
        if done: break
    wins += int(infos["won"])
success_rate = wins / len(games)
```

---

## 8. Concurrency — CRITICAL for the harness

**The text env is NOT thread-safe.** Root cause (verified via traceback): the `tatsu` PEG
parser that parses the TWL2 grammar (`tatsu/contexts.py`, a process-global `_rule_stack`) is
shared module state; concurrent access corrupts it → `IndexError: pop from empty list`.

Measured on this Mac (valid_unseen games, `AlfredDemangler`, 15 random steps each):

| pattern | result |
|---------|--------|
| create 6 envs concurrently in 6 threads, no lock | **1/6 ok** (5 crash) |
| create 16 envs serially, then STEP them concurrently in 16 threads | **1/16 ok** (stepping is also unsafe!) |
| every env call (create+reset+step) behind ONE global lock, 24 threads | **24/24 ok**, wall 38.2 s |
| 4 envs each in its own **process** (fork), create+step | **4/4 ok**, wall 2.9 s |
| TextWorld native batch `register_games(batch_size=8, asynchronous=True)` (fork) | **8/8 ok**; reset 4.7 s, then ~57 ms/lockstep-step across 8 envs |

Implications for a **single-process, 320-thread** harness:
- **Do NOT** run rollouts as in-process threads sharing the env — it will crash
  non-deterministically (even stepping distinct envs concurrently breaks).
- **Cheap fix (eval-scale): one global lock** around every `reset`/`step`. Correct, but it
  **serializes** env ops. Since a step is ~7 ms and LLM latency dominates, mid-episode lock
  contention is negligible; the real cost is the **~1 s Fast Downward grounding per `reset`**,
  which becomes serial → caps episode *starts* at ~1/s. Fine for a 134-game eval pass (~few
  min); a bottleneck for training-scale rollouts.
- **Scalable fix: a subprocess env pool** — one env per worker process (fork), harness
  threads talk to them via a queue/pipe; grounding then parallelizes across cores. TextWorld's
  `asynchronous=True` batch env is a ready-made lockstep version; for independent async
  rollouts, wrap each env in its own short-lived process.

**Costs (measured):**
- **Startup ~1.0 s/episode** (FD PDDL grounding on every load; no built-in cache — re-loading
  the same game is ~1.03 s every time). This is the dominant per-episode overhead.
- **Step ~7 ms/env** (cheap).
- **Memory ~86 MB RSS per fresh env process** (mostly the shared interpreter + numpy + spacy +
  textworld; under `fork` most is copy-on-write shared). Budget ~30–90 MB *incremental* per
  process → 320 procs ≈ 10–28 GB depending on COW sharing. Tune the process-pool size to RAM.
- **`asynchronous=True` uses multiprocessing** → on Linux the default `fork` start method works
  with no `__main__` guard. (On macOS default `spawn` it recurses without a guard — a Mac-only
  gotcha, irrelevant on the Linux server.)

---

## 9. Eval protocol, SOTA lineage, saturation

**Standard protocol**: report success rate on the **134 `valid_unseen`** games, 50-step
budget, 6 task types. (140 `valid_seen`, 3553 `train`, 200 `valid_train` also available.)

**Success-rate lineage** (success = % of 134 unseen solved; numbers from papers/leaderboards,
treat as approximate — max-steps and base model vary):

| method | base model | unseen SR | notes |
|--------|-----------|:---:|-------|
| BUTLER | imitation learning | ~37% | original ALFWorld agent |
| Act (no reasoning) | GPT-3 | ~45% | ReAct paper baseline |
| **ReAct** | GPT-3 (text-davinci-002) | **~71%** | best trial; the classic in-context baseline |
| **Reflexion** | GPT-3 + self-reflection memory | **~97–100%** (≈130–134/134) | after several trials-with-memory; single-config comparisons report lower |
| **ExpeL** | GPT-3.5 | **~54–59%** | in-context experiential learning, single attempt |
| GPT-4o (prompting) | GPT-4o | ~48% | reported in 2025 agent-RL papers |
| Gemini-2.5-Pro | prompting | ~60% | same source |
| AgentPRM / InversePRM | 3B, RL-trained | 88–91% | trained agents |
| BEACON | 1.5B, RL-trained | ~91% | trained agents |

**Saturation**: **not saturated for zero-shot / in-context prompting agents.** Mid-size open
models (qwen-2.5-32B class) sit roughly ~40–65% zero-shot ReAct on unseen — large headroom.
Only **RL-trained** small models reach ~90%. So for a skill-learning / in-context-optimization
method served by qwen, ALFWorld is a good target: strong, directly-comparable baselines
(ReAct/Reflexion/ExpeL) and clear room to show gains. (Frontier closed models via plain
prompting are ~50–60%, i.e. also not maxed out.) Numbers above are from web sources and should
be re-confirmed against the specific papers before citing.

---

## 10. License

- **ALFWorld — MIT**, **TextWorld — MIT** (permissive; fine to vendor/modify).
- **Fast Downward — GPL v3.** It ships inside `fast-downward-textworld` and its grounder runs
  at **every game load** (hard runtime dep for text mode). GPL v3 is a copyleft license: using
  it as an unmodified installed dependency at runtime is normal, but be aware of the
  implication if ALFWorld code is redistributed as part of a combined/derivative work. Flagging
  for the team's license review; not a blocker for internal research use.

---

## 11. Offline-server deployment recipe

**A. Get a Python ≥3.9 env** (server system Python is 3.8):
```bash
conda create -n alfworld python=3.10 -y && conda activate alfworld
# ensure a build toolchain is present for the sdist compiles:
conda install -y gxx_linux-64 make cmake        # or: system gcc/g++/make + `pip install cmake`
```

**B. Install alfworld (text only).** Pick ONE:
- *If the server can compile* (toolchain present, PyPI mirror reachable):
  ```bash
  pip install -i <china-mirror> cmake
  pip install -i <china-mirror> alfworld        # text only; do NOT add [full]/[vis]
  ```
- *If the server can't/shouldn't compile* — **prebuild wheels** on a matching Linux x86_64 +
  py3.10 box (e.g. a manylinux docker), ship them in the git bundle, install offline:
  ```bash
  # on the build box:
  pip wheel "textworld[pddl]>=1.6.1" pyyaml alfworld -w ./alf_wheels
  # ship ./alf_wheels in the bundle, then on the server:
  pip install --no-index --find-links=./alf_wheels alfworld
  ```

**C. Ship the data** (no GitHub on server). On the Mac (already done here — data is in
`~/.cache/alfworld`), scp the 3 text-mode zips or the unpacked tree:
```bash
# already downloaded on this Mac; either scp the ~2GB unpacked tree, or the 3 zips (~137MB):
#   json_2.1.1_json.zip, json_2.1.1_pddl.zip, json_2.1.3_tw-pddl.zip  → unzip into $ALFWORLD_DATA
scp -r ~/.cache/alfworld  server:/path/to/alfworld_data
# on server:
export ALFWORLD_DATA=/path/to/alfworld_data
```

**D. Create the logic files** (they ship in the pip package; no network):
```python
import os, shutil
from os.path import join
from alfworld.info import ALFWORLD_DATA, ALFRED_PDDL_PATH, ALFRED_TWL2_PATH
os.makedirs(join(ALFWORLD_DATA, "logic"), exist_ok=True)
shutil.copy(ALFRED_PDDL_PATH, join(ALFWORLD_DATA, "logic", "alfred.pddl"))
shutil.copy(ALFRED_TWL2_PATH, join(ALFWORLD_DATA, "logic", "alfred.twl2"))
```
(Equivalently `alfworld-download` does C+D, but it needs GitHub for the zips — so on the
offline server, scp the data and run just this snippet.)

**E. Smoke test**:
```python
import glob, textworld, textworld.gym
from alfworld.info import ALFWORLD_DATA
from alfworld.agents.environment.alfred_tw_env import AlfredDemangler
g = sorted(glob.glob(f"{ALFWORLD_DATA}/json_2.1.1/valid_unseen/**/game.tw-pddl", recursive=True))[0]
infos = textworld.EnvInfos(won=True, admissible_commands=True)
env = textworld.gym.make(textworld.gym.register_game(g, infos, max_episode_steps=50, wrappers=[AlfredDemangler()]))
obs, i = env.reset(); print(obs[:200]); print(len(i["admissible_commands"]), "commands")
```

**F. Concurrency adapter**: wrap env access in a subprocess pool (recommended) or a single
global lock (quick). Do not run env calls on the harness's shared rollout threads directly.

---

## 12. Integration effort & risk register

**Effort: low-to-moderate.** The env API is small, stable, and headless. Net-new code is a
thin agent loop (format obs + admissible commands → qwen → parse action → `step` → read
`won`) plus a concurrency adapter. No training code needed for eval.

Risks, ranked:
1. **Python 3.9 requirement vs server's 3.8** → conda env mandatory. *(High-likelihood, easy fix.)*
2. **Offline compile of jericho (C) + fast-downward (C++)** → need toolchain or prebuilt wheels.
   *(Medium; mitigated by shipping wheels.)*
3. **Thread-unsafety vs 320-thread harness** → subprocess pool or global lock. *(Design decision,
   must be made before wiring rollouts.)*
4. **GitHub-only data** → scp the ~137 MB / 2 GB from the Mac. *(Easy; already downloaded here.)*
5. **GPL-v3 runtime dep (Fast Downward)** → license awareness if redistributing. *(Low; flag only.)*
6. **~1 s/episode grounding cost, no cache** → throughput ceiling at scale; parallelize across
   processes. *(Medium at training scale, negligible for a 134-game eval.)*

Local artifacts from this prep (on the Mac, for reuse): venv at
`env_candidates/.venv_alf`, data at `~/.cache/alfworld`, repo at `env_candidates/alfworld`.
