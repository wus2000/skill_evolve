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


# Replica stacks provisioned on the farm host 128 (zkgy-gpu, 64 cores / ~196GB
# free — far more headroom than the contended 162). Each stack = 4 containers,
# base_url isolated per stack. 2026-07-09 measured footprint: ~6.2GB idle per
# stack (gitlab 3.6 + shopping 1.2 + admin 1.1 + reddit 0.25). Default 24 stacks
# (~150GB idle in 128's 251GB) — DOUBLED from 12 to deepen the clean-lane buffer:
# each mutate task needs an exclusive stack, and refresh (lane cleanup) is
# HDD-bound, so more stacks = more parallel mutate lanes + more buffer so the
# eager background refresh replenishes clean lanes before a task waits (raises
# effective concurrency past the ~25 the 12-stack pool bottlenecked at).
# Grow with tools/webarena/bring_up_waves.sh <N> <wave> + WEBARENA_STACKS=N.
#
# HDD CAVEAT: 128's docker root (/home/zkgy/docker) is on a spinning disk (sda),
# so concurrent gitlab boots/reconfigures saturate disk I/O (all-12-at-once
# thrashed to load 105 / 0 progress; 4 concurrent gitlab reconfigures ~8min each
# vs ~40s solo). Mitigations: bring up in WAVES (bring_up_waves.sh, 4/wave) and
# keep webarena_refresh_concurrency LOW (2). Per-stack gitlab BAKING was tried
# and abandoned: a committed gitlab re-run skips gitlab-ctl reconfigure (env-ctrl
# sees the baked external_url already correct) and comes up with broken
# nginx->puma wiring (instant 502). So refresh recreates gitlab from the am1n3e
# base + live repoint: 163s solo (vs 162's 9-11min — still ~4x better on the idle
# box), HDD-bound under concurrency. The 12-lane pool + eager background refresh
# keep refresh off the episode critical path.
N_STACKS = int(os.environ.get("WEBARENA_STACKS", "24"))
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
            #   webarena_worker_procs  = M processes = loop cores used. 16 on
            #     127's 80c (leaves cores for chromium + AW/SS).
            #   webarena_max_contexts  = TOTAL concurrent episodes; split /M across
            #     workers. 128 ≈ 16 browsers + 128 ctx ≈ 55-65GB in 127's 111GB.
            #   webarena_browser_procs = async browsers PER worker (1-2). 16 total.
            # LLM assumed to scale (user ruling 2026-07-09). task_timeout_s bounds
            # a hung episode (worker asyncio.wait_for + main-side grace).
            "webarena_worker_procs": 16,
            "webarena_browser_procs": 1,
            "webarena_max_contexts": 128,
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
            # Refresh-storm caps (PER-SITE, scheduler.py). On 128's HDD docker
            # root concurrent recreates saturate disk I/O; gitlab's recreate is
            # ~5x slower (163s vs ~30s) so it gets its OWN small gate and the fast
            # sites (reddit/shopping/admin) share a separate gate — a slow gitlab
            # refresh no longer starves fast-site refreshes (which was collapsing
            # the clean-lane supply and queuing mutate tasks). Raise both once the
            # refresh path is on SSD/tmpfs (HDD is the current write ceiling).
            "webarena_refresh_concurrency": 3,        # fast sites (shared)
            "webarena_gitlab_refresh_concurrency": 1,  # gitlab (own, slow)
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

    if smoke:
        # End-to-end machinery validation, not science: tiny slices, single
        # rollout, one tree decision, few browsers.
        cfg.n_train, cfg.n_val, cfg.n_test = 8, 4, 4
        cfg.k_rollouts = 1
        cfg.gate_screen_k = 1
        cfg.max_decisions = 1
        cfg.batch_size = 8
        cfg.extra["webarena_worker_procs"] = 2
        cfg.extra["webarena_browser_procs"] = 1
        cfg.extra["webarena_max_contexts"] = 4

    os.makedirs(out_root, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(os.path.join(out_root, "css.log"))])
    log = logging.getLogger("css")
    log.info("CSS WebArena run starting — out_root=%s", out_root)

    target_client, optimizer_client = build_clients(cfg)
    env = build_env(cfg)
    log.info("Train=%d  Val=%d  Test=%d", len(env.train_items()),
             len(env.val_items()), len(env.test_items()))
    log.info("Config: api_workers=%d worker_procs=%s browsers/worker=%s "
             "max_contexts=%s turns=%d stacks=%d", cfg.max_api_workers,
             cfg.extra.get("webarena_worker_procs"),
             cfg.extra.get("webarena_browser_procs"),
             cfg.extra.get("webarena_max_contexts"), cfg.max_turns, len(STACKS))

    run_css(env=env, target_client=target_client,
            optimizer_client=optimizer_client,
            cfg=cfg, out_dir=out_root, max_rounds=20, resume=resume)


if __name__ == "__main__":
    main()
