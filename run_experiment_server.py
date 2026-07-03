#!/usr/bin/env python3
"""CSS experiment on SpreadsheetBench — SERVER run.

Differences from ``run_experiment.py`` (the Mac/local variant):
  * server-side data / model paths,
  * the SpreadsheetBench env is fully self-contained under
    ``css.envs.spreadsheetbench`` (no external framework dependency).

Note: embeddings have been replaced with LLM-based label grouping throughout
the pipeline. No torch / sentence-transformers / faiss dependency.
"""
from __future__ import annotations  # server runs Python 3.8: keep `X | None` annotations lazy

import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))

from css.config import CSSConfig
from css.model.client import build_clients
from css.envs.spreadsheetbench.task_interface import SpreadsheetBenchEnv
from css.orchestrator import run_css


DATA_BASE = "/home/wushang/workspace/data"


def _latest_run_dir() -> str | None:
    """Most recent runs/spreadsheetbench_* directory (for --resume with no path)."""
    import glob
    runs = sorted(glob.glob("runs/spreadsheetbench_*"))
    return runs[-1] if runs else None


def main() -> None:
    # Resume support: `python run_experiment_server.py --resume [out_dir]`
    # reuses an existing run dir and continues from its latest stage checkpoint.
    resume = False
    resume_dir = None
    if len(sys.argv) > 1 and sys.argv[1] == "--resume":
        resume = True
        resume_dir = sys.argv[2] if len(sys.argv) > 2 else _latest_run_dir()
        if not resume_dir:
            print("--resume: no existing run dir found")
            sys.exit(1)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_root = resume_dir if resume else f"runs/spreadsheetbench_{timestamp}"

    cfg = CSSConfig(
        # Data: SHUFFLED split of verified_400 (seed=42), ratio 140:60:200.
        # The whole 400 is shuffled BEFORE splitting so each of train/val/test
        # holds a representative mix of Cell-Level + Sheet-Level tasks. (The earlier
        # t2s_aligned split was SEQUENTIAL — [200:400] held-out — which put 100% of
        # the Cell-Level tasks in test; shuffling fixes that but no longer matches
        # Trace2Skill's exact task split.)
        n_train=140,
        n_val=60,
        n_test=200,
        split_dir=f"{DATA_BASE}/spreadsheetbench_split_shuffled_seed42",
        data_root=f"{DATA_BASE}/spreadsheet_raw/spreadsheetbench_verified_400",

        # LLM (remote OpenAI-compatible endpoint, reachable from this server)
        target_model="qwen3.6-35b-a3b",
        optimizer_model="qwen3.6-35b-a3b",

        # Runtime
        max_api_workers=320,
        concurrency_limit=1,   # ONE tree node per round — new PROPOSAL nodes get inf UCB
                               # (n_steps=0) so they are always selected first for exploitation.
        task_timeout_s=3600,   # 60 min per-rollout wall-clock (multi-turn headroom over the 30min LLM req timeout)
        bash_timeout_s=180,    # per single bash command (3 min); kills hung commands fast (+ their whole tree)
        max_turns=50,          # 99.84% of rollouts finish <=50 turns (median 5); halves the long-tail budget

        # Reflect pipeline (three-way analysis -> unified edit generator)
        reflect_mode="plan_a",
        merger_granularity="point",   # ablation knob: "point" | "section"

        # L0 val gate: two-stage item-paired sign test.
        # The val set is small (60) — a K=1 screen misses too many real flips
        # (a 0.3->0.7 improvement flips a K=1 screen only ~58% of the time),
        # so screen at K=3 to surface enough discordant items for the test.
        gate_mode="paired",
        gate_screen_k=3,
        gate_escalation_k=3,

        # L1 strategy cycle (v3): diverse-iterate + objective lift/deploy_net.
        # Test-set sizes for this run (residual = baseline-0/K, regression = baseline-K/K);
        # other L1 knobs (k_rollouts=3, l1_target_effective=3, max_l1_iterations=8,
        # l1_diagnosis_per_category=5) use the agreed-design dataclass defaults.
        l1_diagnostic_tasks=24,
        l1_regression_tasks=12,

        # Output
        out_root=out_root,

        # OpenAI-compatible LLM backend
        extra={
            "llm_backend": "openai_compat",
            "base_url": "http://10.77.110.162:8888/v1,http://10.77.110.162:8889/v1",
            "api_key": "token-abc123",
            "max_tokens": 16384,
            "temperature": 0.7,
            "enable_thinking": False,
            "timeout_seconds": 1800,   # 30 min per-HTTP-request timeout (avoid 300s timeout->retry waste under 512-way saturation)
            "optimizer_json_mode": True,
        },
    )
    cfg.validate()

    os.makedirs(out_root, exist_ok=True)
    cfg.to_json_file(os.path.join(out_root, "config.json"))

    # Logging
    log_fmt = "%(asctime)s %(levelname)-7s %(name)s — %(message)s"
    logging.basicConfig(level=logging.INFO, format=log_fmt, handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(out_root, "css.log")),
    ])
    log = logging.getLogger("css")
    log.info("CSS run starting — out_root=%s", out_root)
    log.info("Config: target=%s optimizer=%s workers=%d turns=%d reflect_mode=%s",
             cfg.target_model, cfg.optimizer_model, cfg.max_api_workers,
             cfg.max_turns, cfg.reflect_mode)

    # Build components
    target_client, optimizer_client = build_clients(cfg)
    env = SpreadsheetBenchEnv(cfg, split_dir=cfg.split_dir, data_root=cfg.data_root)

    log.info("Train=%d  Val=%d  Test=%d",
             len(env.train_items()), len(env.val_items()), len(env.test_items()))

    # Run CSS
    result = run_css(
        env, target_client, optimizer_client,
        cfg=cfg, out_dir=out_root, max_rounds=20, resume=resume,
    )

    # Summary
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
