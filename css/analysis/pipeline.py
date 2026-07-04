"""Analysis pipeline orchestrator.

The analysis stack turns a single epoch's raw rollouts into the behavioral
pattern landscape that feeds L1 paradigm design. This module is the thin
orchestrator that wires the three layers together for one node, one epoch:

  * **Layer 1** (:func:`css.analysis.layer1.run_layer1`) — per-trajectory
    behavioral arc annotation plus same-task contrastive arc divergence.
    Produces a flat stream of :class:`~css.data.pattern.Observation` records
    (stamped with this ``node_id`` / ``epoch``) and a list of
    :class:`~css.rollout.contrastive.ContrastiveDivergence`.
  * **Layer 2** (:func:`css.analysis.cluster.build_or_update_library`) — embeds
    the new observations, incrementally matches them to the node's existing
    behavioral patterns, clusters the residue, LLM-refines each cluster into a
    stable :class:`~css.data.pattern.PatternRecord`, and re-pairs failure↔success
    counterparts.  Mutates ``node.pattern_records`` in place.
  * **Layer 3** (:func:`css.analysis.longitudinal.record_epoch_occurrences` and
    :func:`css.analysis.longitudinal.detect_l1_signals`) — appends this epoch's
    per-pattern occurrence point and detects L1 signals (persistent failure
    patterns under L0 saturation).

The orchestrator does no heavy work itself: every layer it calls keeps its own
heavy dependencies (sklearn / sentence-transformers / faiss) lazy, so importing
:mod:`css.analysis.pipeline` is cheap and network-free.
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


def analysis_env_context(env: object, cfg: "CSSConfig") -> str:
    """Env-context text for Layer-1 prompts.

    Empty unless ``cfg.analysis_env_context`` is enabled AND the env provides
    ``action_space_description()`` — the per-env launcher opts in, so runs
    already in flight keep their legacy prompt bytes.
    """
    if not getattr(cfg, "analysis_env_context", False):
        return ""
    fn = getattr(env, "action_space_description", None)
    if not callable(fn):
        return ""
    try:
        return str(fn() or "")
    except Exception:  # noqa: BLE001 — context is best-effort, never fatal
        return ""


def run_analysis_epoch(
    client: "LLMClient",
    node: "TreeNode",
    groups: list["TaskRolloutGroup"],
    *,
    epoch: int,
    l0_saturated: bool,
    cfg: "CSSConfig",
    out_dir: str = "",
    env_context: str = "",
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
            env_context=env_context,
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


# ── Rendering helpers for paradigm design prompts ──────────────────────────

def render_pattern_landscape(
    library: "PatternLibrary",
    divergences: "list[ContrastiveDivergence]",
    *,
    max_patterns: int = 20,
    max_evidence_len: int = 200,
) -> str:
    """Render the PatternLibrary as structured text for paradigm design prompts.

    Includes: behavioral patterns sorted by support count (descending), with
    their polarity, counterpart pairings, and representative evidence excerpts.
    Capped at ``max_patterns`` to stay within prompt budget.
    """
    from css.data.pattern import PatternLibrary

    if library is None or len(library) == 0:
        return "(no behavioral patterns discovered yet)"

    all_patterns = sorted(
        [p for p in library.patterns.values() if p.status == "active"],
        key=lambda p: p.support_count,
        reverse=True,
    )

    failure_patterns = [p for p in all_patterns if p.polarity == "failure"]
    success_patterns = [p for p in all_patterns if p.polarity == "success"]
    neutral_patterns = [p for p in all_patterns if p.polarity == "neutral"]

    lines: list[str] = []
    lines.append(f"Total patterns: {len(all_patterns)} "
                 f"(failure: {len(failure_patterns)}, success: {len(success_patterns)}, "
                 f"neutral: {len(neutral_patterns)})")

    def _render_pattern(p, label: str) -> str:
        parts = [f"  [{p.pattern_id}] {p.name} ({label}, {p.support_count} observations)"]
        if p.description:
            parts.append(f"    Description: {p.description[:300]}")
        cpart = ""
        if p.counterpart_id:
            cp = library.patterns.get(p.counterpart_id)
            if cp:
                cpart = f"    Counterpart ({cp.polarity}): {cp.name}"
        if cpart:
            parts.append(cpart)
        # Representative evidence from the most significant observations
        top_obs = sorted(
            p.observations,
            key=lambda o: 0 if o.significance == "critical" else 1,
        )[:2]
        for o in top_obs:
            ev = (o.evidence or "")[:max_evidence_len]
            parts.append(f"    Evidence: {ev}")
        return "\n".join(parts)

    if failure_patterns:
        lines.append("\n### Failure-associated behavioral patterns (sorted by frequency)")
        for p in failure_patterns[:max_patterns // 2]:
            lines.append(_render_pattern(p, "FAILURE"))

    if success_patterns:
        lines.append("\n### Success-associated behavioral patterns")
        for p in success_patterns[:max_patterns // 2]:
            lines.append(_render_pattern(p, "SUCCESS"))

    if neutral_patterns[:5]:
        lines.append("\n### Neutral patterns")
        for p in neutral_patterns[:5]:
            lines.append(_render_pattern(p, "neutral"))

    # Contrastive divergences summary
    if divergences:
        systematic = [d for d in divergences if d.is_systematic]
        lines.append(f"\n### Contrastive divergences (same-task success/failure pairs)")
        lines.append(f"  Total: {len(divergences)} pairs analyzed, "
                     f"{len(systematic)} systematic")
        for d in systematic[:8]:
            level = getattr(d, "divergence_level", "unknown")
            lines.append(
                f"  Task {d.task_id}: {d.divergence_point[:200]} "
                f"[{level}]"
            )

    return "\n".join(lines)


def render_representative_trajectories(
    library: "PatternLibrary",
    all_results: "dict",
    *,
    max_exemplars: int = 6,
    tool_trunc: int = 300,
) -> str:
    """Select and render representative trajectories from analysis products.

    Selection is analysis-driven: picks trajectories that exemplify the most
    significant behavioral patterns (both failure and success). Not random
    sampling — grounded in the analysis pipeline's pattern discovery.

    ``all_results`` is a ``{(task_id, rollout_index): TaskResult}`` mapping.
    """
    from css.trajectory import format_trajectory

    if library is None or len(library) == 0:
        return "(no patterns available for representative selection)"

    # Pick the most significant patterns and find their exemplar trajectories
    top_patterns = sorted(
        [p for p in library.patterns.values() if p.status == "active"],
        key=lambda p: (0 if p.polarity == "failure" else 1,
                       0 if any(o.significance == "critical" for o in p.observations) else 1,
                       -p.support_count),
    )

    selected: list[tuple[str, str]] = []  # (task_id, rollout_index)
    seen_tasks: set[str] = set()
    blocks: list[str] = []

    for pattern in top_patterns:
        if len(blocks) >= max_exemplars:
            break
        # Pick the most significant observation from this pattern
        best_obs = sorted(
            pattern.observations,
            key=lambda o: (0 if o.significance == "critical" else 1),
        )
        for obs in best_obs:
            key = (obs.task_id, obs.rollout_index)
            if obs.task_id in seen_tasks:
                continue
            result = all_results.get(key)
            if result is None:
                continue
            seen_tasks.add(obs.task_id)
            outcome = "PASSED" if result.passed else "FAILED"
            traj_text = format_trajectory(result.messages, tool_trunc=tool_trunc)
            desc = getattr(result, "task_description", "") or ""
            blocks.append(
                f"### Task {obs.task_id} ({outcome}) — exemplifies pattern "
                f"'{pattern.name}'\n"
                f"{f'Task: {desc}' + chr(10) if desc else ''}"
                f"{traj_text}"
            )
            break

    if not blocks:
        return "(no representative trajectories available)"

    return "\n\n---\n\n".join(blocks)
