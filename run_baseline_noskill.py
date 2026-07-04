#!/usr/bin/env python3
"""No-skill baseline on test set (200 tasks x k=3).

Runs the same agent with an EMPTY skill document on the held-out test set
to establish a true baseline for comparison with the CSS-optimized result.
"""
import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))

from css.config import CSSConfig
from css.model.client import build_clients
from css.envs.spreadsheetbench.task_interface import SpreadsheetBenchEnv
from css.rollout.batch import grouped_batch_rollout
from css.data.rollout import aggregate_scores
from css.model.endpoints import resolve_base_url

DATA_BASE = "/home/wushang/workspace/data"


def main() -> None:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = f"runs/baseline_noskill_{timestamp}"

    cfg = CSSConfig(
        n_train=140,
        n_val=60,
        n_test=200,
        split_dir=f"{DATA_BASE}/spreadsheetbench_split_shuffled_seed42",
        data_root=f"{DATA_BASE}/spreadsheet_raw/spreadsheetbench_verified_400",
        target_model="qwen3.6-35b-a3b",
        optimizer_model="qwen3.6-35b-a3b",
        max_api_workers=128,
        task_timeout_s=3600,
        max_turns=100,
        reflect_mode="plan_a",
        out_root=out_dir,
        extra={
            "llm_backend": "openai_compat",
            "base_url": resolve_base_url("http://10.77.110.162:8888/v1"),
            "api_key": "token-abc123",
            "max_tokens": 16384,
            "temperature": 0.7,
            "enable_thinking": False,
            "timeout_seconds": 1800,
            "optimizer_json_mode": True,
        },
    )
    cfg.validate()
    os.makedirs(out_dir, exist_ok=True)

    log_fmt = "%(asctime)s %(levelname)-7s %(name)s — %(message)s"
    logging.basicConfig(level=logging.INFO, format=log_fmt, handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(out_dir, "baseline.log")),
    ])
    log = logging.getLogger("baseline")

    target_client, _ = build_clients(cfg)
    env = SpreadsheetBenchEnv(cfg, split_dir=cfg.split_dir, data_root=cfg.data_root)

    test_items = list(env.test_items())
    log.info("No-skill baseline — test=%d items, k=%d, workers=%d",
             len(test_items), cfg.k_rollouts, cfg.max_api_workers)

    # Empty skill text = no strategy document
    test_groups = grouped_batch_rollout(
        env,
        test_items,
        "",  # NO SKILL
        target_client,
        k_rollouts=cfg.k_rollouts,
        out_dir=os.path.join(out_dir, "test"),
        max_workers=cfg.max_api_workers,
        task_timeout=cfg.task_timeout_s,
        epoch=0,
        node_id="baseline",
    )
    test_flat = [r for g in test_groups for r in g.rollouts]
    test_score = float(aggregate_scores(test_flat).get("task_hard", 0.0))

    n_pass = sum(1 for r in test_flat if r.passed)
    log.info("No-skill baseline done — test_score=%.3f pass=%d/%d",
             test_score, n_pass, len(test_flat))
    print(f"\n{'='*60}")
    print(f"No-skill baseline test score: {test_score:.4f}")
    print(f"Pass: {n_pass}/{len(test_flat)}")
    print(f"Output: {out_dir}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
