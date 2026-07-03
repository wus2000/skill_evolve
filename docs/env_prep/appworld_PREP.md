# AppWorld — Integration Prep

Prepared for integrating **AppWorld** (arXiv [2407.18901](https://arxiv.org/abs/2407.18901), ACL'24 Best Resource Paper; [appworld.dev](https://appworld.dev)) into our research harness.

> Every number in the "verified" sections below was measured on **2026-07-03** against an actual clean install of `appworld==0.2.0` in a Python 3.11.15 venv on this Mac (`env_candidates/appworld` is the shallow clone; a throwaway install + data download was used to measure disk/counts and run one task end-to-end). Leaderboard/baseline numbers come from web research and are tagged with sources.

---

## 0. TL;DR — decision-relevant facts

1. **Python `>=3.11` is a hard floor** (supports 3.11–3.14). The China server's **system Python 3.8 will not work** — must create a `conda create -n appworld python=3.11` env. Verified working on 3.11.15.
2. **Data = a single 33 MB S3 bundle**, NOT GitHub: `https://s3.us-west-2.amazonaws.com/appworld.dev/data-0.2.0.bundle` (verified reachable from Mac; S3-us-west-2 is usually reachable from China, unlike GitHub). Unpacks to **183 MB** on disk under `$APPWORLD_ROOT/data/`. Plus ~2 MB in `~/.appworld/tests`.
3. **The encrypted app/test code ships inside the pip wheel** — `pip install appworld` (works via China PyPI mirror) + `appworld install` needs no network and no GitHub. Only the *data* needs shipping. So the no-GitHub constraint is a non-issue.
4. **Concurrency is the one real integration risk.** Default "unified" (serverless, in-process) mode allows **exactly one task-world per OS process at a time** — `freezegun` mocks wall-clock time process-wide, so **threads/async in a single process are NOT supported** in unified mode. Our harness's model (up to 320 concurrent tasks, thread-parallel, one Python process) is **incompatible with unified mode** and requires **decoupled mode** (N `appworld serve environment` server processes, client threads each talk to a distinct server). See §7 — this drives the integration design.
5. **LLM side is trivial.** Reference agents use `litellm`/`openai` with configurable `base_url` + `api_key`; our vLLM-served qwen (OpenAI-compatible) plugs straight in. We only need the `appworld` **core** package + our own harness; the `appworld-agents` (experiments) package is optional.
6. **Splits (verified counts):** train 90 / dev 57 / test_normal 168 / test_challenge 417 tasks (732 total; 244 scenarios). Metrics: **TGC** (per-task) and **SGC** (per-scenario, all ~3 variants must pass).
7. **Gold solutions are accessible to the harness but not the agent:** `world.task.ground_truth.compiled_solution_code` exists for train/dev only (`ground_truth_mode="full"`). Fits our GT-firewall design.
8. **License: Apache-2.0** (public code) + Apache-2.0-with-encrypted-redistribution (the data/app bundle). Fine for research; do not re-publish decrypted bundle content.
9. **Effort estimate:** ~1 day for a single-process adapter; **2–4 days** including the decoupled server-pool plumbing + `appworld verify tasks` validation to hit our concurrency target.

---

## 1. Python version & environment

- **`requires-python = ">=3.11"`** (`pyproject.toml`). Classifiers list 3.11, 3.12, 3.13, 3.14. Ruff target `py311`.
- Verified: clean install and end-to-end task run on **CPython 3.11.15**.
- **Server action required:** system Python 3.8 is too old. Use conda:
  ```bash
  conda create -n appworld python=3.11 -y && conda activate appworld
  ```
- Heavy-ish but standard dependency set (no exotic native builds): `fastapi`, `uvicorn`, `sqlmodel`, `sqlalchemy-utils`, `pydantic>=2.12`, `ipython` (the execution shell), `libcst` (safety AST checks), `freezegun` (time mocking — **note this for concurrency**), `cryptography` (bundle decryption), `pendulum`, `faker`, `uvloop`. All available on PyPI / China mirrors.
- Optional extra: `appworld[mcp]` adds an MCP server/client (`mcp>=1.19`). Not needed for our harness.

---

## 2. License

Two-tier, both permissive (from README "License" section):
- **Public portion** (execution shell, eval utils, baselines, guides, tests scaffolding): **Apache-2.0**, plain text in the repo.
- **Protected portion** (API docs & implementations, task solutions & evaluation tests, task data): **Apache-2.0 with one extra clause** — any *public redistribution* of it (or derivatives) must stay in encrypted form. **"Training language models and serving their outputs do not constitute redistribution."**
- Practical rule for us: fine to use for research and to train/evaluate on; **do not post decrypted bundle content** (code/data extracted from `.bundle`) online. A canary string (`appworld:d17ac3f:...`) is embedded to detect leakage.

---

## 3. Installation & data — and how to ship to the China server

### 3.1 Standard install (what we verified)
```bash
pip install appworld            # from PyPI (or China mirror). Pulls fastapi/sqlmodel/ipython/etc.
appworld install                # unpacks encrypted .bundle code — NO network, NO GitHub needed
appworld download data          # fetches the 33 MB S3 data bundle
appworld verify tasks           # optional: runs gold solutions on train/dev to self-check (~2-3 min)
```
- `appworld install` unpacks:
  - app implementations → `site-packages/appworld/` (measured **3.4 MB**),
  - evaluation tests → **`~/.appworld/tests`** (measured **2.0 MB**). ← note this home-dir write.
  - If installed from the git repo instead of the wheel, use `appworld install --repo` (and `git lfs pull` first, since `.bundle` files are LFS-tracked).
- If the `appworld` console script isn't on PATH, `python -m appworld.cli ...` is equivalent.

### 3.2 Where the data comes from (verified)
- Source (hard-coded in `src/appworld/common/constants.py` + `download.py`):
  `S3_BASE_URL = https://s3.us-west-2.amazonaws.com/appworld.dev/`, `DATA_VERSION = "0.2.0"`.
  → **`https://s3.us-west-2.amazonaws.com/appworld.dev/data-0.2.0.bundle`**
  - Verified `HTTP 200`, **Content-Length = 34,908,601 bytes (≈33 MB)**, `Last-Modified 2025-10-14`.
- It is a **plain public S3 object** (AWS us-west-2). No GitHub, no auth. China servers can usually reach S3 directly (slower than a mirror but functional). The `appworld download data` command downloads + decrypts + unpacks it in one step (took ~15 s here).
- **`experiment-outputs-0.1.3.bundle`** (baseline agents' rollout logs, optional) is ~171 MB on the same bucket (`appworld download experiment-outputs`).

### 3.3 On-disk footprint (verified `du`)
`$APPWORLD_ROOT/data/` = **183 MB total**:

| subdir | size | contents |
|---|---|---|
| `base_dbs/` | 129 MB | 12 app SQLite DBs (the shared ~100-person simulated world) |
| `tasks/` | 50 MB | 732 task dirs; each stores only the **DB diff** vs base + specs + ground truth |
| `api_docs/` | 4.5 MB | API docs in `standard/`, `function_calling/`, `openapi/` formats |
| `datasets/` | 20 KB | `train.txt` / `dev.txt` / `test_normal.txt` / `test_challenge.txt` (task-id lists) |

Plus `~/.appworld/tests` (2 MB) and unpacked package code (3.4 MB). **Budget ~200 MB per checkout.**

### 3.4 Recommended shipping plan (no GitHub on server)
Cleanest path — the code goes via PyPI, only data is copied:
1. On server: `conda create -n appworld python=3.11 && pip install appworld` (China PyPI mirror), then `appworld install` (offline).
2. Get the data onto the server, either:
   - **(a) direct**: run `appworld download data` on the server (try S3 first — likely works), **or**
   - **(b) rsync**: `appworld download data` on the Mac, then `rsync -a $APPWORLD_ROOT/data/ server:/path/appworld_root/data/`. Also copy `~/.appworld/tests` if you'll run `appworld verify`/evaluation. The `data/` dir is fully relocatable; point to it with `APPWORLD_ROOT` (env var, `--root` flag, or `appworld.update_root()`).
3. If PyPI is also blocked for some dep, ship the code as a **git bundle of `env_candidates/appworld` including LFS objects** (`git lfs fetch --all` before bundling) and `pip install -e .` + `appworld install --repo`. Otherwise the pip path is simpler.

- **`APPWORLD_ROOT`** defaults to the current directory; the package reads/writes `./data` and `./experiments/outputs` relative to it. Override via `APPWORLD_ROOT=...`, `--root`, `.env`, or `from appworld import update_root; update_root(path)`.

---

## 4. Programmatic API — running one task end-to-end

Core objects: `from appworld import AppWorld, load_task_ids`.

```python
from appworld import AppWorld, load_task_ids

# 1) task ids for a split
task_ids = load_task_ids("train")   # "train" | "dev" | "test_normal" | "test_challenge"

# 2) open a world (context manager auto-closes and frees resources)
with AppWorld(
    task_id=task_ids[0],
    experiment_name="my_exp",        # where logs/state go: ./experiments/outputs/my_exp/tasks/<task_id>/
    ground_truth_mode="minimal",     # "minimal" (default; test-safe) | "full" (train/dev only; exposes gold)
    max_interactions=1000,           # hard cap on execute() calls per task
    # remote_environment_url=...     # set -> decoupled mode (see §7)
) as world:

    # 3) what the agent may see (agent designer chooses how much to put in the prompt)
    instr      = world.task.instruction          # str
    supervisor = world.task.supervisor           # {first_name,last_name,email,phone_number}
    app_desc   = world.task.app_descriptions     # {app_name: description}
    api_docs   = world.task.api_docs             # ApiDocCollection (reformattable: .function_calling(), .openapi(), compress_parameters(), ...)

    # 4) the interactive loop: agent writes Python, env executes it (stateful IPython shell)
    for _ in range(MAX_STEPS):
        code   = agent.next_code_block(output)   # your agent (LLM) produces a code string
        output = world.execute(code)             # -> str: printed stdout, or a traceback on error
        if world.task_completed():               # True once agent calls apis.supervisor.complete_task(...)
            break

    # 5) evaluate (state-based unit tests over the DB)
    tracker = world.evaluate()                   # -> TestTracker
    print(tracker.success)                       # bool: all requirements for THIS task passed
```

Key methods/attributes (from `src/appworld/environment.py`):
- **`world.execute(code: str) -> str`** — runs `code` in a persistent IPython kernel; variables persist across calls (like a notebook, e.g. reuse an `access_token` from an earlier login). On error returns the traceback string instead of raising. Two built-in ways to call app APIs from inside: the **functional** form `apis.spotify.show_song(song_id=1)` and the **REST** form via a `requester` object. Std-lib is allowed except destructive modules (guarded by a libcst AST safety check + a runtime guard; both toggleable via `raise_on_unsafe_syntax` / `raise_on_unsafe_execution`).
- **`world.task_completed() -> bool`** — True when the agent has called the Supervisor app's `complete_task` API from within the shell (i.e., the agent self-declares done).
- **`world.evaluate(suppress_errors=True) -> TestTracker`** — runs the hidden per-task unit tests against final DB state; `.success` is the boolean. Available for all splits (test sets ship evaluation code, just not setup/solutions).
- **`world.close()`** — frees resources; unsets the mocked datetime, clears DB cache. Context manager does this automatically.
- `AppWorld.init_defaults(...)` — set defaults (e.g. `experiment_name`) once instead of per call.
- First task load ≈ **3 s** (loads all apps into the process = "starting the server in-process"); subsequent loads <0.5 s.

**What the agent "sees":** typically the instruction + supervisor identity + onboarding prompt explaining it operates a Python REPL and must discover APIs via three meta-APIs — `apis.api_docs.show_app_descriptions()`, `show_api_descriptions(app_name=...)`, `show_api_doc(app_name=..., api_name=...)`. The designer chooses whether to pre-load API docs into the prompt or let the agent retrieve them on demand. (See `notebooks/minimal_agent.ipynb` for a full ReAct example and `PROMPT_TEMPLATE`.)

---

## 5. Evaluation & metrics

- **TGC — Task Goal Completion:** % of tasks where *all* the task's state-based unit tests pass (checks the goal was achieved *and* no collateral DB damage). Per-task.
- **SGC — Scenario Goal Completion:** % of *scenarios* where **every** task variant (usually 3 rephrasings/parameterizations of the same underlying task) passes. Strictly harder than TGC; measures robustness/consistency. To compute SGC you must run all variants of a scenario.
- Both are reported **aggregate and broken down by difficulty level (1/2/3)**.
- CLI: `appworld evaluate <experiment_name> <dataset_name>` → writes
  `./experiments/outputs/<exp>/evaluations/<dataset>.{txt,json}`. The JSON has `aggregate` (`tgc`, `sgc`, `num_tasks`, `num_scenarios`) and per-task `individual` entries listing passed/failed requirements (each with a NL description and a `no_op_pass`/`no_op_fail` label so you can tell which tests a do-nothing agent would trivially pass).
- Programmatic: `world.evaluate().success` per task; aggregate SGC/TGC via the CLI or the `evaluator` module over a finished experiment dir.

---

## 6. Ground truth / gold solutions (firewall-friendly)

- Load with `ground_truth_mode="full"` (train/dev only) to expose `world.task.ground_truth`:
  - `.compiled_solution_code` — the reference solution; run it via `world.execute(gt.compiled_solution_code + "\nsolution(apis, requester)")`. **Verified**: running it flips `task_completed()` to True and `evaluate().success` to True.
  - `.answer`, `.required_apps`, `.required_apis`, `.evaluation_code`, `.api_calls`.
  - `.metadata` (available for ALL splits, including test): `difficulty`, `num_apis`, `num_apps`, `num_solution_code_lines`. Per the README these test-set difficulty indicators are for *post-hoc analysis only* — do not feed them to the agent on test tasks.
- **Test sanctity:** `test_normal` / `test_challenge` ship **evaluation code only** — no setup programs, no gold solutions. You can score at home but can't see how the initial state was built or the intended solution. train/dev have everything.
- This matches our **GT-firewall** design: the harness/optimizer may read gold (train/dev) for supervision, but must not route it into the agent's context. Fits the "optimizer can see gt but outputs must not depend on gt" invariant.

---

## 7. Concurrency model — **read before designing the harness integration**

Two modes (from `guides/parallelizing_worlds.md`, the authoritative doc):

### Unified mode (default, serverless, in-process)
- Client + FastAPI server share one process via FastAPI `TestClient` (no sockets, no server to manage). Fastest per-task.
- **Hard constraint: one world per process at a time.** Opening a second `AppWorld(...)` while one is live corrupts state, because **(i) `freezegun` mocks the current time *process-wide* per task**, and (ii) DB-connection/memory management assumes a single active world. *(We observed the process-wide time mock directly: wall-clock `time.time()` jumps inside the `with AppWorld(...)` block.)*
- **Consequence: threading and asyncio do NOT give you parallel worlds in unified mode.** The guide states this explicitly. The only in-process parallelism is **`multiprocessing`** — one world per child process (`multiprocessing.Pool`, `num_processes`).

### Decoupled mode (client ↔ separate server processes)
- Start N environment servers, each in its own process holding its own world:
  `appworld serve environment --port 8000` (repeat per port), or **auto-managed**:
  ```python
  config = {"experiment_name": "exp", "ground_truth_mode": "full",
            "remote_environment_url": ["http://localhost:{port}"] * N}
  with AppWorld.initializer(start_servers=True, **config) as initializer:
      # initializer.configs -> N ready configs, {port} auto-filled with free ports,
      # servers started on enter and stopped on exit.
      ...
  ```
- With servers external, the **single client process CAN use threads / asyncio / processes** to drive the N servers concurrently, in batches of N (the guide gives full `ThreadPoolExecutor`, `asyncio`, and `multiprocessing` examples). `parallelizable_across` becomes `"batch"` (vs `"all"` in unified mode).
- **No SQLite lock contention:** each world uses **in-memory** DBs private to its process (`output_db_home_path_in_memory`); isolation is by process, not by file lock. So parallel tasks don't fight over `data/*.db`.

### Implication for OUR harness (up to 320 concurrent tasks, thread-parallel, single Python process)
- This pattern **cannot use unified mode.** In one process, only one world can be live, so 320 threads would clobber each other's time mock and DB state.
- **Required design: decoupled mode with a pool of environment-server processes.** The client (our harness, threads) dispatches each concurrent task to a distinct `remote_environment_url`. Degree of true parallelism = number of server processes.
  - For 320-way concurrency that's up to **320 `uvicorn` server processes** (each ~a few hundred MB RAM once apps load). On a big server this is feasible but heavy — size RAM accordingly, or cap concurrency to what the box supports and let the harness batch.
  - `AppWorld.initializer(start_servers=True, remote_environment_url=["http://localhost:{port}"]*N)` handles port-finding and start/stop, so the plumbing is a few lines; the cost is process/RAM overhead, not code.
- **Validate before trusting it:** the guide and CI both run `appworld verify tasks --remote-environment-url ...` (runs gold solutions through the exact parallel setup and asserts they pass). Do this on the server at the target concurrency before running real rollouts.
- Alternative if 320 servers is too heavy: keep our harness thread-parallel for the *LLM calls*, but execute AppWorld tasks through a bounded `multiprocessing` pool (unified mode, one world per worker process). Trades our single-process assumption for lower memory. Decide based on the server's RAM.

---

## 8. Splits, difficulty & task statistics (verified)

| split | tasks | scenarios | ground truth available |
|---|---|---|---|
| `train` | 90 | 30 | full (setup + solution + eval) |
| `dev` | 57 | 19 | full |
| `test_normal` | 168 | 56 | eval only |
| `test_challenge` | 417 | 139 | eval only |
| **total** | **732** | **244** | — |

- Task id format: `<scenario_hash>_<variant>`, ~3 variants per scenario (SGC = all variants of a scenario pass).
- **Difficulty** is level 1/2/3 (binned by solution length/complexity). Verified distribution: train = 36/36/18 (L1/L2/L3), dev = 30/24/3. Test sets skew harder (test_challenge is the largest and hardest, requiring cross-app, longer-horizon solutions).
- **Complexity (verified from train ground truth):** distinct **APIs per task** median 8 (range 5–12); **apps per task** median 1 (harder tasks span several); **gold solution length** median 37 lines (range 18–86). Agents typically take *more* interaction turns than solution lines because they spend steps exploring API docs and reacting to errors. `max_interactions` defaults to **1000** (a safety cap, not a typical length).
- Environment scale: **9 user-facing apps** (Amazon, Spotify, Venmo, Splitwise, Gmail, Phone, SimpleNote, Todoist, FileSystem) + system apps (Supervisor, ApiDocs, Admin), **457 APIs**, ~100 simulated people, 100+ DB tables.

---

## 9. Leaderboard & baselines (web research, mid-2026)

Metric legend: **TN** = Test-Normal, **TC** = Test-Challenge; each cell **TGC / SGC**. Always tag split+metric — a method's TC-TGC is often ~20 pts below its TN-TGC. Sources: [appworld.dev/leaderboard](https://appworld.dev/leaderboard) (+ `leaderboard.json`), and the papers cited below. The public leaderboard was mid-update at time of research; treat 2026 entries as provisional.

| method | base model | TN TGC/SGC | TC TGC/SGC | source |
|---|---|---|---|---|
| **AgentRL** | Qwen3-14B | 86.9 / 80.4 | 67.6 / 50.4 | leaderboard (2026, provisional) |
| IBM CUGA | GPT-4.1 | 73.2 / 62.5 | 57.6 / 48.2 | leaderboard |
| LOOP (RL) | Qwen2.5-32B | 72.6 / 53.6 | 47.2 / 28.8 | [arXiv 2502.01600](https://arxiv.org/abs/2502.01600) |
| ReAct + 2 demos | GPT-4o | 68.5 / 57.1 | 38.9 / 23.0 | leaderboard |
| **ReAct (orig. paper baseline)** | GPT-4o | 48.8 / 32.1 | 30.2 / 13.0 | [arXiv 2407.18901](https://arxiv.org/abs/2407.18901) |

- **Original paper (GPT-4o ReAct)** was the strongest scaffold at release: **TN 48.8/32.1, TC 30.2/13.0**. An "oracle API selection" upper reference reached TN 54.8/35.7, TC 35.2/20.1. **No human baseline** is reported (LLM-agent-only benchmark).
- **Open ~30B (Qwen-class):** base **Qwen2.5-32B** prompted ≈ **39 TN-TGC**; with RL, **LOOP → ~71–72.6 TN-TGC** (beats OpenAI o1's ~61.9 TN-TGC by ~9 pts). SALT (GRPO+SALT, Qwen2.5-32B): TN 66.2/47.9, TC 36.8/20.9 ([arXiv 2510.20022](https://arxiv.org/abs/2510.20022)). These are the most relevant anchors for our qwen-served harness.
- **ACE — "Agentic Context Engineering"** ([arXiv 2510.04618](https://arxiv.org/abs/2510.04618)), built on ReAct with **DeepSeek-V3.1 (671B)** as generator/reflector/curator. AppWorld Table 1 (TN-TGC / TN-SGC / TC-TGC / TC-SGC):
  - ReAct base: 63.7 / 42.9 / 41.5 / 21.6
  - ReAct + GEPA (offline, GT labels): 64.9 / 44.6 / 46.0 / 30.2
  - ReAct + Dynamic Cheatsheet (online, no GT): 65.5 / 58.9 / 52.3 / 30.8
  - **ReAct + ACE (offline, GT): 76.2 / 64.3 / 57.3 / 39.6** ← headline; matches top-1 CUGA on avg with a smaller open model
  - **ReAct + ACE (online, no GT): 69.6 / 53.6 / 66.0 / 48.9** ← surpasses CUGA on Test-Challenge
  - ACE claims +10.6% avg over ReAct+GEPA and +7.6% over Dynamic Cheatsheet. *(Note: GEPA and Dynamic Cheatsheet's own papers don't evaluate on AppWorld — these are ACE's reproductions.)*
- **"Not All Skills Help: Measuring and Repairing Agent Knowledge"** ([arXiv 2606.15390](https://arxiv.org/abs/2606.15390)) — a skill-repair method on DeepSeek-V3 reports **69.3 TGC on Test-Challenge** (claimed SOTA on that split). Directly adjacent to our skill-search line of work.

---

## 10. Integration effort & recommendation

**Effort: ~2–4 engineer-days.**
- *~0.5 day* — env + data on the server (conda py3.11, pip install, `appworld install`, data via S3 or rsync, `appworld verify tasks` green).
- *~1 day* — thin adapter: `load_task_ids(split)` → per task `AppWorld(task_id, experiment_name=...)` → build agent prompt from `task.instruction` + `api_docs`/`app_descriptions` → loop `execute()` / `task_completed()` → `evaluate().success`; collect TGC/SGC. LLM wiring is basically free (OpenAI-compatible `base_url`+`api_key` → our vLLM qwen).
- *~1–2 days* — the **decoupled server-pool** for our thread-parallel harness (§7): stand up N `appworld serve environment` processes (or `AppWorld.initializer(start_servers=True, ...)`), map each concurrent slot to a `remote_environment_url`, tune N to server RAM, and re-run `appworld verify tasks --remote-environment-url ...` at target concurrency.

**Recommendation.** Use only the `appworld` **core** package + our existing harness/agent/LLM stack; skip `appworld-agents`. The single design decision that matters is the **unified-vs-decoupled** concurrency choice: our "single process, hundreds of threads" model forces **decoupled mode with a server pool**. Everything else (API surface, evaluation, gold-solution access, GT firewall, LLM endpoint) maps cleanly onto what we already have. AppWorld is also a strong fit thematically — several 2025-26 skill-learning / context-engineering papers (ACE, "Not All Skills Help", Dynamic Cheatsheet, GEPA-repro) benchmark on it, giving us directly comparable baselines for a skill-search method.

---

## Appendix — verified commands & file map

```bash
# clone (this repo) — shallow
git clone --depth 1 https://github.com/stonybrooknlp/appworld

# install + data (Python 3.11+)
pip install appworld && appworld install && appworld download data
python -m appworld.cli download data      # if `appworld` not on PATH

# self-check (runs gold solutions, ~2-3 min each)
appworld verify tests
appworld verify tasks
appworld verify tasks --remote-environment-url "http://0.0.0.0:8000"   # decoupled check

# explore / run reference agents (optional appworld-agents pkg)
appworld run auto --agent-name react --model-name <m> --dataset-name test_normal
appworld evaluate <experiment_name> <dataset_name>
```

Key source files (in `env_candidates/appworld/`):
- `src/appworld/environment.py` — `AppWorld` class, `execute`, `evaluate`, `task_completed`, `AppWorldInitializer` (parallelism).
- `src/appworld/task.py` — `Task`, `load_task_ids`, ground-truth access.
- `src/appworld/download.py` + `src/appworld/common/constants.py` — S3 URL (`https://s3.us-west-2.amazonaws.com/appworld.dev/`), `DATA_VERSION="0.2.0"`.
- `guides/parallelizing_worlds.md` — **the** concurrency reference (unified vs decoupled, thread/async/process examples).
- `notebooks/minimal_agent.ipynb` — full ReAct agent + prompt template.
- `experiments/code/simplified/language_model.py` — litellm/openai client with `base_url`/`api_key` (vLLM integration pattern).

Constants for reference: data bundle `data-0.2.0.bundle` = 34,908,601 bytes; unpacks to 183 MB (`data/`) + 2 MB (`~/.appworld/tests`).
