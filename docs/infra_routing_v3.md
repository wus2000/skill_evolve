# Client-Side Replica Routing v3 — Infrastructure Design

> **PURE INFRASTRUCTURE.** This document describes engineering that exists
> only to run experiments faster on a multi-replica (and heterogeneous) vLLM
> fleet. It is **not part of the CSS mechanism**, plays no role in the
> paper's method, and must never be described as such. The mechanism is
> transport-agnostic: routing knobs live in client constructor params /
> `extra`, never in mechanism config fields, and the resume fingerprint
> excludes them.

Design reviewed and approved 2026-07-04. Implementation:
`css/model/routing.py` (router), `css/model/endpoints.py` (fleet registry),
`css/model/client.py` (integration), `tools/probe_endpoints.py` (ops probe).

## Problem

* Workload: multi-turn agent episodes (each turn re-sends a strictly growing
  prefix — same-replica placement turns that into vLLM prefix-cache hits;
  episode weights are heavy-tailed) + single-shot optimizer calls (no
  locality). Prefill-dominated (31–50:1 prompt:gen tokens).
* Fleet: currently 2x (A100 TP=2); future H20 144G nodes (~0.5x prefill,
  ~2x decode, larger KV pool) — heterogeneous, and a node's *effective*
  capacity drifts with the cache-hit profile of the traffic it receives.
* Clients: 2–3 independent experiment processes, no IPC.
* Measured failure of pure sticky hashing (v2's predecessor): 80/20
  busy-second split on 2 replicas from two independent experiments.

## Signals (strictly separated roles)

| Source | What | Role |
|---|---|---|
| **S — server truth** | Daemon thread polls each node's `/metrics` every 3s (+ per-process phase jitter): `num_requests_running` + `num_requests_waiting` (**global across all client processes** — this is what coordinates independent processes with zero IPC) and prompt/generation token counters (deltas → actual token throughput). | Primary load + capacity signal |
| **L — local latency** | Peak-EWMA request duration per node (spikes immediately, decays with τ=30s). | Health veto ONLY (durations are confounded by request type & cache hits — never capacity); end-to-end fallback when `/metrics` is unreachable |
| **P — priors/caps** | Registry annotations: `w` (rate prior) and `max_inflight` (hard per-node cap = client-side shadow of `max_num_seqs`). | Cold-start prior + S-dead fallback + tail-latency fuse |

Freshness: snapshot age < 2 polls = fresh; < 10 = stale (usable);
otherwise S-dead → that node degrades to local in-flight + prior weight
(other nodes unaffected).

Optimistic increments: dispatches since the last snapshot are added to the
load estimate (`load_i = running + waiting + disp_i`), so decisions inside a
poll gap self-spread; deliberately conservative (completions inside the gap
are not subtracted).

## Adaptive capacity (busy-gated)

```
rate_i = EWMA_60s( (Δprompt_tokens + Δgen_tokens) / Δt )   # per poll
updated ONLY while avg running >= 8   # idle throughput != capacity
eff_w_i = rate_i normalized; nodes without >=5 samples fill in by prior w
```

Cache hits inflate apparent throughput **by design**: that *is* effective
capacity, and the resulting positive feedback (warm node → more traffic) is
capped by the load term (more traffic → higher expected wait → equilibrium).

## Scheduling (two tiers)

**Cold** (session-table miss — first turn of an episode, every single-shot
optimizer call; i.e. exactly the requests with no KV locality to lose):

```
score_i = (load_i + 1) / (eff_w_i * slowstart_i)     slowstart: 60s linear ramp
pick argmin among {healthy, non-degraded, under max_inflight}
(constraints relax in stages if empty); ties break by the key's HRW order
winner becomes the session's HOME (LRU 200K entries / TTL 2h)
```

**Warm** (table hit — later turns, K-rollout siblings):

```
go home while healthy AND load_home < max(4, ceil(total_load * 1.25 * eff_w_home))
                              AND under max_inflight
else spill along the session's own HRW preference order (deterministic
     secondary → a spilled session warms ONE consistent second home)
>=5 consecutive spills → re-home (persistent saturation/death), 60s
     anti-ping-pong lock
```

The bound floor (4) keeps K-sibling bursts and low-load regimes on home —
bounds exist for macro balance, not to break trivial concurrency.

## KV cache-hit guarantees (mechanism inventory)

1. Warm-path home stickiness → multi-turn prefix hits (the big win).
2. K-siblings share the session key → co-located, mutually warm.
3. Deterministic spill target → a consistent *second* home, not random hops.
4. **Fleet growth = zero remap of live sessions** (table entries keep their
   homes; new sessions flow to the new node because it scores lowest) —
   strictly better than consistent hashing's 1/N remap.
5. Slow-start keeps a cold engine from being flooded before its cache warms.
6. Same-phase system prefixes need no routing help (every node sees many).
7. Table capacity/TTL >> episode lifetime; loss degrades to HRW (one cold
   start for that session only).

## Reliability walkthrough

| Scenario | Behavior |
|---|---|
| Node dies | conn error → cooldown 15s x 2^k (cap 120s) → deterministic spill → re-home after 5 spills; recovery gets slow-start; old sessions do not migrate back (their cache is gone anyway) |
| Node slow (thermal/neighbors) | L-source degrades it (cold-placement veto, halved warm bound) AND S-source rate drops (eff_w shrinks) |
| `/metrics` blocked | that node only → v2-style local-inflight + prior |
| Poll-gap herd (multi-process) | phase jitter + optimistic increments + HRW-diverse spill; worst overshoot bounded by one gap's dispatches, corrected at next snapshot |
| Everything saturated | scores stay comparable → least expected wait; `max_inflight` prevents unbounded server queues |
| Process restart | empty table → all sessions re-place cold (a resume is a cache cold-start anyway) |

## Parameters (defaults; ReplicaRouter kwargs override)

poll 3s (+jitter), busy threshold 8, rate half-life 60s (min 5 samples),
latency τ 30s / α 0.1, degraded > 3x median (n≥10), load_factor 1.25,
bound floor 4, slow-start 60s, cooldown 15s→120s exponential, re-home after
5 spills + 60s lock, session table 200K / TTL 2h.

## Explicit non-goals

No IPC or external coordination store (server truth covers it); no request
migration/cancellation; no `gpu_cache_usage`-based placement yet (recorded,
revisit with data). Graduation criterion to a dedicated router layer
(vLLM production-stack / SGLang router): ≥3 concurrent client processes AND
≥8 nodes, or cross-site deployment — the session-key logic ports as a header.
