#!/usr/bin/env python3
"""CSS experiment on WebArena (Verified scoring, 684-task 4-domain scope) — SERVER run.

Runs on the harness host (127): browsers + evaluator local, site farm on 128
(zkgy-gpu — replaced 162 on 2026-07-09; 162 keeps ONLY the LLM arms) reached
through localhost forwards (cron-ensured). Design + evidence:
docs/env_prep/webarena_PREP.md. Per-env defaults below follow the house
convention: every value carries its rationale; agreed values must not change
silently.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))

from css.config import CSSConfig
from css.model.client import build_clients
from css.envs.registry import build_env
from css.orchestrator import run_css

FARM = "/home/wushang/skills_evolve/webarena/scripts/farm.sh"
FARM_SSH = f"ssh -p 2822 -o BatchMode=yes wushang@10.77.110.128 {FARM}"

# Replica stacks on the farm host (128), reached via localhost forwards on this
# host — the URLs are localhost:PORT (host-agnostic), so migrating the farm only
# moves the forward target (wa_forwards_ensure.sh) and FARM_SSH above. Each
# stack sN uses a port prefix (s1="" s2=1 s3=2 ...; sN -> N-1), matching
# farm.sh's prefix() and wa_forwards_ensure.sh. Concurrency scales with the
# stack count: set WEBARENA_STACKS=N (env) to grow the pool — provision the
# replicas first with tools/webarena_scale.sh N, which builds/starts sN,
# mounts forwards, and logs in. Site keys MUST match the dataset's `sites`.
#
# shopping_admin carries the "/admin" suffix because that IS the site's base
# URL upstream: WebArena's browser_env/env_config.py defines
# ``SHOPPING_ADMIN = f"http://{HOST}:7780/admin"`` and every task config spells
# it out (``"start_url": "http://<host>:7780/admin"``). Without it the
# ``__SHOPPING_ADMIN__`` placeholder expands to the storefront and all 182
# admin tasks start on the wrong page (2026-07-08 probe: 7780/ is "Home Page",
# 7780/admin is "Magento Admin").
# Port scheme mirrors farm.sh host_port() EXACTLY (that file is the single source
# of truth): each stack owns a contiguous block BASE + (n-1)*STRIDE + site_offset.
# The old "${n-1}${base}" concat capped the pool at s6 (s7 shopping = 67770 >
# 65535, docker refused it); blocks from 30000 scale to ~50 stacks and stay below
# the 32768 ephemeral floor. wa_forwards_ensure.sh mirrors the same three consts.
_FARM_BASE_PORT = int(os.environ.get("WEBARENA_BASE_PORT", "30000"))
_STACK_STRIDE = 10
_SITE_OFFSET = {"shopping": 0, "shopping_admin": 1, "reddit": 2, "gitlab": 3}
_ADMIN_SUFFIX = {"shopping_admin": "/admin"}


def _stack_urls(n: int) -> "dict[str, str]":
    block = _FARM_BASE_PORT + (n - 1) * _STACK_STRIDE
    return {site: f"http://localhost:{block + off}{_ADMIN_SUFFIX.get(site, '')}"
            for site, off in _SITE_OFFSET.items()}


# Replica stacks provisioned on the farm host 128 (zkgy-gpu, 64 cores / 251GB).
# Each stack = 4 containers, base_url isolated per stack. HYBRID daemons
# (2026-07-09, HDD-aversion driven): the heavy-boot sites — gitlab (reconfigure
# thrash) + shopping/admin (magento + elasticsearch) — run on a ROOTLESS docker
# daemon with data-root on /dev/shm (RAM tmpfs, 126GB); only reddit (postgres+
# rails, light, no ES) runs on the shared HDD daemon. Measured on HDD: gitlab
# refresh >5min under load and shopping stalled booting ES at 8min, so every
# heavy site goes to RAM; reddit is HDD-fine (~30s). gitlab's RAM writable is tiny
# (~0.5GB/stack), so magento's writable is the real budget item.
# Footprint: RAM images = gitlab 31.6 + shopping 9.9 + admin 2.9 = ~44GB (fixed)
# + per-stack WARM writable ~7.7GB (gitlab ~0.9 + admin ~0.35 + shopping ~6.5 —
# shopping's running mysql + elasticsearch DATA lands on the container writable
# layer, which rootless `docker ps --size` UNDERCOUNTS; the true tmpfs cost only
# shows in `df /dev/shm`). So N ≈ (90-44)/7.7 ≈ 6 keeps ~36GB tmpfs free for the
# SHARED host's ~45 tenants (co-tenant safety is the hard limit) — this 6-lane cap
# is now the throughput ceiling. Lifting it needs magento's DB/ES moved off the
# per-stack writable (image surgery). reddit data is on the TB HDD, off budget.
# Grow with WEBARENA_STACKS=N + bring_up_waves.sh <N> <wave> — but WATCH
# `df /dev/shm`: warm magento can hit 100% and break co-tenants.
#
# gitlab NOTE: refresh = recreate from am1n3e base + live repoint (per-stack
# BAKING stays abandoned: a baked re-run skips reconfigure -> 502 nginx->puma). On
# RAM the reconfigure no longer thrashes so its gate goes high (8). Eager
# background refresh + the deep lane pool keep refresh off the episode critical path.
N_STACKS = int(os.environ.get("WEBARENA_STACKS", "6"))
STACKS = {f"s{n}": _stack_urls(n) for n in range(1, N_STACKS + 1)}

# The stack origins WITHOUT any path suffix — health probes, cookie jars and
# the evaluator's URL normalizer all key on origin, not on the task's entry
# path. Derived once so the two views can never drift apart.
def stack_origins(stack: str) -> "dict[str, str]":
    out = {}
    for site, url in STACKS[stack].items():
        scheme, _, rest = url.partition("://")
        out[site] = f"{scheme}://{rest.split('/', 1)[0]}"
    return out


def _write_verified_env_config(path: str) -> str:
    """Evaluator config: every stack URL registered per site placeholder so
    NetworkEventEvaluator normalizes HAR URLs from any stack."""
    def urls(site: str) -> list:
        return [s[site] for s in STACKS.values()]
    cfg = {"environments": {
        "__SHOPPING__": {"urls": urls("shopping")},
        "__SHOPPING_ADMIN__": {"urls": urls("shopping_admin")},
        "__REDDIT__": {"urls": urls("reddit")},
        "__GITLAB__": {"urls": urls("gitlab")},
    }}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(cfg, f, indent=1)
    return path


def _latest_run_dir() -> "str | None":
    import glob
    runs = sorted(glob.glob("runs/webarena_*"))
    return runs[-1] if runs else None


def _run_throughput_probe(cfg, env, target_client, out_root) -> None:
    """Benchmark-only (env WEBARENA_THROUGHPUT_PROBE=1): measure the TASK-
    ENVIRONMENT throughput ceiling — browser-context acquire + page nav + AXTree
    parse, driven through the real multiprocess browser pool + lease scheduler,
    but WITHOUT the LLM (webarena_scripted_probe short-circuits run_episode_async).
    Isolates env capacity from LLM latency (which the user said is not the ceiling).
    Knobs: WEBARENA_PROBE_STEPS (parses/episode, default 8), WEBARENA_PROBE_ITEMS
    (total episodes, default 640), WEBARENA_PROBE_C (concurrency; default =
    webarena_max_contexts)."""
    import time
    from css.rollout.batch import batch_rollout
    log = logging.getLogger("css")
    steps = int(os.environ.get("WEBARENA_PROBE_STEPS", "8"))
    n_items = int(os.environ.get("WEBARENA_PROBE_ITEMS", "640"))
    conc = int(os.environ.get("WEBARENA_PROBE_C",
                              str(cfg.extra.get("webarena_max_contexts", 128))))
    cfg.extra["webarena_scripted_probe"] = steps   # workers read it at spawn
    env.scorer = None                              # skip Verified scoring (synthetic ids)
    if os.environ.get("WEBARENA_PROBE_WORKERS"):   # sweep parse-core count
        cfg.extra["webarena_worker_procs"] = int(os.environ["WEBARENA_PROBE_WORKERS"])

    # Synthetic READ items across every stack's 4 sites (read task_type => shared
    # leases => full concurrency). Site mix ~ realistic (shopping/reddit heavy).
    ph = {"shopping": "__SHOPPING__", "shopping_admin": "__SHOPPING_ADMIN__",
          "reddit": "__REDDIT__", "gitlab": "__GITLAB__"}
    mix = ["shopping"] * 4 + ["reddit"] * 3 + ["shopping_admin"] * 2 + ["gitlab"]
    items = [{"task_id": 900000 + i, "id": f"probe{i}", "intent": "probe",
              "sites": [mix[i % len(mix)]], "start_urls": [ph[mix[i % len(mix)]]],
              "eval": {}} for i in range(n_items)]

    def _nt(r):
        return (r.get("n_turns", 0) if isinstance(r, dict)
                else int(getattr(r, "n_turns", 0) or 0))

    out_dir = os.path.join(out_root, "throughput_probe")
    log.info("THROUGHPUT PROBE: %d episodes x %d parses, C=%d, %d stacks",
             n_items, steps, conc, len(STACKS))
    t0 = time.time()
    results = batch_rollout(env, items, "", target_client, k_rollouts=1,
                            out_dir=out_dir, max_workers=conc,
                            task_timeout=int(getattr(cfg, "task_timeout_s", 600) or 600))
    dt = max(1e-6, time.time() - t0)
    done = sum(1 for r in results if _nt(r) >= 1)
    full = sum(1 for r in results if _nt(r) >= steps)
    parses = sum(_nt(r) for r in results)
    msg = (f"THROUGHPUT_RESULT eps_min={n_items/(dt/60.0):.1f} "
           f"episodes_ok={done}/{n_items} full_{steps}parse={full} "
           f"parses_per_s={parses/dt:.1f} wallclock_s={dt:.1f} C={conc}")
    log.info(msg)
    print(msg, flush=True)


def _run_e2e_measure(cfg, env, target_client, out_root) -> None:
    """REAL end-to-end throughput (env WEBARENA_E2E_MEASURE=1): one batch_rollout
    over the train set at C=webarena_max_contexts with the REAL LLM + real Verified
    scoring + real mutate/refresh (the actual training-pass workload). Reports
    wall-clock eps/min, pass rate, mean turns. WEBARENA_E2E_ITEMS caps the task
    count (default: all train); WEBARENA_E2E_C overrides concurrency."""
    import time
    from css.rollout.batch import batch_rollout
    from css.envs.webarena.env import task_type_of
    log = logging.getLogger("css")
    items = list(env.train_items())
    n = int(os.environ.get("WEBARENA_E2E_ITEMS", str(len(items))))
    items = items[:max(1, n)]
    conc = int(os.environ.get("WEBARENA_E2E_C",
                              str(cfg.extra.get("webarena_max_contexts", 128))))
    n_mut = sum(1 for it in items if task_type_of(it) == "mutate")
    log.info("E2E MEASURE: %d train tasks (%d mutate / %d read), C=%d, %d stacks, "
             "REAL LLM+scoring", len(items), n_mut, len(items) - n_mut, conc, len(STACKS))
    t0 = time.time()
    results = batch_rollout(env, items, "", target_client, k_rollouts=1,
                            out_dir=os.path.join(out_root, "e2e_measure"),
                            max_workers=conc,
                            task_timeout=int(getattr(cfg, "task_timeout_s", 1800) or 1800))
    dt = max(1e-6, time.time() - t0)
    g = lambda r, k: (r.get(k, 0) if isinstance(r, dict) else getattr(r, k, 0))
    passed = sum(1 for r in results if g(r, "hard"))
    turns = sum(int(g(r, "n_turns") or 0) for r in results)
    msg = (f"E2E_RESULT tasks={len(items)} eps_min={len(items)/(dt/60.0):.2f} "
           f"pass={passed}/{len(items)}({100*passed/max(1,len(items)):.0f}%) "
           f"mean_turns={turns/max(1,len(items)):.1f} wallclock_min={dt/60.0:.1f} "
           f"C={conc} mutate={n_mut}")
    log.info(msg)
    print(msg, flush=True)


def main() -> None:
    resume = False
    resume_dir = None
    smoke = "--smoke" in sys.argv[1:]
    if len(sys.argv) > 1 and sys.argv[1] == "--resume":
        resume = True
        resume_dir = sys.argv[2] if len(sys.argv) > 2 else _latest_run_dir()
        if not resume_dir:
            print("--resume: no existing run dir found")
            sys.exit(1)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = "webarena_smoke" if smoke else "webarena"
    out_root = resume_dir if resume else f"runs/{prefix}_{timestamp}"
    env_config = _write_verified_env_config(
        os.path.join(out_root, "wa_env_config.json"))

    cfg = CSSConfig(
        env_name="webarena",
        # Sealed template-disjoint splits (commit d8289f3): 446/67/171 over the
        # Verified 684 pool. 0 = whole split (guide §2.9).
        n_train=0, n_val=0, n_test=0,
        seed=42,

        # 30 turns = the community-standard WebArena step budget (PREP §3).
        max_turns=30,

        # LLM parallelism (optimizer + agent calls). Browser/episode concurrency
        # is a SEPARATE budget: webarena_browser_procs + webarena_max_contexts
        # below (async BrowserPool).
        max_api_workers=256,
        # 30 min per-rollout wall-clock: measured avg episode 152s, max-turns
        # worst case ~8 min; headroom for lane waits + evaluator.
        task_timeout_s=1800,

        # L0 exploitation (house defaults, same as the other five envs).
        batch_size=40,
        k_rollouts=3,
        gate_screen_k=3,   # val=67 (small) -> 3-vote screen per house rule
        # Latest mechanism (2026-07-10, matches SS/AW): editpipe v3
        # (plan/draft/review/apply). consolidation stays off (default False).
        edit_pipeline="v3",

        # ── Tree mechanism (agreed 2026-07-05 rulings, shared across envs) ──
        burst_steps=5,
        saturation_dry_bursts=2,
        node_degree=3,
        max_decisions=40,
        verify_mode="harm_veto",

        target_model="qwen3.6-35b-a3b",
        optimizer_model="qwen3.6-35b-a3b",
        out_root=out_root,

        extra={
            "llm_backend": "openai_compat",
            # FOUR arms via llmfleet (162 dual LAN + guarded relay tunnels).
            # NB: the vLLM arms stay on 162 — only the WebArena site farm moved
            # to 128 (2026-07-09). LLM host != farm host by design.
            "base_url": ("http://10.77.110.162:8888/v1,"
                         "http://10.77.110.162:8889/v1,"
                         "http://127.0.0.1:8888/v1,"
                         "http://127.0.0.1:8889/v1"),
            "api_key": "token-abc123",
            "max_tokens": 24576,   # client ceiling (clamp) — house value
            # Two sampling domains (user ruling 2026-07-08):
            #  - rollouts sample (0.6) so the K repeats of a task actually
            #    differ; contrastive groups and the paired gate's variance
            #    estimate depend on that spread. Previously each env picked its
            #    own (0.0 / 0.4 / 0.7) — a silent, unowned divergence.
            #  - every other call is greedy (0.0) for reproducibility.
            "target_temperature": 0.6,
            "optimizer_temperature": 0.0,
            "enable_thinking": False,
            "timeout_seconds": 1800,
            "optimizer_json_mode": True,

            # ── WebArena env knobs (PREP §7) ──
            "webarena_split_dir": "data/webarena_splits",
            "webarena_stacks": STACKS,
            # Browser/episode concurrency (multi-process pool, css/envs/webarena/
            # worker_pool.py). The old design launched one thread-affine chromium
            # per episode (~2-4GB) → capped ~24 on 127; the single-loop async pool
            # then hit the GIL (one event-loop thread = one core). So we run M
            # worker PROCESSES (M cores), each a single-loop async BrowserPool:
            #   webarena_worker_procs  = M processes = loop cores used. 32 on
            #     127's 80c (measured 2026-07-09 throughput probe: M=16 -> 9.7
            #     parses/s / 72.8 short-eps/min; M=32 -> 14.4 parses/s / 107.9
            #     short-eps/min at C=128, 0 failures over 640+320 episodes; +48%.
            #     Sub-linear past ~16 cores as page-load/forward transport
            #     co-limits, so 32 is the knee; 48c left for chromium + AW/SS).
            #   webarena_max_contexts  = TOTAL concurrent episodes; split /M across
            #     workers. 128 total (M=32 -> 4 ctx/worker).
            #   webarena_browser_procs = async browsers PER worker (1-2).
            # LLM assumed to scale (user ruling 2026-07-09). task_timeout_s bounds
            # a hung episode (worker asyncio.wait_for + main-side grace).
            # 2026-07-10 CORRECTION: C=128 above was validated on a SYNTHETIC
            # throughput probe (short scripted episodes, 0 failures over 640+320).
            # The first REAL formal run (long multi-turn episodes; mutate tasks lock
            # a per-site lane for minutes) hit lane waits up to 29.5min >= the 30min
            # episode budget -> 49 worker TimeoutErrors + 2 magento containers
            # OOM-killed (exit 137: wa_shopping_s3, wa_shopping_admin_s2), which
            # deflated the baseline (val 0.036 was an artifact). Concurrency must
            # match the 6-stack farm's per-site lane capacity, not a synthetic probe.
            "webarena_worker_procs": 24,
            "webarena_browser_procs": 1,
            "webarena_max_contexts": 24,
            "webarena_verified_cli":
                "/home/wushang/miniconda3/envs/webarena/bin/webarena-verified",
            "webarena_env_config": env_config,
            # Lane refresh: recreate ONE site container from its source image
            # (reddit/gitlab init API is broken upstream — issue #39; recreate
            # is the universal reset). farm.sh refresh BLOCKS until the site
            # serves again (readiness gate, READY_TIMEOUT=1200 on the farm) —
            # gitlab boots from the original image and needs multiple minutes
            # (502 for 4+ min observed in the 2026-07-06 smoke), so the
            # subprocess ceiling must exceed the farm-side gate.
            "webarena_refresh_cmd": FARM_SSH + " refresh {stack} {site}",
            "webarena_refresh_timeout_s": 1500,
            # Refresh-storm caps (PER-SITE, scheduler.py). HYBRID farm (2026-07-09,
            # HDD-aversion driven): the heavy-boot sites — gitlab (gitlab-ctl
            # reconfigure = random-write thrash) + shopping/admin (magento +
            # elasticsearch = disk-heavy boot) — run on the ROOTLESS RAM daemon;
            # only reddit (postgres+rails, light, no ES) runs on the shared HDD
            # daemon. Measured on HDD: gitlab refresh >5min under load, shopping
            # still booting ES at 8min; on RAM all three refresh in ~20-125s with no
            # thrash, so gates go high. gitlab keeps its own gate (reconfigure is CPU,
            # ~125s, 8 concurrent fits the 64-core box); the "fast" gate covers
            # shopping/admin (RAM, ~24s) + reddit (HDD, ~30s, light) — a shared 8 is
            # ample (their combined demand is well under supply). (docker save stalled
            # 0-byte on the loaded daemon, so RAM images came via export|import.)
            "webarena_refresh_concurrency": 8,         # shopping/admin (RAM) + reddit (HDD)
            "webarena_gitlab_refresh_concurrency": 8,  # gitlab, RAM daemon (no thrash)
            "webarena_har_content": "omit",  # URLs+status suffice for evaluator
            "webarena_nav_timeout_ms": 30000,
            # ── Authentication (css/envs/webarena/auth.py) ──
            # Over half the benchmark acts as a logged-in user (mutate = 52% of
            # train / 63% of test). shopping/reddit/gitlab get per-(stack,site)
            # cookie jars from a real UI login, regenerated by the refresh hook
            # because a container recreate kills the server session; the admin
            # site rides the image's auto-login HTTP header instead and needs no
            # regeneration. Bootstrap once: tools/webarena_login.py
            "webarena_auth_dir": ".auth/webarena",
            # ── Trajectory history (agreed 2026-07-08: single-turn prompts
            # + two-layer harness history; wa_0784 A/B probe evidence) ──
            # Env-side scribe: one extra LLM call per PAGE-CHANGING step
            # (no-change steps fold mechanically and skip it). Clerk-not-
            # adviser prompt — facts/intent only, never advice. Part of the
            # ENVIRONMENT: baselines keep it too, so skill-document gains are
            # measured on top of it. False = ablation arm.
            # Stuck-stop: end an episode after N consecutive steps that change
            # nothing on the page (invalid / failed / no-effect actions). The
            # loop pathology burned 24-28 no-op steps to max_turns in the
            # 2026-07-08 probes. 5 (> official's parsing/repeating th of 3)
            # gives a briefly-confused agent room to recover on the coarser
            # AXTree-line change signal while still cutting real loops early.
            "webarena_stuck_stop_steps": 5,
            "webarena_scribe": True,
            # Telemetry-only alert when the rendered history block exceeds
            # this token count (tokens-not-chars ruling). NO truncation ever
            # (rich-content ruling): bloat control is structural — <=3 facts
            # per step, empty-by-default scribe output, mechanical folding.
            "webarena_history_budget_tokens": 3000,
        },
    )

    if os.environ.get("WEBARENA_L0_ONLY"):
        # L0-EXPLOITATION-ONLY variant (user request 2026-07-10): root node
        # bursts only — no spawns, no exploration sessions, no tree branching.
        # Pure config, no mechanism change:
        #  - max_decisions = N bursts (a decision = one burst when spawning
        #    is impossible), default 4 => 20 L0 steps;
        #  - spawn_supply_lambda=0 => spawn_score==0, arbitration always
        #    keeps exploiting (ties keep exploiting by design);
        #  - saturation_dry_bursts=99 => the hard-stall forced-spawn backstop
        #    can't trigger inside the budget (window 99*5 steps >> 20).
        # n_val is set to the REAL val size (67): meaningful_delta() reads
        # cfg.n_val and 0 (=whole split) degenerates the delta to 2.0, which
        # would misjudge every best as non-meaningful (bug noted 2026-07-10,
        # mechanism fix to be ruled on separately; 67 == whole split here).
        cfg.max_decisions = int(os.environ.get("WEBARENA_L0_BURSTS", "4"))
        cfg.spawn_supply_lambda = 0.0
        cfg.saturation_dry_bursts = 99
        cfg.n_val = 67
        print(f"L0-ONLY variant: bursts={cfg.max_decisions} "
              f"(spawn disabled, saturation backstop parked)", flush=True)

    if smoke:
        # GAP-2 mechanism-engagement check (not science): a few tasks, single
        # rollout, one tree decision — but real concurrency so it finishes fast
        # (the farm is validated at C=128). Goal: confirm run_css produces
        # non-empty, sensible rules from the trajectories + distilled eval
        # feedback and val evaluates, in one round, without crashing.
        cfg.n_train, cfg.n_val, cfg.n_test = 16, 8, 8
        cfg.k_rollouts = 1
        cfg.gate_screen_k = 1
        cfg.max_decisions = 1
        cfg.batch_size = 16
        cfg.extra["webarena_worker_procs"] = 16
        cfg.extra["webarena_browser_procs"] = 1
        cfg.extra["webarena_max_contexts"] = 48

    os.makedirs(out_root, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(os.path.join(out_root, "css.log"))])
    log = logging.getLogger("css")
    log.info("CSS WebArena run starting — out_root=%s", out_root)

    target_client, optimizer_client = build_clients(cfg)
    env = build_env(cfg)
    if os.environ.get("WEBARENA_THROUGHPUT_PROBE"):
        _run_throughput_probe(cfg, env, target_client, out_root)
        return
    if os.environ.get("WEBARENA_E2E_MEASURE"):
        _run_e2e_measure(cfg, env, target_client, out_root)
        return
    log.info("Train=%d  Val=%d  Test=%d", len(env.train_items()),
             len(env.val_items()), len(env.test_items()))
    log.info("Config: api_workers=%d worker_procs=%s browsers/worker=%s "
             "max_contexts=%s turns=%d stacks=%d", cfg.max_api_workers,
             cfg.extra.get("webarena_worker_procs"),
             cfg.extra.get("webarena_browser_procs"),
             cfg.extra.get("webarena_max_contexts"), cfg.max_turns, len(STACKS))

    run_css(env=env, target_client=target_client,
            optimizer_client=optimizer_client,
            cfg=cfg, out_dir=out_root, max_rounds=(1 if smoke else 20),
            resume=resume)


if __name__ == "__main__":
    main()
