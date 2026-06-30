"""Phase 5 Layer 4 — root-cause attribution for L1 signals (design §4.3 / D4).

This is the FIRST half of a PROPOSAL/REFINE: before any strategy change is
considered, the optimizer must explain *why* a persistent failure pattern exists.
The design's central claim (D4) is that a good strategy change is a LOGICAL
CONSEQUENCE of a correctly diagnosed root cause — never free LLM "creativity".
So this module produces, for each L1 signal (or group of signals), a four-level
progressive attribution:

  1. behavioral  — what *exactly* the agent did, observably, in the trajectories.
  2. process     — *why* it did that, read from the agent's own THOUGHT text.
  3. strategy    — which part of the CURRENT strategy design caused/permitted it
                   (must point at a specific strategy paragraph/subsection).
  4. assumption  — the hidden assumption baked into that strategy text, and what
                   breaking it would unlock.

Each level is only admissible if it is backed by *cited evidence* (trajectory or
THOUGHT quotes for behavioral/process; the specific strategy text for strategy;
the assumption statement for assumption). A level with no evidence is not a
diagnosis, it is a guess — so the CODE GATE here drops or flags any
:class:`RootCause` missing content or evidence for any level.

Cross-pattern leverage (D4): when several distinct L1 patterns trace back to the
SAME strategy-level cause, that shared cause is a single high-leverage point — one
strategy change suppresses several failure patterns at once. We ask the LLM to
group, then order the resulting causes high-leverage-first (more covered patterns,
then more remedy-resistance) so the orchestrator attacks the biggest lever first.

``remedy_history`` (the L0 rules already tried against these patterns) is fed in
and the LLM is required to explain, at the strategy level, why those L0 rules
could NOT durably fix the pattern — i.e. why this genuinely needs an L1
(thinking-level) change rather than another rule.

Robustness contract (mirrors css/analysis/layer1.py): malformed LLM output never
crashes — :func:`attribute_root_cause` returns ``[]``.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

_log = logging.getLogger(__name__)

if TYPE_CHECKING:  # pragma: no cover - type-only imports
    from css.config import CSSConfig
    from css.data.pattern import PatternLibrary, PatternRecord
    from css.model.client import LLMClient


# ── Data model ───────────────────────────────────────────────────────────────

@dataclass
class RootCause:
    """A four-level progressive attribution for one (group of) L1 signal(s).

    ``pattern_ids`` lists every L1 pattern this cause explains; when more than one
    pattern shares a strategy-level cause, they are merged into one high-leverage
    :class:`RootCause`. ``leverage`` records why the cause is high-leverage (e.g.
    "explains 3 patterns via the same premature-commitment strategy gap").
    ``l0_explanation`` records why the L0 rules in ``remedy_history`` could not fix
    it (the justification for going L1). ``evidence`` maps each level name to its
    cited supporting text; the CODE GATE also writes a ``"_flag"`` key here when a
    level is incomplete.
    """

    pattern_ids: list[str]
    behavioral: str
    process: str
    strategy: str
    assumption: str
    leverage: str = ""
    l0_explanation: str = ""
    evidence: dict = field(default_factory=dict)

    # The four inquiry levels, in order, that the CODE GATE checks for content.
    LEVELS = ("behavioral", "process", "strategy", "assumption")

    @property
    def is_complete(self) -> bool:
        """True iff every level has non-empty content AND cited evidence."""
        return not self.missing_levels()

    def missing_levels(self) -> list[str]:
        """Names of levels lacking content or evidence (drives the CODE GATE)."""
        missing: list[str] = []
        for level in self.LEVELS:
            content = (getattr(self, level, "") or "").strip()
            ev = str(self.evidence.get(level, "") or "").strip()
            if not content or not ev:
                missing.append(level)
        return missing

    @classmethod
    def from_dict(cls, d: dict) -> "RootCause":
        pids = d.get("pattern_ids", [])
        if isinstance(pids, str):
            pids = [pids]
        elif not isinstance(pids, list):
            pids = []
        ev = d.get("evidence", {})
        if not isinstance(ev, dict):
            ev = {}
        return cls(
            pattern_ids=[str(p) for p in pids],
            behavioral=str(d.get("behavioral", "")),
            process=str(d.get("process", "")),
            strategy=str(d.get("strategy", "")),
            assumption=str(d.get("assumption", "")),
            leverage=str(d.get("leverage", "")),
            l0_explanation=str(d.get("l0_explanation", "")),
            evidence={str(k): v for k, v in ev.items()},
        )

    def to_dict(self) -> dict:
        return {
            "pattern_ids": list(self.pattern_ids),
            "behavioral": self.behavioral,
            "process": self.process,
            "strategy": self.strategy,
            "assumption": self.assumption,
            "leverage": self.leverage,
            "l0_explanation": self.l0_explanation,
            "evidence": dict(self.evidence),
        }


# ── Prompt: four-level progressive root-cause inquiry ────────────────────────

_ROOT_CAUSE_SYSTEM = """\
You are a root-cause analyst for an AI agent's COGNITIVE STRATEGY. The agent \
follows a written strategy document, and despite repeated low-level rule fixes \
(call them L0 remedies), certain FAILURE patterns in how it *thinks* keep \
recurring. You are given those persistent failure patterns, their paired \
SUCCESS counterparts (cases where the agent thought differently and succeeded), \
the relevant parts of the current strategy document, and the history of L0 \
remedies already tried. Your job is to diagnose the TRUE root cause of each \
persistent pattern.

This is diagnosis, NOT brainstorming. You are forbidden from proposing fixes \
here. You produce, for each root cause, a FOUR-LEVEL PROGRESSIVE INQUIRY that \
drills from the surface behavior down to the hidden assumption in the strategy. \
Each level must be a strictly deeper "why" than the one above it:

  1. behavioral — what the agent OBSERVABLY did, across the trajectories. Concrete \
     actions/decisions, not interpretation. (e.g. "committed to its first \
     interpretation of the input and never re-checked it against the available \
     evidence.")
  2. process — WHY it did that, read from the agent's OWN reasoning (its THOUGHT \
     text). The internal logic that produced the behavior. (e.g. "it treated the \
     first plausible reading as settled and moved on to execution.")
  3. strategy — which specific part of the CURRENT strategy document caused or \
     permitted this process. You MUST point at concrete strategy text (quote or \
     name the paragraph/subsection). (e.g. "the 'Plan then execute' section tells \
     it to lock a plan early and says nothing about revisiting interpretations.")
  4. assumption — the hidden ASSUMPTION baked into that strategy text, and what \
     breaking it would unlock. The deepest level: the belief the strategy takes \
     for granted that the failures prove false. (e.g. "it assumes the first \
     reading of an ambiguous input is usually right; breaking that — forcing a \
     cheap re-read before committing — would remove the whole class of failures.")

EVIDENCE IS MANDATORY AT EVERY LEVEL. A level with no citable evidence is a \
guess and will be discarded. Cite:
  - behavioral & process: relevant quotes or close paraphrases from the \
    trajectories / the agent's THOUGHT text — include enough context to show the \
    pattern clearly.
  - strategy: the specific strategy paragraph or subsection text you are blaming. \
    If the strategy document is empty or minimal, explain what ABSENCE of guidance \
    permitted the failure (the strategy's gap is itself a cause).
  - assumption: the statement of the assumption (grounded in the strategy text \
    above, or in the strategy's implicit stance through omission).
Put these citations in the "evidence" object keyed by level name.

L0 EXPLANATION (mandatory): in "l0_explanation", explain — referencing the L0 \
remedies already tried — WHY low-level rules could not durably fix this. A real \
L1 (thinking-level) root cause is one that no surface rule can patch because the \
problem is in how the agent reasons, not in a missing instruction.

CROSS-PATTERN LEVERAGE (important): if SEVERAL of the given patterns trace back \
to the SAME strategy-level cause (level 3), MERGE them into ONE root cause whose \
"pattern_ids" lists all of them, and say so in "leverage" (a high-leverage cause \
fixes multiple patterns with one change). Only merge when the strategy-level \
cause is genuinely the same — do not force unrelated patterns together.

Output ONLY a JSON list, each element:
  {
    "pattern_ids": ["<id>", ...],
    "behavioral": "<level 1 — describe the concrete, observable agent behaviors \
across the trajectories in detail>",
    "process": "<level 2 — trace the agent's internal reasoning that produced \
this behavior, referencing its actual thought process>",
    "strategy": "<level 3 — identify the specific strategy text (or absence of \
guidance) that caused or permitted this reasoning process>",
    "assumption": "<level 4 — articulate the hidden assumption and what breaking \
it would unlock>",
    "leverage": "<why high-leverage; '' if it explains a single pattern>",
    "l0_explanation": "<why the L0 remedies could not fix this — reference the \
specific remedies tried>",
    "evidence": {
      "behavioral": "<trajectory quotes showing the behavior>",
      "process": "<quotes from the agent's reasoning>",
      "strategy": "<the strategy text being blamed, or description of the gap>",
      "assumption": "<the assumption statement>"
    }
  }
No prose, no markdown fences, no commentary — just the JSON list."""

_ROOT_CAUSE_USER_TMPL = """\
CURRENT STRATEGY DOCUMENT (the text you may blame at level 3):
-------------------------------------------------------------
{strategy}
-------------------------------------------------------------

L0 REMEDIES ALREADY TRIED (these failed to durably suppress the patterns):
{remedy_history}

PERSISTENT FAILURE PATTERNS (the L1 signals to diagnose):
{patterns}

PAIRED SUCCESS COUNTERPARTS (the agent thinking differently, and succeeding):
{counterparts}

Diagnose the TRUE root cause of each persistent pattern using the four-level \
progressive inquiry. Cite evidence at EVERY level. Merge patterns that share a \
strategy-level cause into one high-leverage root cause. Respond with ONLY the \
JSON list described in the instructions."""


# ── Rendering helpers ────────────────────────────────────────────────────────

def _fmt_observations(pattern: "PatternRecord", *, max_obs: int = 4) -> str:
    """Render a pattern's most-significant observations (with cited evidence)."""
    obs = sorted(
        pattern.observations,
        key=lambda o: 0 if o.significance == "critical" else 1,
    )[:max_obs]
    lines: list[str] = []
    for o in obs:
        ev = (o.evidence or "").strip()
        what = (o.what or "").strip()
        cons = (o.consequence or "").strip()
        line = f"      - {what}"
        if ev:
            line += f"\n        evidence: \"{ev}\""
        if cons:
            line += f"\n        consequence: {cons}"
        lines.append(line)
    return "\n".join(lines) if lines else "      (no observations recorded)"


def _fmt_pattern(pattern: "PatternRecord") -> str:
    """Render one failure pattern with its longitudinal / resistance evidence."""
    occ = pattern.latest_occurrence
    return (
        f"  [{pattern.pattern_id}] {pattern.name}\n"
        f"    cognitive_aspect: {pattern.cognitive_aspect}\n"
        f"    description: {pattern.description}\n"
        f"    latest_occurrence_rate: {occ:.2f}; "
        f"remedy_resistance: {pattern.remedy_resistance}\n"
        f"    observed behaviors:\n{_fmt_observations(pattern)}"
    )


def _fmt_counterpart(failure: "PatternRecord", success: "PatternRecord | None") -> str:
    """Render the success counterpart paired to a failure pattern."""
    if success is None:
        return f"  [{failure.pattern_id}] -> (no paired success counterpart)"
    return (
        f"  [{failure.pattern_id}] -> success counterpart "
        f"[{success.pattern_id}] {success.name}\n"
        f"    cognitive_aspect: {success.cognitive_aspect}\n"
        f"    description: {success.description}\n"
        f"    success behaviors:\n{_fmt_observations(success)}"
    )


# ── Robust JSON extraction (mirrors css/analysis/layer1.py conventions) ──────

def _json_candidates(text: str) -> list[str]:
    """Yield candidate JSON substrings from noisy LLM output (best-first)."""
    if not text:
        return []
    candidates: list[str] = []
    for m in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.DOTALL):
        candidates.append(m.group(1).strip())
    candidates.append(text.strip())
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        candidates.append(m.group(0))
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        candidates.append(m.group(0))
    return candidates


def _coerce_rc_list(obj: Any) -> list[dict] | None:
    """Coerce a parsed JSON value into a list of root-cause dicts, or None."""
    if isinstance(obj, list):
        return [o for o in obj if isinstance(o, dict)]
    if isinstance(obj, dict):
        for key in ("root_causes", "rootCauses", "causes", "items", "list"):
            inner = obj.get(key)
            if isinstance(inner, list):
                return [o for o in inner if isinstance(o, dict)]
        # A single root cause emitted bare.
        if any(k in obj for k in ("behavioral", "strategy", "assumption", "pattern_ids")):
            return [obj]
    return None


def _parse_rc_list(text: str) -> list[dict]:
    """Extract a JSON list of root-cause dicts from noisy LLM output.

    Returns ``[]`` when nothing parseable is found rather than raising.
    """
    for cand in _json_candidates(text):
        if not cand:
            continue
        try:
            obj = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        coerced = _coerce_rc_list(obj)
        if coerced is not None:
            return coerced
    return []


def _leverage_sort_key(rc: RootCause, library: "PatternLibrary") -> tuple:
    """Order high-leverage first: more patterns covered, then more resistance.

    Secondary key sums the covered patterns' remedy_resistance (a more resistant
    set is a more valuable lever); ties keep input order via the stable sort.
    """
    n_patterns = len(rc.pattern_ids)
    resistance = 0
    for pid in rc.pattern_ids:
        rec = library.get(pid)
        if rec is not None:
            resistance += rec.remedy_resistance
    return (-n_patterns, -resistance)


# ── Public API ───────────────────────────────────────────────────────────────

def attribute_root_cause(
    client: "LLMClient",
    l1_signals: list["PatternRecord"],
    library: "PatternLibrary",
    *,
    remedy_history: list[str],
    cfg: "CSSConfig",
    current_strategy: str = "",
) -> list["RootCause"]:
    """Layer 4 — diagnose the root cause(s) of the given L1 signals (design D4).

    Builds a substantive four-level progressive-inquiry prompt over the L1-signal
    failure patterns and their paired SUCCESS counterparts (resolved via
    ``PatternRecord.counterpart_id`` against ``library``), feeds in
    ``remedy_history`` (and requires the LLM to explain why those L0 rules could
    not fix the patterns), parses the response into :class:`RootCause` objects,
    then applies the CODE GATE and high-leverage ordering.

    ``current_strategy`` (the optimizing node's strategy text) is passed in
    EXPLICITLY for the level-3 ("which strategy text caused it") grounding — a
    per-call argument, NOT shared ``cfg`` state, so concurrent Phase-6 nodes
    cannot clobber each other.

    CODE GATE: any parsed :class:`RootCause` whose behavioral/process/strategy/
    assumption content OR whose per-level evidence is empty is *flagged* (its
    ``evidence['_flag']`` records the missing levels) and DROPPED from the result.
    A diagnosis without evidence at every level is not actionable downstream.

    Cross-pattern: the LLM is asked to merge patterns sharing a strategy-level
    cause; the returned list is then ordered high-leverage first (most patterns
    covered, then most remedy-resistance). Returns ``[]`` on malformed output or
    when no L1 signals are given — never raises.
    """
    if not l1_signals:
        return []

    strategy_text = current_strategy
    patterns_block = "\n\n".join(_fmt_pattern(p) for p in l1_signals)
    counterparts_block = "\n\n".join(
        _fmt_counterpart(
            p,
            library.get(p.counterpart_id) if p.counterpart_id else None,
        )
        for p in l1_signals
    )
    if remedy_history:
        remedy_block = "\n".join(f"  - {r}" for r in remedy_history)
    else:
        remedy_block = "  (none recorded; L0 saturation alone is the resistance evidence)"

    user = _ROOT_CAUSE_USER_TMPL.format(
        strategy=strategy_text or "(strategy text unavailable)",
        remedy_history=remedy_block,
        patterns=patterns_block,
        counterparts=counterparts_block,
    )

    try:
        from css.tracing import stage_context
        with stage_context(client, "root_cause_attribution"):
            text, _usage = client.complete_optimizer(_ROOT_CAUSE_SYSTEM, user)
    except Exception:
        return []

    valid_ids = {p.pattern_id for p in l1_signals}

    def _build_causes(resp_text: str) -> "tuple[list[RootCause], int, int]":
        parsed = 0
        dropped = 0
        out: list[RootCause] = []
        for raw in _parse_rc_list(resp_text):
            parsed += 1
            try:
                rc = RootCause.from_dict(raw)
            except Exception:
                continue
            # Keep only ids that are actually among the L1 signals we asked about;
            # fall back to the full signal set if the LLM omitted/garbled the ids.
            rc.pattern_ids = [pid for pid in rc.pattern_ids if pid in valid_ids]
            if not rc.pattern_ids:
                rc.pattern_ids = sorted(valid_ids)
            # CODE GATE: a level with no content or no evidence is not a diagnosis;
            # drop it (a flagged-but-dropped RootCause has no observable caller).
            if rc.missing_levels():
                dropped += 1
                continue
            out.append(rc)
        return out, parsed, dropped

    causes, n_parsed, n_dropped = _build_causes(text)

    # Unified JSON repair: the model routinely produces an excellent diagnosis
    # whose JSON form is corrupted (e.g. unescaped double-quotes in the cited
    # evidence split the strings into spurious keys), so every level loses its
    # evidence and the CODE GATE drops the whole thing -> 0 causes -> a generic
    # fallback strategy downstream. Re-ask the model to repair its own output
    # (with the original request as context) and retry once.
    if not causes:
        from css.model.json_repair import repair_json_via_llm
        repaired = repair_json_via_llm(
            client, _ROOT_CAUSE_SYSTEM, user, text, stage="root_cause_attribution"
        )
        if repaired is not None:
            r_causes, r_parsed, r_dropped = _build_causes(repaired)
            if r_causes:
                _log.info(
                    "[json-repair:root_cause] recovered %d root cause(s) from a "
                    "malformed response via LLM repair (was %d parsed / %d gated)",
                    len(r_causes), n_parsed, n_dropped,
                )
                causes, n_parsed, n_dropped = r_causes, r_parsed, r_dropped

    # High-leverage first.
    causes.sort(key=lambda rc: _leverage_sort_key(rc, library))

    from css.tracing import log_event

    def _clip(s: str, n: int = 400) -> str:
        s = (s or "").strip().replace("\n", " ")
        return s if len(s) <= n else s[:n] + "…"

    log_event("root_cause_attribution",
              n_l1_signals=len(l1_signals),
              n_parsed=n_parsed,
              n_dropped_by_gate=n_dropped,
              n_causes=len(causes),
              causes=[{
                  "pattern_ids": rc.pattern_ids,
                  "behavioral": _clip(rc.behavioral),
                  "process": _clip(rc.process),
                  "strategy": _clip(rc.strategy),
                  "assumption": _clip(rc.assumption),
                  "leverage": _clip(rc.leverage, 200),
              } for rc in causes])

    return causes
