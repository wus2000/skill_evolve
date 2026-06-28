"""L0 Aggregate stage — deterministic hierarchical patch merging.

The Reflect stage (``css/optimizer/reflect.py``) produces one :class:`RawPatch`
per analysed minibatch (failures and successes separately). The Aggregate stage
collapses those independently-proposed edits into a single :class:`Patch` whose
edits carry a ``support_count`` (how many raw patches proposed a semantically
equivalent edit) so the downstream selection/ranking stage can prefer
high-consensus, failure-driven edits.

Design note vs. SkillOpt
------------------------
SkillOpt's ``skillopt/gradient/aggregate.py`` performs the merge with
hierarchical *LLM* calls (``_merge_batch`` / ``_hierarchical_merge`` ->
``chat_optimizer``) and a final failure-vs-success combine. CSS instead does a
*deterministic* merge: edits are grouped by a normalized key
(``op`` + normalized ``target`` + normalized ``content``), ``support_count`` is
the number of raw patches that proposed each key, and a single representative
``Edit`` is kept per key. This removes the nondeterminism / extra LLM cost of
the SkillOpt approach while preserving the same "failure-first, support-counted"
ranking semantics it implements implicitly. We keep SkillOpt's notion that
failure-driven edits take priority (see ``merge_patches`` failure-first ordering,
``related_works/SkillOpt/skillopt/gradient/aggregate.py:153-204``).
"""
from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from css.data.edit import Edit, Patch, RawPatch
from css.model.json_repair import complete_optimizer_json

if TYPE_CHECKING:
    from css.model.client import LLMClient

__all__ = [
    "aggregate_patches",
    "jaccard_dedup_edits",
    "llm_merge_coordinator",
    "llm_semantic_dedup",
    "select_top_edits",
]


# ── Normalization ─────────────────────────────────────────────────────────────

_WS_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Lowercase + collapse all whitespace runs to a single space, stripped.

    Two edits are considered "the same" when their op and their normalized
    target/content match. Normalization makes the comparison robust to trivial
    formatting differences (case, indentation, line breaks) the optimizer may
    introduce across independently-generated minibatch patches.
    """
    if not text:
        return ""
    return _WS_RE.sub(" ", text).strip().lower()


def _edit_key(edit: Edit) -> tuple[str, str, str]:
    """Semantic identity key for de-duplication / support counting."""
    return (edit.op, _normalize(edit.target), _normalize(edit.content))


def _tokenize(text: str) -> set[str]:
    """Split text into a set of lowercase words for Jaccard comparison."""
    if not text:
        return set()
    return set(_WS_RE.split(text.strip().lower())) - {""}


def _jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard similarity between two word sets."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ── Public API ─────────────────────────────────────────────────────────────────

def aggregate_patches(raw_patches: list[RawPatch]) -> Patch:
    """Merge edits across raw patches into a single support-counted ``Patch``.

    Two edits are treated as identical when their normalized key
    (``op`` + normalized ``target`` + normalized ``content``) matches. For each
    distinct key, one representative :class:`Edit` is kept and its
    ``support_count`` is set to the number of *raw patches* that proposed an edit
    with that key (a raw patch proposing the same key twice still counts once).

    Provenance is preserved on the representative edit: ``source_type`` prefers
    ``"failure"`` if any contributing raw patch was failure-driven (matching
    SkillOpt's failure-first priority), and ``reason`` is carried from the first
    contributing edit. Representatives appear in first-seen order so the result
    is deterministic.
    """
    if not raw_patches:
        return Patch(edits=[], reasoning="aggregate: no raw patches")

    # Preserve first-seen order of distinct keys for deterministic output.
    order: list[tuple[str, str, str]] = []
    representative: dict[tuple[str, str, str], Edit] = {}
    # source_type per key: failure wins over success.
    has_failure: dict[tuple[str, str, str], bool] = {}
    has_success: dict[tuple[str, str, str], bool] = {}

    for raw in raw_patches:
        if raw is None or raw.patch is None:
            continue
        rp_source = raw.source_type or "failure"
        # Count each key at most once PER raw patch (support = # of raw patches).
        seen_this_patch: set[tuple[str, str, str]] = set()
        for edit in raw.patch.edits:
            if not isinstance(edit, Edit):
                continue
            key = _edit_key(edit)
            if key not in representative:
                order.append(key)
                rep = Edit(
                    op=edit.op,
                    content=edit.content,
                    target=edit.target,
                    support_count=0,
                    source_type=None,
                    merge_level=1,
                    reason=edit.reason,
                )
                representative[key] = rep
                has_failure[key] = False
                has_success[key] = False
            # Provenance: prefer the edit's own source_type, else the raw
            # patch's source_type.
            edit_source = edit.source_type or rp_source
            if edit_source in ("failure", "contrastive", "synthesized"):
                has_failure[key] = True
            elif edit_source == "success":
                has_success[key] = True
            else:
                if rp_source in ("failure", "contrastive", "synthesized"):
                    has_failure[key] = True
                else:
                    has_success[key] = True
            if key not in seen_this_patch:
                seen_this_patch.add(key)
                rep = representative[key]
                rep.support_count = (rep.support_count or 0) + 1

    edits: list[Edit] = []
    for key in order:
        rep = representative[key]
        rep.source_type = "failure" if has_failure[key] else "success"
        edits.append(rep)

    return Patch(
        edits=edits,
        reasoning=(
            f"aggregate: merged {len(raw_patches)} raw patches "
            f"into {len(edits)} distinct edits"
        ),
    )


# ── LLM merge coordinator (one-shot; replaces mechanical aggregate for plan_a v2) ─

_MERGE_COORDINATOR_SYSTEM = """\
You are a skill-edit coordinator. You receive the CURRENT `rules.md` and several
independently-proposed edit patches — from different minibatches of failure /
success / contrastive analysis within ONE optimization step. Merge them into ONE
coherent, non-redundant, MINIMAL set of edits to `rules.md`.

Rules:
1. FUSE same-theme edits into ONE best-worded edit; set `support_count` to how many
   input patches proposed that theme (cross-minibatch agreement = systematic).
2. GAP-ALIGN against the current `rules.md`: if a theme is already covered by an
   existing `###` section, emit a `rewrite_section` of that section (target = the
   existing heading) — never a duplicate `add_section`. If genuinely new, emit
   `add_section`. For a small change inside an existing section, prefer a refinement
   op (insert_after / replace / delete).
3. INDEPENDENCE: at most one output edit per `###` section / text region.
4. PRIORITY on conflict: failure- and contrastive-driven edits outrank
   success-driven ones. Never let a success edit weaken or remove a fix the failure
   / contrastive edits introduce; keep validated success guidance otherwise intact.
5. PRESERVE unique insights from single patches.
6. Every output edit is MINIMAL and SINGLE-THEME. Do NOT collapse the set into one
   giant edit or a rewritten document — output a SET of small, independent edits.

`target` is a SEMANTIC pointer (section heading, or a description / approximate
quote of the spot); it is resolved by meaning. Op tiers: structural = add_section /
rewrite_section / delete_section; refinement = insert_after / replace / delete /
append.

Output ONLY this JSON object (no fences, no prose):
{
  "reasoning": "<key consolidation decisions>",
  "edits": [
    {"op": "...", "target": "<omit for add_section/append>",
     "content": "<markdown, ONE theme; omit for delete/delete_section>", "rationale": "<why>",
     "support_count": <int>, "source_type": "failure|success|contrastive"}
  ]
}"""


def _section_headings(rules: str) -> str:
    """A compact list of the current ``###`` section headings (merge context)."""
    if not rules:
        return ""
    heads = [ln.strip() for ln in rules.split("\n") if ln.strip().startswith("### ")]
    return "\n".join(f"  {i+1}. {h}" for i, h in enumerate(heads))


def _edit_from_merge_dict(d: dict) -> Edit | None:
    """Build an :class:`Edit` from one coordinator-output edit dict (validated)."""
    from css.data.edit import EDIT_OPS

    op = str(d.get("op", "")).strip().lower()
    if op not in EDIT_OPS:
        return None
    target = str(d.get("target", "") or "")
    if op in ("insert_after", "replace", "delete", "rewrite_section", "delete_section") and not target:
        return None
    try:
        support = int(d.get("support_count", 1) or 1)
    except (TypeError, ValueError):
        support = 1
    src = d.get("source_type")
    if src not in ("failure", "success", "contrastive", "synthesized"):
        src = "failure"
    return Edit(
        op=op,  # type: ignore[arg-type]
        content=str(d.get("content", "") or ""),
        target=target,
        support_count=max(1, support),
        source_type="failure" if src in ("failure", "contrastive", "synthesized") else "success",
        reason=str(d.get("rationale", "") or d.get("reason", "") or ""),
    )


def llm_merge_coordinator(
    client: "LLMClient",
    rules: str,
    raw_patches: list[RawPatch],
    *,
    cfg=None,
) -> Patch:
    """One-shot LLM merge of all minibatch patches into a clean, minimal edit set.

    Unlike :func:`aggregate_patches` (which only key-dedups and can never FUSE two
    differently-worded edits on the same theme), this coordinator reads the current
    ``rules.md`` plus every proposed edit and returns a SET of small, single-theme,
    non-overlapping edits — fusing same-theme proposals, gap-aligning against the
    existing sections (rewrite vs add), enforcing independence, and counting
    cross-minibatch support.  Degrades to the deterministic
    :func:`aggregate_patches` on any LLM/parse failure, so the step never stalls.
    """
    patches = [rp for rp in raw_patches if rp is not None and rp.patch is not None]
    proposed: list[dict] = []
    for rp in patches:
        for e in rp.patch.edits:
            if isinstance(e, Edit):
                proposed.append({
                    "op": e.op, "target": e.target, "content": e.content,
                    "rationale": e.reason, "source_type": rp.source_type or e.source_type or "failure",
                })
    if not proposed:
        return Patch(edits=[], reasoning="llm_merge: no proposed edits")
    # A single proposed edit needs no merge.
    if len(proposed) == 1:
        return aggregate_patches(patches)

    user = (
        "## Current rules.md\n"
        + (rules.strip() if rules and rules.strip() else "(empty)")
        + "\n\n## rules.md section index\n"
        + (_section_headings(rules) or "(none)")
        + f"\n\n## Proposed edits to merge ({len(proposed)} total)\n"
        + json.dumps(proposed, ensure_ascii=False, indent=2)
    )
    try:
        raw = complete_optimizer_json(
            client, _MERGE_COORDINATOR_SYSTEM, user, parse=_parse_merge_output,
            max_tokens=8192, stage="merge",
        )
    except Exception:  # noqa: BLE001
        return aggregate_patches(patches)

    if raw is None:
        return aggregate_patches(patches)
    edits: list[Edit] = []
    for d in raw:
        e = _edit_from_merge_dict(d)
        if e is not None:
            edits.append(e)
    if not edits:
        # The model returned an empty/invalid set; fall back to deterministic merge
        # only if there genuinely were edits to keep.
        return Patch(edits=[], reasoning="llm_merge: coordinator returned no edits")
    return Patch(
        edits=edits,
        reasoning=f"llm_merge: {len(proposed)} proposed -> {len(edits)} merged edits",
    )


def _parse_merge_output(text: str) -> list[dict] | None:
    """Extract the ``edits`` list from the coordinator's JSON response."""
    if not text:
        return None
    for pattern in (
        re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL),
        re.compile(r"\{.*\}", re.DOTALL),
    ):
        m = pattern.search(text)
        if not m:
            continue
        try:
            obj = json.loads(m.group(1) if "```" in pattern.pattern else m.group(0))
        except (json.JSONDecodeError, ValueError, IndexError):
            continue
        if isinstance(obj, dict) and isinstance(obj.get("edits"), list):
            return [e for e in obj["edits"] if isinstance(e, dict)]
    return None


def jaccard_dedup_edits(
    edits: list[Edit],
    existing_rules: str = "",
    *,
    threshold: float = 0.5,
) -> list[Edit]:
    """Drop edits that Jaccard-overlap with existing rules or with each other.

    Phase 1: Remove edits whose content is >= ``threshold`` similar to any
    paragraph in the current ``rules.md`` text (prevents re-adding what's
    already there).

    Phase 2: Among survivors, when two edits are >= ``threshold`` similar,
    keep the one with higher support_count (failure-first tiebreak).

    Delete edits are always kept (their content field is irrelevant).
    """
    existing_sets: list[set[str]] = []
    if existing_rules and existing_rules.strip():
        for para in re.split(r"\n\s*\n|\n(?=- )", existing_rules):
            tokens = _tokenize(para)
            if len(tokens) >= 3:
                existing_sets.append(tokens)

    surviving: list[Edit] = []
    for edit in edits:
        if edit.op in ("delete", "delete_section"):
            surviving.append(edit)
            continue
        tokens = _tokenize(edit.content)
        if len(tokens) < 3:
            surviving.append(edit)
            continue
        if any(_jaccard(tokens, ex) >= threshold for ex in existing_sets):
            continue
        surviving.append(edit)

    if len(surviving) <= 1:
        return surviving

    token_sets = [_tokenize(e.content) for e in surviving]
    keep = [True] * len(surviving)
    for i in range(len(surviving)):
        if not keep[i]:
            continue
        for j in range(i + 1, len(surviving)):
            if not keep[j]:
                continue
            if len(token_sets[i]) < 3 or len(token_sets[j]) < 3:
                continue
            if _jaccard(token_sets[i], token_sets[j]) < threshold:
                continue
            si = surviving[i].support_count or 0
            sj = surviving[j].support_count or 0
            fi = 0 if surviving[i].source_type == "failure" else 1
            fj = 0 if surviving[j].source_type == "failure" else 1
            if (-si, fi) <= (-sj, fj):
                keep[j] = False
            else:
                keep[i] = False
                break

    return [e for e, k in zip(surviving, keep) if k]


# ── LLM-based semantic dedup ─────────────────────────────────────────────────

_DEDUP_SYSTEM = """\
You are a deduplication filter. You are given a numbered list of proposed edits \
to a rules document. Edits may operate at line-level (append, insert_after, \
replace, delete) or section-level (add_section, rewrite_section, delete_section). \
Identify SEMANTICALLY DUPLICATE edits — edits that express the same guidance \
even if worded differently, including across different op types (e.g. an \
`add_section` and an `append` with the same content are duplicates).

For each group of duplicates, keep the ONE best version (most precise, most \
actionable) and drop the rest. Edits that address genuinely different topics \
or add genuinely different guidance are NOT duplicates — keep all of them.

Output ONLY a JSON object:
  {"keep": [1, 3, 5]}
where the values are the 1-based indices of edits to KEEP. No prose, no fences."""


def llm_semantic_dedup(
    client: "LLMClient",
    edits: list[Edit],
) -> list[Edit]:
    """One LLM call to identify and remove semantically duplicate edits.

    Presents all edit contents to the optimizer model and asks it to pick the
    non-redundant subset. On any LLM or parsing failure, returns the original
    list unchanged (safe degradation).

    When duplicates are merged, the surviving edit inherits the sum of
    ``support_count`` from all edits in its duplicate group: each dropped edit
    contributes its support to the nearest kept edit by index (the LLM keeps the
    "best" of each group), so consensus is preserved rather than discarded.
    """
    if len(edits) <= 1:
        return list(edits)

    lines: list[str] = []
    for i, e in enumerate(edits, 1):
        preview = (e.content or "").strip().replace("\n", " ")
        if len(preview) > 300:
            preview = preview[:300] + "..."
        lines.append(f"{i}. [{e.op}] {preview}")

    user = "Proposed edits to deduplicate:\n" + "\n".join(lines)

    try:
        keep_indices = complete_optimizer_json(
            client, _DEDUP_SYSTEM, user,
            parse=lambda t: _parse_keep_indices(t, len(edits)),
            max_tokens=1024, stage="dedup",
        )
    except Exception:
        return list(edits)

    if not keep_indices:
        return list(edits)

    # Merge support_counts from dropped edits into their nearest kept edit.
    sorted_kept = sorted(keep_indices)
    for i in range(len(edits)):
        if i in keep_indices:
            continue
        closest = min(sorted_kept, key=lambda k: abs(k - i))
        edits[closest].support_count = (
            (edits[closest].support_count or 0) + (edits[i].support_count or 0)
        )

    return [edits[i] for i in sorted_kept]


def _parse_keep_indices(text: str, n_edits: int) -> set[int] | None:
    """Extract 0-based keep indices from the LLM dedup response."""
    if not text:
        return None
    for pattern in [
        re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL),
        re.compile(r"\{.*\}", re.DOTALL),
    ]:
        m = pattern.search(text)
        if not m:
            continue
        try:
            obj = json.loads(m.group(1) if pattern.groups else m.group(0))
        except (json.JSONDecodeError, ValueError, IndexError):
            continue
        if isinstance(obj, dict):
            raw = obj.get("keep", [])
            if isinstance(raw, list):
                indices: set[int] = set()
                for v in raw:
                    try:
                        idx = int(v) - 1
                        if 0 <= idx < n_edits:
                            indices.add(idx)
                    except (TypeError, ValueError):
                        pass
                if indices:
                    return indices
    return None


def select_top_edits(patch: Patch, max_edits: int) -> Patch:
    """Rank a merged patch's edits and cap to ``max_edits``.

    Sort key (stable): ``support_count`` descending, then failure-driven edits
    before success-driven ones, preserving original order for ties. The cap
    keeps the highest-consensus / highest-priority edits the L0 step will apply.
    """
    edits = list(patch.edits)

    def sort_key(item: tuple[int, Edit]) -> tuple[int, int, int]:
        idx, edit = item
        support = edit.support_count or 0
        source_rank = 0 if (edit.source_type == "failure") else 1
        # Negative support for descending; idx keeps ties stable.
        return (-support, source_rank, idx)

    ranked = [e for _, e in sorted(enumerate(edits), key=sort_key)]
    if max_edits is not None and max_edits >= 0:
        ranked = ranked[:max_edits]

    return Patch(
        edits=ranked,
        reasoning=patch.reasoning,
        ranking_details={
            "n_input": len(edits),
            "n_selected": len(ranked),
            "max_edits": max_edits,
            "ranking": "support_count desc, failure-first, stable",
        },
    )
