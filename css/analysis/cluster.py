"""Layer 2 cross-trajectory clustering (analysis pipeline, Phase 4).

Layer 1 produces a stream of :class:`Observation` records — one LLM-named
cognitive observation per (trajectory, finding). Layer 2 turns that stream into
a longitudinally-stable :class:`PatternLibrary`:

  * 2a  embed each observation (``what`` + named ``cognitive_aspect``) and
        DBSCAN pre-group them. The clustering *unit* is the observation, not the
        trajectory: the same trajectory can contribute observations to several
        cognitive patterns, and one pattern is built from observations spanning
        many trajectories.
  * 2b  per-cluster LLM refinement: unify a single pattern *name* +
        *description* + *polarity* + *cognitive_aspect* from the member
        observations. Noise points (DBSCAN label ``-1``) are dropped here — they
        lack the cross-trajectory recurrence a pattern requires; they may match
        an existing pattern on a later epoch via :func:`incremental_match`.
  * 2c  cross-cluster failure↔success "counterpart" pairing: a failure pattern
        and the success pattern describing the *same* cognitive aspect are linked
        (``counterpart_id`` both ways). The success side is the constructive
        target a later REFINE/PROPOSAL edit systematizes.

Across epochs the library grows *incrementally*: new observations are first
matched against existing pattern centroids by embedding retrieval
(:func:`incremental_match`); only the unmatched residue is clustered afresh.
This keeps ``pattern_id`` stable for a recurring pattern so Layer 3 can track its
per-epoch occurrence rate.

Heavy dependencies (sklearn / sentence-transformers / faiss) are imported lazily;
this module imports cleanly with only numpy present, and all clustering is driven
through the injected :class:`Embedder` (a :class:`StubEmbedder` in tests).

Design references: ``design_final_en.md`` §4.3 Layer 2 and
``training_mechanism_v6.md`` D4 / D8.
"""
from __future__ import annotations

import json
import re
from typing import Any

import numpy as np

from css.data.pattern import (
    Observation,
    PatternLibrary,
    PatternRecord,
)
from css.analysis.embedding import Embedder, cosine_similarity

# ──────────────────────────────────────────────────────────────────────────
# Layer 2b prompt (per-cluster unification). Open-ended: the LLM names the
# pattern; we never enumerate fixed cognitive dimensions.
# ──────────────────────────────────────────────────────────────────────────
_REFINE_SYSTEM = (
    "You are a cognitive-pattern analyst. You are given several independent "
    "observations of HOW an agent thinks, drawn from different task trajectories "
    "but pre-grouped because they appear to describe the SAME underlying "
    "cognitive behavior. Your job is to unify them into ONE pattern.\n\n"
    "Do NOT invent a behavior that is not supported by the observations. Name the "
    "pattern by the cognitive behavior it captures (e.g. how the agent frames a "
    "problem, verifies assumptions, recovers from error, manages attention) — "
    "not by the surface task. Decide its polarity from the observations' "
    "consequences: 'failure' if the behavior tends to cause poor outcomes, "
    "'success' if it tends to cause good outcomes, else 'neutral'.\n\n"
    "Respond with ONE JSON object and nothing else:\n"
    '{"name": "<short pattern name>", "description": "<1-2 sentence description '
    'of the recurring cognitive behavior>", "cognitive_aspect": "<the named '
    'cognitive aspect>", "polarity": "failure|success|neutral"}'
)

_COUNTERPART_SYSTEM = (
    "You are a cognitive-pattern analyst. You are given a list of cognitive "
    "patterns, each with an id, name, polarity and cognitive_aspect. Pair each "
    "FAILURE pattern with the SUCCESS pattern that describes the SAME cognitive "
    "aspect handled well (its constructive counterpart), when such a pair "
    "clearly exists. A pattern may appear in at most one pair.\n\n"
    "Respond with ONE JSON object and nothing else:\n"
    '{"pairs": [{"failure_id": "<id>", "success_id": "<id>"}, ...]}\n'
    "Return an empty list if there are no clear counterpart pairs."
)


# ──────────────────────────────────────────────────────────────────────────
# JSON parsing helper (StubLLMClient-friendly, tolerant of surrounding prose)
# ──────────────────────────────────────────────────────────────────────────
def _parse_json_object(text: str) -> dict[str, Any]:
    """Extract the first balanced top-level JSON object from ``text``.

    Tolerant of code fences and surrounding prose; returns ``{}`` if nothing
    parseable is found so callers can fall back to a deterministic default.
    """
    if not text:
        return {}
    # Strip common code-fence wrappers.
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    # Scan for the first balanced {...} block.
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    blob = text[start : i + 1]
                    try:
                        obj = json.loads(blob)
                        if isinstance(obj, dict):
                            return obj
                    except (json.JSONDecodeError, ValueError):
                        break
        start = text.find("{", start + 1)
    return {}


def _obs_text(obs: Observation) -> str:
    """The text embedded/clustered for an observation: what + cognitive aspect."""
    return f"{obs.what} {obs.cognitive_aspect}".strip()


def _normalize_polarity(value: Any) -> str:
    v = str(value or "").strip().lower()
    if v in ("failure", "success", "neutral"):
        return v
    return "neutral"


def _vote_polarity(observations: list[Observation]) -> str:
    """Fallback polarity for a cluster: majority of member observation polarity."""
    counts = {"failure": 0, "success": 0, "neutral": 0}
    for o in observations:
        counts[_normalize_polarity(o.polarity)] += 1
    # Prefer a decisive failure/success over neutral on ties.
    if counts["failure"] >= counts["success"] and counts["failure"] > 0:
        if counts["failure"] >= counts["neutral"]:
            return "failure"
    if counts["success"] > 0 and counts["success"] >= counts["neutral"]:
        return "success"
    if counts["failure"] > counts["success"]:
        return "failure"
    if counts["success"] > counts["failure"]:
        return "success"
    return "neutral"


# ──────────────────────────────────────────────────────────────────────────
# 2a — embedding + DBSCAN pre-grouping
# ──────────────────────────────────────────────────────────────────────────
def embed_observations(
    embedder: "Embedder", observations: list["Observation"]
) -> "np.ndarray":
    """Embed each observation's (``what`` + ``cognitive_aspect``) text.

    The resulting unit vector is cached onto ``Observation.embedding`` (as a
    plain ``list[float]`` for JSON round-trip) and the full ``(N, dim)`` matrix
    is returned. An empty input yields a ``(0, dim)`` matrix.
    """
    if len(observations) == 0:
        return np.zeros((0, embedder.dim), dtype=np.float32)
    texts = [_obs_text(o) for o in observations]
    vectors = np.asarray(embedder.embed(texts), dtype=np.float32)
    for obs, vec in zip(observations, vectors):
        obs.embedding = [float(x) for x in vec]
    return vectors


def _adaptive_eps(vectors: "np.ndarray", min_samples: int) -> float:
    """Pick a cosine-distance ``eps`` via a k-distance elbow heuristic.

    For each point we take its distance to the ``k``-th nearest neighbor
    (``k = min_samples``), sort those ascending, and locate the elbow as the
    point of maximum gap in the sorted curve. The eps is set just past that
    knee. Falls back to a sane constant when there are too few points.
    """
    n = vectors.shape[0]
    if n <= min_samples:
        return 0.5
    # Cosine distance matrix on normalized rows: 1 - inner product.
    sims = np.clip(vectors @ vectors.T, -1.0, 1.0)
    dists = 1.0 - sims
    np.fill_diagonal(dists, np.inf)
    k = max(1, min(min_samples, n - 1))
    # k-th nearest distance per point (k-1 index into the sorted neighbor list).
    part = np.sort(dists, axis=1)[:, k - 1]
    k_dist = np.sort(part)
    k_dist = k_dist[np.isfinite(k_dist)]
    if k_dist.size < 2:
        return 0.5
    gaps = np.diff(k_dist)
    knee = int(np.argmax(gaps))
    # eps midway across the largest gap (just past the dense regime).
    eps = float((k_dist[knee] + k_dist[knee + 1]) / 2.0)
    if eps <= 0.0:
        eps = float(k_dist[knee]) or 0.5
    return eps


def dbscan_cluster(
    vectors: "np.ndarray", *, eps: float, min_samples: int
) -> list[int]:
    """DBSCAN cluster labels for ``vectors`` (``-1`` = noise).

    Operates in cosine space (``metric='cosine'``). When ``eps <= 0`` an
    adaptive eps is derived from a k-distance elbow (``k = min_samples``). A
    degenerate input (0 or 1 vectors) is handled without invoking sklearn.
    """
    n = 0 if vectors is None else int(np.asarray(vectors).shape[0])
    if n == 0:
        return []
    if n == 1:
        return [-1]
    vectors = np.asarray(vectors, dtype=np.float32)
    use_eps = eps
    if use_eps is None or use_eps <= 0.0:
        use_eps = _adaptive_eps(vectors, min_samples)

    from sklearn.cluster import DBSCAN  # lazy: sklearn is heavy

    model = DBSCAN(eps=use_eps, min_samples=min_samples, metric="cosine")
    labels = model.fit_predict(vectors)
    return [int(x) for x in labels]


# ──────────────────────────────────────────────────────────────────────────
# 2b — per-cluster LLM refinement → PatternRecord (temp ids; caller assigns
#      stable ids via PatternLibrary.new_pattern_id()).
# ──────────────────────────────────────────────────────────────────────────
def _centroid(observations: list["Observation"]) -> list[float] | None:
    vecs = [o.embedding for o in observations if o.embedding is not None]
    if not vecs:
        return None
    arr = np.asarray(vecs, dtype=np.float32)
    mean = arr.mean(axis=0)
    norm = float(np.linalg.norm(mean))
    if norm > 1e-12:
        mean = mean / norm
    return [float(x) for x in mean]


def _refine_one_cluster(
    client: "LLMClient", members: list["Observation"], temp_id: str
) -> "PatternRecord":
    """LLM-unify one cluster of observations into a single PatternRecord."""
    lines = []
    for i, o in enumerate(members):
        lines.append(
            f"[{i}] aspect={o.cognitive_aspect!r} | what={o.what} | "
            f"consequence={o.consequence} | polarity={o.polarity} | "
            f"significance={o.significance}"
        )
    user = (
        "Observations in this group:\n" + "\n".join(lines) + "\n\n"
        "Unify them into one cognitive pattern as the JSON object specified."
    )
    text, _usage = client.complete_optimizer(_REFINE_SYSTEM, user)
    obj = _parse_json_object(text)

    # Deterministic fallbacks keep the pipeline robust under a terse stub.
    name = str(obj.get("name") or "").strip()
    if not name:
        name = members[0].cognitive_aspect.strip() or "unnamed-pattern"
    cognitive_aspect = str(obj.get("cognitive_aspect") or "").strip()
    if not cognitive_aspect:
        cognitive_aspect = members[0].cognitive_aspect.strip()
    description = str(obj.get("description") or "").strip()
    if not description:
        description = members[0].what.strip()
    if "polarity" in obj:
        polarity = _normalize_polarity(obj.get("polarity"))
    else:
        polarity = _vote_polarity(members)

    return PatternRecord(
        pattern_id=temp_id,
        name=name,
        description=description,
        cognitive_aspect=cognitive_aspect,
        polarity=polarity,  # type: ignore[arg-type]
        observations=list(members),
        centroid=_centroid(members),
        status="active",
    )


def refine_clusters(
    client: "LLMClient",
    observations: list["Observation"],
    labels: list[int],
) -> list["PatternRecord"]:
    """Layer 2b: build one refined :class:`PatternRecord` per non-noise cluster.

    Observations labeled ``-1`` (DBSCAN noise) are dropped — a cross-trajectory
    pattern requires recurrence, and noise points may still be picked up by
    :func:`incremental_match` on a later epoch. Pattern ids are *temporary*
    (``tmp{cluster}``); the caller assigns stable ids via the library.
    """
    if len(observations) != len(labels):
        raise ValueError(
            f"#observations ({len(observations)}) != #labels ({len(labels)})"
        )
    groups: dict[int, list[Observation]] = {}
    for obs, lab in zip(observations, labels):
        if lab < 0:
            continue
        groups.setdefault(int(lab), []).append(obs)

    records: list[PatternRecord] = []
    for lab in sorted(groups):
        members = groups[lab]
        records.append(_refine_one_cluster(client, members, f"tmp{lab}"))
    return records


# ──────────────────────────────────────────────────────────────────────────
# 2c — failure ↔ success counterpart pairing
# ──────────────────────────────────────────────────────────────────────────
def _cosine_counterparts(patterns: list["PatternRecord"]) -> None:
    """Fallback counterpart pairing by centroid cosine (used when no LLM pair)."""
    failures = [p for p in patterns if p.polarity == "failure" and not p.counterpart_id]
    successes = [p for p in patterns if p.polarity == "success" and not p.counterpart_id]
    used_success: set[str] = set()
    for f in failures:
        if f.centroid is None:
            continue
        best: PatternRecord | None = None
        best_sim = 0.5  # require a meaningful aspect overlap
        for s in successes:
            if s.pattern_id in used_success or s.centroid is None:
                continue
            sim = cosine_similarity(
                np.asarray(f.centroid, dtype=np.float32),
                np.asarray(s.centroid, dtype=np.float32),
            )
            if sim > best_sim:
                best_sim = sim
                best = s
        if best is not None:
            f.counterpart_id = best.pattern_id
            best.counterpart_id = f.pattern_id
            used_success.add(best.pattern_id)


def pair_counterparts(
    client: "LLMClient", patterns: list["PatternRecord"]
) -> None:
    """Layer 2c: set ``counterpart_id`` on failure↔success pattern pairs.

    Tries an LLM judgment first (matching by cognitive aspect), then fills any
    remaining unpaired patterns by centroid cosine similarity. Mutates the given
    patterns in place; only patterns currently without a counterpart are touched.
    """
    by_id = {p.pattern_id: p for p in patterns}
    failures = [p for p in patterns if p.polarity == "failure"]
    successes = [p for p in patterns if p.polarity == "success"]
    if failures and successes:
        catalog = "\n".join(
            f"id={p.pattern_id} | polarity={p.polarity} | name={p.name} | "
            f"aspect={p.cognitive_aspect}"
            for p in failures + successes
        )
        user = (
            "Patterns:\n" + catalog + "\n\n"
            "Pair each failure pattern with its success counterpart as the "
            "specified JSON object."
        )
        try:
            text, _usage = client.complete_optimizer(_COUNTERPART_SYSTEM, user)
            obj = _parse_json_object(text)
        except Exception:
            obj = {}
        for pair in obj.get("pairs", []) or []:
            if not isinstance(pair, dict):
                continue
            fid = str(pair.get("failure_id", ""))
            sid = str(pair.get("success_id", ""))
            f = by_id.get(fid)
            s = by_id.get(sid)
            if (
                f is not None
                and s is not None
                and f.polarity == "failure"
                and s.polarity == "success"
                and not f.counterpart_id
                and not s.counterpart_id
            ):
                f.counterpart_id = s.pattern_id
                s.counterpart_id = f.pattern_id

    # Fill any still-unpaired patterns deterministically by centroid cosine.
    _cosine_counterparts(patterns)


# ──────────────────────────────────────────────────────────────────────────
# Incremental matching of new observations to existing patterns
# ──────────────────────────────────────────────────────────────────────────
def _recompute_centroid(record: "PatternRecord") -> None:
    """Recompute the record centroid as the unit-mean of all member embeddings."""
    record.centroid = _centroid(record.observations)


def incremental_match(
    embedder: "Embedder",
    library: "PatternLibrary",
    observations: list["Observation"],
    *,
    sim_threshold: float = 0.6,
) -> tuple[list["Observation"], list["Observation"]]:
    """Attach new observations to existing patterns by centroid retrieval.

    All observations are matched against the patterns' centroids as FROZEN at the
    start of the call, so the result is independent of observation order within a
    batch (a later observation never matches a centroid already shifted by an
    earlier attachment in the same call). Each touched record's centroid is
    recomputed once at the end.

    For each observation (embedded on demand if not already cached), find the
    active pattern whose frozen centroid is most cosine-similar; if the best
    similarity is ``>= sim_threshold`` attach it (set ``obs.pattern_id``, append
    to the record), else leave it unmatched.

    Returns ``(matched, unmatched)``. When the library has no centroid-bearing
    active patterns, every observation is unmatched.
    """
    matched: list[Observation] = []
    unmatched: list[Observation] = []
    if not observations:
        return matched, unmatched

    # Ensure embeddings exist (cache onto observations).
    missing = [o for o in observations if o.embedding is None]
    if missing:
        vecs = embedder.embed([_obs_text(o) for o in missing])
        for o, v in zip(missing, vecs):
            o.embedding = [float(x) for x in v]

    candidates = [p for p in library.active() if p.centroid is not None]
    if not candidates:
        return matched, list(observations)

    # Freeze the centroids once so batch matching is order-independent.
    frozen = [
        (rec, np.asarray(rec.centroid, dtype=np.float32)) for rec in candidates
    ]
    touched: dict[str, PatternRecord] = {}

    for obs in observations:
        ovec = np.asarray(obs.embedding, dtype=np.float32)
        best: PatternRecord | None = None
        best_sim = -1.0
        for rec, cvec in frozen:
            sim = cosine_similarity(ovec, cvec)
            if sim > best_sim:
                best_sim = sim
                best = rec
        if best is not None and best_sim >= sim_threshold:
            obs.pattern_id = best.pattern_id
            best.observations.append(obs)
            touched[best.pattern_id] = best
            matched.append(obs)
        else:
            unmatched.append(obs)

    # Recompute each touched centroid exactly once, after all attachments.
    for rec in touched.values():
        _recompute_centroid(rec)
    return matched, unmatched


# ──────────────────────────────────────────────────────────────────────────
# Full Layer 2 driver
# ──────────────────────────────────────────────────────────────────────────
def build_or_update_library(
    client: "LLMClient",
    embedder: "Embedder",
    library: "PatternLibrary",
    observations: list["Observation"],
    *,
    cfg: "CSSConfig",
) -> "PatternLibrary":
    """Full Layer 2: incremental match → cluster residue → refine → add → pair.

    1. Embed the new observations and match them against existing pattern
       centroids (:func:`incremental_match`); attach the matches.
    2. DBSCAN-cluster the unmatched residue and LLM-refine each cluster into a
       :class:`PatternRecord` (:func:`refine_clusters`).
    3. Allocate a stable ``pattern_id`` for each new record via
       :meth:`PatternLibrary.new_pattern_id`, stamp it onto every member
       observation, set the centroid to the member-embedding mean, and add it.
    4. Re-pair failure↔success counterparts across all active patterns
       (:func:`pair_counterparts`).

    The library is mutated in place and also returned.
    """
    if not observations:
        return library

    # 1) embed everything once (caches onto observations), then match existing.
    embed_observations(embedder, observations)
    _matched, unmatched = incremental_match(
        embedder, library, observations, sim_threshold=0.6
    )

    # 2) cluster + refine the unmatched residue.
    if unmatched:
        residue_vecs = np.asarray(
            [o.embedding for o in unmatched], dtype=np.float32
        )
        labels = dbscan_cluster(
            residue_vecs, eps=cfg.eps_dbscan, min_samples=cfg.min_samples
        )
        new_records = refine_clusters(client, unmatched, labels)

        # 3) assign stable ids + stamp observations + finalize centroid.
        for rec in new_records:
            pid = library.new_pattern_id()
            rec.pattern_id = pid
            for obs in rec.observations:
                obs.pattern_id = pid
            rec.centroid = _centroid(rec.observations)
            library.add(rec)

    # 4) re-pair counterparts across all active patterns.
    pair_counterparts(client, library.active())
    return library
