"""The materials pass — single entry point matching the core loop's injection
contract (``css.tree_search`` ``materials_fn``).

Orchestrates, for ONE burst of ONE node, design §2's stages 2->7:

  1. interpret         — Layer 1 per-trajectory interpretation (full volume);
  2. screen            — Altitude Screen on the interpretations (Layer 1 gate);
  3. mine              — Layer 2 grouping + group analysis + adherence + summary;
  4. behavior profile  — Layer 3 living document (+ altitude screen on the claims
                         that enter it);
  5. frontier          — failure-mode A/B/U + attribution index (+ screen on the
                         attribution summaries);
  6. global unsolved   — cross-strategy synthesis.

Every sub-stage is resumable (skips itself if its product exists) and non-fatal:
a failure in one stage is logged and does not block the independent later ones.
The whole pass is already wrapped by the core loop in a try/except, so a fatal
here never kills the run.
"""
from __future__ import annotations

import copy
import logging
import os
from typing import Any

from css.materials import (
    common, dossier, frontier, global_unsolved, interpret, mining, screen,
)

_log = logging.getLogger("css.materials")


def _apply_interp_screen(records: list[dict], optimizer_client: Any, cfg: Any,
                         out_dir: str, node_id: str, burst_index: int) -> list[dict]:
    """Screen interpretation narratives; adopt revisions, drop rejects. Returns
    the kept records (rejected ones stay on disk in screen_verdicts.json)."""
    if not records:
        return records
    sv_path = os.path.join(
        common.interpretations_dir(out_dir, node_id, burst_index), "screen_verdicts.json")
    cached = common.read_json(sv_path)
    if isinstance(cached, list) and len(cached) == len(records):
        verdicts = cached
    else:
        items = [str(r.get("interp", {}).get("narrative", "")) for r in records]
        verdicts = screen.screen_items(items, optimizer_client, cfg, stage="interp")
        for i, v in enumerate(verdicts):
            if i < len(records):
                v["traj_id"] = records[i]["traj_id"]
        common.write_json_atomic(sv_path, verdicts)

    by_tid = {r["traj_id"]: r for r in records}
    for v in verdicts:
        r = by_tid.get(v.get("traj_id"))
        if r is None:
            idx = v.get("index")
            r = records[idx] if isinstance(idx, int) and 0 <= idx < len(records) else None
        if r is None:
            continue
        if v.get("revised") and v.get("text"):
            r["interp"]["narrative"] = v["text"]
        r["screen_verdict"] = v.get("verdict", "pass")
    kept = [r for r in records if r.get("screen_verdict") != "rejected"]
    _log.info("materials/screen — node=%s burst=%d: %d/%d interpretations kept",
              node_id, burst_index, len(kept), len(records))
    return kept


def _screen_profile_claims(mining_products: dict, optimizer_client: Any, cfg: Any,
                           bdir: str) -> dict:
    """Altitude-screen the distilled claims that will enter behavior_profile.md;
    drop rejects, adopt revisions. Returns a filtered copy of the products."""
    analyses = mining_products.get("group_analyses", [])
    flat: list[tuple[int, int]] = []
    items: list[str] = []
    for gi, a in enumerate(analyses):
        for ci, c in enumerate(a.get("distilled_claims", []) or []):
            if isinstance(c, dict) and str(c.get("claim", "")).strip():
                flat.append((gi, ci))
                items.append(c["claim"])
    if not items:
        return mining_products

    sv_path = os.path.join(bdir, "profile_screen.json")
    cached = common.read_json(sv_path)
    if isinstance(cached, list) and len(cached) == len(items):
        verdicts = cached
    else:
        verdicts = screen.screen_items(items, optimizer_client, cfg, stage="profile_claims")
        common.write_json_atomic(sv_path, verdicts)

    filtered = copy.deepcopy(mining_products)
    fa = filtered.get("group_analyses", [])
    drop: set[tuple[int, int]] = set()
    for pos, (gi, ci) in enumerate(flat):
        v = verdicts[pos] if pos < len(verdicts) else {}
        if v.get("verdict") == "rejected":
            drop.add((gi, ci))
        elif v.get("revised") and v.get("text"):
            fa[gi]["distilled_claims"][ci]["claim"] = v["text"]
    for gi, a in enumerate(fa):
        a["distilled_claims"] = [c for ci, c in enumerate(a.get("distilled_claims", []) or [])
                                 if (gi, ci) not in drop]
    if drop:
        _log.info("materials/screen — dropped %d off-altitude profile claim(s)", len(drop))
    return filtered


def _screen_frontier_summaries(attribution: dict, optimizer_client: Any, cfg: Any,
                               bdir: str) -> None:
    """QA screen on the frontier attribution summaries (record-only: a failure
    mode is never dropped, but off-altitude summaries are surfaced)."""
    items = [str(v.get("summary", "")) for v in attribution.values() if str(v.get("summary", "")).strip()]
    if not items:
        return
    sv_path = os.path.join(bdir, "frontier_screen.json")
    if common.exists(sv_path):
        return
    verdicts = screen.screen_items(items, optimizer_client, cfg, stage="frontier_summary")
    common.write_json_atomic(sv_path, verdicts)


def run_materials_pass(
    *, tree, node, burst_result, env, optimizer_client, cfg, out_dir, ledger=None,
) -> None:
    """Design §2 materials pass for one burst. Never raises (best-effort per stage)."""
    del env, ledger  # not needed: interpretation reads trajectories from disk
    nid, bidx = node.node_id, burst_result.burst_index
    bdir = common.analysis_burst_dir(out_dir, nid, bidx)
    os.makedirs(bdir, exist_ok=True)

    # 1-2. Interpret + Altitude Screen (Layer 1 gate for mining).
    kept: list[dict] = []
    try:
        records = interpret.interpret_burst(node, burst_result, optimizer_client, cfg, out_dir)
        kept = _apply_interp_screen(records, optimizer_client, cfg, out_dir, nid, bidx)
    except Exception:  # noqa: BLE001
        _log.exception("materials/interpret+screen failed (node=%s burst=%d)", nid, bidx)
        return  # nothing downstream without interpretations

    if not kept:
        _log.info("materials — node=%s burst=%d: no kept interpretations; "
                  "skipping mining/dossier/frontier", nid, bidx)
        return

    # 3. Mining (Layer 2).
    mining_products: dict = {}
    try:
        mining_products = mining.mine_burst(node, burst_result, kept,
                                            optimizer_client, cfg, out_dir)
    except Exception:  # noqa: BLE001
        _log.exception("materials/mining failed (node=%s burst=%d)", nid, bidx)

    # 4. Behavior profile (Layer 3 living doc) with an altitude screen on the
    #    claims that enter it.
    try:
        if mining_products:
            gated = _screen_profile_claims(mining_products, optimizer_client, cfg, bdir)
            dossier.update_behavior_profile(node, burst_result, gated,
                                            optimizer_client, cfg, out_dir)
    except Exception:  # noqa: BLE001
        _log.exception("materials/profile failed (node=%s burst=%d)", nid, bidx)

    # 5. Frontier (failure side) + attribution index + QA screen on summaries.
    try:
        fres = frontier.update_frontier(node, burst_result, kept, mining_products,
                                        optimizer_client, cfg, out_dir)
        _screen_frontier_summaries(fres.get("attribution", {}), optimizer_client, cfg, bdir)
    except Exception:  # noqa: BLE001
        _log.exception("materials/frontier failed (node=%s burst=%d)", nid, bidx)

    # 6. Global unsolved (cross-strategy synthesis).
    try:
        global_unsolved.synthesize_global(tree, node, optimizer_client, cfg, out_dir)
    except Exception:  # noqa: BLE001
        _log.exception("materials/global failed (node=%s burst=%d)", nid, bidx)

    _log.info("materials — node=%s burst=%d: pass complete", nid, bidx)
