"""Layer 3 (longitudinal) tracking for the analysis pipeline.

Layer 3 is the temporal half of the analysis stack: where Layer 1 annotates how
the agent *thinks* in a single trajectory and Layer 2 clusters those
observations into stable :class:`~css.data.pattern.PatternRecord` patterns, Layer
3 watches each pattern *across epochs* and decides which failure patterns have
become **L1 signals** — i.e. cognitive problems that survive L0 (rule-level)
optimization and therefore demand a thinking-level (L1) intervention.

This module is deliberately thin and deterministic. It does three things:

  * :func:`record_epoch_occurrences` — append one
    :class:`~css.data.pattern.OccurrencePoint` per active pattern for the current
    epoch, where the occurrence *rate* is the fraction of this epoch's tasks that
    exhibit the pattern (distinct ``task_id`` count / ``n_tasks``). This is the
    longitudinal series :meth:`PatternRecord.occurrence_trend` reads.
  * :func:`detect_l1_signals` — delegate to the EXISTING Phase-1 predicate
    :meth:`PatternLibrary.l1_signals` (design Layer 3). Layer 3 does not reinvent
    the signal definition; it only wires the config-driven knobs in.
  * :func:`merge_duplicate_patterns` — a periodic global de-duplication pass.
    Independent epochs can mint two ``PatternRecord``\s for the same underlying
    cognitive pattern (Layer 2 runs incrementally); this finds near-duplicate
    same-polarity patterns by label-based Jaccard similarity, optionally confirms
    each candidate with the optimizer LLM, and folds the duplicate's observations
    and occurrence history into a single survivor.

Design references: ``design_final_en.md`` §4.3 Layer 3 and
``training_mechanism_v6.md`` D4. This module performs no heavy / network-touching
imports of its own.
"""
from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from css.data.pattern import (
    Observation,
    OccurrencePoint,
    PatternLibrary,
    PatternRecord,
)

if TYPE_CHECKING:
    from css.config import CSSConfig
    from css.model.client import LLMClient


# ──────────────────────────────────────────────────────────────────────────
# Layer 3a — per-epoch occurrence recording
# ──────────────────────────────────────────────────────────────────────────
def record_epoch_occurrences(
    library: "PatternLibrary",
    observations: list["Observation"],
    *,
    epoch: int,
    n_tasks: int,
) -> None:
    """Append this epoch's :class:`OccurrencePoint` to every active pattern.

    For each active pattern, the support is the number of *distinct* ``task_id``
    values among the pattern's observations stamped with ``epoch == epoch``. The
    occurrence rate is that count divided by ``max(n_tasks, 1)`` (so an empty
    epoch never divides by zero). A pattern that produced no observation this
    epoch still gets an explicit zero-rate point, keeping every active pattern's
    ``occurrence_history`` aligned on the same epoch axis — which is what
    :meth:`PatternRecord.occurrence_trend` (and hence the L1 predicate) reads.

    ``observations`` is accepted for interface symmetry with the rest of the
    pipeline, but the authoritative source of a pattern's observations is the
    pattern record itself (Layer 2 attaches each matched observation to its
    record). We count over ``pattern.observations`` so the rate reflects what is
    actually attributed to the pattern, not merely what was passed in.
    """
    del observations  # counted via each pattern's attached observations
    denom = max(int(n_tasks), 1)
    for pattern in library.active():
        task_ids = {
            obs.task_id
            for obs in pattern.observations
            if obs.epoch == epoch and obs.task_id
        }
        support_count = len(task_ids)
        rate = support_count / denom
        pattern.occurrence_history.append(
            OccurrencePoint(
                epoch=epoch,
                occurrence_rate=rate,
                support_count=support_count,
                n_tasks=int(n_tasks),
            )
        )


# ──────────────────────────────────────────────────────────────────────────
# Layer 3b — L1-signal detection (delegates to the Phase-1 predicate)
# ──────────────────────────────────────────────────────────────────────────
def detect_l1_signals(
    library: "PatternLibrary",
    *,
    cfg: "CSSConfig",
    l0_saturated: bool,
) -> list["PatternRecord"]:
    """Return the active failure patterns that currently qualify as L1 signals.

    This is a thin wrapper over the EXISTING, authoritative predicate
    :meth:`PatternLibrary.l1_signals`. Per lead Q2 the default uses the
    authoritative-doc reading (L0 saturation *is* the remedy-resistance
    evidence), i.e. ``require_remedy_count=False`` — which is the library
    default. The config supplies the trend window (``cfg.W``) and the
    "significant fraction" threshold (``cfg.l1_min_task_fraction``).
    """
    return library.l1_signals(
        trend_window=cfg.W,
        l0_saturated=l0_saturated,
        min_task_fraction=cfg.l1_min_task_fraction,
    )


# ──────────────────────────────────────────────────────────────────────────
# Layer 3c — periodic duplicate-pattern merge
# ──────────────────────────────────────────────────────────────────────────
def _pattern_text(pattern: "PatternRecord") -> str:
    """The text used to embed a pattern for duplicate detection.

    Combines the LLM-unified name and description (and cognitive aspect when
    present) so semantically equivalent patterns minted in different epochs land
    close together in embedding space.
    """
    parts = [pattern.name or "", pattern.description or ""]
    if pattern.cognitive_aspect:
        parts.append(pattern.cognitive_aspect)
    return " ".join(p for p in parts if p).strip()


def _confirm_merge_llm(
    client: "LLMClient",
    survivor: "PatternRecord",
    duplicate: "PatternRecord",
) -> bool:
    """Ask the optimizer whether two patterns describe the same cognition.

    Returns ``True`` (merge) when the model affirms, and is *fail-open*: any
    parsing failure or client error defaults to merging, because the candidate
    pair already cleared a high embedding-similarity bar and same-polarity gate.
    A ``False`` is only returned when the model explicitly declines.
    """
    system = (
        "You are reviewing a cognitive-pattern library for duplicates. Two "
        "pattern records were created independently. Decide whether they "
        "describe the SAME underlying way the agent thinks (its cognitive "
        "aspect), not merely a similar topic. Respond with ONLY a JSON object "
        '{"same": true|false} — no prose, no markdown fences.'
    )
    user = (
        "Pattern A:\n"
        f"  name: {survivor.name}\n"
        f"  cognitive_aspect: {survivor.cognitive_aspect}\n"
        f"  description: {survivor.description}\n\n"
        "Pattern B:\n"
        f"  name: {duplicate.name}\n"
        f"  cognitive_aspect: {duplicate.cognitive_aspect}\n"
        f"  description: {duplicate.description}\n"
    )
    try:
        text, _usage = client.complete_optimizer(system, user, max_tokens=128)
    except Exception:
        return True
    return _parse_same_flag(text)


def _parse_same_flag(text: str) -> bool:
    """Parse a ``{"same": bool}`` decision from noisy LLM output (fail-open)."""
    if not text:
        return True
    candidates: list[str] = []
    for m in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.DOTALL):
        candidates.append(m.group(1).strip())
    candidates.append(text.strip())
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        candidates.append(m.group(0))
    for cand in candidates:
        if not cand:
            continue
        try:
            obj = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict) and "same" in obj:
            return bool(obj["same"])
    return True


def _merge_occurrence_history(
    survivor: "PatternRecord", duplicate: "PatternRecord"
) -> None:
    """Recompute ``survivor``'s occurrence history per epoch after a merge.

    The caller has already folded ``duplicate``'s observations into
    ``survivor.observations``, so the correct support for each epoch is the count
    of DISTINCT ``task_id`` actually attached to the survivor for that epoch — NOT
    the sum of the two records' stored supports (which would double-count a task
    observed by both records in the same epoch). The ``n_tasks`` denominator is
    the shared task-pool size for that epoch (max of the two recorded values).
    """
    n_tasks_by_epoch: dict[int, int] = {}
    for pt in list(survivor.occurrence_history) + list(duplicate.occurrence_history):
        n_tasks_by_epoch[pt.epoch] = max(n_tasks_by_epoch.get(pt.epoch, 0), pt.n_tasks)

    tasks_by_epoch: dict[int, set[str]] = {}
    for obs in survivor.observations:
        tasks_by_epoch.setdefault(obs.epoch, set()).add(obs.task_id)

    merged: list[OccurrencePoint] = []
    for epoch in sorted(set(n_tasks_by_epoch) | set(tasks_by_epoch)):
        support = len(tasks_by_epoch.get(epoch, set()))
        n_tasks = n_tasks_by_epoch.get(epoch, 0)
        merged.append(
            OccurrencePoint(
                epoch=epoch,
                occurrence_rate=min(support / max(n_tasks, 1), 1.0),
                support_count=support,
                n_tasks=n_tasks,
            )
        )
    survivor.occurrence_history = merged


def merge_duplicate_patterns(
    client: "LLMClient",
    library: "PatternLibrary",
    *,
    sim_threshold: float = 0.85,
) -> int:
    """Periodically merge independently-created duplicate patterns.

    Every same-polarity pair whose label-based Jaccard similarity is
    ``>= sim_threshold`` is a merge candidate. Each candidate is optionally
    confirmed with the optimizer LLM; on confirmation the lexicographically-
    smaller ``pattern_id`` is kept as the survivor and the other's observations
    and occurrence history are folded in, the duplicate's ``status`` is set to
    ``"merged"``, and its observations' ``pattern_id`` is repointed at the
    survivor.

    Returns the number of merges performed. Deterministic given a deterministic
    client: candidates are processed in a stable id-sorted order, and an
    already-merged pattern is skipped so each duplicate is folded once.
    """
    active = library.active()
    if len(active) < 2:
        return 0

    # Stable order so the survivor choice and processing are deterministic.
    active.sort(key=lambda p: p.pattern_id)

    # Use label-based Jaccard similarity instead of embedding cosine.
    # This avoids the same-domain embedding collapse that plagues observation
    # clustering, and pattern-level labels are even more descriptive.
    from css.analysis.label_grouping import find_merge_candidates

    merged_into: dict[str, str] = {}  # duplicate_id -> survivor_id (transitive)

    def resolve(pid: str) -> str:
        seen: set[str] = set()
        while pid in merged_into and pid not in seen:
            seen.add(pid)
            pid = merged_into[pid]
        return pid

    from concurrent.futures import ThreadPoolExecutor, as_completed

    candidates = find_merge_candidates(active, threshold=sim_threshold)

    # Parallel LLM confirmation for all candidate pairs.
    confirm_results: dict[tuple[int, int], bool] = {}
    with ThreadPoolExecutor(max_workers=max(1, len(candidates))) as pool:
        futures = {
            pool.submit(_confirm_merge_llm, client, active[i], active[j]): (i, j)
            for i, j in candidates
        }
        for fut in as_completed(futures):
            pair = futures[fut]
            try:
                confirm_results[pair] = fut.result()
            except Exception:
                confirm_results[pair] = False

    # Sequential merge using pre-computed confirmations.
    n_merges = 0
    for i, j in candidates:
        a, b = active[i], active[j]
        if a.status != "active" or b.status != "active":
            continue
        survivor_id = resolve(a.pattern_id)
        survivor = library.get(survivor_id) or a
        if survivor.status != "active" or survivor.pattern_id == b.pattern_id:
            continue
        if not confirm_results.get((i, j), False):
            continue
        for obs in b.observations:
            obs.pattern_id = survivor.pattern_id
        survivor.observations.extend(b.observations)
        _merge_occurrence_history(survivor, b)
        survivor.remedy_resistance = max(
            survivor.remedy_resistance, b.remedy_resistance
        )
        b.status = "merged"
        merged_into[b.pattern_id] = survivor.pattern_id
        n_merges += 1
    return n_merges
