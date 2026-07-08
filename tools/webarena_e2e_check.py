#!/usr/bin/env python3
"""End-to-end WebArena sanity check: real WebArenaEnv.run_one on one task.

Exercises the entire production chain exactly as a rollout would — lease
acquisition, the authenticated browser episode (single-turn history + scribe),
the WebArena-Verified offline scorer, and result persistence. This is the gate
before any smoke run: it proves login state, base_url, /admin, the scorer, and
the new agent all work together on a real task.

    python3 tools/webarena_e2e_check.py <task_id> [--stack s1]

Prints hard/soft, the agent's answer, the reference (post-hoc, never seen by
the agent), and turn/scribe counts. Uses the bare skill (no rules) so this is
a pure infrastructure check, not a capability measurement.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from css.config import CSSConfig
from css.envs.webarena.env import WebArenaEnv
from css.model.client import build_clients
import run_experiment_webarena_server as L


def _find(task_id: int) -> "tuple[dict, str]":
    for split in ("val", "train", "test"):
        for r in json.load(open(f"data/webarena_splits/{split}.json")):
            if r.get("task_id") == task_id:
                return r, split
    sys.exit(f"task {task_id} not found")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("task_id", type=int)
    ap.add_argument("--stack", default="s1")
    args = ap.parse_args()
    rec, split = _find(args.task_id)
    ttype = next((e["expected"]["task_type"] for e in rec["eval"]
                  if isinstance(e.get("expected"), dict)), "?")
    print(f"task {args.task_id} ({split}): sites={rec['sites']} type={ttype}")
    print(f"intent: {rec['intent']}")

    out_root = f"runs/webarena_e2e_{args.task_id}"
    os.makedirs(out_root, exist_ok=True)
    env_config = L._write_verified_env_config(
        os.path.join(out_root, "wa_env_config.json"))
    # Pin to ONE stack so the check is deterministic and cheap.
    stacks = {args.stack: L.STACKS[args.stack]}

    cfg = CSSConfig(
        env_name="webarena", n_train=0, n_val=0, n_test=0, seed=42,
        max_turns=30, max_api_workers=8, task_timeout_s=1800,
        target_model="qwen3.6-35b-a3b", optimizer_model="qwen3.6-35b-a3b",
        out_root=out_root,
        extra={**{k: v for k, v in _extra(env_config).items()},
               "webarena_stacks": stacks},
    )
    target_client, _opt = build_clients(cfg)
    env = WebArenaEnv(cfg, items={"train": [], "val": [rec], "test": []})
    res = env.run_one(rec, "", target_client, out_root,
                      rollout_index=0, epoch=0, node_id="e2e")

    ans = (res.extras or {}).get("agent_response") if hasattr(res, "extras") else None
    exp = next((e.get("expected") for e in rec["eval"]
                if isinstance(e.get("expected"), dict)), {})
    print("\n=== RESULT ===")
    print(f"hard={res.hard} soft={res.soft} turns={res.n_turns} "
          f"fail_reason={res.fail_reason}")
    print(f"agent answer (retrieved): {json.dumps(ans, ensure_ascii=False)[:300] if ans else '(n/a)'}")
    print(f"reference  (unseen)     : {json.dumps(exp.get('retrieved_data'), ensure_ascii=False)[:300]}")
    print(f"messages persisted: {len(res.messages)}")


def _extra(env_config: str) -> dict:
    """The launcher's extra dict minus the multi-stack STACKS (set by caller)."""
    return {
        "llm_backend": "openai_compat",
        "base_url": ("http://10.77.110.162:8888/v1,http://10.77.110.162:8889/v1,"
                     "http://127.0.0.1:8888/v1,http://127.0.0.1:8889/v1"),
        "api_key": "token-abc123", "max_tokens": 24576,
        "target_temperature": 0.6, "optimizer_temperature": 0.0,
        "enable_thinking": False, "timeout_seconds": 1800,
        "webarena_split_dir": "data/webarena_splits",
        "webarena_max_browsers": 4,
        "webarena_verified_cli":
            "/home/wushang/miniconda3/envs/webarena/bin/webarena-verified",
        "webarena_env_config": env_config,
        "webarena_har_content": "omit", "webarena_nav_timeout_ms": 120000,
        "webarena_scribe": True, "webarena_auth_dir": ".auth/webarena",
        # no refresh_cmd: the e2e check must not recreate containers
    }


if __name__ == "__main__":
    main()
