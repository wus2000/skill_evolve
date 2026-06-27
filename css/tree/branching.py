"""Phase 6 — branching decision + the real Layer-5c rollout-validation closure.

This module bridges Phase 4's L1 signals to Phase 5's PROPOSAL / REFINE operations
and supplies the concrete ``rollout_validate_fn`` those operations consume.

Two responsibilities (design §5 / §7, D11 / D14):

  :func:`decide_branch` — the deterministic per-node branching rule run at the
    SYNC point of every round. A node that has NOT yet saturated its L0 buffer
    keeps exploiting (``"EXPLOITATION"``). A saturated node with live L1 signals
    spends its REFINE budget first (``"REFINE"`` while ``refine_count < cfg.K``)
    then escalates to a full strategy rewrite (``"PROPOSAL"``). A saturated node
    with no L1 signal has nothing left to branch on (``"NONE"``).

  :func:`make_rollout_validate_fn` — builds the INJECTED Layer-5c callback that
    Phase 5 (:func:`css.proposal.proposal.run_proposal` / ``run_refine``) calls to
    measure whether a candidate skill actually suppresses the targeted failure
    pattern on the persistent-fail subset. It re-rolls those tasks with the
    candidate skill (Phase 2), re-annotates the trajectories (Phase 4 Layer 1),
    matches the new observations against the library, and reports the fraction of
    persistent-fail tasks that STILL exhibit a targeted pattern (occurrence_after)
    versus the targeted patterns' current library occurrence (occurrence_before).

All Phase-2/4/5 heavy modules are imported LAZILY inside the functions so this
module imports cheaply and free of model / embedding / faiss side effects. The
closure is deliberately fail-LOUD: any infrastructure failure RAISES, so Phase 5's
``_safe_rollout`` records it as an INFRASTRUCTURE error (distinct from a measured
no-decrease) rather than silently committing a candidate.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:  # pragma: no cover - type-only imports
    from css.config import CSSConfig
    from css.data.pattern import PatternLibrary
    from css.data.rollout import TaskRolloutGroup
    from css.data.tree import TreeNode
    from css.model.client import LLMClient


# ── Branching decision (deterministic; design §5 / D11) ──────────────────────

def decide_branch(node: "TreeNode", l1_signals: list, *, cfg: "CSSConfig") -> str:
    """Decide the operation to spawn from ``node`` at the round SYNC point.

    v2 logic: L0 saturation directly triggers the L1 hypothesis-test-verify
    cycle. The old l1_signals statistical gate is bypassed — Step 1 multi-
    dimensional analysis replaces it.

      * not L0-saturated -> ``"EXPLOITATION"``
      * saturated, REFINE budget remaining -> ``"REFINE"``
      * saturated, REFINE budget spent     -> ``"PROPOSAL"``
    """
    if not node.is_saturated(cfg.N):
        return "EXPLOITATION"
    if node.refine_count < cfg.K:
        return "REFINE"
    return "PROPOSAL"


# ── Layer-5c rollout-validation closure (design §7 / D14, constraint 5c) ─────

def make_rollout_validate_fn(
    env,
    target_client,
    optimizer_client,
    node: "TreeNode",
    library,
    persistent_fail_groups: list,
    *,
    cfg: "CSSConfig",
    out_dir: str,
    epoch: int,
):
    """Build the injected Layer-5c ``rollout_validate_fn`` for one PROPOSAL/REFINE.

    Returns ``fn(strategy_text, rules_text, targeted_pattern_ids) -> (occ_before,
    occ_after)``:

    Both rates share the SAME denominator — the persistent-fail task subset — so
    the comparison is fair (a global-rate baseline vs a hard-subset residual would
    bias the gate toward false rejections):

      * ``occ_before`` — the FRACTION of the persistent-fail subset whose CURRENT
        (pre-candidate) library observations already hit a targeted pattern. This
        is the rate we are trying to beat, measured on exactly the tasks we re-roll.
      * ``occ_after`` — re-roll the persistent-fail tasks with the CANDIDATE skill
        (strategy + rules rendered through a transient SkillDocument, Phase 2
        :func:`batch_rollout`), re-annotate with Phase 4 Layer 1, match the new
        observations against ``library`` (:func:`match_by_label`), and report the
        FRACTION of the SAME subset whose new observations still hit a targeted
        pattern — the candidate's residual occurrence of the behavior it must fix.

    ``occ_after < occ_before`` is the PASS criterion enforced by Phase 5.

    The closure is fail-LOUD: any infrastructure failure (rollout / annotation /
    matching) propagates as an exception so Phase 5's ``_safe_rollout`` records an
    INFRASTRUCTURE error distinct from a measured no-decrease. ``target_client`` is
    used ONLY for the rollout; ``optimizer_client`` ONLY for Layer 1 annotation
    (First Law: the analyst LLM never drives a rollout and vice-versa).
    """

    def rollout_validate_fn(
        strategy_text: str,
        rules_text: str,
        targeted_pattern_ids: list,
    ) -> tuple:
        # Lazy heavy imports (Phase 2 rollout, Phase 4 Layer 1 / matching, Phase 1
        # skill rendering) so importing this module stays cheap.
        from css.analysis.layer1 import run_layer1
        from css.data.rollout import group_rollouts
        from css.rollout.batch import batch_rollout
        from css.skill_document import SkillDocument

        targeted = [str(pid) for pid in (targeted_pattern_ids or [])]
        targeted_set = set(targeted)

        # occ_before and occ_after MUST share a denominator to be a fair
        # before/after comparison (a global latest_occurrence baseline vs a
        # persistent-fail-subset residual would bias the gate toward false
        # rejections, since persistent-fail tasks are the hardest). We use the
        # SAME persistent-fail task set as the denominator for both.
        groups = list(persistent_fail_groups or [])
        pf_task_ids = {str(g.task_id) for g in groups}
        denom = max(len(pf_task_ids), 1)

        # occurrence_before: fraction of THIS persistent-fail subset whose CURRENT
        # (pre-candidate) library observations already hit a targeted pattern.
        tasks_hitting_before: set = set()
        for pid in targeted:
            rec = library.get(pid)
            if rec is None:
                continue
            for obs in rec.observations:
                if str(obs.task_id) in pf_task_ids:
                    tasks_hitting_before.add(str(obs.task_id))
        occ_before = len(tasks_hitting_before) / denom

        # Gather the persistent-fail task items to re-roll with the candidate skill.
        items = _items_for_groups(env, groups)
        if not items:
            # Nothing to re-roll: the candidate cannot demonstrate a decrease.
            # Surface as occurrence-unchanged rather than fabricating a pass.
            return occ_before, occ_before

        # Render the candidate skill exactly as the frozen agent will see it.
        candidate_doc = SkillDocument(
            skill_dir="",
            strategy=strategy_text or "",
            rules=rules_text or "",
        )
        skill_text = candidate_doc.combined_skill_text()

        # Unique sub-out_dir so concurrent nodes / candidates never collide.
        sub_out = f"{out_dir}/5c_{node.node_id}_e{epoch}"

        # Phase 2 — re-roll the persistent-fail subset with the candidate skill.
        results = batch_rollout(
            env,
            items,
            skill_text,
            target_client,
            k_rollouts=cfg.k_rollouts,
            out_dir=sub_out,
            max_workers=cfg.max_api_workers,
            task_timeout=cfg.task_timeout_s,
            epoch=epoch,
            node_id=node.node_id,
        )
        new_groups = group_rollouts(results)

        # Phase 4 Layer 1 — re-annotate the candidate trajectories (optimizer LLM).
        observations, _divergences = run_layer1(
            optimizer_client,
            new_groups,
            node_id=node.node_id,
            epoch=epoch,
            cfg=cfg,
        )

        # Phase 4 matching — attach new observations to existing library
        # patterns by label similarity. Matched observations carry the matched
        # pattern_id; we tally which TASKS still hit a TARGETED pattern.
        from css.analysis.label_grouping import match_by_label
        matched, _unmatched = match_by_label(optimizer_client, library, observations)

        tasks_hitting_target: set = set()
        for obs in matched:
            if obs.pattern_id in targeted_set:
                tasks_hitting_target.add(str(obs.task_id))

        # Same persistent-fail denominator as occ_before: the fraction of the
        # subset whose post-candidate observations still hit a targeted pattern.
        # Tasks that no longer hit the pattern (or improved) simply drop out of
        # the numerator, so a genuine improvement shows occ_after < occ_before.
        occ_after = len(tasks_hitting_target & pf_task_ids) / denom

        return occ_before, occ_after

    return rollout_validate_fn


# ── Helpers ──────────────────────────────────────────────────────────────────

def _items_for_groups(env, groups: list) -> list:
    """Resolve the env task items for a list of persistent-fail rollout groups.

    A :class:`TaskRolloutGroup` carries only a ``task_id``; the rollout layer needs
    the full env item dict. We index the env's train items by their stable id (the
    same ``task_id``/``id`` key the rollout layer uses) and look each group up.
    Groups whose task id is not found in the env are skipped (best-effort), so a
    stale group never crashes the 5c rollout.
    """
    index = _train_item_index(env)
    items: list = []
    seen: set = set()
    for g in groups:
        tid = getattr(g, "task_id", None)
        if tid is None or tid in seen:
            continue
        item = index.get(str(tid))
        if item is not None:
            items.append(item)
            seen.add(tid)
    return items


def _train_item_index(env) -> dict:
    """Map ``task_id``/``id`` -> env train item dict (best-effort)."""
    index: dict = {}
    try:
        train_items = list(env.train_items())
    except Exception:
        train_items = []
    for item in train_items:
        if not isinstance(item, dict):
            continue
        tid = str(item.get("task_id", item.get("id", "")))
        if tid:
            index[tid] = item
    return index
