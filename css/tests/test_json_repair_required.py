"""Regression tests for schema (missing-required-field) repair.

Reproduces the L0 merger "empty merge" bug (run spreadsheetbench_20260629_220033,
node n0002 / round_0002 / exploit/step2): the merger LLM returned well-formed
section edits but OMITTED the required ``target_tasks`` field on every edit, so
``_validate_merged_edits`` dropped them all -> ``merged_edits.json == []`` ->
``reject_no_survivor``.

With the ``required=`` schema-repair hook wired into the merger, the omission
triggers a feedback-driven LLM repair that restores ``target_tasks``, and the
edits survive validation.

Deterministic and stub-based (no network, no LLM API).

Run:
    python -m pytest css/tests/test_json_repair_required.py -v
"""
from __future__ import annotations

import json

from css.config import CSSConfig
from css.data.edit import Edit, Patch, RawPatch
from css.data.step_buffer import StepBuffer
from css.model.client import StubLLMClient
from css.model.json_repair import complete_optimizer_json
from css.optimizer.aggregate import (
    _merger_required_missing,
    _parse_merger_output,
    merger,
)

# A merger response shaped exactly like the real step2 failure: valid JSON, valid
# delta_type / section_target / content, but NO target_tasks on any edit.
_INCOMPLETE = json.dumps(
    {
        "reasoning": "consolidated raw edits",
        "edits": [
            {
                "section_target": "### Computed Values over Formulas",
                "delta_type": "section_rewrite",
                "content": "### Computed Values over Formulas\nCompute the literal value in Python; never write a formula string.",
            },
            {
                "section_target": "### Sheet and Range Fidelity",
                "delta_type": "section_rewrite",
                "content": "### Sheet and Range Fidelity\nWrite kept rows contiguously into the exact answer range.",
            },
        ],
    }
)

# The repaired response: identical edits, now WITH non-empty target_tasks.
_REPAIRED = json.dumps(
    {
        "reasoning": "consolidated raw edits",
        "edits": [
            {
                "section_target": "### Computed Values over Formulas",
                "delta_type": "section_rewrite",
                "content": "### Computed Values over Formulas\nCompute the literal value in Python; never write a formula string.",
                "target_tasks": ["101", "102"],
            },
            {
                "section_target": "### Sheet and Range Fidelity",
                "delta_type": "section_rewrite",
                "content": "### Sheet and Range Fidelity\nWrite kept rows contiguously into the exact answer range.",
                "target_tasks": ["103"],
            },
        ],
    }
)


def _stub(repaired: str = _REPAIRED) -> StubLLMClient:
    """First optimizer call -> incomplete; the repair call (detected by the repair
    specialist system prompt) -> ``repaired``."""

    def optimizer_fn(system: str, user: str) -> str:
        if "JSON repair specialist" in system:
            return repaired
        return _INCOMPLETE

    return StubLLMClient(optimizer_fn=optimizer_fn)


# ── the missing-required-field detector ──────────────────────────────────────

def test_detector_flags_missing_target_tasks():
    missing = _merger_required_missing(_parse_merger_output(_INCOMPLETE))
    assert len(missing) == 2
    assert all("target_tasks" in m for m in missing)


def test_detector_passes_complete_response():
    assert _merger_required_missing(_parse_merger_output(_REPAIRED)) == []


def test_detector_ignores_absent_optional_fields():
    # after_section / rationale / derivation are absent in _REPAIRED but must
    # NOT be reported as missing (they are conditional / never validated).
    assert _merger_required_missing(_parse_merger_output(_REPAIRED)) == []


# ── complete_optimizer_json schema repair ────────────────────────────────────

def test_schema_repair_recovers_missing_field():
    result = complete_optimizer_json(
        _stub(), "MERGER", "USER",
        parse=_parse_merger_output, required=_merger_required_missing, stage="merger",
    )
    assert result is not None and len(result) == 2
    assert all(e.get("target_tasks") for e in result)
    assert _merger_required_missing(result) == []


def test_without_required_no_repair():
    # No `required` -> the parseable-but-incomplete response is returned unchanged.
    result = complete_optimizer_json(
        _stub(), "MERGER", "USER", parse=_parse_merger_output, stage="merger",
    )
    assert len(result) == 2
    assert all("target_tasks" not in e for e in result)


def test_repair_still_missing_falls_back():
    # If repair ALSO omits the field, fall back to the original (no crash, no loop).
    result = complete_optimizer_json(
        _stub(repaired=_INCOMPLETE), "MERGER", "USER",
        parse=_parse_merger_output, required=_merger_required_missing, stage="merger",
    )
    assert len(result) == 2
    assert all("target_tasks" not in e for e in result)


def test_complete_response_skips_repair():
    calls: list[str] = []

    def optimizer_fn(system, user):
        calls.append("repair" if "JSON repair specialist" in system else "main")
        return _REPAIRED

    result = complete_optimizer_json(
        StubLLMClient(optimizer_fn=optimizer_fn), "MERGER", "USER",
        parse=_parse_merger_output, required=_merger_required_missing, stage="merger",
    )
    assert calls == ["main"]  # no repair call when nothing is missing
    assert all(e.get("target_tasks") for e in result)


# ── end-to-end through merger() (verifies the wiring + validation survival) ───

def test_merger_end_to_end_recovers_and_survives_validation():
    raw_patches = [
        RawPatch(
            patch=Patch(edits=[Edit(op="append", content="x", source_tasks=["101"])]),
            source_type="success",
            batch_size=1,
        ),
    ]
    merged = merger(_stub(), "", raw_patches, StepBuffer(), CSSConfig())
    # Without the fix this would be [] (all dropped for missing target_tasks).
    assert len(merged) == 2
    assert all(m.target_tasks for m in merged)
