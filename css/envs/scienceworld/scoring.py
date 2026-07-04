"""ScienceWorld scoring: native 0-100 predicates + AgentBoard subgoal matcher.

Two protocols share this env (see docs/env_prep/scienceworld_ONBOARDING.md §3c):

  * ORIGINAL (native): ScienceWorld's own 0-100 completion score.
      hard = 1 iff score == 100 (fully solved); soft = clamp(score,0,100)/100.
      Key hard off the SCORE, never off ``isCompleted`` — isCompleted is also
      True on step-limit and on negative-score auto-termination (both FAILURES).

  * AGENTBOARD (subgoal): latched regex subgoal matching against the OBSERVATION
      stream — a verbatim reimplementation of AgentBoard's
      ``Scienceworld._check_temperature_string`` / ``get_reward`` /
      ``_check_is_done`` (hkust-nlp/AgentBoard, agentboard/environment/
      scienceworld_env.py). Progress Rate = fraction of subgoal patterns matched
      so far (monotonic latch → final == max); Success = all subgoals matched.
"""
from __future__ import annotations

import re


def native_hard(score: int) -> int:
    """1 iff the task is fully solved (native score == 100)."""
    return 1 if int(score) >= 100 else 0


def native_soft(score: int) -> float:
    """Native progress in [0, 1]; negative (penalized) scores clamp to 0."""
    return max(0, min(100, int(score))) / 100.0


class SubgoalMatcher:
    """AgentBoard latched-regex subgoal matcher (PR + SR).

    Faithful to AgentBoard: each subgoal is a regex pattern; after every step it
    is tested with ``re.search`` against that step's observation and LATCHED to
    done on first match (monotonic). PR = matched / total (so the final PR equals
    the max-so-far). SR = all subgoals matched. A malformed regex degrades to a
    literal substring test rather than raising.
    """

    def __init__(self, subgoals: "list[str]") -> None:
        self.patterns: "list[str]" = [str(s) for s in (subgoals or [])]
        self.done: "list[int]" = [0] * len(self.patterns)

    def update(self, observation: str) -> None:
        """Latch any subgoal whose pattern matches this observation."""
        obs = observation or ""
        for i, pat in enumerate(self.patterns):
            if self.done[i]:
                continue
            try:
                if re.search(pat, obs):
                    self.done[i] = 1
            except re.error:  # malformed annotation → literal match
                if pat and pat in obs:
                    self.done[i] = 1

    @property
    def n_subgoals(self) -> int:
        return len(self.patterns)

    @property
    def n_done(self) -> int:
        return sum(self.done)

    @property
    def pr(self) -> float:
        """Progress Rate — fraction of subgoals achieved (0 if none defined)."""
        return (self.n_done / len(self.done)) if self.done else 0.0

    @property
    def sr(self) -> int:
        """Success — 1 iff every subgoal has been achieved."""
        return 1 if self.done and self.n_done == len(self.done) else 0


# Observations that signal an invalid/ungrammatical action (for diagnostics; the
# environment itself supplies these strings as natural feedback — we never raise).
_INVALID_MARKERS = (
    "no known action",
    "no known object",
    "ambiguous request",
    "unknown action",
)


def is_invalid_observation(observation: str) -> bool:
    obs = (observation or "").lower()
    return any(m in obs for m in _INVALID_MARKERS)


def parse_subgoals(raw: "object") -> "list[str]":
    """Normalize an instance's ``subgoals`` field into a list of regex patterns.

    Accepts either the canonical LIST form (AgentBoard's ``data.tar.gz``) or the
    HF-unpacked STRING form ("Subgoal 1: ...\\nSubgoal 2: ..."), splitting the
    latter on the ``Subgoal N:`` prefixes and stripping them.
    """
    if isinstance(raw, list):
        return [str(s) for s in raw]
    text = str(raw or "")
    if not text.strip():
        return []
    # Split on "Subgoal <n>:" markers; keep the content after each marker.
    parts = re.split(r"Subgoal\s*\d+\s*:\s*", text)
    return [p.strip() for p in parts if p.strip()]
