"""Cross-session distillation of exploration reports into cached findings.

One or more session reports for a task group are distilled into a single
findings document (narrative paragraphs first, synthesis last; every claim tied
to its probe evidence; any first-ever-pass probe featured). The document then
passes a DOUBLE SCREEN before it is cached (design §3.6):

  (a) content-purity — findings may cite probe OUTCOMES but must not contain
      gold solutions or task-specific answer content;
  (b) altitude — findings must be about behavioral approaches and their
      effectiveness, with no step-level corrections.

Judge/generator separation (decision log #14): the screens JUDGE ONLY — they
never rewrite the document. A non-pass verdict feeds its feedback back into the
DISTILLATION step as a revision critique (the generating pipeline regenerates,
bounded retries), and the regenerated document is re-screened. When the retries
are exhausted the findings are DISCARDED (honest failure: no cache entry, the
session reports remain archived for audit) rather than adopted with a screen's
own rewrite — a single screen call must never be able to replace the evidence-
grounded product of a whole exploration session.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Tuple

from css.explore.prompts import (
    ALTITUDE_SCREEN_SYSTEM,
    FINDINGS_DISTILL_SYSTEM,
    PURITY_SCREEN_SYSTEM,
)
from css.model.json_repair import complete_optimizer_json

_log = logging.getLogger("css.explore.findings")

# 1 initial distillation + up to 2 critique-driven re-distillations.
_MAX_DISTILL_ATTEMPTS = 3


# ── Distillation ──────────────────────────────────────────────────────────────
def _distill(
    session_reports: "list[str]", group_key: str, optimizer_client: Any,
    critique: str = "",
) -> str:
    parts = ["TASK GROUP: %s" % group_key, ""]
    for i, rep in enumerate(session_reports, 1):
        parts.append("=== SESSION REPORT %d ===" % i)
        parts.append(rep.strip())
        parts.append("")
    if critique.strip():
        parts.append("=== REVISION REQUIRED ===")
        parts.append(
            "A previous distillation of these same reports FAILED screening. "
            "Produce a fresh distillation that fully addresses the screening "
            "feedback below while preserving every legitimate, probe-grounded "
            "finding:")
        parts.append(critique.strip())
        parts.append("")
    user = "\n".join(parts)
    try:
        text, _usage = optimizer_client.complete_optimizer(
            FINDINGS_DISTILL_SYSTEM, user, max_tokens=16384
        )
    except Exception:  # noqa: BLE001 — distillation failure -> no findings
        return ""
    return text or ""


# ── Screens (judge only) ─────────────────────────────────────────────────────
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


def _judge(system: str, findings: str, optimizer_client: Any,
           stage: str) -> "Tuple[bool, str]":
    """Run one judge-only screen; return ``(ok, feedback)``.

    A screen that cannot run or answer is an optimistic pass (the screen is a
    safety net, not the gate of record) — recorded in the log.
    """
    try:
        result = complete_optimizer_json(
            optimizer_client, system, findings,
            parse=_parse_obj, ok=_has_verdict,
            max_tokens=16384, stage=stage,
        )
    except Exception:  # noqa: BLE001
        _log.warning("[explore:%s] screen call failed; optimistic pass", stage)
        return True, ""
    if not isinstance(result, dict):
        _log.warning("[explore:%s] screen unparseable; optimistic pass", stage)
        return True, ""
    if str(result.get("verdict", "")).lower() == "pass":
        return True, ""
    bits = []
    fb = str(result.get("feedback", "") or "").strip()
    if fb:
        bits.append(fb)
    violations = result.get("violations")
    if isinstance(violations, list) and violations:
        bits.append("Offending spans: " + "; ".join(str(v) for v in violations))
    quoted = str(result.get("quoted_offense", "") or "").strip()
    if quoted:
        bits.append("Most representative offense: %r" % quoted)
    return False, "\n".join(bits) or "the screen rejected the document"


def _purity_judge(findings: str, optimizer_client: Any) -> "Tuple[bool, str]":
    return _judge(PURITY_SCREEN_SYSTEM, findings, optimizer_client, "explore_purity")


def _altitude_judge(findings: str, optimizer_client: Any,
                    cfg: Any) -> "Tuple[bool, str]":
    """Altitude judge — reuse ``css.materials.screen.screen_items`` if present.

    The reuse hook only JUDGES; its non-pass feedback is forwarded. Any reuse
    failure degrades to the local judge.
    """
    try:
        from css.materials.screen import screen_items  # type: ignore
    except ImportError:
        screen_items = None
    if screen_items is not None:
        try:
            verdicts = screen_items(
                [findings], optimizer_client, cfg, "explore_findings_altitude"
            )
            v = verdicts[0] if verdicts and isinstance(verdicts[0], dict) else None
            if v is not None:
                if str(v.get("verdict", "")).lower() == "pass":
                    return True, ""
                fb = str(v.get("feedback", "") or "").strip()
                quoted = str(v.get("quoted_offense", "") or "").strip()
                out = fb or "the altitude screen rejected the document"
                if quoted:
                    out += "\nMost representative offense: %r" % quoted
                return False, out
        except Exception:  # noqa: BLE001 — signature drift -> local judge
            _log.info("[explore:altitude] screen_items reuse failed; local judge")
    return _judge(ALTITUDE_SCREEN_SYSTEM, findings, optimizer_client,
                  "explore_altitude")


# ── Public: build findings (distill -> judge -> redistill loop) ──────────────
def build_findings(
    session_reports: "list[str]",
    *,
    group_key: str,
    optimizer_client: Any,
    cfg: Any,
) -> str:
    """Distill reports into double-screened findings (``""`` on failure).

    Non-pass screens feed their feedback into a re-distillation (up to
    ``_MAX_DISTILL_ATTEMPTS`` total attempts). Exhaustion DISCARDS the findings
    — the screens never rewrite content themselves.
    """
    reports = [r for r in (session_reports or []) if r and r.strip()]
    if not reports:
        return ""
    critique = ""
    for attempt in range(1, _MAX_DISTILL_ATTEMPTS + 1):
        raw = _distill(reports, group_key, optimizer_client, critique)
        if not raw.strip():
            return ""
        ok_p, fb_p = _purity_judge(raw, optimizer_client)
        ok_a, fb_a = _altitude_judge(raw, optimizer_client, cfg)
        if ok_p and ok_a:
            if attempt > 1:
                _log.info("[explore:%s] findings passed screening after %d "
                          "distillation attempt(s)", group_key, attempt)
            return raw.strip()
        pieces = []
        if not ok_p:
            pieces.append("CONTENT-PURITY screen:\n" + fb_p)
        if not ok_a:
            pieces.append("ALTITUDE screen:\n" + fb_a)
        critique = "\n\n".join(pieces)
        _log.info("[explore:%s] distillation attempt %d failed screening "
                  "(purity=%s altitude=%s); %s", group_key, attempt,
                  "ok" if ok_p else "FAIL", "ok" if ok_a else "FAIL",
                  "re-distilling with critique"
                  if attempt < _MAX_DISTILL_ATTEMPTS else "DISCARDING")
    _log.warning("[explore:%s] findings failed screening after %d attempts — "
                 "discarded (no cache entry; session reports remain archived)",
                 group_key, _MAX_DISTILL_ATTEMPTS)
    return ""
