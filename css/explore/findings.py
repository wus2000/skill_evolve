"""Cross-session distillation of exploration reports into cached findings.

One or more session reports for a task group are distilled into a single
findings document (narrative paragraphs first, synthesis last; every claim tied to
its probe evidence; any first-ever-pass probe featured). The document then passes
a DOUBLE SCREEN before it is cached (design §3.6):

  (a) content-purity — findings may cite probe OUTCOMES but must not contain gold
      solutions or task-specific answer content; one LLM screen rewrites/strips
      any violation;
  (b) altitude — findings must be about behavioral approaches and their
      effectiveness, with no step-level corrections; reuses
      ``css.materials.screen.screen_items`` when that module exists, else a local
      single-call altitude screen with the same verdict shape.

Both screens are best-effort: a screen that cannot run leaves the text unchanged
rather than dropping content.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from css.explore.prompts import (
    ALTITUDE_SCREEN_SYSTEM,
    FINDINGS_DISTILL_SYSTEM,
    PURITY_SCREEN_SYSTEM,
)
from css.model.json_repair import complete_optimizer_json

_log = logging.getLogger("css.explore.findings")


# ── Distillation ──────────────────────────────────────────────────────────────
def _distill(session_reports: "list[str]", group_key: str, optimizer_client: Any) -> str:
    parts = ["TASK GROUP: %s" % group_key, ""]
    for i, rep in enumerate(session_reports, 1):
        parts.append("=== SESSION REPORT %d ===" % i)
        parts.append(rep.strip())
        parts.append("")
    user = "\n".join(parts)
    try:
        text, _usage = optimizer_client.complete_optimizer(
            FINDINGS_DISTILL_SYSTEM, user, max_tokens=16384
        )
    except Exception:  # noqa: BLE001 — distillation failure -> no findings
        return ""
    return text or ""


# ── Screens ───────────────────────────────────────────────────────────────────
def _parse_obj(text: str) -> Any:
    if not text:
        return None
    for cand in ([text.strip()] + re.findall(r"\{.*\}", text, re.DOTALL)):
        try:
            obj = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _has_verdict(result: Any) -> bool:
    return isinstance(result, dict) and "verdict" in result


def _screen_rewrite(system: str, findings: str, optimizer_client: Any, stage: str) -> str:
    """Run one judge-and-rewrite screen; return the possibly-rewritten findings.

    On ``revise``/``reject`` with a usable ``rewritten`` field, adopt it; otherwise
    keep the input unchanged (a screen that fails to produce a clean rewrite must
    not silently delete legitimate findings).
    """
    try:
        result = complete_optimizer_json(
            optimizer_client, system, findings,
            parse=_parse_obj, ok=_has_verdict,
            max_tokens=16384, stage=stage,
        )
    except Exception:  # noqa: BLE001
        return findings
    if not isinstance(result, dict):
        return findings
    verdict = str(result.get("verdict", "")).lower()
    if verdict in ("revise", "reject"):
        rewritten = result.get("rewritten")
        if isinstance(rewritten, str) and rewritten.strip():
            _log.info("[explore:%s] screen rewrote findings (verdict=%s)", stage, verdict)
            return rewritten.strip()
    return findings


def _purity_screen(findings: str, optimizer_client: Any) -> str:
    return _screen_rewrite(PURITY_SCREEN_SYSTEM, findings, optimizer_client, "explore_purity")


def _altitude_screen(findings: str, optimizer_client: Any, cfg: Any) -> str:
    """Altitude screen — reuse ``css.materials.screen.screen_items`` if present.

    The materials package lands in a later phase; until then this falls back to a
    local single-call screen with the design's verdict shape
    ({verdict, violated_criteria, feedback, quoted_offense} + a ``rewritten`` field
    so one call both judges and lifts). The reuse hook is guarded against
    signature drift: any failure degrades to the local screen.
    """
    try:
        from css.materials.screen import screen_items  # type: ignore
    except ImportError:
        screen_items = None
    if screen_items is not None:
        try:
            # Assumed contract: screen_items(items, client=..., cfg=..., kind=...)
            # -> list of verdict dicts in the design shape. screen_items judges but
            # does not rewrite, so a non-pass verdict is lifted locally.
            verdicts = screen_items(
                [findings], client=optimizer_client, cfg=cfg, kind="altitude"
            )
            v = verdicts[0] if verdicts and isinstance(verdicts[0], dict) else None
            if v is not None and str(v.get("verdict", "")).lower() == "pass":
                return findings
            if v is not None:
                return _screen_rewrite(
                    ALTITUDE_SCREEN_SYSTEM, findings, optimizer_client, "explore_altitude"
                )
        except Exception:  # noqa: BLE001 — signature drift / runtime error -> local
            _log.info("[explore:altitude] screen_items reuse failed; using local screen")
    return _screen_rewrite(ALTITUDE_SCREEN_SYSTEM, findings, optimizer_client, "explore_altitude")


# ── Public: build findings (distill + double screen) ──────────────────────────
def build_findings(
    session_reports: "list[str]",
    *,
    group_key: str,
    optimizer_client: Any,
    cfg: Any,
) -> str:
    """Distill reports into screened findings markdown (``""`` on empty/failure)."""
    reports = [r for r in (session_reports or []) if r and r.strip()]
    if not reports:
        return ""
    raw = _distill(reports, group_key, optimizer_client)
    if not raw.strip():
        return ""
    cleaned = _purity_screen(raw, optimizer_client)
    lifted = _altitude_screen(cleaned, optimizer_client, cfg)
    return (lifted or "").strip()
