"""Read-side helpers: assemble the material a generation pipeline feeds the LLM.

Everything here is read-if-exists with honest ``(not yet available)`` placeholders
— the materials and exploration subsystems land incrementally, so a pipeline must
run (with thinner inputs) before they exist. Node strategy text is taken from the
in-memory tree (authoritative, always current); the dossier prose documents
(rationale / behavior profile / frontier analysis) come from disk where the
materials pass writes them.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from css.l1gen import _io
from css.tree_search import dossier_dir, node_dir

_log = logging.getLogger("css.l1gen")

_NA = "(not yet available)"


def gen_dir(out_dir: str, node_id: str) -> str:
    return os.path.join(node_dir(out_dir, node_id), "gen")


def _cap(text: str, cfg: Any) -> str:
    cap = int(getattr(cfg, "gen_doc_char_cap", 0) or 0)
    if cap > 0 and len(text) > cap:
        return text[:cap] + "\n…[truncated %d chars]…" % (len(text) - cap)
    return text


def _read_doc(path: str) -> str:
    txt = _io.read_text(path)
    return txt if (txt and txt.strip()) else _NA


def node_strategy(tree: Any, out_dir: str, node_id: str) -> str:
    """Strategy text: prefer the in-memory node, fall back to on-disk strategy.md."""
    node = tree.get(node_id) if tree is not None else None
    if node is not None and (node.strategy or "").strip():
        return node.strategy
    disk = _io.read_text(os.path.join(node_dir(out_dir, node_id), "strategy.md"))
    return disk if (disk and disk.strip()) else _NA


def node_dossier_block(tree: Any, out_dir: str, node_id: str, cfg: Any) -> str:
    """One node's full dossier: strategy + rationale + behavior profile + frontier."""
    dd = dossier_dir(out_dir, node_id)
    node = tree.get(node_id) if tree is not None else None
    branch = getattr(node, "branch_type", "?") if node is not None else "?"
    val = getattr(node, "val_score", 0.0) if node is not None else 0.0
    parts = [
        "=== NODE %s (branch=%s val=%.4f) ===" % (node_id, branch, val),
        "--- strategy.md ---\n" + _cap(node_strategy(tree, out_dir, node_id), cfg),
        "--- dossier/rationale.md ---\n" + _cap(_read_doc(os.path.join(dd, "rationale.md")), cfg),
        "--- dossier/behavior_profile.md ---\n"
        + _cap(_read_doc(os.path.join(dd, "behavior_profile.md")), cfg),
        "--- dossier/frontier_analysis.md ---\n"
        + _cap(_read_doc(os.path.join(dd, "frontier_analysis.md")), cfg),
    ]
    return "\n\n".join(parts)


def all_node_dossiers(tree: Any, out_dir: str, cfg: Any) -> str:
    ids = sorted(getattr(tree, "nodes", {}).keys()) if tree is not None else []
    if not ids:
        return _NA
    return "\n\n".join(node_dossier_block(tree, out_dir, nid, cfg) for nid in ids)


def prior_strategies(tree: Any, out_dir: str, cfg: Any, *, exclude: "Optional[str]" = None) -> str:
    """Strategy texts of every strategy-bearing node (skips the bare root / excluded)."""
    ids = sorted(getattr(tree, "nodes", {}).keys()) if tree is not None else []
    blocks: "List[str]" = []
    for nid in ids:
        if nid == exclude:
            continue
        strat = node_strategy(tree, out_dir, nid)
        if strat == _NA or not strat.strip():
            continue  # bare root / unwritten node
        blocks.append("=== STRATEGY %s ===\n%s" % (nid, _cap(strat, cfg)))
    return "\n\n".join(blocks) if blocks else "(no prior strategies — this is the first)"


def global_unsolved(out_dir: str, cfg: Any) -> str:
    """Cross-strategy unsolved synthesis: grouping + per-group narratives."""
    base = os.path.join(out_dir, "global", "unsolved")
    parts: "List[str]" = []
    for name in ("groups.json", "grouping.json"):
        obj = _io.read_json(os.path.join(base, name))
        if obj is not None:
            parts.append("--- %s ---\n%s" % (name, _cap(str(obj), cfg)))
            break
    try:
        narratives = sorted(
            f for f in os.listdir(base)
            if f.startswith("group_") and f.endswith(".md")
        ) if os.path.isdir(base) else []
    except OSError:
        narratives = []
    for f in narratives:
        parts.append("--- %s ---\n%s" % (f, _cap(_read_doc(os.path.join(base, f)), cfg)))
    return "\n\n".join(parts) if parts else _NA


def findings_on_disk(out_dir: str, cfg: Any) -> str:
    """Concatenate cached exploration findings.md files (design §2.1)."""
    base = os.path.join(out_dir, "global", "exploration")
    if not os.path.isdir(base):
        return _NA
    blocks: "List[str]" = []
    for root, _dirs, files in os.walk(base):
        for f in sorted(files):
            if f == "findings.md":
                rel = os.path.relpath(os.path.join(root, f), base)
                blocks.append("--- %s ---\n%s"
                              % (rel, _cap(_read_doc(os.path.join(root, f)), cfg)))
    return "\n\n".join(blocks) if blocks else _NA


def frontier_attribution(out_dir: str, node_id: str) -> "Tuple[dict, str]":
    """Load ``dossier/frontier_attribution.json`` (materials contract) + a text view.

    Contract: ``{group_key: {attribution: "A"|"B"|"U", task_ids, summary,
    escalate_to_exploration}}``. Returns ``({}, placeholder)`` if absent.
    """
    obj = _io.read_json(os.path.join(dossier_dir(out_dir, node_id), "frontier_attribution.json"))
    if not isinstance(obj, dict) or not obj:
        return {}, _NA
    import json
    return obj, json.dumps(obj, ensure_ascii=False, indent=2)


def sibling_ids(tree: Any, node_id: str) -> "List[str]":
    if tree is None:
        return []
    return [s.node_id for s in tree.siblings(node_id)]


def sibling_dossiers(tree: Any, out_dir: str, node_id: str, cfg: Any) -> str:
    ids = sibling_ids(tree, node_id)
    if not ids:
        return "(no siblings)"
    return "\n\n".join(node_dossier_block(tree, out_dir, sid, cfg) for sid in ids)


def dead_sibling_strategies(tree: Any, out_dir: str, node_id: str, cfg: Any) -> str:
    """Strategy text of siblings that are terminal/pruned (already-failed directions)."""
    if tree is None:
        return "(none)"
    blocks: "List[str]" = []
    for s in tree.siblings(node_id):
        if getattr(s, "status", "") in ("terminal", "pruned"):
            strat = node_strategy(tree, out_dir, s.node_id)
            if strat != _NA:
                blocks.append("=== DEAD SIBLING %s (status=%s) ===\n%s"
                              % (s.node_id, s.status, _cap(strat, cfg)))
    return "\n\n".join(blocks) if blocks else "(no dead siblings)"


# ── Exploration targets + inputs (sourced from the materials contracts) ───────
def new_exploration_target(out_dir: str) -> "Optional[Tuple[str, List[str]]]":
    """Highest-priority global-unsolved group for NEW exploration, or None.

    Reads ``global/unsolved/groups.json`` (materials contract:
    ``{group_key: {task_ids, summary, priority, ...}}``) and returns
    ``(group_key, task_ids)`` for the top-priority group, or None if the synthesis
    does not exist yet.
    """
    groups = _io.read_json(os.path.join(out_dir, "global", "unsolved", "groups.json"))
    if not isinstance(groups, dict) or not groups:
        return None
    best: "Optional[Tuple[float, str, List[str]]]" = None
    for key, meta in groups.items():
        if not isinstance(meta, dict):
            continue
        tids = [str(t) for t in (meta.get("task_ids") or [])]
        try:
            prio = float(meta.get("priority", len(tids)))
        except (TypeError, ValueError):
            prio = float(len(tids))
        if best is None or prio > best[0]:
            best = (prio, str(key), tids)
    return (best[1], best[2]) if best is not None else None


def refine_exploration_targets(attribution: dict) -> "List[Tuple[str, List[str]]]":
    """U groups flagged ``escalate_to_exploration`` -> ``(group_key, task_ids)`` list.

    Design §2.3: a persistently-unexplained (U) failure family with L0 stalled is
    escalated to a probe. Reads the ``frontier_attribution.json`` contract.
    """
    out: "List[Tuple[str, List[str]]]" = []
    for key, meta in (attribution or {}).items():
        if not isinstance(meta, dict):
            continue
        if str(meta.get("attribution", "")).upper() == "U" and meta.get("escalate_to_exploration"):
            out.append((str(key), [str(t) for t in (meta.get("task_ids") or [])]))
    return out


def _group_narrative_md(out_dir: str, group_key: str) -> str:
    base = os.path.join(out_dir, "global", "unsolved")
    meta = _io.read_json(os.path.join(base, "meta.json")) or {}
    order = list(meta.get("group_order") or [])
    try:
        idx = order.index(group_key)
    except ValueError:
        return ""
    return _io.read_text(os.path.join(base, "group_%d.md" % idx)) or ""


def exploration_briefing_new(tree: Any, out_dir: str, cfg: Any, group_key: str,
                             group_meta: dict) -> str:
    """Complete history package for a NEW target group (the probe agent's briefing)."""
    parts = ["## Target group: %s" % group_key]
    if group_meta.get("summary"):
        parts.append("Summary: %s" % group_meta["summary"])
    nar = _group_narrative_md(out_dir, group_key)
    if nar.strip():
        parts.append("### Cross-strategy synthesis\n" + _cap(nar, cfg))
    parts.append("### Every strategy tried (full dossiers)\n"
                 + all_node_dossiers(tree, out_dir, cfg))
    return "\n\n".join(parts)


def exploration_briefing_refine(tree: Any, out_dir: str, cfg: Any, node_id: str,
                                group_key: str, group_meta: dict) -> str:
    """History package for a REFINE U-group escalation (this node's dossier + group)."""
    parts = ["## Target group: %s" % group_key]
    if group_meta.get("summary"):
        parts.append("Summary: %s" % group_meta["summary"])
    parts.append("### Current strategy dossier\n"
                 + node_dossier_block(tree, out_dir, node_id, cfg))
    return "\n\n".join(parts)


def run_exploration(
    *,
    mode: str,
    group_key: str,
    group_tasks: "List[str]",
    neighbor_tasks: "List[str]",
    briefing_md: str,
    cfg: Any,
    env: Any,
    target_client: Any,
    optimizer_client: Any,
    out_dir: str,
    decision_index: int,
) -> dict:
    """Call ``css.explore.api.get_or_explore`` with real group inputs; never fatal.

    Returns ``{"findings": str, "source": str, "group_key": str}``. Skips (no
    findings) when there is no target group; on a missing ``css.explore`` module or
    any error returns empty findings with a diagnostic ``source`` (design §4:
    "never fail the pipeline over missing exploration"). ``get_or_explore`` is
    itself never-raising, but we guard anyway. ``neighbor_tasks`` is currently ``[]``
    (solved-neighbor selection is a materials concern the director tolerates empty).
    """
    if not group_key:
        return {"findings": "", "source": "no_target_group"}
    try:
        from css.explore.api import get_or_explore  # type: ignore
    except Exception:  # noqa: BLE001 — module may be absent in a thin checkout
        return {"findings": "", "source": "explore_module_absent", "group_key": group_key}
    try:
        findings = get_or_explore(
            group_key, group_tasks=list(group_tasks or []),
            neighbor_tasks=list(neighbor_tasks or []), briefing_md=briefing_md or "",
            mode=mode, env=env, target_client=target_client,
            optimizer_client=optimizer_client, cfg=cfg, out_dir=out_dir,
            decision_index=decision_index,
        )
    except Exception:  # noqa: BLE001 — belt-and-suspenders around a never-raising API
        _log.exception("get_or_explore raised; continuing without findings")
        return {"findings": "", "source": "explore_error", "group_key": group_key}
    return {"findings": str(findings or ""), "source": "explore", "group_key": group_key}
