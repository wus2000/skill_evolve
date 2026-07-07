"""Layer 1 — per-trajectory strategy-behavior interpretation (design §2.3).

Every on-policy step rollout of the burst is interpreted at STRATEGY-BEHAVIOR
altitude (full volume unless ``cfg.interp_max_per_burst`` caps it). Each product
is persisted to ``interpretations/traj_<id>.json`` before proceeding, and the
stage skips any trajectory whose product already exists (step-granular resume).

TWO-PASS protocol (2026-07-07 redesign; replaces the single 8-key JSON call
whose combined reasoning+format load produced a ~30% LLM-repair rate):

  Pass 1 — INTERPRET: free markdown prose under four fixed headings (OVERALL
  BEHAVIOR / STRATEGY ADHERENCE / OUTCOME CAUSALITY / ANOMALIES), section-by-
  section adherence coverage guided by the strategy's real section list, turn
  numbers cited inline. Zero output schema — the reasoning-heavy pass carries
  no format load, and the prose is archived verbatim (``interp_prose``) so any
  future structured field can be re-extracted offline without re-reading the
  trajectory.

  Pass 2 — EXTRACT: a small call over the PROSE (not the trajectory) producing
  the structured fields downstream actually consumes: behavior_signature
  (mining pass-1 grouping) and adherence (closed-set section choice; the
  adherence ledger). Missing-field recovery retries the ORIGINAL extract call
  once (json_repair content path); a still-failing extract records
  ``_extract_error`` while the prose-derived narrative fields survive.

  The record's narrative / outcome_causality / anomalies fields are split
  mechanically from the prose headings — a best-effort, LOSSLESS split: when a
  heading cannot be found the full prose lands in ``narrative`` and the other
  fields stay empty (parsing is a bonus, never a gate).

Retired fields (no production consumer ever read them): strategy_signals,
task_group_hint, key_steps.
"""
from __future__ import annotations

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING, Any

from css.materials import common, prompts
from css.trajectory import format_trajectory

if TYPE_CHECKING:  # pragma: no cover
    from css.config import CSSConfig
    from css.data.tree import TreeNode
    from css.tree_search import BurstResult

_log = logging.getLogger("css.materials")

_PROSE_MAX_TOKENS = 8192
_EXTRACT_MAX_TOKENS = 4096

_VERDICTS = frozenset({"followed", "partial", "ignored", "inapplicable"})

# Prose headings (parse anchors). Matching is case-insensitive and tolerant of
# heading level (#..####) — the split is a bonus, never a gate.
_HEADINGS = ("OVERALL BEHAVIOR", "STRATEGY ADHERENCE", "OUTCOME CAUSALITY",
             "ANOMALIES")


def _strategy_section_names(strategy: str) -> "list[str]":
    """Names of the strategy's '## ' sections (the adherence closed set)."""
    try:
        from css.l1gen.sections import parse_sections
        return [s.name for s in parse_sections(strategy or "")]
    except Exception:  # noqa: BLE001 — a bare/legacy strategy yields no names
        return []


def _split_prose(prose: str) -> dict:
    """Best-effort split of the four-heading prose into record fields.

    LOSSLESS by construction: whatever cannot be attributed to the causality /
    anomalies headings stays in ``narrative`` (which is the ENTIRE prose when
    no heading is found).
    """
    spans: "dict[str, int]" = {}
    for h in _HEADINGS:
        m = re.search(r"(?mi)^#{1,4}\s*" + re.escape(h) + r"\b.*$", prose)
        if m:
            spans[h] = m.start()
    if not spans:
        return {"narrative": prose.strip(), "outcome_causality": "",
                "anomalies": ""}
    ordered = sorted(spans.items(), key=lambda kv: kv[1])

    def _section_text(name: str) -> str:
        if name not in spans:
            return ""
        start = spans[name]
        later = [pos for _h, pos in ordered if pos > start]
        end = min(later) if later else len(prose)
        block = prose[start:end]
        block = re.sub(r"(?mi)^#{1,4}\s*" + re.escape(name) + r"\b.*$", "",
                       block, count=1)
        return block.strip()

    causality = _section_text("OUTCOME CAUSALITY")
    anomalies = _section_text("ANOMALIES")
    if anomalies.lower().rstrip(".") in ("none observed", "none"):
        anomalies = ""
    # Narrative = everything before whichever of causality/anomalies starts
    # first (covers preamble + OVERALL BEHAVIOR + STRATEGY ADHERENCE, however
    # the model arranged them); the whole prose when neither is present.
    cut_candidates = [spans[h] for h in ("OUTCOME CAUSALITY", "ANOMALIES")
                      if h in spans]
    narrative = prose[: min(cut_candidates)].strip() if cut_candidates \
        else prose.strip()
    return {"narrative": narrative, "outcome_causality": causality,
            "anomalies": anomalies}


def _extract_required(obj: Any) -> list:
    if not isinstance(obj, dict):
        return ["the entire extraction object"]
    if not str(obj.get("behavior_signature", "")).strip():
        return ["behavior_signature (required, non-empty)"]
    return []


def _clean_adherence(raw: Any, section_names: "list[str]") -> "tuple[list, int]":
    """Echo-validate adherence entries; returns ``(kept, dropped_count)``.

    ``section`` must verbatim-match the closed set and ``verdict`` the enum —
    a mismatched entry is dropped (counted, logged), never guessed at.
    """
    valid = set(section_names)
    kept: "list[dict]" = []
    dropped = 0
    for a in raw if isinstance(raw, list) else []:
        if not isinstance(a, dict):
            dropped += 1
            continue
        section = str(a.get("section", "")).strip()
        verdict = str(a.get("verdict", "")).strip().lower()
        if section not in valid or verdict not in _VERDICTS:
            dropped += 1
            continue
        steps = [int(s) for s in (a.get("evidence_steps") or [])
                 if isinstance(s, (int, float))]
        kept.append({"section": section, "verdict": verdict,
                     "evidence_steps": steps,
                     "note": str(a.get("note", "") or "")})
    return kept, dropped


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
    feedback: str = "", force: bool = False,
) -> dict:
    """Interpret one trajectory (or load its cached product) → a record dict.

    ``feedback`` (screen critique) makes this a REVISION of a prior reading:
    it is appended to the prose prompt and, with ``force=True``, the cached
    product is re-produced and overwritten (the screen judges; the source
    pipeline regenerates — decision log #14).
    """
    cached = common.read_json(path)
    if not force and isinstance(cached, dict) and cached.get("interp"):
        return cached

    render = format_trajectory(
        traj.result.messages, tool_trunc=getattr(cfg, "tool_trunc", 8000),
        include_system=False,
    )
    section_names = _strategy_section_names(strategy)

    meta = {
        "traj_id": traj.traj_id,
        "task_id": traj.task_id,
        "rollout_index": traj.rollout_index,
        "step": traj.step,
        "passed": traj.passed,
        "hard": int(traj.result.hard),
        "soft": float(traj.result.soft),
        "fail_reason": traj.result.fail_reason,
    }

    # ── Pass 1 — free prose (zero schema; the reasoning-heavy pass) ────────
    prose_user = prompts.build_interpret_prose_user(
        strategy, section_names, render, _outcome_line(traj))
    if feedback.strip():
        prose_user += (
            "\n\n=== REVISION REQUIRED ===\n"
            "A previous reading of this trajectory failed the altitude screen. "
            "Write a fresh reading that fully addresses this feedback while "
            "keeping every judgement grounded in cited turns:\n"
            + feedback.strip())
    try:
        prose, _usage = optimizer_client.complete_optimizer(
            prompts.INTERPRET_PROSE_SYSTEM, prose_user,
            max_tokens=_PROSE_MAX_TOKENS)
    except Exception:  # noqa: BLE001 — one bad call must not sink the batch
        _log.exception("materials/interpret — prose pass failed for %s",
                       traj.traj_id)
        prose = ""
    prose = (prose or "").strip()
    if not prose:
        record = dict(meta)
        record["interp"] = {"narrative": "", "outcome_causality": "",
                            "anomalies": "", "behavior_signature": "",
                            "adherence": [],
                            "_error": "prose interpretation produced no text"}
        record["interp_prose"] = ""
        common.write_json_atomic(path, record)
        return record

    # ── Pass 2 — structured extraction over the PROSE (small call) ─────────
    extract_user = prompts.build_interpret_extract_user(prose, section_names)
    extracted = common.run_json_stage(
        optimizer_client, prompts.INTERPRET_EXTRACT_SYSTEM, extract_user,
        parse=common.parse_object, stage="interp_extract", cfg=cfg,
        required=_extract_required,
        ok=lambda r: isinstance(r, dict) and bool(r),
    )

    interp = _split_prose(prose)
    if isinstance(extracted, dict) and extracted:
        interp["behavior_signature"] = str(
            extracted.get("behavior_signature", "") or "").strip()
        adherence, dropped = _clean_adherence(
            extracted.get("adherence"), section_names)
        interp["adherence"] = adherence
        if dropped:
            interp["_extract_warnings"] = (
                "%d adherence entr(ies) dropped (section not in the closed "
                "set or invalid verdict)" % dropped)
            _log.info("materials/interpret — %s: %d adherence entries dropped "
                      "by echo validation", traj.traj_id, dropped)
    else:
        interp["behavior_signature"] = ""
        interp["adherence"] = []
        interp["_extract_error"] = "structured extraction failed after retry"
        _log.warning("materials/interpret — %s: extract pass failed; prose "
                     "fields survive, structured fields empty", traj.traj_id)

    record = dict(meta)
    record["interp"] = interp
    record["interp_prose"] = prose
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


def reinterpret_one(
    exploit_dir: str, idir: str, traj_id: str, strategy: str, feedback: str,
    optimizer_client: Any, cfg: "CSSConfig",
) -> "dict | None":
    """Re-produce ONE trajectory's interpretation with screen feedback.

    The screen-with-regeneration route (decision log #14): reload the
    trajectory from disk, re-run the two-pass interpretation with the critique
    appended, overwrite the persisted product, and return the fresh record —
    or ``None`` when the trajectory can no longer be loaded.
    """
    trajs = common.load_burst_trajectories(exploit_dir)
    match = next((t for t in trajs if t.traj_id == traj_id), None)
    if match is None:
        _log.warning("materials/interpret — reinterpret: trajectory %s not "
                     "found under %s", traj_id, exploit_dir)
        return None
    path = os.path.join(idir, f"traj_{traj_id}.json")
    return _interpret_one(match, strategy, path, optimizer_client, cfg,
                          feedback=feedback, force=True)
