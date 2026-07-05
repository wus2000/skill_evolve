"""REFINE generation pipeline (design §4.2): strategy-level CONTROLLED edits.

A strategy node spawns a REFINE child by editing whole named ``## `` sections of
its parent strategy — the untouched sections stay byte-identical, so the
parent-vs-child diff is exactly the intervention. There is deliberately NO
whole-document rewrite: a whole-paradigm change is the root's NEW job.

Persisted steps (each skipped when its product exists — step-granular resume):

  1. cause confirmation (A/B target + keep-list; U-group explore retry) -> gen/cause_confirmation.json
  2+3+apply. edit plan -> confrontation -> deterministic apply (retry loop)
                                                                     -> gen/edit_plan.json,
                                                                        gen/confrontation.json,
                                                                        gen/applied.json
  4. coherence diff (restricted to edited sections)                    -> gen/coherence_diff.json
  5. rationale + final altitude/purity (no repair)                     -> gen/rationale.json,
                                                                        gen/altitude_check.json
  6. rules inheritance adjudication (keep/drop/rewrite)                -> gen/inherit_decisions.json

SpawnOutcome semantics for REFINE:
  * success                     -> SpawnOutcome(child=<node>, mode="REFINE")
  * no defensible A/B target,    -> SpawnOutcome(child=None, decline=True, reason=...)
    even after exploring U groups   (the honest no-junk-child path: the loop makes the
                                     parent TERMINAL — the AppWorld-94%-node exit).
  * a step produced nothing      -> SpawnOutcome(child=None, decline=False, reason=...)
    usable                          (pipeline failure: decision consumed, parent stays
                                     saturated, retryable).
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, List, Optional, Tuple

from css.data.tree import TreeNode
from css.l1gen import _context, _io, _llm, prompts, screen
from css.l1gen.sections import (
    SectionEditError, apply_coherence_diff, apply_edit_plan, parse_sections,
    sections_byte_identical,
)
from css.tree_search import SpawnContext, SpawnOutcome, dossier_dir, node_dir

_log = logging.getLogger("css.l1gen")

_CAUSE_MAX = 6144
_PLAN_MAX = 10240
_CONFRONT_MAX = 4096
_COHERENCE_MAX = 6144
_RATIONALE_MAX = 4096
_INHERIT_MAX = 8192

_CONTENT_OP_TYPES = frozenset({"replace_section", "add_section", "rewrite_section_for_adherence"})
_TARGET_OP_TYPES = frozenset({"replace_section", "remove_section", "rewrite_section_for_adherence"})


def _n(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def _modified_section_names(ops: "List[dict]") -> "List[str]":
    """Names of sections the plan writes (replace/add/rewrite) — coherence's scope."""
    out: "List[str]" = []
    for op in ops or []:
        if isinstance(op, dict) and str(op.get("op")) in _CONTENT_OP_TYPES:
            name = str(op.get("section", "")).strip()
            if name:
                out.append(name)
    return out


def _render_rationale_md(rationale: dict) -> str:
    tp = str((rationale or {}).get("target_problem", "") or "")
    src = str((rationale or {}).get("idea_sources", "") or "")
    changes = (rationale or {}).get("expected_behavior_changes", []) or []
    if isinstance(changes, str):
        changes = [changes]
    lines = ["# REFINE strategy rationale", "", "## Target problem", tp or "(unspecified)", "",
             "## Idea sources", src or "(unspecified)", "", "## Expected behavior changes"]
    lines += (["- %s" % str(c) for c in changes] if changes else ["(none stated)"])
    return "\n".join(lines) + "\n"


# ── Step 1: cause confirmation ────────────────────────────────────────────────
def _cause_confirm(ctx: SpawnContext, cfg: Any, attr_text: str, findings: str) -> dict:
    out_dir, tree, pid = ctx.out_dir, ctx.tree, ctx.parent.node_id
    user = prompts.REFINE_CAUSE_USER.format(
        node_dossier=_context.node_dossier_block(tree, out_dir, pid, cfg),
        frontier_attribution=attr_text,
        sibling_dossiers=_context.sibling_dossiers(tree, out_dir, pid, cfg),
        findings=findings or "(none)",
    )
    return _llm.complete_optimizer_json(
        ctx.optimizer_client, prompts.REFINE_CAUSE_SYSTEM, user,
        parse=_llm.parse_json_object, ok=lambda r: ("has_target" in r),
        max_tokens=_CAUSE_MAX, stage=_llm.STAGE_REFINE_CAUSE)


# ── Steps 2+3+apply: plan -> confront -> deterministic apply (retry loop) ──────
def _plan_confront_apply(
    ctx: SpawnContext, cfg: Any, gd: str, cause: dict, dead_siblings: str,
) -> "Tuple[Optional[list], Optional[str], Optional[dict], Optional[str]]":
    """Run the bounded plan/confront/apply loop. Returns (ops, applied_text, confront, err)."""
    oc = ctx.optimizer_client
    parent_strategy = ctx.parent.strategy or ""
    keep_list = [str(k) for k in (cause.get("keep_list", []) or [])]
    keep_norm = {_n(k) for k in keep_list}
    retries = int(getattr(cfg, "gen_refine_plan_retries",
                          getattr(cfg, "gen_novelty_retries", 2)))
    keep_block = "\n".join("- %s" % k for k in keep_list) or "(none)"

    critique = ""
    last_confront: dict = {}
    for attempt in range(retries + 1):
        critique_block = ("" if not critique else
                          "\nPRIOR PLAN WAS REJECTED — fix exactly this and try again:\n"
                          + critique + "\n")
        pu = prompts.REFINE_PLAN_USER.format(
            cause=json.dumps(cause, ensure_ascii=False, indent=2),
            keep_list=keep_block, strategy=parent_strategy, critique=critique_block)
        plan = _llm.complete_optimizer_json(
            oc, prompts.REFINE_PLAN_SYSTEM, pu, parse=_llm.parse_json_object,
            ok=lambda r: isinstance(r.get("ops"), list) and bool(r.get("ops")),
            max_tokens=_PLAN_MAX, stage=_llm.STAGE_REFINE_PLAN)
        ops = plan.get("ops", []) if isinstance(plan, dict) else []
        if not ops:
            critique = "the plan contained no ops"
            continue

        # MECHANICAL keep-list check (independent of the critic LLM).
        targets = [str(op.get("section", "")) for op in ops
                   if isinstance(op, dict) and str(op.get("op")) in _TARGET_OP_TYPES]
        kl_hit = sorted({t for t in targets if _n(t) in keep_norm})
        if kl_hit:
            critique = "edit plan targets keep-list section(s): %s" % ", ".join(kl_hit)
            last_confront = {"proceed": False, "reason": critique, "keep_list_violation": kl_hit}
            continue

        # Independent critic + per-op altitude screen.
        cu = prompts.REFINE_CONFRONT_USER.format(
            cause=json.dumps(cause, ensure_ascii=False, indent=2), keep_list=keep_block,
            ops=json.dumps(ops, ensure_ascii=False, indent=2), dead_siblings=dead_siblings)
        confront = _llm.complete_optimizer_json(
            oc, prompts.REFINE_CONFRONT_SYSTEM, cu, parse=_llm.parse_json_object,
            ok=lambda r: ("proceed" in r), max_tokens=_CONFRONT_MAX,
            stage=_llm.STAGE_REFINE_CONFRONT)
        contents = [str(op.get("content", "")) for op in ops
                    if isinstance(op, dict) and str(op.get("op")) in _CONTENT_OP_TYPES]
        verdicts = screen.screen_texts(oc, contents) if contents else []
        bad = [v for v in verdicts if str(v.get("verdict", "")).lower() in ("revise", "reject")]
        confront["altitude_screen"] = verdicts
        last_confront = confront

        if not bool(confront.get("proceed")) or bad:
            parts = []
            if not bool(confront.get("proceed")):
                parts.append("critic: %s" % str(confront.get("reason", "") or "rejected"))
            if bad:
                parts.append("altitude screen flagged ops: %s"
                             % "; ".join(str(v.get("feedback", "")) for v in bad))
            critique = " | ".join(parts)
            continue

        # Deterministic apply (byte-identity of untouched sections is enforced here).
        try:
            applied_text = apply_edit_plan(parent_strategy, ops)
        except SectionEditError as exc:
            critique = "deterministic apply rejected the plan: %s" % exc
            continue
        ok_kl, offending = sections_byte_identical(parent_strategy, applied_text, keep_list)
        if not ok_kl:
            critique = "apply changed keep-list section(s): %s" % ", ".join(offending)
            continue

        _io.write_json_atomic(os.path.join(gd, "edit_plan.json"),
                              {"ops": ops, "attempt": attempt})
        _io.write_json_atomic(os.path.join(gd, "confrontation.json"), confront)
        _io.write_json_atomic(os.path.join(gd, "applied.json"),
                              {"applied_strategy": applied_text, "ops": ops})
        return ops, applied_text, confront, None

    return None, None, last_confront, ("refine.edit_plan rejected after %d retries: %s"
                                       % (retries, critique))


# ── Step 6: rules inheritance adjudication ────────────────────────────────────
def _assemble_rules(decisions: "List[dict]") -> str:
    kept: "List[str]" = []
    for d in decisions or []:
        if not isinstance(d, dict):
            continue
        verdict = str(d.get("verdict", "")).lower()
        if verdict == "keep":
            t = str(d.get("rule_excerpt", "") or "").strip()
        elif verdict == "rewrite":
            t = str(d.get("rewritten", "") or "").strip() or str(d.get("rule_excerpt", "") or "").strip()
        else:  # drop
            t = ""
        if t:
            kept.append(t)
    return ("\n".join(kept).strip() + "\n") if kept else ""


def _inherit_rules(oc: Any, new_strategy: str, parent_rules: str) -> dict:
    """Per-rule three-way inheritance verdict; assemble child rules from keeps+rewrites."""
    if not (parent_rules or "").strip():
        return {"decisions": [], "rules": ""}
    user = prompts.REFINE_INHERIT_USER.format(strategy=new_strategy, rules=parent_rules)
    decisions = _llm.complete_optimizer_json(
        oc, prompts.REFINE_INHERIT_SYSTEM, user, parse=_llm.parse_json_array,
        ok=lambda r: isinstance(r, list), max_tokens=_INHERIT_MAX,
        stage=_llm.STAGE_REFINE_INHERIT)
    if not isinstance(decisions, list) or not decisions:
        # Unparseable / empty -> conservative FULL inherit (never silently drop all
        # rules on optimizer noise; mirrors css.proposal.inheritance).
        _log.warning("refine.inherit: no decisions parsed; conservative full inherit")
        return {"decisions": [], "rules": parent_rules.strip() + "\n", "fallback": "full_inherit"}
    return {"decisions": decisions, "rules": _assemble_rules(decisions)}


# ── Orchestration ─────────────────────────────────────────────────────────────
def run_refine_pipeline(ctx: SpawnContext) -> SpawnOutcome:
    oc = ctx.optimizer_client
    out_dir = ctx.out_dir
    child_id = ctx.new_node_id
    pid = ctx.parent.node_id
    gd = _context.gen_dir(out_dir, child_id)
    _io.ensure_dir(gd)
    cfg = ctx.cfg

    attribution, attr_text = _context.frontier_attribution(out_dir, pid)

    # ── Step 1 — cause confirmation (+ U-group explore retry) ───────────────
    cause_path = os.path.join(gd, "cause_confirmation.json")
    cause = _io.read_json(cause_path)
    if cause is None:
        cause = _cause_confirm(ctx, cfg, attr_text, findings="")
        if not bool(cause.get("has_target")):
            # No defensible A/B target: explore the top escalate-flagged U group,
            # then re-confirm WITH findings before deciding to decline (design §4.2).
            targets = _context.refine_exploration_targets(attribution)
            findings2, explored, source = "", [], "no_u_escalation"
            if targets:
                gk, tids = targets[0]
                gmeta = attribution.get(gk, {}) if isinstance(attribution, dict) else {}
                expl = _context.run_exploration(
                    mode="REFINE", group_key=gk, group_tasks=tids, neighbor_tasks=[],
                    briefing_md=_context.exploration_briefing_refine(
                        ctx.tree, out_dir, cfg, pid, gk, gmeta),
                    cfg=cfg, env=ctx.env, target_client=ctx.target_client, optimizer_client=oc,
                    out_dir=out_dir, decision_index=ctx.decision_index)
                findings2, explored, source = str(expl.get("findings", "") or ""), [gk], expl.get("source", "")
            cause = _cause_confirm(ctx, cfg, attr_text, findings=findings2)
            cause["_explored_u_groups"] = explored
            cause["_exploration_source"] = source
        _io.write_json_atomic(cause_path, cause)

    if not bool(cause.get("has_target")):
        reason = ("refine: no defensible A/B target — %s"
                  % str(cause.get("no_target_reason", "") or "strategy sound at this altitude"))
        _log.info("REFINE decline — node=%s: %s", pid, reason)
        return SpawnOutcome(child=None, mode="REFINE", decline=True, reason=reason,
                            artifacts_dir=gd)

    # ── Steps 2+3+apply — plan / confront / deterministic apply ─────────────
    applied = _io.read_json(os.path.join(gd, "applied.json"))
    if applied is not None:
        ops = applied.get("ops", []) or (_io.read_json(os.path.join(gd, "edit_plan.json")) or {}).get("ops", [])
        applied_text = str(applied.get("applied_strategy", ""))
    else:
        dead = _context.dead_sibling_strategies(ctx.tree, out_dir, pid, cfg)
        ops, applied_text, _confront, err = _plan_confront_apply(ctx, cfg, gd, cause, dead)
        if err is not None:
            return SpawnOutcome(child=None, mode="REFINE", decline=False, reason=err,
                                artifacts_dir=gd)

    # ── Step 4 — coherence diff (restricted to edited sections; verified) ───
    coh_path = os.path.join(gd, "coherence_diff.json")
    coh = _io.read_json(coh_path)
    if coh is None:
        modified = _modified_section_names(ops)
        cu = prompts.REFINE_COHERENCE_USER.format(
            modified_sections=", ".join(modified) or "(none)", strategy=applied_text)
        coh_obj = _llm.complete_optimizer_json(
            oc, prompts.REFINE_COHERENCE_SYSTEM, cu, parse=_llm.parse_json_object,
            ok=lambda r: ("items" in r), max_tokens=_COHERENCE_MAX,
            stage=_llm.STAGE_REFINE_COHERENCE)
        items = coh_obj.get("items", []) if isinstance(coh_obj, dict) else []
        try:
            final_text = apply_coherence_diff(applied_text, items, modified_sections=modified)
        except SectionEditError as exc:
            # Coherence overreached its scope: drop it (the applied text is already
            # valid and keep-list-safe) rather than sink the spawn. Recorded.
            _log.warning("coherence diff rejected (%s); keeping applied text", exc)
            items, final_text = [], applied_text
        coh = {"items": items, "final_strategy": final_text}
        _io.write_json_atomic(coh_path, coh)
    final_text = str(coh.get("final_strategy", applied_text)) or applied_text

    # ── Step 5 — rationale + final altitude/purity (no repair) ──────────────
    rat_path = os.path.join(gd, "rationale.json")
    rationale = _io.read_json(rat_path)
    if rationale is None:
        ru = prompts.REFINE_RATIONALE_USER.format(
            cause=json.dumps(cause, ensure_ascii=False, indent=2),
            ops=json.dumps(ops, ensure_ascii=False, indent=2))
        rationale = _llm.complete_optimizer_json(
            oc, prompts.REFINE_RATIONALE_SYSTEM, ru, parse=_llm.parse_json_object,
            ok=lambda r: bool(r), max_tokens=_RATIONALE_MAX, stage=_llm.STAGE_REFINE_RATIONALE)
        _io.write_json_atomic(rat_path, rationale)

    alt_path = os.path.join(gd, "altitude_check.json")
    alt = _io.read_json(alt_path)
    if alt is None:
        ok, _final, trail = screen.altitude_purity_check(
            oc, final_text, allow_repair=False, stage=_llm.STAGE_REFINE_ALTITUDE)
        alt = {"ok": bool(ok), "trail": trail}
        _io.write_json_atomic(alt_path, alt)
    if not alt.get("ok"):
        return SpawnOutcome(child=None, mode="REFINE", decline=False,
                            reason="refine.altitude/purity check failed", artifacts_dir=gd)

    # ── Step 6 — rules inheritance adjudication ─────────────────────────────
    inh_path = os.path.join(gd, "inherit_decisions.json")
    inh = _io.read_json(inh_path)
    parent_rules = ctx.parent.best_rules or ctx.parent.rules or ""
    if inh is None:
        inh = _inherit_rules(oc, final_text, parent_rules)
        _io.write_json_atomic(inh_path, inh)
    child_rules = str(inh.get("rules", "") or "")

    # Persist the deployable original + rationale; build the child.
    _io.write_text_atomic(os.path.join(node_dir(out_dir, child_id), "strategy.md"), final_text)
    _io.write_text_atomic(os.path.join(dossier_dir(out_dir, child_id), "rationale.md"),
                          _render_rationale_md(rationale))
    child = TreeNode(node_id=child_id, branch_type="REFINE", strategy=final_text, rules=child_rules)
    _log.info("REFINE spawn ready — child=%s parent=%s sections=%d rules_chars=%d",
              child_id, pid, len(parse_sections(final_text)), len(child_rules))
    return SpawnOutcome(child=child, mode="REFINE", artifacts_dir=gd)
