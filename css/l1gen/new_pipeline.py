"""NEW generation pipeline (design §4.1): invent a strategy from a blank page.

Only the bare root spawns NEW children, each starting from ZERO rules (the loop
enforces ``rules=""`` regardless). Five persisted steps, each written under
``nodes/<child>/gen/`` before proceeding and skipped when its product already
exists (step-granular resume):

  0. exploration findings (lazy; never fatal)                -> gen/exploration_ref.json
  1. study & target selection                                -> gen/target_selection.json
  2+3. conception + novelty confrontation (retry loop)       -> gen/conception.json,
                                                                gen/novelty_verdict.json
  4. drafting (deployable strategy.md + rationale)           -> gen/draft.json (+ strategy.md,
                                                                dossier/rationale.md)
  5. altitude + purity check (one repair round)              -> gen/altitude_check.json

SpawnOutcome semantics for NEW:
  * success                      -> SpawnOutcome(child=<node>, mode="NEW")
  * novelty exhausted / a step   -> SpawnOutcome(child=None, decline=False, reason=...)
    produced nothing usable         (pipeline failure: decision consumed, parent stays
                                     saturated, retryable). NEW never declines-True — a
                                     root always has a frontier to attack.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, List

from css.data.tree import TreeNode
from css.l1gen import _context, _io, _llm, prompts, screen
from css.l1gen.sections import parse_sections
from css.tree_search import SpawnContext, SpawnOutcome, dossier_dir, node_dir

_log = logging.getLogger("css.l1gen")

_TARGET_MAX = 6144
_CONCEPT_MAX = 6144
_NOVELTY_MAX = 4096
_DRAFT_MAX = 12288


def _render_rationale_md(rationale: dict, *, title: str) -> str:
    tp = str((rationale or {}).get("target_problem", "") or "")
    src = str((rationale or {}).get("idea_sources", "") or "")
    changes = (rationale or {}).get("expected_behavior_changes", []) or []
    if isinstance(changes, str):
        changes = [changes]
    lines = ["# %s" % title, "", "## Target problem", tp or "(unspecified)", "",
             "## Idea sources", src or "(unspecified)", "",
             "## Expected behavior changes"]
    if changes:
        lines += ["- %s" % str(c) for c in changes]
    else:
        lines.append("(none stated)")
    return "\n".join(lines) + "\n"


def run_new_pipeline(ctx: SpawnContext) -> SpawnOutcome:
    oc = ctx.optimizer_client
    out_dir = ctx.out_dir
    child_id = ctx.new_node_id
    gd = _context.gen_dir(out_dir, child_id)
    _io.ensure_dir(gd)
    cfg = ctx.cfg

    # ── Step 0 — exploration (lazy; never fatal) ────────────────────────────
    # NEW explores the top-priority global-unsolved group ahead of design
    # (design §4.1 step 0). Uncharted tasks (never attempted; L1_actions_
    # redesign §4) join the probe menu tagged in the briefing, so the frontier
    # map has no blind spots. If neither exists yet, skip.
    expl_path = os.path.join(gd, "exploration_ref.json")
    exploration = _io.read_json(expl_path)
    if exploration is None:
        target = _context.new_exploration_target(out_dir)
        uncharted = _context.uncharted_task_ids(out_dir)
        if target is None and not uncharted:
            exploration = {"findings": "", "source": "no_global_unsolved_yet"}
        else:
            gk, tids = target if target is not None else ("uncharted_frontier", [])
            groups = _io.read_json(
                os.path.join(out_dir, "global", "unsolved", "groups.json")) or {}
            gmeta = groups.get(gk, {}) if isinstance(groups, dict) else {}
            menu_ids = list(tids) + [t for t in uncharted if t not in set(tids)]
            exploration = _context.run_exploration(
                mode="NEW", group_key=gk, group_tasks=menu_ids, neighbor_tasks=[],
                briefing_md=_context.exploration_briefing_new(
                    ctx.tree, out_dir, cfg, gk, gmeta,
                    extra_task_ids=uncharted, uncharted=uncharted),
                cfg=cfg, env=ctx.env, target_client=ctx.target_client, optimizer_client=oc,
                out_dir=out_dir, decision_index=ctx.decision_index)
            exploration["menu_task_ids"] = menu_ids
        _io.write_json_atomic(expl_path, exploration)
    findings = str(exploration.get("findings", "") or "") or "(no exploration findings)"
    # Probe leads ride along with the findings into every downstream prompt
    # (design §1.2): ideas already found, not yet absorbed by any strategy.
    lb = _context.leads_block(out_dir, list(exploration.get("menu_task_ids") or []))
    if lb:
        findings = findings + "\n\n" + lb

    # ── Step 1 — study & target selection ───────────────────────────────────
    sel_path = os.path.join(gd, "target_selection.json")
    sel = _io.read_json(sel_path)
    if sel is None:
        user = prompts.NEW_TARGET_USER.format(
            node_dossiers=_context.all_node_dossiers(ctx.tree, out_dir, cfg),
            global_unsolved=_context.global_unsolved(out_dir, cfg),
            findings=findings,
        )
        sel = _llm.complete_optimizer_json(
            oc, prompts.NEW_TARGET_SYSTEM, user, parse=_llm.parse_json_object,
            ok=lambda r: bool(r.get("target_group") or r.get("why_all_paradigms_fail")),
            max_tokens=_TARGET_MAX, stage=_llm.STAGE_NEW_TARGET,
        )
        if not (isinstance(sel, dict) and sel.get("target_group")):
            return SpawnOutcome(child=None, mode="NEW", decline=False,
                                reason="new.target_selection produced no target_group",
                                artifacts_dir=gd)
        _io.write_json_atomic(sel_path, sel)

    prior = _context.prior_strategies(ctx.tree, out_dir, cfg)

    # ── Steps 2+3 — conception + novelty confrontation (retry loop) ─────────
    concept_path = os.path.join(gd, "conception.json")
    novelty_path = os.path.join(gd, "novelty_verdict.json")
    concept = _io.read_json(concept_path)
    prior_verdict = _io.read_json(novelty_path)
    if not (isinstance(concept, dict) and concept
            and isinstance(prior_verdict, dict) and prior_verdict.get("novel")):
        concept, verdict, err = _confront_novelty(oc, cfg, sel, prior, findings, novelty_path)
        if err is not None:
            return SpawnOutcome(child=None, mode="NEW", decline=False, reason=err,
                                artifacts_dir=gd)
        _io.write_json_atomic(concept_path, concept)

    # ── Steps 4+5 — drafting + altitude/purity gate (regenerate loop) ───────
    # The gate JUDGES only (decision log #14): a failing draft is never
    # rewritten by the gate; its feedback re-enters DRAFTING as a critique and
    # a fresh draft is re-gated, up to cfg.gen_novelty_retries extra rounds.
    draft_path = os.path.join(gd, "draft.json")
    alt_path = os.path.join(gd, "altitude_check.json")
    draft = _io.read_json(draft_path)
    alt = _io.read_json(alt_path)
    if not (isinstance(draft, dict) and draft
            and isinstance(alt, dict) and alt.get("ok")):
        retries = int(getattr(cfg, "gen_novelty_retries", 2))
        critique = ""
        trails: list = []
        ok = False
        for attempt in range(retries + 1):
            critique_block = ("" if not critique else
                              "\nTHE PREVIOUS DRAFT FAILED THE ALTITUDE/PURITY "
                              "GATE — address exactly this feedback in a fresh "
                              "draft:\n" + critique + "\n")
            user = prompts.DRAFTING_USER.format(
                conception=json.dumps(concept, ensure_ascii=False, indent=2),
                target_selection=json.dumps(sel, ensure_ascii=False, indent=2),
                findings=findings, critique=critique_block,
            )
            draft = _llm.complete_optimizer_json(
                oc, prompts.drafting_system(), user, parse=_llm.parse_json_object,
                ok=lambda r: bool(r.get("strategy_md")),
                max_tokens=_DRAFT_MAX, stage=_llm.STAGE_NEW_DRAFT,
            )
            if not (isinstance(draft, dict) and str(draft.get("strategy_md", "")).strip()):
                return SpawnOutcome(child=None, mode="NEW", decline=False,
                                    reason="new.drafting produced no strategy_md",
                                    artifacts_dir=gd)
            if not parse_sections(str(draft.get("strategy_md", ""))):
                return SpawnOutcome(child=None, mode="NEW", decline=False,
                                    reason="new.drafting produced no '## ' behavioral-mechanism sections",
                                    artifacts_dir=gd)
            ok, feedback, trail = screen.altitude_purity_check(
                oc, str(draft.get("strategy_md", "")), stage=_llm.STAGE_NEW_ALTITUDE)
            trails.append(trail)
            if ok:
                break
            critique = feedback
        alt = {"ok": bool(ok), "trail": trails}
        _io.write_json_atomic(alt_path, alt)
        if not ok:
            return SpawnOutcome(
                child=None, mode="NEW", decline=False,
                reason="new.altitude gate rejected the draft after %d attempt(s)"
                       % len(trails), artifacts_dir=gd)
        _io.write_json_atomic(draft_path, draft)

    strategy_md = str(draft.get("strategy_md", ""))
    rationale = draft.get("rationale", {}) if isinstance(draft.get("rationale"), dict) else {}
    final_text = strategy_md
    if not parse_sections(final_text):
        return SpawnOutcome(child=None, mode="NEW", decline=False,
                            reason="new.drafting produced no '## ' behavioral-mechanism sections",
                            artifacts_dir=gd)

    # Persist the deployable original + rationale, build the child.
    _io.write_text_atomic(os.path.join(node_dir(out_dir, child_id), "strategy.md"), final_text)
    _io.write_text_atomic(os.path.join(dossier_dir(out_dir, child_id), "rationale.md"),
                          _render_rationale_md(rationale, title="NEW strategy rationale"))
    child = TreeNode(node_id=child_id, branch_type="NEW", strategy=final_text, rules="")
    _log.info("NEW spawn ready — child=%s sections=%d strategy_chars=%d",
              child_id, len(parse_sections(final_text)), len(final_text))
    return SpawnOutcome(child=child, mode="NEW", artifacts_dir=gd)


def _confront_novelty(oc: Any, cfg: Any, sel: dict, prior: str, findings: str, novelty_path: str):
    """Conceive, then confront novelty against every historical strategy.

    Returns ``(concept, verdict, err)``. On a duplicate the conception is
    regenerated with the critique appended, up to ``cfg.gen_novelty_retries``
    times; on exhaustion (or a barren conception) ``err`` is a reason string and
    ``concept``/``verdict`` are the last attempt. The final novelty verdict is
    always persisted for audit.
    """
    retries = int(getattr(cfg, "gen_novelty_retries", 2))
    critique = ""
    concept: dict = {}
    verdict: dict = {}
    history: "List[dict]" = []
    for attempt in range(retries + 1):
        critique_block = ("" if not critique else
                          "\nPRIOR CONCEPTION WAS JUDGED A DUPLICATE — address this critique and "
                          "differentiate further:\n" + critique + "\n")
        cu = prompts.NEW_CONCEPT_USER.format(
            target_selection=json.dumps(sel, ensure_ascii=False, indent=2),
            prior_strategies=prior, findings=findings, critique=critique_block)
        concept = _llm.complete_optimizer_json(
            oc, prompts.NEW_CONCEPT_SYSTEM, cu, parse=_llm.parse_json_object,
            ok=lambda r: bool(r.get("core_behavioral_commitment")),
            max_tokens=_CONCEPT_MAX, stage=_llm.STAGE_NEW_CONCEPT)
        if not (isinstance(concept, dict) and concept.get("core_behavioral_commitment")):
            history.append({"attempt": attempt, "error": "empty_conception"})
            _io.write_json_atomic(novelty_path, {"novel": False, "history": history})
            return concept, {}, "new.conception produced no core_behavioral_commitment"

        nu = prompts.NOVELTY_USER.format(
            conception=json.dumps(concept, ensure_ascii=False, indent=2),
            prior_strategies=prior)
        verdict = _llm.complete_optimizer_json(
            oc, prompts.NOVELTY_SYSTEM, nu, parse=_llm.parse_json_object,
            ok=lambda r: ("novel" in r), max_tokens=_NOVELTY_MAX,
            stage=_llm.STAGE_NEW_NOVELTY)
        is_novel = bool(verdict.get("novel"))
        history.append({"attempt": attempt, "novel": is_novel,
                        "duplicates": verdict.get("duplicates", ""),
                        "reason": verdict.get("reason", "")})
        if is_novel:
            _io.write_json_atomic(novelty_path, {"novel": True, "verdict": verdict,
                                                 "history": history})
            return concept, verdict, None
        critique = str(verdict.get("reason", "") or "duplicate of an existing strategy")

    _io.write_json_atomic(novelty_path, {"novel": False, "verdict": verdict, "history": history})
    return concept, verdict, ("new.novelty exhausted %d retries; last duplicate: %s"
                              % (retries, str(verdict.get("duplicates", "") or "?")))
