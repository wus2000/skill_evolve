#!/usr/bin/env python3
"""Launcher template — copy to ``run_experiment_<env>_server.py`` and fill in.

Every launcher follows the same shape:
  1. build a :class:`CSSConfig` (mechanism knobs + env selection + env-private
     knobs under ``extra``),
  2. build the clients and the env (via the registry),
  3. call :func:`css.orchestrator.run_css` with ``resume`` support.

TODO(env) markers show what a new benchmark must customize. Keep each env's
launcher self-contained — per-env defaults (batch sizes, timeouts, gate screen
K) belong HERE, not in ``CSSConfig`` defaults.
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


# TODO(env): server-side data locations.
DATA_BASE = "/home/wushang/workspace/data"
ENV_NAME = "template"          # must match a css/envs/registry.py alias
RUN_PREFIX = "template"        # runs/<RUN_PREFIX>_<timestamp>


def _latest_run_dir() -> str | None:
    import glob
    runs = sorted(glob.glob(f"runs/{RUN_PREFIX}_*"))
    return runs[-1] if runs else None


def main() -> None:
    # `--resume [dir]` reuses an existing run dir and continues from its
    # latest stage checkpoint (defaults to the newest run dir).
    resume = False
    resume_dir = None
    if len(sys.argv) > 1 and sys.argv[1] == "--resume":
        resume = True
        resume_dir = sys.argv[2] if len(sys.argv) > 2 else _latest_run_dir()
        if not resume_dir:
            print("--resume: no existing run dir found")
            sys.exit(1)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_root = resume_dir if resume else f"runs/{RUN_PREFIX}_{timestamp}"

    cfg = CSSConfig(
        # ── Environment selection ─────────────────────────────────────────
        env_name=ENV_NAME,

        # ── Data split ────────────────────────────────────────────────────
        # TODO(env): real split sizes. 0 = use the whole split file.
        # Convention: split_dir contains {train,val,test}/items.json, each a
        # pre-shuffled JSON list of item dicts with unique "id" keys.
        n_train=0,
        n_val=0,
        n_test=0,
        split_dir=f"{DATA_BASE}/<your_split_dir>",     # TODO(env)
        data_root=f"{DATA_BASE}/<your_data_root>",     # TODO(env)

        # ── LLM ───────────────────────────────────────────────────────────
        target_model="qwen3.6-35b-a3b",
        optimizer_model="qwen3.6-35b-a3b",

        # ── Runtime ───────────────────────────────────────────────────────
        # TODO(env): size to your task's latency profile.
        max_api_workers=320,
        concurrency_limit=1,
        task_timeout_s=1800,   # per-rollout wall clock
        max_turns=30,          # generic turn cap (mirror your env knob below)
        k_rollouts=3,

        # ── L0 exploitation ───────────────────────────────────────────────
        # TODO(env): batch_size relative to n_train decides steps-per-epoch
        # (ceil(n_train / batch_size)); minibatch_size is the reflect unit.
        batch_size=40,
        minibatch_size=8,
        reflect_mode="plan_a",
        merger_granularity="point",   # ablation knob: "point" | "section"

        # ── L0 val gate (two-stage item-paired sign test) ─────────────────
        # TODO(env): screen K by val-set size — large val (≈1000+) works at
        # K=1; small val (≈100 or less) needs K=3 to surface enough
        # discordant items for the sign test.
        gate_mode="paired",
        gate_screen_k=1,
        gate_escalation_k=3,

        # ── Dataset-size subsets (0 = full) ───────────────────────────────
        # TODO(env): keep expensive stages tractable on large splits.
        coldstart_train_size=0,
        exploitation_val_size=0,     # 0 = full val set for the gate
        analysis_train_size=0,

        # ── L1 strategy cycle ─────────────────────────────────────────────
        l1_diagnostic_tasks=24,
        l1_regression_tasks=12,

        out_root=out_root,

        extra={
            # OpenAI-compatible LLM backend
            "llm_backend": "openai_compat",
            "base_url": "http://10.77.110.162:8888/v1",
            "api_key": "token-abc123",
            "max_tokens": 16384,
            "temperature": 0.7,
            "enable_thinking": False,
            "timeout_seconds": 1800,
            "optimizer_json_mode": True,
            # ── Env-private knobs: ALWAYS namespaced "<env>_*" ────────────
            # TODO(env): your agent's knobs (turn cap, exec timeout, agent
            # max_tokens/temperature, extra data roots ...). Read them in
            # your task_interface/agent via cfg.extra.
            "template_max_turns": 1,
            "template_max_tokens": 4096,
            "template_temperature": 0.0,
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
    log.info("CSS %s run starting — out_root=%s", ENV_NAME, out_root)

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
    best = result.tree.get(result.best_node_id) if result.best_node_id else None
    if best:
        log.info("Best val_score: %.4f", best.val_score)

    print(f"\n{'=' * 60}")
    print(f"Terminated: {result.terminated_reason}")
    print(f"Best node:  {result.best_node_id}")
    print(f"Rounds:     {len(result.rounds)}")
    if best:
        print(f"Val score:  {best.val_score:.4f}")
    print(f"Output:     {out_root}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
