"""editpipe orchestration: merger -> adjudication -> apply, fully audited.

``consolidate`` produces the verified-ready edit set (what per-edit
verification consumes, one edit at a time); ``build_candidate`` applies a
set of edits to produce a candidate document. ``run_pipeline`` chains both
for replay/testing.

The LLM appears in exactly three roles, all judgement roles:
  merger (consolidation), validator+repair (adjudication), and the
  section-scoped anchor resolver at apply time. Document assembly itself is
  always deterministic.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

from css.data.edit import RawPatch
from css.optimizer.editpipe.adjudicate import AdjudicationResult, adjudicate
from css.optimizer.editpipe.apply import ApplyResult, Resolver, apply_edits
from css.optimizer.editpipe.merger import run_merger
from css.optimizer.editpipe.schema import (
    EditAudit,
    SectionEdit,
    Violation,
    syntax_gate,
)

_log = logging.getLogger(__name__)


@dataclass
class ConsolidationResult:
    edits: list[SectionEdit]
    audits: list[EditAudit] = field(default_factory=list)
    accepted_risks: list[Violation] = field(default_factory=list)
    converged: bool = True
    stats: dict = field(default_factory=dict)

    def audit_dicts(self) -> list[dict]:
        return [a.to_dict() for a in self.audits]


def consolidate(
    client: Any,
    rules_md: str,
    raw_patches: list[RawPatch],
    history_text: str = "",
    *,
    run_semantic: bool = True,
) -> ConsolidationResult:
    """merger + adjudication. Returns the final edit set plus full audit."""
    edits, violations, audits, mstats = run_merger(
        client, rules_md, raw_patches, history_text)
    if not edits:
        return ConsolidationResult([], audits, [], True, {"merger": mstats})

    adj = adjudicate(
        client, rules_md, edits, violations, run_semantic=run_semantic)
    stats = {
        "merger": mstats,
        "adjudication_rounds": adj.rounds,
        "adjudication_llm_calls": adj.llm_calls,
        "converged": adj.converged,
    }
    return ConsolidationResult(
        adj.edits, audits + adj.audits, adj.accepted_risks,
        adj.converged, stats)


# ── Section-scoped anchor resolver (the only LLM at apply time) ─────────────

_RESOLVER_SYSTEM = """\
You are a precise text editor. You receive ONE section of a rules document
and ONE edit whose anchor text could not be located verbatim. Apply the
edit's INTENT to the section body.

- add_point: insert the edit body at the most semantically appropriate spot
  (after the text the anchor most plausibly refers to; at the end if unclear).
- edit_point: find the text the anchor most plausibly refers to and replace
  it with the edit body.
- remove_point: find the text the anchor most plausibly refers to and delete
  it. If nothing plausibly matches, return the body unchanged.

Rules: preserve every other part of the body EXACTLY; never add markdown
heading lines; output JSON only, no fences, no prose.

Output: {"body": "<the complete updated section body>"}"""


def make_section_resolver(client: Any) -> Resolver:
    """Build the LLM-backed anchor resolver; blast radius = one section."""

    def resolver(subject: str, body: str, edit: SectionEdit,
                 feedback: str = "") -> str | None:
        user = (
            f"## Section: {subject}\n{body}\n\n"
            f"## Edit\nkind: {edit.kind}\nanchor (not found verbatim): "
            f"{edit.anchor!r}\nbody:\n{edit.body}"
        )
        if feedback:
            user += f"\n\n## Feedback on your previous attempt\n{feedback}"
        try:
            text, _usage = client.complete_optimizer(
                _RESOLVER_SYSTEM, user, max_tokens=8192)
        except Exception:
            _log.warning("editpipe.resolver: LLM call failed", exc_info=True)
            return None
        m = re.search(r"\{.*\}", text or "", re.DOTALL)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
        except (json.JSONDecodeError, ValueError):
            return None
        new_body = obj.get("body")
        if not isinstance(new_body, str) or not new_body.strip():
            return None
        return new_body

    return resolver


# ── Candidate assembly ───────────────────────────────────────────────────────

def build_candidate(
    rules_md: str,
    edits: list[SectionEdit],
    resolver: Resolver | None = None,
) -> ApplyResult:
    """Deterministic apply of an edit set onto rules_md."""
    return apply_edits(rules_md, edits, resolver=resolver)


@dataclass
class PipelineResult:
    consolidation: ConsolidationResult
    apply_result: ApplyResult

    @property
    def candidate_text(self) -> str:
        return self.apply_result.text


# ── Drop-in facades for the exploitation loop (MergedEdit wire format) ──────

def consolidate_to_merged(
    client: Any,
    rules: str,
    raw_patches: list[RawPatch],
    step_buffer: Any,
    cfg: Any,
    *,
    audit_path: str | None = None,
    telemetry_task_ids: "set[str] | None" = None,
) -> list:
    """v2 replacement for the legacy merger+validation stage.

    Returns MergedEdit objects (the downstream wire format). The full audit
    trail (every drop with actor+reason, accepted risks, convergence) is
    persisted to ``audit_path`` when given — the step directory's permanent
    record of what happened to every edit.
    """
    history_text = ""
    if getattr(cfg, "merger_inject_history", True):
        from css.optimizer.aggregate import (
            _check_has_history,
            _format_merger_history,
        )
        if _check_has_history(step_buffer):
            window = getattr(cfg, "merger_history_window", 3)
            history_text = _format_merger_history(step_buffer, window)

    cons = consolidate(client, rules, raw_patches, history_text)
    if audit_path:
        try:
            payload = {
                "audits": cons.audit_dicts(),
                "accepted_risks": [v.to_dict() for v in cons.accepted_risks],
                "converged": cons.converged,
                "stats": cons.stats,
            }
            os.makedirs(os.path.dirname(audit_path), exist_ok=True)
            tmp = audit_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=1)
            os.replace(tmp, audit_path)
        except Exception:
            _log.warning("editpipe: failed to persist audit trail",
                         exc_info=True)

    merged = [e.to_merged() for e in cons.edits]
    try:
        # Log-only purity telemetry, kept identical to the legacy path so the
        # monitoring signal stays continuous across the pipeline switch.
        from css.optimizer.edit_validator import purity_telemetry
        purity_telemetry(merged, telemetry_task_ids)
    except Exception:
        _log.debug("editpipe: purity telemetry failed", exc_info=True)
    return merged


def apply_merged(
    client: Any,
    rules: str,
    merged_edits: list,
    *,
    audit_path: str | None = None,
) -> str:
    """v2 replacement for llm_apply_edit / llm_apply_edits.

    Deterministic section surgery + section-scoped LLM anchor resolution.
    Accepts MergedEdit objects (or dicts) in either vocabulary; the syntax
    gate re-normalizes losslessly on the way in. When ``audit_path`` is
    given, the FULL apply record (gate audits, apply audits, normalize
    notes, assertion failures) is persisted — every degradation the apply
    stage performs is part of the step's permanent record.
    """
    from css.optimizer.editpipe.render import RulesDoc

    edits = []
    for m in merged_edits:
        d = m.to_dict() if hasattr(m, "to_dict") else dict(m)
        edits.append(SectionEdit.from_dict(d))
    kept, _violations, gate_audits = syntax_gate(edits, RulesDoc.parse(rules))
    result = apply_edits(rules, kept, resolver=make_section_resolver(client))
    if result.assertion_failures:
        # Structure assertions failed even after normalization — log loudly;
        # the text is still the best deterministic effort (never a raw LLM
        # rewrite), so return it rather than block the step.
        _log.warning("editpipe.apply_merged: assertion failures: %s",
                     result.assertion_failures)
    if audit_path:
        try:
            payload = {
                "gate_audits": [a.to_dict() for a in gate_audits],
                "apply_audits": [a.to_dict() for a in result.audits],
                "normalize_notes": result.normalize_notes,
                "assertion_failures": result.assertion_failures,
            }
            os.makedirs(os.path.dirname(audit_path), exist_ok=True)
            tmp = audit_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=1)
            os.replace(tmp, audit_path)
        except Exception:
            _log.warning("editpipe: failed to persist apply audit",
                         exc_info=True)
    return result.text


def run_pipeline(
    client: Any,
    rules_md: str,
    raw_patches: list[RawPatch],
    history_text: str = "",
    *,
    run_semantic: bool = True,
    use_llm_resolver: bool = True,
) -> PipelineResult:
    cons = consolidate(
        client, rules_md, raw_patches, history_text,
        run_semantic=run_semantic)
    resolver = make_section_resolver(client) if use_llm_resolver else None
    applied = build_candidate(rules_md, cons.edits, resolver=resolver)
    return PipelineResult(cons, applied)
