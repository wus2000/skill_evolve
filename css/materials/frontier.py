"""Frontier (failure-side) analysis feeding REFINE (design §2.3).

Groups a node's residual failing tasks by FAILURE MODE, narrates each mode's
behavior -> structural-failure mechanism and its cross-burst evolution, and ends
each with an A/B/U attribution:

  * A — the strategy lacks a behavioral mechanism the task needs (a REFINE target);
  * B — the strategy says it but the agent does not follow it (expression target);
  * U — no strategy-level cause established (an EVIDENTIAL verdict). A failure mode
        that persists as U while the node's L0 has stalled is flagged
        ``escalate_to_exploration`` so a probe is sent to find out why it resists.

frontier_analysis.md is a living document (integrated via
:func:`css.materials.dossier.integrate_living_document`). The machine-readable
index ``nodes/<id>/dossier/frontier_attribution.json`` is the CONTRACT the
generation package reads:

    {group_key: {"attribution": "A"|"B"|"U", "task_ids": [...],
                 "summary": str, "escalate_to_exploration": bool}}
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from css.materials import common, dossier, prompts

if TYPE_CHECKING:  # pragma: no cover
    from css.config import CSSConfig
    from css.data.tree import TreeNode
    from css.tree_search import BurstResult

_log = logging.getLogger("css.materials")

_FRONTIER_MAX_TOKENS = 10240


def _node_stalled(node: "TreeNode", cfg: "CSSConfig") -> bool:
    stall = int(getattr(cfg, "l0_stall_steps", 8) or 0)
    return stall > 0 and node.step_buffer.steps_since_new_best() >= stall


def _residual_representatives(records: list[dict]) -> dict:
    """task_id -> a representative failing record, for tasks no rollout solved."""
    by_task: dict[str, list[dict]] = {}
    for r in records:
        by_task.setdefault(r["task_id"], []).append(r)
    residual: dict[str, dict] = {}
    for tid, rs in by_task.items():
        if not any(x.get("passed") for x in rs):
            residual[tid] = rs[0]
    return residual


def _failing_view(rec: dict) -> dict:
    interp = rec.get("interp", {})
    return {"task_id": rec.get("task_id"), "traj_id": rec.get("traj_id"),
            "narrative": interp.get("narrative", ""),
            "outcome_causality": interp.get("outcome_causality", ""),
            "anomalies": interp.get("anomalies", "")}


def _append_residual_history(path: str, burst_index: int, task_ids: list[str]) -> int:
    """Idempotently record this burst's residuals; return cross-burst persistence."""
    history = common.read_jsonl(path)
    prior_counts: dict[str, int] = {}
    already = False
    for row in history:
        if int(row.get("burst", -1)) == burst_index:
            already = True
        for t in row.get("residual_task_ids", []) or []:
            prior_counts[t] = prior_counts.get(t, 0) + 1
    if not already:
        common.append_jsonl(path, {"burst": burst_index, "residual_task_ids": task_ids})
    # persistence = how many of THIS burst's residuals also failed in prior bursts
    return sum(1 for t in task_ids if prior_counts.get(t, 0) > 0)


def _normalize_frontier_groups(groups: list, residual_ids: set[str]) -> list[dict]:
    seen: set[str] = set()
    norm: list[dict] = []
    for g in groups if isinstance(groups, list) else []:
        if not isinstance(g, dict):
            continue
        key = str(g.get("group_key", "")).strip() or f"mode_{len(norm)}"
        tasks = [str(t) for t in (g.get("task_ids") or [])
                 if str(t) in residual_ids and str(t) not in seen]
        seen.update(tasks)
        if tasks:
            norm.append({"group_key": key, "rationale": str(g.get("rationale", "")),
                         "task_ids": tasks})
    leftover = [t for t in residual_ids if t not in seen]
    if leftover:
        norm.append({"group_key": "unclustered_failures",
                     "rationale": "Residual tasks the failure-mode grouping did not assign.",
                     "task_ids": leftover})
    return norm


def update_frontier(
    node: "TreeNode", burst_result: "BurstResult", records: list[dict],
    mining_products: dict, optimizer_client: Any, cfg: "CSSConfig", out_dir: str,
) -> dict:
    """Failure-mode grouping -> per-group narrative + A/B/U -> living doc + attribution."""
    bdir = common.analysis_burst_dir(out_dir, node.node_id, burst_result.burst_index)
    frontier_diff = f"{bdir}/frontier_update_diff.json"
    attribution_path = common.dossier_path(out_dir, node.node_id, "frontier_attribution.json")

    if common.exists(frontier_diff):
        return {"attribution": common.read_json(attribution_path) or {}, "skipped": True}

    residual = _residual_representatives(records)
    residual_ids = sorted(residual.keys())
    _append_residual_history(
        common.dossier_path(out_dir, node.node_id, "residual_history.jsonl"),
        burst_result.burst_index, residual_ids)

    prior_groups = common.read_json(
        common.dossier_path(out_dir, node.node_id, "frontier_groups.json")) or {}
    adherence_reading = mining_products.get("adherence", {}).get("reading", "")
    stalled = _node_stalled(node, cfg)
    doc_path = common.dossier_path(out_dir, node.node_id, "frontier_analysis.md")

    # No residuals this burst: record the improvement in the living doc if one
    # exists, and clear the current attribution index.
    if not residual_ids:
        common.write_json_atomic(attribution_path, {})
        if common.read_text(doc_path):
            note = [{"group_key": "(none)", "attribution": "U",
                     "narrative": "This burst produced no residual failing tasks; "
                     "previously-failing modes should be re-checked for resolution."}]

            def _build_empty(old_doc: str, feedback: str) -> str:
                return prompts.build_frontier_integrate_user(old_doc, note, feedback)

            dossier.integrate_living_document(
                doc_path=doc_path, diff_path=frontier_diff,
                integrate_system=prompts.FRONTIER_INTEGRATE_SYSTEM, build_user=_build_empty,
                stage="frontier", optimizer_client=optimizer_client, cfg=cfg)
        else:
            common.write_json_atomic(frontier_diff, {"ledger": [], "unaccounted": [],
                                                     "note": "no residuals, no prior doc"})
        _log.info("materials/frontier — node=%s burst=%d: no residual tasks",
                  node.node_id, burst_result.burst_index)
        return {"attribution": {}, "grouping": {"groups": []}}

    # 1. Failure-mode grouping (reuse prior keys for continuity).
    failing_for_grouping = [_failing_view(residual[t]) for t in residual_ids]
    raw = common.run_json_stage(
        optimizer_client, prompts.FRONTIER_GROUP_SYSTEM,
        prompts.build_frontier_group_user(failing_for_grouping, sorted(prior_groups.keys())),
        parse=common.parse_object, stage="frontier_group", cfg=cfg,
        ok=lambda r: isinstance(r, dict) and isinstance(r.get("groups"), list),
        max_tokens=_FRONTIER_MAX_TOKENS,
    )
    groups = _normalize_frontier_groups((raw or {}).get("groups", []), set(residual_ids))
    common.write_json_atomic(f"{bdir}/frontier_grouping.json", {"groups": groups})

    # 2. Per-group narrative + A/B/U attribution.
    group_narratives: list[dict] = []
    attribution: dict[str, dict] = {}
    new_group_store: dict[str, dict] = dict(prior_groups)
    for g in groups:
        key = g["group_key"]
        prior = prior_groups.get(key, {})
        prior_attr = str(prior.get("attribution", "")).strip().upper()
        failing = [_failing_view(residual[t]) for t in g["task_ids"] if t in residual]
        obj = common.run_json_stage(
            optimizer_client, prompts.FRONTIER_NARRATIVE_SYSTEM,
            prompts.build_frontier_narrative_user(
                key, g.get("rationale", ""), failing, prior.get("narrative", ""),
                adherence_reading, stalled, prior_attr),
            parse=common.parse_object, stage="frontier_narrative", cfg=cfg,
            ok=lambda r: isinstance(r, dict) and bool(str(r.get("narrative", "")).strip()),
            max_tokens=_FRONTIER_MAX_TOKENS,
        )
        obj = obj if isinstance(obj, dict) else {}
        attr = str(obj.get("attribution", "U")).strip().upper()
        if attr not in ("A", "B", "U"):
            attr = "U"
        rationale_txt = str(obj.get("attribution_rationale", "")).strip()
        narrative_txt = str(obj.get("narrative", "")).strip()
        # Escalation is U-only: LLM flag OR (persistent-U across bursts AND stalled).
        escalate = attr == "U" and (
            bool(obj.get("escalate_to_exploration", False))
            or (prior_attr == "U" and stalled))

        group_narratives.append({"group_key": key, "attribution": attr,
                                 "narrative": narrative_txt})
        attribution[key] = {
            "attribution": attr,
            "task_ids": g["task_ids"],
            "summary": rationale_txt or g.get("rationale", "")[:400],
            "escalate_to_exploration": bool(escalate),
        }
        new_group_store[key] = {
            "narrative": narrative_txt, "attribution": attr,
            "attribution_rationale": rationale_txt, "task_ids": g["task_ids"],
            "escalate_to_exploration": bool(escalate),
            "first_burst": prior.get("first_burst", burst_result.burst_index),
            "last_burst": burst_result.burst_index,
        }

    # 3. Living frontier_analysis.md + no-silent-loss audit.
    def build_user(old_doc: str, feedback: str) -> str:
        return prompts.build_frontier_integrate_user(old_doc, group_narratives, feedback)

    dossier.integrate_living_document(
        doc_path=doc_path, diff_path=frontier_diff,
        integrate_system=prompts.FRONTIER_INTEGRATE_SYSTEM, build_user=build_user,
        stage="frontier", optimizer_client=optimizer_client, cfg=cfg)

    # 4. Persist the machine-readable indexes (CONTRACT + continuity store).
    common.write_json_atomic(attribution_path, attribution)
    common.write_json_atomic(
        common.dossier_path(out_dir, node.node_id, "frontier_groups.json"), new_group_store)

    n_escalate = sum(1 for v in attribution.values() if v["escalate_to_exploration"])
    _log.info("materials/frontier — node=%s burst=%d: %d failure modes "
              "(A=%d B=%d U=%d, escalate=%d)",
              node.node_id, burst_result.burst_index, len(attribution),
              sum(1 for v in attribution.values() if v["attribution"] == "A"),
              sum(1 for v in attribution.values() if v["attribution"] == "B"),
              sum(1 for v in attribution.values() if v["attribution"] == "U"), n_escalate)
    return {"attribution": attribution, "grouping": {"groups": groups}}
