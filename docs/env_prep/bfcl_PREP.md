# BFCL Multi-Turn Integration Prep

Prepared for integrating the **Berkeley Function Calling Leaderboard (BFCL) v3/v4 multi-turn**
track into the CSS skill-document optimization harness. Every claim below is from **reading the
pinned source or running code on this Mac** — not from memory or the web. Companion to the
`*_PREP.md` files and `bfcl_v3_ASSESSMENT.md` (the go/no-go that preceded this).

- **Repo**: https://github.com/ShishirPatil/gorilla (sparse-checkout of `berkeley-function-call-leaderboard`
  → `env_candidates/gorilla`)
- **Pinned commit**: `6ea57973c7a6097fd7c5915698c54c17c5b1b6c8` (`git describe = v1.3-48-g6ea5797`),
  package `bfcl_eval` (dynamic version via setuptools-scm). **The CHANGELOG shows continuous
  ground-truth patching, so this commit is pinned and MUST be stated wherever numbers are quoted.**
- **License**: **Apache-2.0**. No research restrictions.
- **Probe scripts + results**: `env_candidates/bfcl_smoke/` (`scripts/dissect_and_probe.py`,
  `scripts/live_smoke.py`, `results/*.json`).

---

## 0. TL;DR for integrators

- **Deployment is trivial** vs the GUI envs: 8 pure-Python in-process backends, free per-entry
  reset, no Docker/VM/network. Vendor ~11 files; don't `pip install bfcl-eval` (it drags ~25 heavy
  SDKs). [§1–2]
- **Determinism: PROVEN** (byte-identical double-replay on all 4 primary backends). **Thread-safety:
  conditional** — instances live in module `globals()` keyed by `model_name+id+class`; concurrent
  rollouts of the SAME item with the same key silently cross-contaminate (measured 1/8 correct).
  Fix: unique per-rollout instance key. [§5]
- **Scoring is deterministic, no user-simulator, binary per entry** — state + call-subsequence
  checks; the `miss_func`/`miss_param` "refrain" turns are enforced only *indirectly*. [§4]
- **FC caveat (measured, important):** our vLLM endpoint returns Qwen **XML tool-call TEXT**, not
  structured OpenAI `tool_calls`; CSS's `complete_target_tools` expects structured calls. Needs a
  parser fix OR a text fallback. [§6]
- **Headroom (official board, scraped 2026-07-04):** Qwen3-32B FC **47.9% MT**, Qwen3-30B-A3B FC
  **30.0% MT** — our model's closest proxies; miss_func/miss_param are the weakest sub-scores. A
  same-size *tuned* model (xLAM-2-32B) hits **69.5%**, proving these are learnable, not capability
  floors. [§8]

---

## 1. Python, dependencies, install — VENDOR, don't pip

- **`requires-python >=3.10`** (`pyproject.toml`). Our Mac probe ran on **Python 3.10.14**; the
  server's 3.8 will NOT work → needs a 3.10+ conda env (same as tau2/appworld).
- **The eval path is dependency-light.** `multi_turn_checker.py` imports only `multi_turn_utils`;
  `multi_turn_utils` imports `bfcl_eval.constants.executable_backend_config` (pure dicts) + stdlib
  (`copy/importlib/inspect/json/re`). `bfcl_eval/__init__.py` is **empty**. The 8 multi-turn backends
  import **stdlib only** (`datetime/random/copy/typing/math/decimal/subprocess`) **+ `mpmath`**
  (math_api) + a sibling `long_context.py` (pure data constants, no imports). `subprocess` is
  imported by `gorilla_file_system` but **never called** (dead import — no shell-out).
- **CRITICAL — do NOT `pip install bfcl-eval`.** Its `pyproject` `dependencies` (not extras) pin
  **~25 heavy packages**: `openai, anthropic, cohere, mistralai, google-genai, qwen-agent,
  writer-sdk, boto3, sentence-transformers, faiss-cpu, networkx, numpy==1.26.4, tree-sitter*` (and
  `vllm==0.8.5` as an extra). None of these are on the multi-turn eval path.
- **RECOMMENDATION: vendor the minimal subset** (Apache-2.0, ~11 files, deps = stdlib + `mpmath`):
  ```
  bfcl_eval/eval_checker/multi_turn_eval/multi_turn_checker.py
  bfcl_eval/eval_checker/multi_turn_eval/multi_turn_utils.py
  bfcl_eval/constants/executable_backend_config.py            (+ default_prompts.py for MAXIMUM_STEP_LIMIT + canned prompt)
  bfcl_eval/eval_checker/multi_turn_eval/func_source_code/{gorilla_file_system,math_api,message_api,
      posting_api,ticket_api,trading_bot,travel_booking,vehicle_control,long_context}.py
  ```
  Mirrors the `spreadsheet-env-vendored` precedent (zero-dependency copy into the css library).
- **Offline transfer** (server has GitHub + HF blocked, PyPI reachable): `git bundle` from the Mac,
  OR just copy the vendored subset + the data files. `mpmath` is on PyPI (pin `1.3.0`; `1.4.1` also
  imported cleanly in the probe).

## 2. Offline capability / deployment

- **100% local/offline after vendoring.** The backends are in-process Python objects; a rollout
  instantiates them from the entry's `initial_config` (free reset). **No network, no filesystem
  side effects** in the multi-turn track (the `datetime.now()` in GorillaFileSystem only writes a
  `_`-private attr; the `subprocess` import is unused). The only outbound traffic is the LLM API to
  our vLLM endpoint.
- **Data footprint is tiny** (~4 MB total): the 4 active + 1 composite question JSONL (~0.4 MB each),
  their `possible_answer/*` ground-truth JSONL, and `multi_turn_func_doc/*.json` (function schemas).
- **No Docker / VM / KVM** — the disk/RAM/virt walls that killed WebArena/OSWorld do not apply.

## 3. Dataset — measured (`scripts/dissect_and_probe.py`, all 800 + 200 parsed)

The repo now labels files `BFCL_v4_multi_turn_*` but the multi-turn **content is v3** (V4 only
*added* separate web-search/memory tracks). Entry schema:
`{id, question:[per-turn user msgs], initial_config, path, involved_classes, excluded_function?,
missed_function? (miss_func only)}`.

| Category (`test-category`) | Entries | User-turns (min–max, mean) | Gold calls/entry (mean) | Empty-GT ("refrain") turns |
|---|---|---|---|---|
| `multi_turn_base` | **200** | 1–7, 3.67 | 5.71 | 2 entries |
| `multi_turn_miss_func` | **200** | 2–8, 4.67 | 5.70 | **200 entries (all)** |
| `multi_turn_miss_param` | **200** | 2–8, 4.67 | 5.70 | **200 entries (all)** |
| `multi_turn_long_context` | **200** | 1–7, 3.67 | 6.01 (max 35) | 2 entries |
| **TOTAL ACTIVE** | **800** | | | |
| `multi_turn_composite` | **200** | (unused; `data/unused_datasets/`) | | |

- **Backends: 8 stateful pure-Python classes, 128 functions** (vehicle 22, trading 20, gfs 18,
  travel 18, math 17, twitter/posting 14, message 10, ticket 9). `MathAPI` is the only STATELESS
  class. **Function schemas exposed per entry: 18–39, mean 27.8.**
- **68% of entries (540/800) involve 2 backends.** Per-class occurrence: GFS/Vehicle/Trading/Travel
  = 200 each (the "primary" domains), Message 160, Twitter 156, Ticket 124, Math 100. Top combos:
  TradingBot-solo 80, VehicleControl-solo 76, Ticket+Travel 60, Twitter+Vehicle 56, GFS-solo 52.
- **`long_context` bloat is RUNTIME-injected, not stored.** `initial_config` byte-size is identical
  across all 4 categories (mean 852 B, max 2571 B). Passing `long_context=True` to `_load_scenario`
  pads backend state at runtime: measured **TradingBot `get_watchlist()` 23 B → 7006 B (×305)**. So
  the distractor context appears in **tool results during the episode**, not in the initial prompt.
- **`missed_function`** (miss_func only) = a `{turn_index: [funcs]}` holdout map (e.g.
  `{"3": ["sort"]}`). **`excluded_function`** is a *separate*, optional global-exclusion list present
  in only 18/200 entries per category (top: cp/mv/rm) — NOT the miss_func mechanism.

## 4. Scoring mechanics — deterministic, binary, no user-simulator (read from source)

Two phases. **Generation** (`base_handler.inference_multi_turn_FC`): the model drives the episode,
its calls execute live on instances stashed in `globals()`. **Evaluation**
(`eval_runner._evaluate_single_multi_turn_entry` → `multi_turn_checker`): the recorded calls are
**re-executed on FRESH instances** and compared to ground truth.

- **No user-simulator LLM.** `question` turns are pre-scripted static text (contrast tau²). Fully
  deterministic; no second model in the loop.
- **Per turn:** execute the model's decoded calls (accumulating on the model instance), execute the
  turn's GT on a **separate** instance; then two checks — **state** (`_compare_instances`: all
  non-`_` attributes must be exactly equal) and **response** (`_is_subsequence_unordered`: the GT's
  result strings must be an unordered subsequence of the model's accumulated results). **First
  failing turn → whole entry fails. All turns pass → valid. Binary.**
- **Turn ends** when the model emits no parseable tool call. **`MAXIMUM_STEP_LIMIT = 20` steps/turn**;
  exceeding → `force_quit` → entry fails as `multi_turn:force_terminated` (also fails if the model's
  turn count ≠ GT turn count).
- **`miss_func` mechanism:** at the holdout turn the withheld functions are appended to the tool list
  and the (empty) user turn is replaced by the canned prompt **"I have updated some more functions
  you can choose from. What about now?"**. The pre-release turn's GT is empty → the model should NOT
  call the absent tool.
- **`miss_param` mechanism:** the refrain turn's user message is deliberately ambiguous (a required
  arg is missing, GT empty); the next turn clarifies it. The model should ask, not guess.
- **CRITICAL NUANCE — "refrain" is enforced only INDIRECTLY.** `multi_turn_irrelevance_checker` is
  **defined but never called** (verified: no call site). On an empty-GT turn the checker `continue`s
  (executes the model's calls for downstream state accuracy but does not fail it for acting). So a
  spurious call on a refrain turn is punished only if it corrupts downstream state relative to GT — a
  **lucky correct early-guess can still pass**. Implication: `miss_param`/`miss_func` reward the
  ask-don't-guess behavior but do not *strictly* require it. This matters for the CSS soft-score and
  for honest E-criterion framing.
- **Gold self-consistency (measured): 800/800 active + 40/40 composite gold trajectories pass their
  own state+response check** — the harness and my re-implementation agree end-to-end.

## 5. Environment infra — determinism & thread-safety PROBES (measured)

- **Determinism: PROVEN.** Double-replay of the same gold trajectory on fresh instances yields
  **byte-identical** non-private state for **TradingBot, TravelAPI, GorillaFileSystem,
  VehicleControlAPI**. Randomness is per-instance **seeded** (`self._random =
  random.Random(scenario["random_seed"])`), time is a fixed constant (`CURRENT_TIME =
  datetime(2024,9,1,10,30)`), and the one wall-clock `datetime.now()` writes a `_`-private attr the
  checker skips. → **safe for a reproducible concurrent evaluator.**
- **Thread-safety: CONDITIONAL (hazard measured + fix confirmed).** `execute_multi_turn_func_call`
  stashes each instance in module **`globals()[f"{model_name}_{id}_{class}_instance"]`**.
  - 8 concurrent replays of the SAME entry with a **shared `model_name`** → **1/8 correct** (7 silently
    cross-contaminated; **no crash**).
  - 8 concurrent replays with a **unique `model_name`** → **8/8 correct**.
  - The 8 backend classes themselves have **no class-level mutable state** (static scan clean).
  - **REQUIREMENT:** give every (item, rollout) a unique instance key (e.g. `model_name =
    f"r{rollout_index}_{node_id}"`), OR drive the backends directly. Also note `globals()` **never
    clears** → a long run leaks instances; periodically clear or key by a bounded id.

## 6. Model configuration — the FC-mode finding (measured on the live endpoint)

- **Native structured FC is NOT currently returned by our endpoints.** Passing OpenAI `tools=[...]`
  to `http://10.77.110.162:8888/v1` (and `:8889`), model `qwen3.6-35b-a3b`,
  `enable_thinking=False`, the response has **`tool_calls=[]`, `finish_reason=stop`**, and the call
  appears as **content** in Qwen XML form:
  ```
  <tool_call>\n<function=get_stock_info>\n<parameter=symbol>\nNVDA\n</parameter>\n</function>\n</tool_call>
  ```
  Identical on both endpoints, with or without `tool_choice="auto"`. The model clearly *understands*
  the tools — vLLM just isn't extracting them (a `--tool-call-parser` mismatch for this model's XML
  format).
- **CSS gap:** `client.complete_target_tools` returns `message.get("tool_calls")` with **no text
  fallback** — so as-is it would see zero tool calls for BFCL (and possibly Bird, if that endpoint is
  configured the same). **Two options** (TO-NEGOTIATE): (a) relaunch vLLM with the correct
  `--enable-auto-tool-choice --tool-call-parser <qwen-xml>`; or (b) add a text-format parser to the
  CSS client (the `<tool_call><function=..><parameter=..>` grammar is deterministic — the smoke
  parser in `live_smoke.py` does exactly this).
- **Mode choice:** for our model, **FC > Prompt** on the board (Qwen3-32B FC 47.9 vs Prompt 43.3;
  Qwen3-30B-A3B FC 30.0 vs Prompt 23.5), so target FC. Note BFCL's *default* system prompt already
  nudges *"if the question lacks required parameters, point it out"* — the zero-skill baseline is not
  fully naive, so we use a **minimal neutral system prompt** in the smoke to expose true headroom.

## 7. Live smoke — zero-skill on the frozen model (`scripts/live_smoke.py`)

Minimal system prompt (no procedural guidance), FC tools passed + XML parsed, real
`multi_turn_checker` scoring, `temperature=0.0`.

**Full-episode driver (base):** `multi_turn_base_0` PASS (4 turns, 18 steps, 115 k cumulative
prompt-tok, 157 s); `multi_turn_base_1` PASS (4 turns, 9 steps, 44 k tok, 47 s). The FC-XML driver
works end-to-end and scores via the real checker. The 18-step base_0 (near the 20-step cap) already
shows loop/over-exploration — an E-surface a skill doc could tighten. **Endpoint contention note:**
the two endpoints are shared with the running scienceworld/appworld/bird experiments, so
per-entry wall-time is high (100 s–several min); a larger zero-skill base sample is a cheap
follow-up when contention clears.

**Refrain behavior — the key E-surface, measured live (`scripts/refrain_probe.py`).** For 4 miss_*
entries I replayed the gold prior turns to reach the refrain turn (the first empty-GT turn), then
issued ONE query and classified. **All 4/4 FAILED to refrain — the zero-skill model acted/guessed
instead of asking or holding off:**

| Entry | Refrain situation | Model did | Verdict |
|---|---|---|---|
| miss_param_0 | "Move *one of the file*…" (which file?) | called `cd`, `ls` (explored, didn't ask) | ACTED |
| miss_param_1 | "show the last several lines the file" (which file?) | **guessed** `tail(file_name='log.txt', lines=5)` | ACTED |
| miss_func_0 | "sort the report" — but `sort` is **held out of the tool list** | **fabricated `sort(...)`** (a tool it was NOT given!) | ACTED |
| miss_func_1 | "move log.txt…" — but `mv` is **held out** | called `cd` toward the absent `mv` | ACTED |

This is direct live evidence that the frozen model **does not spontaneously ask-when-ambiguous or
refuse-absent-tools** — it guesses parameters and even **hallucinates a tool absent from its schema**
(miss_func_0). These are precisely the procedural conventions a skill document installs ("if a
required arg is missing, ask one question; if no available tool matches, say so — never invent a
call"), and the leaderboard confirms the gap is large and closable (open A3B miss_func 10.5 vs
same-size tuned xLAM-2-32B 72.5). Caveat: single-query probe with gold-replayed history, n=4 — a
directional but consistent signal, not an accuracy estimate.

**Resource math (from the smoke):** ~4 turns × up to ~4–18 steps ≈ 10–20 model calls/entry; a full
800-entry pass ≈ 8–16 k model calls; a ~160-entry val pass ≈ 2–3 k calls. `long_context` is the only
heavy slice (×300 tool results). On the 3B-active MoE this is throughput-cheap; wall-time is
endpoint-contention-bound (three experiments currently share the two endpoints).

## 8. Leaderboard & headroom — official BFCL V4, scraped 2026-07-04 (`results/leaderboard_v4_multiturn.json`)

Multi-turn sub-columns (MT-overall / base / miss_func / miss_param / long_context), FC unless noted:

| Model | Overall (V4) | **MT overall** | base | miss_func | miss_param | long_ctx |
|---|---|---|---|---|---|---|
| Claude-Opus-4-5 | 77.5 | **68.4** | 81 | 64 | 58 | 70.5 |
| GLM-4.6 (FC-think) | 72.4 | **68.0** | 74.5 | 68 | 63 | 66.5 |
| Gemini-3-Pro (FC) | 68.1 | **63.1** | 69 | 63 | 56.5 | 64 |
| Claude-Sonnet-4-5 | 73.2 | **61.4** | 69 | 65 | 52.5 | 59 |
| Kimi-K2-Instruct | 59.1 | **50.6** | 62 | 41 | 44.5 | 55 |
| DeepSeek-V3.2 (Prompt+think) | 56.7 | **44.9** | 55 | 49 | 27 | 48.5 |
| **GPT-5.2** | 55.9 | **28.1** | 36.5 | 18 | 27.5 | 30.5 |
| — open ~30–35B (closest to our model) — | | | | | | |
| **Qwen3-32B (FC)** | 48.7 | **47.9** | 56 | 52.5 | 40 | 43 |
| Qwen3-32B (Prompt) | 46.8 | 43.3 | 54 | 46 | 36.5 | 36.5 |
| Qwen3-235B-A22B (FC) | 48.0 | 45.4 | 57.5 | 35 | 33.5 | 55.5 |
| **Qwen3-30B-A3B-2507 (FC)** | 41.4 | **30.0** | 43.5 | **10.5** | 25 | 41 |
| Qwen3-8B (FC) | 42.6 | 41.8 | 50.5 | 42 | 40 | 34.5 |
| — tuned FC specialists (the ceiling) — | | | | | | |
| **xLAM-2-70b-fc-r** | 53.1 | **77.4** | 82.5 | 77 | 74 | 76 |
| **xLAM-2-32b-fc-r** | 54.7 | **69.5** | 81.5 | 72.5 | 67.5 | 56.5 |
| ToolACE-2-8B | 42.4 | 38.4 | 49 | 28 | 30.5 | 46 |
| Hammer2.1-7b | 31.7 | 23.9 | 24 | 28.5 | 21.5 | 21.5 |

**Reads:** (1) No `qwen3.6-35b-a3b` on the board; the **A3B MoE (Qwen3-30B-A3B) is the closest
architecture at MT 30.0**, and the dense Qwen3-32B at 47.9 — our newer model plausibly lands ~30–50%,
squarely in the 10–85% headroom band. (2) **miss_func / miss_param are the weakest sub-scores for
open models** (A3B miss_func = 10.5!) — exactly the CSS-target convention surface. (3) A **same-size
tuned** model (xLAM-2-32B) reaches miss_func 72.5 / miss_param 67.5 — proving these categories are
**learnable, not capability floors** (strong criterion-E evidence). (4) The V4 "Overall" is dragged
down by the hard agentic web-search/memory tracks; multi-turn is reported separately, which is what
we optimize.

## 9. Prior optimization work (secondary, from the assessment)

Frozen-model text/memory precedents bound the expected gain at **~+5–15 pp**: **ReMe** (procedural
memory, no weight updates) +8.45 pp on Qwen3-8B MT-base; **GEPA** (reflective prompt evolution)
+8.7–13.9 % on tau-bench multi-turn. Fine-tuning is the upper bound (installs missing
tool-result→next-action mechanics into weights): **FunReason-MT** Qwen3-4B 15.75→56.5; **Magnet**
5.38→37.88; **xLAM-2** family above. Text recovers the *procedural* slice (ask/refrain/loop-avoid),
not the capability slice (long-context, deep state).

## 10. CSS `TaskEnv` mapping — DRAFT (models on `css/envs/bird`)

| CSS contract | BFCL binding |
|---|---|
| `item` | one multi-turn entry `{id, question, initial_config, involved_classes, missed_function}`; GT `ground_truth` (gold call sequences) carried for the evaluator only, **firewalled from the agent** |
| `run_one(item, skill_text, target_client, …)` | run the FC multi-turn agent under `skill_text` (system prompt) + tool schemas; drive the 20-step/turn loop over the scripted user turns (inject holdout funcs + canned prompt at release turns); execute on a **per-rollout-unique** instance; score with `multi_turn_checker` |
| `hard` | `multi_turn_checker(...)["valid"]` (0/1), all-turns-pass |
| `soft` (PROPOSE) | **fraction of turns passed** = (# turns clearing state+response before first failure) / (# GT turns). **Validated:** the failing turn index is recoverable from the checker's error message (base_0 corrupted at turn 2 → soft 2/4 = 0.50). Cheap, gives the optimizer a gradient for difficulty-weighting; finer option = fraction of the 2·N per-turn checks passed |
| `task_type` (PROPOSE) | the **category** (`base/miss_func/miss_param/long_context`) → stratification + per-category `extra_metrics` |
| GT firewall | gold call sequences appear ONLY in `eval_annotation_message(outcome, ground_truth, detail)` appended as the last message; the agent sees tools + user turns only |
| skill injection | **system prompt** (verified: the endpoint honors a system message alongside `tools`) |
| `action_space_description()` | FC loop: per user turn the agent may emit function calls against the involved backends and observe results, continuing until it stops calling; some tools may be absent initially and appear later (miss_func); some requests may under-specify a required arg (miss_param) → ask rather than guess |
| `eval_splits()` / `extra_metrics()` | primary = the balanced test carve; `extra_metrics` = **per-category accuracy** (base/miss_func/miss_param/long_context), mirroring the leaderboard sub-scores |
| K rollouts / temperature (PROPOSE) | K=3 at **temperature ≈ 0.7** (mirrors scienceworld/appworld) — **TO-NEGOTIATE** (BFCL's own board uses 0.0 greedy) |

We will **import the vendored backends + checker and drive our own loop** (not adopt bfcl's runner),
reusing only the deterministic checker + backends.

## 11. Split design — PROPOSE split by SCENARIO INDEX (measured leakage risk)

**Measured:** categories mirror each other by index — for scenario `i`, `involved_classes` is
identical across all 4 categories (200/200) and `initial_config` identical 190/200; `base_i`,
`miss_func_i`, `miss_param_i`, `long_context_i` are the **same scenario** with different challenge
perturbations. **Naive category- or row-level splitting would leak scenarios** (base_i in train
teaches the scenario miss_param_i tests).

- **PROPOSE:** split by the **scenario index** (the numeric id suffix, 0–199), taking all 4
  category-variants of an index together, **stratified by primary domain** (GFS/Vehicle/Trading/Travel
  ≈ 50 indices each). E.g. **120 idx train / 40 val / 40 test** → **train ≈ 480, val ≈ 160, test ≈
  160** entries, every challenge type present in every split, **zero scenario leakage**.
- **composite (200)** also shares scenarios by index, so apply the **same index split**; composite on
  the *test* indices is a clean sealed extra probe (composite on train indices would leak).
- This satisfies the requirement that ask/refrain conventions are *learned* on train and *tested* on
  unseen entries of the same challenge types.

## 12. Top 5 risks

1. **FC-mode plumbing (blocker-ish).** Endpoint returns Qwen XML tool text, not structured
   `tool_calls`; CSS's client has no text fallback. Must fix the vLLM parser or add a parser. [§6]
2. **Thread-safety footgun.** `globals()` instance stash silently cross-contaminates same-key
   concurrent rollouts (measured 1/8). Requires a unique per-rollout key + periodic clearing. [§5]
3. **Contamination.** Whole dataset (Q + GT + backends) is public, no private holdout, continuously
   patched; tuned models (xLAM/ToolACE/Hammer) train on BFCL-like data. → report **relative** with/
   without-skill lift, pin the commit, never claim a board number.
4. **Refrain not strictly scored.** `miss_param`/`miss_func` reward ask-don't-guess only indirectly
   (irrelevance checker unused) → a lucky guess can pass; don't over-claim the "asks correctly"
   result without inspecting behavior (the smoke captures it). [§4]
5. **Bounded text ceiling + long_context cost.** Frozen-text precedent ≈ +5–15 pp, concentrated in
   the procedural categories; long_context tool results blow up ×300 → run it at lower concurrency or
   treat as optional. [§8–9]

## 13. TO-NEGOTIATE config list (per-env default-config convention)

| Item | Proposed | Rationale / open question |
|---|---|---|
| **FC vs Prompt mode** | **FC** + XML text parser | FC>Prompt for our model on the board; but needs the parser decision (fix vLLM vs CSS fallback) |
| **temperature** | 0.7 | mirrors scienceworld/appworld; BFCL board uses 0.0 — confirm diversity vs fidelity |
| **k_rollouts** | 3 | standard |
| **soft score** | turns-passed fraction | vs 2·N per-turn-checks fraction |
| **per-turn step cap** | 20 | adopt BFCL `MAXIMUM_STEP_LIMIT` |
| **categories in scope** | base + miss_func + miss_param (+ long_context optional) | miss_* are the E-target; long_context is heavy/capability-bound |
| **split** | by scenario index, 120/40/40, domain-stratified | avoids scenario leakage (§11) |
| **composite** | sealed probe on test indices only | shares scenarios with base by index |
| **max_tokens/turn** | 1024 | smoke value; confirm |
| **skill injection point** | system prompt | verified endpoint honors system+tools |

---

## 14. Integration delivered (2026-07-05, formal onboarding)

Approved to formal integration; the config in §13 was resolved (below) and the env
was built following the ScienceWorld/AppWorld pattern. **Staged-ready — not committed
(team lead reviews + commits).**

**Files** (all new except a 5-line registry branch):
- `css/envs/bfcl/vendor/` — the 8 backends + long_context + 8 func-doc schemas + Apache-2.0
  LICENSE, pinned 6ea5797 (two minimal edits: the relative `long_context` import; one
  `List[str]` annotation in `vehicle_control` for 3.8-import safety — both documented in
  `vendor/__init__.py`).
- `css/envs/bfcl/checker.py` — adapted checker: fresh per-rollout instances, **no `globals()`**;
  parity-locked to upstream.
- `css/envs/bfcl/agent.py` — FC multi-turn loop (`complete_target_tools`; per-turn 20-step cap +
  force-terminate; holdout injection; canonical trajectory flattening).
- `css/envs/bfcl/prompts.py` — minimal mechanics system prompt + skill-doc slot (no ask/refrain
  nudge → headroom preserved) + `ACTION_SPACE_DESCRIPTION`.
- `css/envs/bfcl/task_interface.py` — `BfclEnv` (`eval_splits=[("test",160),("composite_probe",40)]`,
  per-category `extra_metrics`, GT-firewall annotation with `bfcl_gt_mode`).
- `css/envs/registry.py` — `bfcl` branch + aliases.
- `tools/make_bfcl_split.py` + `data/bfcl_split_seed42/` — seed-42 manifests.
- `run_experiment_bfcl_server.py` — launcher with the approved config.
- `css/tests/test_bfcl_{manifest,checker,env}.py` — 16 tests.

**Resolved config (agreed 2026-07-05):** split by scenario index 120/40/40 domain-stratified →
**train 480 / val 160 / test 160**, composite_probe 40 (sealed, test indices only); hard =
all-turns-pass, **soft = fraction of turns passed**; **native FC** via `complete_target_tools`
(skill doc in the system prompt), the endpoint's Qwen-XML output recovered by the client fallback
(commit 9a1823b) with the dedicated `127.0.0.1:8888` (parser ON) for the server run; in-process
fresh instances per (item, rollout) — never `globals()`; 20-step/turn cap; `bfcl_temperature=0.4`,
`bfcl_gt_mode="gold_calls"` (ablation: `"none"` / prompt-mode). `batch_size=96` flagged TO-CONFIRM.

**Validation:**
- **16/16 tests pass.** Checker **parity 20/20** vs the upstream checker on gold replays (all 4
  categories); split manifests: exact counts, **zero scenario leakage**, every category in every
  split, deterministic; **8-way concurrency isolation** regression (gold+corrupt interleaved on the
  same entry → each thread its own correct verdict).
- **Gold replay through the env: 12/12 hard=1** across all 4 categories (offline, stub client); the
  **GT firewall holds** (gold only in the final `evaluation` message).
- **Live (Mac, 10.77.110.162:8888):** FC sanity via the client XML fallback returns a structured
  tool_call. Zero-skill FC rollouts + 8-way live concurrency run under `env_candidates/bfcl_smoke/
  integration_check/` (endpoint contended → completing; per-category pass + miss_* behavior folded
  in on completion).

**Deviations from the spec:** (1) the FC-mode plumbing was resolved outside the env (client XML
fallback + dedicated parser-ON endpoint), so `agent.py` uses `complete_target_tools` unchanged
rather than an env-local parser; (2) two minimal, documented vendoring edits for self-containment /
3.8-import safety. No orchestrator/coldstart/optimizer-core changes.
