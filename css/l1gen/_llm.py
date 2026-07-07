"""Single optimizer-LLM seam for the generation pipelines.

Every optimizer JSON call in :mod:`css.l1gen` goes through this module's
``complete_optimizer_json`` (re-exported from :mod:`css.model.json_repair`, the
canonical context-aware repair helper). Pipeline modules reference it as
``_llm.complete_optimizer_json(...)`` so a test can monkeypatch this one attribute
and drive every stage from a scripted fake, dispatching on the ``stage`` argument.

The JSON extraction helpers mirror the best-first candidate scan used across
``css.proposal`` (fenced block -> whole text -> first ``{...}`` / ``[...]``), kept
here so the pipelines do not import the retiring ``css.proposal`` package.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, List

from css.model.json_repair import complete_optimizer_json  # noqa: F401 (re-exported seam)

_log = logging.getLogger("css.l1gen")

# ── Stage identifiers (also the monkeypatch dispatch keys in tests) ───────────
STAGE_NEW_TARGET = "l1gen.new.target_selection"
STAGE_NEW_CONCEPT = "l1gen.new.conception"
STAGE_NEW_NOVELTY = "l1gen.new.novelty"
STAGE_NEW_DRAFT = "l1gen.new.drafting"
STAGE_NEW_ALTITUDE = "l1gen.new.altitude"

STAGE_REFINE_CAUSE = "l1gen.refine.cause"
STAGE_REFINE_PLAN = "l1gen.refine.edit_plan"
STAGE_REFINE_CONFRONT = "l1gen.refine.confrontation"
STAGE_REFINE_COHERENCE = "l1gen.refine.coherence"
STAGE_REFINE_RATIONALE = "l1gen.refine.rationale"
STAGE_REFINE_ALTITUDE = "l1gen.refine.altitude"
STAGE_REFINE_INHERIT = "l1gen.refine.inherit"

STAGE_MERGE_CONCEPT = "l1gen.merge.conception"
STAGE_MERGE_NOVELTY = "l1gen.merge.novelty"
STAGE_MERGE_DRAFT = "l1gen.merge.drafting"
STAGE_MERGE_ALTITUDE = "l1gen.merge.altitude"
STAGE_MERGE_RULES_SELECT = "l1gen.merge.rules_select"
STAGE_MERGE_RULES_CONSOLIDATE = "l1gen.merge.rules_consolidate"

STAGE_SCREEN = "l1gen.altitude_screen"


def _json_candidates(text: str) -> "List[str]":
    """Best-first JSON substrings from noisy LLM output (mirrors css.proposal)."""
    if not text:
        return []
    out: "List[str]" = []
    for m in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.DOTALL):
        out.append(m.group(1).strip())
    out.append(text.strip())
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        out.append(m.group(0))
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        out.append(m.group(0))
    return out


def parse_json_object(text: str) -> dict:
    """First parseable JSON object from noisy output; ``{}`` if none (never raises)."""
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
    return {}


def parse_json_array(text: str) -> list:
    """First parseable JSON array from noisy output; ``[]`` if none (never raises).

    A lone object is wrapped into a single-element list so callers that expect a
    list of decisions still get one.
    """
    for cand in _json_candidates(text):
        if not cand:
            continue
        try:
            obj = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, list):
            return obj
        if isinstance(obj, dict):
            return [obj]
    return []
