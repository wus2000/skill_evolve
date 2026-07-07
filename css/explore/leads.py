"""Probe leads archive — signal only, never solved-state.

Design: docs/L1_actions_redesign.md §1.2. A probe pass proves the TASK is
crackable under SOME behavior — not that any actual strategy solves it (user
ruling 2026-07-07: probe reachability must never flip coverage-ledger state;
the deadlock otherwise: a lucky probe empties ``global_unsolved`` -> MERGE
fuses strategies none of which solve the task -> the task is orphaned).

So passes are archived here as LEADS:

    task_id -> [{behavior_prompt, n_pass, k, session_ref, decision_index}]

capped per task (best pass-rate first, recency breaking ties) and consumed as
hints: NEW/MERGE conception inputs, exploration briefings, and target
ordering (an unsolved task WITH a lead ranks first — the idea exists, no
strategy has absorbed it yet). This module has NO dependency on the coverage
ledger, by construction.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

_log = logging.getLogger("css.explore")

_LEADS_REL = os.path.join("global", "exploration", "leads.json")


def leads_path(out_dir: str) -> str:
    return os.path.join(out_dir, _LEADS_REL)


def _read(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _atomic_write(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def record_lead(
    path: str,
    *,
    task_id: str,
    behavior_prompt: str,
    n_pass: int,
    k: int,
    session_ref: str = "",
    decision_index: int = -1,
    cap: int = 3,
) -> bool:
    """Archive one passing probe as a lead; returns True when stored.

    Keeps at most ``cap`` leads per task, ranked by pass rate then recency.
    Exact-duplicate behavior prompts fold into their best entry.
    """
    if not path or not task_id or int(n_pass) <= 0:
        return False
    entry = {
        "behavior_prompt": str(behavior_prompt or ""),
        "n_pass": int(n_pass),
        "k": max(1, int(k)),
        "session_ref": str(session_ref or ""),
        "decision_index": int(decision_index),
    }
    leads = _read(path)
    bucket = [e for e in leads.get(str(task_id), []) if isinstance(e, dict)]
    # Fold exact-duplicate prompts: keep the stronger record.
    merged = False
    for e in bucket:
        if e.get("behavior_prompt") == entry["behavior_prompt"]:
            if (entry["n_pass"] / entry["k"]) >= (
                    int(e.get("n_pass", 0)) / max(1, int(e.get("k", 1)))):
                e.update(entry)
            merged = True
            break
    if not merged:
        bucket.append(entry)
    bucket.sort(key=lambda e: (-(int(e.get("n_pass", 0)) / max(1, int(e.get("k", 1)))),
                               -int(e.get("decision_index", -1))))
    leads[str(task_id)] = bucket[: max(1, int(cap))]
    _atomic_write(path, leads)
    return True


def load_leads(path: str) -> dict:
    """Full leads mapping (empty when absent)."""
    return _read(path)


def leads_for(path: str, task_ids) -> dict:
    """Subset of the archive for ``task_ids`` (only tasks that have leads)."""
    wanted = {str(t) for t in (task_ids or [])}
    return {t: v for t, v in _read(path).items() if t in wanted}


def render_leads(path: str, task_ids, *, max_chars: int = 4000) -> str:
    """Briefing/conception block: known leads for these tasks (may be '')."""
    subset = leads_for(path, task_ids)
    if not subset:
        return ""
    lines = ["=== KNOWN LEADS (probe passes; ideas found, not yet absorbed "
             "by any strategy) ==="]
    for tid in sorted(subset):
        for e in subset[tid]:
            prompt = str(e.get("behavior_prompt", "")).strip().replace("\n", " ")
            lines.append("- %s [%d/%d passed]: %s"
                         % (tid, int(e.get("n_pass", 0)), int(e.get("k", 1)),
                            prompt))
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[: max_chars - 15] + "\n[truncated]"
    return text
