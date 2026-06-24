"""Phase 5 orchestrator — wire PROPOSAL and REFINE end-to-end (design §4.2 / §4.3, D4 / D10).

This module is the ENTRY POINT of an L1 (cognitive-strategy) operation. Phase 4
produces L1 signals (persistent, remedy-resistant failure patterns); this module
turns one such signal set into either a new PROPOSAL tree node (a structural
strategy rewrite) or a new REFINE tree node (a local 1-2 subsection edit), or — on
failure — a negative-archive entry recording the disproven direction so the search
never blindly re-explores it.

Both operations SHARE Layer 4 (root-cause attribution) and the Layer 5b/5c
validation spine; they DIVERGE only at Layer 5a (full derive vs. local refine) and
at knowledge inheritance (semantic keep/drop vs. full-inherit + conflict cleanup).

The pipeline (design §4.3):

  Layer 4  attribute_root_cause  -> ordered RootCauses (high-leverage first).
                                    No cause -> early return (no_root_cause).
  Layer 5a (PROPOSAL) derive_strategy from the top RootCause + the paired SUCCESS
           counterpart patterns (systematize what already worked); then the
           negative-archive gate (reminder-not-prohibition).
  Layer 5a (REFINE)   derive_refine -> a gated local edit; on gate failure we
           return a reason carrying ``escalate_to_proposal`` so the caller can
           switch operation.
  Layer 5b retrospective_validate  -> cheap pre-rollout coverage gate. A
           ``"reconsider"`` verdict kills the proposal cheaply (low_coverage).
  Layer 5c rollout_validate_fn (INJECTED callback) -> (occ_before, occ_after) for
           the TARGET L1 pattern on the persistent-fail subset. The authoritative
           metric is the TARGET pattern's occurrence-rate DROP, not total pass
           rate. occ_after strictly below occ_before -> PASS.
           PASS -> a new TreeNode (PROPOSAL / REFINE).
           FAIL -> a NegativeArchiveEntry (origin proposal_/refine_failed_rollout).

The 5c rollout is an INJECTED callback so this module is testable without the
Phase-2 rollout / Phase-4 analysis stack (real wiring lands in Phase 6). Heavy
Phase-5 submodules are imported LAZILY inside the functions, so importing this
module is cheap and free of model/embedding side effects.

``rollout_validate_fn`` signature:
    (strategy_text: str, rules_text: str, targeted_pattern_ids: list[str])
        -> tuple[float, float]   # (occurrence_before, occurrence_after)

Robustness contract (mirrors the rest of Phase 5): malformed LLM output never
crashes; every failure mode returns a :class:`ProposalOutcome` with
``success=False`` and a documented ``reason``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:  # pragma: no cover - type-only imports
    from css.analysis.embedding import Embedder
    from css.config import CSSConfig
    from css.data.negative_archive import NegativeArchive, NegativeArchiveEntry
    from css.data.pattern import PatternLibrary, PatternRecord
    from css.data.rollout import TaskResult, TaskRolloutGroup
    from css.data.tree import TreeNode
    from css.model.client import LLMClient
    from css.proposal.derivation import StrategyProposal, ValidationResult
    from css.proposal.root_cause import RootCause


# 5c PASS criterion (documented threshold). The design's core 5c metric is that
# the TARGET L1 pattern's occurrence rate "significantly DECREASES". The injected
# rollout callback returns the measured (occurrence_before, occurrence_after); we
# require occ_after STRICTLY below occ_before. "Strictly" is the minimal,
# unambiguous reading of "decrease"; we expose the comparison as a single helper
# so the Phase-6 wiring (which owns statistical significance over real rollouts)
# can tighten it (e.g. a min-effect margin) in exactly one place without changing
# the orchestration logic here.
def _rollout_passed(occ_before: float, occ_after: float) -> bool:
    """True iff the target pattern's occurrence rate strictly decreased."""
    return occ_after < occ_before


# Type alias for the injected Layer-5c rollout validation callback.
RolloutValidateFn = Callable[[str, str, "list[str]"], "tuple[float, float]"]


@dataclass
class ProposalOutcome:
    """Result of one PROPOSAL or REFINE attempt (frozen public API).

    ``success`` reflects the Layer-5c rollout verdict (PASS). ``operation`` is the
    operation that ran (``"PROPOSAL"`` or ``"REFINE"``). On PASS, ``new_node`` is
    the freshly built child :class:`~css.data.tree.TreeNode`; on a rollout FAIL,
    ``archived`` is the :class:`~css.data.negative_archive.NegativeArchiveEntry`
    just written. ``validation`` carries the Layer-5b result when it was computed.
    ``occurrence_before`` / ``occurrence_after`` carry the 5c measurements (``-1.0``
    when 5c was not reached). ``reason`` documents WHY a non-success outcome
    happened (and, for REFINE gate failures, carries ``escalate_to_proposal`` so
    the caller can switch operation).
    """

    success: bool
    operation: str  # "PROPOSAL" | "REFINE"
    new_node: "TreeNode | None" = None
    archived: "NegativeArchiveEntry | None" = None
    validation: "ValidationResult | None" = None
    occurrence_before: float = -1.0
    occurrence_after: float = -1.0
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "operation": self.operation,
            "new_node": self.new_node.to_dict() if self.new_node is not None else None,
            "archived": (
                self.archived.to_dict(include_embedding=True)
                if self.archived is not None
                else None
            ),
            "validation": self.validation.to_dict() if self.validation is not None else None,
            "occurrence_before": self.occurrence_before,
            "occurrence_after": self.occurrence_after,
            "reason": self.reason,
        }


# ── Shared helpers ───────────────────────────────────────────────────────────

def _attribute(
    node: "TreeNode",
    l1_signals: "list[PatternRecord]",
    library: "PatternLibrary",
    optimizer_client: "LLMClient",
    *,
    cfg: "CSSConfig",
) -> "list[RootCause]":
    """Layer 4 shared by PROPOSAL and REFINE.

    The node's current strategy is passed to the attribution prompt's level-3
    ("which strategy text permitted it?") via an EXPLICIT ``current_strategy``
    argument — not stashed on shared ``cfg`` — so concurrent Phase-6 nodes
    (``concurrency_limit`` per round, one shared ``cfg``) cannot clobber each
    other's strategy text. The remedy history is the union of the L0 rules already
    tried (``node.rules`` lines) — the evidence the LLM must explain away when
    arguing this is a genuine L1 (thinking-level) cause.
    """
    from css.proposal.root_cause import attribute_root_cause

    remedy_history = _remedy_history(node)
    return attribute_root_cause(
        optimizer_client,
        l1_signals,
        library,
        remedy_history=remedy_history,
        cfg=cfg,
        current_strategy=node.strategy or "",
    )


def _remedy_history(node: "TreeNode") -> "list[str]":
    """The L0 rules already tried at this node (one per non-empty line).

    These are the surface remedies the root-cause analyst must explain could NOT
    durably fix the pattern (the justification for an L1 change). Best-effort: the
    node's ``rules`` text split into lines.
    """
    rules = (node.rules or "").strip()
    if not rules:
        return []
    return [ln.strip() for ln in rules.splitlines() if ln.strip()]


def _counterpart_success_patterns(
    targeted_pattern_ids: "list[str]",
    library: "PatternLibrary",
) -> "list[PatternRecord]":
    """Resolve the paired SUCCESS counterparts for the targeted failure patterns.

    For each targeted (failure) pattern, follow ``PatternRecord.counterpart_id``
    into ``library`` and keep the resolved record only when it is a SUCCESS pattern
    (``polarity == "success"``). De-duplicates while preserving order. These are
    the behaviors Layer 5a SYSTEMATIZES — the cases where the agent already thought
    the right way and succeeded.
    """
    out: list["PatternRecord"] = []
    seen: set[str] = set()
    for pid in targeted_pattern_ids:
        failure = library.get(pid)
        if failure is None or not failure.counterpart_id:
            continue
        counterpart = library.get(failure.counterpart_id)
        if counterpart is None:
            continue
        if getattr(counterpart, "polarity", None) != "success":
            continue
        if counterpart.pattern_id in seen:
            continue
        seen.add(counterpart.pattern_id)
        out.append(counterpart)
    return out


def _root_cause_summary(rc: "RootCause") -> str:
    """A compact one-line root-cause summary for negative-archive provenance."""
    pids = ", ".join(rc.pattern_ids) or "(unspecified)"
    strat = (rc.strategy or "").strip()
    assm = (rc.assumption or "").strip()
    return f"patterns[{pids}] strategy: {strat} | assumption: {assm}".strip()


def _embed_strategy(embedder: "Embedder", strategy_text: str) -> "list[float] | None":
    """Embed a strategy snapshot for the negative archive (best-effort).

    Returns a plain list of floats (the contract's
    ``[float(x) for x in embedder.embed([text])[0]]``) so the entry is JSON-round-
    trippable and ``NegativeArchive.recall`` can score it later. Degrades to
    ``None`` on any embedding failure rather than blocking the archive write.
    """
    try:
        vec = embedder.embed([strategy_text])
        row = vec[0]
        return [float(x) for x in row]
    except Exception:
        return None


def _archive_failed_rollout(
    archive: "NegativeArchive",
    embedder: "Embedder",
    *,
    strategy_snapshot: str,
    origin: str,
    root_cause: "RootCause",
    occ_before: float,
    occ_after: float,
    epoch: int,
    source_node_id: str,
    rollout_error: str = "",
) -> "NegativeArchiveEntry":
    """Write a disproven direction to the negative archive and return the entry.

    Embeds ``strategy_snapshot`` at write time (so a later Layer-5b recall can find
    it) and records the root-cause summary + the failure evidence. When
    ``rollout_error`` is set, the evidence records the INFRASTRUCTURE failure
    distinctly from a measured no-decrease (which would otherwise look identical).
    """
    from css.data.negative_archive import NegativeArchiveEntry

    if rollout_error:
        failure_evidence = (
            f"5c rollout could not be measured ({rollout_error}); "
            f"archived as unvalidated to avoid re-trying a broken direction"
        )
    else:
        failure_evidence = (
            f"target pattern occurrence rate {occ_before} -> {occ_after} "
            f"(no significant decrease) on persistent-fail rollout"
        )
    entry = NegativeArchiveEntry(
        entry_id=archive.new_entry_id(),
        strategy_snapshot=strategy_snapshot,
        origin=origin,
        root_cause=_root_cause_summary(root_cause),
        failure_evidence=failure_evidence,
        created_epoch=epoch,
        created_step=-1,
        source_node_id=source_node_id,
        embedding=_embed_strategy(embedder, strategy_snapshot),
    )
    archive.add(entry)
    return entry


# ── PROPOSAL ─────────────────────────────────────────────────────────────────

def run_proposal(
    node: "TreeNode",
    l1_signals: "list[PatternRecord]",
    library: "PatternLibrary",
    archive: "NegativeArchive",
    embedder: "Embedder",
    optimizer_client: "LLMClient",
    *,
    cfg: "CSSConfig",
    new_node_id: str,
    epoch: int,
    success_results: "list[TaskResult]",
    persistent_fail_groups: "list[TaskRolloutGroup]",
    rollout_validate_fn: RolloutValidateFn,
) -> "ProposalOutcome":
    """Run a full PROPOSAL: Layer 4 -> 5a derive -> neg-archive gate -> 5b -> 5c.

    Steps (design §4.3 / D4):
      1. Layer 4 (:func:`_attribute`). No root cause -> ``no_root_cause``.
      2. Take the TOP (highest-leverage) RootCause; gather its paired SUCCESS
         counterpart patterns (via ``counterpart_id``) and Layer-5a
         :func:`derive_strategy` to SYSTEMATIZE them into a new strategy body.
         An empty derived strategy -> ``empty_strategy``.
      3. Negative-archive gate (:func:`check_negative_archive`): if the candidate
         is the SAME disproven direction (no articulable difference), archive it
         and return ``neg_archive_block``.
      4. Layer 5b (:func:`retrospective_validate`): a ``"reconsider"`` verdict ->
         ``low_coverage`` (cheap early-kill, no rollout spent).
      5. Layer 5c (``rollout_validate_fn`` on the inherited rules): PASS
         (occ_after < occ_before) -> build a new PROPOSAL :class:`TreeNode` whose
         rules are the SEMANTIC keep/drop inheritance of the parent rules. FAIL ->
         archive (origin ``proposal_failed_rollout``).

    Never raises. The same inherited rules are computed once and threaded into both
    the 5c rollout and the accepted node so what we validated is what we keep.
    """
    from css.proposal.derivation import check_negative_archive, derive_strategy, retrospective_validate
    from css.proposal.inheritance import proposal_inherit_rules

    # Layer 4 — root-cause attribution (shared).
    root_causes = _attribute(node, l1_signals, library, optimizer_client, cfg=cfg)
    if not root_causes:
        return ProposalOutcome(
            success=False,
            operation="PROPOSAL",
            reason="no_root_cause: Layer 4 produced no evidence-complete diagnosis",
        )

    top = root_causes[0]

    # Layer 5a — derive (NOT generate) from the top root cause + success counterparts.
    counterparts = _counterpart_success_patterns(top.pattern_ids, library)
    proposal = derive_strategy(
        optimizer_client, top, counterparts, cfg=cfg, current_strategy=node.strategy or ""
    )
    if not (proposal.strategy_text or "").strip():
        return ProposalOutcome(
            success=False,
            operation="PROPOSAL",
            reason="empty_strategy: Layer 5a derivation produced no strategy text",
        )

    # Layer 5b gate (1/2) — negative-archive "reminder not prohibition".
    proceed, neg_reason = check_negative_archive(
        optimizer_client,
        embedder,
        proposal.strategy_text,
        archive,
        top_k=cfg.neg_archive_top_k,
    )
    if not proceed:
        # The candidate repeats a disproven direction: record it so the search is
        # reminded next time too, and stop here.
        archived = _archive_failed_rollout(
            archive,
            embedder,
            strategy_snapshot=proposal.strategy_text,
            origin="proposal_failed_rollout",
            root_cause=top,
            occ_before=-1.0,
            occ_after=-1.0,
            epoch=epoch,
            source_node_id=node.node_id,
        )
        return ProposalOutcome(
            success=False,
            operation="PROPOSAL",
            archived=archived,
            reason=f"neg_archive_block: {neg_reason}",
        )

    # Layer 5b gate (2/2) — cheap retrospective coverage check.
    validation = retrospective_validate(
        optimizer_client,
        proposal,
        success_results,
        persistent_fail_groups,
        cfg=cfg,
    )
    if validation.verdict == "reconsider":
        return ProposalOutcome(
            success=False,
            operation="PROPOSAL",
            validation=validation,
            reason=f"low_coverage: coverage {validation.coverage:.3f} < "
            f"coverage_high {cfg.coverage_high:.2f}; reconsider root cause",
        )

    # Knowledge inheritance — SEMANTIC keep/drop of the parent rules under the new
    # strategy. Compute ONCE so the 5c rollout validates the exact rules we keep.
    inherited_rules = proposal_inherit_rules(
        optimizer_client, proposal.strategy_text, node.rules, cfg=cfg
    )

    # Layer 5c — injected rollout validation on the persistent-fail subset.
    occ_before, occ_after, rollout_error = _safe_rollout(
        rollout_validate_fn,
        proposal.strategy_text,
        inherited_rules,
        proposal.targeted_pattern_ids,
    )

    if not rollout_error and _rollout_passed(occ_before, occ_after):
        new_node = _build_node(
            node,
            new_node_id=new_node_id,
            branch_type="PROPOSAL",
            strategy=proposal.strategy_text,
            rules=inherited_rules,
            refine_count=node.refine_count,
            epoch=epoch,
        )
        return ProposalOutcome(
            success=True,
            operation="PROPOSAL",
            new_node=new_node,
            validation=validation,
            occurrence_before=occ_before,
            occurrence_after=occ_after,
            reason=f"rollout_pass: target occurrence {occ_before} -> {occ_after}",
        )

    # Rollout FAIL -> negative archive (disproven by real rollout).
    archived = _archive_failed_rollout(
        archive,
        embedder,
        strategy_snapshot=proposal.strategy_text,
        origin="proposal_failed_rollout",
        root_cause=top,
        occ_before=occ_before,
        occ_after=occ_after,
        epoch=epoch,
        source_node_id=node.node_id,
        rollout_error=rollout_error,
    )
    return ProposalOutcome(
        success=False,
        operation="PROPOSAL",
        archived=archived,
        validation=validation,
        occurrence_before=occ_before,
        occurrence_after=occ_after,
        reason=(
            f"rollout_error: {rollout_error}" if rollout_error
            else f"rollout_fail: target occurrence {occ_before} -> {occ_after} "
            f"(no significant decrease)"
        ),
    )


# ── REFINE ───────────────────────────────────────────────────────────────────

def run_refine(
    node: "TreeNode",
    l1_signals: "list[PatternRecord]",
    library: "PatternLibrary",
    archive: "NegativeArchive",
    embedder: "Embedder",
    optimizer_client: "LLMClient",
    *,
    cfg: "CSSConfig",
    new_node_id: str,
    epoch: int,
    success_results: "list[TaskResult]",
    persistent_fail_groups: "list[TaskRolloutGroup]",
    rollout_validate_fn: RolloutValidateFn,
) -> "ProposalOutcome":
    """Run a full REFINE: Layer 4 (shared) -> 5a local edit -> 5b -> 5c.

    REFINE shares Layer 4 with PROPOSAL but diverges at 5a to a LOCAL change:
      1. Layer 4 (:func:`_attribute`). No root cause -> ``no_root_cause``.
      2. Layer 5a (:func:`derive_refine`) rewrites only 1-2 ``###`` subsections of
         the parent strategy (gated deterministically by
         :func:`css.markdown_utils.check_refine_diff`). If the gate fails, the
         reason carries ``escalate_to_proposal`` and we return immediately so the
         caller can switch to :func:`run_proposal` — REFINE never silently widens.
      3. Layer 5b (:func:`retrospective_validate`, reused via a thin
         :class:`StrategyProposal` wrapper around ``refine.strategy_text``):
         ``"reconsider"`` -> ``low_coverage``.
      4. Knowledge inheritance — FULL inherit + conflict cleanup
         (:func:`refine_apply_cleanup`, modify/delete-only): the validated rules
         are the parent rules with the conflicting-rule cleanup applied.
      5. Layer 5c (``rollout_validate_fn``): PASS -> a new REFINE
         :class:`TreeNode` with ``refine_count = parent.refine_count + 1``. FAIL ->
         archive (origin ``refine_failed_rollout``).

    Never raises.
    """
    from css.proposal.derivation import StrategyProposal, retrospective_validate
    from css.proposal.inheritance import refine_apply_cleanup
    from css.proposal.refine import derive_refine

    # Layer 4 — shared root-cause attribution.
    root_causes = _attribute(node, l1_signals, library, optimizer_client, cfg=cfg)
    if not root_causes:
        return ProposalOutcome(
            success=False,
            operation="REFINE",
            reason="no_root_cause: Layer 4 produced no evidence-complete diagnosis",
        )

    top = root_causes[0]

    # Layer 5a — derive a LOCAL refine; the gate may reject -> escalate to PROPOSAL.
    refine, gate_reason = derive_refine(
        optimizer_client, top, node.strategy, node.rules, cfg=cfg, max_changed=2
    )
    if refine is None:
        # The local edit is not admissible (over-broad, structural, escalated, or
        # unparseable). The caller escalates to PROPOSAL on this reason.
        return ProposalOutcome(
            success=False,
            operation="REFINE",
            reason=gate_reason or "refine_gate_failed:escalate_to_proposal",
        )

    # Layer 5b — reuse retrospective_validate via a StrategyProposal wrapper.
    targeted = list(top.pattern_ids)
    proposal_view = StrategyProposal(
        strategy_text=refine.strategy_text,
        rationale="REFINE local edit of subsections: " + ", ".join(refine.changed_subsections),
        root_cause=top,
        targeted_pattern_ids=targeted,
    )
    validation = retrospective_validate(
        optimizer_client,
        proposal_view,
        success_results,
        persistent_fail_groups,
        cfg=cfg,
    )
    if validation.verdict == "reconsider":
        return ProposalOutcome(
            success=False,
            operation="REFINE",
            validation=validation,
            reason=f"low_coverage: coverage {validation.coverage:.3f} < "
            f"coverage_high {cfg.coverage_high:.2f}; reconsider root cause",
        )

    # Knowledge inheritance — FULL inherit + conflict cleanup (modify/delete only).
    # refine_apply_cleanup asserts is_cleanup_only internally; the cleanup Patch is
    # sanitized addition-free by derive_refine, so this cannot raise here.
    cleaned_rules, _reports = refine_apply_cleanup(node.rules, refine.rules_cleanup)

    # Layer 5c — injected rollout validation on the validated (cleaned) rules.
    occ_before, occ_after, rollout_error = _safe_rollout(
        rollout_validate_fn,
        refine.strategy_text,
        cleaned_rules,
        targeted,
    )

    if not rollout_error and _rollout_passed(occ_before, occ_after):
        new_node = _build_node(
            node,
            new_node_id=new_node_id,
            branch_type="REFINE",
            strategy=refine.strategy_text,
            rules=cleaned_rules,
            refine_count=node.refine_count + 1,
            epoch=epoch,
        )
        return ProposalOutcome(
            success=True,
            operation="REFINE",
            new_node=new_node,
            validation=validation,
            occurrence_before=occ_before,
            occurrence_after=occ_after,
            reason=f"rollout_pass: target occurrence {occ_before} -> {occ_after}",
        )

    # Rollout FAIL -> negative archive (origin refine_failed_rollout).
    archived = _archive_failed_rollout(
        archive,
        embedder,
        strategy_snapshot=refine.strategy_text,
        origin="refine_failed_rollout",
        root_cause=top,
        occ_before=occ_before,
        occ_after=occ_after,
        epoch=epoch,
        source_node_id=node.node_id,
        rollout_error=rollout_error,
    )
    return ProposalOutcome(
        success=False,
        operation="REFINE",
        archived=archived,
        validation=validation,
        occurrence_before=occ_before,
        occurrence_after=occ_after,
        reason=(
            f"rollout_error: {rollout_error}" if rollout_error
            else f"rollout_fail: target occurrence {occ_before} -> {occ_after} "
            f"(no significant decrease)"
        ),
    )


# ── Build / safety helpers ────────────────────────────────────────────────────

def _build_node(
    parent: "TreeNode",
    *,
    new_node_id: str,
    branch_type: str,
    strategy: str,
    rules: str,
    refine_count: int,
    epoch: int,
) -> "TreeNode":
    """Construct the accepted child node (PROPOSAL or REFINE).

    The new node is a fresh strategy under investigation: it carries the derived
    ``strategy`` (L1) and the inherited/cleaned ``rules`` (L0), is parented to the
    source node, and starts its own optimization state (empty step buffer / pattern
    library, created at ``epoch``). REFINE threads ``refine_count`` forward for the
    K-escalation budget; PROPOSAL leaves it at the parent's value.
    """
    from css.data.tree import TreeNode

    return TreeNode(
        node_id=new_node_id,
        branch_type=branch_type,  # type: ignore[arg-type]
        parent_id=parent.node_id,
        strategy=strategy,
        rules=rules,
        refine_count=refine_count,
        created_epoch=epoch,
    )


def _safe_rollout(
    rollout_validate_fn: RolloutValidateFn,
    strategy_text: str,
    rules_text: str,
    targeted_pattern_ids: "list[str]",
) -> "tuple[float, float, str]":
    """Invoke the injected 5c callback, degrading to a non-pass on failure.

    Returns ``(occ_before, occ_after, error)``. On success ``error`` is ``""``. If
    the callback raises or returns a malformed value we return ``(0.0, 0.0, msg)``
    — a non-pass (so the candidate is not committed) but with a non-empty ``error``
    so the caller can record an INFRASTRUCTURE failure in the archive distinctly
    from a legitimate measured no-decrease (which would also be 0.0 -> 0.0).
    """
    try:
        result = rollout_validate_fn(strategy_text, rules_text, list(targeted_pattern_ids))
        occ_before, occ_after = result
        return float(occ_before), float(occ_after), ""
    except Exception as exc:  # noqa: BLE001
        return 0.0, 0.0, f"rollout callback error: {type(exc).__name__}: {exc}"
