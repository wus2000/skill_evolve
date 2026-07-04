# llmfleet

KV-cache-aware client-side **addressing, routing, load balancing and
scheduling** over an OpenAI-compatible replica fleet (vLLM first-class).
Standalone, **stdlib-only, zero dependencies, Python ≥ 3.8** — vendor the
directory, or `pip install /path/to/llmfleet`.

Born inside a multi-agent-experiment harness where pure sticky hashing left
one endpoint at 80% busy-seconds and the other idle; the design below fixed
it to 50.7/49.3 while *raising* prefix-cache hits, and generalizes to
heterogeneous fleets (e.g. mixing A100 and H20 nodes) and to several
independent client processes.

## Adopt in one of two ways

**Sidecar proxy — zero code changes (any client, any language):**

```bash
llmfleet proxy --registry endpoints.txt --listen 127.0.0.1:9000
# everywhere else:  base_url = http://127.0.0.1:9000/v1   — done.
```

One proxy per machine additionally gives every local process a **shared
router state** (one in-flight view, one metrics poller).

**Python library — one line changed:**

```python
from llmfleet import FleetClient
client = FleetClient.from_registry("endpoints.txt", api_key="token")
data = client.post_chat({"model": "m", "messages": [...]})   # routed
```

Projects with their own transport can drive the router directly
(`client.router.acquire(key)` / `.release(idx, elapsed, ok=...)`).

## The registry (one file = the whole fleet)

```
# one OpenAI-compatible base URL per line; annotations optional
http://a100-host:8888/v1   w=1.0  max_inflight=256
http://h20-host:8888/v1    w=0.5  max_inflight=320
http://other-host:8888/v1  key=node-token  metrics=none
```

| annotation | meaning |
|---|---|
| `w` | rate **prior** (relative speed). The adaptive busy-time throughput estimate takes over once the node runs warm. |
| `max_inflight` | hard per-node concurrency cap — the client-side shadow of `max_num_seqs`; a weak node's tail-latency fuse. |
| `key` | per-node API key (heterogeneous auth). |
| `metrics` | `vllm` (default: poll `/metrics` for server truth) or `none` (degrade to local signals + prior). |

Env override: `LLMFLEET_ENDPOINTS="http://a/v1 w=1,http://b/v1"`.
Adding a node = one registry line; live sessions never remap.

## How it works (short version)

Three signal sources with strictly separated roles:

* **S — server truth**: a 3s background poll of each vLLM node's `/metrics`
  (`num_requests_running`/`waiting` are **global across all client
  processes** — this coordinates independent processes with zero IPC; token
  counter deltas give real throughput, sampled only while the node is busy).
* **L — local latency**: peak-EWMA per node; **health veto only** (durations
  are confounded by request type and cache hits — never fed to capacity)
  and the end-to-end fallback when `/metrics` is unreachable.
* **P — priors/caps**: the registry annotations above.

Two-tier scheduling:

* **Cold** (a session's first request, and every single-shot call — exactly
  the requests with **no KV cache anywhere to lose**): place by weighted
  least expected wait `(load+1)/(eff_w × slowstart)`; the winner becomes the
  session's **home** (LRU/TTL table).
* **Warm** (later turns, sibling rollouts): go home for prefix-cache hits
  while home is healthy and under its personalized bound; spill
  deterministically (a consistent *second* home) otherwise; re-home after 5
  consecutive spills (persistent saturation), with an anti-ping-pong lock.

Reliability: exponential-backoff cooldowns, slow-start ramps for
new/recovered nodes, staged degradation (metrics dead → local in-flight +
prior; table lost → rendezvous hash; all unhealthy → least-loaded).

Session identity defaults to `system + first user message` (shared by every
turn of an episode); override per request with the `X-LLMFleet-Session`
header (proxy) or `session_key=` (library).

## Ops

```bash
llmfleet probe    --registry endpoints.txt --model my-model --api-key tok
llmfleet snapshot --listen 127.0.0.1:9000        # live router state
tools/vllm_cluster.sh                             # per-host replica manager
```

## Scope (v0.1)

Non-streaming library client (the proxy streams SSE fine); vLLM metrics
adapter only (others degrade gracefully); homogeneous model name across the
fleet. Graduate to a dedicated router service (vLLM production-stack /
SGLang router) when you exceed ~3 client processes × ~8 nodes or go
cross-site — the session-key header ports directly.
