"""Trajectory history for the WebArena agent's single-turn prompts.

Design (agreed 2026-07-08, after the wa_0784 full/latest A-B probe): the agent
prompt is rebuilt every turn (survey field standard — no past observations),
and the past is carried by ONE time-ordered TRAJECTORY HISTORY block with two
layers:

- MECHANICAL layer (deterministic, zero LLM): per-step effect signatures
  (URL change + observation line diff), consecutive no-effect folding, repeat
  alerts, the rendered block itself, and token telemetry.
- SCRIBE layer (:class:`Scribe`): one env-side LLM call per *page-changing*
  step that restates the step's INTENT (from the agent's own reasoning) and
  transcribes 0-3 key FACTS from the observation the agent acted on. The
  scribe is part of the environment (baselines keep it too); the task agent
  never writes its own history and is unaware of how the block is produced.

Discipline (user rulings):
- Clerk, not adviser: facts and intent only — never suggestions, evaluations
  or predictions. Strategy stays the skill document's learning target.
- PAGE facts vs AGENT intent keep separate labels (epistemic provenance), so
  a wrong belief can never masquerade as a page fact.
- Anti-bloat is STRUCTURAL, never truncation: <=3 facts per step (cap on
  count, not content), empty scribe output is legal and expected for routine
  steps, no-change steps are folded mechanically and never reach the scribe.
  The token budget check is telemetry only (tokens-not-chars ruling; the
  rich-semantic-content ruling forbids length caps on semantic fields).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

_log = logging.getLogger("css.webarena")

# Fold runs of >=2 consecutive contentless steps; alert when the tail shows
# this many actions without any page change (mechanical loop signal).
_FOLD_MIN = 2
_NO_CHANGE_ALERT = 4


def short_path(url: str) -> str:
    """Path+query view of a URL — stack-local origins carry no information."""
    try:
        p = urlparse(url or "")
        out = p.path or "/"
        if p.query:
            out += "?" + p.query
        return out
    except Exception:  # noqa: BLE001 — display helper must never raise
        return url or "/"


def effect_signature(url_before: str, url_after: str, obs_before: str,
                     obs_after: str, *, invalid: bool = False,
                     exec_error: str = "") -> tuple[str, bool]:
    """Deterministic one-line effect of a step + whether the page changed.

    The line diff is set-based on observation lines: coarse, but it reliably
    separates "the click did something" (menu expanded, rows loaded) from
    "nothing happened" — the signal whose absence caused the 28-click loop in
    the 2026-07-08 latest-arm probe.
    """
    if invalid:
        return "INVALID action (not executed)", False
    before = set((obs_before or "").splitlines())
    after = set((obs_after or "").splitlines())
    appeared = len(after - before)
    disappeared = len(before - after)
    prefix = f"action FAILED ({exec_error}); " if exec_error else ""
    if short_path(url_before) != short_path(url_after):
        return prefix + f"navigated to {short_path(url_after)}", True
    if appeared or disappeared:
        return (prefix + "URL unchanged; page updated "
                f"(+{appeared}/-{disappeared} lines)", True)
    return prefix + "no visible page change", False


@dataclass
class StepRecord:
    turn: int                 # 1-based
    action: str
    url: str                  # short path of the page the step acted ON
    effect: str
    page_changed: bool
    intent: str = ""          # scribe: the agent's stated goal this step
    facts: list = field(default_factory=list)   # scribe: 0-3 page facts

    @property
    def has_content(self) -> bool:
        return bool(self.intent or self.facts)


class TrajectoryHistory:
    """Accumulates step records and renders the TRAJECTORY HISTORY block."""

    def __init__(self, *, budget_tokens: int = 3000,
                 count_tokens: "Callable[[str], int | None] | None" = None) -> None:
        self.records: list[StepRecord] = []
        self._budget = int(budget_tokens)
        self._count = count_tokens
        self._warned = False

    # -- recording ------------------------------------------------------------
    def append(self, *, turn: int, action: str, url: str, effect: str,
               page_changed: bool) -> StepRecord:
        rec = StepRecord(turn=turn, action=action, url=short_path(url),
                         effect=effect, page_changed=page_changed)
        self.records.append(rec)
        return rec

    def should_scribe(self) -> bool:
        """Scribe the step just appended? Its transcription target is the
        observation the step DECIDED on — fresh iff the PREVIOUS step changed
        the page (first step: the landing page is always fresh)."""
        if len(self.records) <= 1:
            return True
        return self.records[-2].page_changed

    # -- rendering ------------------------------------------------------------
    def _path_trail(self) -> str:
        seen: list[str] = []
        for rec in self.records:
            if not seen or seen[-1] != rec.url:
                seen.append(rec.url)
        return " -> ".join(seen)

    def _alerts(self) -> list[str]:
        out: list[str] = []
        tail_same = 0
        for rec in reversed(self.records):
            if rec.page_changed or rec.action != self.records[-1].action:
                break
            tail_same += 1
        if tail_same >= 3:
            out.append(f"alert: '{self.records[-1].action}' repeated "
                       f"{tail_same} times with no page change")
        tail_flat = 0
        for rec in reversed(self.records):
            if rec.page_changed:
                break
            tail_flat += 1
        if tail_flat >= _NO_CHANGE_ALERT and tail_flat > tail_same:
            out.append(f"alert: the last {tail_flat} actions produced "
                       "no page change")
        return out

    @staticmethod
    def _block(rec: StepRecord) -> list[str]:
        lines = [f"--- t{rec.turn} @ {rec.url} ---",
                 f"ACTION: {rec.action} -> {rec.effect}"]
        if rec.intent:
            lines.append(f"INTENT: {rec.intent}")
        if rec.facts:
            lines.append("FACTS:")
            lines.extend(f"  - {f}" for f in rec.facts)
        return lines

    @staticmethod
    def _fold(run: list[StepRecord]) -> list[str]:
        actions: list[str] = []
        for rec in run:
            if rec.action not in actions:
                actions.append(rec.action)
        shown = " / ".join(actions[:3]) + (" / ..." if len(actions) > 3 else "")
        return [f"--- t{run[0].turn}..t{run[-1].turn} @ {run[0].url} ---",
                f"ACTIONS (folded, {len(run)} steps, no page change): {shown}"]

    def render(self, *, turn_now: int, max_turns: int) -> str:
        """The block injected into the agent's prompt. Empty before step 1.

        Contentless no-change steps (never scribed by construction) fold into
        one line per run, so a 20-step loop costs one history line while the
        repeat count stays loud in the alert header.
        """
        if not self.records:
            return ""
        lines = ["TRAJECTORY HISTORY (maintained by the harness; you do not "
                 "write this):",
                 f"path: {self._path_trail()}",
                 f"turn {turn_now}/{max_turns}"]
        lines.extend(self._alerts())
        lines.append("")
        run: list[StepRecord] = []
        for rec in self.records:
            if not rec.page_changed and not rec.has_content:
                run.append(rec)
                continue
            if run:
                lines.extend(self._fold(run) if len(run) >= _FOLD_MIN
                             else self._block(run[0]))
                run = []
            lines.extend(self._block(rec))
        if run:
            lines.extend(self._fold(run) if len(run) >= _FOLD_MIN
                         else self._block(run[0]))
        block = "\n".join(lines)
        self._telemetry(block)
        return block

    def _telemetry(self, block: str) -> None:
        """Token telemetry ONLY — no truncation path exists by design.

        Budget unit is tokens (tokens-not-chars ruling). The chars//3 lower
        gate only decides whether to spend an exact /tokenize round-trip; the
        verdict itself always uses the exact count when available.
        """
        if self._warned or len(block) // 3 <= self._budget:
            return
        tokens = None
        if self._count is not None:
            try:
                tokens = self._count(block)
            except Exception:  # noqa: BLE001 — telemetry must never break a turn
                tokens = None
        if tokens is None:
            tokens = len(block) // 3
        if tokens > self._budget:
            self._warned = True
            _log.warning(
                "webarena/history — block at ~%d tokens exceeds the %d-token "
                "alert budget (telemetry only; structural anti-bloat should "
                "keep this rare — inspect the scribe outputs)",
                tokens, self._budget)


# ── scribe ────────────────────────────────────────────────────────────────────

_PLACEHOLDER_FACTS = {"(none)", "none", "n/a", "-", ""}


def parse_scribe_reply(reply: str) -> tuple[str, list[str]]:
    """Line protocol: one INTENT line, 0-3 FACT lines. Tolerant: anything
    unparseable degrades to an empty contribution, never an error."""
    intent, facts = "", []
    for ln in (reply or "").splitlines():
        s = ln.strip()
        if s.upper().startswith("INTENT:") and not intent:
            intent = s[len("INTENT:"):].strip()
        elif s.upper().startswith("FACT:"):
            f = s[len("FACT:"):].strip()
            if f.lower() not in _PLACEHOLDER_FACTS and f not in facts:
                facts.append(f)
    if len(facts) > 3:
        _log.warning("webarena/scribe — %d FACT lines, keeping the first 3 "
                     "(structural count cap, not content truncation)",
                     len(facts))
        facts = facts[:3]
    return intent, facts


class Scribe:
    """Env-side trajectory scribe: one LLM call per page-changing step.

    Runs on the TARGET client path deliberately: the scribe is part of the
    environment every arm shares (baselines included), so its cost belongs to
    the episode's target-side accounting, and the optimizer path's
    ground-truth-firewall preamble must not leak into its prompt. It never
    sees the record's ``eval`` block (run_one strips it before the episode).
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self.telemetry: list[dict] = []

    def transcribe(self, *, objective: str, url: str, observation: str,
                   reasoning: str, action_str: str,
                   effect: str) -> tuple[str, list[str]]:
        import time
        from css.envs.webarena import prompts as P
        user = P.build_scribe_user(objective=objective, url=url,
                                   observation=observation,
                                   reasoning=reasoning,
                                   action_str=action_str, effect=effect)
        t0 = time.time()
        try:
            # The scribe is environment bookkeeping, not a rollout: it must be
            # greedy like every other non-rollout call (user ruling 2026-07-08).
            # It runs on the TARGET client only because the optimizer path would
            # inject the ground-truth firewall preamble into a clerk's prompt.
            reply = self._client.complete_target(P.SCRIBE_SYSTEM, user,
                                                 temperature=0.0)
        except Exception as exc:  # noqa: BLE001 — scribe failure must not hurt the episode
            _log.warning("webarena/scribe — call failed, empty contribution: %s",
                         exc)
            self.telemetry.append({"wall_s": round(time.time() - t0, 2),
                                   "error": str(exc)[:200]})
            return "", []
        intent, facts = parse_scribe_reply(reply)
        self.telemetry.append({"wall_s": round(time.time() - t0, 2),
                               "reply_chars": len(reply or ""),
                               "n_facts": len(facts)})
        return intent, facts
