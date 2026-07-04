# WebArena & OSWorld — Integration Assessment (both NO-GO on this box)

Deployment-feasibility assessment of **WebArena** (CMU, Zhou et al. 2023) and **OSWorld**
(xlang-ai, Xie et al. 2024) — plus their variants and the lighter GUI alternatives — as
task environments for the CSS skill-optimization harness. Companion to the five `*_PREP.md`
files. Facts are tagged **[verified-from-docs/paper]**, **[verified-from-repo]**, or
**[inferred]**; SOTA numbers carry a source. Assessed 2026-07-04.

- WebArena: https://arxiv.org/abs/2307.13854 · env docker README: https://github.com/web-arena-x/webarena/blob/main/environment_docker/README.md
- OSWorld: https://arxiv.org/abs/2404.07972 · https://github.com/xlang-ai/OSWorld · leaderboard http://osworld-v1.xlang.ai/ · OSWorld-Verified https://xlang.ai/blog/osworld-verified

Harness requirements this is judged against: (A) ≥150 tasks for train/val/test splits;
(B) deterministic automatic scoring; (C) **local high-concurrency** on one server — K=3
rollouts × ~45-task batches × up to 128 parallel workers × dozens of steps → thousands of
rollouts, with cheap parallel-safe reset; (D) low cost/task; (E) **knowledge-repairable**
failures (fixable by a written skill document, not raw capability/perception); (F) community
comparability.

---

## 0. TL;DR verdict

| Environment | Verdict | Single decisive issue |
|---|---|---|
| **WebArena (full 812)** | ❌ **NO-GO** | Stateful singleton Docker sites → no parallel-safe reset; ~250–350 GB images don't fit 149 GB free. |
| **WebArena-Lite (165)** | ❌ **NO-GO on this box** (recoverable on a bigger-disk host) | GitLab image alone 100 GB+; Lite images ~110–130 GB vs **149 GB free on a 98%-full disk** → disk-blocked; still needs a re-engineered isolated-per-rollout env layer. |
| **VisualWebArena (910)** | ❌ **NO-GO** | Visual grounding is the benchmark's purpose; same Docker burden, adds nothing to a text-skill-doc thesis. |
| **WorkArena / ++ (33 / 682)** | ❌ **NO-GO (local)** | Runs against a live **ServiceNow cloud SaaS**; not locally self-hostable, concurrency gated by an external instance pool. |
| **MiniWoB++ (104)** | 🟡 **SMOKE-TEST ONLY** | Trivially local, parallel, deterministic — but toy/saturated (98–99%); little procedural knowledge to encode. |
| **OSWorld (369/375)** | ❌ **NO-GO** | Full GUI **VM per rollout** (~6 GB RAM + ~24 GB disk each); 128 workers ≈ 768 GB ≫ 131 GB available; **KVM/nested-virt** dependency. |
| **AndroidWorld (116)** | ❌ **NO-GO** | Android **emulator needs KVM/VT-x**; ~4.5 GB + ~78 s per emulator; no native parallel-eval. (Text-friendly, but deployment kills it.) |
| **WebShop (12,087)** | ✅ **STRONG GO — already prepped** | Pure-Python in-process gym, text-native, deterministic [0,1] reward, trivially parallel. See `webshop_PREP.md`. |

**Bottom line:** neither WebArena nor OSWorld is viable for a 128-worker, thousands-of-rollouts
optimizer on this server. WebArena dies on **disk + parallel-safe reset**; OSWorld dies on
**VM-per-rollout RAM/disk + KVM**. The one web-flavored environment that clears every criterion —
**WebShop** — is already in the landing plan. If a web/GUI signal is genuinely wanted beyond
WebShop, WebArena is only recoverable with (i) a dedicated host with ≥300 GB free disk and
≥200 GB RAM, and (ii) multi-week environment-layer re-engineering. Details below.

---

## 1. Verdict under the actual server baseline (decisive)

The generic analysis (§4–§9) already flags parallelism as the blocker; applying **this
server's real numbers** turns the marginal cases NO-GO. Server baseline (from `README.md` /
env-candidates audit): Ubuntu 20.04, Python 3.8.10 system (no conda), Java 17, 80 cores,
**251 GB RAM but ~131 GB available**, **149 GB free disk on a volume that is 98% full**
("clean before big data"), **GitHub + huggingface.co blocked** (git bundles from Mac;
HF via hf-mirror.com), Docker + nested-virt status **unconfirmed**.

1. **Disk is the hard wall for WebArena — even Lite.** [verified-from-docs + inferred]
   WebArena-Lite still requires **GitLab (image 100 GB+)** plus Shopping (~6 GB) + Shopping-Admin
   + Reddit ≈ **110–130 GB of base images**. There is **149 GB free**, so it *nominally* fits —
   but that consumes essentially all remaining space on an already-98%-full volume, leaving no
   headroom for the DB growth these mutating tasks cause, container overlay/ZFS working copies,
   logs, or the optimizer's own artifacts. Net: **disk-blocked without first reclaiming space**.
   Full WebArena (~250–350 GB, official rec 1 TB EBS) **does not fit at all**. WebServ's clone
   layer keeps *per-container* disk tiny (28 MiB) but the **base images still occupy disk**, so
   the ceiling bites regardless of the clone trick.

2. **RAM ceiling is below the 128-worker target.** [inferred from verified per-unit numbers]
   With **~131 GB available** (not 251): a WebServ-style isolated-per-rollout WebArena caps at
   **~75 containers** (131 / 1.74 GB); OSWorld caps at **~20 VMs** (131 / 6 GB). Both fall well
   short of 128 parallel workers — a ~1.7× (WebArena) to ~6× (OSWorld) throughput shortfall
   versus what the harness assumes.

3. **OSWorld fails on disk too.** [inferred] 24–25 GB per VM image plus reflink working copies,
   on 149 GB free / 98%-full, is a non-starter even before the KVM question — and reflink needs
   an XFS/Btrfs volume, not guaranteed here.

4. **WebShop already clears every constraint and is prepped.** [verified-from-repo, see `webshop_PREP.md`]
   Pure-Python in-process gym, `rank_bm25` swap drops Java, 5.67 GB catalog via hf-mirror
   (`quanwei0/webshop-minimal`) with a zero-download 1k/100k smoke test, 100+ thread-parallel
   rollouts sharing one `SimServer`. The independent conclusion that "WebShop is the best
   web-flavored fit" **reinforces the existing landing plan** (AppWorld → ALFWorld → τ² →
   WebShop → ScienceWorld); it is not a new dependency.

**Net server verdict:** WebArena → **NO-GO on this box** (disk), recoverable only on a
bigger-disk/bigger-RAM host with the env-layer rebuild. OSWorld → **NO-GO** (disk, RAM, and
KVM — any one is sufficient). Nothing here changes the recommendation to lean on the
already-vetted text-native environments.

---

## 2. Premise correction — qwen3.6-35b-a3b is multimodal (matters beyond this assessment)

**The target model is NOT text-only.** `qwen3.6-35b-a3b` is a **natively multimodal
(vision-language) MoE** with a vision encoder that accepts image / video / document input
(MMMU 81.7, VideoMMMU 83.7). It is a **single unified model** — there is no separate text-only
variant vs a VL variant. [verified — recipes.vllm.ai/Qwen/Qwen3.6-35B-A3B labels it "Smaller
Qwen3.6 **multimodal** MoE model"; model announcement (marktechpost, 2026-04-16);
llm-stats.com/models/qwen3.6-35b-a3b]

Caveats and implications:
- **Whether our vLLM endpoint currently serves images is UNVERIFIED.** The recipe's serve
  command (`vllm serve Qwen/Qwen3.6-35B-A3B-FP8 --max-model-len 262144 --reasoning-parser qwen3`)
  shows no vision flags (e.g. no `--limit-mm-per-prompt`) — that may be incomplete docs or an
  intentionally text-only deployment. `llm-endpoint-qwen` memory does not record modality.
  **Action: confirm on the endpoint before assuming either way.**
- **This does not change any verdict here.** WebArena/OSWorld/AndroidWorld are killed by
  **deployment** (disk / VM / KVM), independent of modality. The CSS harness and the
  skill-document paradigm are built text-only.
- **Forward-looking value:** it removes "modality" as an *automatic* disqualifier for visual
  environments. If a vision env is ever wanted (and the endpoint is configured for images), this
  model can participate — the infra blockers, not the modality, are what stand in the way.

---

## 3. WebArena — dataset, eval, forks

### 3.1 Dataset [verified-from-paper]
812 tasks instantiated from **241 templates** (~3.3 instances each). Per-site (AgentOccam Table 2,
the standard split): **Shopping 187, Shopping-Admin 182, GitLab 180, Map 109, Reddit 106,
Multisite 48**. Wikipedia is a supporting reference site (offline Kiwix `.zim`), not a task bucket.
Intents = natural-language goals in 3 functional classes: information-seeking, navigation,
content/config. Site scale: OneStopShop ~90k products, Postmill 127k posts, GitLab 300 repos.

### 3.2 Eval mechanism [verified-from-paper + code]
Three evaluator families: `string_match` (`exact_match` / `must_include` / **`fuzzy_match` =
GPT-4 LLM-judge**), `url_match`, and `program_html` (DOM element locators + DB queries + JS state).
**335/812 tasks are string-match**; the **`fuzzy_match` GPT-4 judge is the only nondeterministic
evaluator** and is a minority. `url_match` / `program_html` are deterministic **given clean site
state** — which is exactly what parallel mutation breaks (§4.2). WebArena also includes deliberately
**infeasible "N/A" tasks** (agent must abstain); GPT-4 mislabels 54.9% of feasible tasks as
impossible — a scoring-noise source.

### 3.3 Eval-correctness issues & cleaned forks [verified-from-paper]
The official evaluator is known-buggy. Cleaned forks:
- **WebArena-Verified (ServiceNow)** — audited all 812 with human verification + 8-agent trajectory
  analysis; **removed the LLM-judge and substring matching** in favor of type-aware normalization +
  structural comparison → **fully deterministic**. github.com/ServiceNow/webarena-verified
- **WebArena-Lite / VAB (165)** — human-verified subset (VisualAgentBench), **39 task-level
  corrections**, cross-site tasks removed; the remaining ~647 repurposed for training. The
  **de-facto eval used by WebRL and WebAgent-R1** → direct numeric comparability. arxiv 2408.06327
- **AgentOccam** corrected specific broken evaluators (e.g. template 165 was previously unsolvable).

---

## 4. WebArena — deployment & the reset/parallelism crux

### 4.1 Deployment reality 2026 [verified-from-docs]
Official self-hosting = **Docker, one image per site**: `shopping_final_0712` (7770),
`shopping_admin_final_0719` (7780), `gitlab-populated-final-port8023` (8023, **image 100 GB+**,
warmup ≥5 min), `postmill-populated-exposed-withimg` (9999), `kiwix-serve` + Wikipedia `.zim`
(8888, **~90–100 GB**), Map = multi-container OSM stack (3000, **warmup 60–90 min**). Total
**~250–350 GB**; official rec **t3a.xlarge + 1000 GB EBS**. Feb-2026 ServiceNow "optimized images"
up to **92% smaller**. A **Docker daemon is effectively required** (the only non-Docker path is a
third-party Incus reimplementation, WebServ). Images live on CMU / Google-Drive / Archive.org
mirrors — **not HF** — so on our blocked-network box they must be **side-loaded manually**
(download on Mac → scp → `docker load`).

### 4.2 State-reset / parallelism — THE CRUX [verified-from-docs + multiple papers]
- **Official reset is coarse:** the environment README instructs resetting **"after evaluating the
  812 examples"** (per full sweep, NOT per task) via `docker stop/rm/run`. Confirmed verbatim.
  **No cheap per-rollout reset exists**, and the README makes **no mention of parallel execution**.
- **Concurrent rollouts cannot share one site instance for mutating tasks.** Sites are singleton
  stateful containers; concurrent create/edit/delete cross-contaminate. Official issues:
  *"running 5–10 tasks in parallel interferes … makes them likely to fail."* The shipped
  `parallel_run.sh` is just multiple `run.py` workers on **one shared stack** with no per-task
  reset — it only "works" because tasks are pre-ordered so earlier mutations don't corrupt later
  ones. Read-only info-seeking tasks *can* share safely; mutation tasks cannot.
- **What parallel-rollout works actually do — they re-engineer the env layer:**

  | Work | Parallelism approach | Cost / scale |
  |---|---|---|
  | **WebServ** (2510.16252) | own isolated env+server pair **per rollout**; Incus + ZFS snapshots + container cloning; per-episode deterministic reset | **1.74 GB RAM / 28 MiB disk / 1.78 s launch** per container; 200+ concurrent on 128 vCPU / **1024 GB RAM**; covers only the 3-site Lite subset |
  | **AgentGym-RL** (2509.08755) | environment-as-a-service; HTTP `/reset` restores each web server to initial state **after every episode**; multiple replicas | subprocess browsers behind one server |
  | **WebAgent-R1** (2505.16421) | async rollout across independent browser instances | evaluates on WebArena-Lite |
  | **BrowserGym / AgentLab** (2412.05467) | ray/joblib parallel eval, "proper instance reset protocols" | ~20–100 parallel tasks by hardware |

- **Fit to our loop:** 128 stock Docker stacks is infeasible (GitLab ~2–4 GB each ≫ 131 GB avail).
  A WebServ-style clone layer fits **~75 containers** in 131 GB (§1.2) but is a separate Incus/ZFS
  stack (conflicts with "Docker unknown") covering only 3 sites, and its base images still exceed
  our free disk (§1.1). **Realistic path = WebArena-Lite + an AgentGym-RL-style single env-server
  with per-episode reset — on a different, bigger-disk host.**

### 4.3 SOTA lineage [verified-from-paper/leaderboard; 2026 rows lower-confidence]
GPT-4 **14.4%** (2023) → WebPilot 37.2% → AgentOccam **43.1 / 45.7%** → Operator/CUA **58.1%** →
IBM CUGA **61.7%** → GPT-5-class **~71–72%** (2026), vs **human 78.24%**. Approaching but not
saturated. Open ~35B **prompted** (WebArena-Lite, realistic starting baseline): **Qwen2.5-32B
16.9%, QwQ-32B 22.4%**; trained-open ceiling (WebRL/WebAgent-R1) ~42–49%. **Large headroom.**

### 4.4 Skill/memory works ON WebArena — direct comparables [verified-from-paper]
| Work | Base | "Skill" form | WebArena delta |
|---|---|---|---|
| **AWM** (2409.07429) | GPT-4 | induced NL workflows | **23.5→35.5%** (+12.0 abs) |
| **AutoManual** (2405.16247) | GPT-4-turbo | **NL rules → manual** (closest analog to a skill doc) | WebArena-Reddit 65.1%; MiniWoB 98.3% |
| **AgentOccam** (2410.13825) | GPT-4-turbo | static action/observation conventions | **16.5→43.1%** (+26.6 abs) |
| **SkillWeaver** (2504.07079) | GPT-4o | skills-as-**Python APIs** (contrast: code not prose) | 12.3→22.6% |
| ExpeL / Synapse / Go-Browse / CER | various | NL insights / exemplars / memory | ALFWorld / MiniWoB / WebArena-family |

**Criterion E is strongly satisfied:** AgentOccam alone shows **+26.6 points from written
representation/action conventions on a frozen model** — WebArena failures are dominated by
convention/procedure, not raw capability or perception (the DOM is given). This is precisely the
kind of failure a skill document repairs — which is why WebArena is *scientifically* attractive and
only the *deployment* rules it out here.

---

## 5. WebArena variants

| Variant | Tasks | Text-accessible? | Local high-concurrency? | Verdict |
|---|---|---|---|---|
| WebArena-Lite | 165 (+647 train) | ✅ DOM/AXTree | needs re-engineered env layer + disk | ❌ on this box (§1) |
| VisualWebArena | 910 | ❌ vision-mandatory by design (best VLM 16.4% vs human 88.7%) | Docker, as WebArena | ❌ NO-GO |
| WorkArena L1 / ++ | 33 / 682 | ✅ AXTree | ❌ **ServiceNow cloud SaaS**, external instance pool | ❌ NO-GO (local) |
| BrowserGym + AgentLab | unifies all above + MiniWoB | ✅ | standardizes interface + parallel study mgmt; does **not** remove per-benchmark hosting | integration layer only |
| MiniWoB++ | 104 | ✅ DOM | ✅✅ static HTML/JS, instant reset | 🟡 smoke-test only (toy/saturated) |

---

## 6. OSWorld — dataset, deployment, text-only, SOTA

### 6.1 Dataset & eval [verified-from-repo/paper + Epoch AI audit]
369 tasks (repo `test_all.json` drifted to 375): Office ~117 (Calc 47 / Impress 47 / Writer 23),
Daily ~79 (Chrome 47 / VLC 17 / Thunderbird 15), Professional ~49 (GIMP 26 / VS Code 23), OS 24,
Multi-app workflow ~106. Each task = NL instruction + initial-state setup (base VM snapshot + files
+ setup script) + an **execution-based checker** (134 unique checker functions). ~**10% of tasks
have serious flaws** and ~**10% depend on live internet** (Epoch AI). **OSWorld-Verified**
(2025-07-28) fixed 300+ issues, migrated to AWS (up to **50 parallel envs**), compressed the VM
image 50→25 GB — but kept updating, so it is a **moving target** (bad for longitudinal comparison).

### 6.2 Deployment (the crux) [verified-from-docs]
Every rollout = a **full GUI Ubuntu VM** (~**6 GB RAM, ~24 GB disk each**; reset = revert-to-snapshot).
Backends (VMware / VirtualBox / KVM-QEMU / Docker-QEMU / AWS) **all need hardware virtualization**;
docs: *"non-bare-metal server → Docker + KVM support recommended,"* check
`egrep -c '(vmx|svm)' /proc/cpuinfo > 0`. Without `/dev/kvm` → **TCG software emulation (~10–20% of
native)** → a GUI desktop running GIMP/LibreOffice/Chrome is unusable at scale.
- **128 workers = 128 VMs ≈ 768 GB RAM** vs our 131 GB available → cap **~20 VMs** (§1.2). OSGym ran
  128 replicas but needed **88 cores + 768 GB RAM + reflink disk**.
- **Cheap check on the box before anything:** `egrep -c '(vmx|svm)' /proc/cpuinfo` and `ls -l /dev/kvm`.
  If empty → OSWorld is not locally deployable, full stop.

### 6.3 Text-only / a11y feasibility [paper Table 3, secondary-corroborated]
An a11y-tree-only mode exists (`observation_type ∈ {a11y_tree, screenshot, screenshot_a11y_tree, som}`).
Baselines: **GPT-4 a11y-tree 12.24%** (best single baseline), GPT-4V screenshot 5.26%, screenshot+a11y
12.17%; **open text models at the floor: Llama-3-70B 1.61%, GPT-3.5 2.69%**; **human 72.36%**. Even
though the model has vision, a **text-configured ~35B on OSWorld sits ~1–4%** — Linux AT-SPI a11y trees
are notoriously incomplete for GIMP/Calc/Chrome canvas, so grounding silently needs pixels → **near-zero
optimization gradient** and failures a skill document cannot fix. Criterion E fails for a text config.

### 6.4 SOTA [leaderboard; all vision-based]
~12% (2024) → GTA1 45.2 → Agent S2.5 56.0 → CoACT-1 60.76 → Agent S3 63.5 → Simular 72.6 (Dec 2025) →
Claude 4.6 ~72.7% → 2026 vendor claims ~83–85%, vs **human 72.36%** → **saturating past human**.
Essentially 100% of the leaderboard is vision/pixel grounding; **no competitive text-only entrant exists.**

---

## 7. AndroidWorld & lighter alternatives

- **AndroidWorld (116 tasks, millions of param variants; state-based ADB/SQLite reward):** genuinely
  **text-friendly** — a11y-tree/view-hierarchy observation, and **text-only (30.6%) beat multimodal
  (25.4%)** with GPT-4-Turbo (human 80%). **But deployment disqualifies it:** the Android emulator needs
  **KVM/VT-x** (software/ARM fallback "much slower"), ~**4.5 GB + ~78 s per emulator**, **no native
  parallel-eval**, Docker support "experimental." Same nested-virt blocker as OSWorld. Watch **MobileGym**
  (emulator-free browser sim, 96-way parallel) — but it is screenshot/JSON, not text-only. [arxiv 2405.14573]
- **WebShop — the standout, already prepped** (see `webshop_PREP.md`): pure-Python in-process gym
  (`observation_mode='text'|'text_rich'`), no browser/Docker, deterministic reward = attribute/option
  overlap in [0,1] (Success = 1.0), 12,087 instructions / 500-task standard test, `rank_bm25` swap drops
  Java. Canonical testbed for ExpeL / Reflexion / LATS / AWM. Caveat: top agents match human *score*
  (~75.9) though success rate (~38%) trails human (~50%) → graded reward still gives signal.
- **ALFWorld** and **τ²-bench** (both already prepped) — text-native, deterministic, cheaply parallel,
  knowledge-repairable. **ScienceWorld** viable with JVM overhead. **TravelPlanner** deterministic + text
  but ~0.6% GPT-4 pass → no gradient (capability-bound), tiny train pool (45).

---

## 8. Six-criteria GO/NO-GO

Criteria: A task-pool · B deterministic scoring · C local high-concurrency · D cost/task ·
E knowledge-repairable · F comparability.

| Env | A | B | C | D | E | F | Verdict |
|---|---|---|---|---|---|---|---|
| **WebArena-Lite** | ✅ 165 (+647) | ✅ (Verified/Lite) | ❌ **disk-blocked (§1) + env rebuild** | 🟡 long DOM = high tokens | ✅✅ (+26.6 AgentOccam) | ✅✅ AWM/AutoManual/AgentOccam/WebRL | ❌ NO-GO on this box |
| **WebArena (full)** | ✅✅ | ⚠️ fuzzy judge minority | ❌❌ 250–350 GB + Map/GitLab warmup | 🟡 | ✅✅ | ✅✅ | ❌ NO-GO |
| **VisualWebArena** | ✅ 910 | ✅ | ❌ (as WebArena) | 🟡 | ❌ vision-bound | 🟡 | ❌ NO-GO |
| **WorkArena/++** | ✅ 715 | ✅ | ❌❌ ServiceNow cloud | 🟡 | ✅ enterprise procedures | 🟡 | ❌ NO-GO (local) |
| **MiniWoB++** | 🟡 104 | ✅ | ✅✅ trivial | ✅ cheap | ❌ toy/saturated | 🟡 | 🟡 smoke-test only |
| **OSWorld** | ✅ 369 | ⚠️ ~10% flawed | ❌❌ VM/rollout, KVM, 768 GB | ❌ full-VM, many steps | ❌ (text ~1–4%, grounding-bound) | 🟡 all vision | ❌ NO-GO |
| **AndroidWorld** | 🟡 116 | ⚠️ flaky (Pass@k) | ❌❌ emulator+KVM | ❌ heavy | ✅ (text>MM) | 🟡 | ❌ NO-GO |
| **WebShop** | ✅✅ 12,087 | ✅ [0,1] | ✅✅ in-process gym | ✅✅ cheap | ✅ query/option strategy | ✅✅ ExpeL/AWM/LATS | ✅ STRONG GO |

---

## 9. If WebArena is pursued anyway — architecture & effort

Not recommended on this box, but the exact path if a bigger-disk/bigger-RAM host becomes available:
1. **Subset:** WebArena-Lite (165 test + ~647 train) → Shopping / Shopping-Admin / GitLab / Reddit;
   drops the 90–100 GB Wikipedia + 60–90 min Map stack.
2. **Env layer:** do **not** use stock singleton containers for parallel rollouts. Adopt
   **AgentGym-RL's env-server + per-episode `/reset`** (portable, Docker-based) OR **WebServ's
   Incus + ZFS clone-per-rollout** (~1.74 GB/container). Both are separate infra to stand up.
3. **Scoring:** use **WebArena-Verified** (deterministic; removes the external GPT-4 fuzzy-judge, which
   would otherwise route through our own endpoint).
4. **Pre-check the box first (cheap):** `docker info`; `egrep -c '(vmx|svm)' /proc/cpuinfo`; `ls /dev/kvm`;
   confirm ≥300 GB free disk; confirm multi-GB images can be side-loaded (no HF/GitHub). If Docker is
   absent, the official path is blocked and only the Incus route remains.
5. **Effort: HIGH (multi-week)** — rebuilding the environment/reset layer, not wrapping an existing gym.
   Contrast with WebShop/ALFWorld/τ² (in-process parallelism already solved in the `*_PREP.md` plans).

---

## 10. Confidence flags

- Deployment/reset mechanics (WebArena README reset-after-812 + no-parallel-mention; OSWorld VM+KVM;
  AndroidWorld emulator+KVM; WebShop in-process gym): **verified-from-docs, HIGH.**
- Per-container/VM resource numbers (WebServ 1.74 GB, OSWorld 6 GB, OSGym 128×768 GB, AndroidWorld
  4.5 GB): **verified-from-papers, HIGH.**
- Server baseline (131 GB avail RAM, 149 GB free disk 98% full, GH/HF blocked): **verified from
  `README.md` / env-candidates audit, HIGH.**
- qwen3.6-35b-a3b multimodal: **HIGH** for the model; **whether our endpoint serves vision: UNVERIFIED.**
- 2026 SOTA rows ≥72% (both benchmarks): **vendor/aggregator, LOWER confidence.** Open ~35B prompted
  baselines and skill-work deltas: **verified-from-papers, HIGH.**
- OSWorld open-text a11y floor (Llama-3-70B 1.61%, GPT-3.5 2.69%): secondary-source-corroborated
  (paper Table 3), **MEDIUM-HIGH**; the qualitative "open text at the floor" is robust.

---

## 11. Sources

WebArena arxiv 2307.13854 · env README github.com/web-arena-x/webarena/blob/main/environment_docker/README.md ·
`parallel_run.sh` github.com/web-arena-x/webarena/blob/main/parallel_run.sh ·
WebArena-Verified github.com/ServiceNow/webarena-verified · openreview CSIo4D7xBG ·
VAB-Lite arxiv 2408.06327 · VisualWebArena arxiv 2401.13649 · WorkArena arxiv 2403.07718 ·
BrowserGym/AgentLab github.com/ServiceNow/BrowserGym · MiniWoB++ miniwob.farama.org ·
WebServ arxiv 2510.16252 · AgentGym-RL arxiv 2509.08755 · WebRL arxiv 2411.02337 ·
WebAgent-R1 arxiv 2505.16421 · AWM arxiv 2409.07429 · AutoManual arxiv 2405.16247 ·
AgentOccam arxiv 2410.13825 (Amazon Science PDF) · SkillWeaver arxiv 2504.07079 ·
OSWorld arxiv 2404.07972 · github.com/xlang-ai/OSWorld · osworld-v1.xlang.ai · xlang.ai/blog/osworld-verified ·
OSGym arxiv 2511.11672 · Epoch AI OSWorld audit epoch.ai/blog/what-does-osworld-tell-us-about-ais-ability-to-use-computers ·
AndroidWorld arxiv 2405.14573 · MobileGym mobilegym.dev · WebShop arxiv 2207.01206 · github.com/princeton-nlp/WebShop ·
qwen3.6-35b-a3b recipes.vllm.ai/Qwen/Qwen3.6-35B-A3B · llm-stats.com/models/qwen3.6-35b-a3b
