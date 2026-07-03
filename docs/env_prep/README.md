# Candidate Environment Onboarding — Master Deployment Plan

Prepared 2026-07-03. Five benchmark environments were deep-audited for integration
into the CSS harness (per-env details in `*_PREP.md`, all facts empirically
verified on a Mac dev machine; repo clones live in `env_candidates/`, gitignored).

Target: Linux server 10.77.110.127 — Ubuntu 20.04, **Python 3.8.10 system, no
conda**, Java 17, 80 cores, 251 GB RAM (131 GB available), **149 GB free disk
(98% full — clean before big data)**. Network: PyPI direct + Tsinghua/Aliyun
mirrors + repo.anaconda.com all reachable; **GitHub and huggingface.co blocked**
(use git bundles from the Mac; HF via hf-mirror.com).

## Cross-environment decision table

| | AppWorld | ALFWorld | τ²-bench | WebShop | ScienceWorld |
|---|---|---|---|---|---|
| Task pool (train-ish/eval) | 90+57 / 168+417 | 3,553 / 134 unseen | 178 / 100 (+telecom_full 2,285) | 12,087 sessions (test=first 500) | 7,207 variations, 50/25/25 in-JAR |
| Scoring | state unit-tests (TGC/SGC) | goal check (`won`) | DB-state + info checks, pass^k | reward∈[0,1] at Buy, SR=1.0 | progress 0-100 + success |
| GT firewall hook | `ground_truth.compiled_solution_code` (train/dev) | oracle expert / task type | evaluation_criteria (hidden from agent) | goal attrs internal | `get_gold_action_sequence()` |
| Python | **>=3.11** | >=3.9 (TextWorld 1.7) | **>=3.12** | 3.8-3.10 era code | any (pure wheel + py4j) |
| Java | no | no | no | JDK for pyserini — **droppable via rank_bm25** | **yes (17 OK on server)** |
| Data | 33 MB S3 bundle → 183 MB disk | 137 MB zips → 2.0 GB (GitHub releases; already on Mac `~/.cache/alfworld`) | bundled in repo (~858 MB tree, shrinkable) | 5.67 GB via hf-mirror (`quanwei0/webshop-minimal`); 1k/100k subsets bundled in webshop-minimal repo | **bundled in pip wheel (7.8 MB JAR)** |
| Concurrency model | **decoupled mode: N env-server procs** (unified = 1 world/process, freezegun) | **NOT thread-safe** → subprocess env pool (verified) or global lock | built-in ThreadPoolExecutor (+raise litellm httpx pool) | share 1 SimServer + per-thread env; pure-BM25 searcher shareable | 1 JVM proc per env (~200-300 MB), verified isolated |
| RAM @100 conc. | ~tens of GB (uvicorn procs) | 10-28 GB @320 procs | LLM-bound, negligible | 6-8 GB shared dict once | 25-30 GB |
| Turns & cost/task | median 8 APIs, ~3s env overhead | <=50 steps, 7ms/step | 10-30 turns, **150k-300k tok** | ~5-10 steps | tens-hundreds steps, 0-100 score |
| Open-model headroom (qwen-32B class) | ~39 TGC-N base vs 76 (ACE) / 87 (RL SOTA) | 40-65% zero-shot (not saturated) | telecom 0.13-0.44 pass^1 | ~50-65 score | ReAct 36 vs SwiftSage 85 |
| Direct comparables | **ACE, GEPA, DC, "Not All Skills Help"** | ExpeL, Reflexion, SkillOpt | POLCA, SEVerA | ExpeL, LATS, Reflexion | WorldEvolver, Evo-Memory (AgentBoard protocol) |
| License | Apache-2.0 (+encrypted-data clause) | MIT (Fast Downward runtime dep is **GPL-v3** — flag) | MIT | MIT (data research-only) | Apache-2.0 |
| Integration effort | 2-4 d (server pool is the work) | 0.5-1.5 d (+wheel prebuild) | ~1 d (policy seam is 1 line) | 0.5-1 d (with BM25 swap) | ~1 d |

## Server deployment plan (ordered)

1. **Miniconda (once)**: installer from `mirrors.tuna.tsinghua.edu.cn/anaconda/miniconda/`
   (reachable, verified HTTP 200). Create `appworld-py311`, `tau2-py312`,
   `alfworld-py310` envs; pip via Tsinghua index.
2. **AppWorld**: `pip install appworld && appworld install`; data: try S3 URL from
   server first, else download on Mac + rsync `$APPWORLD_ROOT/data`. Validate with
   `appworld verify tasks --remote-environment-url` at target concurrency.
   Build the env-server pool manager (start N `appworld serve environment` procs).
3. **ALFWorld**: prebuild jericho/fast-downward wheels in a manylinux/py3.10
   container on the Mac, ship with git bundle; scp `~/.cache/alfworld` (2 GB).
   Subprocess env-pool adapter (fork), `max_episode_steps=50`.
4. **τ²-bench**: git bundle repo; conda py3.12; `pip install -e .` core only;
   point agent+user LLM args at the qwen endpoint (litellm `openai/` prefix).
   Confirm vLLM serves `--enable-auto-tool-choice --tool-call-parser hermes`
   (Bird already uses native function calling — likely on). Drop voice/audio
   JSONs to shrink. CSS seam: `env.policy = <skill doc>` before build_agent.
5. **WebShop**: vendor `webshop-minimal` via git bundle (1k/100k data bundled,
   zero-download smoke test); full 5.67 GB catalog later via hf-mirror on server.
   Swap pyserini → `rank_bm25` (~30 lines) to drop Java+thread-safety issues.
6. **ScienceWorld**: `pip install scienceworld==1.2.3` from mirror (wheel bundles
   JAR; server Java 17 works). One JVM per rollout worker, recycle every M episodes.

## Notes for the CSS adapter layer

- All five expose a deterministic per-task success signal compatible with
  `TaskResult(hard=...)`; ScienceWorld/WebShop additionally give continuous
  scores (map to `soft`).
- Thread-based `batch_rollout` (current harness) works as-is ONLY for τ²-bench
  and WebShop(+BM25). AppWorld/ALFWorld/ScienceWorld need a process-pool or
  env-server indirection — a reusable `ProcessEnvPool` utility in `css/envs/common.py`
  would serve all three.
- Eval-protocol pitfalls: WebShop numbers are subsample-fragmented (500 vs 100
  vs 50); ScienceWorld has two protocols (original 0-100 avg vs AgentBoard
  success/progress); ALFWorld community standard is 134 unseen @ 50 steps;
  AppWorld reports TGC/SGC on test_normal AND test_challenge; τ² uses pass^k
  (k trials). Pin the protocol in the launcher from day one.
- τ² user-sim noise (16%/6%-critical in telecom) interacts with our gate noise
  findings — prefer airline/retail first, paired seeds, multi-trial.
