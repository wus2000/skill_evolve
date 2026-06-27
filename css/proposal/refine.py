"""Phase 5 REFINE — Layer-5 LOCAL change derivation (design §4.2 / D10).

PROPOSAL and REFINE share Layers 1-4 (an L1 signal + its four-level
root-cause attribution from :mod:`css.proposal.root_cause`). They DIVERGE at
Layer 5:

  * PROPOSAL rewrites the whole strategy (a structural, "new tree branch"
    change derived from the root cause — see :mod:`css.proposal.derivation`).
  * REFINE makes a *controlled, local* change: it rewrites only 1-2 ``###``
    subsections of the parent strategy that the root cause directly implicates,
    leaving every other subsection byte-identical, and then cleans up any L0
    rules in ``rules.md`` that now CONFLICT with the refined thinking. Per D10,
    REFINE is the cheaper, lower-variance edit you try before escalating to a
    full PROPOSAL once ``refine_count`` exhausts ``K``.

Two design constraints make this module deliberately narrow:

  1. The "1-2 subsection" guarantee is NOT entrusted to LLM self-report. After
     the LLM returns a full candidate strategy, we re-derive the change set
     deterministically via :func:`css.markdown_utils.check_refine_diff`
     (REUSED, not reimplemented). If the candidate touched too many subsections,
     changed the subsection *set* (add/remove a heading), or perturbed an
     "unchanged" subsection's body, the gate fails and we return
     ``(None, reason)`` so the caller (:func:`css.proposal.proposal.run_refine`)
     can escalate to PROPOSAL. We never silently accept an over-broad edit.

  2. The rules cleanup is MODIFY/DELETE ONLY. REFINE inherits the parent rules
     in full (knowledge inheritance, D10) and may only *remove or rewrite* rules
     that now contradict the refined strategy — it must NOT add new rules (that
     is the L0 EXPLOITATION optimizer's job, not L1's). So any ``append`` /
     ``insert_after`` op the LLM proposes is DROPPED here, and the surviving
     :class:`~css.data.edit.Patch` is asserted cleanup-only via
     :func:`is_cleanup_only` before it ever reaches the edit engine.

Robustness contract (mirrors css/proposal/root_cause.py): malformed LLM output
never crashes. :func:`derive_refine` returns ``(None, reason)`` on any parse /
gate failure so the caller can route to PROPOSAL.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from css.data.edit import EDIT_OPS, Edit, Patch
from css.markdown_utils import check_refine_diff, diff_subsections
from css.proposal.root_cause import RootCause

if TYPE_CHECKING:  # pragma: no cover - type-only imports
    from css.config import CSSConfig
    from css.model.client import LLMClient


# Edit ops that ADD content to rules.md. REFINE cleanup is modify/delete only,
# so these are rejected/dropped (additions are the L0 optimizer's job, not L1).
_ADDITIVE_OPS: frozenset[str] = frozenset({"append", "insert_after"})
# The complement: the only ops a REFINE rules-cleanup Patch may contain.
_CLEANUP_OPS: frozenset[str] = frozenset({"replace", "delete"})


# ── Data model ───────────────────────────────────────────────────────────────

@dataclass
class RefineProposal:
    """A LOCAL strategy edit (1-2 ``###`` subsections) + conflicting-rules cleanup.

    ``strategy_text`` is the FULL candidate strategy document (every subsection
    the refine did not touch is byte-identical to the parent). ``changed_subsections``
    is the deterministic diff result (heading keys that actually changed), NOT the
    LLM's self-report. ``rules_cleanup`` is a modify/delete-only :class:`Patch`
    removing/rewriting rules that now conflict with the refined thinking;
    :func:`is_cleanup_only` holds for it by construction.
    """

    strategy_text: str
    changed_subsections: list[str]
    root_cause: "RootCause"
    rules_cleanup: "Patch"

    @classmethod
    def from_dict(cls, d: dict) -> "RefineProposal":
        subs = d.get("changed_subsections", [])
        if isinstance(subs, str):
            subs = [subs]
        elif not isinstance(subs, list):
            subs = []
        rc_raw = d.get("root_cause", {})
        rc = rc_raw if isinstance(rc_raw, RootCause) else RootCause.from_dict(
            rc_raw if isinstance(rc_raw, dict) else {}
        )
        cl_raw = d.get("rules_cleanup", {})
        cleanup = cl_raw if isinstance(cl_raw, Patch) else Patch.from_dict(
            cl_raw if isinstance(cl_raw, dict) else {}
        )
        return cls(
            strategy_text=str(d.get("strategy_text", "")),
            changed_subsections=[str(s) for s in subs],
            root_cause=rc,
            rules_cleanup=cleanup,
        )

    def to_dict(self) -> dict:
        return {
            "strategy_text": self.strategy_text,
            "changed_subsections": list(self.changed_subsections),
            "root_cause": self.root_cause.to_dict(),
            "rules_cleanup": self.rules_cleanup.to_dict(),
        }


# ── Cleanup-only predicate (frozen public API) ───────────────────────────────

def is_cleanup_only(patch: "Patch") -> bool:
    """True iff EVERY edit op in ``patch`` is a modify/delete (no additions).

    A REFINE rules cleanup may only ``replace`` or ``delete`` rules that now
    conflict with the refined strategy; it must never ``append`` / ``insert_after``
    (adding rules is the L0 EXPLOITATION optimizer's job). An empty patch is
    vacuously cleanup-only. Never raises.
    """
    if patch is None:
        return True
    edits = getattr(patch, "edits", None)
    if not edits:
        return True
    for e in edits:
        op = getattr(e, "op", None)
        if op not in _CLEANUP_OPS:
            return False
    return True


def _sanitize_cleanup(patch: "Patch") -> "Patch":
    """Drop any additive ops so the surviving Patch is modify/delete only.

    Returns a NEW :class:`Patch` keeping only ``replace`` / ``delete`` edits
    (each with a usable ``target``); the original ``reasoning`` is preserved so
    provenance survives. By construction :func:`is_cleanup_only` holds for the
    result.
    """
    kept: list[Edit] = []
    for e in getattr(patch, "edits", []) or []:
        op = getattr(e, "op", None)
        if op not in _CLEANUP_OPS:
            continue  # drop append / insert_after / unknown ops
        # replace/delete are anchored edits; without a target they are no-ops
        # the edit engine would only skip, so drop them here for a clean patch.
        if not (getattr(e, "target", "") or "").strip():
            continue
        kept.append(e)
    return Patch(edits=kept, reasoning=getattr(patch, "reasoning", "") or "")


# ── Prompt: derive a LOCAL refine from the root cause ─────────────────────────

_REFINE_SYSTEM = """\
You are refining an AI agent's COGNITIVE STRATEGY document. A root cause for a \
persistent failure pattern has already been diagnosed for you (four levels: the \
observable behavior, the agent's own reasoning process, the SPECIFIC strategy \
text that permitted it, and the hidden assumption that text bakes in). Your job \
is a REFINE: a controlled, LOCAL edit — NOT a rewrite.

A REFINE is bound by hard rules:
  1. LOCALITY. Rewrite ONLY the 1-2 subsections of the strategy that the \
     root cause's STRATEGY level (level 3) and ASSUMPTION level (level 4) directly \
     implicate. Every OTHER subsection — its heading and its body — must be \
     reproduced BYTE-FOR-BYTE, unchanged. Do not add, remove, reorder, or rename \
     subsections. Do not touch any preamble text before the first section heading. \
     If a correct fix would require touching more than 2 subsections or changing \
     the document's shape, say so in "escalate" and STOP — that is a PROPOSAL, \
     not a REFINE.
  2. DERIVE, DON'T INVENT. The edit must be the logical consequence of the \
     diagnosed assumption: break that assumption and write the thinking the \
     assumption was suppressing. Keep the surrounding strategy's voice and format.
  3. RULES CLEANUP IS MODIFY/DELETE ONLY. The agent also follows a separate \
     low-level rules document (rules.md). After the refine, some existing rules \
     may CONTRADICT the refined thinking. You may ONLY remove or rewrite such \
     conflicting rules. You MUST NOT add any new rule — adding rules is a \
     different optimizer's job. Express each cleanup as an edit op:
        - {"op": "delete", "target": "<verbatim text of the conflicting rule>"}
        - {"op": "replace", "target": "<verbatim old rule>", "content": "<rewritten, non-conflicting rule>"}
     The "target" MUST be verbatim text that appears in the rules document. If no \
     rule conflicts, return an empty "edits" list. NEVER use "append" or \
     "insert_after".

Output ONLY a JSON object, no prose, no markdown fences:
  {
    "strategy_text": "<the FULL refined strategy document — only the 1-2 implicated subsections differ; all other text byte-identical to the parent>",
    "changed_subsections": ["<heading text of each subsection you edited>"],
    "rationale": "<why this local edit is the logical consequence of the diagnosed assumption>",
    "rules_cleanup": {
      "reasoning": "<which rules conflicted and why>",
      "edits": [ {"op": "delete"|"replace", "target": "...", "content": "..."} ]
    },
    "escalate": "<'' if a clean 1-2 subsection refine is possible; otherwise explain why this needs a full PROPOSAL>"
  }"""

_REFINE_USER_TMPL = """\
DIAGNOSED ROOT CAUSE (Layers 1-4 — already established; do not re-diagnose):
  patterns targeted: {pattern_ids}
  1) behavioral (what the agent observably did): {behavioral}
  2) process (the agent's own reasoning behind it): {process}
  3) strategy (the SPECIFIC strategy text that permitted it): {strategy}
  4) assumption (the hidden belief that text bakes in, which the failures disprove): {assumption}
  leverage: {leverage}
  why L0 rules could not fix it: {l0_explanation}

PARENT STRATEGY DOCUMENT (edit ONLY 1-2 subsections; reproduce the rest verbatim):
-------------------------------------------------------------
{parent_strategy}
-------------------------------------------------------------

PARENT RULES DOCUMENT (rules.md — you may only DELETE/REPLACE conflicting rules, never add):
-------------------------------------------------------------
{parent_rules}
-------------------------------------------------------------

Produce the LOCAL refine: rewrite only the subsection(s) the assumption implicates \
so the broken assumption is corrected, keep every other subsection byte-identical, \
and clean up (delete/replace only) any rule that now conflicts. Respond with ONLY \
the JSON object described in the instructions."""


# ── Robust JSON extraction (mirrors css/proposal/root_cause.py) ──────────────

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
    return candidates


def _parse_refine_obj(text: str) -> dict | None:
    """Extract the refine JSON object from noisy LLM output, or ``None``.

    Returns the first parseable mapping (unwrapping a single-element list if the
    LLM wrapped the object). Never raises.
    """
    for cand in _json_candidates(text):
        if not cand:
            continue
        try:
            obj = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, list):
            obj = next((o for o in obj if isinstance(o, dict)), None)
        if isinstance(obj, dict):
            return obj
    return None


def _build_cleanup_patch(raw: Any) -> "Patch":
    """Build a modify/delete-only cleanup :class:`Patch` from raw LLM JSON.

    Accepts either a patch-shaped dict (``{"edits": [...], "reasoning": ...}``)
    or a bare list of edit dicts. Additive / unknown ops and untargeted edits are
    dropped via :func:`_sanitize_cleanup`. Never raises.
    """
    if isinstance(raw, Patch):
        return _sanitize_cleanup(raw)
    if isinstance(raw, list):
        raw = {"edits": raw}
    if not isinstance(raw, dict):
        return Patch(edits=[], reasoning="")
    try:
        patch = Patch.from_dict(raw)
    except Exception:
        return Patch(edits=[], reasoning="")
    return _sanitize_cleanup(patch)


# ── Public API ───────────────────────────────────────────────────────────────

def derive_refine(
    client: "LLMClient",
    root_cause: "RootCause",
    parent_strategy: str,
    parent_rules: str,
    *,
    cfg: "CSSConfig",
    max_changed: int = 2,
) -> tuple["RefineProposal | None", str]:
    """Layer-5 REFINE: derive a LOCAL strategy edit + conflicting-rules cleanup.

    Prompts the optimizer to rewrite ONLY the 1-2 ``###`` subsections the
    ``root_cause`` implicates (returning the FULL modified strategy, other
    subsections byte-identical) plus a modify/delete-only ``rules_cleanup``.

    The candidate is then GATED deterministically with
    :func:`css.markdown_utils.check_refine_diff(parent_strategy, candidate,
    max_changed=max_changed)` — we do NOT trust the LLM's self-report of how much
    it changed. On any failure (parse failure, the LLM signalling ``escalate``,
    an empty/identical candidate, a structural change, perturbed "unchanged"
    subsections, or > ``max_changed`` subsections) this returns
    ``(None, reason)`` so :func:`css.proposal.proposal.run_refine` can escalate to
    PROPOSAL. ``changed_subsections`` on the returned proposal is taken from the
    deterministic diff, not the LLM. Any ``append`` / ``insert_after`` the LLM
    proposed for the rules cleanup is dropped; the surviving :class:`Patch`
    satisfies :func:`is_cleanup_only` by construction. Never raises.
    """
    del cfg  # thresholds are not consulted here; signature kept for symmetry.

    parent_strategy = parent_strategy or ""
    parent_rules = parent_rules or ""

    user = _REFINE_USER_TMPL.format(
        pattern_ids=", ".join(root_cause.pattern_ids) or "(unspecified)",
        behavioral=root_cause.behavioral or "(none)",
        process=root_cause.process or "(none)",
        strategy=root_cause.strategy or "(none)",
        assumption=root_cause.assumption or "(none)",
        leverage=root_cause.leverage or "(single pattern)",
        l0_explanation=root_cause.l0_explanation or "(none)",
        parent_strategy=parent_strategy or "(parent strategy unavailable)",
        parent_rules=parent_rules or "(no rules.md / empty)",
    )

    try:
        text, _usage = client.complete_optimizer(_REFINE_SYSTEM, user)
    except Exception as exc:
        return None, f"llm_error:{type(exc).__name__}"

    obj = _parse_refine_obj(text)
    if obj is None:
        return None, "unparseable_refine_output:escalate_to_proposal"

    # The LLM may explicitly decline a clean local edit -> escalate.
    escalate = str(obj.get("escalate", "") or "").strip()
    if escalate:
        return None, f"llm_escalated:{escalate[:200]}:escalate_to_proposal"

    candidate = str(obj.get("strategy_text", "") or "")
    if not candidate.strip():
        return None, "empty_candidate_strategy:escalate_to_proposal"

    # CODE GATE (deterministic; REUSED, not reimplemented): at least one and at
    # most ``max_changed`` ### subsections changed, no structural change, and
    # every untouched subsection byte-identical to the parent.
    ok, reason = check_refine_diff(parent_strategy, candidate, max_changed=max_changed)
    if not ok:
        return None, reason

    # changed_subsections from the DETERMINISTIC diff, not the LLM's self-report.
    diff = diff_subsections(parent_strategy, candidate)
    changed = list(diff.changed_keys)

    # Rules cleanup: modify/delete only. Drop any additive / untargeted ops; the
    # result satisfies is_cleanup_only by construction (assert defensively).
    cleanup = _build_cleanup_patch(obj.get("rules_cleanup", {}))
    if not is_cleanup_only(cleanup):  # pragma: no cover - guaranteed by sanitize
        cleanup = Patch(edits=[], reasoning=getattr(cleanup, "reasoning", "") or "")

    proposal = RefineProposal(
        strategy_text=candidate,
        changed_subsections=changed,
        root_cause=root_cause,
        rules_cleanup=cleanup,
    )
    return proposal, reason
