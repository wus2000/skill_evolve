#!/usr/bin/env python3
"""CSS experiment on AppWorld (interactive coding agent) — SERVER run.

Mirrors run_experiment_alfworld_server.py; only the env, its data paths, and
the env-specific knobs differ. Benchmark audit + deployment notes:
docs/env_prep/appworld_PREP.md. Config values below were negotiated
one-by-one on 2026-07-04 (per-env default-config convention: the launcher is
the carrier; agreed values must not change silently).

Split design (canonical four-way — community-comparable):
  train 90 = optimization pool / val = dev 57 (paired gate) /
  test = test_normal 168 (mechanism reporting) / test_challenge 417 SEALED
  (never in config; standalone eval only, unsealed twice: bare + final best).

Server prerequisites:
  * conda env:  ~/miniconda3/envs/appworld  (python 3.11 + appworld 0.2.0
    installed from the vendored clone's wheel — PyPI only carries 0.1.x!)
  * data:       /home/wushang/workspace/data/appworld_root  ($APPWORLD_ROOT,
    contains data/{datasets,tasks,base_dbs,api_docs}, version 0.2.0)
"""
from __future__ import annotations  # server runs Python 3.8: keep `X | None` lazy

import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))

from css.config import CSSConfig
from css.envs.common.subprocess_worker import ensure_nofile_limit
from css.envs.registry import build_env
from css.model.client import build_clients
from css.orchestrator import run_css
from css.model.endpoints import resolve_base_url


DATA_BASE = "/home/wushang/workspace/data"


def _latest_run_dir() -> str | None:
    """Most recent runs/appworld_* directory (for --resume with no path)."""
    import glob
    runs = sorted(glob.glob("runs/appworld_*"))
    return runs[-1] if runs else None


def main() -> None:
    resume = False
    resume_dir = None
    if len(sys.argv) > 1 and sys.argv[1] == "--resume":
        resume = True
        resume_dir = sys.argv[2] if len(sys.argv) > 2 else _latest_run_dir()
        if not resume_dir:
            print("--resume: no existing run dir found")
            sys.exit(1)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_root = resume_dir if resume else f"runs/appworld_{timestamp}"

    # 128 workers x 3 pipes + HTTP sockets exceed the common 1024 default.
    ensure_nofile_limit(8192)

    cfg = CSSConfig(
        # ── Environment ──────────────────────────────────────────────────
        env_name="appworld",

        # Canonical splits (agreed: external comparability outranks internal
        # gate power, which K=3 escalation buys back).
        n_train=90,
        n_val=57,
        n_test=168,
        data_root=f"{DATA_BASE}/appworld_root",

        # LLM (remote OpenAI-compatible endpoint, reachable from this server)
        target_model="qwen3.6-35b-a3b",
        optimizer_model="qwen3.6-35b-a3b",

        # Runtime. Each rollout runs its episode in a dedicated py3.11 worker
        # subprocess; a loaded world is ~300-500MB RSS -> engine slots are the
        # RAM-bound cap (128 ~= 40-64GB; calibrate after live measurement).
        max_api_workers=300,   # raised 256->300 (2026-07-07, user decision:
                               # fresh-restart pair AW=300 / SS=512 on the
                               # four-arm llmfleet pool); engine slots stay 128
                               # (RAM-bound physical cap — the EngineSlotLimiter
                               # is the env throttle)
        concurrency_limit=1,
        task_timeout_s=1800,   # 50 interactions x worst-case LLM latency + eval
        max_turns=50,          # mirrors appworld_max_interactions (generic field)
        k_rollouts=3,

        # L0 exploitation. batch = 45 (half the 90-task pool per step;
        # agreed 2026-07-05 — replaces the initial full-pool-90 setting to
        # double step throughput). Batches are UNIFORM shuffle partitions
        # (per-node independent streams, user ruling 2026-07-08); the
        # difficulty-weighted sampling once noted here was never wired in
        # and stays off — weighting would bias the fragile statistics.
        batch_size=45,
        # Per-edit verification floor: ABSOLUTE 8 tasks (agreed 2026-07-05;
        # replaces the derived batch//8 which would give 5 at batch=45).
        verify_floor_tasks=8,
        minibatch_size=16,
        reflect_mode="plan_a",
        merger_granularity="point",
        # Edit pipeline v3 (user decision 2026-07-07: fresh restart on the
        # full redesigned mechanism): plan/draft/review/apply consolidation
        # with ###-level DSP, group-level ablation verify, and lossless
        # fallback. See docs/editpipe_v3_design.md. "v2" kept as rollback.
        edit_pipeline="v3",
        # Budget-bounded L0 (V3.4) — same policy as the Bird/ALFWorld arms.
        min_l0_epochs=0,
        max_l0_steps=20,
        l0_stall_steps=8,

        # L0 val gate: item-paired two-stage gate on the full 57-item dev
        # carve. K=1 screen + K=3 escalation (agreed 2026-07-04): the standard
        # two-stage design; escalation buys back power on the small val.
        gate_mode="paired",
        gate_screen_k=1,
        gate_escalation_k=3,

        # Burst-end document metabolism (user rulings 2026-07-08, design
        # docs/L0_document_metabolism.md): live audit measured 15K->88K rules
        # growth over 11 steps with zero gain past the step-2 best — one
        # whole-document tidy-up per burst, accepted only through the
        # symmetric-fresh non-inferiority gate (margin 1.5pp ~ 1 sigma of the
        # val mean; bullet budget 15 as the curation trigger signal).
        # DISABLED (user ruling 2026-07-08 evening): three real-document
        # smoke rounds showed the single burst-end tidy-up call cannot yet
        # deliver deep lossless compression (best round: -37% but 97 lost-
        # identifier flags, mostly checker false positives; SS: rewrite
        # bodies re-split by the ### self-heal). Code kept for later
        # revisiting; bloat control now rests on the differential upstream
        # chain (REFLECT coverage test / DRAFT four-way classification /
        # REVIEW redundant_with_doc / APPLIER minimal-complete rewrite).
        consolidation_enabled=False,
        consolidation_margin=0.015,
        l0_section_bullet_budget=15,
        # Token-based size budgets (user ruling: accounting is tokens, never
        # chars). Section over 1500 tokens = mandatory tidy-up target (the
        # bullet trigger alone missed the fattest nested-bullet section);
        # plan calls slice above 15K document tokens (a single full-output
        # call over a 28K-token document truncates at the 16K completion cap).
        l0_section_token_budget=1500,
        consolidation_split_tokens=15000,

        # ── L1 TREE SEARCH (mechanism default since 2026-07-06; design:
        #    L1_tree_mechanism_design.md — run_css delegates to the
        #    burst-granular tree loop; legacy L0 budget knobs are inert) ──
        burst_steps=5,              # one tree visit = 5 L0 steps (agreed)
        saturation_dry_bursts=2,    # W_hard: 2 bursts with NO MEANINGFUL new best
                                    # => saturated (2026-07-08 ruling lineage:
                                    # zero-accept -> no-anb -> no-MEANINGFUL-anb)
        # ── L1 action layer (user rulings 2026-07-08) ──
        saturation_meaningful_tasks=2,   # delta = max(2/n_val, 1pp): a new best
                                         # must clear >= 2 net val tasks
        saturation_meaningful_floor=0.01,
        spawn_soft_bursts=1,             # W_soft: 1 dry burst unlocks spawn
                                         # arbitration (supply vs recent pace)
        spawn_supply_lambda=0.05,        # 20% open supply ~ 1pp/burst pace
        fragile_rate=0.4,                # mature pass rate < 0.4 = fragile
        fragile_mature_step=2,           # attempts at step>=2 in a burst count
        fragile_mature_min=3,
        fragile_hist_attempts=4,
        coverage_recent_len=12,
        node_degree=3,              # REFINE children per strategy node; root unlimited (user ruling)
        max_decisions=40,           # decision budget; NOT fingerprinted — resume may extend
        verify_mode="harm_veto",    # per-edit probe only vetoes measured net harm (fix 2026-07-05)

        # Dataset-size subsets: the 90-task pool leaves no room for sampling.
        coldstart_train_size=0,
        exploitation_val_size=0,
        analysis_train_size=0,

        # L1 strategy cycle (v3) — shrunk to fit the 90-task pool (48+24 <= 90).
        l1_diagnostic_tasks=48,
        l1_regression_tasks=24,

        # Analysis prompt-shape fixes (first env to run with both ON; legacy
        # envs keep False until their in-flight experiments finish):
        json_list_wrap=True,        # un-flatten list-shaped optimizer outputs
        analysis_env_context=True,  # analyzer sees the REPL/termination semantics

        out_root=out_root,

        extra={
            "llm_backend": "openai_compat",
            # FOUR arms via llmfleet: 162 dual (LAN) + 127:8888/8889 guarded
            # ssh tunnels to 173.0.69.2 (guard: ServerAliveInterval=15 +
            # auto-restart; verified 2026-07-06 — same model, 24/24 concurrent
            # OK). LITERAL string — bypasses the endpoint registry (BFCL
            # mis-routing lesson, commit 5ed3f91).
            "base_url": ("http://10.77.110.162:8888/v1,"
                         "http://10.77.110.162:8889/v1,"
                         "http://127.0.0.1:8888/v1,"
                         "http://127.0.0.1:8889/v1"),
            "api_key": "token-abc123",
            "max_tokens": 24576,  # client CEILING (clamp), not a request: raised 16384->24576 on 2026-07-05 — the merger requests 20480 and was being silently clamped (one measured truncation); callers still request less
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
            # AppWorld env knobs (css/envs/appworld/task_interface.py).
            "appworld_python": "/home/wushang/miniconda3/envs/appworld/bin/python",
            "appworld_root": f"{DATA_BASE}/appworld_root",
            # 50 = the official minimal-ReAct notebook cap (and our ALFWorld
            # value) — agreed alignment with the community baseline protocol.
            "appworld_max_interactions": 50,
            # 0.4 everywhere (training AND eval): K=3 needs sampling diversity
            # on a deterministic env; one temperature keeps internal baselines
            # directly comparable (ALFWorld precedent).
            "appworld_temperature": 0.4,
            # Agent-turn completion cap: 16384 (user ruling 2026-07-06 —
            # unify target-agent caps at 16K across arms; supersedes the
            # 4096 runaway bound).
            # Target (task-agent) completion cap. 8K (user ruling 2026-07-09):
            # trims per-generation decoding-collapse waste while covering real
            # ReAct steps; the OPTIMIZER keeps the 16K floor (client ceiling
            # below stays 24576). NOT in the resume fingerprint, so lowering it
            # does not invalidate cached rollouts.
            "appworld_max_tokens": 8192,
            # Agreed 2026-07-05: NO observation truncation for AppWorld (the
            # provisional 6000-char cap is retired). 0 = unlimited — the
            # worker passes outputs through verbatim; the prompt still
            # steers the agent toward narrow queries to keep context lean.
            "appworld_obs_max_chars": 0,
            # RAM-bound engine cap; holds across concurrent batches via the
            # env-internal EngineSlotLimiter. Calibrate after RAM measurement.
            "appworld_engine_slots": 128,
            # Eval-annotation GT richness (ablation knob): "solution" attaches
            # the gold solution code on top of the per-requirement evaluation
            # report; the ablation arm uses "tests" (report only).
            "appworld_gt_mode": "solution",
        },
    )
    # ── Variant switches (env-var driven; the agreed defaults above stay
    #    intact — per-env default-config convention). Same semantics as the
    #    WEBARENA_L0_ONLY block (2026-07-10), generalized for this launcher. ──
    if os.environ.get("CSS_L0_ONLY"):
        # L0-EXPLOITATION-ONLY variant: root node bursts only — no spawns, no
        # exploration sessions, no tree branching. Pure config:
        #  - max_decisions = N bursts (a decision == one burst when spawning
        #    is impossible);
        #  - spawn_supply_lambda=0 => spawn_score==0, arbitration always
        #    keeps exploiting (ties keep exploiting by design);
        #  - saturation_dry_bursts=99 => the hard-stall forced-spawn backstop
        #    can't trigger inside the budget.
        cfg.max_decisions = int(os.environ.get("CSS_L0_BURSTS", "6"))
        cfg.spawn_supply_lambda = 0.0
        cfg.saturation_dry_bursts = 99
        print(f"L0-ONLY variant: bursts={cfg.max_decisions} "
              f"(spawn disabled, saturation backstop parked)", flush=True)
    if os.environ.get("CSS_MODEL"):
        # Model-swap variant (2026-07-11: Qwen3.5-9B self-evolve probe).
        # Self-evolve = BOTH roles (target + optimizer) run the same model.
        cfg.target_model = os.environ["CSS_MODEL"]
        cfg.optimizer_model = os.environ["CSS_MODEL"]
        print(f"MODEL override: target=optimizer={cfg.target_model}", flush=True)
    if os.environ.get("CSS_BASE_URL"):
        cfg.extra["base_url"] = os.environ["CSS_BASE_URL"]
        print(f"BASE_URL override: {cfg.extra['base_url']}", flush=True)
    if os.environ.get("CSS_WORKERS"):
        cfg.max_api_workers = int(os.environ["CSS_WORKERS"])
        print(f"WORKERS override: {cfg.max_api_workers}", flush=True)

    cfg.validate()

    os.makedirs(out_root, exist_ok=True)
    cfg.to_json_file(os.path.join(out_root, "config.json"))

    log_fmt = "%(asctime)s %(levelname)-7s %(name)s — %(message)s"
    logging.basicConfig(level=logging.INFO, format=log_fmt, handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(out_root, "css.log")),
    ])
    log = logging.getLogger("css")
    log.info("CSS AppWorld run starting — out_root=%s", out_root)
    log.info("Config: env=%s target=%s optimizer=%s workers=%d turns=%d",
             cfg.env_name, cfg.target_model, cfg.optimizer_model,
             cfg.max_api_workers, cfg.max_turns)

    target_client, optimizer_client = build_clients(cfg)
    env = build_env(cfg)

    log.info("Train=%d  Val=%d  Test=%d",
             len(env.train_items()), len(env.val_items()), len(env.test_items()))

    result = run_css(
        env, target_client, optimizer_client,
        cfg=cfg, out_dir=out_root, max_rounds=20, resume=resume,
    )

    log.info("Terminated: %s", result.terminated_reason)
    log.info("Best node: %s", result.best_node_id)
    log.info("Rounds: %d", len(result.rounds))
    best = result.tree.get(result.best_node_id) if result.best_node_id else None
    if best:
        log.info("Best val_score: %.4f", best.val_score)
        log.info("Best strategy:\n%s", best.strategy[:500])

    print(f"\n{'='*60}")
    print(f"Terminated: {result.terminated_reason}")
    print(f"Best node:  {result.best_node_id}")
    print(f"Rounds:     {len(result.rounds)}")
    if best:
        print(f"Val score:  {best.val_score:.4f}")
    print(f"Output:     {out_root}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
