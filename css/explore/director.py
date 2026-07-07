"""The exploration director session loop (design §3).

The director is the optimizer LLM driven as a multi-turn STRUCTURED-OUTPUT loop
(not native tool-calling — the deployment endpoints have flaky tool parsers).
Each turn it is handed the running session transcript and must emit ONE JSON
object: ``{"action": "dispatch", "probes": [...]}`` (1-3 probes, run concurrently)
or ``{"action": "report", "report_markdown": "..."}``.

Silent guardrail (design §3.5): the prompt carries NO budget language; once the
real-probe count reaches ``cfg.explore_probe_guardrail`` a single system note
("Resources are exhausted; write your report now.") is injected and only a report
is accepted thereafter. Everything before that is completely silent so the
director's natural exploration tendency can be measured from the telemetry.

The whole session is archived under ``session_dir`` (plan.json, briefing.md,
probes/probe_<n>.json, transcript.jsonl, report.md, session_meta.json).
"""
from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from css.explore._util import append_jsonl, atomic_write_json, atomic_write_text
from css.explore.probe import build_task_menu, dispatch_probe, render_task_menu
from css.explore.prompts import director_system_prompt
from css.model.json_repair import complete_optimizer_json

_MAX_PROBES_PER_TURN = 3
_GUARDRAIL_NOTE = "Resources are exhausted; write your report now."


# ── Director action parsing ───────────────────────────────────────────────────
def _extract_json_object(text: str) -> Any:
    """Best-effort single-JSON-object extraction from a director turn."""
    if not text:
        return None
    for cand in _json_candidates(text):
        try:
            obj = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _json_candidates(text: str) -> "list[str]":
    cands: "list[str]" = [text.strip()]
    for m in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.DOTALL):
        cands.append(m.group(1).strip())
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        cands.append(m.group(0))
    return cands


def _parse_action(text: str) -> dict:
    obj = _extract_json_object(text)
    return obj if isinstance(obj, dict) else {}


def _action_ok(result: Any) -> bool:
    return isinstance(result, dict) and result.get("action") in ("dispatch", "report")


def _normalize_probes(action: dict) -> "list[dict]":
    """Pull a clean list of probe specs out of a dispatch action."""
    probes = action.get("probes")
    if isinstance(probes, dict):  # a single probe emitted bare
        probes = [probes]
    if not isinstance(probes, list):
        return []
    return [p for p in probes if isinstance(p, dict)]


# ── Session loop ──────────────────────────────────────────────────────────────
def run_director_session(
    *,
    group_key: str,
    mode: str,
    group_tasks: "list[dict]",
    neighbor_tasks: "list[dict]",
    briefing_md: str,
    env: Any,
    target_client: Any,
    optimizer_client: Any,
    cfg: Any,
    session_dir: str,
    decision_index: int = 0,
    history_texts: "list[str]" = (),
    leads_path: str = "",
) -> dict:
    """Run one exploration session; return ``{"report_md", "meta"}``.

    Never raises: a hard failure yields an empty report and a meta with
    ``stop_reason`` describing it. ``history_texts`` (past strategy texts) is used
    only for the ``n_replications`` telemetry (exact-match detection).
    ``leads_path``, when given, archives every passing probe as a LEAD
    (:mod:`css.explore.leads`) — signal for later conception, never
    coverage-ledger state.
    """
    os.makedirs(session_dir, exist_ok=True)
    system = director_system_prompt(mode)
    menu = build_task_menu(group_tasks, neighbor_tasks)
    menu_text = render_task_menu(menu)
    guardrail = max(1, int(getattr(cfg, "explore_probe_guardrail", 48)))
    history_set = {str(h) for h in (history_texts or []) if str(h).strip()}

    # Persisted setup.
    atomic_write_text(os.path.join(session_dir, "briefing.md"), briefing_md or "")
    atomic_write_json(os.path.join(session_dir, "plan.json"), {
        "group_key": group_key, "mode": mode, "decision_index": decision_index,
        "guardrail": guardrail, "explore_probe_k_max": int(getattr(cfg, "explore_probe_k_max", 2)),
        "task_menu": [{"task_id": tid, "kind": m["kind"], "descriptor": m["descriptor"]}
                      for tid, m in menu.items()],
    })
    transcript_path = os.path.join(session_dir, "transcript.jsonl")

    # The running conversation handed to the director as the user message.
    convo = (
        briefing_md.strip() + "\n\n" + menu_text + "\n\n=== YOUR TURN ===\n"
        "Output your JSON object now (dispatch or report)."
    )

    # Telemetry accumulators.
    probe_seq = 0            # monotonic id for EVERY dispatched spec (incl. errors)
    n_probes = 0             # real probes (rollout actually ran) — guardrail domain
    n_contrastive_pairs = 0
    n_replications = 0
    n_repeat_k = 0
    tasks_covered: set = set()
    n_turns = 0
    stop_reason = "self"
    report_md = ""

    guardrail_tripped = False
    max_turns = guardrail + 8   # backstop against a director that never reports

    while n_turns < max_turns:
        n_turns += 1
        action = complete_optimizer_json(
            optimizer_client, system, convo,
            parse=_parse_action, ok=_action_ok,
            max_tokens=12288, stage="explore_director",
        )
        if not _action_ok(action):
            append_jsonl(transcript_path, {"turn": n_turns, "role": "director",
                                           "action": "unparseable"})
            convo += ("\n\n=== SYSTEM NOTE ===\nYour last message was not a valid "
                      "action JSON. Emit exactly one JSON object with an \"action\" "
                      "of \"dispatch\" or \"report\".\n\n=== YOUR TURN ===\n"
                      "Output your JSON object now.")
            continue

        kind = action.get("action")
        append_jsonl(transcript_path, {"turn": n_turns, "role": "director",
                                       "action": kind})

        if kind == "report":
            report_md = str(action.get("report_markdown", "") or "").strip()
            stop_reason = "guardrail" if guardrail_tripped else "self"
            break

        # kind == "dispatch"
        if guardrail_tripped:
            # Only a report is accepted now; re-inject the note and re-ask.
            convo += ("\n\n=== SYSTEM NOTE ===\n" + _GUARDRAIL_NOTE +
                      "\n\n=== YOUR TURN ===\nEmit your report JSON now.")
            continue

        specs = _normalize_probes(action)
        truncated = len(specs) > _MAX_PROBES_PER_TURN
        specs = specs[:_MAX_PROBES_PER_TURN]
        if not specs:
            convo += ("\n\n=== SYSTEM NOTE ===\nNo valid probe was found in your "
                      "dispatch. Provide at least one probe {behavior_prompt, "
                      "task_id, k, purpose}, or write your report.\n\n"
                      "=== YOUR TURN ===\nOutput your JSON object now.")
            continue

        # Assign a stable archival index to each spec, then run concurrently.
        indexed = []
        for spec in specs:
            indexed.append((probe_seq, spec))
            probe_seq += 1
        records = _run_probes_concurrently(
            indexed, menu=menu, env=env, target_client=target_client,
            optimizer_client=optimizer_client, cfg=cfg, session_dir=session_dir,
            decision_index=decision_index,
        )

        # Telemetry from the real (executed) probes of THIS turn.
        real = [r for r in records if not r.get("error")]
        n_probes += len(real)
        for r in real:
            tasks_covered.add(r.get("task_id", ""))
            if int(r.get("k", 1)) > 1:
                n_repeat_k += 1
            if r.get("behavior_prompt", "") in history_set:
                n_replications += 1
        n_contrastive_pairs += _count_contrastive_pairs(real)

        # Passing probes become LEADS (design §1.2) — hints for later
        # conception; NEVER solved-state (a lucky probe must not be able to
        # empty global_unsolved).
        if leads_path:
            from css.explore.leads import record_lead
            for r in real:
                if int(r.get("n_pass", 0)) > 0:
                    try:
                        record_lead(
                            leads_path,
                            task_id=str(r.get("task_id", "")),
                            behavior_prompt=str(r.get("behavior_prompt", "")),
                            n_pass=int(r.get("n_pass", 0)),
                            k=int(r.get("k", 1)),
                            session_ref="%s#probe_%s" % (
                                os.path.basename(session_dir),
                                r.get("probe_index")),
                            decision_index=decision_index,
                            cap=int(getattr(cfg, "leads_per_task", 3)),
                        )
                    except Exception:  # noqa: BLE001 — leads must not kill a turn
                        pass

        for r in records:
            append_jsonl(transcript_path, {"turn": n_turns, "role": "probe",
                                           "probe_index": r.get("probe_index"),
                                           "task_id": r.get("task_id"),
                                           "error": bool(r.get("error"))})

        convo += _render_turn_results(n_turns, records, truncated=truncated)

        if n_probes >= guardrail and not guardrail_tripped:
            guardrail_tripped = True
            convo += ("\n\n=== SYSTEM NOTE ===\n" + _GUARDRAIL_NOTE +
                      "\n\n=== YOUR TURN ===\nEmit your report JSON now.")
    else:
        # Loop exhausted without a report.
        stop_reason = "guardrail" if guardrail_tripped else "self"
        if not report_md:
            report_md = ("(No report was produced: the session ended on its turn "
                         "backstop after %d probes.)" % n_probes)

    meta = {
        "group_key": group_key, "mode": mode, "decision_index": decision_index,
        "n_probes": n_probes,
        "n_contrastive_pairs": n_contrastive_pairs,
        "n_replications": n_replications,
        "n_repeat_k": n_repeat_k,
        "tasks_covered": len([t for t in tasks_covered if t]),
        "n_turns": n_turns,
        "stop_reason": stop_reason,
        "report_chars": len(report_md),
    }
    atomic_write_text(os.path.join(session_dir, "report.md"), report_md)
    atomic_write_json(os.path.join(session_dir, "session_meta.json"), meta)
    return {"report_md": report_md, "meta": meta}


def _run_probes_concurrently(
    indexed: "list[tuple]", *, menu, env, target_client, optimizer_client, cfg,
    session_dir, decision_index,
) -> "list[dict]":
    """Dispatch this turn's probes concurrently, preserving spec order."""
    if len(indexed) == 1:
        idx, spec = indexed[0]
        return [dispatch_probe(spec, menu=menu, env=env, target_client=target_client,
                               optimizer_client=optimizer_client, cfg=cfg,
                               session_dir=session_dir, probe_index=idx,
                               decision_index=decision_index)]
    results: "list[dict]" = [None] * len(indexed)  # type: ignore[list-item]
    with ThreadPoolExecutor(max_workers=len(indexed)) as pool:
        fut_to_pos = {}
        for pos, (idx, spec) in enumerate(indexed):
            fut = pool.submit(
                dispatch_probe, spec, menu=menu, env=env, target_client=target_client,
                optimizer_client=optimizer_client, cfg=cfg, session_dir=session_dir,
                probe_index=idx, decision_index=decision_index,
            )
            fut_to_pos[fut] = pos
        for fut in fut_to_pos:
            pos = fut_to_pos[fut]
            try:
                results[pos] = fut.result()
            except Exception as exc:  # noqa: BLE001 — never let one probe kill the turn
                idx, spec = indexed[pos]
                results[pos] = {"probe_index": idx, "task_id": spec.get("task_id", ""),
                                "error": "probe crashed: %s" % exc, "verdicts": [],
                                "narration": "probe crashed: %s" % exc}
    return results


def _count_contrastive_pairs(real_records: "list[dict]") -> int:
    """Count contrastive prompt-pairs within one turn: same task_id, distinct
    behavior_prompts. ``C(distinct, 2)`` per task (usually 1 for a clean pair)."""
    by_task: dict = {}
    for r in real_records:
        by_task.setdefault(r.get("task_id", ""), set()).add(r.get("behavior_prompt", ""))
    pairs = 0
    for prompts in by_task.values():
        d = len(prompts)
        if d >= 2:
            pairs += d * (d - 1) // 2
    return pairs


def _render_turn_results(turn: int, records: "list[dict]", *, truncated: bool) -> str:
    parts = ["\n\n=== PROBE RESULTS (turn %d) ===" % turn]
    if truncated:
        parts.append("(only the first %d probes of this turn were run; dispatch "
                     "fewer per turn or continue next turn)" % _MAX_PROBES_PER_TURN)
    for r in records:
        head = "\n--- probe %s | task_id=%s | k=%s ---" % (
            r.get("probe_index"), r.get("task_id"), r.get("k", 1))
        purpose = r.get("purpose", "")
        block = [head]
        if purpose:
            block.append("purpose: %s" % purpose)
        block.append(str(r.get("narration", "")))
        parts.append("\n".join(block))
    parts.append("\n=== YOUR TURN ===\nOutput your next JSON object (dispatch or report).")
    return "\n".join(parts)
