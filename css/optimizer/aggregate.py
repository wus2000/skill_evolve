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

import re

from css.data.edit import Edit, Patch, RawPatch

__all__ = ["aggregate_patches", "select_top_edits"]


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
            if edit_source == "failure":
                has_failure[key] = True
            elif edit_source == "success":
                has_success[key] = True
            else:
                # Unknown source: fall back to the raw patch's classification.
                if rp_source == "failure":
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
