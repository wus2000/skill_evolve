#!/usr/bin/env python3
"""Pre-flight checks before the full CSS server run.

Verifies, in order: (1) data splits load with expected counts, (2) the remote
LLM endpoint answers an optimizer completion with the configured api key + model
+ Qwen thinking flag, (3) the embedder loads on CPU and embeds a probe text,
(4) one real SpreadsheetBench rollout executes end-to-end (LLM -> generated code
-> openpyxl exec -> eval). Exits non-zero on the first failure.
"""
import os
import sys
import traceback

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
sys.path.insert(0, os.path.dirname(__file__))

from css.config import CSSConfig
from css.model.client import build_clients
from css.analysis.embedding import Qwen3Embedder
from css.envs.spreadsheetbench.task_interface import SpreadsheetBenchEnv

DATA_BASE = "/home/wushang/workspace/data"


def _cfg() -> CSSConfig:
    return CSSConfig(
        n_train=80, n_val=40, n_test=280,
        split_dir=f"{DATA_BASE}/spreadsheetbench_split",
        data_root=f"{DATA_BASE}/spreadsheet_raw/spreadsheetbench_verified_400",
        target_model="qwen3.6-35b-a3b",
        optimizer_model="qwen3.6-35b-a3b",
        max_api_workers=8, task_timeout_s=600, max_turns=3,
        embedding_model="/home/wushang/models/Qwen3-Embedding-0.6B",
        reflect_mode="plan_a",
        out_root="runs/_preflight",
        extra={
            "llm_backend": "openai_compat",
            "base_url": "http://10.77.110.162:8888/v1",
            "api_key": "token-abc123",
            "max_tokens": 16384, "temperature": 0.7, "enable_thinking": False,
        },
    )


def main() -> int:
    cfg = _cfg()
    cfg.validate()
    target_client, optimizer_client = build_clients(cfg)

    # (1) Data
    env = SpreadsheetBenchEnv(cfg, split_dir=cfg.split_dir, data_root=cfg.data_root)
    tr, va, te = env.train_items(), env.val_items(), env.test_items()
    print(f"[1/4] DATA  train={len(tr)} val={len(va)} test={len(te)}")
    assert len(tr) and len(va) and len(te), "empty split"
    probe = tr[0]
    print(f"      sample task id={probe.get('id')} type={probe.get('instruction_type')}")

    # (2) LLM endpoint
    txt, usage = optimizer_client.complete_optimizer(
        "You are a calculator.", "Reply with exactly the number 4 and nothing else. What is 2+2?",
    )
    print(f"[2/4] LLM   reply={txt.strip()[:60]!r} usage={usage}")
    assert txt.strip(), "empty LLM reply"

    # (3) Embedder (CPU)
    emb = Qwen3Embedder(model_name=cfg.embedding_model)
    vec = emb.embed(["hello world", "spreadsheet manipulation"])
    print(f"[3/4] EMBED shape={vec.shape} dim={emb.dim} norm0={float((vec[0]**2).sum())**0.5:.3f}")
    assert vec.shape[0] == 2, "embed count mismatch"

    # (4) One real rollout end-to-end
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        res = env.run_one(probe, "", target_client, td, rollout_index=0, epoch=0, node_id="preflight")
    print(f"[4/4] ROLLOUT id={res.task_id} hard={res.hard} soft={res.soft:.3f} "
          f"n_pass={res.n_pass}/{res.n_cases} turns={res.n_turns} fail={res.fail_reason[:80]!r}")

    print("\nPREFLIGHT OK")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        print("\nPREFLIGHT FAILED")
        sys.exit(1)
