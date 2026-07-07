"""MERGE generation pipeline (docs/L1_actions_redesign.md §6): fuse coverage.

Triggered by the root's three-way dispatch when TRUE FULL COVERAGE holds
(``global_unsolved`` and ``uncharted`` both empty): every train task has been
solved by SOME node, so the problem phase-transitions from invention to
integration — produce one strategy expected to preserve the UNION of the
sources' coverage.

Seven persisted steps under ``nodes/<child>/gen/`` (step-granular resume,
mirroring NEW):

  0. complementarity matrix (mechanical, zero LLM;         -> gen/merge_matrix.json
     includes pruned/terminal nodes — coverage evidence
     outlives its node; degenerate matrix -> DECLINE)
  1. conflict-detection exploration (mode=MERGE)           -> gen/exploration_ref.json
  2. fusion conception (blueprint + expected_coverage)     -> gen/conception.json
  3. novelty vs PRIOR MERGE strategies only                -> gen/novelty_verdict.json
  4. drafting (deployable fused strategy.md)               -> gen/draft.json (+ strategy.md,
                                                              dossier/rationale.md)
  5. altitude + purity check (one repair round)            -> gen/altitude_check.json
  6. selective rules integration: per-source keep/drop     -> gen/rules_selection.json,
     judged against the FUSED strategy, then consolidation    gen/rules_consolidation.json,
     — SELECT-AND-PRUNE ONLY, assembly is verbatim by         dossier/rules_provenance.json
     construction (user ruling: never rewrite verified
     rule text; wording adaptation is the first burst's
     L0 job)

  7. coverage-preservation check runs AFTER the child's first burst, in the
     tree loop (the ledger must see the burst) -> dossier/merge_coverage_check.json.

SpawnOutcome semantics:
  * success                    -> SpawnOutcome(child=<node MERGE>, rules=assembled)
  * degenerate matrix          -> SpawnOutcome(child=None, decline=True)  — a root
                                  decline blocks WITHOUT a strike (tree loop)
  * a step produced nothing    -> SpawnOutcome(child=None, decline=False, reason=...)
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from css.data.tree import TreeNode
from css.l1gen import _context, _io, _llm, prompts, screen
from css.l1gen.new_pipeline import _render_rationale_md
from css.l1gen.sections import parse_sections
from css.optimizer.section_apply import parse_rules_into_sections
from css.tree_search import SpawnContext, SpawnOutcome, dossier_dir, node_dir

_log = logging.getLogger("css.l1gen")

_CONCEPT_MAX = 8192
_NOVELTY_MAX = 4096
_DRAFT_MAX = 12288
_RULES_SELECT_MAX = 6144
_RULES_CONSOLIDATE_MAX = 6144

# Probe menu cap for the conflict-detection session: representatives of every
# source's exclusive coverage (the director contrasts across them).
_MENU_CAP = 12


# ── Step 0 — complementarity matrix (mechanical) ─────────────────────────────
def build_merge_matrix(ledger: Any, tree: Any) -> dict:
    """Coverage complementarity from the ledger; includes DEAD nodes.

    A pruned/terminal node's coverage evidence outlives it — MERGE may
    resurrect a dead branch's exclusive capability (design §6 step 0).
    Degenerate (no node has exclusive coverage) => nothing to fuse.
    """
    node_ids = ledger.node_ids()
    exclusive: Dict[str, List[str]] = {}
    solved: Dict[str, List[str]] = {}
    for nid in node_ids:
        solved[nid] = sorted(ledger.solved_set(nid))
        exc = sorted(ledger.exclusive_coverage(nid))
        if exc:
            exclusive[nid] = exc
    all_solved = ledger.solved_anywhere()
    common = sorted(t for t in all_solved
                    if len(ledger.solvers(t)) == max(1, len(node_ids)))
    status = {}
    for nid in node_ids:
        node = tree.get(nid) if tree is not None else None
        status[nid] = getattr(node, "status", "gone") if node is not None else "gone"
    return {
        "node_ids": node_ids,
        "exclusive_coverage": exclusive,
        "solved_counts": {nid: len(solved[nid]) for nid in node_ids},
        "common_tasks": common,
        "node_status": status,
        "degenerate": not exclusive,
    }


def _render_matrix(matrix: dict) -> str:
    lines = ["Nodes: " + ", ".join(
        "%s(status=%s, solved=%d)" % (nid, matrix["node_status"].get(nid, "?"),
                                      matrix["solved_counts"].get(nid, 0))
        for nid in matrix["node_ids"])]
    lines.append("Exclusive coverage (solved here, nowhere else):")
    for nid, tasks in sorted(matrix["exclusive_coverage"].items()):
        lines.append("  %s: %s" % (nid, ", ".join(tasks)))
    lines.append("Common tasks (every node solves): %d"
                 % len(matrix.get("common_tasks", [])))
    return "\n".join(lines)


def _source_strategies(tree: Any, out_dir: str, cfg: Any, node_ids: List[str]) -> str:
    blocks = []
    for nid in node_ids:
        strat = _context.node_strategy(tree, out_dir, nid)
        if strat and strat != "(not yet available)" and strat.strip():
            blocks.append("=== SOURCE %s ===\n%s" % (nid, strat))
        else:
            blocks.append("=== SOURCE %s ===\n(bare node: no strategy layer; "
                          "its behavior lives in its rules)" % nid)
    return "\n\n".join(blocks)


def _merge_briefing(tree: Any, out_dir: str, cfg: Any, matrix: dict) -> str:
    parts = ["## MERGE conflict detection — fuse complementary strategies",
             "Full coverage holds: every train task is solved by SOME node. "
             "The fusion must preserve the UNION. Your job: find coverage-"
             "complementary but behaviorally-CONFLICTING strategy pairs, "
             "propose a reconciliation (e.g. conditional routing on a task "
             "feature), and PROBE a fused draft behavior on representative "
             "tasks from BOTH sides. Report which reconciliations held and "
             "which pairs are irreconcilable (keep the division of labor).",
             "### Complementarity matrix\n" + _render_matrix(matrix)]
    lb = _context.leads_block(
        out_dir, [t for ts in matrix["exclusive_coverage"].values() for t in ts])
    if lb:
        parts.append(lb)
    parts.append("### Source strategies and dossiers\n"
                 + _context.all_node_dossiers(tree, out_dir, cfg))
    return "\n\n".join(parts)


def _prior_merge_strategies(tree: Any, out_dir: str, cfg: Any) -> Tuple[str, int]:
    """Strategies of prior MERGE nodes only (novelty scope, design §6 step 3)."""
    ids = sorted(getattr(tree, "nodes", {}).keys()) if tree is not None else []
    blocks = []
    for nid in ids:
        node = tree.get(nid)
        if getattr(node, "branch_type", "") != "MERGE":
            continue
        strat = _context.node_strategy(tree, out_dir, nid)
        if strat.strip() and strat != "(not yet available)":
            blocks.append("=== PRIOR MERGE %s ===\n%s" % (nid, strat))
    return ("\n\n".join(blocks) if blocks else "(no prior MERGE strategies)",
            len(blocks))


# ── Step 6 — selective rules integration ─────────────────────────────────────
def _rules_sections(rules_md: str) -> "List":
    """Named '### ' sections of a rules doc (preamble excluded from selection)."""
    return [s for s in parse_rules_into_sections(rules_md or "")
            if s.heading != "_preamble" and s.content.strip()]


def _select_rules_per_source(
    oc: Any, cfg: Any, fused_strategy: str, source_node: str,
    rules_md: str, exclusive_tasks: List[str],
) -> Tuple[List[dict], List]:
    """One LLM call: keep/drop verdict per section, judged vs the fused strategy."""
    sections = _rules_sections(rules_md)
    if not sections:
        return [], []
    user = prompts.MERGE_RULES_SELECT_USER.format(
        strategy=fused_strategy, source_node=source_node,
        exclusive_tasks=", ".join(exclusive_tasks) or "(none)",
        rules=rules_md)
    verdicts = _llm.complete_optimizer_json(
        oc, prompts.MERGE_RULES_SELECT_SYSTEM, user, parse=_llm.parse_json_array,
        ok=lambda r: isinstance(r, list), max_tokens=_RULES_SELECT_MAX,
        stage=_llm.STAGE_MERGE_RULES_SELECT)
    if not isinstance(verdicts, list) or not verdicts:
        # Unparseable -> conservative keep-all for coverage-bearing sources
        # (mirrors refine.inherit's never-drop-all-on-noise stance).
        _log.warning("merge.rules_select(%s): no verdicts parsed; keeping all",
                     source_node)
        return ([{"section": s.heading, "verdict": "keep",
                  "reason": "fallback: selector unparseable"} for s in sections],
                sections)
    by_heading = {s.heading: s for s in sections}
    kept = []
    norm_verdicts = []
    for v in verdicts:
        if not isinstance(v, dict):
            continue
        heading = str(v.get("section", "")).strip()
        sec = by_heading.get(heading) or by_heading.get("### " + heading.lstrip("# ").strip())
        verdict = str(v.get("verdict", "")).lower()
        norm_verdicts.append({"section": heading, "verdict": verdict,
                              "reason": str(v.get("reason", ""))})
        if sec is not None and verdict == "keep":
            kept.append(sec)
    return norm_verdicts, kept


def _consolidate_rules(
    oc: Any, cfg: Any, fused_strategy: str,
    kept_by_source: "Dict[str, List]",
) -> Tuple[str, List[dict], dict]:
    """Consolidation call -> verbatim assembly from (source, section) picks.

    The LLM only CHOOSES and ORDERS; the final document is assembled from the
    original section bytes here, so the no-rewrite fidelity constraint holds
    by construction. Returns (rules_md, provenance, consolidation_record).
    """
    candidates = []
    index: "Dict[Tuple[str, str], Any]" = {}
    for src, secs in kept_by_source.items():
        for s in secs:
            index[(src, s.heading)] = s
            candidates.append("--- source=%s section=%s ---\n%s"
                              % (src, s.heading, s.content.rstrip()))
    if not index:
        return "", [], {"sections": [], "dropped": [], "note": "no kept sections"}
    if len(index) == 1:
        (src, heading), sec = next(iter(index.items()))
        return (sec.content.rstrip() + "\n",
                [{"source": src, "section": heading}],
                {"sections": [{"source": src, "section": heading}],
                 "dropped": [], "note": "single section: consolidation skipped"})

    user = prompts.MERGE_RULES_CONSOLIDATE_USER.format(
        strategy=fused_strategy, candidates="\n\n".join(candidates))
    picks = _llm.complete_optimizer_json(
        oc, prompts.MERGE_RULES_CONSOLIDATE_SYSTEM, user,
        parse=_llm.parse_json_object,
        ok=lambda r: isinstance(r, dict) and isinstance(r.get("sections"), list),
        max_tokens=_RULES_CONSOLIDATE_MAX, stage=_llm.STAGE_MERGE_RULES_CONSOLIDATE)
    chosen: "List[Tuple[str, str]]" = []
    if isinstance(picks, dict):
        for p in picks.get("sections", []) or []:
            if not isinstance(p, dict):
                continue
            key = (str(p.get("source", "")), str(p.get("section", "")).strip())
            if key in index and key not in chosen:
                chosen.append(key)
    if not chosen:
        # Unparseable consolidation -> keep everything in source order (dedup
        # is a quality concern; losing verified rules is a coverage concern).
        _log.warning("merge.rules_consolidate: no picks parsed; keeping all kept")
        chosen = list(index.keys())
        picks = {"sections": [{"source": s, "section": h} for s, h in chosen],
                 "dropped": [], "note": "fallback: consolidator unparseable"}

    parts = [index[k].content.rstrip() for k in chosen]
    provenance = [{"source": s, "section": h} for s, h in chosen]
    return "\n\n".join(parts) + "\n", provenance, picks


# ── Orchestration ─────────────────────────────────────────────────────────────
def run_merge_pipeline(ctx: SpawnContext) -> SpawnOutcome:
    from css.coverage import load_coverage

    oc = ctx.optimizer_client
    out_dir = ctx.out_dir
    child_id = ctx.new_node_id
    cfg = ctx.cfg
    gd = _context.gen_dir(out_dir, child_id)
    _io.ensure_dir(gd)

    ledger = load_coverage(out_dir, min_attempts=int(
        getattr(cfg, "ledger_min_attempts", 1)))

    # ── Step 0 — complementarity matrix ─────────────────────────────────────
    matrix_path = os.path.join(gd, "merge_matrix.json")
    matrix = _io.read_json(matrix_path)
    if matrix is None:
        matrix = build_merge_matrix(ledger, ctx.tree)
        _io.write_json_atomic(matrix_path, matrix)
    if matrix.get("degenerate"):
        return SpawnOutcome(
            child=None, mode="MERGE", decline=True,
            reason="merge.matrix degenerate — no node holds exclusive coverage; "
                   "nothing to fuse", artifacts_dir=gd)
    source_ids = sorted(matrix["exclusive_coverage"].keys())
    matrix_text = _render_matrix(matrix)
    sources_text = _source_strategies(ctx.tree, out_dir, cfg, source_ids)

    # ── Step 1 — conflict-detection exploration (lazy; never fatal) ─────────
    expl_path = os.path.join(gd, "exploration_ref.json")
    exploration = _io.read_json(expl_path)
    if exploration is None:
        menu = [t for ts in matrix["exclusive_coverage"].values()
                for t in ts][:_MENU_CAP]
        exploration = _context.run_exploration(
            mode="MERGE", group_key="merge_%s" % ledger.signature(),
            group_tasks=menu, neighbor_tasks=[],
            briefing_md=_merge_briefing(ctx.tree, out_dir, cfg, matrix),
            cfg=cfg, env=ctx.env, target_client=ctx.target_client,
            optimizer_client=oc, out_dir=out_dir,
            decision_index=ctx.decision_index)
        _io.write_json_atomic(expl_path, exploration)
    findings = str(exploration.get("findings", "") or "") or "(no exploration findings)"

    # ── Step 2 — fusion conception ───────────────────────────────────────────
    concept_path = os.path.join(gd, "conception.json")
    concept = _io.read_json(concept_path)
    if concept is None:
        user = prompts.MERGE_CONCEPT_USER.format(
            matrix=matrix_text, source_strategies=sources_text, findings=findings)
        concept = _llm.complete_optimizer_json(
            oc, prompts.MERGE_CONCEPT_SYSTEM, user, parse=_llm.parse_json_object,
            ok=lambda r: bool(r.get("contributions")) or bool(r.get("base_node")),
            max_tokens=_CONCEPT_MAX, stage=_llm.STAGE_MERGE_CONCEPT)
        if not (isinstance(concept, dict) and
                (concept.get("contributions") or concept.get("base_node"))):
            return SpawnOutcome(child=None, mode="MERGE", decline=False,
                                reason="merge.conception produced no blueprint",
                                artifacts_dir=gd)
        _io.write_json_atomic(concept_path, concept)

    # ── Step 3 — novelty vs prior MERGE strategies only ─────────────────────
    novelty_path = os.path.join(gd, "novelty_verdict.json")
    verdict = _io.read_json(novelty_path)
    if not (isinstance(verdict, dict) and verdict.get("novel")):
        prior_merge, n_prior = _prior_merge_strategies(ctx.tree, out_dir, cfg)
        if n_prior == 0:
            verdict = {"novel": True, "verdict": {"novel": True},
                       "note": "first MERGE: no prior fusion to duplicate"}
        else:
            nu = prompts.NOVELTY_USER.format(
                conception=json.dumps(concept, ensure_ascii=False, indent=2),
                prior_strategies=prior_merge)
            raw = _llm.complete_optimizer_json(
                oc, prompts.NOVELTY_SYSTEM, nu, parse=_llm.parse_json_object,
                ok=lambda r: ("novel" in r), max_tokens=_NOVELTY_MAX,
                stage=_llm.STAGE_MERGE_NOVELTY)
            verdict = {"novel": bool(raw.get("novel")), "verdict": raw}
            if not verdict["novel"]:
                _io.write_json_atomic(novelty_path, verdict)
                return SpawnOutcome(
                    child=None, mode="MERGE", decline=False,
                    reason="merge.novelty: duplicate of prior fusion %s"
                           % str(raw.get("duplicates", "") or "?"),
                    artifacts_dir=gd)
        _io.write_json_atomic(novelty_path, verdict)

    # ── Step 4 — drafting ────────────────────────────────────────────────────
    draft_path = os.path.join(gd, "draft.json")
    draft = _io.read_json(draft_path)
    if draft is None:
        user = prompts.MERGE_DRAFT_USER.format(
            conception=json.dumps(concept, ensure_ascii=False, indent=2),
            source_strategies=sources_text, findings=findings)
        draft = _llm.complete_optimizer_json(
            oc, prompts.merge_draft_system(), user, parse=_llm.parse_json_object,
            ok=lambda r: bool(r.get("strategy_md")),
            max_tokens=_DRAFT_MAX, stage=_llm.STAGE_MERGE_DRAFT)
        if not (isinstance(draft, dict) and str(draft.get("strategy_md", "")).strip()):
            return SpawnOutcome(child=None, mode="MERGE", decline=False,
                                reason="merge.drafting produced no strategy_md",
                                artifacts_dir=gd)
        if not parse_sections(str(draft.get("strategy_md", ""))):
            return SpawnOutcome(child=None, mode="MERGE", decline=False,
                                reason="merge.drafting produced no '## ' sections",
                                artifacts_dir=gd)
        _io.write_json_atomic(draft_path, draft)

    strategy_md = str(draft.get("strategy_md", ""))
    rationale = draft.get("rationale", {}) if isinstance(draft.get("rationale"), dict) else {}

    # ── Step 5 — altitude + purity (one repair round) ───────────────────────
    alt_path = os.path.join(gd, "altitude_check.json")
    alt = _io.read_json(alt_path)
    if alt is None:
        ok, final_text, trail = screen.altitude_purity_check(
            oc, strategy_md, allow_repair=True, stage=_llm.STAGE_MERGE_ALTITUDE)
        alt = {"ok": bool(ok), "final_strategy": final_text, "trail": trail}
        _io.write_json_atomic(alt_path, alt)
    if not alt.get("ok"):
        return SpawnOutcome(child=None, mode="MERGE", decline=False,
                            reason="merge.altitude check failed after repair",
                            artifacts_dir=gd)
    final_text = str(alt.get("final_strategy", strategy_md)) or strategy_md
    if not parse_sections(final_text):
        return SpawnOutcome(child=None, mode="MERGE", decline=False,
                            reason="merge.altitude repair dropped the section structure",
                            artifacts_dir=gd)

    # ── Step 6 — selective rules integration (select-and-prune only) ────────
    sel_path = os.path.join(gd, "rules_selection.json")
    con_path = os.path.join(gd, "rules_consolidation.json")
    consolidation = _io.read_json(con_path)
    if consolidation is None:
        selections: "Dict[str, List[dict]]" = {}
        kept_by_source: "Dict[str, List]" = {}
        for nid in source_ids:
            node = ctx.tree.get(nid) if ctx.tree is not None else None
            rules_md = getattr(node, "best_rules", "") or getattr(node, "rules", "")
            if not (rules_md or "").strip():
                continue
            verdicts, kept = _select_rules_per_source(
                oc, cfg, final_text, nid, rules_md,
                matrix["exclusive_coverage"].get(nid, []))
            selections[nid] = verdicts
            if kept:
                kept_by_source[nid] = kept
        _io.write_json_atomic(sel_path, selections)
        rules_md, provenance, picks = _consolidate_rules(
            oc, cfg, final_text, kept_by_source)
        cap = int(getattr(cfg, "rules_max_chars", 0) or 0)
        if cap > 0 and len(rules_md) > cap:
            # Trim whole trailing sections until under the cap — never cut
            # inside a section (that WOULD be a rewrite).
            trimmed = list(provenance)
            parts = rules_md.split("\n\n")
            while trimmed and len("\n\n".join(parts)) > cap and len(parts) > 1:
                parts.pop()
                trimmed.pop()
            rules_md = "\n\n".join(parts).rstrip() + "\n"
            _log.warning("merge.rules: assembled rules exceeded rules_max_chars"
                         " (%d); trimmed to %d sections", cap, len(trimmed))
            provenance = trimmed
        consolidation = {"picks": picks, "rules_md": rules_md,
                         "provenance": provenance}
        _io.write_json_atomic(con_path, consolidation)
        _io.write_json_atomic(
            os.path.join(dossier_dir(out_dir, child_id), "rules_provenance.json"),
            provenance)
    rules_md = str(consolidation.get("rules_md", ""))

    # Persist deployable artifacts + child (branch MERGE keeps its rules;
    # the tree loop zeroes rules for NEW only).
    _io.write_text_atomic(os.path.join(node_dir(out_dir, child_id), "strategy.md"),
                          final_text)
    _io.write_text_atomic(os.path.join(dossier_dir(out_dir, child_id), "rationale.md"),
                          _render_rationale_md(rationale, title="MERGE strategy rationale"))
    expected = [str(t) for t in (concept.get("expected_coverage") or [])]
    child = TreeNode(node_id=child_id, branch_type="MERGE",
                     strategy=final_text, rules=rules_md, best_rules=rules_md)
    _log.info("MERGE spawn ready — child=%s sources=%s sections=%d rules_chars=%d "
              "expected_coverage=%d",
              child_id, ",".join(source_ids), len(parse_sections(final_text)),
              len(rules_md), len(expected))
    return SpawnOutcome(child=child, mode="MERGE", artifacts_dir=gd)


# ── Step 7 — coverage-preservation check (post-first-burst; tree loop) ──────
def merge_coverage_check(out_dir: str, child_id: str, cfg: Any) -> "Optional[dict]":
    """Compare the child's post-burst solved set against expected_coverage.

    Record-only (design §6 step 7): the lost-list is the next MERGE/REFINE's
    input, never an automatic action. Returns the record (None when there is
    no expectation to check).
    """
    from css.coverage import load_coverage

    concept = _io.read_json(os.path.join(
        _context.gen_dir(out_dir, child_id), "conception.json")) or {}
    expected = [str(t) for t in (concept.get("expected_coverage") or [])]
    if not expected:
        return None
    ledger = load_coverage(out_dir, min_attempts=int(
        getattr(cfg, "ledger_min_attempts", 1)))
    solved = ledger.solved_set(child_id)
    attempted_cells = {t for t in expected
                       if ledger.stats(child_id, t)["attempts"] > 0}
    record = {
        "expected": expected,
        "kept": sorted(t for t in expected if t in solved),
        "lost": sorted(t for t in attempted_cells if t not in solved),
        "unattempted": sorted(t for t in expected if t not in attempted_cells),
        "extra": sorted(t for t in solved if t not in set(expected)),
    }
    _io.write_json_atomic(os.path.join(dossier_dir(out_dir, child_id),
                                       "merge_coverage_check.json"), record)
    _log.info("MERGE coverage check — %s: kept %d/%d, lost %d, unattempted %d",
              child_id, len(record["kept"]), len(expected),
              len(record["lost"]), len(record["unattempted"]))
    return record
