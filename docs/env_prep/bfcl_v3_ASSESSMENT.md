# BFCL v3 Multi-Turn — Integration Assessment (CONDITIONAL GO, leaning GO)

Deployment-and-fit assessment of **BFCL — the Berkeley Function Calling Leaderboard**
(Patil, Mao, Yan et al., UC Berkeley; ICML 2025), specifically the **v3 multi-turn** track,
as a task environment for the CSS skill-optimization harness. Companion to the `*_PREP.md`
files and the `webarena_osworld_ASSESSMENT.md`. Facts are tagged **[verified-from-repo]**
(I parsed the live `main` repo, eval code, and CHANGELOG via `gh`/`curl` on 2026-07-04),
**[verified-from-blog]** (Gorilla blog 13), **[verified-from-paper]** (ICML paper / cited
arXiv), **[secondary]** (leaderboard tracker / aggregator / forum, gathered by research
sub-agents), or **[inferred]**. Assessed 2026-07-04.

- Repo: https://github.com/ShishirPatil/gorilla/tree/main/berkeley-function-call-leaderboard (Apache-2.0)
- v3 multi-turn blog: https://gorilla.cs.berkeley.edu/blogs/13_bfcl_v3_multi_turn.html
- Paper (ICML 2025, PMLR v267 patil25a): https://proceedings.mlr.press/v267/patil25a.html
- Leaderboard (now V4): https://gorilla.cs.berkeley.edu/leaderboard.html
- pip package: `bfcl-eval` (PyPI)

Harness requirements this is judged against: (A) **≥150 tasks** splittable train/val/test;
(B) **deterministic auto-scoring, no LLM judge**; (C) **fully local, 50–128 concurrent
rollouts** with cheap reset; (D) **moderate cost/task**; (E) **knowledge-repairable** failure
surface (procedural/convention knowledge a text doc fixes, not raw reasoning); (F) **community
comparability**; (G) **headroom** for a frozen ~35B (base in the 10–85% band).

---

## 0. TL;DR verdict

| Facet | Status | One-line |
|---|---|---|
| **Overall** | 🟢 **CONDITIONAL GO (leaning GO)** | Best mechanical fit of any candidate for a tool-use convention playbook; scope to the procedural categories + relative-lift framing. |
| Dataset / splits (A) | ✅ STRONG | **800 active entries** (200 × base/miss_func/miss_param/long_context) on a clean 4×4 (challenge × primary-domain) grid; +200 unused `composite`. |
| Scoring (B) | ✅ STRONG | Deterministic state + call-subsequence checks; **no user-simulator LLM** (user turns are pre-scripted). |
| Deployment (C) | ✅ PASS | Pure-Python importable backends, **no Docker/VM/KVM**, free per-entry reset; the disk/virt walls that killed WebArena/OSWorld do not apply. |
| Cost (D) | ✅ PASS | ~8–12 model calls/entry, 1–2 backends per entry; `long_context` is the only heavy slice. |
| Failure surface (E) | 🟡 PASS (nuanced) | `miss_param` / `miss_func` are literally convention categories → ideal for a skill doc; long-context / deep-state are capability-bound. |
| Comparability (F) | ✅ STRONG | The de-facto function-calling benchmark — but popularity is also the contamination risk. |
| Headroom (G) | ✅ PASS | Qwen3-32B native-FC ≈ **43% multi-turn** (best proxy for frozen qwen3.6-35b-a3b); ~35pp reachable. |

**Bottom line:** BFCL multi-turn clears every mechanical criterion that sinks the GUI
environments, and its `miss_param` (ask-don't-guess) and `miss_func` (refuse-absent-tool)
categories are almost purpose-built to demonstrate the CSS thesis that a written playbook
installs *calling conventions* into a frozen model. Adopt it as the **restraint / clarification
/ irrelevance-detection complement to AppWorld**, under three conditions: (1) report
frozen-model *relative* lift (with-skill vs without-skill), **not** absolute SOTA, because the
whole dataset is public with no held-out test; (2) budget modest integration effort to drive
the backends from the CSS rollout loop; (3) expect a **bounded text ceiling (~+5–15pp
precedent)** concentrated in the procedural categories. Top 3 risks in §9–§12.

---

## 1. Deployment fit under the server baseline (decisive-positive)

Where WebArena died on **disk + parallel-safe reset** and OSWorld on **VM-per-rollout RAM/disk
+ KVM** (`webarena_osworld_ASSESSMENT.md` §1), BFCL multi-turn sidesteps all of it. [verified-from-repo]

1. **No containers, no VMs, no nested virt.** The multi-turn "backends" are eight ordinary
   Python classes (`bfcl_eval/eval_checker/multi_turn_eval/func_source_code/*.py`). A rollout
   instantiates them in-process. **Reset is free**: a fresh object built from the entry's
   `initial_config` dict — no `docker run`, no snapshot revert. [verified-from-repo]
2. **Tiny disk footprint.** The entire multi-turn dataset is ~1.6 MB of JSONL (4 × ~0.4 MB);
   the pip package + deps are a normal Python install. None of the 149 GB-free / 98 %-full disk
   pressure that blocked WebArena-Lite applies. [verified-from-repo]
3. **Network-block-friendly install.** The server has **GitHub + huggingface.co blocked**, but
   **PyPI is reachable** (per the AppWorld prep). `pip install bfcl-eval` pulls the code *and*
   the bundled data from PyPI, avoiding a GitHub clone; a git bundle from the Mac is the fallback
   if a repo checkout is wanted. [inferred from server baseline + verified-from-repo]
4. **Plugs into the existing vLLM endpoint.** `--skip-server-setup` +
   `LOCAL_SERVER_ENDPOINT/PORT` (or `REMOTE_OPENAI_BASE_URL/KEY`) points BFCL at our already-running
   qwen3.6-35b-a3b OpenAI-compatible server; no second model, no GPU contention beyond the
   rollouts themselves. [verified-from-repo]

**Net:** deployment is a **PASS with room to spare** — the opposite of the GUI benchmarks.

---

## 2. Dataset anatomy

The repo now labels the files `BFCL_v4_multi_turn_*`, but the **multi-turn content is
unchanged from v3** — V4 only *added* separate agentic tracks (web-search, memory). Counts
below are from parsing the live `main` data files. [verified-from-repo]

### 2.1 Active multi-turn pool (scored)

| Category (`--test-category` id) | Entries | Turns/entry (min–max, mean) | What it tests |
|---|---|---|---|
| `multi_turn_base` | **200** | 1–7, **3.7** | core sequential/stateful tool use |
| `multi_turn_miss_func` | **200** | 2–8, **4.7** | a needed tool is withheld until a later turn — model must recognize the gap and **hold off** |
| `multi_turn_miss_param` | **200** | 2–8, **4.7** | a required argument is absent — model must **ask** rather than guess |
| `multi_turn_long_context` | **200** | 1–7, **3.7** | hundreds of files / thousands of records injected as distractors |
| **TOTAL ACTIVE** | **800** | — | scored on the board |
| `multi_turn_composite` | **200** | — | combines all three challenges — ships in `data/unused_datasets/`, **NOT scored** |

- **Authoring:** hand-authored, expert-human-verified triples of *(Question, Function List,
  Initial Config)*; questions seeded from **Persona Hub** for persona diversity. [verified-from-blog]
- **Entry schema:** `{id, question (list of pre-scripted user turns), initial_config, path,
  involved_classes, excluded_function}`. [verified-from-repo]
- **The unused `composite` 200** is a real, authored, human-verified set sitting outside the
  scored pool → a convenient **fresher held-out probe** less exposed to leaderboard-driven
  contamination (see §9). [verified-from-repo]

### 2.2 Two clean split axes (train/val/test carving)

The 800 entries are balanced on **two orthogonal axes**, each 4×200: [verified-from-repo]

- **Challenge type** — base / miss_func / miss_param / long_context, 200 each.
- **Primary backend domain** — each entry has exactly one primary domain, 200 each:
  **GorillaFileSystem, VehicleControlAPI, TradingBot, TravelAPI**. Companion backends are mixed
  in (**TwitterAPI 156, MessageAPI 160, TicketAPI 124, MathAPI 100** occurrences across the 800).

So it's effectively a **4 × 4 grid of ~50-entry cells**. Split options: hold out whole
categories (e.g. train on base+long_context, test on miss_param+miss_func to probe convention
transfer), hold out whole domains (train on 3 domains, test on the 4th for cross-domain
generalization), or stratified 60/20/20 within cells. **Criterion A is comfortably satisfied.**

### 2.3 Backends — 8 stateful pure-Python classes, 128 functions

| Backend class | Functions | Role |
|---|---|---|
| `vehicle_control` (VehicleControlAPI) | 22 | primary |
| `trading_bot` (TradingBot) | 20 | primary |
| `gorilla_file_system` (GorillaFileSystem) | 18 | primary |
| `travel_booking` (TravelAPI) | 18 | primary |
| `math_api` (MathAPI) | 17 | companion |
| `posting_api` (TwitterAPI) | 14 | companion |
| `message_api` (MessageAPI) | 10 | companion |
| `ticket_api` (TicketAPI) | 9 | companion |
| **TOTAL** | **128** | 8 stateful classes |

Fresh instance per entry, no network / filesystem side effects in the multi-turn track. For
scale contrast, **AppWorld exposes 457 real-ish APIs** — BFCL is ~3.6× smaller and fully
simulated. [verified-from-repo / project]

---

## 3. Scoring mechanics (deterministic; corrects the "force-execution" belief)

Read directly from `eval_checker/multi_turn_eval/multi_turn_checker.py`. [verified-from-repo]

- **No user-simulator LLM.** The `question` field is a list of **pre-scripted static user
  turns**, delivered in order regardless of what the model does. Unlike tau-bench / τ², there
  is **no second model in the loop** → fully deterministic and cheaper. [verified-from-repo]
- **The model drives the whole episode on its own state.** Its emitted function calls are
  executed via `execute_multi_turn_func_call()` against **its own** backend instances
  (`model_instances`); this state **carries forward** turn to turn. Ground-truth calls run on a
  **separate** set (`ground_truth_instances`) purely to compute the comparison target.
- **There is NO per-turn ground-truth state injection.** A wrong turn corrupts the model's own
  state and typically fails the entry — the harness does **not** reset the model to the GT state
  before the next turn. (This corrects the common "GT is force-executed per turn" belief: only
  the *user prompts* are forced; the *backend state the model acts on is its own*.) This is the
  load-bearing fact for rollout design — replay fixed user turns, let the model act freely, and
  score against a parallel reference instance.
- **Two per-turn checks:**
  - **State-based** (`state_checker` / `_compare_instances`) — all non-private attributes of
    model vs GT instances must match **exactly** (captures write/delete correctness).
  - **Response-based** (`response_checker`) — the GT's minimal required calls must appear as an
    **unordered subsequence** of the model's accumulated calls (`_is_subsequence_unordered`),
    tolerating extra exploration/recovery steps.
- **Per-entry binary pass: every turn must pass both checks.** Malformed / missing / extra calls
  → state diverges or the required subsequence is absent → fail. `miss_func` correct behavior =
  do **not** call the absent tool and wait; `miss_param` correct behavior = **ask** for the
  missing argument. [verified-from-repo / verified-from-blog]

**Criterion B is STRONG:** deterministic, execution-grounded, no LLM judge anywhere in the
multi-turn path.

---

## 4. Concurrency & resource math

- **`--num-threads` for parallelism** (default 1, no hard cap — bounded by endpoint throughput).
  Because the backends are in-process Python objects, 50–128-way concurrency is limited by the
  vLLM prefill/KV budget, **not** by the environment. [verified-from-repo]
- **Backends are importable**, so CSS can bypass bfcl's single-model runner entirely and call
  the backend classes from its own 50–128-worker rollout loop (the integration-effort item —
  modest, since there is no server/reset machinery to reproduce). [verified-from-repo / inferred]
- **Per-entry cost:** ~4 turns × several tool-call steps ≈ **~8–12 model generations/entry**;
  each entry exposes only ~15–40 function schemas (1–2 backends) → ~3–8K prompt tokens growing
  over the episode, ~1–3K output total. A **full 800-entry pass ≈ ~8K model calls**; a
  ~150–200-entry val pass ≈ ~2K calls — cheap for iterative optimization. [inferred from verified turn/backend counts]
- **The one heavy slice:** `long_context` injects "hundreds of files / thousands of records"
  → 20–50K-token prompts. Run it at lower concurrency or treat it as optional. On a 3B-active
  MoE (qwen3.6-35b-a3b) the base/miss_* categories are comfortable at high concurrency.
  [verified-from-blog + inferred]

**Criterion C/D: PASS.** No parallel-reset crux (contrast WebArena), no VM RAM wall (contrast
OSWorld).

---

## 5. Leaderboard & headroom

The **official board is now V4 (agentic) but keeps the v3 multi-turn category inside Overall.**
The live board is a JavaScript SPA that WebFetch cannot read, so **every cell below is
[secondary]** (paper/aggregator); the cleanest anchor is a paper measurement, not the board.
An exact live-board scrape is open follow-up #1 (§11).

| Model | Overall | **Multi-turn** | Mode | Source |
|---|---|---|---|---|
| Claude Opus / Sonnet 4.5 | ~85–90 (V4) | **~77–81** | native FC | [secondary] |
| Gemini 3 Pro | — | **~76.8** | native FC | [secondary] |
| GPT-5.2 | — | **~60** | native FC | [secondary] |
| **Qwen3-32B (key anchor)** | **69.25** | **43.1** | **native FC** | [secondary: LoopTool, arXiv 2511.09148] |
| Qwen3-30B-A3B | — | ~37 (low-conf) | prompt | [secondary] |
| Qwen2.5-32B | — | ~32 | FC | [secondary] |
| xLAM-2-70B (MT-tuned) | 78.2 | **75.1** | FC | [secondary: APIGen-MT] |

- **Frontier ~60–81% vs open-30B ~32–43% → a ~25–40pp gap.** No direct qwen3.6-35b-a3b number
  exists; **Qwen3-32B native-FC ≈ 43% multi-turn / 69% overall** is the best proxy. Multi-turn
  is the single weakest column for a 30B (single-turn/live ~78–89% vs MT ~43%). The base sits
  squarely in the 10–85% headroom band with ~35pp reachable. [secondary]
- Only **purpose-built MT finetunes** (xLAM-2, InfTool) close the gap; general open models do not.
- **Confidence caveat:** Claude's BFCL numbers swing wildly by harness (an AST-parser artifact
  once put Opus 4 at 25% overall), and cross-harness *overall* numbers are explicitly
  **not comparable across BFCL versions**. Trust the *within-paper* FC multi-turn deltas, not
  the aggregator totals. [secondary]

**Criterion G: PASS.**

---

## 6. E-criterion — failure surface & text-optimization precedent

### 6.1 Failure modes: procedural (text-fixable) vs capability-bound

| Failure mode | Text-fixable? | Evidence |
|---|---|---|
| **Clarify-don't-guess (`miss_param`)** | **PROCEDURAL — ideal** ("if a required arg is absent and not inferable, ask") | [verified-from-paper: FunReason-MT] |
| **Refuse/hold absent tool (`miss_func`)** | **PROCEDURAL** ("if no tool matches, say so; never fabricate a call") | [verified-from-paper / verified-from-blog] |
| Sequencing / loop-avoidance / premature-stop | **Mostly procedural** — in a tuned model, 32.5% of errors were redundant loops, 13.2% premature stops | [secondary: NVIDIA dev-forum analysis] |
| Explore-before-mutate state discipline | Mixed — the *convention* ("read current state before any write") is instructable | [verified-from-blog] |
| Hallucinated function/param names | Partial — "only call schemas present verbatim" dents it | [secondary] |
| **Long-context retrieval / deep >5-turn state** | **CAPABILITY-bound — text-resistant** | [verified-from-blog] |

**Load-bearing point:** BFCL multi-turn **"deliberately avoids prompt engineering and ReAct"**
to measure base capability [verified-from-blog]. So the leaderboard numbers are a
**no-scaffolding floor** — a method explicitly allowed to inject a playbook starts with slack
the published numbers hide. The largest text-fixable slices — `miss_param`, `miss_func`,
loop-avoidance, explore-before-mutate — are exactly the CSS target; long-context and deep-state
are the capability residual to *not* budget for.

### 6.2 Prior optimization work — what text can vs can't recover

| Work | Base model | Base MT% | Result MT% | Method | Source |
|---|---|---|---|---|---|
| **ReMe** (procedural memory, **no weight updates**) | Qwen3-8B | 59.6 | **68.0 (+8.45)** | retrieved procedural memory into context | [verified-from-paper: arXiv 2512.10696] |
| **GEPA** (reflective prompt evolution, weights frozen) | agent prompt | — | **+8.7 / +13.9%** | prompt-only, on **tau-bench** multi-turn tool use | [verified-from-paper: arXiv 2507.19457] |
| — upper bounds from **fine-tuning** — | | | | | |
| **FunReason-MT** | Qwen3-4B | **15.75** | **56.5 (+40.75)** | SFT+RL | [verified-from-paper: arXiv 2510.24645] |
| **Magnet** | Qwen2.5-Coder-14B | **5.38** | **37.88 (+32.5)** | graph-synth + mDPO | [verified-from-paper: arXiv 2503.07826] |

**Read:** the only weight-frozen text/memory precedents (ReMe on BFCL-MT, GEPA on tau-bench)
land in a **~+5–15pp** envelope — the honest expectation for a playbook on a frozen ~35B.
Fine-tuning's +30–40pp mostly installs *tool-result→next-action* mechanics into weights (a
capability install a prompt can't replicate); text recovers only the **procedural** slice —
which is the point, and where the transferable, general headroom actually lives.
**Criterion E: PASS (nuanced).**

---

## 7. Overlap with AppWorld — complementary, not redundant

| | AppWorld [project] | BFCL multi-turn [verified-from-repo] |
|---|---|---|
| Action form | REPL **Python code** over 457 APIs | discrete **JSON function calls**, 8 backends / 128 funcs |
| Horizon | long (dozens of steps) | short (mean ~4 turns) |
| Evaluation | unit tests on final DB state | per-turn state + call-subsequence |
| Dominant failure | code orchestration, data flow, API discovery | **when-to-ask, when-to-refuse, irrelevance detection, restraint, native-FC discipline** |

The `miss_param` / `miss_func` / irrelevance axis — behavioral **restraint** and
**clarification** — is **largely absent from AppWorld**, which assumes the task is doable and
you write code to do it. That is BFCL's distinct, publishable value-add. The **overlap** is in
the shared *mechanic* (frozen-model text playbook + deterministic multi-turn state eval), so the
honest framing is: if CSS already shows procedural lift on AppWorld's orchestration surface,
BFCL is the **restraint/clarification/convention** complement — pitch it around `miss_param` /
`miss_func`, not as "another tool-use env."

---

## 8. Auxiliary pools & the V4 tracks

- **v1/v2 single-turn** (AST simple/multiple/parallel, live/non-live, **irrelevance/relevance**):
  thousands of entries, but single-turn and near-saturated for capable models. Irrelevance
  detection is a convention-fixable behavior but low-headroom. Usable as an auxiliary pool, not
  the main event. [verified-from-repo]
- **V4 agentic tracks** [verified-from-repo / secondary]:
  - `web_search` — needs **live internet** (multi-hop search + error recovery) → **unsuitable**
    for a fully-local harness.
  - `memory` — uses **local Python memory backends** (`memory_kv` / `memory_vector` /
    `memory_rec_sum`); a different task (memory management), newer and less-established, but
    local-friendly if a memory-optimization angle is ever wanted.
  - `format_sensitivity` — prompt-format robustness; niche.
  - V4 multi-turn == v3 multi-turn (same four categories, relabeled).

---

## 9. Contamination & mitigations (flag)

**The entire dataset — questions, ground truth, and backend source — is public** in the
Apache-2.0 repo and on HuggingFace, with **no private held-out test set** and continuous public
ground-truth patching (dozens of CHANGELOG fixes, e.g. `multi_turn_base_154` GT fixed
2025-10-01). Popular tool-calling finetunes (xLAM / ToolACE / Hammer) train on BFCL-like data,
and some reportedly overfit (BFCL-high but <10% on OOD tool-use stress tests).
[verified-from-repo / secondary]

**Mitigations that keep the science valid:**
1. **Measure relative lift, not absolute SOTA** — frozen-model *with-skill vs without-skill*
   A/B is contamination-robust because both arms see the same (possibly memorized) data; the
   delta isolates the playbook's effect.
2. **Use the unused 200-entry `composite` set as a fresher held-out probe** — it is authored and
   human-verified but outside the scored pool, so far less leaderboard-driven exposure.
3. **Keep the learned playbook general and procedural** ("ask one clarifying question when a
   required arg is missing"; "confirm before irreversible actions") rather than task-memorized —
   which is also where transferable headroom lives, so this doubles as an anti-overfit guard.
4. **Never claim a leaderboard number** — the skill-doc-in-system-prompt seam is non-standard for
   the official board (they "deliberately avoid prompt engineering"), so results are
   research-valid but not leaderboard-legal. State this explicitly in any writeup.

---

## 10. A–G GO/NO-GO scorecard

Criteria: A pool/splits · B deterministic scoring · C local concurrency · D cost/task ·
E knowledge-repairable · F comparability · G headroom.

| Env | A | B | C | D | E | F | G | Verdict |
|---|---|---|---|---|---|---|---|---|
| **BFCL v3 multi-turn** | ✅✅ 800, 4×4 grid | ✅✅ state+AST, **no user-sim** | ✅ pure-Python, no Docker/VM | ✅ ~8–12 calls/entry (long_context heavy) | 🟡 miss_param/miss_func ideal; long-ctx/deep-state capability-bound | ✅✅ de-facto standard (⚠ contamination) | ✅ Qwen3-32B ~43% MT, ~35pp reachable | 🟢 **CONDITIONAL GO** |

Contrast the companion doc: WebArena/OSWorld are ❌ on **C** (disk/VM/KVM). BFCL's **C is a
clean pass**, and its **A/B are stronger** (bigger deterministic pool, no LLM judge). Its only
non-strong cell is **E**, and even there the two headline categories are near-ideal for a
convention playbook.

---

## 11. If adopted — integration path & open follow-ups

1. **Install:** `pip install bfcl-eval` from PyPI (avoids the GitHub block); or git-bundle the
   repo from the Mac. Point at the running vLLM via `--skip-server-setup` + `LOCAL_SERVER_*`.
2. **Rollout:** either drive via `bfcl generate --test-category multi_turn_base ... --num-threads N`,
   or (preferred for the CSS loop) **import the backend classes** and run the fixed-user-turn +
   free-model-action episode inside the existing 50–128-worker harness, scoring with the
   `multi_turn_checker` logic. No server/reset infra to rebuild (contrast WebArena's multi-week
   env-layer job).
3. **Splits:** carve train/val/test off the 4×4 grid; reserve `composite` (200, unused) as a
   fresher held-out probe.
4. **Scope:** lead with `miss_param` + `miss_func`; treat `long_context` as optional/heavy.

**Open follow-ups (cheap, recommended before committing):**
- **#1 — Live-board scrape.** The official leaderboard is a JS SPA; a Playwright pass over
  gorilla.cs.berkeley.edu (the plugin is now available) would lock the exact multi-turn cells
  for frontier + open models, replacing the [secondary] numbers in §5.
- **#2 — Zero-skill baseline.** Run frozen qwen3.6-35b-a3b on the 800 (native-FC, no skill doc)
  to confirm the **~40% starting point** and the per-category floor before investing — this both
  validates the ~43% proxy and establishes the without-skill arm of the A/B.

---

## 12. Confidence flags

- **Dataset counts, backend/function counts, entry schema, split-axis balance:** parsed from the
  live `main` repo via `gh`/`curl` on 2026-07-04 — **verified-from-repo, HIGH.** (The earlier
  187/178 line-counts were truncated-stream artifacts; the full-file counts are 200 each.)
- **Scoring mechanics** (no user-sim, model-side state carry-forward, no per-turn GT injection,
  state+subsequence checks, all-turns-pass): read from `multi_turn_checker.py` —
  **verified-from-repo, HIGH.**
- **Deployment flags** (`--skip-server-setup`, `--num-threads`, `--backend`, Apache-2.0):
  **verified-from-repo, HIGH.** PyPI-reachable / GitHub-blocked is **inferred** from the server
  baseline (AppWorld prep) — MEDIUM.
- **Blog facts** (authoring, Persona Hub, long_context size, no-prompt-engineering, state vs
  response checks): **verified-from-blog, HIGH.**
- **Leaderboard numbers** (§5): live board unreadable (JS SPA) → **all [secondary]**, gathered by
  research sub-agents; Qwen3-32B FC 43.1/69.25 from LoopTool (arXiv 2511.09148) is the best
  anchor — **MEDIUM.** Frontier cells and cross-harness totals — **LOWER.**
- **Prior-work deltas** (ReMe +8.45, GEPA +8.7–13.9, FunReason-MT 15.75→56.5, Magnet 5.38→37.88):
  arXiv-sourced via sub-agent abstract reads — **verified-from-paper, MEDIUM-HIGH**; the ~+5–15pp
  frozen-model envelope conclusion is robust.
- **AppWorld comparison:** from project context (`appworld-env-integrated`) — HIGH for AppWorld,
  the qualitative overlap verdict is robust.

---

## 13. Sources

Repo github.com/ShishirPatil/gorilla/tree/main/berkeley-function-call-leaderboard (Apache-2.0) ·
v3 multi-turn blog gorilla.cs.berkeley.edu/blogs/13_bfcl_v3_multi_turn.html ·
V4 blogs 15_bfcl_v4_web_search / 16_bfcl_v4_memory / 17_bfcl_v4_prompt_variation ·
paper PMLR v267 patil25a (ICML 2025; Patil, Mao, Yan, Ji, Suresh, Stoica, Gonzalez) ·
leaderboard gorilla.cs.berkeley.edu/leaderboard.html · pip `bfcl-eval` ·
CHANGELOG (V3 2024-09-19 #644, V4 2025-07-17 #1019, executable cats retired 2025-04-09 #943) ·
eval `bfcl_eval/eval_checker/multi_turn_eval/multi_turn_checker.py` ·
LoopTool arXiv 2511.09148 (Qwen3-32B FC breakdown) · APIGen-MT / xLAM-2 ·
ReMe arXiv 2512.10696 · GEPA arXiv 2507.19457 · FunReason-MT arXiv 2510.24645 ·
Magnet arXiv 2503.07826 · llm-stats.com/benchmarks/bfcl-v4 · emergentmind BFCL-v3-multi-turn ·
NVIDIA dev-forum multi-turn failure analysis · companion `webarena_osworld_ASSESSMENT.md`, `appworld_PREP.md`.
