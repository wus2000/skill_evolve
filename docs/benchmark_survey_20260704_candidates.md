# Agentic Benchmark Candidate Survey — Expansion Beyond the 2026-07-03 Set (2026-07-04)

Follow-on to `benchmark_survey_20260703.md`. That doc covered the already-integrated / already-decided set
(SpreadsheetBench, Bird, ALFWorld, AppWorld; in-progress ScienceWorld; decided-and-rejected τ-/τ²-bench,
WebShop, WebArena/OSWorld, GAIA, SWE-bench, MLE-bench, AIME/GPQA/MMLU; and SkillsBench as an external eval).
This survey scans ~35 **additional** benchmarks (2024–2026) across six clusters plus four newly-surfaced candidates,
scoring each for the CSS harness (skill-document optimization for a **frozen ~35B open LLM** acting as a ReAct agent).

**Method:** six parallel sub-agents, one per cluster, each verifying pool sizes and scoring mechanisms from primary
sources. Top picks (MedAgentBench, WorkBench, BFCL, ColBench) and five arXiv IDs re-verified by hand this session.
Every load-bearing number is tagged `[paper]` / `[repo]` / `[leaderboard]` / `[secondary]`.

## 1. Rubric (A–G) — E is decisive

- **A. Task pool** ≥150 (ideally 300+), splittable into train/val/test
- **B. Deterministic auto-scoring** (execution / state-based unit tests; NO human eval; LLM-judge at most minor)
- **C. Fully-local deploy** on one Linux server: no real internet / no external paid API at eval; VMs undesirable,
  docker ok; must support 50–128 **concurrent** rollouts with **cheap state reset**
- **D. Moderate cost/task** (ReAct episode ≤50 turns; token cost)
- **E. ★Knowledge-repairable failure surface★** — failures stem from MISSING procedural/domain knowledge
  (API semantics, environment conventions, workflows, policy rules, pitfalls) a TEXT skill doc can fix.
  NOT raw reasoning/capability, NOT perception, NOT irreducible stochasticity. **This is the decisive axis.**
- **F. Community comparability** (skill/memory/experience-learning or prompt-opt prior work reports on it)
- **G. Headroom** for a frozen qwen3-35B: base score neither <10% (starvation) nor >85% (saturated)

Score key: ✓ good · ~ marginal · ✗ bad.

## 2. Broad scan table

### 2.1 SHORTLIST (clears the hard gates + a real knowledge-repairable surface)

| Benchmark | Pool [src] | A | B | C | D | **E** | F | G | One-line |
|---|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|---|
| **MedAgentBench** | 300 = 150 GET + 150 POST [paper] | ✓ | ✓ | ✓✓ | ✓ | **✓✓** | ~ | ✓ | Local FHIR-EHR ReAct agent; read-only eval → free reset; Qwen2.5-72B 51% |
| **WorkBench** | 690 = 69×10 [paper/repo] | ✓ | ✓✓ | ✓✓ | ✓ | **✓** | ~ | ✓ | In-memory pandas workplace sandbox; ms reset; GPT-4 43% |
| **BFCL v3 multi-turn** | 1,000 [blog] | ✓ | ✓ | ✓ | ✓ | **~/✓** | ✓ | ✓ | Local stateful FC backends; deterministic dual-check; de-facto FC standard |
| **Plancraft (text-only)** | 2,295 = 1145/570/580 [paper] | ✓ | ✓ | ✓ | ✓ | **✓** | ✗ | ✓ | Minecraft GUI planning; measured Qwen3-30B 0.27; RAG-search lever +0.40 |
| **InfiAgent-DABench** | 257 q / 461 sub-q [paper] | ~ | ✓ | ✓ | ✓ | **✓** | ✓ | ✓ | Local CSV data-analysis; exact-match; CodeLlama-34B 31 → Qwen-72B 60 |
| **OfficeBench** | 300 (LEGOMem 148/152) [repo] | ✓ | ✓ | ✓ | ✓/~ | **✓** | ✓/~ | ✓ | Multi-app office; docker; procedural-memory baseline exists |
| **ACEBench** | ~2,000 items [repo] | ✓ | ✓ | ✓ | ✓ | **✓** | ~ | ✓ | Local simulated FC; "Special" track = param/format/infeasibility conventions |
| **DA-Code** | 500 [paper] | ✓ | ✓ | ✓ | ~ | **✓** | ~ | ~ | Docker data-science; execution-scored; ceiling low (Deepseek-33B 11%) |
| **DABstep** | >450 [paper] | ✓ | ✓ | ✓ | ~ | **✓✓** | ~ | ~ | Payments-manual data analysis; purest E; bimodal headroom |
| **Terminal-Bench (v1 Core)** | 80 / v2 89 [repo] | ~ | ✓ | ✓ | ✓ | **✓** | ✓ | ~ | Docker terminal; GEPA/skill-lib prior work; small heterogeneous pool |
| **ToolSandbox** | 1,032 [paper] | ~ | ~ | ~ | ~ | **✓✓** | ✗ | ✓ | Richest E; but mandatory LLM user-sim per turn + RapidAPI search |
| **InterCode-CTF / Bash** | 100 / 200 [paper] | ~ | ✓ | ✓ | ✓ | **~** | ✓ | ~ | Cheapest deploy; picoCTF tool-playbooks; saturation/elicitation risk |

### 2.2 REJECT (with blocking gate + reason)

| Benchmark | Pool [src] | Blocking reason |
|---|---|---|
| **ColBench / SWEET-RL** | 1,000 + 500 test; 10k+10k train [paper] | E✗/G✗ — code-capability-bound (GPT-4o 16% base); LLM human-sim in-loop; RL-training bench |
| **GAIA2 / ARE** (Meta) | 1,120 = 800+320 [secondary] | G✗ — best OPEN model Kimi-K2 (~1T) only 21%; frozen 35B <10%. (E is best-in-cluster — revisit on model scale-up) |
| **TheAgentCompany** | 175 [paper/repo] | C✗ — self-hosted GitLab/Plane/RocketChat/ownCloud; no 128-concurrent cheap reset; partial LLM-judge |
| **CRMArena** | 1,170 [paper] | C✗ — live Salesforce cloud org (E✓✓ wasted) |
| **CRMArena-Pro** | 4,280 [paper] | C✗ — same Salesforce-cloud dependency + LLM user-sim |
| **Spider 2.0** | 632 (Lite 547) [paper/repo] | C✗/G✗ — bulk cloud-locked (BigQuery/Snowflake); o1 21% → 35B ~0; instance-not-shared knowledge; little over Bird |
| **DSBench** | 540 [paper] | E✗ — financial-modeling reasoning + ML-training capability; partial LLM-judge; expensive |
| **InsightBench** | 100 [paper] | A✗/B✗ — LLM-judge is the core scorer; open-ended "insights" |
| **Text2Analysis** | 2,249 [paper/repo] | D✗ — single-turn code-gen, not an agent loop; dataset release-gated |
| **AIOpsLab** | 48 [paper] | A✗/C✗ — K8s + DeathStarBench; reset = Helm redeploy; concurrency infeasible |
| **ITBench** | 94 [paper] | C✗/G✗ — K8s-heavy; SRE 11.4% frontier → 35B ~0 |
| **NYU CTF Bench** | 255 [repo] | E✗/G✗ — competition pwn/rev/crypto = creative exploit; 35B <10% |
| **CyBench** | 40 [paper] | A✗/E✗ — 40 tasks; professional exploit capability; near-saturated by frontier |
| **ScienceAgentBench** | 102 [paper] | A✗/E~ — too small to split; own oracle-knowledge ablation = **only +2pp** (evidence against E) |
| **DiscoveryWorld** | 120 (24 unique) [paper] | D✗/E✗ — up to 1,000 steps/episode; irreducible empirical discovery |
| **LAB-Bench** | 1,967 public MCQ [paper] | Format✗/E✗ — static MCQ, not a ReAct loop; perception+retrieval-bound |
| **BixBench** | 61 capsules / 205 q [paper] | B✗/G✗ — LLM-judge open-answer; agents don't beat no-notebook recall |
| **AgentClinic** | 215 text + 120 image [repo] | B✗ — doubly-stochastic reward (LLM judge over LLM patient); NEJM half is images |
| **Jericho / BALROG / NetHack** | 32–56 games [paper] | E✗/G✗ — exploration + long-horizon reasoning; normalized ceiling ~9.5% |
| **BabyAI-Text** | 5 task types [paper] | E~/G✗ — spatial nav; built to prove RL (not text) fixes grounding |
| **MindAgent / CuisineWorld** | 12–13 levels [paper] | A✗/G✗ — GPT-3.5 & Llama-70B both ~0 (cold-start) |
| **Robotouille** | ~100 + 30 [repo] | A~/E~ — small; async residual is reasoning; concurrency/local-LLM undocumented |
| **TALES** | 122 [paper] | Redundant with ALFWorld + signal-starved Jericho tail (ScienceWorld already integrated) |
| **Debug-gym** | Aider / Mini-nightmare / SWE-bench [paper] | E~/G✗ — underlying tasks SWE-bench-class capability (deferred); "how-to-pdb" skill too thin |
| **ToolHop** | 995 [paper] | E✗ — multi-hop decomposition reasoning; per-query bespoke tools (nothing transferable) |
| **StableToolBench / ToolBench** | ~765 solvable [repo] | B✗ — GPT-4 LLM-judge is the core metric; LLM-simulated APIs |
| **API-Bank** | 314 eval [paper] | A~/G~ — small eval pool; aging toward saturation |
| **NexusRaven / Nexus** | 9 task groups [repo] | A✗/E~ — tiny, single-turn schema/format only |
| **TravelPlanner, FiNER/Formula** | 1,225 / large [prior] | No new evidence changes the 07-03 rejects (constraint-reasoning / non-agentic NER) |

## 3. Shortlist deep-dives

### #1 — MedAgentBench (arXiv 2501.14654; Stanford ML Group; NEJM AI 2025) — re-verified this session
- **Task:** ReAct agent (AgentBench harness) manages a FHIR-compliant EHR over HTTP; each round emits a **GET**
  (query patient/labs/meds) or **POST** (record an order/observation/referral/med), ≤8 rounds, then finishes.
- **Pool & split:** **300 = exactly 150 GET + 150 POST**, 10 clinician-authored categories, >100 patients / >700k records
  `[paper]`. No official split → use **leave-categories-out** (only 10 categories, so coarse).
- **Scoring:** fully deterministic, **no LLM judge** `[paper]`. Query = compare to reference-solution answers;
  Action = hand-written **rule-based sanity checks on the POST payload** (JSON-loadable + correct FHIR fields). pass@1.
- **Deploy (decisive):** local Dockerized **HAPI FHIR JPA + H2** (`stanfordmlgroup/medagentbench`). Authors send **only GET
  to the server; POSTs are graded by inspecting the payload, never executed** → the server is **read-only static state,
  no per-task re-init** `[paper/repo]`. Reset is free (one-time ~90s boot). For 128-way concurrency, replicate the
  read-only server behind a load balancer (trivial — state never mutates).
- **Cost:** ≤8 rounds — cheap.
- **Headroom `[paper]`:** Claude-3.5-Sonnet-v2 **69.67%** (Q 85.3 / A 54.0); GPT-4o 64.0; **Qwen2.5-72B 51.33% (Q 38.7 / A 64.0)**;
  Llama-3.3-70B 46.3; **Gemma2-27B 19.3 (Q 38.7 / A 0.0)**; Mistral-7B 4.0. A frozen qwen3-35B should land ~40–50% overall.
- **E — what a skill doc fixes:** FHIR resource-construction conventions — *"to record serum K⁺, POST `/Observation` with
  `status:'final'`, category `laboratory`, `code`=LOINC 2823-3, `subject.reference:'Patient/{id}'`, ISO-8601 `effectiveDateTime`,
  `valueQuantity` in UCUM `mmol/L`"*; resource selection (MedicationRequest vs ServiceRequest vs Observation), required
  status/intent fields, patient-ID resolution (`GET /Patient?identifier=…` first), `_sort=-date&_count=1` for latest value.
  The maximal case for the CSS thesis — pure API/domain convention.
- **Risks:** (a) Gemma2-27B = **0% on the action half** → the most knowledge-repairable half may be cold-start for a small
  model; **probe the frozen qwen3-35B action-SR before committing**. (b) Bounded knowledge (~10 recipes) → saturation +
  memorization; a per-task split leaks the recipe → leave-categories-out. (c) Effective optimization pool ≈150 action tasks
  (query half is easier). (d) Confirm H2/HAPI throughput at 128 concurrent (solvable via read-only replicas).

### #2 — WorkBench (arXiv 2405.00823; COLM 2024) — re-verified this session
- **Task:** ReAct agent uses **26 read/write tools** over **5 sandbox DBs** (Email, Calendar, Web-Analytics, CRM,
  Project-Mgmt) to execute workplace tasks (send email, schedule meeting, update CRM, query analytics).
- **Pool & split:** **690 = 69 human templates × 10** `[paper/repo]`. **Split by template**, not by instance.
- **Scoring:** **outcome-centric, deterministic, no LLM judge** `[repo]` — compares the sandbox's final DB state to
  ground truth, with 3 outcome classes: correct / failed-but-harmless / **harmful side-effect**. Actively maintained
  (a 2026 v2 ground-truth correction exists).
- **Deploy (best-in-class C):** pure local Python, **in-memory pandas DBs**, no internet; reset = re-init dataframes
  (**milliseconds**); parallel to 128 trivially. Only external key is for the model endpoint (supply the local qwen).
- **Headroom `[paper/repo]`:** GPT-4 **43%**, Llama2-70B 3%, frontier ~98% (2026). Frozen 35B likely 15–40%.
- **E — what a skill doc fixes:** the dominant failure is **fabricating record IDs/dates instead of resolving them** —
  *"for every write, first call the matching search/analytics tool to get the exact primary key; never invent IDs;
  dates ISO-8601; match names case-insensitively"* + a **26-tool intent→tool selection table** + a **"no extra writes"**
  rule that directly targets the harmful-action metric.
- **Risks:** binary all-or-nothing scoring → sparse gradient; 69 templates → instance-split lets the skill memorize
  template answers (MUST split by template); frozen-35B base rate unmeasured (Llama2-70B 3% is a caution) → probe.

### #3 — BFCL v3 multi-turn (Berkeley/Gorilla) — re-verified this session
- **Task:** function calls against **local stateful backends** (GorillaFileSystem, TradingBot, TravelBooking,
  VehicleControl…) across turns; sub-tasks: base, missing-parameters (should ask), missing-functions (should refuse,
  not hallucinate), long-context, composite.
- **Pool `[blog]`:** **1,000 multi-turn entries** = 200 base + 200 each of the four augmented categories. (Avoid the
  single-turn AST/executable categories — they saturate.)
- **Scoring `[blog]`:** **deterministic, no LLM judge** — state-based (backend state after each turn) AND response-based
  (execution-path subset-match); correct only if both pass in all turns.
- **Deploy:** multi-turn backends are **local Python classes** `[repo, sub-agent]` — deterministic, cheap reset,
  trivially concurrent, no internet/docker.
- **F:** the **strongest comparability in the whole survey** — BFCL is the de-facto FC standard reported by most
  FC/prompt-opt work.
- **E — what a skill doc fixes:** *"obtain the target ID via `ls`/lookup before any file op; if a required function isn't
  in the provided list, declare the task infeasible — never invent a call; if a required argument is missing, ask rather
  than fabricate."*
- **Risks:** a nontrivial slice of multi-turn failure is **state-tracking reasoning** (not text-fixable); heterogeneous
  backends fragment the skill doc; surface overlaps the already-integrated AppWorld.

### Alternates (strong, each with a specific caveat)
- **Plancraft — text-only mode** (arXiv 2412.21033; COLM 2025). The most *scientifically* compelling alternate: the only
  candidate with large official splits **(1145/570/580)**, a **measured in-band ~30B base** (Qwen3-30B 0.27; Gemma-27B
  0.12→0.48 with tools), AND a **built-in knowledge-lever ablation** — RAG search over the bundled Minecraft wiki lifts
  Llama-70B 0.18→~0.67 (Δ≈**+0.40**) `[paper]`. Deterministic goal-check, docker + offline wiki, ≤30 steps. Skill-doc
  fixes: recipes/smelt chains, the `move [I4]->[A1]` slot-notation convention, and a feasibility rule (naively enabling
  `impossible` *drops* Qwen3 ~0.30 via over-declaring). **Caveats: AVOID image mode (all VLMs <1%, perception wall);
  no prior skill/memory baselines (F weak — reviewer will ask "why not just RAG?"); overlaps the ALFWorld genre.**
  *Correction to a common premise:* primary sources show **AutoManual and AWM do NOT use TextCraft** (AutoManual =
  ALFWorld/MiniWoB++/WebArena; AWM = WebArena/Mind2Web); TextCraft's real lineage is ADaPT (arXiv 2311.05772, NAACL'24).
- **InfiAgent-DABench** (arXiv 2401.05507; ICML 2024). Cleanest *measured* headroom of all (CodeLlama-34B 31 / Qwen-72B 60 /
  GPT-4 79%), deterministic `@answer[…]` exact-match (a GPT-3.5 step only *reformats*, does not judge), fully local
  CSV+sandbox, ≤5 turns. Fixes stats/pandas conventions (ddof, IQR interpolation, dropna-before-stats, `scipy.stats`).
  **Caveat: only 257 tasks (thin 3-way split); overlaps Bird/SpreadsheetBench tabular coverage.**
- **OfficeBench** (arXiv 2407.19056). Docker, deterministic (exact/fuzzy/execution match), Qwen2-72B 21% (headroom), and
  uniquely **already has a procedural-memory baseline + 148/152 split (LEGOMem, arXiv 2510.04851)** for comparability.
  Fixes app-switching + operation vocabulary + anti-hallucination/anti-redundancy rules. **Caveat: 300 tasks; the 112
  three-app tasks are long-horizon capability-bound.**
- **DABstep** (arXiv 2506.23719; Adyen×HF). If E is weighted hardest, the **purest knowledge-repairable surface** — Hard
  tasks fail precisely because the agent doesn't know the payments-domain manual (fee formulas, MCC semantics).
  Deterministic factoid check, fully local. **Caveat: bimodal headroom (Easy ~90% / Hard ~16% frontier).**
- **ToolSandbox** (arXiv 2408.04682; Apple). Single richest E in the tool-use cluster (state-dependency, canonicalization,
  insufficient-info handling) + deterministic milestone scoring, but held back by a **mandatory per-turn LLM user-simulator
  + RapidAPI search** → costly rollouts + conversational stochasticity. Viable only if you stub RapidAPI and self-host the
  user-sim on a local model.

## 4. New candidates surfaced + re-assessments

- **ColBench / SWEET-RL** (Meta; arXiv 2503.15478) — re-verified this session. Best-looking new find on paper: **1,000
  backend-programming + 500 frontend-design test tasks (+10k+10k train)**; Backend scored by **10 hidden unit tests
  (deterministic 0/1)**, Frontend by CLIP-embedding cosine similarity; runs locally; ≤10 turns. **REJECT for CSS:**
  (a) failure surface is **code-writing capability + underspecification-resolution**, not missing knowledge — base
  zero-shot Llama-8B 6.9% / Llama-70B 14.8% / GPT-4o 16.2% `[paper]`; a frozen 35B ~15% → starvation; (b) a **mandatory
  LLM "human collaborator" simulator** (Llama-3.1-70B backend, Qwen2-VL-72B frontend) sits in-loop at eval → cost + noise;
  (c) explicitly an **RL-training** benchmark, no prompt-opt comparability.
- **Debug-gym** (Microsoft; arXiv 2503.21557) — REJECT. Interactive debugging with pdb tools, but its benchmarks are
  **Aider / Mini-nightmare / SWE-bench** — the meaty half is SWE-bench-class capability (team deferred SWE-bench), the
  rest are small hand-crafted sets. The "how to drive pdb" workflow-skill angle is real but too thin to carry a benchmark.
- **BALROG** (ICLR 2025; arXiv 2411.13543) — REJECT; this is the canonical LLM wrapper for the "Crafter/Craftax" question.
  Aggregates BabyAI / Crafter / TextWorld / Baba-Is-AI / MiniHack / NetHack; deterministic 0–100 scoring, procedurally
  generated (good), but E is exploration/long-horizon-planning-bound and NetHack/MiniHack are signal-starved for a 35B —
  same family as the rejected Jericho/BabyAI.
- **SkillsBench** (arXiv 2602.12670) — **already documented in `benchmark_survey_20260703.md`** as an external eval
  (87 tasks × 8 domains × deterministic verifiers; curated skills +16.6pp). Not a training-loop candidate; keep it as the
  "CSS-learned skill vs human-curated skill" comparison line, not a new integration target. (My independent pool number,
  ~86–87 tasks, matches.)
- **ClawsBench** (arXiv 2604.05172, 2026) — flagged, **not deep-verified**. Simulated workspaces, capability+safety of
  productivity agents; potentially local. Worth watching; could not confirm pool/scoring from primary sources this pass.
- **Re-assessments requested by lead:** **ScienceAgentBench** (arXiv 2410.05080) — REJECT; 102 tasks and, decisively,
  **its own oracle-expert-knowledge ablation moved Success Rate only ~+2pp** `[paper]` — primary-source evidence *against*
  knowledge-repairability. **DiscoveryWorld** (arXiv 2406.06769, NeurIPS'24 Spotlight, AI2) — REJECT; 120 tasks (24 unique
  configs) and up-to-1,000-step episodes measuring irreducible empirical discovery. **TravelPlanner / InterCode / FiNER** —
  no new evidence changes the 07-03 calls (InterCode stays a marginal-shortlist for cheap deploy; TravelPlanner
  constraint-reasoning and FiNER non-agentic-NER stay rejected).

## 5. Final ranked recommendation — deep-audit these

1. **MedAgentBench** — the strongest single addition. Clears every hard gate cleanly (deterministic no-judge scoring,
   local Docker, **free read-only reset**, ≤8 rounds), lands a frozen ~35B in a near-perfect 40–50% band, and its failure
   surface is the textbook CSS case (FHIR API construction conventions). Adds a **genuinely new domain** (healthcare/EHR)
   orthogonal to the existing set. **Gate:** measure the frozen qwen3-35B **action-half** base rate first (Gemma-27B's 0%
   is the one warning sign); adopt a leave-categories-out split.
2. **WorkBench** — the best *deployment* fit in the whole survey (in-memory pandas, millisecond reset, 128-concurrent
   trivially), deterministic outcome-state scoring with a bonus side-effect-safety metric, a 690-task pool, and a
   knowledge-repairable ID-resolution/tool-selection surface. Lowest integration risk, highest throughput. **Gate:**
   split-by-template + a base-rate probe (binary scoring → sparse gradient).
3. **BFCL v3 multi-turn** — the safest hard-gate choice and, decisively, the **only candidate with top-tier community
   comparability (F)** — CSS gains situate directly against the whole FC/prompt-opt literature. Fully local Python
   backends, deterministic dual-check scoring, 1,000 entries. Accept that part of its surface is reasoning and that it
   partially overlaps AppWorld.

**Optional 4th, to *demonstrate* the CSS thesis rather than just test it — Plancraft (text-only):** the only option that
ships a controlled knowledge-injection baseline (RAG-search Δ≈+0.40) alongside large official splits and a measured
in-band ~30B base — a built-in "how much does the right text help" yardstick, at the cost of thin prior-work comparability.

**Do not spend audit budget on:** ColBench (capability-bound + LLM-in-loop), GAIA2/ARE (starved at 35B — revisit only on
model scale-up; its E is best-in-class), the Salesforce/CMU enterprise stack (cloud/heavy), the K8s SRE and CTF benches
(undeployable at concurrency and/or capability-bound), or ScienceAgentBench (its own ablation argues against E).

## Sources

Primary sources; `[verified]` = arXiv paper / official repo read this session or by the cluster sub-agents from primary
text; `[secondary]` = inferred from a non-primary page and not independently confirmed.

**Top picks (re-verified by hand this session):**
- MedAgentBench — https://arxiv.org/abs/2501.14654 · https://github.com/stanfordmlgroup/MedAgentBench · NEJM AI https://ai.nejm.org/doi/full/10.1056/AIdbp2500144 `[verified]`
- WorkBench — https://arxiv.org/abs/2405.00823 · https://github.com/olly-styles/WorkBench `[verified]`
- BFCL v3 multi-turn — https://gorilla.cs.berkeley.edu/blogs/13_bfcl_v3_multi_turn.html · leaderboard https://gorilla.cs.berkeley.edu/leaderboard.html `[verified]`
- ColBench / SWEET-RL — https://arxiv.org/abs/2503.15478 · https://github.com/facebookresearch/sweet_rl `[verified]`
- Plancraft — https://arxiv.org/abs/2412.21033 · https://github.com/gautierdag/plancraft `[verified]`
- ScienceAgentBench — https://arxiv.org/abs/2410.05080 · https://github.com/OSU-NLP-Group/ScienceAgentBench `[verified]`
- DiscoveryWorld — https://arxiv.org/abs/2406.06769 · https://github.com/allenai/discoveryworld `[verified]`
- LAB-Bench — https://arxiv.org/abs/2407.10362 · https://github.com/Future-House/LAB-Bench `[verified]`
- ADaPT (TextCraft origin) — https://arxiv.org/abs/2311.05772 · https://github.com/archiki/ADaPT `[verified]`

**Tool-use cluster:**
- ToolSandbox — https://arxiv.org/abs/2408.04682 `[verified: paper]`
- ACEBench — https://arxiv.org/abs/2501.12851 `[verified: paper]`
- ToolHop — https://arxiv.org/abs/2501.02506 `[verified: paper]`
- StableToolBench — https://arxiv.org/abs/2403.07714 · ToolBench/ToolLLM https://arxiv.org/abs/2307.16789 · MirrorAPI https://arxiv.org/abs/2503.20527 `[verified: paper/repo]`
- API-Bank — https://arxiv.org/abs/2304.08244 `[verified: paper]`

**Enterprise cluster:**
- TheAgentCompany — https://arxiv.org/abs/2412.14161 · https://github.com/TheAgentCompany/TheAgentCompany `[verified: paper/repo]`
- CRMArena — https://arxiv.org/abs/2411.02305 · CRMArena-Pro — https://arxiv.org/abs/2505.18878 `[verified: paper]`
- OfficeBench — https://arxiv.org/abs/2407.19056 · https://github.com/zlwang-cs/OfficeBench · LEGOMem https://arxiv.org/abs/2510.04851 `[verified: paper/repo]`
- Text2Analysis — https://arxiv.org/abs/2312.13671 · https://github.com/microsoft/Text2Analysis `[verified: paper/repo]`

**DevOps / terminal / CTF cluster:**
- Terminal-Bench — https://www.tbench.ai/leaderboard `[verified: leaderboard]` · arXiv 2601.11868 `[secondary]`
- AIOpsLab — https://arxiv.org/abs/2501.06706 `[verified: paper]`
- ITBench — https://arxiv.org/abs/2502.05352 · https://github.com/itbench-hub/ITBench `[verified: paper/repo]`
- InterCode — https://arxiv.org/abs/2306.14898 · Palisade "Hacking CTFs with Plain Agents" https://arxiv.org/abs/2412.02776 `[verified: paper]`
- NYU CTF Bench — https://arxiv.org/abs/2406.05590 · https://github.com/NYU-LLM-CTF/NYU_CTF_Bench `[verified: paper/repo]`
- CyBench — https://arxiv.org/abs/2408.08926 `[verified: paper]`

**Data-analysis cluster:**
- DSBench — https://arxiv.org/abs/2409.07703 `[verified: paper]`
- DA-Code — https://arxiv.org/abs/2410.07331 `[verified: paper]`
- InsightBench — https://arxiv.org/abs/2407.06423 `[verified: paper]`
- DABstep — https://arxiv.org/abs/2506.23719 · https://huggingface.co/blog/dabstep `[verified: paper/blog]`
- Spider 2.0 — https://arxiv.org/abs/2411.07763 · https://github.com/xlang-ai/Spider2 `[verified: paper/repo]`
- InfiAgent-DABench — https://arxiv.org/abs/2401.05507 `[verified: paper]`

**Science / medical cluster:**
- BixBench — https://arxiv.org/abs/2503.00096 `[verified: paper]`
- AgentClinic — https://arxiv.org/abs/2405.07960 `[verified: paper]`

**Embodied / games / sim-app cluster:**
- BALROG — https://arxiv.org/abs/2411.13543 `[verified: paper]`
- Debug-gym — https://arxiv.org/abs/2503.21557 · https://github.com/microsoft/debug-gym `[verified: paper/repo]`
- GAIA2 / ARE (Meta) — arXiv 2602.11964 `[secondary]`
- SkillsBench — https://arxiv.org/abs/2602.12670 (see `benchmark_survey_20260703.md`) `[secondary]`
- ClawsBench — arXiv 2604.05172 `[secondary]`
