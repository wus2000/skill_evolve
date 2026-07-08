"""Burst-end document consolidation — the metabolism stage.

Design: docs/L0_document_metabolism.md §3. The rules document grows through
many per-step accepted edits, each reasonable alone; the accumulated result
is instance pile-up, cross-section duplication, theme splits, structural
debris, and stale audience/leakage residue (measured live: AW 15K->88K,
SS 17K->112K over ~11 steps with ZERO score gain past the early best). This
stage is the single point in the mechanism holding all three missing
authorities at once: whole-document VISION, subtractive AUTHORIZATION, and
BACKLOG re-review.

Shape (user rulings 2026-07-08):
  * runs once per burst, after exploitation and the val refresh, before the
    skill snapshot — the tidied document is what the tree inherits;
  * ONE whole-document LLM call producing PER-SECTION decisions
    (keep / rewrite / merge / delete); "keep" carries no body, so the output
    budget is spent only on actual changes;
  * mechanical completeness backstop: every input handle appears in exactly
    one decision (protocol repair, then a half-split retry on failure —
    losing cross-half merges but never the tidy-up);
  * lossless backstop: backtick identifiers of the input document must
    survive in the output or be declared in dropped_facts — one repair
    attempt, then the consolidation is ABANDONED (the document is never
    silently degraded);
  * acceptance = symmetric-fresh NON-INFERIORITY gate
    (:func:`css.evaluation.paired_gate.run_noninferiority_gate`): binary
    layer n_lost <= n_gained AND continuous layer
    cand_mean >= inc_mean - consolidation_margin. Reject keeps the old
    document untouched and does NOT retry this burst;
  * on accept: node.rules = node.best_rules = the tidied document,
    node.best_score = node.val_score = its fresh measured mean (honest
    bookkeeping — the old (score, rules) pair is archived in the audit),
    val ledger reset inside the gate.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

from css.optimizer.editpipe3 import prompts
from css.optimizer.editpipe3.docmodel import RulesDocV3
from css.optimizer.editpipe3.pipeline import (
    _call_json,
    _code_identifiers,
    _protocol_repair,
    _strip_handle_title,
    _top_bullet_count,
)

_log = logging.getLogger("css")

_CONSOLIDATE_MAX_TOKENS = 16384
_PLAN_OPS = ("keep", "rewrite", "merge", "delete")
_MIN_SECTIONS = 3        # below this there is nothing worth tidying


@dataclass
class ConsolidationOutcome:
    ran: bool
    accepted: bool = False
    reason: str = ""
    chars_before: int = 0
    chars_after: int = 0
    sections_before: int = 0
    sections_after: int = 0
    cand_mean: float = 0.0
    inc_mean: float = 0.0

    def to_dict(self) -> dict:
        return {"ran": self.ran, "accepted": self.accepted,
                "reason": self.reason,
                "chars_before": self.chars_before,
                "chars_after": self.chars_after,
                "sections_before": self.sections_before,
                "sections_after": self.sections_after,
                "cand_mean": self.cand_mean, "inc_mean": self.inc_mean}


# ── plan validation / execution (rule code owns structure) ───────────────────
def _check_plan(obj: Any, doc: RulesDocV3,
                scope: "list[str]") -> "list[str]":
    """Mechanical completeness check: every scope handle in EXACTLY one
    decision; rewrite/merge carry title+body; delete carries a reason."""
    violations: "list[str]" = []
    if not isinstance(obj, dict) or not isinstance(obj.get("sections"), list):
        return ["the output must be an object with a \"sections\" list"]
    seen: "dict[str, int]" = {}
    scope_set = set(scope)
    for si, s in enumerate(obj["sections"]):
        if not isinstance(s, dict):
            violations.append("sections[%d] is not an object" % si)
            continue
        op = str(s.get("op", "")).strip()
        if op not in _PLAN_OPS:
            violations.append("sections[%d].op %r is not one of %s"
                              % (si, op, ", ".join(_PLAN_OPS)))
            continue
        handles = [str(h).strip() for h in (s.get("handles") or [])]
        if not handles:
            violations.append("sections[%d] has no handles" % si)
            continue
        for h in handles:
            if doc.resolve(h) is None or h not in scope_set:
                violations.append(
                    "sections[%d] references handle %r outside this call's "
                    "scope" % (si, h))
            seen[h] = seen.get(h, 0) + 1
        if op in ("rewrite", "merge"):
            if not str(s.get("title", "") or "").strip():
                violations.append("sections[%d] (%s) lacks a title"
                                  % (si, op))
            if not str(s.get("body", "") or "").strip():
                violations.append("sections[%d] (%s) lacks a body"
                                  % (si, op))
        if op == "delete" and not str(s.get("reason", "") or "").strip():
            violations.append("sections[%d] (delete) lacks a reason" % si)
    dupes = sorted(h for h, n in seen.items() if n > 1)
    if dupes:
        violations.append("handles appear in MORE than one decision: %s"
                          % ", ".join(dupes))
    missing = sorted(set(scope_set) - set(seen), key=lambda h: int(h[2:]))
    if missing:
        violations.append(
            "handles appear in NO decision (every section must be kept, "
            "rewritten, merged, or deleted): %s" % ", ".join(missing))
    return violations


def _plan_call(client: Any, doc: RulesDocV3, size_note: str,
               scope: "list[str]", audit: "list[dict]") -> "Optional[dict]":
    """One consolidation plan call over ``scope`` + protocol repair."""
    scope_note = ""
    if len(scope) < len(doc.sections):
        scope_note = ("\nDecide ONLY for these handles: %s. Every other "
                      "section is kept as is by the system (do not list it)."
                      % ", ".join(scope))
    user = prompts.build_consolidate_user(doc.render(), size_note + scope_note)
    obj = _call_json(client, prompts.CONSOLIDATE_SYSTEM, user,
                     ok=lambda r: isinstance(r, dict) and "sections" in r,
                     stage="ep3_consolidate",
                     max_tokens=_CONSOLIDATE_MAX_TOKENS)
    violations = _check_plan(obj, doc, scope)
    if not violations:
        return obj
    repaired = _protocol_repair(
        client, "Tidy up a rules document via per-section decisions.",
        "sections: [{op: keep|rewrite|merge|delete, handles, title?, body?, "
        "reason?}]; every in-scope handle in exactly one decision.",
        obj, violations,
        ok=lambda r: isinstance(r, dict) and "sections" in r,
        stage="ep3_consolidate")
    if repaired is not None and not _check_plan(repaired, doc, scope):
        audit.append({"stage": "plan", "action": "protocol_repaired",
                      "violations": violations})
        return repaired
    audit.append({"stage": "plan", "action": "plan_failed",
                  "scope": len(scope), "violations": violations})
    return None


def _make_plan(client: Any, doc: RulesDocV3, size_note: str,
               audit: "list[dict]") -> "Optional[dict]":
    """Whole-document plan; on failure fall back to two half-scope calls.

    Returns ``{"sections": [...], "dropped_facts": [...]}`` or ``None``. The
    half-split self-heal keeps the tidy-up available when one full-output
    call cannot carry all changed bodies (output-budget truncation shows up
    as an unparseable/incomplete plan). Cross-half merges are lost in the
    fallback — an accepted cost; the next burst can still perform them.
    """
    all_handles = [doc.handle(i) for i in range(len(doc.sections))]
    obj = _plan_call(client, doc, size_note, all_handles, audit)
    if obj is not None:
        return {"sections": [s for s in obj["sections"] if isinstance(s, dict)],
                "dropped_facts": list(obj.get("dropped_facts") or [])}
    mid = len(all_handles) // 2
    halves = [all_handles[:mid], all_handles[mid:]]
    merged: "list[dict]" = []
    dropped: "list" = []
    for half in halves:
        if not half:
            continue
        obj = _plan_call(client, doc, size_note, half, audit)
        if obj is None:
            audit.append({"stage": "plan", "action": "half_split_failed"})
            return None
        merged.extend(s for s in obj["sections"] if isinstance(s, dict))
        dropped.extend(obj.get("dropped_facts") or [])
    audit.append({"stage": "plan", "action": "half_split_used"})
    return {"sections": merged, "dropped_facts": dropped}


def _build_output(doc: RulesDocV3, plan: "list[dict]",
                  audit: "list[dict]") -> str:
    """Deterministic execution of the per-section plan (order = plan order)."""
    out = RulesDocV3(preamble=doc.preamble)
    for s in plan:
        op = str(s.get("op", "")).strip()
        idxs = [doc.resolve(str(h).strip()) for h in (s.get("handles") or [])]
        idxs = [i for i in idxs if i is not None]
        if not idxs:
            continue
        if op == "keep":
            for i in idxs:
                out.add_section(doc.sections[i].title, doc.sections[i].body)
        elif op in ("rewrite", "merge"):
            title = _strip_handle_title(str(s.get("title", "") or ""))
            out.add_section(title, str(s.get("body", "") or ""))
            audit.append({"stage": "apply", "action": op, "title": title,
                          "old": [{"title": doc.sections[i].title,
                                   "body": doc.sections[i].body}
                                  for i in idxs]})
        elif op == "delete":
            audit.append({"stage": "apply", "action": "delete",
                          "reason": str(s.get("reason", "") or ""),
                          "old": [{"title": doc.sections[i].title,
                                   "body": doc.sections[i].body}
                                  for i in idxs]})
    return out.serialize()


def _doc_lost_identifiers(old_md: str, new_md: str,
                          dropped_facts: "list") -> "list[str]":
    declared = json.dumps(dropped_facts or [], ensure_ascii=False)
    return [i for i in sorted(_code_identifiers(old_md))
            if i not in new_md and i not in declared]


def _size_note(doc: RulesDocV3, bullet_budget: int) -> str:
    over = ["[%s] %s (%d top-level bullets)"
            % (doc.handle(i), s.title, _top_bullet_count(s.body))
            for i, s in enumerate(doc.sections)
            if bullet_budget > 0 and _top_bullet_count(s.body) > bullet_budget]
    lines = ["%d sections, %d chars total."
             % (len(doc.sections), len(doc.serialize()))]
    if over:
        lines.append("Sections over the %d-bullet budget (prime merge "
                     "candidates):\n%s" % (bullet_budget, "\n".join(over)))
    empties = ["[%s] %s" % (doc.handle(i), s.title)
               for i, s in enumerate(doc.sections) if not s.body.strip()]
    if empties:
        lines.append("Empty sections (structural debris): %s"
                     % ", ".join(empties))
    return "\n".join(lines)


# ── entry ─────────────────────────────────────────────────────────────────────
def run_burst_consolidation(
    node, env, val_items, target_client, optimizer_client, cfg,
    out_dir: str, *, decision_index: int = -1,
) -> ConsolidationOutcome:
    """Consolidate ``node.best_rules`` at a burst boundary (idempotent).

    Mutates ``node`` ONLY on gate accept (rules/best_rules/best_score/
    val_score; the ledger transition happens inside the gate). Every failure
    path leaves the document untouched — abandoning a tidy-up is always
    safe; a rejected or failed consolidation simply waits for the next burst.
    """
    from css.evaluation.paired_gate import run_noninferiority_gate

    gate_path = os.path.join(out_dir, "gate.json")
    out_rules_path = os.path.join(out_dir, "output_rules.md")
    if os.path.exists(gate_path):                       # idempotent replay
        with open(gate_path, encoding="utf-8") as f:
            gd = json.load(f)
        if gd.get("applied") and os.path.exists(out_rules_path):
            with open(out_rules_path, encoding="utf-8") as f:
                tidied = f.read()
            node.rules = tidied
            node.best_rules = tidied
            node.best_score = float(gd.get("cand_mean", node.best_score))
            node.val_score = float(gd.get("cand_mean", node.val_score))
            _log.info("consolidation: replayed accepted checkpoint (%s)",
                      out_dir)
        return ConsolidationOutcome(
            ran=True, accepted=bool(gd.get("applied")),
            reason="replayed checkpoint",
            cand_mean=float(gd.get("cand_mean", 0.0)),
            inc_mean=float(gd.get("inc_mean", 0.0)))

    source_rules = node.best_rules or node.rules or ""
    doc = RulesDocV3.parse(source_rules)
    if len(doc.sections) < _MIN_SECTIONS:
        return ConsolidationOutcome(
            ran=False, reason="document too small (%d section(s))"
            % len(doc.sections))

    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "input_rules.md"), "w",
              encoding="utf-8") as f:
        f.write(source_rules)

    audit: "list[dict]" = []
    bullet_budget = int(getattr(cfg, "l0_section_bullet_budget", 15))
    plan = _make_plan(optimizer_client, doc,
                      _size_note(doc, bullet_budget), audit)
    outcome = ConsolidationOutcome(
        ran=True, chars_before=len(source_rules),
        sections_before=len(doc.sections))

    def _persist(plan_obj, gate_obj) -> None:
        with open(os.path.join(out_dir, "plan.json"), "w",
                  encoding="utf-8") as f:
            json.dump(plan_obj, f, ensure_ascii=False, indent=1)
        with open(os.path.join(out_dir, "audit.json"), "w",
                  encoding="utf-8") as f:
            json.dump(audit, f, ensure_ascii=False, indent=1)
        with open(gate_path, "w", encoding="utf-8") as f:
            json.dump(gate_obj, f, ensure_ascii=False, indent=1)

    if plan is None:
        outcome.reason = "plan failed (document unchanged)"
        _persist(None, {"applied": False, "reason": outcome.reason})
        _log.warning("consolidation: %s", outcome.reason)
        return outcome

    tidied = _build_output(doc, plan["sections"], audit)
    # Lossless check: an identifier "survives" only by being in the OUTPUT
    # document or explicitly declared in dropped_facts. The audit archive of
    # deleted/merged originals is OUR backup, not the model's declaration —
    # it never satisfies this check (a delete carrying a unique identifier
    # that lives nowhere else must abort the tidy-up, by design).
    lost = _doc_lost_identifiers(source_rules, tidied,
                                 plan.get("dropped_facts") or [])
    if lost:
        outcome.reason = ("abandoned: %d identifier(s) would be lost (%s...)"
                          % (len(lost), ", ".join(lost[:5])))
        audit.append({"stage": "lossless", "action": "abandoned",
                      "lost": lost})
        _persist(plan, {"applied": False, "reason": outcome.reason})
        _log.warning("consolidation: %s", outcome.reason)
        return outcome

    outcome.chars_after = len(tidied)
    outcome.sections_after = len(RulesDocV3.parse(tidied).sections)
    with open(out_rules_path, "w", encoding="utf-8") as f:
        f.write(tidied)

    # ── non-inferiority acceptance ────────────────────────────────────────
    gate = run_noninferiority_gate(
        env, node, source_rules, tidied, val_items, target_client, cfg,
        out_dir)
    outcome.cand_mean = gate.cand_mean
    outcome.inc_mean = gate.inc_mean
    outcome.accepted = gate.accept

    gate_obj = dict(gate.to_dict())
    gate_obj.update({
        "applied": bool(gate.accept),
        "margin": float(getattr(cfg, "consolidation_margin", 0.015)),
        "decision_index": decision_index,
        "chars_before": outcome.chars_before,
        "chars_after": outcome.chars_after,
        "archived_best": {"best_score": node.best_score,
                          "best_step": node.best_step},
    })
    _persist(plan, gate_obj)

    if gate.accept:
        node.rules = tidied
        node.best_rules = tidied
        node.best_score = gate.cand_mean
        node.val_score = gate.cand_mean
        outcome.reason = "accepted"
        _log.info(
            "consolidation ACCEPTED: %d -> %d chars (%.0f%%), %d -> %d "
            "sections, mean %.4f (was %.4f)",
            outcome.chars_before, outcome.chars_after,
            100.0 * outcome.chars_after / max(1, outcome.chars_before),
            outcome.sections_before, outcome.sections_after,
            gate.cand_mean, gate.inc_mean)
    else:
        outcome.reason = "gate rejected (document unchanged)"
        _log.info("consolidation rejected by the non-inferiority gate "
                  "(cand %.4f vs inc %.4f) — document unchanged",
                  gate.cand_mean, gate.inc_mean)
    return outcome
