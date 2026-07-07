"""Public contract of the exploration subsystem for the generation package.

``get_or_explore`` is the one entry point the NEW/REFINE spawners call before
producing a child: it returns cached findings for a task group when they are fresh,
otherwise runs (or resumes) a director session, distills the group's session
reports into screened findings, caches them, and returns the markdown. It NEVER
raises and returns ``""`` on total failure.

Artifacts live under ``<out_dir>/global/exploration/``:
  * ``sessions/session_<t>/`` — the full per-session archive;
  * ``<group_key>/findings.md`` (+ ``findings.meta.json``) — the cached, screened
    cross-session findings for a group.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

from css.explore._util import (
    atomic_write_json,
    atomic_write_text,
    read_json,
    read_text,
    safe_name,
)
from css.explore.director import run_director_session
from css.explore.findings import build_findings

_log = logging.getLogger("css.explore")


def _exploration_root(out_dir: str) -> str:
    return os.path.join(out_dir, "global", "exploration")


def _group_dir(out_dir: str, group_key: str) -> str:
    return os.path.join(_exploration_root(out_dir), safe_name(group_key))


def _sessions_root(out_dir: str) -> str:
    return os.path.join(_exploration_root(out_dir), "sessions")


def _findings_paths(out_dir: str, group_key: str) -> "tuple[str, str]":
    gd = _group_dir(out_dir, group_key)
    return os.path.join(gd, "findings.md"), os.path.join(gd, "findings.meta.json")


def _ledger_signature(out_dir: str, cfg=None) -> str:
    """Current coverage-ledger partition signature ('' when unavailable).

    Uses cfg.ledger_min_attempts: the signature is m-sensitive (attempted
    thresholds), and a default-m signature would track a DIFFERENT partition
    than the one driving NEW targeting the moment m > 1 (code-review
    2026-07-07).
    """
    try:
        from css.coverage import load_coverage
        led = load_coverage(out_dir, min_attempts=int(
            getattr(cfg, "ledger_min_attempts", 1) or 1))
        return led.signature() if led.has_data() else ""
    except Exception:  # noqa: BLE001 — exploration must run without the ledger
        return ""


def _cached_findings(out_dir: str, group_key: str,
                     current_sig: str = "") -> "str | None":
    """Return fresh cached findings for a group, or ``None`` to (re)explore.

    Freshness = not marked stale AND the coverage partition is unchanged since
    the findings were distilled (L1_actions_redesign §4: a task flipping
    solved/unsolved is exactly the evidence a new session should see; attempts
    piling up inside a state do not re-trigger).
    """
    findings_path, meta_path = _findings_paths(out_dir, group_key)
    if not os.path.exists(findings_path):
        return None
    meta = read_json(meta_path) or {}
    if meta.get("stale"):
        return None
    cached_sig = str(meta.get("ledger_signature", "") or "")
    if cached_sig:
        current = current_sig
        if current and current != cached_sig:
            _log.info("[explore] findings for group=%r stale by ledger "
                      "signature (%s -> %s)", group_key, cached_sig, current)
            return None
    text = read_text(findings_path)
    return text if text.strip() else None


def _session_dir(out_dir: str, decision_index: int, group_key: str) -> str:
    """Resolve this exploration's session dir (design ``session_<t>``).

    The normal path is ``session_<decision_index>`` (one exploration per tree
    decision, so it is collision-free). If a finished session already sits there
    for a DIFFERENT group (e.g. repeated default indices in a harness), a
    group-suffixed sibling is used so no group overwrites another's archive.
    """
    root = _sessions_root(out_dir)
    base = os.path.join(root, "session_%04d" % decision_index)
    existing = read_json(os.path.join(base, "session_meta.json"))
    if existing and existing.get("group_key") not in (None, group_key):
        return base + "__" + safe_name(group_key)
    return base


def _session_reports_for_group(out_dir: str, group_key: str) -> "list[tuple[str, str]]":
    """All finished session reports for ``group_key`` as ``(session_name, report)``.

    Scans every session archive and keeps those whose meta names this group and
    whose report is non-empty. Order is by session directory name (stable).
    """
    root = _sessions_root(out_dir)
    out: "list[tuple[str, str]]" = []
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root)):
        sdir = os.path.join(root, name)
        meta = read_json(os.path.join(sdir, "session_meta.json"))
        if not meta or meta.get("group_key") != group_key:
            continue
        report = read_text(os.path.join(sdir, "report.md"))
        if report.strip():
            out.append((name, report))
    return out


def get_or_explore(
    group_key: str,
    *,
    group_tasks: list,
    neighbor_tasks: list,
    briefing_md: str,
    mode: str,
    env,
    target_client,
    optimizer_client,
    cfg,
    out_dir: str,
    decision_index: int = 0,
) -> str:
    """Return findings markdown for ``group_key`` (cached or freshly explored).

    Reuses cached findings unless they are marked stale. On a cache miss, runs (or
    resumes) a director session for this group, distills the group's session
    reports into double-screened findings, caches them, and returns them. Never
    raises; returns ``""`` on total failure.
    """
    try:
        # One signature computation serves both the freshness check and the
        # meta write below (it was recomputed twice per session before).
        current_sig = _ledger_signature(out_dir, cfg)
        cached = _cached_findings(out_dir, group_key, current_sig)
        if cached is not None:
            _log.info("[explore] cache hit for group=%r (%d chars)", group_key, len(cached))
            return cached

        session_dir = _session_dir(out_dir, decision_index, group_key)
        report_path = os.path.join(session_dir, "report.md")
        existing_meta = read_json(os.path.join(session_dir, "session_meta.json"))
        # Stage-level resume: a finished session for THIS group is not re-run.
        finished = (
            existing_meta is not None
            and existing_meta.get("group_key") == group_key
            and read_text(report_path).strip() != ""
        )
        if finished:
            _log.info("[explore] resuming: session %s already finished",
                      os.path.basename(session_dir))
        else:
            history_texts = []
            extra = getattr(cfg, "extra", None) or {}
            if isinstance(extra, dict):
                history_texts = extra.get("explore_history_texts", []) or []
            from css.explore.leads import leads_path as _leads_path
            run_director_session(
                group_key=group_key, mode=mode,
                group_tasks=list(group_tasks or []),
                neighbor_tasks=list(neighbor_tasks or []),
                briefing_md=briefing_md or "",
                env=env, target_client=target_client, optimizer_client=optimizer_client,
                cfg=cfg, session_dir=session_dir, decision_index=decision_index,
                history_texts=history_texts,
                leads_path=_leads_path(out_dir),
            )

        reports = _session_reports_for_group(out_dir, group_key)
        if not reports:
            _log.warning("[explore] no session reports for group=%r; returning empty",
                         group_key)
            return ""

        findings = build_findings(
            [r for _, r in reports], group_key=group_key,
            optimizer_client=optimizer_client, cfg=cfg,
        )
        if not findings.strip():
            _log.warning("[explore] distillation produced empty findings for group=%r",
                         group_key)
            return ""

        findings_path, meta_path = _findings_paths(out_dir, group_key)
        atomic_write_text(findings_path, findings)
        atomic_write_json(meta_path, {
            "group_key": group_key,
            "created_decision": decision_index,
            "created_ts": time.time(),
            "source_sessions": [name for name, _ in reports],
            "stale": False,
            "ledger_signature": current_sig,
        })
        _log.info("[explore] cached findings for group=%r (%d chars, %d session(s))",
                  group_key, len(findings), len(reports))
        return findings
    except Exception:  # noqa: BLE001 — exploration must never break the caller
        _log.exception("[explore] get_or_explore failed for group=%r (returning empty)",
                       group_key)
        return ""


def mark_stale(group_key: str, out_dir: str, reason: str = "") -> None:
    """Mark a group's cached findings stale so the next call re-explores.

    Used when the group's failure mode changed (design §3.1). A no-op when no
    findings are cached (the next call already re-explores on a cache miss); when
    findings exist their meta gets ``stale: true`` + the reason.
    """
    findings_path, meta_path = _findings_paths(out_dir, group_key)
    if not os.path.exists(findings_path):
        return
    meta = read_json(meta_path) or {"group_key": group_key}
    meta["stale"] = True
    meta["stale_reason"] = reason
    meta["stale_ts"] = time.time()
    try:
        atomic_write_json(meta_path, meta)
        _log.info("[explore] marked group=%r stale: %s", group_key, reason)
    except Exception:  # noqa: BLE001
        _log.exception("[explore] mark_stale failed for group=%r", group_key)
