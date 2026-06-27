#!/usr/bin/env python3
"""No-skill baseline on the test set — SERVER run.

Runs the ReAct agent with empty skill_content on the test split to produce
a comparable baseline against Trace2Skill's held-out [200:400] results.
"""
import json
import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))

from css.config import CSSConfig
from css.model.client import build_clients
from css.envs.spreadsheetbench.task_interface import SpreadsheetBenchEnv
from css.rollout.batch import batch_rollout
from css.data.rollout import aggregate_scores, group_rollouts
from css.tracing import init_trace, TracingLLMClient


DATA_BASE = "/home/wushang/workspace/data"


def main() -> None:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_root = f"runs/baseline_noskill_{timestamp}"

    cfg = CSSConfig(
        n_train=140,
        n_val=60,
        n_test=200,
        split_dir=f"{DATA_BASE}/spreadsheetbench_split_t2s_aligned",
        data_root=f"{DATA_BASE}/spreadsheet_raw/spreadsheetbench_verified_400",

        target_model="qwen3.6-35b-a3b",
        optimizer_model="qwen3.6-35b-a3b",

        max_api_workers=512,
        task_timeout_s=3600,
        max_turns=30,
        k_rollouts=1,

        out_root=out_root,

        extra={
            "llm_backend": "openai_compat",
            "base_url": "http://10.77.110.162:8888/v1",
            "api_key": "token-abc123",
            "max_tokens": 16384,
            "temperature": 0.7,
            "enable_thinking": False,
            "timeout_seconds": 1800,
        },
    )
    cfg.validate()

    os.makedirs(out_root, exist_ok=True)
    cfg.to_json_file(os.path.join(out_root, "config.json"))

    log_fmt = "%(asctime)s %(levelname)-7s %(name)s — %(message)s"
    logging.basicConfig(level=logging.INFO, format=log_fmt, handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(out_root, "baseline.log")),
    ])
    log = logging.getLogger("css.baseline")

    init_trace(out_root)

    target_client, _ = build_clients(cfg)
    target_client = TracingLLMClient(target_client, role="target")
    env = SpreadsheetBenchEnv(cfg, split_dir=cfg.split_dir, data_root=cfg.data_root)

    test_items = env.test_items()
    log.info("Baseline (no-skill) on test set: %d items, k=%d, workers=%d, max_turns=%d",
             len(test_items), cfg.k_rollouts, cfg.max_api_workers, cfg.max_turns)

    results = batch_rollout(
        env,
        test_items,
        skill_text="",
        target_client=target_client,
        k_rollouts=cfg.k_rollouts,
        out_dir=os.path.join(out_root, "test_rollouts"),
        max_workers=cfg.max_api_workers,
        task_timeout=cfg.task_timeout_s,
        epoch=0,
        node_id="baseline",
    )

    scores = aggregate_scores(results)
    groups = group_rollouts(results)

    per_task = []
    for g in groups:
        per_task.append({
            "task_id": g.task_id,
            "pass_rate": g.pass_rate,
            "n_rollouts": len(g.rollouts),
            "n_pass": len(g.successes),
            "task_type": g.rollouts[0].task_type if g.rollouts else "",
        })

    summary = {
        "timestamp": timestamp,
        "skill": "none (baseline)",
        "n_test_items": len(test_items),
        "k_rollouts": cfg.k_rollouts,
        "max_turns": cfg.max_turns,
        "scores": scores,
        "per_task": per_task,
    }
    with open(os.path.join(out_root, "baseline_results.json"), "w") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    task_hard = scores.get("task_hard", 0)
    rollout_hard = scores.get("rollout_hard", 0)
    log.info("Baseline done — task_hard=%.4f rollout_hard=%.4f n_results=%d",
             task_hard, rollout_hard, len(results))
    print(f"\n{'='*60}")
    print(f"BASELINE (no-skill) RESULTS")
    print(f"  Test items:    {len(test_items)}")
    print(f"  K rollouts:    {cfg.k_rollouts}")
    print(f"  Task hard:     {task_hard:.4f}")
    print(f"  Rollout hard:  {rollout_hard:.4f}")
    print(f"  Output:        {out_root}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
