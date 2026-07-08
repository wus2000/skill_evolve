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
    text_tokens,
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


def _filter_to_scope(obj: dict, scope: "list[str]",
                     audit: "list[dict]") -> dict:
    """Tolerant scope filtering (measured failure: the model decides the
    WHOLE document despite a scope instruction).

    Out-of-scope decisions are simply dropped — the other slice owns them. A
    decision straddling the scope boundary (a cross-slice merge) is dropped
    whole and its in-scope handles fall back to keep: lossless (content
    unchanged), and the next burst may still perform that merge."""
    scope_set = set(scope)
    kept_sections: "list[dict]" = []
    fallback_keeps: "list[str]" = []
    n_dropped = 0
    for s in obj.get("sections") or []:
        if not isinstance(s, dict):
            continue
        handles = [str(h).strip() for h in (s.get("handles") or [])]
        inside = [h for h in handles if h in scope_set]
        if not inside:
            n_dropped += 1
            continue
        if len(inside) < len(handles):
            fallback_keeps.extend(inside)
            n_dropped += 1
            continue
        kept_sections.append(s)
    for h in fallback_keeps:
        kept_sections.append({"op": "keep", "handles": [h]})
    if n_dropped or fallback_keeps:
        audit.append({"stage": "plan", "action": "scope_filtered",
                      "dropped_out_of_scope": n_dropped,
                      "straddle_fallback_keeps": fallback_keeps})
    return {"sections": kept_sections,
            "dropped_facts": obj.get("dropped_facts")}


def _plan_call(client: Any, doc: RulesDocV3, size_note: str,
               scope: "list[str]", audit: "list[dict]") -> "Optional[dict]":
    """One consolidation plan call over ``scope`` + protocol repair."""
    scope_note = ""
    if len(scope) < len(doc.sections):
        scope_note = ("\nDecide ONLY for these handles: %s. Every other "
                      "section is handled in a separate call (do not list "
                      "it)." % ", ".join(scope))
    user = prompts.build_consolidate_user(doc.render(), size_note + scope_note)
    obj = _call_json(client, prompts.CONSOLIDATE_SYSTEM, user,
                     ok=lambda r: isinstance(r, dict) and "sections" in r,
                     stage="ep3_consolidate",
                     max_tokens=_CONSOLIDATE_MAX_TOKENS)
    if isinstance(obj, dict):
        obj = _filter_to_scope(obj, scope, audit)
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
    if isinstance(repaired, dict):
        repaired = _filter_to_scope(repaired, scope, audit)
    if repaired is not None and not _check_plan(repaired, doc, scope):
        audit.append({"stage": "plan", "action": "protocol_repaired",
                      "violations": violations})
        return repaired
    audit.append({"stage": "plan", "action": "plan_failed",
                  "scope": len(scope), "violations": violations})
    return None


def _scope_slices(client: Any, doc: RulesDocV3,
                  split_tokens: int) -> "list[list[str]]":
    """Contiguous handle slices sized so each call's rewrite load fits the
    output budget (measured failure: one full call over a 28K-token document
    truncates). Greedy by per-section TOKEN size (user ruling: budgets are
    token-based); always at least one slice."""
    if split_tokens <= 0 or text_tokens(client, doc.serialize()) <= split_tokens:
        return [[doc.handle(i) for i in range(len(doc.sections))]]
    slices: "list[list[str]]" = []
    cur: "list[str]" = []
    cur_tokens = 0
    for i, s in enumerate(doc.sections):
        size = text_tokens(client, s.body) + text_tokens(client, s.title)
        if cur and cur_tokens + size > split_tokens:
            slices.append(cur)
            cur, cur_tokens = [], 0
        cur.append(doc.handle(i))
        cur_tokens += size
    if cur:
        slices.append(cur)
    return slices


def _make_plan(client: Any, doc: RulesDocV3, size_note: str,
               audit: "list[dict]", split_tokens: int = 15000
               ) -> "Optional[dict]":
    """Sliced whole-document plan.

    The document is split into contiguous slices sized to the output budget
    (one slice for documents under ``split_tokens``); each slice gets its own
    plan call over the SAME full-document rendering, deciding only its
    handles. On a slice failure that slice retries once split in half;
    a still-failing slice fails the plan (abandon — next burst retries).
    Cross-slice merges are lost by construction — an accepted cost.
    """
    slices = _scope_slices(client, doc, split_tokens)
    if len(slices) > 1:
        audit.append({"stage": "plan", "action": "sliced",
                      "n_slices": len(slices),
                      "sizes": [len(s) for s in slices]})
    merged: "list[dict]" = []
    dropped: "list" = []
    for scope in slices:
        obj = _plan_call(client, doc, size_note, scope, audit)
        if obj is None and len(scope) > 1:
            mid = len(scope) // 2
            halves = [scope[:mid], scope[mid:]]
            audit.append({"stage": "plan", "action": "slice_half_retry",
                          "scope": len(scope)})
            parts = [_plan_call(client, doc, size_note, h, audit)
                     for h in halves if h]
            if any(p is None for p in parts):
                audit.append({"stage": "plan", "action": "slice_failed"})
                return None
            obj = {"sections": [s for p in parts for s in p["sections"]],
                   "dropped_facts": [x for p in parts
                                     for x in (p.get("dropped_facts") or [])]}
        elif obj is None:
            audit.append({"stage": "plan", "action": "slice_failed"})
            return None
        merged.extend(s for s in obj["sections"] if isinstance(s, dict))
        dropped.extend(obj.get("dropped_facts") or [])
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


def _over_budget_handles(client: Any, doc: RulesDocV3, bullet_budget: int,
                         token_budget: int) -> "list[tuple[str, str, str]]":
    """(handle, title, why) of sections over the curation budget.

    TWO size signals, either marks a MANDATORY target: top-level bullet
    count over ``bullet_budget``, or body TOKENS over ``token_budget``
    (user ruling: budgets are token-based). Measured necessity (AW step11):
    the fattest section (~6.4K tokens) carried its 68 bullets NESTED — only
    13 top-level — so a bullet-only trigger missed the single biggest bloat
    carrier."""
    out = []
    for i, s in enumerate(doc.sections):
        n = _top_bullet_count(s.body)
        why = []
        if bullet_budget > 0 and n > bullet_budget:
            why.append("%d top-level bullets" % n)
        if token_budget > 0:
            n_tok = text_tokens(client, s.body)
            if n_tok > token_budget:
                why.append("%d tokens" % n_tok)
        if why:
            out.append((doc.handle(i), s.title, ", ".join(why)))
    return out


def _size_note(client: Any, doc: RulesDocV3, bullet_budget: int,
               token_budget: int) -> str:
    over = ["[%s] %s (%s)" % (h, t, why)
            for h, t, why in
            _over_budget_handles(client, doc, bullet_budget, token_budget)]
    lines = ["%d sections, %d tokens total."
             % (len(doc.sections), text_tokens(client, doc.serialize()))]
    if over:
        lines.append("MANDATORY targets — over the size budget (%d "
                     "top-level bullets or %d tokens per section); each must "
                     "appear in a rewrite or merge decision, never in "
                     "keep:\n%s" % (bullet_budget, token_budget,
                                    "\n".join(over)))
    empties = ["[%s] %s" % (doc.handle(i), s.title)
               for i, s in enumerate(doc.sections) if not s.body.strip()]
    if empties:
        lines.append("Empty sections (structural debris): %s"
                     % ", ".join(empties))
    return "\n".join(lines)


def _kept_over_budget(client: Any, plan_sections: "list[dict]",
                      doc: RulesDocV3, bullet_budget: int,
                      token_budget: int) -> "list[tuple[str, str]]":
    """(handle, description) of mandatory targets the plan left as 'keep'."""
    over = {h: "[%s] %s (%s)" % (h, t, why)
            for h, t, why in
            _over_budget_handles(client, doc, bullet_budget, token_budget)}
    kept: "list[tuple[str, str]]" = []
    for s in plan_sections:
        if str(s.get("op", "")).strip() != "keep":
            continue
        for h in (s.get("handles") or []):
            h = str(h).strip()
            if h in over:
                kept.append((h, over[h]))
    return kept


def _quality_repair(client: Any, doc: RulesDocV3, size_note: str,
                    plan: dict, lost: "list[str]",
                    kept_over: "list[tuple[str, str]]",
                    audit: "list[dict]") -> "Optional[dict]":
    """ONE semantic repair round naming the losses / kept mandatory targets.

    Protocol repair cannot do this (it must not alter semantic content);
    this mirrors the applier's lossless-repair pattern. SCOPED to the
    problem entries only — re-deciding the whole plan in one output was
    measured to truncate on large documents; entries without a defect stand
    as previously planned. Returns the merged plan dict or ``None``."""
    lost_handles: "set[str]" = set()
    for i, s in enumerate(doc.sections):
        if any(ident in s.body or ident in s.title for ident in lost):
            lost_handles.add(doc.handle(i))
    problem = lost_handles | {h for h, _ in kept_over}
    stand: "list[dict]" = []
    scope: "list[str]" = []
    insert_at = None
    for s in plan["sections"]:
        hs = [str(h).strip() for h in (s.get("handles") or [])]
        if any(h in problem for h in hs):
            if insert_at is None:
                insert_at = len(stand)
            scope.extend(hs)
        else:
            stand.append(s)
    if not scope:
        return None
    scope_note = (size_note
                  + "\nRepair scope: decide ONLY for these handles: %s. "
                    "Every other decision of your previous plan stands "
                    "unchanged (do not list it)." % ", ".join(scope))
    user = prompts.build_consolidate_repair_user(
        doc.render(), scope_note,
        json.dumps(plan, ensure_ascii=False, indent=1),
        lost, [d for _, d in kept_over])
    obj = _call_json(client, prompts.CONSOLIDATE_SYSTEM, user,
                     ok=lambda r: isinstance(r, dict) and "sections" in r,
                     stage="ep3_consolidate_quality_repair",
                     max_tokens=_CONSOLIDATE_MAX_TOKENS)
    if isinstance(obj, dict):
        obj = _filter_to_scope(obj, scope, audit)
    violations = _check_plan(obj, doc, scope)
    if violations:
        audit.append({"stage": "quality_repair",
                      "action": "repair_plan_invalid",
                      "violations": violations})
        return None
    audit.append({"stage": "quality_repair", "action": "repaired",
                  "lost": lost, "kept_over_budget": [d for _, d in kept_over],
                  "scope": scope})
    repaired_entries = [s for s in obj["sections"] if isinstance(s, dict)]
    at = insert_at if insert_at is not None else len(stand)
    merged_sections = stand[:at] + repaired_entries + stand[at:]
    return {"sections": merged_sections,
            "dropped_facts": (list(plan.get("dropped_facts") or [])
                              + list(obj.get("dropped_facts") or []))}


def tidy_document(client: Any, source_rules: str, *, bullet_budget: int,
                  token_budget: int, split_tokens: int,
                  audit: "list[dict]"
                  ) -> "Optional[tuple[dict, str, list, list]]":
    """The complete plan -> build -> quality-repair chain.

    SHARED by the production path (:func:`run_burst_consolidation`) and the
    offline smoke tool — the smoke must exercise exactly what ships. Returns
    ``(plan, tidied_md, lost_identifiers, kept_over_budget)`` or ``None``
    when planning failed entirely.

    Lossless invariant: an identifier "survives" only by being in the OUTPUT
    document or explicitly declared in dropped_facts; the audit archive of
    deleted/merged originals is OUR backup, not the model's declaration, and
    never satisfies the check. ``lost_identifiers`` non-empty means the
    caller must NOT adopt the output; a non-empty ``kept_over_budget`` is a
    depth shortfall only (adoptable, audited).
    """
    doc = RulesDocV3.parse(source_rules)
    note = _size_note(client, doc, bullet_budget, token_budget)
    plan = _make_plan(client, doc, note, audit, split_tokens)
    if plan is None:
        return None
    tidied = _build_output(doc, plan["sections"], audit)
    lost = _doc_lost_identifiers(source_rules, tidied,
                                 plan.get("dropped_facts") or [])
    kept_over = _kept_over_budget(client, plan["sections"], doc,
                                  bullet_budget, token_budget)

    # ONE quality-repair round when the plan lost identifiers or kept a
    # mandatory target (both measured on the first real-document smoke).
    if lost or kept_over:
        repaired = _quality_repair(client, doc, note, plan, lost, kept_over,
                                   audit)
        if repaired is not None:
            re_audit: "list[dict]" = []
            re_tidied = _build_output(doc, repaired["sections"], re_audit)
            re_lost = _doc_lost_identifiers(
                source_rules, re_tidied,
                repaired.get("dropped_facts") or [])
            if not re_lost:
                plan, tidied, lost = repaired, re_tidied, []
                kept_over = _kept_over_budget(client, plan["sections"], doc,
                                              bullet_budget, token_budget)
                audit.extend(re_audit)
            else:
                audit.append({"stage": "quality_repair",
                              "action": "repair_still_lossy",
                              "lost": re_lost})
    return plan, tidied, lost, kept_over


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

    result = tidy_document(
        optimizer_client, source_rules,
        bullet_budget=int(getattr(cfg, "l0_section_bullet_budget", 15)),
        token_budget=int(getattr(cfg, "l0_section_token_budget", 1500)),
        split_tokens=int(getattr(cfg, "consolidation_split_tokens", 15000)),
        audit=audit)
    if result is None:
        outcome.reason = "plan failed (document unchanged)"
        _persist(None, {"applied": False, "reason": outcome.reason})
        _log.warning("consolidation: %s", outcome.reason)
        return outcome
    plan, tidied, lost, kept_over = result

    if lost:
        outcome.reason = ("abandoned: %d identifier(s) would be lost (%s...)"
                          % (len(lost), ", ".join(lost[:5])))
        audit.append({"stage": "lossless", "action": "abandoned",
                      "lost": lost})
        _persist(plan, {"applied": False, "reason": outcome.reason})
        _log.warning("consolidation: %s", outcome.reason)
        return outcome
    if kept_over:
        # Depth shortfall is NOT a loss: accept the shallow (lossless)
        # tidy-up and let the next burst deepen it. Audited, never silent.
        audit.append({"stage": "quality", "action": "depth_shortfall_kept",
                      "kept_over_budget": kept_over})
        _log.info("consolidation: shallow tidy-up (mandatory targets kept: "
                  "%d) — accepted losslessly, next burst deepens",
                  len(kept_over))

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
