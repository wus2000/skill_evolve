"""dispatch_probe — run one director-authored behavior prompt as a real rollout.

The core insight (design §3.2 + the rollout layer): the ``skill_text`` argument of
:func:`css.rollout.batch.grouped_batch_rollout` IS the behavior layer of the agent's
system prompt. The env's own agent implementation composes the scaffold (action
format, turn protocol) around it. So a probe is exactly::

    grouped_batch_rollout(env, [task_item], skill_text=behavior_prompt, target_client,
                          k_rollouts=k, out_dir=...)

with the scaffold staying locked automatically — nothing env-specific leaks into
the exploration subsystem. The k rollouts are then narrated faithfully
(:mod:`css.explore.narrator`) and the whole probe archived.
"""
from __future__ import annotations

from typing import Any

from css.explore._util import atomic_write_json, item_descriptor, item_id
from css.explore.narrator import narrate_probe
from css.rollout.batch import grouped_batch_rollout

# Node id tag for probe rollouts (keeps their artifacts out of any tree node's
# namespace; probes are exploratory, not part of a node's persistent state).
_PROBE_NODE_ID = "explore"


# ── Task menu ─────────────────────────────────────────────────────────────────
def build_task_menu(group_tasks: "list[dict]", neighbor_tasks: "list[dict]") -> dict:
    """Map ``task_id -> {item, kind, descriptor}`` for probe resolution.

    ``group_tasks`` are the target (unsolved) group members; ``neighbor_tasks``
    are solved neighbors offered for contrast. Insertion order is preserved so
    the rendered menu is stable. Duplicate ids keep their first (target) kind.
    """
    menu: dict = {}
    for item in group_tasks or []:
        tid = item_id(item)
        if tid and tid not in menu:
            menu[tid] = {"item": item, "kind": "target-unsolved",
                         "descriptor": item_descriptor(item)}
    for item in neighbor_tasks or []:
        tid = item_id(item)
        if tid and tid not in menu:
            menu[tid] = {"item": item, "kind": "solved-neighbor",
                         "descriptor": item_descriptor(item)}
    return menu


def render_task_menu(menu: dict) -> str:
    """Human-readable menu for the director conversation head."""
    tgt = [(tid, m) for tid, m in menu.items() if m["kind"] == "target-unsolved"]
    nbr = [(tid, m) for tid, m in menu.items() if m["kind"] == "solved-neighbor"]
    lines = ["=== TASK MENU (pick task_id EXACTLY from this list) ==="]
    lines.append("")
    lines.append("Target group — unsolved; the tasks the new strategy must crack:")
    for tid, m in tgt:
        lines.append("  - %s: %s" % (tid, m["descriptor"]))
    if nbr:
        lines.append("")
        lines.append("Solved neighbors — nearby tasks current strategies DO solve "
                     "(use for contrast):")
        for tid, m in nbr:
            lines.append("  - %s: %s" % (tid, m["descriptor"]))
    return "\n".join(lines)


# ── Probe dispatch ────────────────────────────────────────────────────────────
def dispatch_probe(
    spec: dict,
    *,
    menu: dict,
    env: Any,
    target_client: Any,
    optimizer_client: Any,
    cfg: Any,
    session_dir: str,
    probe_index: int,
    decision_index: int = 0,
    leads_path: str = "",
) -> dict:
    """Execute one probe and return its archived record (never raises).

    ``spec`` = ``{behavior_prompt, task_id, k, purpose}``. An unknown ``task_id``
    (not in ``menu``) returns a graceful error record — no rollout — so the
    director learns to pick from the menu rather than crashing the session.
    A passing probe is archived as a LEAD here (design §1.2: probe execution is
    the leads boundary, so every dispatch_probe caller feeds the book, not just
    the director loop) — signal for later conception, never coverage state.
    """
    import os

    behavior_prompt = str(spec.get("behavior_prompt", "") or "")
    task_id = str(spec.get("task_id", "") or "")
    purpose = str(spec.get("purpose", "") or "")
    k_req = spec.get("k", 1)
    try:
        k_req = int(k_req)
    except (TypeError, ValueError):
        k_req = 1
    k_max = max(1, int(getattr(cfg, "explore_probe_k_max", 2)))
    k = max(1, min(k_req, k_max))

    record: dict = {
        "probe_index": probe_index,
        "behavior_prompt": behavior_prompt,
        "task_id": task_id,
        "k": k,
        "purpose": purpose,
    }

    entry = menu.get(task_id)
    if entry is None:
        known = ", ".join(list(menu.keys())[:12]) or "(menu empty)"
        err = (
            "ERROR: unknown task_id %r. Pick a task_id EXACTLY from the menu. "
            "Known ids: %s" % (task_id, known)
        )
        record.update(error=err, verdicts=[], narration=err)
        _archive(session_dir, probe_index, record)
        return record

    if not behavior_prompt.strip():
        err = "ERROR: empty behavior_prompt; a probe must carry behavioral instructions."
        record.update(error=err, verdicts=[], narration=err)
        _archive(session_dir, probe_index, record)
        return record

    item = entry["item"]
    probe_out = os.path.join(session_dir, "probes", "probe_%03d_rollouts" % probe_index)
    try:
        groups = grouped_batch_rollout(
            env, [item], behavior_prompt, target_client,
            k_rollouts=k, out_dir=probe_out,
            max_workers=int(getattr(cfg, "max_api_workers", 32)),
            task_timeout=int(getattr(cfg, "task_timeout_s", 600)),
            epoch=decision_index, node_id=_PROBE_NODE_ID,
        )
    except Exception as exc:  # noqa: BLE001 — a probe failure is an observation
        err = "ERROR: the rollout could not run: %s: %s" % (type(exc).__name__, exc)
        record.update(error=err, verdicts=[], narration=err)
        _archive(session_dir, probe_index, record)
        return record

    group = groups[0] if groups else None
    rollouts = list(getattr(group, "rollouts", [])) if group is not None else []
    verdicts = [
        {
            "rollout_index": int(getattr(r, "rollout_index", 0)),
            "passed": bool(getattr(r, "passed", False)),
            "soft": float(getattr(r, "soft", 0.0)),
            "fail_reason": str(getattr(r, "fail_reason", "") or ""),
        }
        for r in rollouts
    ]

    if group is None:
        narration = "(no rollout was produced for this probe)"
    else:
        narration = narrate_probe(
            group, purpose=purpose, optimizer_client=optimizer_client,
            env=env, item=item, cfg=cfg,
        )

    record.update(
        kind=entry["kind"],
        n_pass=sum(1 for v in verdicts if v["passed"]),
        verdicts=verdicts,
        narration=narration,
    )
    _archive(session_dir, probe_index, record)
    if leads_path and record["n_pass"] > 0:
        try:
            from css.explore.leads import record_lead
            record_lead(
                leads_path,
                task_id=task_id,
                behavior_prompt=behavior_prompt,
                n_pass=int(record["n_pass"]),
                k=k,
                session_ref="%s#probe_%d" % (os.path.basename(session_dir),
                                             probe_index),
                decision_index=decision_index,
                cap=int(getattr(cfg, "leads_per_task", 3)),
            )
        except Exception:  # noqa: BLE001 — leads must never kill a probe
            pass
    return record


def _archive(session_dir: str, probe_index: int, record: dict) -> None:
    import os

    path = os.path.join(session_dir, "probes", "probe_%03d.json" % probe_index)
    atomic_write_json(path, record)
