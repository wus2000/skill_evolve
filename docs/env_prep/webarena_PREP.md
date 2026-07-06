# WebArena — Introduction PREP (re-assessment 2026-07-06: CONDITIONAL GO)

Supersedes the deployment verdict in `webarena_osworld_ASSESSMENT.md` (2026-07-04,
NO-GO). The scientific case in that document (§4.3–4.4) stands unchanged and is
extended here; what changed is **deployment feasibility** — three of the four
NO-GO pillars have collapsed on re-audit, and the fourth (parallel-safe reset)
is dissolved by a scoring-layer discovery (WebArena-Verified offline HAR replay)
plus a Docker-only stack-farm design.

Motivation for re-opening: ALFWorld (round0 test 0.94) and AppWorld (val 0.689
round0) are too easy for the target model — the L1 strategy search and the
free-exploration probe mechanism barely engage when the unsolved residual is
near zero. WebArena at an expected ~15–35% zero-shot band restores a large
unsolved frontier, and it is THE canonical benchmark of the skill/experience-
document literature (AWM, CER, ASI, SkillWeaver, Learn-by-Interact).

Facts below are tagged: **[measured]** = first-hand on our server today,
**[computed]** = exact analysis of primary config/code files (research agent,
2026-07-06), **[docs/paper]** = external source (confidence noted).

---

## 0. TL;DR

| Question | Answer |
|---|---|
| Fits on the server? | **YES via ServiceNow optimized images** (~31 GB pull → ~50–70 GB unpacked; `/` has 77.5 GB free for the docker store) [measured+docs]. Stock images (208 GB tars, measured on CMU metis) remain impossible. |
| Pullable despite blocked docker.io? | **YES** — `docker.1panel.live` and `dockerproxy.net` proxy registries resolve `am1n3e/webarena-verified-*` manifests from the server [measured]. Mac-relay (docker save→scp) is the fallback (Mac has Docker 27.4, 107 GB free) [measured]. |
| Parallel-reset death spiral? | **Dissolved at the scoring layer**: WebArena-Verified evaluates offline from a network trace (HAR) + structured agent response — no live post-episode site state needed. Observation-level cross-talk remains and is handled by a small stack farm + mutation-aware scheduling (§5). |
| Deterministic scoring? | **YES with WebArena-Verified** (removes the 118-task GPT-4 fuzzy judge + 36 abstain-judge tasks; type-aware normalization + backend-state checks; all 812 re-audited) [docs HIGH]. |
| Difficulty fit? | Expected qwen3.6-35b-a3b zero-shot **~22–28% (band 15–35%)** on Lite-class AXTree harness; human 78.24; frozen-model skill-doc lifts of **+8–12 abs** are multi-lab replicated (AWM/CER/LbI) [docs HIGH]. |
| Effort | ~1.5–2.5 weeks elapsed: P0 validation (1–2 d) → adapter (2–4 d) → stack farm (2–3 d) → splits+smoke (1–2 d). Old "multi-week env-layer re-engineering" estimate no longer applies (Verified + optimized images + Docker presence removed the hard parts). |
| Blocking risks | P0 gates: (i) HAR-replay evaluator works as advertised on mutation tasks; (ii) optimized images boot healthy + in-place reset API status (known issue #39: broken for GitLab/Reddit as of Feb 2026); (iii) disk headroom on `/` under real container write-layers. |

---

## 1. What changed since the 2026-07-04 NO-GO [measured 2026-07-06]

| NO-GO pillar (07-04) | Re-audit (07-06) |
|---|---|
| Docker unconfirmed | **Docker 26.1.3 installed; `wushang` in docker group** (no sudo needed for docker ops). NO passwordless sudo otherwise. |
| Images must be side-loaded (GitHub/HF blocked) | **CMU metis.lti.cs.cmu.edu reachable over plain HTTP** — stock tars directly downloadable (sizes verified via HEAD: gitlab 77.8 GB, shopping 67.6 GB, postmill 53.4 GB, admin 9.6 GB, wiki zim 95.2 GB — 4-site total 208.4 GB ⇒ still does not fit). |
| "149 GB free on 98%-full volume" | Real layout: `/` 191 GB (77.5 GB free, **docker root lives here**), `/home` 5.6 TB (81 GB free, 99%), `/data` 1.7 TB (~52 GB free). The 4.6 TB `sdc` is a foreign NTFS drive (unusable, no sudo). RAM 251 GB total, ~105 GB available with 3 experiments running. |
| No path to smaller images | **ServiceNow optimized images (v1.1.0, 2026-02-02)**: shopping 5.05 / admin 1.16 / reddit 4.26 / gitlab 20.49 GB compressed ≈ **31 GB total (−85%)**; Docker-Hub-only (`am1n3e/*`) but **proxy-pull verified from the server**. |
| No parallel-safe reset | WebArena-Verified **offline HAR-replay scoring** + trajectory-only task pool + per-site reset tooling + warmed `docker commit` snapshots (§5). |

Network reachability summary [measured]: metis ✅ (HTTP), ghcr.io + pkg-containers.githubusercontent.com ✅, quay.io ✅, s3.amazonaws.com ✅, pypi ✅, playwright CDN ✅, hf-mirror ✅, download.kiwix.org ✅; **docker.io ❌, github.com ❌, objects.githubusercontent.com ❌**. Docker-Hub proxies `docker.1panel.live` ✅ / `dockerproxy.net` ✅ (manifest inspect of the target images succeeds).

---

## 2. Environment logistics (from wa-logistics report, verified where marked)

**Scope: WebArena-Lite 4-site stack** — Shopping (Magento, :7770), Shopping-Admin
(Magento CMS, :7780), Reddit (Postmill, :9999), GitLab (:8023) + tiny Flask
homepage (:4399). Wikipedia (.zim 95 GB) and the Map stack (60–90 min warmup)
are NOT needed for Lite (ScaleCUA WebArena-Lite-v2 confirms; 134 of 812 tasks
touch map/wiki and are excluded by our core-4 scoping anyway).

**Images — the only viable set for this box** (ServiceNow `am1n3e/webarena-verified-*`,
Docker Hub, Feb 2026):

| Site | Compressed pull | vs stock tar |
|---|---|---|
| shopping | 5.05 GB | 67.6 GB (−92.5%) |
| shopping_admin | 1.16 GB | 9.6 GB (−88%) |
| reddit | 4.26 GB | 53.4 GB (−92%) |
| gitlab | 20.49 GB | 77.8 GB (−74%) |
| **total** | **~31 GB** | 208.4 GB |

Unpacked estimate ~50–70 GB into `/var/lib/docker` (78 GB free) — tight but
workable with tars streamed one-at-a-time and aggressive pruning. These images
also bake in **auto-login headers** (drops the `auto_login.py` cookie step) and
an **in-place reset API** (`POST :8877/init` / `docker exec <c> env-ctrl init`)
— **known broken for GitLab and Reddit as of Feb 2026 (webarena-verified issue
#39); validate in P0, fall back to container recreate**.

**Runtime footprint** [docs MED]: GitLab ~4–8 GB RAM (warmup ~5 min; give
`--shm-size=256m+` to avoid 502s), Magento sites ~3–5 GB each, Postmill ~0.5–1 GB.
**One 4-site stack ≈ 12–20 GB RAM.** Post-start base-URL rewrite steps required
(Magento `setup:store-config:set` + cache flush; GitLab `external_url` +
`gitlab-ctl reconfigure`) — bake the result into **warmed `docker commit`
golden snapshots** so replica/reset spin-up skips the 5-min GitLab reconfigure.

**Reset costs** (stock mechanics): Reddit 10–30 s, Magento ~1–2 min, GitLab ~5 min
(→ ~1–3 min from a warmed committed image). Accounts are fixed singletons per
site (`emma.lopez@…`, `admin`, `MarvelsGrantMan136`, `byteblaze`); parallelism
is done by whole-stack replicas on distinct port ranges (ScaleCUA precedent).

**Community harness status**: web-arena-x/webarena still maintained (last update
~Mar 2026); the modern canonical path is ServiceNow BrowserGym/AgentLab
(`browsergym.webarena`, `browsergym.webarena_verified`) — we vendor only what we
need (obs/action extraction + evaluator), consistent with house style (no heavy
framework dependency).

---

## 3. Task & evaluator structure [computed — exact counts from primary configs]

- **812 tasks / 190 unique `intent_template_id`** (241 unique template strings).
  Split MUST be at template granularity (same template = near-duplicate).
- Single-site: shopping 187, admin 182, gitlab 180, map 109, reddit 106, multisite 48.
  **Core-4-only tasks: 678** (excluding the 134 that touch map/wikipedia).
- **Evaluator families**: string_match 335 (must_include 176 / fuzzy_match 118 /
  exact_match 45), url_match 205, program_html 411 (mix per task; product of all).
- **Noise sources in stock scoring**: `fuzzy_match` = GPT-4-1106 LLM judge
  (118 tasks; map-heavy); 36 infeasible/abstain tasks ALSO LLM-judged
  (`llm_ua_match`). **WebArena-Verified deterministizes all of this** (all 812
  re-audited; LLM judge and substring matching removed; ~11 pp false-negative
  reduction reported) [docs HIGH].
- **Scoring's live-state dependency**: program_html (411 tasks) queries the live
  site post-episode (109 need the agent's exact final page). string/url-only
  tasks (391) score from the trajectory alone. **Verified's HAR-replay evaluation
  (`evaluate_task(task_id, agent_response, network_trace)`) removes the live-site
  requirement entirely** — P0 must validate this on real mutation tasks.
- **Mutation proxy**: program_html presence ⇒ likely state-changing (411);
  string/url-only ⇒ read-only-ish (391). No official per-task mutation labels
  exist; we will classify our pool this way for scheduling (§5).
- **WebArena-Lite (VAB) = 165 human-verified test tasks** (subset of the 812,
  re-indexed; join on `old_task_id`), 42 evaluator corrections, cross-site tasks
  retained (10), **36 still fuzzy** (Lite alone ≠ deterministic; Verified is).
  Train complement = 647, of which **493 deterministic core-4** (313 need
  live-site scoring under stock evaluators — moot under HAR replay).
- **Template-leakage trap** [computed]: 99 of ~143 deterministic-core-4 template
  groups present in the 647 train complement ALSO appear in Lite-165. A naive
  "train 647 / test Lite" split leaks ~99 templates. This forces the split
  decision in §6.

**Harness interface** (official): obs = accessibility tree with numbered element
ids; actions = `click [id]`, `type [id] [text]`, `hover`, `press`, `scroll`,
`goto`, `new_tab`, `close_tab`, `go_back`, `go_forward`, `tab_focus`,
`stop [answer]`; **max 30 steps** (community standard); answer via `stop[...]`.

---

## 4. Difficulty fit & competitive landscape (from wa-sota report)

- **Zero-shot band for qwen3.6-35b-a3b (AXTree, prompted): central ~22–28%,
  plausible 15–35%** — anchored by Qwen3.6-27B "vanilla" 44–51% on the three
  easier domains (budget study 2606.15017), GPT-4o 13.9% / GPT-4-Turbo 17.6% on
  Lite (WebRL), Qwen2.5-32B 16.9% / QwQ-32B 22.4% (prior audit), discounted for
  3B-active MoE on a planning-heavy benchmark. Harness quality swings the
  baseline ±15 pp → freeze and report the harness.
- **Frozen-model skill-document lifts are multi-lab replicated**: AWM +12.0
  (23.5→35.5), CER +12.4 (24.3→36.7), Learn-by-Interact +12.2 (Claude
  35.8→48.0); ASI (programmatic skills) +23.5% rel; SkillWeaver +31.8% rel;
  ReasoningBank +8.3 abs. Realistic target for us: **~22% → 32–40%**, brushing
  the WebRL/WebAgent-R1 RL-trained band (42–49%) with zero weight updates —
  the strongest possible framing for a no-finetuning method.
- **Gains are larger for weaker base models** (SkillWeaver transfer +54.3% to
  weak agents; LbI biggest lifts on weaker models) — favorable for our 35B-A3B.
- **⚠️ The result we must design against — arXiv 2606.15017**: under
  token-budget matching, a vanilla actor ≥ AWM/ASI/ReasoningBank (tested with
  Qwen3.6-27B among others). **Our protocol must include a budget-matched
  vanilla baseline from day one** (report token/step budgets; show the lift
  survives). This applies to the whole paper, not just WebArena.
- Failure structure: planning/knowledge/convention bucket ≈ 27–40% of failures
  (the skill-doc-repairable slice); access/grounding ≈ half (not repairable) —
  states the honest ceiling. Reddit easiest site; GitLab/Shopping/CMS hardest
  (Map excluded by scoping).
- **CSS-specific fit**: at ~22% start the unsolved-task residual is large →
  L1's dossier frontier, unsolved groups, and the free-exploration probe
  mechanism finally have material to work on (the explicit reason this env is
  being introduced). AgentOccam (+26.6 from written conventions alone) marks
  the L0/tactical share; AWM-style workflow knowledge marks the L1/strategy
  share.

---

## 5. Runtime architecture (Docker-only; no sudo ⇒ no Incus/ZFS WebServ path)

Reference points: WebServ clone-per-rollout (1.78 s / 28 MiB / 200+ concurrent)
is the gold standard but needs root+ZFS — unavailable here. WebRL used 8
parallel groups on ONE stack with full 3–5 min refreshes between rounds
(off-policy tolerance); AgentGym-RL's `/reset` is browser-level only (not DB).
Cross-rollout mutation bleed is real (WebAgent-R1 cart-bleed) and would inject
**correlated noise into the paired gate** — the design below removes it by
construction where it matters.

**Stack farm**: 2–3 replicas of the 4-site stack (optimized images, shared
read-only layers ⇒ +10–20 GB write-layer disk and +12–20 GB RAM per replica),
distinct port ranges; golden warmed snapshots via `docker commit` per site.

**Mutation-aware scheduler** (env-private, in the adapter):
- Task classes from §3: **read-only pool** (string/url-only evaluators) runs
  freely against any stack — high concurrency; **mutating tasks**
  (program_html) each acquire an exclusive **(stack, site) lane**, run
  serially, and trigger a **site refresh** (container recreate from warmed
  snapshot: reddit ~10–30 s, magento ~1–2 min, gitlab ~1–3 min) before the
  lane accepts the next mutating episode. 2–3 stacks × 4 sites = 8–12 mutation
  lanes + a read-only pool at 16–32 concurrent browsers.
- **K-rollouts of the same mutating task** (paired gate) each get a fresh lane
  slot — identical clean initial state per rollout, no idempotency assumptions
  (nobody has quantified safe re-running; universal practice is defensive
  reset).
- **Scoring is offline** (Verified HAR replay + structured answer): no
  post-episode live-site queries, so a lane can refresh immediately after the
  episode ends — scoring never blocks the farm. If P0 falsifies HAR replay on
  some class, fallback = score-before-refresh on the lane (slower) or restrict
  to the trajectory-only pool (§6 fallback).
- Browsers: playwright chromium per rollout (process-isolated per our
  subprocess-worker pattern; playwright ships native HAR recording —
  `record_har_path`). 80 cores sustain 32–48 comfortably.

**Throughput estimate**: mutating lanes ~8–12 × ~6–10 episodes/hr + read-only
pool ~2–3× that ⇒ **~150–300 rollouts/hr** (vs WebServ 600–1000, stock-Docker
naive 100–200). Episode ≈ 11 turns avg / 30 max, ~3–8 min at our endpoint
latencies. A CSS burst (5 steps × [minibatch + gate screens/escalations]) lands
in the **several-hours** class — comparable to SpreadsheetBench today; val size
and screen_k must be budgeted accordingly (§7).

**Disk watch**: `/` holds images + write layers; recreate-from-snapshot resets
shed layer growth by design; monitor with `docker system df` + prune; keep
≥15 GB margin. If `/` proves too tight: dind with data-root on `/home` (81 GB)
is the no-sudo escape hatch; asking the admin to move docker data-root to a
bigger volume is the clean fix.

---

## 6. Split design (decision required — the template-leakage tension is irreducible)

Pools [computed]: deterministic core-4 = **607 tasks / 143 template ids**;
trajectory-only deterministic core-4 = **231 tasks / ~79 templates**; Lite-165
deterministic-core-4 slice = **117 tasks**; template-disjoint-from-Lite train
slice = only **~99–158 tasks** (Lite covers ~121/143 templates — it eats nearly
all template groups, which is exactly why WebRL/LbI synthesize training data).

- **Design A — clean optimization split (recommended)**: partition the 607-task
  /143-template pool at template level ≈ **100 train / 15 val / 28 test
  templates ⇒ ~420 / 60 / 120 tasks**, template-disjoint by construction,
  scored by Verified throughout. Not directly comparable to published Lite SR;
  we report the community zero-shot bands as context instead. Largest train
  pool → healthiest optimization signal.
- **Design B — comparable test**: test = Lite-165 under Verified; train/val =
  the template-disjoint ~99–158 deterministic core-4 tasks. Clean comparability,
  but the train pool is ~3–4× smaller — weak for a multi-epoch optimizer.
- **Design A′ — A plus a Lite window**: as A, but choose test templates to
  maximize overlap with Lite tasks; additionally report the Lite∩test subset
  under the community protocol as a secondary, honestly-labeled number.
- Guard: Reddit is template-scarce — ensure every split keeps Reddit coverage.
- Fallback pool if HAR replay fails P0: the 231 trajectory-only tasks
  (~55/8/16 templates ≈ 160/25/45) — fully reset-agnostic, biased to
  info-seeking (declare the bias).

---

## 7. CSS integration surface (maps 1:1 onto `docs/env_integration_guide.md`)

- `css/envs/webarena/`: items = task records (stable `id` = original task_id,
  sites, intent, `intent_template_id`, evaluator bundle, mutation class);
  splits pre-built into `split_dir/{train,val,test}/items.json` from the §6
  design.
- `run_one`: lease a (stack, site) slot from the scheduler → playwright episode
  (AXTree obs, ~12-action DSL, `max_turns=30`, `record_har_path` on) →
  `stop[answer]` → release/refresh lane → **score offline via vendored
  Verified evaluator** → flatten transcript (obs/action per turn) → eval
  annotation LAST (outcome + gold reference; GT firewall: reference answers
  live only in evaluator configs, never in agent prompts).
- `action_space_description()`: the WebArena action DSL verbatim — L1 designs
  behavioral paradigms against exactly this text.
- Env-private knobs under `cfg.extra["webarena_*"]`: stack count, port bases,
  lane policy, refresh thresholds, AXTree truncation, HAR dir.
- Launcher `run_experiment_webarena_server.py` with the per-env agreed-default
  convention (every value annotated with its rationale, per house rule);
  strawman defaults to negotiate at onboarding: `max_turns 30` (community),
  `k_rollouts 3`, `batch 40 / minibatch 8`, `workers` = browser concurrency
  32, `gate_screen_k 3` (val ~60), `task_timeout 1800`, agent temp 0,
  endpoints = the shared three-arm pool (to be agreed).
- Tests: contract suite (8 greens) + a scheduler unit test (lane exclusivity,
  refresh-on-mutation) + an evaluator golden test (fixed HAR + answer →
  expected verdict).

## 8. Phased landing plan

- **P0 — validation spike (1–2 days, GATES EVERYTHING)**: proxy-pull the 4
  optimized images (~31 GB) → stand up 1 stack → warmed golden commits →
  (i) boot health + base-URL correctness; (ii) reset API status vs issue #39;
  (iii) **Verified evaluator + HAR replay on 3–5 real episodes incl. mutation
  tasks** (the load-bearing validation); (iv) measure RAM/warmup/reset/disk;
  (v) 5–10 zero-shot qwen episodes → confirm the ~22% band, episode latency,
  token volume. Abort criteria: HAR replay unusable AND trajectory-only pool
  deemed too biased; or disk blows past `/` margins.
- **P1 — env adapter (2–4 days)**: agent loop + AXTree rendering + action
  parsing + Verified scoring + persistence/caching + contract tests.
- **P2 — stack farm + scheduler (2–3 days)**: replicas, lanes, refresh policy,
  concurrency load test (32–48 browsers), throughput measurement.
- **P3 — splits + launcher + smoke (1–2 days)**: split manifests per the §6
  decision, per-env default-config negotiation, tiny smoke
  (n_train 8 / n_val 4 / n_test 4, k=1), then round0 full run.

## 9. Decision points for the user

1. **GO/NO-GO** on WebArena as the new flagship (rec: GO, gated on P0).
2. **Split design**: A / B / A′ (rec: A′ — clean optimization + honest Lite window).
3. **Scoring**: adopt WebArena-Verified + HAR offline replay (rec: yes; fallback trajectory-only pool).
4. **Disk**: proceed on `/` (77.5 GB free) with monitoring vs first ask the server admin to move docker data-root to a big volume (rec: proceed + ask in parallel).
5. **Timing**: run P0 now alongside the three live experiments (1 stack ≈ 15–20 GB RAM, fine) vs wait (rec: now).
6. **Endpoints** for WebArena rollouts: share the three-arm pool vs dedicate (to negotiate at onboarding per house convention).
7. **Protocol addition**: adopt the budget-matched vanilla baseline (2606.15017 defense) as a standing part of our eval protocol (rec: yes, paper-wide).

## 9-bis. ADDENDUM 2026-07-06 (second-server audit — supersedes §5 sizing and §6 split)

User granted access to a second server: **zkgy-gpu = 10.77.110.162 (ssh -p 5102, user
haoyang; workspace /data3/wushang/skills_evolve)** — the same host that serves the
vLLM arms. Audit [measured]:

- **Disk solved outright**: `/data3` 17 TB with **5.3 TB free**, and **Docker Root
  Dir is already /data3/docker**. Even stock images fit; optimized images still
  preferred (auto-login headers + reset API + Verified task-set alignment).
- **RAM 503 GB (445 GB available)**, 80 cores, Ubuntu 20.04, Docker 28.1.1,
  haoyang in docker group; target ports 7770/7780/9999/8023/4399/8888/8889 all free.
- Network from 162: metis ✅ (stock tars direct), docker.1panel.live ✅ +
  dockerproxy.net ✅ (optimized images direct pull), pypi ✅, playwright CDN ✅;
  hf-mirror ❌ (irrelevant for WebArena; relay via 127 if ever needed).
- No conda (system py3.8) → keep the CSS harness on 127 (topology below).
- sudo: haoyang is a sudoer (password-gated; password known) — Incus/ZFS
  clone-per-rollout becomes a REAL upgrade path (§5's "no-sudo ⇒ no WebServ"
  no longer binds on this host). v1 still Docker-only for simplicity.
- Shared host caution: two long-running third-party containers (qwen3vl,
  cosmos-policy, up 7–8 weeks) — leave resource headroom.
- vLLM replicas: both went down via **graceful shutdown** (log tails:
  "Application shutdown complete"), not a crash; script now at GPU_UTIL=0.8;
  restart = `./vllm_cluster.sh start` when the user green-lights.

**Revised topology (recommended)**: sites farm (6–10 four-site stacks from
optimized images + kiwix wikipedia for the 6 gitlab+wiki multi tasks) + vLLM on
**162**; CSS harness + playwright browser pool + runs/ + monitors stay on
**127** (CPU idle there; browsers are the CPU hogs); farm control (per-episode
site recreate) via ssh key 127→162 (needs user OK to add key to haoyang's
authorized_keys). Stack count ceiling moves from 2–3 (127 disk-bound) to 8–16
(162 RAM-bound) → mutation lanes 24–40, browsers 32–64, throughput estimate
revised to **~300–600 rollouts/hr**.

**Revised split (user proposal, adopted — supersedes §6 Designs A/B/A′)**:
scope = the **684-task 5-category protocol** (ReasoningBank-style: drop all 128
map-touching tasks from 812; keep Shopping 187 / Admin 182 / Gitlab 180 /
Reddit 106 / Multi 29, where Multi = gitlab+reddit 18, reddit+shopping 5,
gitlab+wiki 6). Under Verified scoring the 118 ex-fuzzy tasks are deterministic,
so the full 684 is usable (bigger than the 607 fuzzy-free pool §6 assumed).
Split **within each category at intent_template_id-group granularity** (never
task-level — 812 tasks / 190 templates means task-level splits leak
near-duplicates), targeting **65/10/25**:

| Category | total | train | val | test |
|---|---|---|---|---|
| Shopping | 187 | ~122 | ~19 | ~46 |
| Admin | 182 | ~118 | ~18 | ~46 |
| Gitlab | 180 | ~117 | ~18 | ~45 |
| Reddit | 106 | ~69 | ~11 | ~26 |
| Multi | 29 | ~19 | ~3 | ~7 |
| **Σ** | **684** | **~445** | **~68** | **~171** |

(Template groups are the draw unit ⇒ ±3–5 wobble; seeded manifests sealed as
per house convention. Multi contributes ~3 val tasks — negligible gate signal,
acceptable; N/A-infeasible tasks retained pending exact count at P0 — abstention
is learnable procedural knowledge and Verified scores it deterministically.)

**684-protocol provenance [verified from primary sources 2026-07-06]**:
ReasoningBank (arXiv 2509.25140, ICLR'26) states: "We exclude the domain of Map
due to website issues following Miyai et al. (2025) for a fair comparison" —
Miyai et al. 2025 = **WebChoreArena** (arXiv 2506.01952), which builds on
exactly the four reproducible WebArena environments (Shopping/Admin/Reddit/
GitLab), dropping Map for reproducibility. Citation chain for our scope:
WebChoreArena → ReasoningBank → us. CAVEATS: ReasoningBank scores with the
OFFICIAL evaluators including the LLM fuzzy judge ("both LLM-based fuzzy
matching and exact string matching"), runs temp 0.7 on BrowserGym, online
memory over the sequential test stream (no train/test split), single run —
so its absolute SR (Gemini-2.5-Pro 46.7→53.9) is NOT directly comparable to
our Verified-scored, split-protocol numbers. Position via per-site structure,
zero-shot bands, and the budget-matched vanilla baseline; never bare-compare.

**Verified dataset hands-on audit [computed 2026-07-06 on assets/dataset/webarena-verified.json]**:
812 records; fields per record: sites, task_id, intent_template_id, start_urls,
intent, intent_template, instantiation_dict, eval, revision — everything the
split generator needs, no join with original configs required. Eval model:
every task has an **AgentResponseEvaluator** (expected = {task_type ∈
retrieve 325 / navigate 113 / mutate 374, status, retrieved_data} with
type-aware normalization + a results_schema); 663 tasks additionally have a
**NetworkEventEvaluator** (offline HAR checks: navigated URLs + HTTP statuses).
⇒ **official mutation labels for free** (scheduler lane policy keys off
task_type=mutate, replacing the program_html proxy) and **fully offline
scoring** (agent_response.json + network.har → eval_result.json with
score/checksums). Infeasible tasks are deterministic status expectations
(within-684: NOT_FOUND_ERROR 22, ACTION_NOT_ALLOWED_ERROR 11,
PERMISSION_DENIED_ERROR 1 = 34; RETAIN — abstention is learnable and scored
deterministically). Map-drop filter on Verified reproduces the ReasoningBank
table exactly (187/182/180/106/29; multi = gitlab+reddit 18, gitlab+wiki 6,
reddit+shopping 5). Zero templates span categories. Usage pipeline (docs):
py3.11+ package; `agent-input-get` exports task JSON → agent (any framework)
produces agent_response.json + playwright-recorded network.har →
`eval-tasks` CLI scores offline; site URLs injected via config placeholder
substitution (__SHOPPING__ etc.); demo-gitlab lightweight container available
for smoke tests.

**Computed split (seed=20260706, template-group unit, ≥1 group per category
per split, zero leakage verified)**:

| split | shopping | admin | gitlab | reddit | multi | TOTAL | templates | task_type mix |
|---|---|---|---|---|---|---|---|---|
| train | 120 | 113 | 114 | 65 | 16 | **428** | 104 | mut 236 / ret 158 / nav 34 |
| val | 20 | 21 | 21 | 15 | 5 | **82** | 16 | mut 46 / ret 16 / nav 20 |
| test | 47 | 48 | 45 | 26 | 8 | **174** | 37 | mut 92 / ret 57 / nav 25 |

Val landed at 82 (12%) due to the ≥1-group seeding heuristic; trim toward ~68
(10%) at manifest time by moving 1–2 small val template groups to train if
gate cost requires (each val task costs K rollouts/side; 46 mutating val tasks
drive lane demand). Pin dataset revision + evaluator/data checksums in the
sealed manifests.

**Hard subset [computed + docs, 2026-07-06]**: difficulty-prioritized
eval-cost subset; paper version 137 (−83% cost), release version 258
(`subsets/webarena-verified-hard.json`; selection methodology not published in
README). Composition: shopping 56 / admin 55 / gitlab 57 / reddit 42 / multi
48 (= ALL multisite tasks), 19 touch map, **hard∩684 = 239**, 62% mutate.
Overlap with our computed split: train 161 / val 23 / **test 55**. USE: as a
pre-registered REPORTING slice only ("lift on hard∩test = 55") — never as
train/val pool (difficulty-biased sample would skew the gate and the train
distribution).

**Map-drop rationale (full)**: (1) reproducibility — original Map was
effectively not self-hostable (official runs pointed at a hosted instance;
self-host = 5-container OSM stack, 60–90 min warmup) and instance outages made
map numbers irreproducible → WebChoreArena's documented reason, inherited by
ReasoningBank; (2) noise — 47/128 map-touching tasks use the fuzzy LLM judge
(most judge-laden domain; geo answers drift with map-data versions); (3)
protocol alignment with the 684 citation chain. HONEST NUANCE: Verified
2026-02 ships a single-container Map (beta) + data-init tooling, so hosting is
now *possible* — we drop it for beta-risk + noise + alignment, not
impossibility; future option if reviewers ask. The 6 gitlab+wikipedia multi
tasks stay (kiwix is reproducible/read-only).

**Adoption status (2026-07)**: PyPI Jan-2026 / Docker Feb-2026 — ~5 months
old, EARLY adoption. Confirmed adopter: arXiv 2604.27151 (Apr 2026,
compute-cascading CUA) — evaluates on WebArena-Verified, calling it "a more
reliable and reproducible evaluation protocol"; **Verified-scored anchors:
gpt-oss-20b standalone 37.6%, GPT-5.2 60.1%** (recalibrates our qwen zero-shot
expectation upward to ~28–38% under Verified scoring — official-scored bands
run ~11pp lower). Ecosystem: BrowserGym ships `browsergym.webarena_verified`;
paper at NeurIPS'25 SEA wksp + ICLR'26 submission. We are early adopters —
cite the Verified paper + 2604.27151 as precedent; always annotate "scored by
WebArena-Verified".

**Template-disjointness evidence dossier [verified 2026-07-06]** (archived for
paper-writing): (a) Structure: 684 pool = 157 templates, 138 with ≥2 siblings,
modal group size 5 (105 templates) — siblings are parameter swaps, e.g.
template 279 "Get the top-{{n}} best-selling {{entity}} in {{period}}" (task 0
product/2022 vs task 1 brand/Q1-2022; identical procedure), template 332
GitLab "Create a {{create_spec}} project ..." ×10. (b) AWM explicitly treats
within-template transfer as a CONFOUND: "Some tasks in the WebArena benchmark
have highly overlapping canonical trajectories, due to the benchmark
construction process that instantiates multiple examples from a single
underlying task template" and "To confirm that the benefits of AWM are not
just from learning workflows that help only within a template … we extract a
subset … sourcing from non-overlapping templates" (197 tasks, one per
template; AWM 33.2% vs baseline 20.5% cross-template — gains survive). (c) The
de facto Lite train/test protocol does NOT enforce disjointness (647-complement
shares 99/143 template groups with Lite-165, our computation), and WebRL
self-generates training instructions with NO decontamination/similarity filter
vs the 165 test (verified from paper — no such mechanism described). (d)
Online-protocol works (AWM main, ReasoningBank, CER, ASI) score-then-memorize
over the test stream, so their numbers inherently include within-template
transfer credit. VERDICT: no community-enforced norm exists; for an OFFLINE
train→test protocol like ours, template-level splitting is the only design
that supports the "generalizes to unseen task types" claim; our numbers are
conservatively lower than task-level-split numbers by construction (state in
paper); AWM's cross-template subset is the direct precedent.

P0 now runs entirely on 162 (direct pulls, no relay). Topology DECIDED
2026-07-06: split-duty (farm+vLLM on 162, harness+browsers on 127, spillover
to 162 as needed). SPLIT SOURCE DECIDED: the Verified dataset JSON is the
single source of truth for items + scoring (684 scope filtered on it; map
dropped per the WebChoreArena→ReasoningBank chain). SPLIT GRANULARITY DECIDED:
per-category template-group draw, 65/10/25.
Open user decisions: val size final trim; vLLM restart timing; P0 start.

## 10. Sources

First-hand: server audit + metis HEAD sizes + proxy manifest checks, 2026-07-06.
Agent reports (2026-07-06): logistics / parallel-infra / SOTA / eval-structure —
key externals: github.com/web-arena-x/webarena (configs+evaluators),
github.com/ServiceNow/webarena-verified (+ issue #39; NeurIPS'25 SEA wksp),
hub.docker.com/r/am1n3e/*, THUDM/VisualAgentBench (VAB-WebArena-Lite), WebRL
2411.02337, WebServ 2510.16252, AgentGym-RL 2509.08755, WebAgent-R1 2505.16421,
AWM 2409.07429, CER 2506.06698, Learn-by-Interact 2501.10893, ASI 2504.06821,
SkillWeaver 2504.07079, ReasoningBank 2509.25140, AgentOccam 2410.13825,
budget-matched study 2606.15017, failure taxonomy 2603.14248, ScaleCUA
WebArena-Lite-v2 (OpenGVLab), BrowserGym/AgentLab (ServiceNow).
