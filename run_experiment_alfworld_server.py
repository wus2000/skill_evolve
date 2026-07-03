#!/usr/bin/env python3
"""CSS experiment on ALFWorld (text mode) — SERVER run.

Mirrors run_experiment_bird_server.py; only the env, its data paths, and the
env-specific knobs differ. Benchmark audit + deployment notes:
docs/env_prep/alfworld_PREP.md. Split design: data/alfworld_split_seed42
(train 3153 / val 400 carved+stratified / test = valid_unseen 134;
valid_seen 140 is the offline in-domain eval via run_test_eval_alfworld.py).

Server prerequisites (already provisioned 2026-07-03):
  * conda env:  ~/miniconda3/envs/alfworld  (python 3.10 + alfworld 0.5.x)
    — the WORKER interpreter; this launcher itself runs on system python.
  * data:       /home/wushang/workspace/data/alfworld_data  ($ALFWORLD_DATA
    layout: json_2.1.1/{train,valid_seen,valid_unseen,valid_train}, logic/)
"""
from __future__ import annotations  # server runs Python 3.8: keep `X | None` lazy

import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))

from css.config import CSSConfig
from css.model.client import build_clients
from css.envs.registry import build_env
from css.orchestrator import run_css


DATA_BASE = "/home/wushang/workspace/data"


def _latest_run_dir() -> str | None:
    """Most recent runs/alfworld_* directory (for --resume with no path)."""
    import glob
    runs = sorted(glob.glob("runs/alfworld_*"))
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
    out_root = resume_dir if resume else f"runs/alfworld_{timestamp}"

    cfg = CSSConfig(
        # ── Environment ──────────────────────────────────────────────────
        env_name="alfworld",

        # Split: official train carved into 3153 train / 400 val (stratified
        # by task_type, seed 42); test = the community-standard 134
        # valid_unseen games. valid_seen (140) is evaluated offline only.
        n_train=3153,
        n_val=400,
        n_test=134,
        split_dir=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "data", "alfworld_split_seed42"),
        data_root=f"{DATA_BASE}/alfworld_data",

        # LLM (remote OpenAI-compatible endpoint, reachable from this server)
        target_model="qwen3.6-35b-a3b",
        optimizer_model="qwen3.6-35b-a3b",

        # Runtime. Each rollout runs its episode in a dedicated worker
        # subprocess (~100-150 MB RSS) — 256 concurrent ≈ 40 GB RAM.
        max_api_workers=256,
        concurrency_limit=1,
        task_timeout_s=1200,   # 50 steps x worst-case LLM latency under load
        max_turns=50,          # mirrors alfworld_max_steps (generic field)
        k_rollouts=3,

        # L0 exploitation
        batch_size=128,        # train tasks per L0 step
        minibatch_size=16,     # trajectories per reflect minibatch
        reflect_mode="plan_a",
        merger_granularity="point",
        # Budget-bounded L0 (V3.4) — same policy as the Bird arms.
        min_l0_epochs=0,       # no full-epoch floor before saturation checks
        max_l0_steps=20,       # hard per-round L0 step budget
        l0_stall_steps=8,      # saturate after 8 steps without a new best

        # L0 val gate: item-paired two-stage gate on the full 400-item val
        # carve. K=1 screen (default) — 400 paired items beat 200 x K3 for
        # the same rollout budget.
        gate_mode="paired",
        gate_screen_k=1,
        gate_escalation_k=3,

        # Dataset-size subsets (0 = use all). Episodes cost ~15-40 LLM calls,
        # ~3x a Bird task — subset sizes are scaled down accordingly.
        coldstart_train_size=400,    # cold-start bare rollout
        exploitation_val_size=0,     # full val carve for the gate (400)
        analysis_train_size=600,     # difficulty-weighted analysis rollout

        # L1 strategy cycle (v3)
        l1_diagnostic_tasks=64,
        l1_regression_tasks=32,

        out_root=out_root,

        extra={
            "llm_backend": "openai_compat",
            "base_url": "http://10.77.110.162:8888/v1",
            "api_key": "token-abc123",
            "max_tokens": 16384,
            "temperature": 0.7,
            "enable_thinking": False,
            "timeout_seconds": 1800,
            "optimizer_json_mode": True,
            # ALFWorld env knobs (css/envs/alfworld/task_interface.py).
            "alfworld_python": "/home/wushang/miniconda3/envs/alfworld/bin/python",
            "alfworld_max_steps": 50,
            "alfworld_temperature": 0.4,
            "alfworld_max_tokens": 16384,
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
    log.info("CSS ALFWorld run starting — out_root=%s", out_root)
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
