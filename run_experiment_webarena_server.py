#!/usr/bin/env python3
"""CSS experiment on WebArena (Verified scoring, 684-task 4-domain scope) — SERVER run.

Runs on the harness host (127): browsers + evaluator local, site farm on 162
reached through localhost forwards (cron-ensured). Design + evidence:
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

FARM = "/data3/wushang/skills_evolve/webarena/scripts/farm.sh"
FARM_SSH = f"ssh -p 5102 -o BatchMode=yes haoyang@10.77.110.162 {FARM}"

# Replica stacks on 162, reached via localhost forwards on this host. Each
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
_BASE_PORT = {"shopping": 7770, "shopping_admin": 7780,
              "reddit": 9999, "gitlab": 8023}
_ADMIN_SUFFIX = {"shopping_admin": "/admin"}


def _stack_urls(n: int) -> "dict[str, str]":
    pfx = "" if n == 1 else str(n - 1)          # s1="" s2=1 s3=2 ...
    return {site: f"http://localhost:{pfx}{port}{_ADMIN_SUFFIX.get(site, '')}"
            for site, port in _BASE_PORT.items()}


# 6 replica stacks provisioned on 162 (2026-07-08): 24 containers, all sites
# healthy + authenticated, base_url isolated per stack. Grow with
# tools/webarena/scale_pool.sh <N> then WEBARENA_STACKS=<N>.
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

        # LLM parallelism (optimizer + agent calls). Browser concurrency is a
        # SEPARATE budget: extra["webarena_max_browsers"] below.
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
            # 40 concurrent chromium instances (user-agreed 2026-07-06):
            # measured 127 at load 11/80 cores, 122GB RAM available with all
            # four experiments live; 40 browsers ~= 15-20GB + moderate CPU.
            "webarena_max_browsers": 40,
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
            # Refresh-storm cap: concurrent recreates on the farm host degrade
            # each other (gitlab 81s idle -> 4-7 min under storm, measured
            # 2026-07-06); eager refresh hides the queueing.
            "webarena_refresh_concurrency": 3,
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
        cfg.extra["webarena_max_browsers"] = 6

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
    log.info("Config: workers=%d browsers=%s turns=%d stacks=%d",
             cfg.max_api_workers, cfg.extra.get("webarena_max_browsers"),
             cfg.max_turns, len(STACKS))

    run_css(env=env, target_client=target_client,
            optimizer_client=optimizer_client,
            cfg=cfg, out_dir=out_root, max_rounds=20, resume=resume)


if __name__ == "__main__":
    main()
