#!/usr/bin/env python3
"""Standalone skill evaluation on an AppWorld split — SERVER run.

AppWorld counterpart of run_test_eval_alfworld.py: evaluates a frozen
(strategy.md, rules.md) pair on dev / test_normal / test_challenge / train
with K rollouts, fully independent of any running experiment (own runs/eval_*
directory, own prediction cache). Also the tool for the bare / strategy-only
baselines (pass /dev/null as the corresponding file).

Reporting (agreed protocol 2026-07-04):
  * TGC == task_hard at K=1 (single attempt; the official metric).
  * SGC is computed here in the summary layer: task ids are
    ``<scenario>_<variant>``; a scenario counts only when ALL its variants in
    the split pass. Whole-split runs only — task-level sampling would break
    scenario grouping.
  * Difficulty breakdown comes from ``task_type`` (``difficulty_<n>``,
    optimizer-only metadata; official guidance allows post-hoc analysis).
  * test_challenge is the SEALED split — unseal deliberately (bare baseline
    once + final best skill once).

Usage:
  python3 run_test_eval_appworld.py \
      --strategy PATH --rules PATH \
      [--split test_normal] [--k 1] [--workers 64] [--label NAME]
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
from css.data.rollout import aggregate_scores, group_rollouts
from css.envs.common.subprocess_worker import ensure_nofile_limit
from css.envs.registry import build_env
from css.model.client import build_clients
from css.rollout.batch import batch_rollout
from css.skill_document import SkillDocument
from css.tracing import TracingLLMClient, init_trace
from css.model.endpoints import resolve_base_url


DATA_BASE = "/home/wushang/workspace/data"


def _read_text(path: str) -> str:
    if not path or path == "/dev/null":
        return ""
    with open(path, encoding="utf-8") as f:
        return f.read()


def _scenario_id(task_id: str) -> str:
    return task_id.rsplit("_", 1)[0] if "_" in task_id else task_id


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strategy", required=True, help="path to strategy.md ('' or /dev/null = empty)")
    ap.add_argument("--rules", required=True, help="path to rules.md ('' or /dev/null = empty)")
    ap.add_argument("--split", default="test_normal",
                    choices=["dev", "test_normal", "test_challenge", "train"])
    ap.add_argument("--k", type=int, default=1, help="rollouts per item (official protocol: 1)")
    ap.add_argument("--workers", type=int, default=64)
    ap.add_argument("--label", default="skill", help="run label used in the output dir name")
    args = ap.parse_args()

    strategy = _read_text(args.strategy)
    rules = _read_text(args.rules)
    skill_text = SkillDocument(skill_dir="", strategy=strategy, rules=rules).combined_skill_text()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_root = "runs/eval_appworld_%s_%s_%s" % (args.label, args.split, timestamp)

    ensure_nofile_limit(8192)

    cfg = CSSConfig(
        # ── Environment (identical to run_experiment_appworld_server.py) ──
        env_name="appworld",
        n_train=90,
        n_val=57,
        n_test=168,
        data_root=f"{DATA_BASE}/appworld_root",

        target_model="qwen3.6-35b-a3b",
        optimizer_model="qwen3.6-35b-a3b",

        max_api_workers=args.workers,
        concurrency_limit=1,
        task_timeout_s=1800,
        max_turns=50,
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
            # Env knobs: same agreed defaults as the experiment launcher.
            "appworld_python": "/home/wushang/miniconda3/envs/appworld/bin/python",
            "appworld_root": f"{DATA_BASE}/appworld_root",
            "appworld_max_interactions": 50,
            "appworld_temperature": 0.4,
            "appworld_max_tokens": 4096,
            "appworld_obs_max_chars": 6000,
            "appworld_engine_slots": 128,
            # Standalone evals never need gold in annotations.
            "appworld_gt_mode": "tests",
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
        "dev": env.val_items,
        "test_normal": env.test_items,
        "test_challenge": env.test_challenge_items,
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
        "scenario": _scenario_id(g.task_id),
        "task_type": g.rollouts[0].task_type if g.rollouts else "",
        "pass_rate": g.pass_rate,
        "n_rollouts": len(g.rollouts),
        "n_pass": len(g.successes),
    } for g in groups]

    # SGC: a scenario counts only when EVERY variant in this split passed
    # (pass == all K rollouts of the task passed, i.e. pass_rate == 1).
    scenarios: dict = {}
    for t in per_task:
        ok = t["pass_rate"] >= 1.0
        scenarios[t["scenario"]] = scenarios.get(t["scenario"], True) and ok
    sgc = round(sum(scenarios.values()) / len(scenarios), 4) if scenarios else 0.0

    # Per-difficulty breakdown (task_type == "difficulty_<n>").
    by_type: dict = {}
    for t in per_task:
        agg = by_type.setdefault(t["task_type"] or "unknown",
                                 {"n": 0, "pass_rate_sum": 0.0})
        agg["n"] += 1
        agg["pass_rate_sum"] += t["pass_rate"]
    type_scores = {
        k: {"tgc": round(v["pass_rate_sum"] / v["n"], 4), "n": v["n"]}
        for k, v in sorted(by_type.items())
    }

    summary = {
        "timestamp": timestamp,
        "label": args.label,
        "split": args.split,
        "n_items": len(items),
        "n_scenarios": len(scenarios),
        "k_rollouts": args.k,
        "elapsed_s": round(elapsed, 1),
        "scores": scores,
        "tgc": scores.get("task_hard", 0),
        "sgc": sgc,
        "difficulty_scores": type_scores,
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

    tgc = scores.get("task_hard", 0)
    log.info("Eval done — TGC=%.4f SGC=%.4f n_results=%d in %.0fs",
             tgc, sgc, len(results), elapsed)
    print("\n" + "=" * 60)
    print("APPWORLD SKILL EVAL RESULTS (%s)" % args.label)
    print("  Split:         %s (%d tasks / %d scenarios)"
          % (args.split, len(items), len(scenarios)))
    print("  K rollouts:    %d" % args.k)
    print("  TGC:           %.4f" % tgc)
    print("  SGC:           %.4f" % sgc)
    print("  By difficulty: %s" % json.dumps(type_scores))
    print("  Elapsed:       %.0fs" % elapsed)
    print("  Output:        %s" % out_root)
    print("=" * 60)


if __name__ == "__main__":
    main()
