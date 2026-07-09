# WebArena async high-concurrency browser layer (design)

**Goal (user directive 2026-07-09):** make the task-interaction environment stop
being the throughput bottleneck — support 100+ concurrent episodes with bounded
RAM. LLM capacity is assumed to scale (not the cap). Full refactor of the
*environment-side* code is authorized; the CSS mechanism/rollout core is off-limits.

## Why the current design caps at ~24
`env.run_one` gates browsers with a `BoundedSemaphore(webarena_max_browsers)` and
each episode does `sync_playwright() → browser.launch() → new_context() → close`
(agent.py). Playwright's **sync API is thread-affine** — a browser can only be
driven by its creating thread — so the 256 rollout threads each need their own
full browser (~2–4 GB). RAM caps concurrency at ~24–28, and repeated
launch/close leaks process/FD/memory over a long run.

## Target architecture (async, single event loop)
One **dedicated asyncio loop thread** owns **K async chromium browsers** (K small,
~6–8). Each episode runs as a **coroutine** using its own **BrowserContext** on
one of the K browsers. Browser ops (goto, action, CDP AXTree) are `await`-ed and
multiplexed on the loop; the blocking LLM calls are **offloaded to a thread pool**
(`run_in_executor`) so the loop never stalls. RAM = K browsers + N contexts
(~100 MB each) → 100+ concurrent in a few tens of GB.

```
rollout thread (×N)                asyncio loop thread                LLM executor
  run_one(item) ──submit coro──►  run_episode_async(item)
     .result() (blocks)             ├─ await page.goto / aexecute_action
                                    ├─ await aprocess (CDP fetch) ──► executor: parse AXTree (pure, CPU)
                                    └─ agent/scribe LLM ───────────► executor: sync client call
```

- **Concurrency cap:** an asyncio `Semaphore(webarena_max_contexts)` on the loop
  (default target 100). `webarena_browser_procs` = K (default 8). RAM-safety cap.
- **Context = per episode:** `new_context(viewport, device_scale_factor=1,
  storage_state=<per-lease>, record_har_path=<workdir>/network.har,
  record_har_content, extra_http_headers=<admin>)` — identical to today, just on a
  pooled async browser. HAR + storage_state + CDP-per-page invariants preserved.
- **CPU offload:** the AXTree text build (pure, ~1–2 s) runs in the executor, not
  on the loop, so 100 concurrent parses don't serialize the loop.
- **Crash recovery:** a dead browser is detected (`browser.is_connected()`/errors)
  and relaunched; the in-flight episode returns an error result (as today).
- **Graceful shutdown:** close contexts → browsers → stop loop at experiment end.

## Harness async port (correctness-critical, validated)
Split the vendored harness into **pure logic (shared)** + **thin Playwright calls
(sync + async variants)**:
- `processors.py`: `aprocess(page, client)` — `await client.send(...)` (CDP),
  `await page.evaluate(...)`; the AXTree→text traversal stays a pure function reused
  by both `.process` and `.aprocess`.
- `actions.py`: `aexecute_action(action, page, ctx, proc)` — async variants of the
  ~15 execute_* helpers (`await page.mouse/keyboard/goto/...`, `await locator.*`);
  the action parsing (`create_id_based_action`) is pure, unchanged.
- **Sync path stays intact** as the regression oracle.

## Phased, validated rollout
1. Async harness port (`aprocess`, `aexecute_action`) — keep sync intact.
2. **Parity gate:** on a fixed page set, `aprocess`≡`process` (identical AXTree
   text) and async actions produce the same effect as sync. Must pass before wiring.
3. `browser_loop.py` — loop thread, K browsers, context factory, semaphore,
   crash recovery, shutdown.
4. `run_episode_async` (turn loop; LLM/scribe via `run_in_executor`).
5. `env.run_one` bridge (`submit → future.result(task_timeout)`) + config knobs.
6. **Concurrency gate:** throughput harness at C=100 — RAM bounded, no leak growth,
   crash recovery verified, throughput scales vs C=12.
7. Deploy to 127 + smoke.

## Config (per-env defaults, launcher)
- `webarena_browser_procs`: K async browsers on 127 (default 8).
- `webarena_max_contexts`: episode concurrency cap (default 100; RAM ≈ K + 100×~0.1 GB).
- `webarena_max_browsers`: ret/deprecated → becomes an alias for max_contexts.
