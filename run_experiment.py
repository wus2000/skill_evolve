#!/usr/bin/env python3
"""CSS experiment on SpreadsheetBench — full validation run."""
import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))

from css.config import CSSConfig
from css.model.client import build_clients
from css.envs.spreadsheetbench.task_interface import SpreadsheetBenchEnv
from css.orchestrator import run_css


DATA_BASE = (
    "/Users/shangwu/workspace/vibe_coding_projects/work/reproduction_work"
    "/SkillOpt/reproduction/data"
)


def main() -> None:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_root = f"runs/spreadsheetbench_{timestamp}"

    cfg = CSSConfig(
        # Data (matches SkillOpt split: 80/40/280)
        n_train=80,
        n_val=40,
        n_test=280,
        split_dir=f"{DATA_BASE}/spreadsheetbench_split",
        data_root=f"{DATA_BASE}/spreadsheet_raw/spreadsheetbench_verified_400",

        # LLM
        target_model="qwen3.6-35b-a3b",
        optimizer_model="qwen3.6-35b-a3b",

        # Runtime
        max_api_workers=256,
        task_timeout_s=600,
        max_turns=30,

        # Output
        out_root=out_root,

        # OpenAI-compatible LLM backend
        extra={
            "llm_backend": "openai_compat",
            "base_url": "http://10.77.110.162:8888/v1",
            "api_key": "token-abc123",
            "max_tokens": 16384,
            "temperature": 0.7,
            "enable_thinking": False,
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
    log.info("Config: target=%s optimizer=%s workers=%d turns=%d",
             cfg.target_model, cfg.optimizer_model, cfg.max_api_workers, cfg.max_turns)

    # Build components
    target_client, optimizer_client = build_clients(cfg)
    env = SpreadsheetBenchEnv(cfg, split_dir=cfg.split_dir, data_root=cfg.data_root)

    log.info("Train=%d  Val=%d  Test=%d",
             len(env.train_items()), len(env.val_items()), len(env.test_items()))

    # Run CSS
    result = run_css(
        env, target_client, optimizer_client,
        cfg=cfg, out_dir=out_root, max_rounds=20,
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
