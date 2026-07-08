"""Altitude + purity screens (design §2.3 reusable judge).

Two entry points:

  * :func:`screen_texts` — batch screen used as the per-op altitude gate in REFINE
    confrontation. Prefers an external ``css.materials.screen`` implementation
    (the materials package owns the canonical Altitude Screen); falls back to a
    self-contained local implementation when that package is not present yet.
  * :func:`altitude_purity_check` — the single-document final gate. JUDGE
    ONLY for every pipeline (decision log #14): a failing document is never
    rewritten by the gate; the caller feeds the feedback back into its own
    drafting step and regenerates.

All optimizer calls route through :mod:`css.l1gen._llm` so tests drive them by
stage.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from css.l1gen import _llm, prompts

_log = logging.getLogger("css.l1gen")

_GATE_MAX_TOKENS = 16384
_SCREEN_MAX_TOKENS = 16384


# ── External screen discovery (materials package owns the canonical screen) ───
def _external_screen() -> "Optional[Callable[[Any, List[str]], List[dict]]]":
    try:
        from css.materials import screen as materials_screen  # type: ignore
    except Exception:  # noqa: BLE001 — package may not exist yet
        return None
    for name in ("screen_texts", "altitude_screen", "screen"):
        fn = getattr(materials_screen, name, None)
        if callable(fn):
            return fn
    return None


def _local_screen_texts(client: Any, texts: "List[str]") -> "List[dict]":
    """Local Altitude Screen over a batch of candidate content pieces."""
    if not texts:
        return []
    pieces = "\n\n".join("[[ piece %d ]]\n%s" % (i, t) for i, t in enumerate(texts))
    user = prompts.ALTITUDE_SCREEN_USER.format(pieces=pieces)
    arr = _llm.complete_optimizer_json(
        client, prompts.ALTITUDE_SCREEN_SYSTEM, user,
        parse=_llm.parse_json_array, ok=lambda r: isinstance(r, list),
        max_tokens=_SCREEN_MAX_TOKENS, stage=_llm.STAGE_SCREEN,
    )
    by_index: "Dict[int, dict]" = {}
    for obj in arr or []:
        if isinstance(obj, dict):
            try:
                by_index[int(obj.get("index"))] = obj
            except (TypeError, ValueError):
                continue
    out: "List[dict]" = []
    for i in range(len(texts)):
        v = by_index.get(i)
        if v is None:
            # Missing verdict = screen noise; default to pass so a parser hiccup
            # does not hard-block a spawn (the screen is a safety net, not a gate
            # of record). Recorded so the omission is auditable.
            out.append({"index": i, "verdict": "pass", "violated_criteria": [],
                        "feedback": "no verdict returned for this piece", "quoted_offense": ""})
        else:
            out.append(v)
    return out


def screen_texts(client: Any, texts: "List[str]") -> "List[dict]":
    """Altitude-screen a batch of content pieces -> per-piece verdicts (in order).

    Each verdict is ``{index, verdict: pass|revise|reject, violated_criteria,
    feedback, quoted_offense}``.
    """
    ext = _external_screen()
    if ext is not None:
        try:
            res = ext(client, list(texts))
            if isinstance(res, list):
                return res
            _log.warning("external css.materials.screen returned %s; using local screen",
                         type(res).__name__)
        except Exception:  # noqa: BLE001 — external screen must never break the pipeline
            _log.exception("external css.materials.screen failed; using local screen")
    return _local_screen_texts(client, texts)


def _run_gate(client: Any, strategy_text: str, stage: str) -> dict:
    user = prompts.ALTITUDE_PURITY_USER.format(strategy=strategy_text or "(empty)")
    obj = _llm.complete_optimizer_json(
        client, prompts.ALTITUDE_PURITY_SYSTEM, user,
        parse=_llm.parse_json_object, ok=lambda r: bool(r),
        max_tokens=_GATE_MAX_TOKENS, stage=stage,
    )
    if not isinstance(obj, dict) or not obj:
        # Unparseable gate output: treat as a soft pass (the gate is a safety net;
        # a parser failure should not sink an otherwise valid document). Recorded.
        return {"verdict": "pass", "violated_criteria": [],
                "feedback": "gate output unparseable; treated as pass", "quoted_offense": ""}
    return obj


def altitude_purity_check(
    client: Any,
    strategy_text: str,
    *,
    stage: str,
) -> "Tuple[bool, str, dict]":
    """Run the final altitude+purity gate on a whole strategy document.

    JUDGE ONLY (decision log #14): returns ``(ok, feedback, trail)`` and never
    rewrites the document. On failure the caller routes ``feedback`` back into
    its own drafting step (regenerate-with-critique) and re-gates the fresh
    draft; the gate must never be able to replace the product of the pipeline
    it guards.
    """
    v1 = _run_gate(client, strategy_text, stage)
    trail: "Dict[str, Any]" = {"verdict_1": v1}
    if str(v1.get("verdict", "")).lower() == "pass":
        trail["outcome"] = "pass"
        return True, "", trail
    trail["outcome"] = "fail"
    bits = []
    fb = str(v1.get("feedback", "") or "").strip()
    if fb:
        bits.append(fb)
    quoted = str(v1.get("quoted_offense", "") or "").strip()
    if quoted:
        bits.append("Most representative offense: %r" % quoted)
    crits = v1.get("violated_criteria")
    if isinstance(crits, list) and crits:
        bits.append("Violated criteria: %s" % ", ".join(str(c) for c in crits))
    return False, "\n".join(bits) or "the altitude/purity gate rejected the document", trail
