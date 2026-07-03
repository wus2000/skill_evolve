# WebShop — Integration Prep

Ground-truth prep for integrating **WebShop** (NeurIPS 2022, Princeton NLP) into our research harness.
All facts below are verified against the actual cloned source unless marked *(web)*.

- Upstream cloned to: `env_candidates/webshop/` (princeton-nlp/WebShop, master)
- Minimal fork cloned to: `env_candidates/webshop-minimal/` (ZihanWang314/webshop-minimal)
- Paper: https://arxiv.org/abs/2207.01206 · Site: https://webshop-pnlp.github.io/

---

## 0. TL;DR recommendation

**Use the text env (`WebAgentTextEnv`) fully in-process. It is NOT a webserver** — Flask is imported
only to render Jinja templates in memory; no HTTP port is ever bound. The single heavy dependency for
text mode is the **search backend: pyserini → Lucene → Java (JDK 11)**. Everything else (products, goals,
reward, observations) is pure Python running in one process.

For our China Linux/py3.8 server the clean path is:
1. **Vendor `webshop-minimal` into our repo** (git-bundle from Mac; it bundles the 1k/100k data,
   the 12,087 human instructions, and prebuilt indexes — **zero external download** for smoke tests).
2. **Replace `init_search_engine` + `get_top_n_product_from_keywords` with a pure-Python `rank_bm25`
   (or `bm25s`) searcher** (~30 lines) to **drop Java/pyserini entirely**. `rank_bm25` is already a
   declared dependency in both repos; only the `.search()`/`.doc().raw()` shim is needed.
3. **Full 1.18M-product catalog only if needed**: pull the two big files from the HF mirror
   `quanwei0/webshop-minimal` via **hf-mirror.com** (Google Drive is unreachable in CN).

---

## 1. Install reality (upstream princeton-nlp/WebShop)

### Runtime / system deps
| Dep | Version (pinned) | Needed for text env? | Notes |
|---|---|---|---|
| Python | **3.8.13** (conda) | yes | old repo; py3.8/3.9 era. `setup_arm.sh` + `requirements_arm.txt` for Apple Silicon. |
| Java (JDK) | **openjdk 11** (`conda install -c conda-forge openjdk=11`) | **yes (default)** | required by pyserini/Lucene search backend. The #1 install burden. |
| pyserini | 0.17.0 | **yes (default)** | pulls Anserini fat-jar (~150 MB from Maven) + `pyjnius` (needs JDK headers). Talks to JVM. |
| faiss-cpu | conda `-c pytorch` | **no** | text env never imports faiss (verified). Only baseline IL/RL models. Skip for text. |
| spacy | 3.3.0 | **yes** | reward path `goal.py` loads **`en_core_web_sm`** (~12 MB). `setup.sh` downloads `en_core_web_lg` (~560 MB) but the text-env reward path does **not** need lg — `sm` suffices. |
| torch | 1.11.0 | only if `get_image=1` | text obs needs no torch unless image features enabled. |
| transformers | 4.19.2 | no | baseline models only. |
| selenium + ChromeDriver | 4.2.0 | no | only `html` **site** mode (`WebAgentSiteEnv`), not text. |
| beautifulsoup4, rank_bm25, thefuzz, cleantext, rich, gym==0.24.0, numpy, pandas | — | yes | lightweight, pure-Python. |

`gym==0.24.0` is old-Gym API: `env.step()` returns **4-tuple** `(obs, reward, done, info)` (not the
Gymnasium 5-tuple). `reset()` returns `(obs, None)`.

### setup.sh steps (in order)
1. `pip install -r requirements.txt`
2. `conda install faiss-cpu` + `conda install openjdk=11`
3. `gdown` data from **Google Drive** (see §2)
4. `python -m spacy download en_core_web_lg`
5. Build Lucene index: `convert_product_file_format.py` (products → JsonCollection docs) then
   `run_indexing.sh` (`python -m pyserini.index.lucene …`, needs Java). Builds `indexes_100`,
   `indexes_1k`, `indexes_100k`, `indexes` (full).
6. Download 50 sample MTurk trajectories (optional).

### Known install pain points *(web + issue tracker)*
- **`cannot import name 'url_quote' from 'werkzeug.urls'`** (issue #28): Flask 2.1.2 breaks with newer
  Werkzeug → **pin `werkzeug==2.2.2`**. Extremely common.
- **pyserini/JVM**: `JAVA_HOME` unset, JVM-not-found, `pyjnius` build needs JDK; Anserini jar fetched
  from Maven (slow/blocked in CN). This is the main reason to swap to `rank_bm25`.
- **gdown on the 5.48 GB file**: Google Drive virus-scan/quota interstitial frequently makes `gdown`
  fail on `items_shuffle.json` — another reason to prefer the HF mirror.
- `num_products` must be one of **{100, 1000, 100000, None}** else `NotImplementedError` (indexes are
  prebuilt only for those sizes).
- spacy model/version mismatches under the 3.3.0 pin.

---

## 2. Data files — hosting & sizes (CRITICAL for CN)

| File | Purpose | Size | GDrive id | CN-reachable mirror |
|---|---|---|---|---|
| `items_shuffle.json` | full **1.18M** products | **5.48 GB** (5,479,720,229 B) | `1A2whVgOO0euk5O13n2iYDM0bQRkkRduB` | ✅ HF `quanwei0/webshop-minimal` |
| `items_ins_v2.json` | product attributes + synthetic instr. | **186 MB** (186,295,270 B) | `1s2j6NgHljiZzQNL3veZaAiyW_qDEgBNi` | ✅ HF `quanwei0/webshop-minimal` |
| `items_human_ins.json` | **12,087 human instructions** (goals) | **~5 MB** | `14Kb5SPBk_jfdLZ_CDBNitW98QLDlKR5O` | ⚠️ **NOT** on that HF repo → bundled in `webshop-minimal` GitHub repo (git-bundle from Mac) |
| `items_shuffle_1000.json` + `items_ins_v2_1000.json` | small 1k-product subset | ~4.4 MB total | `1EgHdxQ…` / `1IduG0xl…` | ✅ bundled in `webshop-minimal` repo |
| `feat_conv.pt` + `feat_ids.pt` | image features | ~1.5 GB | folder `1jglJDqNV2…` | not needed for text |
| prebuilt Lucene `indexes*` | search index | ~9 MB (subsets) | — | ✅ bundled in `webshop-minimal` repo |

- **HF mirror total** `quanwei0/webshop-minimal` = **5.67 GB** = the two big files only (items_shuffle +
  items_ins_v2). Pull via `HF_ENDPOINT=https://hf-mirror.com hf download quanwei0/webshop-minimal --repo-type dataset`.
- **Download budget**: FULL text eval ≈ **5.67 GB** (+ ~5 MB human ins from repo). SMALL/1k eval ≈
  **~15 MB, fully bundled — zero network on the server.**
- **Disk after full index build**: ~5.67 GB data + a few hundred MB Lucene full index (if using pyserini)
  OR 0 extra if using in-RAM `rank_bm25`. Optional image feats +1.5 GB (skip).

---

## 3. Lighter alternatives (the ask)

### (a) `webshop-minimal` (ZihanWang314) — the ready-made minimal fork
- Pip-installable package (`pip install --no-build-isolation --no-deps .`), `import webshop_minimal`,
  `webshop_minimal.init_basedir(path)` to point at full data (defaults to bundled 1k data).
- **Bundles**: small 1k data, the full `items_human_ins.json` (12,087 instr), and **prebuilt Lucene
  indexes** (100/1k/100k) — so no gdown and no index-build step for subset eval.
- Env code is **byte-for-byte the upstream text env** ("adapted from official repo"): same `reset/step/
  get_available_actions`, same `server=` arg, same reward.
- **Caveat**: it does **NOT** remove Java — `engine.py` still `LuceneSearcher(...)` and `setup.sh` still
  `apt install default-jdk`. "Minimal" = smaller data + packaging + prebuilt indexes, not fewer system deps.
- License declared **MIT**.

### (b) Text env WITHOUT the Flask webserver — already the default
`WebAgentTextEnv` never starts an HTTP server. `class SimServer` + `class SimBrowser` simulate the site
**in-process**; `flask` is used only for `render_template_string` + `app.app_context()`. `run_web_agent_
text_env.py` runs a full episode standalone. So "no flask webserver, fully in-process" is **already true**
for text mode. The only non-pure-Python piece is the Lucene/Java search backend.

### (c) HF-hosted data mirror
`quanwei0/webshop-minimal` (HF **dataset**, 5.67 GB) mirrors the two big product/attribute files — the
CN-reachable substitute for Google Drive. Human-instructions file comes from the `webshop-minimal` repo.

### ⭐ Recommended lightest viable path for text-mode LLM agents
Vendor `webshop-minimal` (or copy the ~6 files: `env.py, engine.py, goal.py, normalize.py, utils/paths,
templates/`) **and swap the searcher for pure-Python BM25**:
- Implement a tiny class exposing `search(query, k) -> [hit(.docid)]` and `doc(docid).raw() -> json str
  with an 'id' field`, backed by `rank_bm25.BM25Okapi` (already a dep) or `bm25s` (faster, numpy-only).
- Point `init_search_engine` at it. **This removes Java/pyserini/JVM entirely** and makes the searcher
  trivially shareable read-only across 100+ threads.
- Startup cost moves from "build Lucene index on disk" to "tokenize corpus + build BM25 in RAM" once at
  load (seconds for 1k/100k; ~1–2 min for full 1.18M).

---

## 4. Programmatic API (`WebAgentTextEnv`)

Gym id registered in `web_agent_site/envs/__init__.py`: **`WebAgentTextEnv-v0`**.

```python
import gym
from web_agent_site.envs import WebAgentTextEnv

env = gym.make('WebAgentTextEnv-v0',
               observation_mode='text',   # 'text' | 'text_rich' | 'html' | 'url'
               num_products=1000,          # 100 | 1000 | 100000 | None(=full 1.18M)
               human_goals=1)              # 1 → 12,087 human instr; 0 → synthetic (items_ins_v2)

obs, _ = env.reset(session=0)              # int session → deterministic goal index; str → named session
acts = env.get_available_actions()         # {'has_search_bar': bool, 'clickables': [str, ...]}

obs, reward, done, info = env.step('search[red running shoes size 9]')
obs, reward, done, info = env.step('click[b09xxxxxxx]')   # click an ASIN from results
obs, reward, done, info = env.step('click[x-large]')      # select an option
obs, reward, done, info = env.step('click[Buy Now]')      # TERMINAL → reward computed, done=True
```

- **Observation** (`text` mode): visible page text, tokens joined by `' [SEP] '`. Starts with
  `Instruction: [SEP] <goal text> [SEP] …`. `text_rich` adds `[button]…[button_]`, `[clicked button]…`,
  and `You have clicked X.` markers. `num_prev_obs`/`num_prev_actions` kwargs prepend history.
- **Actions**: exactly two verbs — `search[<query>]` (only valid when `has_search_bar`, i.e. on the
  search page / Back-to-Search) and `click[<text>]` where `<text>` must be in `clickables` (buttons,
  product ASINs, option values, `Next >`, `< Prev`, `Description/Features/Reviews/Attributes`,
  `Back to Search`, `Buy Now`). Invalid/malformed action → **no-op, reward 0, done False**.
- **Reward** (`engine/goal.py:get_reward`): continuous **[0, 1]**, only nonzero at `Buy Now`:
  `r = r_type × (num_attr_matches + num_option_matches + r_price) / (|attrs| + |options| + 1)`.
  `r_type ∈ {0,0.1,0.5,1.0}` from product-type/category/title match; attrs/options via `thefuzz`
  token-set ratio > 85; `r_price = price ≤ price_upper`. **Success = reward == 1.0**; **task score =
  mean reward × 100**. `info` is `None` on `step`; verbose breakdown stored server-side
  (`server.user_sessions[sid]['verbose_info']`).
- **done**: True only after `Buy Now`.
- **Instruction ↔ session mapping**: `SimServer` builds goals via `get_goals`, then
  `random.seed(233); random.shuffle(goals)` → **fixed deterministic order**. `reset(session=i)` selects
  `goals[i]`. 12,087 human instructions (`human_goals=1`) or synthetic goals (from `items_ins_v2`).

### Standard eval protocol *(web + convention)*
- WebShop test split = **first 500 instructions** (indices 0–499) under the seed-233 shuffle; 12,087 total
  (rest for train). Report **mean task score (×100)** and **success rate (reward==1.0)**.
- LLM-agent papers subsample for cost: **ReAct 100**, **LATS 50**, **ExpeL 100**. Cross-paper numbers are
  **not directly comparable** (subset size, base LLM, human vs synthetic goals all differ).

---

## 5. Concurrency

- **Statefulness**: one `WebAgentTextEnv` holds a single active rollout (`prev_obs`, `prev_actions`,
  `text_to_clickable`, current `session`). It is **reusable sequentially** via `reset(session=i)`.
- **100+ concurrent rollouts** → one env per worker, but **share ONE `SimServer`** via the constructor
  arg: `WebAgentTextEnv(server=shared_server, observation_mode='text')`. Products dict + goals + searcher
  live on the server (load once); per-env state is small. Avoids loading the multi-GB product dict N times.
- **Search backend is the thread hazard**:
  - **pyserini `LuceneSearcher` is NOT thread-safe to share** across threads *(web: gunicorn multi-worker
    breakage)*. Options: (i) one searcher **per thread**, (ii) use `batch_search` (Java-side threads,
    robust, GIL-free), or (iii) **replace with pure-Python `bm25s`/`rank_bm25`** — read-only, trivially
    shareable across all threads. **(iii) is the recommendation for 100+ concurrency.**
  - If sharing one `SimServer` across threads, also guard the `user_sessions` dict and **avoid the
    `assigned_instruction_text` hack** (it's a shared mutable field); give each thread a distinct
    `session_id`.
- **JVM**: single JVM per process (pyjnius) — favor **thread-parallel (not process-parallel)** rollouts so
  one product dict + one JVM/searcher is shared. (Dropping to `rank_bm25` removes the JVM concern.)
- **Memory footprint**: full 1.18M-product load ≈ several GB resident (5.48 GB JSON → larger as Python
  dicts; budget ~6–8 GB). `num_products=100000` ≈ <1 GB; `1000` ≈ negligible. Load once per process,
  share across threads.

---

## 6. SOTA lineage (WebShop) *(web; score = reward×100 / SR = success%)*

| Method | Base | Score | SR | Notes |
|---|---|---|---|---|
| **Human expert** | — | **82.1** | **59.6%** | ceiling |
| Rule-based | — | ~45.6 | 9.6% | WebShop paper |
| IL | trained | 59.9 | 29.1% | WebShop paper |
| IL+RL | trained | 62.4 | 28.7% | WebShop paper |
| Fine-tune / WebN-T5-style | trained | 67.5 | 45.0% | LATS Table 6 |
| ReAct | GPT-3.5 | 53.8 (best-of-k 59.1) | 28–32% | LATS Table 6 (50-inst). Orig ReAct paper (PaLM,100): ~66.6 / 40%. |
| Reflexion | GPT-3.5 | 64.2 | 35% | LATS Table 6 |
| ExpeL | GPT-3.5 | ~70.1 | — | vs ReAct 66.5 (100-inst) |
| **LATS** | GPT-3.5 | **75.9** | 38% | training-free SOTA-ish (50-inst) |
| Multi-agent frameworks | — | — | ~74.7% | recent *(web)* |

- **~7–30B open models**: with plain ReAct prompting typically ~50–65 score / 25–40% SR; fine-tuned agent
  stacks (AgentGym, ArCHer RL, xLAM) push higher. Numbers vary widely by recipe.
- Note LATS's 75.9 uses a **50-instruction** subset; ReAct/ExpeL use 100. Treat as trend, not leaderboard.

---

## 7. License

- **Upstream code**: `LICENSE.md` is **MIT** (© 2023 Princeton NLP). *(The README badge says "Princeton"
  but the actual file is MIT.)*
- **`webshop-minimal`**: declares **MIT**.
- **Data**: WebShop products are a **scrape of real Amazon listings**; instructions are **crowdsourced
  (MTurk)**. Intended for **non-commercial research**; Amazon-content ToS constraints apply. Keep usage
  research-only; do not redistribute the product corpus commercially.

---

## 8. Integration effort estimate

- **Smoke test (subset, no Java)**: vendor `webshop-minimal` + BM25 shim → ~0.5 day. Zero external
  download; runs on py3.8 CN server offline.
- **Full-catalog text eval**: + hf-mirror pull of 5.67 GB + in-RAM BM25 build → ~1 day incl. a
  100+-thread rollout wrapper sharing one `SimServer`.
- **Keep pyserini/Java instead**: add JDK install + Maven jar fetch (may need a CN Maven mirror) — higher
  risk on the locked-down server; not recommended.
