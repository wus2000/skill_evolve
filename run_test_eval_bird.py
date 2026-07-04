#!/usr/bin/env python3
"""Standalone skill evaluation on a BIRD split — SERVER run.

Evaluates a frozen (strategy.md, rules.md) pair on the test / val / train
split with K rollouts, fully independent of any running experiment: all
artifacts (rollouts, trace, summary) go to a fresh ``runs/eval_*`` directory,
so live runs and their prediction caches are untouched.

The env/config wiring mirrors run_experiment_bird_server.py exactly; only
runtime knobs (workers, K) come from the CLI.

Usage:
  python3 run_test_eval_bird.py \
      --strategy runs/<run>/skill_snapshots/<node>/round_0000/strategy.md \
      --rules    runs/<run>/<node>/round_0000/exploit/step3/candidate_rules.md \
      [--split test] [--k 1] [--workers 160] [--label point_r0s3]
"""
from __future__ import annotations  # server runs Python 3.8: keep `X | None` lazy

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))

from css.config import CSSConfig
from css.model.client import build_clients
from css.envs.registry import build_env
from css.rollout.batch import batch_rollout
from css.data.rollout import aggregate_scores, group_rollouts
from css.skill_document import SkillDocument
from css.tracing import init_trace, TracingLLMClient
from css.model.endpoints import resolve_base_url


DATA_BASE = "/home/wushang/workspace/data"


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strategy", required=True, help="path to strategy.md")
    ap.add_argument("--rules", required=True, help="path to rules.md")
    ap.add_argument("--split", default="test", choices=["test", "val", "train"])
    ap.add_argument("--k", type=int, default=1, help="rollouts per item")
    ap.add_argument("--workers", type=int, default=160)
    ap.add_argument("--label", default="skill", help="run label used in the output dir name")
    args = ap.parse_args()

    strategy = _read_text(args.strategy)
    rules = _read_text(args.rules)
    skill_text = SkillDocument(skill_dir="", strategy=strategy, rules=rules).combined_skill_text()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_root = "runs/eval_%s_%s_%s" % (args.label, args.split, timestamp)

    cfg = CSSConfig(
        # ── Environment (identical to run_experiment_bird_server.py) ──────
        env_name="bird",
        n_train=5501,
        n_val=1100,
        n_test=1534,
        split_dir=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "data", "bird_split_filtered_seed42"),
        data_root=f"{DATA_BASE}/bird_raw/train/train_databases",

        target_model="qwen3.6-35b-a3b",
        optimizer_model="qwen3.6-35b-a3b",

        # Runtime (CLI-tunable so a side eval can stay light on the endpoint)
        max_api_workers=args.workers,
        concurrency_limit=1,
        task_timeout_s=1800,
        max_turns=30,
        k_rollouts=args.k,

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
            "bird_test_db_root": f"{DATA_BASE}/bird_raw/dev/dev_databases",
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
        logging.FileHandler(os.path.join(out_root, "eval.log")),
    ])
    log = logging.getLogger("css.eval")

    init_trace(out_root)

    target_client, _ = build_clients(cfg)
    target_client = TracingLLMClient(target_client, role="target")
    env = build_env(cfg)

    items = {"test": env.test_items, "val": env.val_items, "train": env.train_items}[args.split]()
    log.info("Skill eval on %s split: %d items, k=%d, workers=%d",
             args.split, len(items), args.k, args.workers)
    log.info("strategy=%s (%d chars)  rules=%s (%d chars)",
             args.strategy, len(strategy), args.rules, len(rules))

    t0 = time.time()
    results = batch_rollout(
        env,
        items,
        skill_text=skill_text,
        target_client=target_client,
        k_rollouts=args.k,
        out_dir=os.path.join(out_root, "rollouts"),
        max_workers=args.workers,
        task_timeout=cfg.task_timeout_s,
        epoch=0,
        node_id="eval_%s" % args.label,
    )
    elapsed = time.time() - t0

    scores = aggregate_scores(results)
    groups = group_rollouts(results)
    per_task = [{
        "task_id": g.task_id,
        "pass_rate": g.pass_rate,
        "n_rollouts": len(g.rollouts),
        "n_pass": len(g.successes),
    } for g in groups]

    summary = {
        "timestamp": timestamp,
        "label": args.label,
        "split": args.split,
        "n_items": len(items),
        "k_rollouts": args.k,
        "elapsed_s": round(elapsed, 1),
        "scores": scores,
        "skill_provenance": {
            "strategy_path": os.path.abspath(args.strategy),
            "rules_path": os.path.abspath(args.rules),
            "strategy_sha256": hashlib.sha256(strategy.encode("utf-8")).hexdigest()[:16],
            "rules_sha256": hashlib.sha256(rules.encode("utf-8")).hexdigest()[:16],
            "strategy_chars": len(strategy),
            "rules_chars": len(rules),
            "skill_chars": len(skill_text),
        },
        "per_task": per_task,
    }
    with open(os.path.join(out_root, "eval_results.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    task_hard = scores.get("task_hard", 0)
    rollout_hard = scores.get("rollout_hard", 0)
    log.info("Eval done — task_hard=%.4f rollout_hard=%.4f n_results=%d in %.0fs",
             task_hard, rollout_hard, len(results), elapsed)
    print("\n" + "=" * 60)
    print("SKILL EVAL RESULTS (%s)" % args.label)
    print("  Split:         %s (%d items)" % (args.split, len(items)))
    print("  K rollouts:    %d" % args.k)
    print("  Task hard:     %.4f" % task_hard)
    print("  Rollout hard:  %.4f" % rollout_hard)
    print("  Elapsed:       %.0fs" % elapsed)
    print("  Output:        %s" % out_root)
    print("=" * 60)


if __name__ == "__main__":
    main()
