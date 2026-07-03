# τ²-bench Integration Prep

Prepared for integrating the Sierra τ²-bench / τ³-bench harness into our skill/policy-document
optimization research. All findings below are from reading the cloned source, not from memory.

- **Repo**: https://github.com/sierra-research/tau2-bench (cloned to `env_candidates/tau2-bench`)
- **Cloned commit**: `1901a301961cbbe3fd11f3e84a2a376530c759e3` (2026-07-01), package `tau2` **v1.0.0** (this
  release is branded **τ³-bench**: it is a superset of τ²-bench that adds voice + a knowledge/RAG domain).
- **License**: **MIT** (`LICENSE`, © 2025 Sierra Research). No usage restrictions for research.
- **Repo size**: ~858 MB working tree (mostly data; `telecom/tasks_voice.json` alone is 62 MB), `.git` ~80 MB.

---

## 1. Python, dependencies, install

- **Python required: `>=3.12, <3.14`** (`pyproject.toml`). The code uses 3.10+ runtime syntax
  (`list[dict]`, `X | Y` annotations evaluated at import). **System Python 3.8 on the server will NOT work** —
  create a **conda env with Python 3.12** (e.g. `conda create -n tau2 python=3.12`).
- **Build backend**: `hatchling`. Install with either `uv sync` (README's recommended path, needs `uv`) or
  plain **`pip install -e .`** (works because it's a standard PEP-517 project). For the offline China server,
  `pip install -e .` against a PyPI mirror is the simplest path.
- **Core runtime deps** (all pure-python or have manylinux wheels — fine via China PyPI mirror):
  `litellm>=1.80.15,<1.82.7`, `rich`, `tabulate`, `fastapi`, `uvicorn`, `pandas`, `psutil`, `loguru`,
  `docstring-parser`, `tenacity`, `deepdiff`, `addict`, `PyYAML`, `toml`, `python-dotenv`, `typer`,
  `requests`, `numpy`, `httpx`. **`litellm` is the entire LLM layer** — this is what makes a custom
  OpenAI-compatible endpoint trivial (see §4).
- **Optional extras (skip for our use)**: `voice` (pyaudio/portaudio, elevenlabs, aws/google SDKs — heavy,
  needs system libs), `knowledge` (`rank-bm25` + `openai`, only for `banking_knowledge`), `gym`
  (gymnasium RL wrapper), `dev`, `experiments`. **For text-mode airline/retail/telecom we only need the core
  install.**
- **Offline transfer**: no GitHub on the server → `git bundle create tau2.bundle --all` on the Mac, scp,
  `git clone tau2.bundle`. To shrink the bundle, the voice/audio JSONs are droppable
  (`*/tasks_voice.json`, `*/audio_difficulty.json`) if only text mode is used.

## 2. Offline capability after install

- **airline / retail / telecom / mock are 100% local/offline after install.** Every domain ships its DB
  (`db.json`/`db.toml`), policy markdown, and tasks JSON under `data/tau2/domains/<domain>/`. No network is
  touched except the LLM API calls, which we point at the local vLLM endpoint.
- **`banking_knowledge` is NOT fully offline** by default: its RAG configs call out to OpenAI/OpenRouter
  embedding APIs (`OPENROUTER_API_KEY` in `.env.example`). Avoid this domain, or wire a local embedder — not
  needed for our policy-optimization work.
- **Data dir is relocatable**: `TAU2_DATA_DIR` env var overrides the data root
  (`src/tau2/utils/utils.py`). Useful for pointing workers at a modified copy of the domain data (see §9).

## 3. Domains, task counts, task anatomy, evaluation

### Domains & task counts (text mode, `base` split is the default and matches original τ-bench)

| Domain | Total (`base`) | train | test | Notes |
|---|---|---|---|---|
| `airline` | 50 | 30 | 20 | single-control (only agent has tools) |
| `retail` | 114 | 74 | 40 | single-control |
| `telecom` | 114 | 74 | 40 | **dual-control**: the *user simulator* also has tools (a mock phone) |
| `telecom_full` | 2285 | — | — | full combinatorial set (15 subtask groups → 2285 combos); `telecom_small`=20 |
| `mock` | 10 | — | — | tiny smoke-test domain |
| `banking_knowledge` | 97 | — | — | RAG domain, needs embeddings (skip) |

`telecom` also has a `telecom-workflow` variant (same tasks, different policy doc). Splits live in
`data/tau2/domains/<domain>/split_tasks.json`.

### What a task is (`src/tau2/data_model/tasks.py`, `Task`)

A task JSON contains:
- `user_scenario` → `{persona, instructions{reason_for_call, known_info, unknown_info, task_instructions}}`.
  This is the **only** thing given to the **user simulator** (as its system prompt scenario).
- `evaluation_criteria` → `{actions, env_assertions, communicate_info, nl_assertions, reward_basis}`.
- `initial_state` (optional) → env DB overrides, setup actions, and/or a pre-seeded message history.
- `ticket` (optional, for the solo-agent variant), `user_tools` (which user tools are enabled), `id`,
  `description`.

The **agent never sees** `evaluation_criteria`. It only sees the **policy document** + tool schemas +
the live conversation (see §9).

### Evaluation / reward (`src/tau2/evaluator/evaluator.py`)

- Final reward = **product** of the components listed in the task's `reward_basis`. Default basis is
  **`[DB, COMMUNICATE]`** (matches original τ-bench); airline/retail/telecom use this.
- **DB check**: the task's reference `actions` are **replayed on a fresh env** to derive a target DB hash;
  the agent's resulting DB state hash is compared. **Any agent trajectory that reaches an equivalent end
  state passes** — the reference actions are not a per-call requirement (unless `RewardType.ACTION` is in the
  basis, which only a few `banking_knowledge` tasks use).
- **COMMUNICATE**: each `communicate_info` string must appear (substring) in the agent's messages.
- **NL_ASSERTION**: LLM-judged (experimental/WIP); needs an assertions LLM.
- **Premature termination** (max_steps / max_errors / crash, i.e. not a clean `AGENT_STOP`/`USER_STOP`) →
  **reward 0**.
- **`pass^k` metric** (`src/tau2/metrics/agent_metrics.py`): `pass^k = C(successes, k) / C(trials, k)` over
  `num_trials` temperature-0 runs. **`pass^1` = plain success rate.** Higher k measures reliability
  (probability *all* k independent runs of a task succeed). `compute_metrics()` prints `avg_reward` +
  `pass^k` for all k up to `num_trials`.

## 4. Model configuration — pointing BOTH agent and user at the vLLM qwen endpoint

**This is the key integration fact and it is clean.** Every LLM call (agent, user simulator, and any
NL-assertion judge) funnels through one function: `generate(model, messages, tools, **kwargs)` in
`src/tau2/utils/llm_utils.py`, which calls **`litellm.completion(model=model, messages=..., tools=...,
**kwargs)`** directly. `self.llm_args` is spread straight into that call for both the agent
(`LLMAgent._generate_next_message`) and the user (`UserSimulator._generate_next_message`).

Because it's litellm, a custom OpenAI-compatible endpoint needs **no code changes** — just the right model
string + `api_base`/`api_key` in the args dict:

- **Model string**: `openai/<served-model-name>` (litellm's OpenAI-compatible provider prefix). For our
  endpoint that's `openai/qwen3.6-35b-a3b`. (`hosted_vllm/<name>` also works.)
- **Endpoint + key**: pass `api_base` and `api_key` inside the **llm-args dict** (they are just litellm
  `completion` kwargs). Alternatively set env vars `OPENAI_API_BASE` / `OPENAI_API_KEY`.
- Both agent and user have **independent** model + args, so they can point at the same vLLM endpoint (or
  different ones).
- `litellm.drop_params = True` is set globally, so params the server doesn't support are dropped instead of
  erroring. `temperature` defaults to `0.0` for both.

**CLI form** (args are parsed with `type=json.loads`, so pass a JSON string):

```bash
tau2 run --domain airline \
  --agent-llm "openai/qwen3.6-35b-a3b" \
  --agent-llm-args '{"temperature":0.0,"api_base":"http://10.77.110.162:8888/v1","api_key":"token-abc123"}' \
  --user-llm  "openai/qwen3.6-35b-a3b" \
  --user-llm-args  '{"temperature":0.0,"api_base":"http://10.77.110.162:8888/v1","api_key":"token-abc123"}' \
  --num-trials 1 --num-tasks 5 --max-concurrency 20 --seed 300
```

> **CRITICAL server-side prerequisite**: τ²-bench relies entirely on **OpenAI-style tool/function calling**
> (the agent must emit `tool_calls`; in **telecom the user simulator also makes tool calls**). The vLLM
> server MUST be launched with tool calling enabled, e.g.
> `vllm serve <qwen> --enable-auto-tool-choice --tool-call-parser hermes` (qwen3 uses the hermes-style
> parser). If tool calling isn't enabled, every agent turn fails and all rewards are 0. Verify this first.
>
> **Cost tracking caveat**: `completion_cost()` has no price entry for a custom qwen model, so per-call
> `cost` will log an error and record `0.0` (handled gracefully — does not break runs). **Token usage is
> still tracked** from the response `usage` field, so use tokens (not cost) for accounting.

## 5. Policy document location + how the agent prompt is composed (the injection point)

**Where the policy lives on disk:**
- airline: `data/tau2/domains/airline/policy.md` (~7.7 KB)
- retail: `data/tau2/domains/retail/policy.md` (~6.7 KB)
- telecom: `data/tau2/domains/telecom/main_policy.md` (+ `tech_support_manual.md`; `telecom-workflow` uses
  `tech_support_workflow.md`)
- mock: `data/tau2/domains/mock/policy.md`

**How it enters the agent prompt** (`src/tau2/agent/llm_agent.py`):
each domain's `get_environment()` reads its policy file into a string and passes it to
`Environment(policy=...)`. `build_agent()` calls `environment.get_policy()` and hands it to the agent, which
bakes it verbatim into the **system prompt**:

```
<instructions>
{agent_instruction}      # fixed 4-line "you are a customer service agent…" preamble
</instructions>
<policy>
{domain_policy}          # <-- the ENTIRE policy.md goes here, unmodified
</policy>
```

The policy string is the **only** domain-knowledge the agent gets. This is exactly the artifact our research
optimizes, and it is a single clean seam.

**Three ways to inject a custom policy per run:**
1. **Programmatic override (cleanest, recommended)** — build the env, overwrite `env.policy`, then build the
   agent (see §6). `environment.policy` is a plain attribute; `get_policy()` just returns it.
2. **Register a custom domain** in the registry whose `get_environment` returns an env with your policy —
   lets you keep the batch runner (concurrency, checkpointing) by passing `--domain <your_name>`.
3. **File / `TAU2_DATA_DIR` override** — point workers at a data dir whose `policy.md` you've replaced.
   Heaviest (data copy) but works with the stock CLI unchanged.

> Scoring is **policy-independent**: the evaluator rebuilds its own env from the registry and scores on DB
> state + `communicate_info` from the *task*, not the policy text (`run_simulation` even records the actual
> policy used separately). So overriding `env.policy` changes **only** the agent's behavior, never the
> reward criteria — exactly what we want for a fair policy-optimization loop.

## 6. Programmatic API — run one task and get the reward, with policy injection

The lowest-level entry point is `run_simulation(orchestrator)` (`src/tau2/runner/simulation.py`), which runs
the agent↔user loop **and evaluates**, returning `SimulationRun` with `.reward_info.reward`.

```python
from tau2.registry import registry
from tau2.runner import build_agent, build_user, run_simulation
from tau2.orchestrator.orchestrator import Orchestrator
from tau2.run import get_tasks

QWEN = "openai/qwen3.6-35b-a3b"
ARGS = {"temperature": 0.0,
        "api_base": "http://10.77.110.162:8888/v1",
        "api_key": "token-abc123"}

# 1. Build the environment and INJECT the custom policy
env = registry.get_env_constructor("airline")()      # airline get_environment()
env.policy = my_custom_policy_text                    # <-- policy injection seam

# 2. Pick a task
task = get_tasks("airline", task_ids=["0"])[0]        # or get_tasks("airline")[i]

# 3. Build agent + user, both on the vLLM endpoint
agent = build_agent("llm_agent", env, llm=QWEN, llm_args=ARGS, task=task)
user  = build_user("user_simulator", env, task, llm=QWEN, llm_args=ARGS)

# 4. Wire the orchestrator and run + evaluate in one call
orch = Orchestrator(domain="airline", agent=agent, user=user, environment=env,
                    task=task, max_steps=200, max_errors=10, seed=42)
sim = run_simulation(orch)                            # EvaluationType.ALL by default
print(sim.reward_info.reward)                         # 0.0 or 1.0 (product of DB & COMMUNICATE)
```

For **batch runs** (concurrency + checkpoint/resume + metrics), use the config-driven path instead:
`run_domain(TextRunConfig(domain=..., agent="llm_agent", llm_agent=QWEN, llm_args_agent=ARGS,
llm_user=QWEN, llm_args_user=ARGS, num_trials=..., max_concurrency=...))` from `tau2.run` — but that path
re-reads the on-disk policy, so for custom policies register a custom domain (injection method 2) or set
`TAU2_DATA_DIR`.

## 7. Determinism / seeding of the user simulator

- Both agent and user default to **temperature 0.0**.
- The batch runner seeds `random` with `config.seed` (default **300**) and derives one seed per trial;
  `set_seed()` writes that into `llm_args["seed"]`, which litellm forwards to the endpoint. **vLLM honors
  `seed`**, so runs are reproducible up to server-side nondeterminism (batching/kernels can still cause minor
  drift). For `num_trials>1`, each trial uses a distinct seed to sample independent rollouts for `pass^k`.
- **Residual variance is real** and comes mostly from the **LLM user simulator** (it's another stochastic
  model). Sierra deliberately reduced this in telecom by tightly coupling the user to environment tools (see
  §8). Expect to run **multiple trials** and compare with paired seeds when measuring policy improvements.

## 8. Concurrency

- Built-in **thread-pool** parallelism: `run_tasks` uses
  `ThreadPoolExecutor(max_workers=config.max_concurrency)` (`src/tau2/runner/batch.py`); each task rollout
  runs on a worker thread with its own asyncio loop. CLI flag `--max-concurrency` (default 3, set it to
  tens–hundreds for our throughput). Since work is I/O-bound on the LLM endpoint, threads scale well; the
  real limiter is the vLLM server's concurrent-request capacity.
- litellm is configured with an httpx connection pool capped at `max_connections=10` /
  `max_keepalive=5` **per process** (`llm_utils.py`) — if we push very high concurrency from one process,
  raise those limits or shard across processes.
- Built-in **checkpoint/resume** (`--auto-resume`) and graceful Ctrl-C (finished sims are already persisted).

## 9. Typical turns per task & token cost (estimate — measurable via tracked usage)

- **Turns**: typically ~10–30 agent/user turns per task (~20–60 messages incl. tool calls), hard-capped at
  `--max-steps` (default **200**). Telecom runs longer (dual-control coordination).
- **Tokens**: order-of-magnitude **~150k–300k total tokens per task** summed across all agent+user calls,
  dominated by the **re-sent conversation history + policy + tool schemas on every turn**. Directly relevant
  to us: the policy (~2k tokens) is resent on **every** agent turn, so a larger optimized policy raises cost
  roughly linearly in turns — worth keeping policies tight. Exact usage is available per run via
  `get_token_usage()` / the `usage` on each message; measure empirically once the endpoint is live.

## 10. Status: leaderboard numbers (pass^1) & known variance

Frontier (current taubench.com / llm-stats leaderboard, `base` split, pass^1):
- **telecom**: Claude Opus 4.6 **0.993**, GPT-5.4 **0.989**, LongCat-Flash-Thinking-2601 0.993.
- **retail**: Claude Opus 4.6 **0.919**, GPT-5.2 ~**0.82**.

Original τ²-bench paper (arXiv 2506.07982), pass^1 for **GPT-4.1**:
- retail ≈ **0.74**, telecom ≈ **0.34** (dual-control causes up to a ~40-point drop vs single-control);
  airline intermediate. Eval uses *k* temperature-0 independent runs and reports pass^k.

**Open-weight models (relevant — our endpoint serves a Qwen a3b-class model), telecom pass^1:**
- Qwen3-235B-A22B-Thinking **0.456**, Qwen3-Next-80B-A3B-**Thinking 0.439**,
  Qwen3-Next-80B-A3B-**Instruct 0.132**, Nemotron-3-Super-120B 0.644.
- **Implication**: an a3b-active Qwen (like `qwen3.6-35b-a3b`) will likely score **low on telecom**
  (roughly 0.13–0.45 depending on reasoning mode) and **higher on airline/retail**. This is **large
  headroom** for policy-document optimization — a good property for our study. Start on airline/retail
  (single-control, cheaper, more stable), then telecom for the harder dual-control signal.

**Known variance issue**: LLM user simulators are the main noise source; τ²-bench's own measurement is a
**16% total / 6% critical** user-sim error rate in telecom (vs 40–47% in prior single-control benchmarks).
Non-zero, so use paired seeds + multiple trials when attributing gains to a policy change.

## 11. Integration effort & risks

- **Effort: low.** No code changes needed to use our vLLM qwen for both agent and user — it's pure config
  (model string + `api_base`/`api_key`). Policy injection is a one-line `env.policy = ...` override on a
  clean seam. Data is local. Concurrency, checkpointing, and pass^k are built in.
- **Risks / must-verify**: (1) Python 3.12 via conda (server's 3.8 is incompatible). (2) vLLM must be
  launched with tool-calling enabled or everything scores 0. (3) cost=0 for custom models — use token counts.
  (4) skip `banking_knowledge` (needs external embeddings). (5) user-sim variance → multi-trial + paired
  seeds.
