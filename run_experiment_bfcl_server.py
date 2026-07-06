#!/usr/bin/env python3
"""CSS experiment on BFCL multi-turn (native function-calling agent) — SERVER run.

Mirrors run_experiment_appworld_server.py; only the env, its data paths, and the
env-specific knobs differ. Benchmark audit + integration design:
docs/env_prep/bfcl_PREP.md. Config values below were approved 2026-07-05
(per-env default-config convention: the launcher is the carrier; agreed values
must not change silently).

Split design (scenario-index split, domain-stratified — see the PREP doc §11 and
tools/make_bfcl_split.py): train 480 / val 160 / test 160 (every challenge type
in every split, zero scenario leakage); composite-200 is a SEALED probe exposed
only as the ~40 composite entries on the TEST indices (eval_splits second slot,
reporting-only — mirrors AppWorld's test_challenge discipline).

Execution: IN-PROCESS. The 8 backends are vendored (css/envs/bfcl/vendor,
Apache-2.0, pinned 6ea5797); each rollout builds its own fresh instances (no
upstream globals() registry — the 1/8 contamination probe). No worker pool, no
external data. Endpoint: 127.0.0.1:8888 is dedicated to BFCL (native tool-call
parsing ON); the 10.77.110.162 pair stays with the other three experiments.

Server prerequisites:
  * harness python >= 3.10 with ``mpmath`` importable (BFCL targets >=3.10; the
    vendored backends were made 3.8-import-safe but 3.10 is the tested harness);
  * the split manifests under data/bfcl_split_seed42/ (committed; regenerate with
    tools/make_bfcl_split.py against the pinned checkout if ever needed).
"""
from __future__ import annotations  # server runs Python 3.8: keep `X | None` lazy

import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))

from css.config import CSSConfig
from css.envs.registry import build_env
from css.model.client import build_clients
from css.orchestrator import run_css

REPO = os.path.dirname(os.path.abspath(__file__))


def _latest_run_dir() -> str | None:
    """Most recent runs/bfcl_* directory (for --resume with no path)."""
    import glob
    runs = sorted(glob.glob("runs/bfcl_*"))
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
    out_root = resume_dir if resume else f"runs/bfcl_{timestamp}"

    cfg = CSSConfig(
        # ── Environment ──────────────────────────────────────────────────
        env_name="bfcl",

        # Whole manifest (0 = use all): train 480 / val 160 / test 160. The
        # split is pre-sized + pre-shuffled by tools/make_bfcl_split.py.
        n_train=0,
        n_val=0,
        n_test=0,
        split_dir=os.path.join(REPO, "data", "bfcl_split_seed42"),
        data_root="",  # BFCL is self-contained (vendored backends); no external data

        # LLM (dedicated server-local endpoint; see extra.base_url)
        target_model="qwen3.6-35b-a3b",
        optimizer_model="qwen3.6-35b-a3b",

        # Runtime. Rollouts are in-process; concurrency is LLM-bound (each entry
        # ~8-16 sequential model calls). The real per-episode cap is the env's
        # bfcl_max_steps_per_turn (20/turn, official); max_turns here is the
        # generic mirror (the env drives the scripted user turns itself).
        max_api_workers=128,
        concurrency_limit=1,
        task_timeout_s=600,    # ~4 turns x up to 20 steps x endpoint latency
        max_turns=20,
        k_rollouts=3,

        # L0 exploitation. batch = 96 (~1/5 of the 480 pool; entries are cheap;
        # agreed 2026-07-05, flagged TO-CONFIRM at first run once step throughput
        # is measured — may raise for fuller per-step coverage).
        batch_size=96,
        # Per-edit verification floor: ABSOLUTE 8 tasks (AppWorld precedent).
        verify_floor_tasks=8,
        minibatch_size=16,
        reflect_mode="plan_a",
        merger_granularity="point",
        edit_pipeline="v2",
        # Budget-bounded L0 (V3.4) — same policy as the AppWorld/ALFWorld arms.
        min_l0_epochs=0,
        max_l0_steps=20,
        l0_stall_steps=8,

        # L0 val gate: item-paired two-stage gate on the 160-item val carve.
        # K=1 screen + K=3 escalation (AppWorld precedent).
        gate_mode="paired",
        gate_screen_k=1,
        gate_escalation_k=3,

        # ── L1 TREE SEARCH (mechanism default since 2026-07-06; design:
        #    L1_tree_mechanism_design.md — run_css delegates to the
        #    burst-granular tree loop; legacy L0-budget/L1-cycle knobs are
        #    fingerprint-inert under burst mode) ─────────────────────────
        burst_steps=5,              # one tree visit = 5 L0 steps (agreed)
        saturation_dry_bursts=2,    # saturated when no NEW BEST for 2 bursts' steps (user ruling 2026-07-06)
        node_degree=3,              # REFINE children per strategy node; root unlimited (user ruling)
        max_decisions=40,           # decision budget; NOT fingerprinted — resume may extend
        verify_mode="harm_veto",    # per-edit probe only vetoes measured net harm (fix 2026-07-05)

        # Dataset-size subsets (0 = use all); the 480 pool is sampled in batches.
        coldstart_train_size=0,
        exploitation_val_size=0,
        analysis_train_size=0,

        # L1 strategy cycle (v3) — sized to the 480 pool (64+32 <= pool).
        l1_diagnostic_tasks=64,
        l1_regression_tasks=32,

        # Analysis prompt-shape fixes (on, matching AppWorld/ScienceWorld).
        json_list_wrap=True,
        analysis_env_context=True,

        out_root=out_root,

        extra={
            "llm_backend": "openai_compat",
            # DEDICATED endpoint — a PLAIN string, deliberately NOT
            # resolve_base_url(): the fleet registry (config/llm_endpoints.txt)
            # overrides any launcher default, which re-routed the first launch
            # onto the shared old pair (caught 2026-07-05). This arm is pinned
            # to the new server-local endpoint (native tool-call parsing ON),
            # shared only with the SpreadsheetBench arm.
            "base_url": "http://127.0.0.1:8888/v1",
            "api_key": "token-abc123",
            "max_tokens": 24576,  # client CEILING (clamp), not a request: raised 16384->24576 on 2026-07-05 — the merger requests 20480 and was being silently clamped (one measured truncation); callers still request less
            "temperature": 0.7,          # optimizer temperature
            "enable_thinking": False,
            "timeout_seconds": 1800,
            "optimizer_json_mode": True,
            # BFCL env knobs (css/envs/bfcl/task_interface.py).
            # 0.4 for K=3 rollout diversity on a deterministic env (AppWorld/
            # ScienceWorld precedent). The official leaderboard uses greedy 0.0;
            # 0.4 is the internal-baseline choice, TO-CONFIRM against fidelity.
            "bfcl_temperature": 0.4,
            # A tool-calling step (reasoning off) is short; 1024 bounds runaway
            # generations that would hold a vLLM slot.
            "bfcl_max_tokens": 1024,
            # Official per-turn step cap; exceeding fails the entry.
            "bfcl_max_steps_per_turn": 20,
            # Eval-annotation GT (ablation knob): "gold_calls" attaches the
            # per-turn gold call sequences; the ablation arm uses "none".
            "bfcl_gt_mode": "gold_calls",
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
    log.info("CSS BFCL run starting — out_root=%s", out_root)
    log.info("Config: env=%s target=%s optimizer=%s workers=%d",
             cfg.env_name, cfg.target_model, cfg.optimizer_model, cfg.max_api_workers)

    target_client, optimizer_client = build_clients(cfg)
    env = build_env(cfg)

    log.info("Train=%d  Val=%d  Test=%d  CompositeProbe=%d",
             len(env.train_items()), len(env.val_items()), len(env.test_items()),
             len(env.composite_probe_items()))

    result = run_css(
        env, target_client, optimizer_client,
        cfg=cfg, out_dir=out_root, max_rounds=20, resume=resume,
    )

    log.info("Terminated: %s", result.terminated_reason)
    log.info("Best node: %s", result.best_node_id)
    best = result.tree.get(result.best_node_id) if result.best_node_id else None
    if best:
        log.info("Best val_score: %.4f", best.val_score)

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
