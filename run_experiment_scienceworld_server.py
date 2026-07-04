#!/usr/bin/env python3
"""CSS experiment on ScienceWorld (elementary-science text agent) — SERVER run.

Mirrors run_experiment_appworld_server.py; only the env, its data paths, and the
env-specific knobs differ. Benchmark audit: docs/env_prep/scienceworld_PREP.md.
Integration design + config rationale: docs/env_prep/scienceworld_ONBOARDING.md.
Config values below were resolved one-by-one on 2026-07-04 (per-env default-config
convention: the launcher is the carrier; agreed values must not change silently).

Dual-protocol design (approved 2026-07-04):
  * TRAIN / VAL / SECONDARY test optimize + report on ScienceWorld's native 0-100
    score; PRIMARY test = the 90-instance AgentBoard subset scored by subgoal
    Success Rate (the mechanism test_score, headline) + Progress Rate.
  * All splits draw from the SAME 15 AgentBoard task types (the subset's actual
    coverage, measured after var recovery), under the SAME unified AgentBoard
    simplification set (NO `easy` preset anywhere — training with teleport while
    the primary test lacks it would teach skills that break).

Execution substrate: ScienceWorld runs IN-PROCESS in this harness (each env is
its own JVM via py4j; the wheel is importable under the harness Python). There is
NO separate worker interpreter (contrast AppWorld/ALFWorld) and NO
`scienceworld_python` knob. Prerequisites on the harness host:
  * `pip install scienceworld==1.2.3` INTO THE HARNESS ENV (offline wheel or
    Tsinghua mirror — see env_candidates/scienceworld_deploy/SERVER_DEPLOY.md);
  * `java` (>= 8; server Java 17 works) on PATH for this process.
Split manifests + gold_actions.json: data/scienceworld_split_seed42/ (generated
seed-deterministically by tools/make_scienceworld_split.py).
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
    """Most recent runs/scienceworld_* directory (for --resume with no path)."""
    import glob
    runs = sorted(glob.glob("runs/scienceworld_*"))
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
    out_root = resume_dir if resume else f"runs/scienceworld_{timestamp}"

    # 128 in-process JVMs each hold py4j sockets; raise the fd soft limit.
    ensure_nofile_limit(8192)

    cfg = CSSConfig(
        # ── Environment ──────────────────────────────────────────────────
        env_name="scienceworld",

        # Splits: manifests are pre-sized by tools/make_scienceworld_split.py
        # (stratified over the 15 AgentBoard task types, seed 42). 0 = use the
        # whole manifest. Generated counts: train 198 (<=15/task) / val 72
        # (<=5/task, dev fold) / primary test = 90 AgentBoard / secondary test
        # 116 (<=8/task, test fold). test_items() = the AgentBoard subset (its SR
        # is the mechanism test_score); the native secondary is reported
        # alongside via eval_splits().
        n_train=0,
        n_val=0,
        n_test=0,
        split_dir=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "data", "scienceworld_split_seed42"),
        data_root=f"{DATA_BASE}/scienceworld",  # goldpaths zip / AB jsonl staging (env reads split_dir)

        # LLM (remote OpenAI-compatible endpoint, reachable from this server)
        target_model="qwen3.6-35b-a3b",
        optimizer_model="qwen3.6-35b-a3b",

        # Runtime. Each rollout borrows an in-process JVM env from the pool
        # (~265 MB RSS each, measured); 128 concurrent ~= 34 GB RAM. Each JVM is
        # single-core-busy only during step(); concurrency is LLM-bound.
        max_api_workers=128,
        concurrency_limit=1,
        task_timeout_s=900,    # <=50 steps x worst-case LLM latency; env step is ms.
                               # Raised 600->900 on 2026-07-04 (launch decision):
                               # three experiments share the two vLLM endpoints, so
                               # per-call tail latency inflates; a timeout wastes the
                               # whole rollout AND injects a false failure. Revisit
                               # down when endpoint contention clears.
        max_turns=50,          # generic mirror of the native step budget
        k_rollouts=3,

        # L0 exploitation. batch = 45 (mirrors AppWorld's agreed value; ~1/4 of
        # the 198-task pool per step — TO CONFIRM at first run: may raise for
        # fuller per-step coverage once step throughput is measured).
        batch_size=45,
        # Per-edit verification floor: ABSOLUTE 8 tasks (AppWorld precedent).
        verify_floor_tasks=8,
        minibatch_size=16,
        reflect_mode="plan_a",
        merger_granularity="point",
        edit_pipeline="v2",
        # Budget-bounded L0 (V3.4) — same policy as the AppWorld/ALFWorld arms.
        min_l0_epochs=0,
        max_l0_steps=20,
        l0_stall_steps=8,

        # L0 val gate: item-paired two-stage gate on the ~90-item val carve.
        # K=1 screen + K=3 escalation (AppWorld precedent — escalation buys back
        # power on a small val).
        gate_mode="paired",
        gate_screen_k=1,
        gate_escalation_k=3,

        # Dataset-size subsets (0 = use all). The ~275-task pool is sampled in
        # batches; keep the whole pool available for difficulty-weighted sampling.
        coldstart_train_size=0,
        exploitation_val_size=0,
        analysis_train_size=0,

        # L1 strategy cycle (v3) — sized to the ~275 pool (64+32 <= pool; ALFWorld
        # values, which the pool comfortably fits).
        l1_diagnostic_tasks=64,
        l1_regression_tasks=32,

        # Analysis prompt-shape fixes (on, matching AppWorld).
        json_list_wrap=True,
        analysis_env_context=True,

        out_root=out_root,

        extra={
            "llm_backend": "openai_compat",
            "base_url": resolve_base_url(
                "http://10.77.110.162:8888/v1,http://10.77.110.162:8889/v1"),
            "api_key": "token-abc123",
            "max_tokens": 16384,
            "temperature": 0.7,
            "enable_thinking": False,
            "timeout_seconds": 1800,
            "optimizer_json_mode": True,
            # ScienceWorld env knobs (css/envs/scienceworld/task_interface.py).
            # Step budgets: 30 for the AgentBoard PRIMARY test (fidelity-locked to
            # AgentBoard's max_num_steps==envStepLimit==30); 50 for the native
            # train/val/secondary track (our ALFWorld/AppWorld convention; covers
            # most native gold paths in this task universe).
            "scienceworld_step_limit_original": 50,
            "scienceworld_step_limit_agentboard": 30,
            # UNIFIED simplification across ALL splits (amendment 2026-07-04):
            # exactly AgentBoard's set (openContainers + openDoors + selfWatering
            # + noElectrical; NO teleport, NO `easy` preset). Rationale: training
            # with teleport while the primary test lacks it teaches skills that
            # break cross-domain. NOTE: the native secondary track is therefore
            # NOT strictly comparable to SwiftSage's `easy`-preset numbers — it is
            # our own unified-simplification native baseline (a protocol
            # difference to flag when citing).
            "scienceworld_simplification":
                "selfWateringFlowerPots,openContainers,openDoors,noElectricalAction",
            # 0.4 everywhere (train AND eval): K=3 needs sampling diversity on a
            # deterministic env; one temperature keeps baselines comparable.
            "scienceworld_temperature": 0.4,
            # One <reasoning>+<action> per turn is short; 512 bounds runaway
            # generations that would zombie-hold a vLLM slot (optimizer stays 16384).
            "scienceworld_max_tokens": 512,
            # In-process JVM pool: cap live JVMs at the rollout concurrency
            # (RAM-bound, ~265 MB each); recycle every 200 episodes to bound RSS
            # creep; -Xmx256m caps heap tail growth (non-heap ~130 MB floor stays).
            "scienceworld_pool_size": 128,
            "scienceworld_pool_recycle_episodes": 200,
            "scienceworld_java_tool_options": "-Xmx256m",
            # Eval-annotation GT (ablation knob): "live" attaches the gold action
            # sequence (offline gold_actions.json accelerator + live-gen fallback
            # for measure-melting-unknown); the ablation arm uses "checklist"
            # (path-free native goal-progress structure).
            "scienceworld_gt_mode": "live",
        },
    )
    cfg.validate()

    os.makedirs(out_root, exist_ok=True)
    cfg.to_json_file(os.path.join(out_root, "config.json"))

    log_fmt = "%(asctime)s %(levelname)-7s %(name)s — %(message)s"
    logging.basicConfig(level=logging.INFO, format=log_fmt, handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(out_root, "css.log")),
    ])
    log = logging.getLogger("css")
    log.info("CSS ScienceWorld run starting — out_root=%s", out_root)
    log.info("Config: env=%s target=%s optimizer=%s workers=%d turns=%d",
             cfg.env_name, cfg.target_model, cfg.optimizer_model,
             cfg.max_api_workers, cfg.max_turns)

    target_client, optimizer_client = build_clients(cfg)
    env = build_env(cfg)

    log.info("Train=%d  Val=%d  Test(AgentBoard)=%d  Test(secondary)=%d",
             len(env.train_items()), len(env.val_items()), len(env.test_items()),
             len(env.test_secondary_items()))

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
