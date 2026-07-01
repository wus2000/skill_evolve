#!/usr/bin/env python3
"""CSS experiment on BIRD Text-to-SQL — SERVER run.

Mirrors run_experiment_server.py (the SpreadsheetBench launcher) but selects the
BIRD environment through the env registry (``cfg.env_name="bird"`` ->
``css.envs.registry.build_env``). The mechanism is identical; only the env, its
data paths, and a few Bird knobs differ.

Data layout on the server:
  * split:    {DATA_BASE}/bird_split/{train,val,test}/items.json
  * databases:{DATA_BASE}/bird_raw/database/<db_id>/<db_id>.sqlite
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
    """Most recent runs/bird_* directory (for --resume with no path)."""
    import glob
    runs = sorted(glob.glob("runs/bird_*"))
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
    out_root = resume_dir if resume else f"runs/bird_{timestamp}"

    cfg = CSSConfig(
        # ── Environment ──────────────────────────────────────────────────
        env_name="bird",

        # Split: filtered train (6601) shuffled seed=42 -> train/val 5:1
        # (5501/1100); test = BIRD dev (1534). FULL split loaded; the per-op
        # subset knobs below keep each expensive stage tractable.
        n_train=5501,
        n_val=1100,
        n_test=1534,
        # Split items (db_id/question/SQL) are version-controlled in the repo;
        # the SQLite databases live under the server's bird_raw (not in git).
        split_dir=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "data", "bird_split_filtered_seed42"),
        # train/val databases (BIRD train); test databases (BIRD dev) via extra.
        data_root=f"{DATA_BASE}/bird_raw/train/train_databases",

        # LLM (remote OpenAI-compatible endpoint, reachable from this server)
        target_model="qwen3.6-35b-a3b",
        optimizer_model="qwen3.6-35b-a3b",

        # Runtime
        max_api_workers=320,
        concurrency_limit=1,
        task_timeout_s=1800,   # up to 30 agent turns/task -> generous 30-min ceiling
        max_turns=30,          # mirrors bird_max_turns below (generic field, kept consistent)
        k_rollouts=3,

        # L0 exploitation
        batch_size=128,        # train tasks per L0 step
        minibatch_size=16,     # trajectories per reflect minibatch
        reflect_mode="plan_a",

        # Dataset-size subsets for the full split (0 = use all):
        coldstart_train_size=500,    # cold-start bare rollout
        exploitation_val_size=0,     # 0 = use FULL val set for gate (1100 items)
        analysis_train_size=1100,    # difficulty-weighted analysis rollout

        # L1 strategy cycle (v3)
        l1_diagnostic_tasks=64,
        l1_regression_tasks=32,

        out_root=out_root,

        extra={
            # OpenAI-compatible LLM backend (required for native function-calling)
            "llm_backend": "openai_compat",
            "base_url": "http://10.77.110.162:8888/v1",
            "api_key": "token-abc123",
            "max_tokens": 16384,
            "temperature": 0.7,
            "enable_thinking": False,
            "timeout_seconds": 1800,
            "optimizer_json_mode": True,
            # BIRD dev databases for the test split (train/val use data_root).
            "bird_test_db_root": f"{DATA_BASE}/bird_raw/dev/dev_databases",
            # Bird task-agent knobs
            "bird_max_turns": 30,
            "bird_exec_timeout": 15.0,
            "bird_max_tokens": 16384,
            "bird_temperature": 0.0,
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
    log.info("CSS BIRD run starting — out_root=%s", out_root)
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
