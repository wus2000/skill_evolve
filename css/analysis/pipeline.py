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

import json
import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from css.analysis.cluster import build_or_update_library
from css.analysis.layer1 import run_layer1
from css.analysis.longitudinal import (
    detect_l1_signals,
    record_epoch_occurrences,
)

if TYPE_CHECKING:  # pragma: no cover - type-only imports
    from css.config import CSSConfig
    from css.data.pattern import PatternRecord
    from css.data.rollout import TaskRolloutGroup
    from css.data.tree import TreeNode
    from css.model.client import LLMClient
    from css.rollout.contrastive import ContrastiveDivergence

_log = logging.getLogger(__name__)


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


def _save_ckpt(path: str, data: dict) -> None:
    """Atomic JSON write (tmp + os.replace) — crash-safe checkpoint."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def _load_ckpt(path: str) -> dict | None:
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def run_analysis_epoch(
    client: "LLMClient",
    node: "TreeNode",
    groups: list["TaskRolloutGroup"],
    *,
    epoch: int,
    l0_saturated: bool,
    cfg: "CSSConfig",
    out_dir: str = "",
) -> "AnalysisResult":
    """Run Layers 1→3 for one node over one epoch's rollout groups.

    Each layer checkpoints its output so that a crash/resume skips completed
    LLM work.  Checkpoint files live under ``out_dir/`` (the analysis subdir
    for this node-round).
    """
    import time
    from css.data.pattern import Observation, PatternLibrary
    from css.rollout.contrastive import ContrastiveDivergence

    t_start = time.time()

    # ── Checkpoint paths ───────────────────────────────────────────────────
    layer1_ckpt = os.path.join(out_dir, "layer1_ckpt.json") if out_dir else ""
    layer23_ckpt = os.path.join(out_dir, "layer23_ckpt.json") if out_dir else ""

    # ── Try full resume (Layer 2+3 done) ───────────────────────────────────
    saved_23 = _load_ckpt(layer23_ckpt) if layer23_ckpt else None
    if saved_23 is not None:
        node.pattern_records = PatternLibrary.from_dict(saved_23["pattern_records"])
        signals = [
            rec for rec in node.pattern_records
            if rec.pattern_id in set(saved_23.get("l1_signal_ids", []))
        ]
        divergences = [
            ContrastiveDivergence.from_dict(d) for d in saved_23.get("divergences", [])
        ]
        _log.info("Analysis: loaded full checkpoint (Layer 1-3) — "
                  "%d observations, %d patterns, %d l1_signals",
                  saved_23["n_observations"], len(node.pattern_records), len(signals))
        return AnalysisResult(
            epoch=epoch,
            n_observations=saved_23["n_observations"],
            n_patterns=len(node.pattern_records),
            l1_signals=signals,
            divergences=divergences,
        )

    # ── Layer 1: trajectories → observations + contrastive divergences ─────
    saved_l1 = _load_ckpt(layer1_ckpt) if layer1_ckpt else None
    if saved_l1 is not None:
        observations = [Observation.from_dict(d) for d in saved_l1["observations"]]
        divergences = [ContrastiveDivergence.from_dict(d) for d in saved_l1["divergences"]]
        _log.info("Analysis: loaded Layer 1 from checkpoint — %d observations, %d divergences",
                  len(observations), len(divergences))
    else:
        observations, divergences = run_layer1(
            client,
            groups,
            node_id=node.node_id,
            epoch=epoch,
            cfg=cfg,
        )
        if layer1_ckpt:
            _save_ckpt(layer1_ckpt, {
                "observations": [o.to_dict() for o in observations],
                "divergences": [d.to_dict() for d in divergences],
            })

    n_patterns_before = len(node.pattern_records)

    # ── Layer 2: observations → stable pattern library (mutates in place) ──
    build_or_update_library(
        client,
        node.pattern_records,
        observations,
        cfg=cfg,
        out_dir=out_dir,
    )

    # ── Layer 3: longitudinal occurrence + L1-signal detection ─────────────
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

    # ── Checkpoint Layer 2+3 result ────────────────────────────────────────
    if layer23_ckpt:
        _save_ckpt(layer23_ckpt, {
            "pattern_records": node.pattern_records.to_dict(),
            "l1_signal_ids": [s.pattern_id for s in signals],
            "divergences": [d.to_dict() for d in divergences],
            "n_observations": len(observations),
        })

    result = AnalysisResult(
        epoch=epoch,
        n_observations=len(observations),
        n_patterns=len(node.pattern_records),
        l1_signals=signals,
        divergences=divergences,
    )

    if out_dir:
        _save_analysis_result(
            out_dir,
            result=result,
            n_divergences=len(divergences),
            n_patterns_before=n_patterns_before,
            elapsed_s=time.time() - t_start,
        )

    return result


def _save_analysis_result(
    out_dir: str,
    *,
    result: "AnalysisResult",
    n_divergences: int,
    n_patterns_before: int,
    elapsed_s: float,
) -> None:
    """Write ``analysis_result.json`` — never raises (auditability is best-effort)."""
    import json
    import os

    try:
        artifact = {
            "epoch": result.epoch,
            "n_observations": result.n_observations,
            "n_divergences": n_divergences,
            "n_patterns": {
                "before": n_patterns_before,
                "after": result.n_patterns,
                "new": max(0, result.n_patterns - n_patterns_before),
            },
            "l1_signals": [
                {"pattern_id": s.pattern_id, "name": s.name}
                for s in result.l1_signals
            ],
            "timing": {"elapsed_s": round(elapsed_s, 3)},
        }
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "analysis_result.json"),
                  "w", encoding="utf-8") as f:
            json.dump(artifact, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
