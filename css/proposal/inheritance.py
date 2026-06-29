"""Phase 5 — knowledge inheritance for PROPOSAL and REFINE (design §4.2 / §4.3, D4 / D10).

.. deprecated::
    DEAD in v3. PROPOSAL now deploys with ``rules=""`` (empty rules), so rule
    inheritance is unnecessary. ``proposal_inherit_rules`` and
    ``refine_apply_cleanup`` are no longer called from the main flow
    (``run_l1_cycle`` does not invoke them). This module is retained only because
    existing tests import it; do NOT add new callers.

When a strategy change is accepted, the L0 ``rules.md`` of the parent node cannot
be carried over blindly: a rule that made sense under the OLD strategy may now
contradict the NEW strategy (e.g. a rule that hard-codes a workaround for a habit
the new strategy explicitly abandons). The two operations inherit differently:

  * PROPOSAL (the bigger, strategy-level change) — the optimizer makes a SEMANTIC
    keep/drop judgment per parent rule: keep rules still compatible with the new
    strategy, drop rules that contradict it. Implemented by
    :func:`proposal_inherit_rules`, which prompts the optimizer with the new
    strategy + the parent rules and parses back the kept rules as a single
    ``rules.md`` text ("" if nothing survives).

  * REFINE (the smaller, local change) — "full inherit + conflict cleanup": the
    child starts from the COMPLETE parent rules and the only change permitted is
    to *clean up* rules that now conflict with the refined subsections. The
    cleanup is a :class:`~css.data.edit.Patch` restricted to ``replace`` / ``delete``
    ops (NO additions — additions are L0's job, not inheritance). Implemented by
    :func:`refine_apply_cleanup`, which asserts the cleanup is addition-free (via
    :func:`css.proposal.refine.is_cleanup_only`) and then applies it through the
    deterministic :func:`css.optimizer.edit_engine.apply_patch`.

Robustness contract (mirrors the rest of Phase 5): malformed optimizer output in
:func:`proposal_inherit_rules` never crashes — it degrades to a conservative,
documented fallback rather than raising.
"""
from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from css.model.json_repair import complete_optimizer_json
from css.optimizer.edit_engine import apply_patch
from css.proposal.refine import is_cleanup_only

if TYPE_CHECKING:  # pragma: no cover - type-only imports
    from css.config import CSSConfig
    from css.data.edit import EditReport, Patch
    from css.model.client import LLMClient


# ── Prompt: PROPOSAL keep/drop rule inheritance ──────────────────────────────

_INHERIT_SYSTEM = """\
You are deciding which low-level operating rules an AI agent should KEEP after \
its high-level COGNITIVE STRATEGY has just been replaced. The agent uses two \
documents: a strategy document (how it should THINK) and a rules document \
(``rules.md`` — concrete low-level "always/never do X" rules layered on top of \
the strategy). The strategy was just rewritten. Some existing rules still make \
sense under the new strategy; others were written to patch behaviors the OLD \
strategy caused and now CONTRADICT or are made REDUNDANT by the new strategy.

Your job is a strict KEEP/DROP judgment over the existing rules — NOT rewriting \
them, NOT inventing new rules. For each rule you must decide:
  - KEEP: the rule is still compatible with — or actively reinforces — the new \
    strategy. Carry it over UNCHANGED.
  - DROP: the rule contradicts the new strategy, or it only existed to compensate \
    for a habit the new strategy explicitly removes (so it is now redundant or \
    actively harmful). Discard it.

Be conservative about dropping: only drop a rule when you can name the specific \
tension with the new strategy. A rule that is merely unrelated to the strategy \
change is still compatible — KEEP it. Preserve each kept rule's original wording \
verbatim; do not paraphrase, merge, reorder semantics, or add anything new.

Output ONLY a JSON object:
  {
    "kept_rules": ["<verbatim text of a rule to keep>", ...],
    "dropped": [{"rule": "<verbatim text>", "reason": "<why it contradicts the new strategy>"}, ...]
  }
"kept_rules" is the list of surviving rules in their original order; it may be \
empty if every rule contradicts the new strategy. No prose, no markdown fences, \
no commentary — just the JSON object."""

_INHERIT_USER_TMPL = """\
NEW STRATEGY DOCUMENT (the agent's thinking has just been changed to this):
-------------------------------------------------------------
{strategy}
-------------------------------------------------------------

EXISTING rules.md (written under the OLD strategy — judge each rule):
-------------------------------------------------------------
{rules}
-------------------------------------------------------------

Decide KEEP vs DROP for each existing rule under the NEW strategy. Keep every \
rule that remains compatible (verbatim); drop only rules that contradict the new \
strategy or that only existed to patch a habit the new strategy removes, naming \
the specific tension. Respond with ONLY the JSON object described in the \
instructions."""


# ── Robust parse helpers (best-first candidate extraction, mirrors root_cause) ─

def _json_candidates(text: str) -> list[str]:
    """Yield candidate JSON substrings from noisy LLM output (best-first)."""
    if not text:
        return []
    candidates: list[str] = []
    for m in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.DOTALL):
        candidates.append(m.group(1).strip())
    candidates.append(text.strip())
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        candidates.append(m.group(0))
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        candidates.append(m.group(0))
    return candidates


def _coerce_kept(obj: Any) -> list[str] | None:
    """Coerce a parsed JSON value into a list of kept-rule strings, or None.

    Accepts the documented ``{"kept_rules": [...]}`` shape, a few tolerant
    aliases, and a bare JSON list of strings.
    """
    if isinstance(obj, dict):
        for key in ("kept_rules", "keep", "kept", "rules"):
            inner = obj.get(key)
            if isinstance(inner, list):
                return [str(r) for r in inner if isinstance(r, (str,)) and str(r).strip()]
        return None
    if isinstance(obj, list):
        # A bare list — accept it only if it is a list of plain strings (the
        # "kept rules" list emitted without the wrapper object). Lists of dicts
        # are ambiguous (could be the dropped list) so we reject them here.
        if all(isinstance(r, str) for r in obj):
            return [str(r) for r in obj if str(r).strip()]
    return None


def _parse_kept_rules(text: str) -> list[str] | None:
    """Extract the kept-rule list from noisy LLM output.

    Returns the list of kept rule strings, or ``None`` when nothing parseable is
    found (so the caller can apply a conservative fallback rather than silently
    treating an unparseable response as "drop everything").
    """
    for cand in _json_candidates(text):
        if not cand:
            continue
        try:
            obj = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        kept = _coerce_kept(obj)
        if kept is not None:
            return kept
    return None


def _join_rules(kept: list[str]) -> str:
    """Join kept rule strings into a single rules.md text ("" if none)."""
    cleaned = [r.strip("\n").rstrip() for r in kept if r and r.strip()]
    if not cleaned:
        return ""
    return "\n".join(cleaned).strip() + "\n"


# ── PROPOSAL inheritance: semantic keep/drop ─────────────────────────────────

def proposal_inherit_rules(
    client: "LLMClient",
    new_strategy: str,
    parent_rules: str,
    *,
    cfg: "CSSConfig",
) -> str:
    """LLM semantic keep/drop of parent rules under a NEW strategy (PROPOSAL).

    Prompts the optimizer with ``new_strategy`` + ``parent_rules`` and asks which
    existing rules remain compatible (keep, verbatim) versus contradict the new
    strategy (drop). Returns the kept rules as a single ``rules.md`` text — ``""``
    if none are kept or if the parent had no rules.

    Robustness: if the parent rules are empty there is nothing to inherit
    (returns ``""`` without calling the model). If the optimizer output cannot be
    parsed, we conservatively fall back to carrying the parent rules over
    UNCHANGED (the REFINE-style "full inherit") rather than guessing a drop — a
    PROPOSAL must never silently discard rules on a parse failure.
    """
    if not parent_rules or not parent_rules.strip():
        return ""

    user = _INHERIT_USER_TMPL.format(
        strategy=(new_strategy or "").strip() or "(empty)",
        rules=parent_rules.strip(),
    )
    del cfg  # signature symmetry with the other Phase-5 derive/validate calls;
    # the optimizer call uses the client's default token budget (as root_cause.py).
    try:
        kept = complete_optimizer_json(
            client, _INHERIT_SYSTEM, user, parse=_parse_kept_rules,
            ok=lambda r: r is not None, stage="inherit",
        )
    except Exception:
        # Model/transport failure -> conservative full inherit.
        return parent_rules.strip() + "\n"

    if kept is None:
        # Unparseable -> conservative full inherit (do not drop on noise).
        return parent_rules.strip() + "\n"
    return _join_rules(kept)


# ── REFINE inheritance: full inherit + conflict cleanup ──────────────────────

def refine_apply_cleanup(
    parent_rules: str,
    cleanup: "Patch",
) -> tuple[str, list["EditReport"]]:
    """Apply a REFINE rules cleanup to the FULL parent rules (full inherit + cleanup).

    REFINE inherits the COMPLETE parent ``rules.md`` and only cleans up rules that
    now conflict with the refined strategy subsections. The cleanup ``Patch`` is
    therefore restricted to ``replace`` / ``delete`` ops: it may MODIFY or REMOVE
    existing rules but may NOT add new ones (additions are L0 EXPLOITATION's job).

    This function enforces that contract: it asserts the cleanup is addition-free
    via :func:`css.proposal.refine.is_cleanup_only` and raises ``ValueError`` if an
    ``append`` / ``insert_after`` op slipped in, BEFORE applying anything. Then it
    delegates to the deterministic :func:`css.optimizer.edit_engine.apply_patch`
    and returns ``(new_rules_text, reports)``.
    """
    if not is_cleanup_only(cleanup):
        raise ValueError(
            "REFINE rules cleanup must contain only 'replace'/'delete' ops "
            "(no additions); an 'append'/'insert_after' op was found."
        )
    # Full inherit: start from the complete parent rules, apply cleanup in place.
    return apply_patch(parent_rules or "", cleanup)
