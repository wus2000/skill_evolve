"""Global unsolved — cross-strategy synthesis (design §2.3), the NEW-target source.

Mechanical intersection over every node's latest residual set: tasks failing at
EVERY node are the global hard residual; tasks solved by only some strategies are
paradigm-sensitive. Each global-hard group gets one cross-strategy reading — do
the paradigms fail the SAME way (a common failure mechanism, the prime target for
a genuinely new strategy) or in different ways — plus a priority.

CONTRACT (read by the NEW generation pipeline):
  * ``global/unsolved/groups.json`` — ``{group_key: {task_ids, summary, priority,
    common_mechanism: bool}}``. Insertion order matches the ``group_<k>.md`` files.
  * ``global/unsolved/group_<k>.md`` — the k-th group's narrative synthesis.
  * ``global/unsolved/meta.json`` — signature (resume skip), global-hard and
    paradigm-sensitive task id lists.
"""
from __future__ import annotations

import hashlib
import logging
import os
from typing import TYPE_CHECKING, Any

from css.materials import common, prompts

if TYPE_CHECKING:  # pragma: no cover
    from css.config import CSSConfig
    from css.data.tree import SearchTree, TreeNode

_log = logging.getLogger("css.materials")



def _node_latest_residuals(out_dir: str, node_id: str) -> set[str] | None:
    hist = common.read_jsonl(
        common.dossier_path(out_dir, node_id, "residual_history.jsonl"))
    if not hist:
        return None
    return set(str(t) for t in hist[-1].get("residual_task_ids", []) or [])


def _signature(node_residuals: dict[str, set]) -> str:
    parts = [f"{nid}:{','.join(sorted(ids))}" for nid, ids in sorted(node_residuals.items())]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _frontier_groups(out_dir: str, node_id: str) -> dict:
    return common.read_json(
        common.dossier_path(out_dir, node_id, "frontier_groups.json")) or {}


def _attributions_touching(tree: "SearchTree", out_dir: str, task_ids: set[str]) -> list[dict]:
    """Per-node frontier attributions whose tasks overlap ``task_ids`` (grouping context)."""
    out = []
    for nid in tree.nodes:
        for key, g in _frontier_groups(out_dir, nid).items():
            gt = set(str(t) for t in g.get("task_ids", []))
            if gt & task_ids:
                out.append({"node_id": nid, "group_key": key,
                            "attribution": g.get("attribution", "U"),
                            "task_ids": sorted(gt & task_ids)})
    return out


def _node_narratives_for(tree: "SearchTree", out_dir: str, task_ids: list[str]) -> list[dict]:
    """Each node's single best-overlapping frontier narrative for these tasks."""
    tset = set(task_ids)
    out = []
    for nid in tree.nodes:
        best = None
        best_overlap = 0
        for key, g in _frontier_groups(out_dir, nid).items():
            overlap = len(set(str(t) for t in g.get("task_ids", [])) & tset)
            if overlap > best_overlap:
                best_overlap, best = overlap, g
        if best is not None:
            out.append({"node_id": nid, "attribution": best.get("attribution", "U"),
                        "narrative": best.get("narrative", "")})
    return out


def _normalize_global_groups(groups: list, hard: set[str]) -> list[dict]:
    seen: set[str] = set()
    norm: list[dict] = []
    for g in groups if isinstance(groups, list) else []:
        if not isinstance(g, dict):
            continue
        key = str(g.get("group_key", "")).strip() or f"global_{len(norm)}"
        tasks = [str(t) for t in (g.get("task_ids") or [])
                 if str(t) in hard and str(t) not in seen]
        seen.update(tasks)
        if tasks:
            norm.append({"group_key": key, "task_ids": tasks,
                         "character": str(g.get("character", ""))})
    leftover = [t for t in hard if t not in seen]
    if leftover:
        norm.append({"group_key": "global_residual", "task_ids": leftover,
                     "character": "Globally-unsolved tasks not otherwise grouped."})
    return norm


def synthesize_global(
    tree: "SearchTree", node: "TreeNode", optimizer_client: Any,
    cfg: "CSSConfig", out_dir: str,
) -> dict:
    """Recompute the cross-strategy unsolved synthesis. Cheap mechanical core +
    signature-gated LLM readings (skipped when the residual landscape is unchanged)."""
    gdir = common.global_unsolved_dir(out_dir)
    os.makedirs(gdir, exist_ok=True)
    groups_path = os.path.join(gdir, "groups.json")
    meta_path = os.path.join(gdir, "meta.json")

    node_residuals: dict[str, set] = {}
    for nid in tree.nodes:
        ids = _node_latest_residuals(out_dir, nid)
        if ids is not None:
            node_residuals[nid] = ids
    if not node_residuals:
        return {}

    sets = list(node_residuals.values())
    global_hard = sorted(set.intersection(*sets)) if sets else []
    union = sorted(set().union(*sets)) if sets else []
    paradigm_sensitive = sorted(set(union) - set(global_hard))
    signature = _signature(node_residuals)

    prior_meta = common.read_json(meta_path)
    if isinstance(prior_meta, dict) and prior_meta.get("signature") == signature:
        return common.read_json(groups_path) or {}

    if not global_hard:
        common.write_json_atomic(groups_path, {})
        common.write_json_atomic(meta_path, {
            "signature": signature, "n_nodes": len(node_residuals),
            "global_hard_task_ids": [], "paradigm_sensitive_task_ids": paradigm_sensitive})
        _log.info("materials/global — no global-hard residual (%d nodes, %d paradigm-sensitive)",
                  len(node_residuals), len(paradigm_sensitive))
        return {}

    hard_set = set(global_hard)
    raw = common.run_json_stage(
        optimizer_client, prompts.GLOBAL_GROUP_SYSTEM,
        prompts.build_global_group_user(global_hard, _attributions_touching(tree, out_dir, hard_set)),
        parse=common.parse_object, stage="global_group", cfg=cfg,
        ok=lambda r: isinstance(r, dict) and isinstance(r.get("groups"), list),
    )
    groups = _normalize_global_groups((raw or {}).get("groups", []), hard_set)

    contract: dict[str, dict] = {}
    for k, g in enumerate(groups):
        node_narratives = _node_narratives_for(tree, out_dir, g["task_ids"])
        reading = common.run_json_stage(
            optimizer_client, prompts.GLOBAL_READING_SYSTEM,
            prompts.build_global_reading_user(g["group_key"], g["task_ids"], node_narratives),
            parse=common.parse_object, stage="global_reading", cfg=cfg,
            ok=lambda r: isinstance(r, dict) and bool(str(r.get("narrative_md", "")).strip()),
        )
        reading = reading if isinstance(reading, dict) else {}
        try:
            priority = float(reading.get("priority", len(g["task_ids"])))
        except (TypeError, ValueError):
            priority = float(len(g["task_ids"]))
        contract[g["group_key"]] = {
            "task_ids": g["task_ids"],
            "summary": str(reading.get("summary", g.get("character", ""))),
            "priority": priority,
            "common_mechanism": bool(reading.get("common_mechanism", False)),
        }
        common.write_text_atomic(os.path.join(gdir, f"group_{k}.md"),
                                 str(reading.get("narrative_md", "")).strip()
                                 or "(cross-strategy synthesis unavailable)")

    # Emit in priority order (higher first); dict order matches group_<k>.md by
    # re-writing the md files to the reordered index would break the k<->key tie,
    # so keep groups.json in the same order the md files were written.
    common.write_json_atomic(groups_path, contract)
    common.write_json_atomic(meta_path, {
        "signature": signature, "n_nodes": len(node_residuals),
        "global_hard_task_ids": global_hard,
        "paradigm_sensitive_task_ids": paradigm_sensitive,
        "group_order": list(contract.keys())})
    _log.info("materials/global — %d global-hard tasks in %d groups (%d nodes)",
              len(global_hard), len(contract), len(node_residuals))
    return contract
