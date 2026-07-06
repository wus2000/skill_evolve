"""Layer 3 — living documents with cumulative integration (design §2.2).

behavior_profile.md and frontier_analysis.md are a node's whole-life syntheses:
each burst's products are INTEGRATED into the existing full document — every
prior substantive claim is kept, revised-with-cause, or retired-with-cause, and
nothing silently disappears. An independent no-silent-loss audit compares old vs
new and, if any prior claim lost its disposition, the integration is re-run once
with that as mandatory feedback.

This module owns the reusable integration machinery (:func:`integrate_living_document`,
used here for behavior_profile.md and by :mod:`css.materials.frontier` for
frontier_analysis.md) and the behavior-profile assembly.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable

from css.materials import common, prompts

if TYPE_CHECKING:  # pragma: no cover
    from css.config import CSSConfig
    from css.data.tree import TreeNode
    from css.tree_search import BurstResult

_log = logging.getLogger("css.materials")

_INTEGRATE_MAX_TOKENS = 12288
_AUDIT_MAX_TOKENS = 8192


# ── reusable living-document integration + no-silent-loss audit ──────────────
def _integrate(integrate_system: str, build_user: Callable[[str, str], str],
               old_doc: str, feedback: str, client: Any, cfg: "CSSConfig",
               stage: str) -> str:
    obj = common.run_json_stage(
        client, integrate_system, build_user(old_doc, feedback),
        parse=common.parse_object, stage=f"{stage}_integrate", cfg=cfg,
        ok=lambda r: isinstance(r, dict) and bool(str(r.get("document_md", "")).strip()),
        max_tokens=_INTEGRATE_MAX_TOKENS,
    )
    doc = str((obj or {}).get("document_md", "")).strip()
    # Never lose the document: an unusable integration keeps the prior text.
    return doc or (old_doc or "")


def _audit(old_doc: str, new_doc: str, client: Any, cfg: "CSSConfig", stage: str) -> dict:
    """Independent per-claim ledger; empty when there is no prior document."""
    if not (old_doc and old_doc.strip()):
        return {"ledger": [], "unaccounted": []}
    obj = common.run_json_stage(
        client, prompts.NO_SILENT_LOSS_SYSTEM, prompts.build_audit_user(old_doc, new_doc),
        parse=common.parse_object, stage=f"{stage}_audit", cfg=cfg,
        ok=lambda r: isinstance(r, dict),
        max_tokens=_AUDIT_MAX_TOKENS,
    )
    obj = obj if isinstance(obj, dict) else {}
    ledger = obj.get("ledger", [])
    ledger = ledger if isinstance(ledger, list) else []
    unaccounted = obj.get("unaccounted", [])
    unaccounted = [str(u) for u in unaccounted] if isinstance(unaccounted, list) else []
    # A ledger entry whose disposition is not one of the three is a silent loss.
    for e in ledger:
        if not isinstance(e, dict):
            continue
        disp = str(e.get("disposition", "")).strip().lower()
        if disp not in ("kept", "revised", "retired"):
            claim = str(e.get("old_claim", "")).strip()
            if claim and claim not in unaccounted:
                unaccounted.append(claim)
    return {"ledger": ledger, "unaccounted": unaccounted}


def integrate_living_document(
    *, doc_path: str, diff_path: str, integrate_system: str,
    build_user: Callable[[str, str], str], stage: str,
    optimizer_client: Any, cfg: "CSSConfig",
) -> dict:
    """Integrate this burst into a living document with a no-silent-loss guard.

    ``build_user(old_doc, feedback)`` returns the integration user message (the
    caller bakes in this burst's new evidence). The diff ledger is written to
    ``diff_path``; its presence is the resume marker (skip re-integration).
    Returns ``{"document", "ledger", "reran": bool}``.
    """
    if common.exists(diff_path):
        return {"document": common.read_text(doc_path) or "",
                "ledger": common.read_json(diff_path) or {}, "skipped": True}

    old_doc = common.read_text(doc_path) or ""
    new_doc = _integrate(integrate_system, build_user, old_doc, "",
                         optimizer_client, cfg, stage)
    ledger = _audit(old_doc, new_doc, optimizer_client, cfg, stage)

    reran = False
    if ledger["unaccounted"]:
        _log.info("materials/%s — no-silent-loss audit flagged %d dropped claim(s); "
                  "re-running integration once", stage, len(ledger["unaccounted"]))
        feedback = ("These prior claims disappeared with no stated disposition; you "
                    "MUST keep, revise-with-cause, or retire-with-cause each one:\n- "
                    + "\n- ".join(ledger["unaccounted"]))
        new_doc = _integrate(integrate_system, build_user, old_doc, feedback,
                             optimizer_client, cfg, stage)
        ledger = _audit(old_doc, new_doc, optimizer_client, cfg, stage)
        reran = True
        if ledger["unaccounted"]:
            _log.warning("materials/%s — %d claim(s) still unaccounted after re-run",
                         stage, len(ledger["unaccounted"]))

    common.write_text_atomic(doc_path, new_doc)
    ledger_out = dict(ledger)
    ledger_out["reran"] = reran
    common.write_json_atomic(diff_path, ledger_out)
    return {"document": new_doc, "ledger": ledger_out, "reran": reran}


# ── behavior_profile.md ──────────────────────────────────────────────────────
def _profile_evidence(mining_products: dict) -> str:
    parts = ["### Burst summary\n" + (mining_products.get("burst_summary_md", "") or "").strip()]
    reading = mining_products.get("adherence", {}).get("reading", "")
    if reading and reading.strip():
        parts.append("### Adherence reading\n" + reading.strip())
    claims = []
    for a in mining_products.get("group_analyses", []):
        for c in a.get("distilled_claims", []) or []:
            if not isinstance(c, dict):
                continue
            claims.append(f"- ({a.get('group_key', '?')}) {c.get('claim', '')}  "
                          f"[evidence: {c.get('evidence', '')}]")
    if claims:
        parts.append("### Distilled behavioral claims this burst\n" + "\n".join(claims))
    return "\n\n".join(parts)


def update_behavior_profile(
    node: "TreeNode", burst_result: "BurstResult", mining_products: dict,
    optimizer_client: Any, cfg: "CSSConfig", out_dir: str,
) -> dict:
    """Integrate the burst into behavior_profile.md; return the diff ledger."""
    doc_path = common.dossier_path(out_dir, node.node_id, "behavior_profile.md")
    diff_path = common.analysis_burst_dir(out_dir, node.node_id, burst_result.burst_index)
    diff_path = f"{diff_path}/profile_update_diff.json"
    expected = common.read_text(common.dossier_path(out_dir, node.node_id, "rationale.md")) or ""
    new_evidence = _profile_evidence(mining_products)

    def build_user(old_doc: str, feedback: str) -> str:
        return prompts.build_profile_integrate_user(old_doc, new_evidence, expected, feedback)

    result = integrate_living_document(
        doc_path=doc_path, diff_path=diff_path,
        integrate_system=prompts.PROFILE_INTEGRATE_SYSTEM, build_user=build_user,
        stage="profile", optimizer_client=optimizer_client, cfg=cfg,
    )
    _log.info("materials/profile — node=%s burst=%d integrated (reran=%s)",
              node.node_id, burst_result.burst_index, result.get("reran"))
    return result
