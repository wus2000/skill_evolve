"""Semantic adjudication loop: detect (mechanical + LLM) -> repair (ID-diff)
-> converge or fall back deterministically.

Detection is two-source each round:
  * mechanical: the disjointness table (schema.detect_conflicts) recomputed
    on the current edit set — reliable, free;
  * semantic: one LLM validator call (independence + content purity), the
    judgement mechanical code cannot make.

Repair is the proven ID-diff protocol: the LLM sees the edit set with
transient ``E#n`` handles plus the violations, and emits a SMALL list of
operations (replace / drop / merge / add) restricted to the implicated IDs.
Every drop is audited with the violation context that motivated it.

Non-convergence falls back deterministically, and the fallback NEVER deletes
content: colliding identities keep the max-support claimant and demote the
rest to point-additions; unresolvable placement/anchor issues are left to the
apply stage's content-preserving degradation chain; purity issues are
delivered as-is and left to per-edit verification — the pipeline's objective
backstop — to kill. Blocking forever is not an option; silently losing
signal is not either.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from css.model.json_repair import complete_optimizer_json
from css.optimizer.editpipe.render import RulesDoc, demote_headings
from css.optimizer.editpipe.schema import (
    EditAudit,
    SectionEdit,
    Violation,
    detect_conflicts,
    detect_restatements,
    syntax_gate,
)

_log = logging.getLogger(__name__)

MAX_ROUNDS = 3

# Violations the apply stage degrades safely on its own — they never block
# convergence and are excluded from the repair conversation after round 1
# fails to fix them (the fix is cheap downstream, the loop is expensive).
_APPLY_DEGRADABLE = frozenset({
    "missing_anchor", "missing_target", "dependency_on_new",
    "anchor_not_found", "ambiguous_anchor", "missing_body",
    "missing_subject", "missing_target_tasks", "invalid_kind",
})

# Violation types produced by the mechanical detectors (recomputable after
# the loop). Anything else in the final round's blocking set is
# semantic-only and must be CARRIED to the fallback, or it would vanish
# without an accepted-risk audit.
_MECH_TYPES = frozenset({
    "identity_collision", "add_exists", "section_conflict",
    "anchor_overlap", "restates_existing", "restates_sibling",
    "body_contains_heading",
})


@dataclass
class AdjudicationResult:
    edits: list[SectionEdit]
    audits: list[EditAudit] = field(default_factory=list)
    accepted_risks: list[Violation] = field(default_factory=list)
    rounds: int = 0
    converged: bool = True
    llm_calls: int = 0


# ── Semantic validator (LLM) ─────────────────────────────────────────────────

_VALIDATOR_SYSTEM = """\
You are an edit validator for a rules.md optimization pipeline. You perform \
two checks on a set of proposed edits: INDEPENDENCE (can they be applied \
independently and simultaneously without conflicts?) and CONTENT PURITY \
(is every edit's body pure agent-facing instruction text?).

Each edit has: kind (add_section | rewrite_section | remove_section | \
add_point | edit_point | remove_point), subject (the section it defines or \
modifies), body (content), anchor (point ops: the text it targets), \
placement (add_section only).

## Check 1 — Independence (point-level edits)

Two edits are INDEPENDENT iff they target distinct, non-overlapping text and \
applying one does not change the text the other targets, so their combined \
effect equals the sum of their individual effects.

Key rule: multiple add_point edits sharing the same anchor are ALLOWED — \
insertions do not modify existing text. Do NOT flag them.

Conflict patterns (non-exhaustive):
- **Modification overlap**: two edit_point/remove_point edits target the \
same or overlapping text (even with slightly different anchor wording).
- **Causal dependency**: applying edit A changes/removes text that edit B's \
anchor references.
- **Anchor not found**: an anchor does not exist in its subject section.
- **Ambiguous anchor**: an anchor matches multiple locations (does NOT \
apply to add_point).
- **Paraphrase duplicate**: two edits state the same rule in different
words — including an add_section whose theme an existing section (or another
edit in this set) already owns under a different name.

## Check 2 — Content purity (every edit's body)

The body is what a SEPARATE task-executing agent reads as operational rules. \
Flag bodies containing optimization-process material: provenance or \
justification prose, references to training tasks or task identifiers, \
protocol bookkeeping (edit IDs, verification outcomes), meta commentary \
about the optimization process. Also flag a body whose topic plainly does \
not belong under its subject (semantic misplacement).

Judge by SEMANTICS, not keywords. When uncertain, do NOT flag.

## Output format — JSON only, no fences, no prose
{
  "valid": true/false,
  "violations": [
    {
      "type": "anchor_overlap | paraphrase_duplicate | causal_dependency | \
anchor_not_found | ambiguous_anchor | content_purity",
      "edit_indices": [i, j],
      "detail": "<the specific problem>",
      "suggestion": "<how to fix it>"
    }
  ]
}

If everything passes, return {"valid": true, "violations": []}.
Report ONLY genuine violations."""


def _render_edit(i: int, e: SectionEdit) -> str:
    lines = [f"E#{i}: [{e.kind}] subject={e.subject!r}"]
    if e.anchor:
        lines.append(f"  anchor: {e.anchor!r}")
    if e.placement:
        lines.append(f"  placement: {e.placement!r}")
    if e.target_tasks:
        lines.append(f"  target_tasks: {e.target_tasks}")
    body = (e.body or "").replace("\n", "\n    ")
    lines.append(f"  body:\n    {body}")
    return "\n".join(lines)


def _build_validator_user(rules_md: str, edits: list[SectionEdit]) -> str:
    doc = RulesDoc.parse(rules_md)
    point_subjects = {e.key for e in edits if e.is_point}
    parts: list[str] = []
    if point_subjects:
        parts.append("## Relevant sections from rules.md")
        for sec in doc.sections:
            if sec.key in point_subjects:
                parts.append(f"--- {sec.subject} ---\n{sec.body}")
    parts.append(f"## Edits to validate ({len(edits)} total)")
    for i, e in enumerate(edits):
        parts.append(_render_edit(i, e))
    return "\n\n".join(parts)


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_JSON_BARE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_json_obj(text: str, must_have: str) -> dict | None:
    if not text:
        return None
    for pattern in (_JSON_FENCE, _JSON_BARE):
        m = pattern.search(text)
        if not m:
            continue
        try:
            raw = m.group(1) if pattern is _JSON_FENCE else m.group(0)
            obj = json.loads(raw)
        except (json.JSONDecodeError, ValueError, IndexError):
            continue
        if isinstance(obj, dict) and must_have in obj:
            return obj
    return None


def semantic_check(
    client: Any, rules_md: str, edits: list[SectionEdit],
) -> list[Violation]:
    """One LLM validator call (with structural JSON repair). On failure
    returns [] (optimistic — the mechanical detections still stand and
    apply still degrades safely)."""
    if not edits:
        return []
    user = _build_validator_user(rules_md, edits)
    try:
        obj = complete_optimizer_json(
            client, _VALIDATOR_SYSTEM, user,
            parse=lambda t: _parse_json_obj(t, "valid"),
            max_tokens=4096,
            stage="editpipe_validator",
        )
    except Exception:
        _log.warning("editpipe.validator: LLM call failed; optimistic",
                     exc_info=True)
        return []
    if obj is None:
        _log.warning("editpipe.validator: unparseable output; optimistic")
        return []
    out: list[Violation] = []
    for v in obj.get("violations", []) or []:
        if not isinstance(v, dict):
            continue
        idxs = [i for i in (v.get("edit_indices") or [])
                if isinstance(i, int) and 0 <= i < len(edits)]
        if not idxs:
            continue
        vtype = str(v.get("type", "semantic"))
        # add_point insertions sharing an anchor are fine by design.
        if vtype in ("anchor_overlap", "paraphrase_duplicate") and all(
                edits[i].kind == "add_point" for i in idxs):
            continue
        detail = str(v.get("detail", ""))
        sugg = str(v.get("suggestion", ""))
        if sugg:
            detail = f"{detail} Suggested fix: {sugg}"
        out.append(Violation(vtype, [f"E#{i}" for i in idxs], detail))
    return out


# ── ID-diff repair ───────────────────────────────────────────────────────────

_REPAIR_SYSTEM = """\
You repair a set of rules.md edits that failed validation. You will fix the
violations by emitting a SMALL list of repair operations. Edits you do not
reference stay EXACTLY as they are — do not re-emit them.

Edit fields: kind (add_section | rewrite_section | remove_section |
add_point | edit_point | remove_point), subject (identity: the section the
edit defines or modifies; for add_section it is the NEW section's own
descriptive name), placement (add_section only: end | start | existing
section name), anchor (point ops: verbatim text from the section), body
(content, NEVER containing a markdown heading line), target_tasks,
rationale, derivation.

Output format — JSON only, no fences, no prose:
{
  "reasoning": "<what you fixed, 1-2 sentences>",
  "operations": [
    {"op": "replace", "id": "E#3", "edit": {<full edit object>}},
    {"op": "drop",    "id": "E#7", "reason": "<why this edit should die>"},
    {"op": "merge",   "ids": ["E#4", "E#9"], "edit": {<full edit object>}},
    {"op": "add",     "edit": {<full edit object>}}
  ]
}

Rules:
- Operations may ONLY reference the edit IDs implicated in the violations
  (listed below). Operations on other IDs will be rejected.
- "merge" removes all listed edits and inserts the single provided edit in
  their place.
- "drop" REQUIRES a reason; it becomes part of the permanent audit trail.
- Prefer content-preserving fixes (rename a subject, split a body, merge
  duplicates) over drops; drop only what is genuinely redundant or harmful.
- IDs like E#3 are protocol handles for THIS conversation only. NEVER write
  them into any edit's body.
- Fix ALL listed violations with the fewest operations possible."""


def _feedback_text(violations: list[Violation]) -> str:
    parts = ["VIOLATIONS FOUND:\n"]
    for i, v in enumerate(violations):
        ids = ", ".join(v.edit_ids)
        parts.append(
            f"Violation {i + 1} [{v.vtype.upper()}] (edits: {ids}):\n"
            f"  {v.detail}")
    return "\n\n".join(parts)


def _implicated(violations: list[Violation]) -> set[int]:
    out: set[int] = set()
    for v in violations:
        for ref in v.edit_ids:
            m = re.match(r"[Ee]#?(\d+)$", ref.strip())
            if m:
                out.add(int(m.group(1)))
    return out


def _parse_ops(text: str) -> list[dict] | None:
    obj = _parse_json_obj(text or "", "operations")
    if obj is None:
        return None
    ops = obj.get("operations")
    if not isinstance(ops, list):
        return None
    return [o for o in ops if isinstance(o, dict)]


def _resolve_id(raw: object) -> int | None:
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        m = re.match(r"[Ee]#?(\d+)$", raw.strip())
        if m:
            return int(m.group(1))
    return None


def _apply_ops(
    edits: list[SectionEdit],
    ops: list[dict],
    allowed: set[int],
    audits: list[EditAudit],
) -> list[SectionEdit]:
    """Apply ID-diff operations. Payloads are constructed tolerantly — a
    payload is rejected only if it cannot name a subject at all; every other
    imperfection is re-detected next round or degraded at apply time."""
    n = len(edits)
    slots: list[SectionEdit | None] = list(edits)
    appended: list[SectionEdit] = []

    def _payload(op: dict, inherit_from: list[SectionEdit] = ()) -> SectionEdit | None:
        d = op.get("edit")
        if not isinstance(d, dict):
            return None
        e = SectionEdit.from_dict(d)
        if not e.subject:
            return None
        # Provenance is mechanical bookkeeping, not judgement: a repair
        # payload that omits target_tasks inherits the union from the edits
        # it replaces/merges, so support is never zeroed by an LLM omission
        # (an empty-support edit would auto-fail verification -> lost signal).
        if not e.target_tasks and inherit_from:
            seen: list[str] = []
            for src in inherit_from:
                for t in src.target_tasks:
                    if t not in seen:
                        seen.append(t)
            e.target_tasks = seen
        return e

    for op in ops:
        kind = str(op.get("op", "")).strip().lower()
        if kind == "add":
            e = _payload(op)
            if e is None or not e.target_tasks:
                _log.warning(
                    "editpipe.repair: rejected add with bad payload "
                    "(missing subject or target_tasks)")
                continue
            appended.append(e)
            audits.append(EditAudit(
                "E#new", e.subject, "kept", "added by repair", "adjudicator"))
        elif kind in ("replace", "drop"):
            idx = _resolve_id(op.get("id"))
            if idx is None or not (0 <= idx < n) or idx not in allowed:
                _log.warning(
                    "editpipe.repair: rejected %s on id %r (unknown or "
                    "outside allowlist)", kind, op.get("id"))
                continue
            old = slots[idx]
            if kind == "drop":
                reason = str(op.get("reason", "")).strip() \
                    or "dropped by repair (no reason given)"
                slots[idx] = None
                audits.append(EditAudit(
                    f"E#{idx}", old.subject if old else "?",
                    "dropped", reason, "adjudicator"))
            else:
                e = _payload(op, inherit_from=[old] if old else [])
                if e is None:
                    _log.warning(
                        "editpipe.repair: rejected replace E#%d bad payload",
                        idx)
                    continue
                slots[idx] = e
                audits.append(EditAudit(
                    f"E#{idx}", e.subject, "kept",
                    "replaced by repair", "adjudicator"))
        elif kind == "merge":
            raw_ids = op.get("ids", [])
            idxs = [_resolve_id(r)
                    for r in (raw_ids if isinstance(raw_ids, list) else [])]
            if not idxs or any(
                    i is None or not (0 <= i < n) for i in idxs):
                _log.warning("editpipe.repair: rejected merge ids %r", raw_ids)
                continue
            if any(i not in allowed for i in idxs):
                _log.warning(
                    "editpipe.repair: rejected merge %r outside allowlist",
                    raw_ids)
                continue
            e = _payload(op, inherit_from=[
                s for s in (slots[i] for i in idxs) if s is not None])
            if e is None:
                _log.warning("editpipe.repair: rejected merge bad payload")
                continue
            first = min(idxs)
            for i in idxs:
                merged_old = slots[i]
                if merged_old is not None and i != first:
                    audits.append(EditAudit(
                        f"E#{i}", merged_old.subject, "merged_into",
                        f"merged into E#{first} ({e.subject!r}) by repair",
                        "adjudicator"))
                slots[i] = None
            slots[first] = e
            audits.append(EditAudit(
                f"E#{first}", e.subject, "kept",
                f"merge target of {len(idxs)} edits", "adjudicator"))
        else:
            _log.warning("editpipe.repair: unknown op kind %r", op.get("op"))

    return [e for e in slots if e is not None] + appended


# ── Focused purity purge (last LLM engagement before delivery) ──────────────

_PURGE_SYSTEM = """\
You clean ONE rules.md edit body. The body must contain ONLY domain
instructions the task-executing agent can act on. Remove optimization-process
material: provenance/justification prose, references to training tasks or
task identifiers, verification outcomes, meta commentary. Keep every
actionable domain instruction intact and unchanged.

Output JSON only, no fences: {"body": "<the cleaned body>"}"""


def _purify_impure_edits(
    client: Any,
    edits: list[SectionEdit],
    blocking: list[Violation],
    audits: list[EditAudit],
) -> list[Violation]:
    """One focused purge call per content_purity edit; on success the
    violation is resolved, on failure it stays for the fallback's honest
    accepted-risk audit."""
    remaining: list[Violation] = []
    by_id = {f"E#{i}": e for i, e in enumerate(edits)}
    for v in blocking:
        if v.vtype != "content_purity":
            remaining.append(v)
            continue
        resolved_all = True
        for ref in v.edit_ids:
            e = by_id.get(ref)
            if e is None or not e.body.strip():
                continue
            try:
                text, _usage = client.complete_optimizer(
                    _PURGE_SYSTEM,
                    f"## Problem\n{v.detail}\n\n## Edit body\n{e.body}",
                    max_tokens=4096)
                obj = _parse_json_obj(text, "body")
            except Exception:
                obj = None
            new_body = (obj or {}).get("body")
            if isinstance(new_body, str) and new_body.strip():
                e.body = new_body.strip()
                audits.append(EditAudit(
                    ref, e.subject, "normalized",
                    "content_purity purge applied by a focused LLM call "
                    "before delivery", "adjudicator"))
            else:
                resolved_all = False
        if not resolved_all:
            remaining.append(v)
    return remaining


# ── Deterministic convergence fallback ───────────────────────────────────────

def deterministic_fallback(
    edits: list[SectionEdit],
    violations: list[Violation],
    audits: list[EditAudit],
) -> tuple[list[SectionEdit], list[Violation]]:
    """Minimal-intervention fallback when the LLM loop did not converge.

    Rule code makes NO content or arbitration decisions here. The only
    mechanical action is pure format hygiene (demoting heading lines inside
    bodies — the document invariant). Every semantically contested edit is
    delivered UNCHANGED to per-edit verification, which adjudicates with
    rollout measurements — the only referee more objective than the LLM.
    Apply-level content-preserving semantics (add-to-existing appends,
    removals are idempotent) guarantee delivery cannot lose content.
    """
    accepted: list[Violation] = []
    by_id = {f"E#{i}": e for i, e in enumerate(edits)}
    suppressed: set[int] = set()

    for v in violations:
        # GUARD (not arbitration): an UNRESOLVED conflict that includes a
        # whole-section removal must not delete content nobody ruled on.
        # The removal is suppressed (audited); if it is a real improvement
        # the reflector/merger will re-propose it next step with the
        # conflict gone. Irreversible actions require an explicit ruling.
        if v.vtype in ("identity_collision", "add_exists",
                       "section_conflict"):
            refs = [r for r in v.edit_ids if r in by_id]
            kinds = {by_id[r].kind for r in refs}
            if "remove_section" in kinds and len(kinds) > 1:
                for r in refs:
                    e = by_id[r]
                    if e.kind == "remove_section":
                        suppressed.add(id(e))
                        audits.append(EditAudit(
                            r, e.subject, "dropped",
                            f"unresolved {v.vtype} pairs this removal with "
                            "other live edits on the same section; removal "
                            "suppressed — deleting content requires an "
                            "explicit ruling, and a warranted removal will "
                            "be re-proposed next step", "fallback"))
        if v.vtype == "body_contains_heading":
            for ref in v.edit_ids:
                e = by_id.get(ref)
                if e is None:
                    continue
                new_body, n = demote_headings(e.body)
                if n:
                    e.body = new_body
                    audits.append(EditAudit(
                        ref, e.subject, "normalized",
                        f"fallback demoted {n} heading line(s) in body",
                        "fallback"))
        else:
            accepted.append(v)
            if v.vtype == "content_purity":
                note = ("delivered with UNRESOLVED purity risk — the "
                        "focused purge failed and rollout verification "
                        "measures solvability, not purity; review the "
                        "accepted_risks record")
            else:
                note = ("delivered unchanged for objective per-edit "
                        "verification to judge")
            audits.append(EditAudit(
                ",".join(v.edit_ids), "*", "kept",
                f"unresolved [{v.vtype}] after LLM adjudication: "
                f"{v.detail[:140]} — {note}", "fallback"))

    if suppressed:
        edits = [e for e in edits if id(e) not in suppressed]
    return edits, accepted


# ── Main loop ────────────────────────────────────────────────────────────────

def adjudicate(
    client: Any,
    rules_md: str,
    edits: list[SectionEdit],
    initial_violations: list[Violation] | None = None,
    *,
    max_rounds: int = MAX_ROUNDS,
    hard_cap_rounds: int = MAX_ROUNDS + 2,
    run_semantic: bool = True,
) -> AdjudicationResult:
    """Run the detect->repair loop to convergence or fallback.

    The LLM is the decision maker; rule code only detects, meters and
    executes. Two consequences: (1) apply-degradable violations are still
    SHOWN to the repair LLM (it may fix an anchor outright — better than
    any downstream degradation) but never block convergence; (2) the round
    budget is elastic — while each repair round strictly reduces the
    blocking-violation count, the loop earns extra rounds up to
    ``hard_cap_rounds`` instead of being cut off mid-progress.
    """
    doc = RulesDoc.parse(rules_md)
    audits: list[EditAudit] = []
    res = AdjudicationResult(edits=list(edits), audits=audits)
    carried = list(initial_violations or [])
    prev_blocking = None
    last_blocking: list[Violation] = []

    round_no = 0
    while True:
        round_no += 1
        res.rounds = round_no
        mech = detect_conflicts(res.edits, doc) \
            + detect_restatements(res.edits, doc)
        sem: list[Violation] = []
        if run_semantic:
            sem = semantic_check(client, rules_md, res.edits)
            res.llm_calls += 1
        violations = _dedup_violations(carried + mech + sem)
        carried = []  # gate leftovers only enter the first round

        blocking = [v for v in violations
                    if v.vtype not in _APPLY_DEGRADABLE]
        degradable = [v for v in violations
                      if v.vtype in _APPLY_DEGRADABLE]

        if not blocking:
            res.converged = True
            for v in degradable:
                audits.append(EditAudit(
                    ",".join(v.edit_ids), "*", "kept",
                    f"[{v.vtype}] unresolved but non-blocking; the apply "
                    f"stage degrades it content-preservingly: {v.detail[:120]}",
                    "adjudicator"))
            _log.info(
                "editpipe.adjudicate: converged in round %d (%d degradable "
                "violation(s) left to apply)", round_no, len(degradable))
            return res

        _log.info(
            "editpipe.adjudicate: round %d — %d blocking violation(s): %s",
            round_no, len(blocking),
            ", ".join(sorted({v.vtype for v in blocking})))

        making_progress = (
            prev_blocking is not None and len(blocking) < prev_blocking)
        prev_blocking = len(blocking)
        if round_no >= hard_cap_rounds or (
                round_no >= max_rounds and not making_progress):
            last_blocking = blocking
            break

        # The repair LLM sees EVERYTHING (blocking + degradable) and may
        # operate on any implicated edit; only blocking gates convergence.
        allowed = _implicated(blocking) | _implicated(degradable)
        feedback = _feedback_text(blocking)
        if degradable:
            feedback += (
                "\n\nADDITIONALLY (non-blocking — fix if you can, e.g. by "
                "supplying a correct anchor; otherwise the apply stage will "
                "degrade them safely):\n" + _feedback_text(degradable))
        user = (
            "## Current rules.md\n"
            + (rules_md.strip() if rules_md and rules_md.strip() else "(empty)")
            + "\n\n## CURRENT EDIT SET (protocol handles E#n)\n"
            + "\n\n".join(_render_edit(i, e) for i, e in enumerate(res.edits))
            + "\n\n## VIOLATIONS TO FIX\n" + feedback
            + "\n\n## ALLOWED IDS (operations may only reference these)\n"
            + (", ".join(f"E#{i}" for i in sorted(allowed)) or "(none — add only)")
        )
        try:
            ops = complete_optimizer_json(
                client, _REPAIR_SYSTEM, user,
                parse=_parse_ops, max_tokens=16384, stage="editpipe_repair")
            res.llm_calls += 1
        except Exception:
            _log.warning("editpipe.repair: LLM call failed", exc_info=True)
            ops = None
        if not ops:
            _log.warning(
                "editpipe.repair: no operations produced (round %d)", round_no)
            continue

        repaired = _apply_ops(res.edits, ops, allowed, audits)
        # Re-gate tolerantly: lossless normalization + fresh violation set.
        gated, gate_viols, gate_audits = syntax_gate(repaired, doc)
        audits += gate_audits
        carried = gate_viols
        res.edits = gated
        _log.info(
            "editpipe.repair: %d ops -> %d edits after round %d",
            len(ops), len(res.edits), round_no)

    # Re-detect mechanically after the last repair, and CARRY the final
    # round's semantic-only blocking findings (they are not recomputable
    # without another validator call and must not vanish unaudited).
    mech = detect_conflicts(res.edits, doc) \
        + detect_restatements(res.edits, doc)
    semantic_carry = [v for v in last_blocking
                      if v.vtype not in _MECH_TYPES]
    blocking = [v for v in _dedup_violations(mech + semantic_carry)
                if v.vtype not in _APPLY_DEGRADABLE]
    if not blocking:
        res.converged = True
        _log.info(
            "editpipe.adjudicate: converged on the final repair (round %d)",
            res.rounds)
        return res

    # Last LLM engagement before delivery: content_purity has no objective
    # backstop downstream (verification measures solvability, not purity;
    # the GT firewall is non-negotiable), so give the LLM one focused
    # purge call per impure edit before accepting any residual risk.
    blocking = _purify_impure_edits(client, res.edits, blocking, audits)

    # Non-convergence: minimal, content-preserving fallback.
    res.edits, accepted = deterministic_fallback(res.edits, blocking, audits)
    res.accepted_risks = accepted
    res.converged = False
    _log.warning(
        "editpipe.adjudicate: fallback after %d rounds — %d violation(s) "
        "resolved deterministically, %d accepted as risks",
        res.rounds, len(blocking) - len(accepted), len(accepted))
    return res


def _dedup_violations(violations: list[Violation]) -> list[Violation]:
    seen: set[tuple] = set()
    out: list[Violation] = []
    for v in violations:
        key = (v.vtype, tuple(sorted(v.edit_ids)))
        if key in seen:
            continue
        seen.add(key)
        out.append(v)
    return out
