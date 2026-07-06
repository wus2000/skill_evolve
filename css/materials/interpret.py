"""Layer 1 — per-trajectory strategy-behavior interpretation (design §2.3).

Every on-policy step rollout of the burst is interpreted at STRATEGY-BEHAVIOR
altitude (full volume unless ``cfg.interp_max_per_burst`` caps it). Each product
is persisted to ``interpretations/traj_<id>.json`` before proceeding, and the
stage skips any trajectory whose product already exists (step-granular resume).
"""
from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING, Any

from css.materials import common, prompts
from css.trajectory import format_trajectory

if TYPE_CHECKING:  # pragma: no cover
    from css.config import CSSConfig
    from css.data.tree import TreeNode
    from css.tree_search import BurstResult

_log = logging.getLogger("css.materials")



def _interp_required(obj: Any) -> list:
    """Missing mandatory narrative fields (feeds the json_repair schema hook)."""
    if not isinstance(obj, dict):
        return ["the entire interpretation object"]
    missing = []
    for f in ("narrative", "outcome_causality", "behavior_signature"):
        if not str(obj.get(f, "")).strip():
            missing.append(f"{f} (required, non-empty)")
    return missing


def _outcome_line(traj: "common.LoadedTraj") -> str:
    r = traj.result
    verdict = "PASS" if traj.passed else "FAIL"
    line = f"{verdict} (hard={r.hard}, soft={r.soft:.3f}, turns={r.n_turns})"
    if r.fail_reason:
        line += f"; fail_reason={r.fail_reason}"
    return line


def _interpret_one(
    traj: "common.LoadedTraj", strategy: str, path: str,
    optimizer_client: Any, cfg: "CSSConfig",
) -> dict:
    """Interpret one trajectory (or load its cached product) → a record dict."""
    cached = common.read_json(path)
    if isinstance(cached, dict) and cached.get("interp"):
        return cached

    render = format_trajectory(
        traj.result.messages, tool_trunc=getattr(cfg, "tool_trunc", 8000),
        include_system=False,
    )
    user = prompts.build_interpret_user(strategy, render, _outcome_line(traj))
    interp = common.run_json_stage(
        optimizer_client, prompts.INTERPRET_SYSTEM, user,
        parse=common.parse_object, stage="interp", cfg=cfg,
        required=_interp_required,
        ok=lambda r: isinstance(r, dict) and bool(r),
    )
    if not isinstance(interp, dict):
        interp = {"narrative": "", "outcome_causality": "",
                  "behavior_signature": "", "_error": "unparseable interpretation"}
    record = {
        "traj_id": traj.traj_id,
        "task_id": traj.task_id,
        "rollout_index": traj.rollout_index,
        "step": traj.step,
        "passed": traj.passed,
        "hard": int(traj.result.hard),
        "soft": float(traj.result.soft),
        "fail_reason": traj.result.fail_reason,
        "interp": interp,
    }
    common.write_json_atomic(path, record)
    return record


def interpret_burst(
    node: "TreeNode", burst_result: "BurstResult",
    optimizer_client: Any, cfg: "CSSConfig", out_dir: str,
) -> list[dict]:
    """Interpret all of the burst's on-policy trajectories → list of records.

    Records are returned sorted by ``traj_id`` for deterministic downstream
    grouping. Interpretation runs in parallel over ``cfg.max_api_workers``.
    """
    trajs = common.cap_trajectories(
        common.load_burst_trajectories(burst_result.exploit_dir), cfg)
    if not trajs:
        _log.info("materials/interpret — node=%s burst=%d: no trajectories on disk "
                  "under %s", node.node_id, burst_result.burst_index,
                  burst_result.exploit_dir)
        return []

    idir = common.interpretations_dir(out_dir, node.node_id, burst_result.burst_index)
    os.makedirs(idir, exist_ok=True)
    strategy = node.strategy or ""

    records: list[dict] = []
    max_workers = max(1, int(getattr(cfg, "max_api_workers", 32)))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {}
        for t in trajs:
            path = os.path.join(idir, f"traj_{t.traj_id}.json")
            futs[pool.submit(_interpret_one, t, strategy, path, optimizer_client, cfg)] = t.traj_id
        for fut in as_completed(futs):
            try:
                records.append(fut.result())
            except Exception:  # noqa: BLE001 — one bad trajectory must not sink the burst
                _log.exception("materials/interpret — trajectory %s failed", futs[fut])

    records.sort(key=lambda r: r.get("traj_id", ""))
    _log.info("materials/interpret — node=%s burst=%d: %d/%d trajectories interpreted",
              node.node_id, burst_result.burst_index, len(records), len(trajs))
    return records
