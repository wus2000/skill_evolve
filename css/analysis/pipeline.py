"""Analysis pipeline orchestrator — Phase 4 top level (design §4.3 / D4).

The analysis stack turns a single epoch's raw rollouts into the longitudinal
signal that drives the rest of CSS. This module is the thin orchestrator that
wires the three layers together for one node, one epoch:

  * **Layer 1** (:func:`css.analysis.layer1.run_layer1`) — open-ended,
    per-trajectory cognitive annotation plus same-task contrastive divergence.
    Produces a flat stream of :class:`~css.data.pattern.Observation` records
    (stamped with this ``node_id`` / ``epoch``) and a list of
    :class:`~css.rollout.contrastive.ContrastiveDivergence`.
  * **Layer 2** (:func:`css.analysis.cluster.build_or_update_library`) — embeds
    the new observations, incrementally matches them to the node's existing
    patterns, clusters the residue, LLM-refines each cluster into a stable
    :class:`~css.data.pattern.PatternRecord`, and re-pairs failure↔success
    counterparts. Mutates ``node.pattern_records`` in place.
  * **Layer 3** (:func:`css.analysis.longitudinal.record_epoch_occurrences` and
    :func:`css.analysis.longitudinal.detect_l1_signals`) — appends this epoch's
    per-pattern occurrence point and then asks the EXISTING Phase-1 predicate
    which active failure patterns now qualify as L1 signals.

The orchestrator does no heavy work itself: every layer it calls keeps its own
heavy dependencies (sklearn / sentence-transformers / faiss) lazy, so importing
:mod:`css.analysis.pipeline` is cheap and network-free. Only the standard-library
and Phase-1 dataclass imports live at module top.

Design references: ``design_final_en.md`` §4.3 Layer 1-3 and
``training_mechanism_v6.md`` D4.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from css.analysis.cluster import build_or_update_library
from css.analysis.layer1 import run_layer1
from css.analysis.longitudinal import (
    detect_l1_signals,
    record_epoch_occurrences,
)

if TYPE_CHECKING:  # pragma: no cover - type-only imports
    from css.analysis.embedding import Embedder
    from css.config import CSSConfig
    from css.data.pattern import PatternRecord
    from css.data.rollout import TaskRolloutGroup
    from css.data.tree import TreeNode
    from css.model.client import LLMClient
    from css.rollout.contrastive import ContrastiveDivergence


@dataclass
class AnalysisResult:
    """Summary of one node-epoch analysis pass.

    ``l1_signals`` are the active failure patterns that, at this epoch, satisfy
    the L1-signal predicate (no declining trend + L0 saturated + affecting a
    significant task fraction) — the trigger set for L1 interventions
    (PROPOSAL / REFINE). ``divergences`` are the same-task contrastive findings
    from Layer 1 (carried through for downstream use). The node's pattern
    library has already been mutated in place by the time this result is built.
    """

    epoch: int
    n_observations: int
    n_patterns: int
    l1_signals: list["PatternRecord"] = field(default_factory=list)
    divergences: list["ContrastiveDivergence"] = field(default_factory=list)


def run_analysis_epoch(
    client: "LLMClient",
    embedder: "Embedder",
    node: "TreeNode",
    groups: list["TaskRolloutGroup"],
    *,
    epoch: int,
    l0_saturated: bool,
    cfg: "CSSConfig",
) -> "AnalysisResult":
    """Run Layers 1→3 for one node over one epoch's rollout groups.

    Steps (per the Phase-4 contract):

    1. **Layer 1** — annotate every rollout and every same-task contrastive
       pair, stamping ``node.node_id`` / ``epoch`` / polarity onto each
       observation.
    2. **Layer 2** — fold the new observations into ``node.pattern_records``
       (incremental match → cluster residue → refine → add → pair counterparts),
       mutating the library in place.
    3. **Layer 3** — record this epoch's per-pattern occurrence point
       (``n_tasks = len(groups)``), then detect the L1 signals via the existing
       :meth:`PatternLibrary.l1_signals` predicate.

    Returns an :class:`AnalysisResult`; ``node.pattern_records`` is updated in
    place as a side effect.
    """
    # ── Layer 1: trajectories → observations + contrastive divergences ──────
    observations, divergences = run_layer1(
        client,
        groups,
        node_id=node.node_id,
        epoch=epoch,
        cfg=cfg,
    )

    # ── Layer 2: observations → stable pattern library (mutates in place) ───
    build_or_update_library(
        client,
        embedder,
        node.pattern_records,
        observations,
        cfg=cfg,
    )

    # ── Layer 3: longitudinal occurrence + L1-signal detection ──────────────
    record_epoch_occurrences(
        node.pattern_records,
        observations,
        epoch=epoch,
        n_tasks=len(groups),
    )
    signals = detect_l1_signals(
        node.pattern_records,
        cfg=cfg,
        l0_saturated=l0_saturated,
    )

    return AnalysisResult(
        epoch=epoch,
        n_observations=len(observations),
        n_patterns=len(node.pattern_records),
        l1_signals=signals,
        divergences=divergences,
    )
