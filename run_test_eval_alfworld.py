#!/usr/bin/env python3
"""Standalone skill evaluation on an ALFWorld split — SERVER run.

ALFWorld counterpart of run_test_eval_bird.py: evaluates a frozen
(strategy.md, rules.md) pair on val / test_seen / test_unseen / train with K
rollouts, fully independent of any running experiment (own runs/eval_*
directory, own prediction cache). Also the tool for the bare / strategy-only
baselines (pass /dev/null as the corresponding file) and for the in-domain
vs out-of-domain generalization comparison (test_seen vs test_unseen).

Usage:
  python3 run_test_eval_alfworld.py \
      --strategy PATH --rules PATH \
      [--split test_unseen] [--k 1] [--workers 128] [--label NAME]
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


DATA_BASE = "/home/wushang/workspace/data"


def _read_text(path: str) -> str:
    if not path or path == "/dev/null":
        return ""
    with open(path, encoding="utf-8") as f:
        return f.read()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strategy", required=True, help="path to strategy.md ('' or /dev/null = empty)")
    ap.add_argument("--rules", required=True, help="path to rules.md ('' or /dev/null = empty)")
    ap.add_argument("--split", default="test_unseen",
                    choices=["val", "test_seen", "test_unseen", "train"])
    ap.add_argument("--k", type=int, default=1, help="rollouts per item")
    ap.add_argument("--workers", type=int, default=128)
    ap.add_argument("--label", default="skill", help="run label used in the output dir name")
    args = ap.parse_args()

    strategy = _read_text(args.strategy)
    rules = _read_text(args.rules)
    skill_text = SkillDocument(skill_dir="", strategy=strategy, rules=rules).combined_skill_text()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_root = "runs/eval_alfworld_%s_%s_%s" % (args.label, args.split, timestamp)

    cfg = CSSConfig(
        # ── Environment (identical to run_experiment_alfworld_server.py) ──
        env_name="alfworld",
        n_train=3153,
        n_val=400,
        n_test=134,
        split_dir=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "data", "alfworld_split_seed42"),
        data_root=f"{DATA_BASE}/alfworld_data",

        target_model="qwen3.6-35b-a3b",
        optimizer_model="qwen3.6-35b-a3b",

        max_api_workers=args.workers,
        concurrency_limit=1,
        task_timeout_s=1200,
        max_turns=50,
        k_rollouts=args.k,

        out_root=out_root,

        extra={
            "llm_backend": "openai_compat",
            "base_url": "http://10.77.110.162:8888/v1,http://10.77.110.162:8889/v1",
            "api_key": "token-abc123",
            "max_tokens": 16384,
            "temperature": 0.7,
            "enable_thinking": False,
            "timeout_seconds": 1800,
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
        logging.FileHandler(os.path.join(out_root, "eval.log")),
    ])
    log = logging.getLogger("css.eval")

    init_trace(out_root)

    target_client, _ = build_clients(cfg)
    target_client = TracingLLMClient(target_client, role="target")
    env = build_env(cfg)

    items = {
        "val": env.val_items,
        "test_seen": env.test_seen_items,
        "test_unseen": env.test_items,
        "train": env.train_items,
    }[args.split]()
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
        "task_type": g.rollouts[0].task_type if g.rollouts else "",
        "pass_rate": g.pass_rate,
        "n_rollouts": len(g.rollouts),
        "n_pass": len(g.successes),
    } for g in groups]

    # Per-task-type breakdown (6 ALFWorld task families).
    by_type: dict = {}
    for t in per_task:
        agg = by_type.setdefault(t["task_type"], {"n": 0, "pass_rate_sum": 0.0})
        agg["n"] += 1
        agg["pass_rate_sum"] += t["pass_rate"]
    type_scores = {k: round(v["pass_rate_sum"] / v["n"], 4) for k, v in sorted(by_type.items())}

    summary = {
        "timestamp": timestamp,
        "label": args.label,
        "split": args.split,
        "n_items": len(items),
        "k_rollouts": args.k,
        "elapsed_s": round(elapsed, 1),
        "scores": scores,
        "type_scores": type_scores,
        "skill_provenance": {
            "strategy_path": os.path.abspath(args.strategy) if strategy else "(empty)",
            "rules_path": os.path.abspath(args.rules) if rules else "(empty)",
            "strategy_sha256": hashlib.sha256(strategy.encode("utf-8")).hexdigest()[:16],
            "rules_sha256": hashlib.sha256(rules.encode("utf-8")).hexdigest()[:16],
            "skill_chars": len(skill_text),
        },
        "per_task": per_task,
    }
    with open(os.path.join(out_root, "eval_results.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    task_hard = scores.get("task_hard", 0)
    log.info("Eval done — task_hard=%.4f n_results=%d in %.0fs",
             task_hard, len(results), elapsed)
    print("\n" + "=" * 60)
    print("ALFWORLD SKILL EVAL RESULTS (%s)" % args.label)
    print("  Split:         %s (%d items)" % (args.split, len(items)))
    print("  K rollouts:    %d" % args.k)
    print("  Task hard:     %.4f" % task_hard)
    print("  By type:       %s" % json.dumps(type_scores))
    print("  Elapsed:       %.0fs" % elapsed)
    print("  Output:        %s" % out_root)
    print("=" * 60)


if __name__ == "__main__":
    main()
