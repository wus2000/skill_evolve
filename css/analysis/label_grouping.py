"""Layer 2 observation grouping by cognitive-aspect label canonicalization.

Replaces the embedding + DBSCAN pipeline with a two-stage approach that
leverages the semantic labels Layer 1 already produced:

  Stage 1 — **Jaccard pre-group**: tokenize each ``cognitive_aspect`` label and
  merge labels whose word-set Jaccard similarity exceeds a threshold. This is
  pure computation (zero LLM calls) and catches trivial variants like
  "Defensive Null Checking" ↔ "Defensive Null-Checking".

  Stage 2 — **LLM batch canonicalization**: send batches of unique labels to
  the optimizer LLM and ask it to group synonyms. The LLM understands the
  semantic intent behind differently-worded labels far better than any
  embedding distance, because it *generated* these labels in the first place.

The output is a list of ``(canonical_name, [Observation, ...])`` groups that
feed directly into ``refine_clusters``-style PatternRecord construction.

Why not embedding?  Diagnostic experiments showed that a general-purpose
embedding model (Qwen3-Embedding-0.6B) maps all "LLM agent cognitive behavior"
observations into a narrow similarity cone (cosine mean=0.48, std=0.11),
making density-based clustering (DBSCAN) degenerate — either one giant cluster
or all noise. The cognitive_aspect labels themselves carry far richer
discriminative signal than their embeddings.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from css.data.pattern import Observation
    from css.model.client import LLMClient


# ── Tokenizer ───────────────────────────────────────────────────────────────

def _tokenize(label: str) -> set[str]:
    """Split a cognitive_aspect label into a lowercase word set."""
    return set(re.findall(r"[a-z]+", label.lower()))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ── Stage 1: Jaccard pre-grouping ───────────────────────────────────────────

def jaccard_pregroup(
    observations: list["Observation"],
    threshold: float = 0.55,
) -> list[list["Observation"]]:
    """Group observations whose cognitive_aspect labels are near-identical.

    Uses greedy single-pass assignment: for each observation, find the first
    existing group whose representative label has Jaccard ≥ threshold; if none,
    start a new group. Deterministic (input-order dependent). Returns groups
    preserving input order.
    """
    groups: list[list["Observation"]] = []
    rep_tokens: list[set[str]] = []

    rep_labels: list[str] = []  # original label text for exact-match fallback

    for obs in observations:
        label = (obs.cognitive_aspect or "").strip()
        tokens = _tokenize(label)

        matched = False
        for idx, rep in enumerate(rep_tokens):
            # Short labels (≤1 token): require exact text match
            if len(tokens) <= 1 or len(rep) <= 1:
                if label.lower() == rep_labels[idx].lower():
                    groups[idx].append(obs)
                    matched = True
                    break
            else:
                if _jaccard(tokens, rep) >= threshold:
                    groups[idx].append(obs)
                    matched = True
                    break
        if not matched:
            groups.append([obs])
            rep_tokens.append(tokens)
            rep_labels.append(label)

    return groups


# ── Stage 2: LLM batch canonicalization ─────────────────────────────────────

_GROUP_SYSTEM = """\
You are organizing cognitive behavior labels from an LLM agent analysis.

You receive a numbered list of cognitive-aspect labels. Each label describes \
a specific way the agent thinks or makes decisions during task execution. \
Many labels describe the SAME underlying cognitive pattern with different \
wording (e.g., "Unvalidated Structural Assumption" and "Unverified Schema \
Dependency" both describe making structural assumptions without verification).

Your job: group labels that describe the SAME underlying cognitive pattern.

Rules:
- Only merge labels that truly describe the same cognitive mechanism, not \
  merely the same topic area.
- A group can have 1 member (a unique pattern with no synonyms in this batch).
- Choose a clear, concise canonical name for each group.

Output ONLY a JSON object:
{"groups": [{"name": "<canonical name>", "indices": [1, 5, 12]}, ...]}

Every input index must appear in exactly one group."""


def _llm_group_labels(
    client: "LLMClient",
    labels: list[str],
) -> list[list[int]]:
    """Ask the LLM to group a batch of labels by synonym.

    Returns a list of index-lists (0-based). On any failure, returns each
    label as its own singleton group (graceful degradation).
    """
    if len(labels) <= 1:
        return [list(range(len(labels)))]

    numbered = "\n".join(f"{i+1}. {lab}" for i, lab in enumerate(labels))
    user = f"Labels to group:\n{numbered}"

    from css.tracing import stage_context
    try:
        with stage_context(client, "label_group"):
            text, _usage = client.complete_optimizer(_GROUP_SYSTEM, user)
    except Exception:
        return [[i] for i in range(len(labels))]

    return _parse_group_response(text, len(labels))


def _parse_group_response(text: str, n_labels: int) -> list[list[int]]:
    """Parse the LLM grouping response into 0-based index lists."""
    # Try to extract JSON
    obj = None
    for pattern in [
        re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL),
        re.compile(r"\{.*\}", re.DOTALL),
    ]:
        m = pattern.search(text or "")
        if m:
            try:
                candidate = m.group(1) if pattern.groups else m.group(0)
                obj = json.loads(candidate)
                break
            except (json.JSONDecodeError, IndexError):
                continue

    if obj is None:
        return [[i] for i in range(n_labels)]

    raw_groups = obj.get("groups", []) if isinstance(obj, dict) else []
    if not isinstance(raw_groups, list):
        return [[i] for i in range(n_labels)]

    result: list[list[int]] = []
    seen: set[int] = set()
    for g in raw_groups:
        if not isinstance(g, dict):
            continue
        indices = g.get("indices", [])
        if not isinstance(indices, list):
            continue
        zero_based = []
        for idx in indices:
            try:
                zb = int(idx) - 1  # LLM uses 1-based
                if 0 <= zb < n_labels and zb not in seen:
                    zero_based.append(zb)
                    seen.add(zb)
            except (TypeError, ValueError):
                pass
        if zero_based:
            result.append(zero_based)

    # Any indices not covered → singleton groups
    for i in range(n_labels):
        if i not in seen:
            result.append([i])

    return result


def llm_canonicalize(
    client: "LLMClient",
    groups: list[list["Observation"]],
    *,
    batch_size: int = 60,
    batch_details: list[dict] | None = None,
) -> list[list["Observation"]]:
    """Refine Jaccard pre-groups by LLM synonym detection.

    Takes the pre-groups (each already a Jaccard cluster), extracts one
    representative label per group, batches these into LLM calls, and merges
    groups the LLM identifies as synonymous.

    When ``batch_details`` is supplied, one dict per LLM batch is appended to it
    (``batch_index`` / ``n_labels`` / ``n_groups_out``) so an auditor can see
    how each batch collapsed.
    """
    from css.tracing import log_event

    if len(groups) <= 1:
        return groups

    # One representative label per group (the most common, or first)
    rep_labels: list[str] = []
    for g in groups:
        aspects = [o.cognitive_aspect or "" for o in g]
        # Pick the most frequent label in the group
        from collections import Counter
        mc = Counter(aspects).most_common(1)
        rep_labels.append(mc[0][0] if mc else "")

    # Batch the representative labels for LLM grouping
    n = len(rep_labels)
    merged_groups = list(range(n))  # union-find: merged_groups[i] = canonical group index

    for batch_index, batch_start in enumerate(range(0, n, batch_size)):
        batch_end = min(batch_start + batch_size, n)
        batch_labels = rep_labels[batch_start:batch_end]
        if len(batch_labels) <= 1:
            continue

        llm_groups = _llm_group_labels(client, batch_labels)

        log_event("llm_label_group",
                  batch_index=batch_index,
                  n_labels=len(batch_labels),
                  n_groups_out=len(llm_groups),
                  multi_member_groups=sum(1 for g in llm_groups if len(g) > 1),
                  sample_labels=batch_labels[:10])
        if batch_details is not None:
            batch_details.append({
                "batch_index": batch_index,
                "n_labels": len(batch_labels),
                "n_groups_out": len(llm_groups),
            })

        # Merge within each LLM-identified group
        for idx_list in llm_groups:
            if len(idx_list) <= 1:
                continue
            # Map batch-local indices back to global indices
            global_indices = [batch_start + bi for bi in idx_list]
            # Union all to the first
            canonical = global_indices[0]
            for gi in global_indices[1:]:
                merged_groups[gi] = canonical

    # Resolve transitive merges
    def find(i: int) -> int:
        while merged_groups[i] != i:
            merged_groups[i] = merged_groups[merged_groups[i]]
            i = merged_groups[i]
        return i

    # Build final groups
    final: dict[int, list["Observation"]] = defaultdict(list)
    for i, g in enumerate(groups):
        root = find(i)
        final[root].extend(g)

    result = [obs_list for obs_list in final.values() if obs_list]

    log_event("label_canonicalize",
              n_input_groups=len(groups),
              n_output_groups=len(result),
              n_observations=sum(len(g) for g in result),
              n_llm_batches=(n + batch_size - 1) // batch_size,
              group_sizes=sorted([len(g) for g in result], reverse=True)[:20])

    return result


# ── Combined pipeline ───────────────────────────────────────────────────────

def _group_rep_label(group: list["Observation"]) -> str:
    """The most frequent cognitive_aspect label in a group (its representative)."""
    from collections import Counter
    aspects = [o.cognitive_aspect or "" for o in group]
    mc = Counter(aspects).most_common(1)
    return mc[0][0] if mc else ""


def group_observations(
    client: "LLMClient",
    observations: list["Observation"],
    *,
    jaccard_threshold: float = 0.55,
    batch_size: int = 60,
    min_group_size: int = 2,
    out_dir: str | None = None,
) -> tuple[list[list["Observation"]], list["Observation"]]:
    """Full two-stage observation grouping (replaces embed + DBSCAN).

    Returns ``(groups, noise)`` where each group has ``>= min_group_size``
    members (analogous to DBSCAN clusters) and noise contains observations
    in singleton groups (analogous to DBSCAN noise points).

    When ``out_dir`` is given, a self-contained ``label_grouping.json`` audit
    artifact is written there describing both stages and the final clustering.
    """
    from css.tracing import log_event

    if not observations:
        return [], []

    # Stage 1: Jaccard pre-group
    pre_groups = jaccard_pregroup(observations, threshold=jaccard_threshold)
    n_pre = len(pre_groups)

    # Capture the non-trivial Jaccard merges (pre-groups that fused >1 distinct
    # label) for the audit artifact — the labels that the cheap stage collapsed.
    sample_merges: list[dict] = []
    for g in pre_groups:
        if len(g) < 2:
            continue
        distinct_labels = sorted({(o.cognitive_aspect or "").strip() for o in g})
        if len(distinct_labels) >= 2:
            sample_merges.append({
                "labels": distinct_labels[:6],
                "n_members": len(g),
            })

    # Stage 2: LLM canonicalization (collect per-batch detail for the artifact)
    batch_details: list[dict] = []
    final_groups = llm_canonicalize(
        client, pre_groups, batch_size=batch_size, batch_details=batch_details
    )

    # Separate clusters from noise (singletons)
    clusters: list[list["Observation"]] = []
    noise: list["Observation"] = []
    for g in final_groups:
        if len(g) >= min_group_size:
            clusters.append(g)
        else:
            noise.extend(g)

    log_event("group_observations",
              n_observations=len(observations),
              n_jaccard_groups=n_pre,
              n_final_groups=len(final_groups),
              n_clusters=len(clusters),
              n_noise=len(noise))

    if out_dir:
        _save_grouping_artifact(
            out_dir,
            n_input=len(observations),
            jaccard_threshold=jaccard_threshold,
            n_pre=n_pre,
            sample_merges=sample_merges,
            n_final=len(final_groups),
            batch_details=batch_details,
            clusters=clusters,
            noise=noise,
        )

    return clusters, noise


def _save_grouping_artifact(
    out_dir: str,
    *,
    n_input: int,
    jaccard_threshold: float,
    n_pre: int,
    sample_merges: list[dict],
    n_final: int,
    batch_details: list[dict],
    clusters: list[list["Observation"]],
    noise: list["Observation"],
) -> None:
    """Write ``label_grouping.json`` — never raises (auditability is best-effort)."""
    import os

    try:
        cluster_summaries = []
        for g in clusters:
            cluster_summaries.append({
                "name": _group_rep_label(g),
                "size": len(g),
                "sample_labels": sorted(
                    {(o.cognitive_aspect or "").strip() for o in g}
                )[:8],
            })
        cluster_summaries.sort(key=lambda c: c["size"], reverse=True)

        artifact = {
            "n_input_observations": n_input,
            "stage1_jaccard": {
                "threshold": jaccard_threshold,
                "n_groups_before": n_input,
                "n_groups_after": n_pre,
                "sample_merges": sample_merges[:20],
            },
            "stage2_llm": {
                "n_batches": len(batch_details),
                "n_groups_before": n_pre,
                "n_groups_after": n_final,
                "batch_details": batch_details,
            },
            "final": {
                "n_clusters": len(clusters),
                "n_noise": len(noise),
                "cluster_sizes": [len(g) for g in
                                  sorted(clusters, key=len, reverse=True)],
                "clusters": cluster_summaries,
            },
        }
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "label_grouping.json"),
                  "w", encoding="utf-8") as f:
            json.dump(artifact, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ── Label-based incremental matching (replaces centroid cosine) ─────────────

def match_by_label(
    client: "LLMClient",
    library: "PatternLibrary",
    observations: list["Observation"],
    *,
    jaccard_threshold: float = 0.45,
) -> tuple[list["Observation"], list["Observation"]]:
    """Match new observations to existing patterns by cognitive_aspect similarity.

    For each observation, checks its ``cognitive_aspect`` label against all
    active pattern names/cognitive_aspects using Jaccard word overlap. A match
    above ``jaccard_threshold`` attaches the observation to that pattern.

    Returns ``(matched, unmatched)``.
    """
    from css.data.pattern import PatternLibrary

    active = library.active() if isinstance(library, PatternLibrary) else []
    if not active or not observations:
        return [], list(observations)

    # Build a label→pattern lookup from existing patterns
    pattern_labels: list[tuple["PatternRecord", set[str]]] = []
    for p in active:
        # Use both the pattern name and cognitive_aspect for matching
        combined = f"{p.name or ''} {p.cognitive_aspect or ''}".strip()
        tokens = _tokenize(combined)
        if tokens:
            pattern_labels.append((p, tokens))

    matched: list["Observation"] = []
    unmatched: list["Observation"] = []

    for obs in observations:
        obs_tokens = _tokenize(obs.cognitive_aspect or "")
        if len(obs_tokens) < 2:
            unmatched.append(obs)
            continue

        best_pat = None
        best_sim = 0.0
        for pat, pat_tokens in pattern_labels:
            sim = _jaccard(obs_tokens, pat_tokens)
            if sim > best_sim:
                best_sim = sim
                best_pat = pat

        if best_pat is not None and best_sim >= jaccard_threshold:
            obs.pattern_id = best_pat.pattern_id
            best_pat.observations.append(obs)
            matched.append(obs)
        else:
            unmatched.append(obs)

    return matched, unmatched


# ── Label-based counterpart fallback (replaces cosine counterparts) ─────────

def label_counterparts(patterns: list["PatternRecord"]) -> None:
    """Fallback counterpart pairing by label Jaccard (replaces cosine centroid).

    For each unpaired failure pattern, find the most similar unpaired success
    pattern by cognitive_aspect/name word overlap.
    """
    failures = [p for p in patterns if p.polarity == "failure" and not p.counterpart_id]
    successes = [p for p in patterns if p.polarity == "success" and not p.counterpart_id]

    used: set[str] = set()
    for f in failures:
        f_tokens = _tokenize(f"{f.name or ''} {f.cognitive_aspect or ''}")
        if len(f_tokens) < 2:
            continue
        best_s = None
        best_sim = 0.3  # minimum threshold for pairing
        for s in successes:
            if s.pattern_id in used:
                continue
            s_tokens = _tokenize(f"{s.name or ''} {s.cognitive_aspect or ''}")
            sim = _jaccard(f_tokens, s_tokens)
            if sim > best_sim:
                best_sim = sim
                best_s = s
        if best_s is not None:
            f.counterpart_id = best_s.pattern_id
            best_s.counterpart_id = f.pattern_id
            used.add(best_s.pattern_id)


# ── Label-based duplicate pattern detection (replaces embedding similarity) ─

def find_merge_candidates(
    patterns: list["PatternRecord"],
    *,
    threshold: float = 0.60,
) -> list[tuple[int, int]]:
    """Find same-polarity pattern pairs with high label Jaccard (merge candidates).

    Returns (i, j) index pairs into the input list. Replaces the embedding
    cosine similarity step in ``merge_duplicate_patterns``.
    """
    n = len(patterns)
    candidates: list[tuple[int, int]] = []
    tokens = [_tokenize(f"{p.name or ''} {p.cognitive_aspect or ''} {p.description or ''}")
              for p in patterns]

    for i in range(n):
        for j in range(i + 1, n):
            if patterns[i].polarity != patterns[j].polarity:
                continue
            if len(tokens[i]) < 2 or len(tokens[j]) < 2:
                continue
            sim = _jaccard(tokens[i], tokens[j])
            if sim >= threshold:
                candidates.append((i, j))

    return candidates
