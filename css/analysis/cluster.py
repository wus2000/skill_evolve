"""Layer 2 cross-trajectory clustering (analysis pipeline).

Layer 1 produces a stream of :class:`Observation` records — one behavioral
arc annotation per (trajectory, finding). Layer 2 turns that stream into a
longitudinally-stable :class:`PatternLibrary` of recurring behavioral patterns:

  * 2a  match new observations against existing patterns by behavioral-pattern
        label similarity (Jaccard pre-group + LLM synonym detection via
        :mod:`css.analysis.label_grouping`), then group the unmatched residue
        by label canonicalization.
  * 2b  per-cluster LLM refinement: unify a single pattern *name* +
        *description* + *polarity* + *cognitive_aspect* from the member
        observations.  In the behavioral-paradigm design, the "cognitive_aspect"
        is now a generalizable behavioral-pattern label (action-level, not
        purely cognitive).
  * 2c  cross-cluster failure/success "counterpart" pairing: a failure pattern
        and the success pattern describing the *same* behavioral aspect are
        linked (``counterpart_id`` both ways). The success side exemplifies the
        constructive behavior a paradigm design should systematize.

Across epochs the library grows *incrementally*: new observations are first
matched against existing patterns by label similarity; only the unmatched
residue is grouped and refined afresh. This keeps ``pattern_id`` stable for a
recurring pattern so Layer 3 can track its per-epoch occurrence rate.
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
from css.model.json_repair import complete_optimizer_json

# ──────────────────────────────────────────────────────────────────────────
# Layer 2b prompt (per-cluster unification). Open-ended: the LLM names the
# pattern; we never enumerate fixed cognitive dimensions.
# ──────────────────────────────────────────────────────────────────────────
_REFINE_SYSTEM = (
    "You are a behavioral-pattern analyst. You are given several independent "
    "observations of HOW an agent ACTS during task solving, drawn from different "
    "trajectories but pre-grouped because they appear to describe the SAME "
    "underlying behavioral pattern. Your job is to unify them into ONE pattern.\n\n"
    "Do NOT invent a behavior that is not supported by the observations. Name the "
    "pattern by the ACTION-LEVEL behavior it captures — what the agent does, in "
    "what phase, under what conditions — not by abstract cognitive tendencies or "
    "surface task details. The description should capture the RECURRING behavioral "
    "mechanism: what the agent does, when, what triggers it, and what consequences "
    "it produces — grounded in the concrete actions described in the observations. "
    "Decide its polarity from the observations' consequences: 'failure' if this "
    "behavior tends to cause poor outcomes, 'success' if it tends to cause good "
    "outcomes, else 'neutral'.\n\n"
    "Respond with ONE JSON object and nothing else:\n"
    '{"name": "<specific, descriptive pattern name>", "description": "<a rich '
    "description of the recurring behavioral pattern: what the agent does, under "
    "what conditions, what triggers it, and what consequences it produces — "
    'grounded in the observations>", "cognitive_aspect": "<the named behavioral '
    'pattern label>", "polarity": "failure|success|neutral"}'
)

_COUNTERPART_SYSTEM = (
    "You are a behavioral-pattern analyst. You are given a list of behavioral "
    "patterns, each with an id, name, polarity and cognitive_aspect (which is "
    "a behavioral pattern label). Pair each FAILURE pattern with the SUCCESS "
    "pattern that describes the SAME behavioral aspect handled well (its "
    "constructive counterpart), when such a pair clearly exists. A pattern may "
    "appear in at most one pair.\n\n"
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


# Caps for the per-cluster refinement prompt (see _refine_one_cluster).
_MAX_REFINE_MEMBERS = 60
_MAX_FIELD_CHARS = 240


def _truncate_field(s: str, n: int = _MAX_FIELD_CHARS) -> str:
    s = (s or "").strip().replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


def _select_refine_members(members: list["Observation"]) -> list["Observation"]:
    """Pick up to ``_MAX_REFINE_MEMBERS`` representative observations for the prompt.

    Prefers ``significance == "critical"`` observations, then fills with the rest,
    preserving input order for determinism. Returns ``members`` unchanged when it
    is already within the cap.
    """
    if len(members) <= _MAX_REFINE_MEMBERS:
        return members
    crit: list["Observation"] = []
    rest: list["Observation"] = []
    for o in members:
        if str(getattr(o, "significance", "")).strip().lower() == "critical":
            crit.append(o)
        else:
            rest.append(o)
    selected = crit[:_MAX_REFINE_MEMBERS]
    if len(selected) < _MAX_REFINE_MEMBERS:
        selected += rest[: _MAX_REFINE_MEMBERS - len(selected)]
    return selected


def _refine_one_cluster(
    client: "LLMClient", members: list["Observation"], temp_id: str
) -> "PatternRecord":
    """LLM-unify one cluster of observations into a single PatternRecord."""
    # Bound the refinement prompt. A single DBSCAN cluster can hold hundreds or
    # thousands of observations (esp. during coldstart analysis over the full
    # train set); concatenating ALL of them blows the optimizer context window
    # (observed: a 258k-token prompt -> HTTP 400 on a 262k-context model). We
    # show only a representative, size-capped sample to the LLM (preferring
    # critical-significance observations) and truncate each field. ALL members
    # are still retained on the PatternRecord below, so occurrence tracking and
    # the centroid are unaffected — only the naming/description prompt is sampled.
    shown = _select_refine_members(members)
    lines = []
    for i, o in enumerate(shown):
        lines.append(
            f"[{i}] aspect={o.cognitive_aspect!r} | what={_truncate_field(o.what)} | "
            f"consequence={_truncate_field(o.consequence)} | polarity={o.polarity} | "
            f"significance={o.significance}"
        )
    header = f"Observations in this group ({len(members)} total"
    if len(shown) < len(members):
        header += f"; showing {len(shown)} representative samples"
    header += "):\n"
    user = (
        header + "\n".join(lines) + "\n\n"
        "Unify them into one cognitive pattern as the JSON object specified."
    )
    from css.tracing import stage_context
    with stage_context(client, "cluster_refine"):
        obj = complete_optimizer_json(
            client, _REFINE_SYSTEM, user, parse=_parse_json_object,
            stage="cluster_refine",
        )

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

    from css.tracing import log_event
    log_event("cluster_refine",
              temp_id=temp_id,
              n_members=len(members),
              n_shown=len(shown),
              polarity=polarity,
              name=name,
              description=_truncate_field(description, 300))

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


def _fallback_pattern_record(
    members: list["Observation"], temp_id: str
) -> "PatternRecord":
    """Build a PatternRecord from cluster members without an LLM call.

    Used when ``_refine_one_cluster`` raises (e.g. a transient optimizer error or
    a still-oversized prompt) so a single bad cluster degrades to a deterministic
    record instead of crashing the whole analysis epoch.
    """
    return PatternRecord(
        pattern_id=temp_id,
        name=(members[0].cognitive_aspect.strip() or "unnamed-pattern"),
        description=members[0].what.strip(),
        cognitive_aspect=members[0].cognitive_aspect.strip(),
        polarity=_vote_polarity(members),  # type: ignore[arg-type]
        observations=list(members),
        centroid=_centroid(members),
        status="active",
    )


# ──────────────────────────────────────────────────────────────────────────
# 2c — failure ↔ success counterpart pairing
# ──────────────────────────────────────────────────────────────────────────
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
            from css.tracing import stage_context
            with stage_context(client, "counterpart_pair"):
                obj = complete_optimizer_json(
                    client, _COUNTERPART_SYSTEM, user, parse=_parse_json_object,
                    stage="counterpart",
                )
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

    # Fill any still-unpaired patterns deterministically by label similarity.
    from css.analysis.label_grouping import label_counterparts
    label_counterparts(patterns)

    from css.tracing import log_event
    pairs = [
        {"failure_id": p.pattern_id, "success_id": p.counterpart_id,
         "failure_name": p.name}
        for p in patterns
        if p.polarity == "failure" and p.counterpart_id
    ]
    log_event("counterpart_pair",
              n_failures=len(failures),
              n_successes=len(successes),
              n_pairs=len(pairs),
              pairs=pairs)


# ──────────────────────────────────────────────────────────────────────────
# Full Layer 2 driver
# ──────────────────────────────────────────────────────────────────────────
def build_or_update_library(
    client: "LLMClient",
    library: "PatternLibrary",
    observations: list["Observation"],
    *,
    cfg: "CSSConfig",
    out_dir: str = "",
) -> "PatternLibrary":
    """Full Layer 2: match existing -> group residue -> refine -> add -> pair.

    Uses label-based grouping (Jaccard pre-group + LLM synonym detection
    on cognitive_aspect labels).

    Pipeline:

    1. Match new observations against existing patterns by cognitive_aspect
       label similarity (:func:`match_by_label`); attach matches.
    2. Group the unmatched residue by label canonicalization
       (:func:`group_observations` -- Jaccard + LLM synonym).
    3. LLM-refine each group into a :class:`PatternRecord` (reuses
       :func:`_refine_one_cluster`).
    4. Allocate stable ``pattern_id``, stamp observations, add to library.
    5. Re-pair failure/success counterparts.
    """
    from css.tracing import log_event
    from css.analysis.label_grouping import (
        group_observations,
        match_by_label,
        label_counterparts,
    )

    if not observations:
        return library

    # 1) Match against existing patterns by label similarity.
    _matched, unmatched = match_by_label(client, library, observations)

    log_event("layer2_match",
              n_observations=len(observations),
              n_matched=len(_matched),
              n_unmatched=len(unmatched),
              n_existing_patterns=len(library))

    # Match results for the audit artifact: which observation attached to which
    # existing pattern (match_by_label stamps obs.pattern_id on a hit).
    match_results = [
        {"obs_id": o.obs_id, "pattern_id": o.pattern_id,
         "cognitive_aspect": o.cognitive_aspect}
        for o in _matched
    ]

    # 2) Group unmatched observations by cognitive_aspect canonicalization.
    new_count = 0
    new_patterns_audit: list[dict] = []
    if unmatched:
        clusters, _noise = group_observations(
            client, unmatched,
            jaccard_threshold=0.55,
            batch_size=getattr(cfg, "minibatch_size", 60),
            min_group_size=cfg.min_samples,
            out_dir=out_dir or None,
        )

        # 3) LLM-refine each group into a PatternRecord.
        new_records = []
        for i, members in enumerate(clusters):
            try:
                rec = _refine_one_cluster(client, members, f"tmp{i}")
                new_records.append(rec)
            except Exception:  # noqa: BLE001
                new_records.append(_fallback_pattern_record(members, f"tmp{i}"))

        # 4) Assign stable ids + stamp observations + add to library.
        for rec in new_records:
            pid = library.new_pattern_id()
            rec.pattern_id = pid
            for obs in rec.observations:
                obs.pattern_id = pid
            library.add(rec)
            new_patterns_audit.append({
                "pattern_id": pid,
                "name": rec.name,
                "polarity": rec.polarity,
                "member_obs_ids": [o.obs_id for o in rec.observations],
            })
        new_count = len(new_records)

    # 5) Re-pair counterparts: LLM first, then label-based fallback.
    active = library.active()
    pair_counterparts(client, active)
    label_counterparts(active)

    counterpart_pairs = [
        {"failure_id": p.pattern_id, "success_id": p.counterpart_id}
        for p in active
        if p.polarity == "failure" and p.counterpart_id
    ]

    log_event("layer2_done",
              n_new_patterns=new_count,
              n_total_patterns=len(library),
              patterns=[{"id": p.pattern_id, "name": p.name,
                         "polarity": p.polarity,
                         "n_obs": len(p.observations),
                         "counterpart": p.counterpart_id}
                        for p in library.active()])

    if out_dir:
        _save_layer2_artifact(
            out_dir,
            match_results=match_results,
            new_patterns=new_patterns_audit,
            counterpart_pairs=counterpart_pairs,
            n_total_patterns=len(library),
        )

    return library


def _save_layer2_artifact(
    out_dir: str,
    *,
    match_results: list[dict],
    new_patterns: list[dict],
    counterpart_pairs: list[dict],
    n_total_patterns: int,
) -> None:
    """Write ``layer2_library.json`` — never raises (auditability is best-effort)."""
    import os

    try:
        artifact = {
            "match_results": match_results,
            "n_matched": len(match_results),
            "new_patterns": new_patterns,
            "n_new_patterns": len(new_patterns),
            "counterpart_pairings": counterpart_pairs,
            "n_total_patterns": n_total_patterns,
        }
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "layer2_library.json"),
                  "w", encoding="utf-8") as f:
            json.dump(artifact, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
