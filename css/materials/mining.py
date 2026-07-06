"""Layer 2 — mining (design §2.3).

Turns the burst's per-trajectory interpretations into behavior-mode groups, a
deep analysis per group, a mechanical adherence ledger with a synthesized
reading, and a burst summary. Each product is persisted and resume-skipped.
"""
from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING, Any

from css.materials import common, prompts

if TYPE_CHECKING:  # pragma: no cover
    from css.config import CSSConfig
    from css.data.tree import TreeNode
    from css.tree_search import BurstResult

_log = logging.getLogger("css.materials")

_GROUP_MAX_TOKENS = 10240
_MERGE_MAX_TOKENS = 10240
_ADHERENCE_MAX_TOKENS = 6144
_SUMMARY_MAX_TOKENS = 6144
_ADHERENCE_NOTE_CAP = 12
_ADHERENCE_EVIDENCE_CAP = 40


# ── grouping (two-pass) ──────────────────────────────────────────────────────
def _normalize_groups(groups: list, all_ids: list[str]) -> list[dict]:
    """Enforce the partition invariant: every id in exactly one non-empty group."""
    seen: set[str] = set()
    norm: list[dict] = []
    for g in groups if isinstance(groups, list) else []:
        if not isinstance(g, dict):
            continue
        key = str(g.get("group_key", "")).strip() or f"group_{len(norm)}"
        members = [str(m) for m in (g.get("member_traj_ids") or [])
                   if str(m) in all_ids and str(m) not in seen]
        seen.update(members)
        mset = set(members)
        norm.append({
            "group_key": key,
            "rationale": str(g.get("rationale", "")),
            "member_traj_ids": members,
            "representative_traj_ids": [str(m) for m in (g.get("representative_traj_ids") or [])
                                        if str(m) in mset],
            "uncertain_traj_ids": [str(m) for m in (g.get("uncertain_traj_ids") or [])
                                   if str(m) in mset],
        })
    leftover = [i for i in all_ids if i not in seen]
    if leftover:
        norm.append({
            "group_key": "unclustered",
            "rationale": "Trajectories pass 1 did not assign to a behavioral mode.",
            "member_traj_ids": leftover,
            "representative_traj_ids": leftover[:2],
            "uncertain_traj_ids": [],
        })
    return [g for g in norm if g["member_traj_ids"]]


def _apply_reassignments(groups: list[dict], assignments: list) -> tuple[list[dict], list[dict]]:
    key_to_group = {g["group_key"]: g for g in groups}
    id_to_key = {m: g["group_key"] for g in groups for m in g["member_traj_ids"]}
    reassigns: list[dict] = []
    for a in assignments if isinstance(assignments, list) else []:
        if not isinstance(a, dict):
            continue
        tid, tgt = str(a.get("traj_id", "")), str(a.get("group_key", ""))
        cur = id_to_key.get(tid)
        if not tid or tgt not in key_to_group or cur is None or cur == tgt:
            continue
        key_to_group[cur]["member_traj_ids"].remove(tid)
        key_to_group[tgt]["member_traj_ids"].append(tid)
        id_to_key[tid] = tgt
        reassigns.append({"traj_id": tid, "from": cur, "to": tgt})
    return [g for g in groups if g["member_traj_ids"]], reassigns


def _group_two_pass(records: list[dict], by_id: dict, path: str,
                    client: Any, cfg: "CSSConfig") -> dict:
    cached = common.read_json(path)
    if isinstance(cached, dict) and cached.get("groups"):
        return cached

    all_ids = [r["traj_id"] for r in records]
    signatures = [{"traj_id": r["traj_id"], "passed": r.get("passed"),
                   "behavior_signature": r.get("interp", {}).get("behavior_signature", "")}
                  for r in records]
    raw = common.run_json_stage(
        client, prompts.GROUP_PASS1_SYSTEM, prompts.build_group_pass1_user(signatures),
        parse=common.parse_object, stage="group_pass1", cfg=cfg,
        ok=lambda r: isinstance(r, dict) and isinstance(r.get("groups"), list),
        max_tokens=_GROUP_MAX_TOKENS,
    )
    groups = _normalize_groups((raw or {}).get("groups", []), all_ids)

    # Pass 2: re-check boundary members against full narratives.
    boundary_ids = [tid for g in groups for tid in g.get("uncertain_traj_ids", [])]
    reassigns: list[dict] = []
    if boundary_ids:
        boundary_narratives = [
            {"traj_id": tid, "narrative": by_id.get(tid, {}).get("interp", {}).get("narrative", "")}
            for tid in boundary_ids
        ]
        p2 = common.run_json_stage(
            client, prompts.GROUP_PASS2_SYSTEM,
            prompts.build_group_pass2_user(groups, boundary_narratives),
            parse=common.parse_list_field("assignments"), stage="group_pass2", cfg=cfg,
            ok=lambda r: isinstance(r, list),
            max_tokens=_GROUP_MAX_TOKENS,
        )
        groups, reassigns = _apply_reassignments(groups, p2 or [])

    result = {"groups": groups, "pass2_reassignments": reassigns}
    common.write_json_atomic(path, result)
    return result


# ── per-group deep analysis (map -> merge when large) ────────────────────────
def _narratives_for(group: dict, by_id: dict) -> list[dict]:
    out = []
    for m in group["member_traj_ids"]:
        r = by_id.get(m)
        if r is None:
            continue
        out.append({"traj_id": m, "passed": r.get("passed"),
                    "narrative": r.get("interp", {}).get("narrative", "")})
    return out


def _analyze_group(group: dict, by_id: dict, gpath: str,
                   client: Any, cfg: "CSSConfig") -> dict:
    cached = common.read_json(gpath)
    if isinstance(cached, dict) and cached.get("analysis_narrative"):
        return cached

    key = group["group_key"]
    narratives = _narratives_for(group, by_id)
    chunk = max(1, int(getattr(cfg, "materials_group_chunk", 12)))

    if len(narratives) <= chunk:
        analysis = common.run_json_stage(
            client, prompts.GROUP_ANALYSIS_SYSTEM,
            prompts.build_group_analysis_user(key, group.get("rationale", ""), narratives),
            parse=common.parse_object, stage="group_analysis", cfg=cfg,
            ok=lambda r: isinstance(r, dict) and bool(str(r.get("analysis_narrative", "")).strip()),
            max_tokens=_GROUP_MAX_TOKENS,
        ) or {}
    else:
        subs = []
        for start in range(0, len(narratives), chunk):
            sub = common.run_json_stage(
                client, prompts.GROUP_ANALYSIS_SYSTEM,
                prompts.build_group_analysis_user(key, group.get("rationale", ""),
                                                  narratives[start:start + chunk]),
                parse=common.parse_object, stage="group_analysis", cfg=cfg,
                ok=lambda r: isinstance(r, dict) and bool(str(r.get("analysis_narrative", "")).strip()),
                max_tokens=_GROUP_MAX_TOKENS,
            )
            if isinstance(sub, dict):
                subs.append(sub)
        analysis = common.run_json_stage(
            client, prompts.GROUP_MERGE_SYSTEM,
            prompts.build_group_merge_user(key, subs),
            parse=common.parse_object, stage="group_merge", cfg=cfg,
            ok=lambda r: isinstance(r, dict) and bool(str(r.get("analysis_narrative", "")).strip()),
            max_tokens=_MERGE_MAX_TOKENS,
        ) or {}
        # Preserve distilled claims from the sub-analyses if the merge dropped them.
        if not analysis.get("distilled_claims"):
            merged_claims = [c for s in subs for c in (s.get("distilled_claims") or [])]
            if merged_claims:
                analysis["distilled_claims"] = merged_claims

    analysis = dict(analysis)
    analysis["group_key"] = key
    analysis.setdefault("distilled_claims", [])
    analysis.setdefault("open_questions", [])
    analysis.setdefault("analysis_narrative", "")
    common.write_json_atomic(gpath, analysis)
    return analysis


# ── adherence ledger (mechanical aggregation + one reading) ──────────────────
_VERDICTS = ("followed", "partial", "ignored", "inapplicable")


def _mechanical_adherence(records: list[dict]) -> dict:
    sections: dict[str, dict] = {}
    for r in records:
        for a in r.get("interp", {}).get("adherence", []) or []:
            if not isinstance(a, dict):
                continue
            sec = str(a.get("section", "")).strip()
            if not sec:
                continue
            verdict = str(a.get("verdict", "")).strip().lower()
            entry = sections.setdefault(sec, {v: 0 for v in _VERDICTS})
            entry.setdefault("evidence_traj_ids", [])
            entry.setdefault("notes", [])
            if verdict in _VERDICTS:
                entry[verdict] += 1
            if len(entry["evidence_traj_ids"]) < _ADHERENCE_EVIDENCE_CAP:
                entry["evidence_traj_ids"].append(r["traj_id"])
            note = str(a.get("note", "")).strip()
            if note and len(entry["notes"]) < _ADHERENCE_NOTE_CAP:
                entry["notes"].append(note)
    return sections


def _adherence_ledger(records: list[dict], path: str,
                      client: Any, cfg: "CSSConfig") -> dict:
    cached = common.read_json(path)
    if isinstance(cached, dict) and "sections" in cached:
        return cached

    sections = _mechanical_adherence(records)
    reading = ""
    if sections:
        obj = common.run_json_stage(
            client, prompts.ADHERENCE_READING_SYSTEM,
            prompts.build_adherence_reading_user(sections),
            parse=common.parse_object, stage="adherence_reading", cfg=cfg,
            ok=lambda r: isinstance(r, dict) and bool(str(r.get("reading", "")).strip()),
            max_tokens=_ADHERENCE_MAX_TOKENS,
        )
        if isinstance(obj, dict):
            reading = str(obj.get("reading", ""))
    ledger = {"sections": sections, "reading": reading}
    common.write_json_atomic(path, ledger)
    return ledger


# ── burst synthesis ──────────────────────────────────────────────────────────
def _burst_summary(group_analyses: list[dict], adherence_reading: str,
                   stats: dict, path: str, client: Any, cfg: "CSSConfig") -> str:
    cached = common.read_text(path)
    if cached is not None and cached.strip():
        return cached
    obj = common.run_json_stage(
        client, prompts.BURST_SUMMARY_SYSTEM,
        prompts.build_burst_summary_user(group_analyses, adherence_reading, stats),
        parse=common.parse_object, stage="burst_summary", cfg=cfg,
        ok=lambda r: isinstance(r, dict) and bool(str(r.get("summary_md", "")).strip()),
        max_tokens=_SUMMARY_MAX_TOKENS,
    )
    md = str((obj or {}).get("summary_md", "")).strip() or "(burst summary unavailable)"
    common.write_text_atomic(path, md)
    return md


# ── entry point ──────────────────────────────────────────────────────────────
def mine_burst(node: "TreeNode", burst_result: "BurstResult", records: list[dict],
               optimizer_client: Any, cfg: "CSSConfig", out_dir: str) -> dict:
    """Group -> deep-analyze -> adherence ledger -> burst summary. Returns the products."""
    if not records:
        return {"grouping": {"groups": []}, "group_analyses": [],
                "adherence": {"sections": {}, "reading": ""}, "burst_summary_md": ""}

    bdir = common.analysis_burst_dir(out_dir, node.node_id, burst_result.burst_index)
    gdir = common.group_analyses_dir(out_dir, node.node_id, burst_result.burst_index)
    os.makedirs(gdir, exist_ok=True)
    by_id = {r["traj_id"]: r for r in records}

    grouping = _group_two_pass(records, by_id,
                               os.path.join(bdir, "grouping.json"), optimizer_client, cfg)
    groups = grouping.get("groups", [])

    group_analyses: list[dict] = []
    max_workers = max(1, min(int(getattr(cfg, "max_api_workers", 32)), max(1, len(groups))))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {
            pool.submit(_analyze_group, g, by_id,
                        os.path.join(gdir, f"{common.safe_id(g['group_key'])}.json"),
                        optimizer_client, cfg): g["group_key"]
            for g in groups
        }
        for fut in as_completed(futs):
            try:
                group_analyses.append(fut.result())
            except Exception:  # noqa: BLE001
                _log.exception("materials/mining — group %s analysis failed", futs[fut])
    group_analyses.sort(key=lambda a: a.get("group_key", ""))

    adherence = _adherence_ledger(records, os.path.join(bdir, "adherence_ledger.json"),
                                  optimizer_client, cfg)

    n_pass = sum(1 for r in records if r.get("passed"))
    stats = {
        "burst": burst_result.burst_index,
        "n_trajectories": len(records),
        "n_pass": n_pass, "n_fail": len(records) - n_pass,
        "n_groups": len(groups),
        "accepted_steps": burst_result.n_accepted,
        "reward": round(burst_result.reward, 6),
        "val_before": round(burst_result.val_before, 6),
        "val_after": round(burst_result.val_after, 6),
    }
    summary_md = _burst_summary(group_analyses, adherence.get("reading", ""), stats,
                                os.path.join(bdir, "burst_summary.md"),
                                optimizer_client, cfg)

    _log.info("materials/mining — node=%s burst=%d: %d groups, %d group analyses",
              node.node_id, burst_result.burst_index, len(groups), len(group_analyses))
    return {"grouping": grouping, "group_analyses": group_analyses,
            "adherence": adherence, "burst_summary_md": summary_md, "stats": stats}
