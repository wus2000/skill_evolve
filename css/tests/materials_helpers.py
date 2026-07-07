"""Shared fakes for the materials tests (not a test module — no test_* prefix).

The whole materials pipeline funnels its optimizer-JSON calls through
``css.materials.common.run_json_stage``. A test monkeypatches that one function
with :class:`FakeStage`, which routes on the ``stage`` argument and returns the
ALREADY-PARSED object each stage expects (a dict for object stages, a list for
the ``parse_list_field`` stages). Handlers may be a constant or a callable of the
rendered ``user`` string, so a handler can adapt to the actual items on disk.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Callable


class FakeStage:
    """Monkeypatch stand-in for ``common.run_json_stage`` routed by ``stage``."""

    def __init__(self, handlers: dict | None = None) -> None:
        self.handlers: dict[str, Any] = dict(handlers or {})
        self.calls: list[tuple[str, str]] = []

    def set(self, stage: str, resp: Any) -> "FakeStage":
        self.handlers[stage] = resp
        return self

    def count(self, stage: str) -> int:
        return sum(1 for s, _ in self.calls if s == stage)

    def __call__(self, client, system, user, *, parse, stage,
                 cfg=None, required=None, ok=None, max_tokens=4096):
        self.calls.append((stage, user))
        if stage not in self.handlers:
            raise AssertionError(f"unexpected materials stage: {stage!r}")
        h = self.handlers[stage]
        return h(user) if callable(h) else h


# ── user-string parsers (let handlers adapt to real items) ───────────────────
def n_screen_items(user: str) -> int:
    return len(re.findall(r"### Item \d+", user))


def screen_all(verdict: str) -> Callable[[str], list]:
    """Every item -> the same verdict (list of verdict dicts, index-aligned)."""
    def _h(user: str) -> list:
        return [{"index": i, "verdict": verdict, "violated_criteria": [],
                 "feedback": "", "quoted_offense": ""}
                for i in range(1, n_screen_items(user) + 1)]
    return _h


def screen_by_rule(rule: Callable[[str], str]) -> Callable[[str], list]:
    """Per-item verdict from ``rule(item_text)``; items split on the ### markers."""
    def _h(user: str) -> list:
        chunks = re.split(r"### Item \d+\n", user)[1:]
        out = []
        for i, chunk in enumerate(chunks, 1):
            out.append({"index": i, "verdict": rule(chunk.strip()),
                        "violated_criteria": [4], "feedback": "lift to altitude",
                        "quoted_offense": chunk.strip()[:40]})
        return out
    return _h


def traj_ids_from_signatures(user: str) -> list[str]:
    return re.findall(r"(?m)^- (\S+) \[", user)


def task_ids_from_frontier(user: str) -> list[str]:
    return re.findall(r"### task (\S+)", user)


def global_hard_from_user(user: str) -> list[str]:
    m = re.search(r"failed at every node\)\n(.+)", user)
    if not m:
        return []
    return [t.strip() for t in m.group(1).split(",") if t.strip()]


# ── on-disk trajectory writer (mirrors the shared env persister) ─────────────
def write_rollout(exploit_dir: str, step: int, task_id: str, k: int, hard: int,
                  text: str = "did work", fail_reason: str = "") -> None:
    pred = os.path.join(exploit_dir, f"step{step}", "rollout", "predictions",
                        task_id, f"r{k}")
    os.makedirs(pred, exist_ok=True)
    result = {
        "task_id": task_id, "rollout_index": k, "hard": hard, "soft": float(hard),
        "n_cases": 1, "n_pass": hard,
        "messages": [{"role": "user", "content": f"task {task_id}"},
                     {"role": "assistant", "content": text}],
        "n_turns": 2, "fail_reason": fail_reason, "epoch": 0, "node_id": "n0000",
    }
    with open(os.path.join(pred, "result.json"), "w", encoding="utf-8") as f:
        json.dump(result, f)


# ── canned interpretation record maker ───────────────────────────────────────
def interp_record(traj_id: str, task_id: str, passed: bool, *,
                  signature: str = "explore-then-commit",
                  narrative: str = "The agent explored broadly then committed.",
                  adherence: list | None = None) -> dict:
    return {
        "traj_id": traj_id, "task_id": task_id, "rollout_index": 0, "step": 0,
        "passed": passed, "hard": int(passed), "soft": float(passed), "fail_reason": "",
        "interp": {
            "narrative": narrative,
            "outcome_causality": "the way it behaved led to the outcome",
            "anomalies": "",
            "behavior_signature": signature,
            "adherence": adherence if adherence is not None else
            [{"section": "Exploration", "verdict": "followed",
              "evidence_steps": [1], "note": "scanned first"}],
        },
        "interp_prose": narrative,
    }


DEFAULT_PROSE = (
    "## OVERALL BEHAVIOR\nThe agent explored broadly then committed to one "
    "path (at turn 1 it scanned the workspace).\n\n"
    "## STRATEGY ADHERENCE\nExploration: followed — at turn 1 the agent "
    "scanned first.\n\n"
    "## OUTCOME CAUSALITY\nBroad exploration surfaced the key constraint.\n\n"
    "## ANOMALIES\nNone observed.\n")


def default_extract(user: str) -> dict:
    return {
        "behavior_signature": "explore-then-commit",
        "adherence": [{"section": "Exploration", "verdict": "followed",
                       "evidence_steps": [1], "note": "scanned first"}],
    }


class ProseClient:
    """Fake optimizer client for the two-pass interpret: pass 1 prose."""

    def __init__(self, prose: str = DEFAULT_PROSE):
        self.prose = prose
        self.calls = 0

    def complete_optimizer(self, system, user, max_tokens=4096):
        self.calls += 1
        return self.prose, {}


def default_handlers() -> dict:
    """A full set of benign handlers for an end-to-end pass; override per test."""
    return {
        "interp_extract": default_extract,
        "interp_screen": screen_all("pass"),
        "interp_revise": lambda u: {"revised": "clean revised text"},
        "group_pass1": lambda u: {"groups": [{
            "group_key": "explore_then_commit",
            "rationale": "They explore broadly then commit.",
            "member_traj_ids": traj_ids_from_signatures(u),
            "representative_traj_ids": traj_ids_from_signatures(u)[:1],
            "uncertain_traj_ids": []}]},
        "group_pass2": lambda u: [],
        "group_analysis": lambda u: {
            "analysis_narrative": "This mode explores broadly before committing.",
            "distilled_claims": [{"claim": "Exploring first improves outcomes.",
                                  "evidence": "several trajectories"}],
            "open_questions": ["When does it over-explore?"]},
        "group_merge": lambda u: {"analysis_narrative": "merged",
                                  "distilled_claims": [], "open_questions": []},
        "adherence_reading": lambda u: {"reading": "Exploration was honored throughout."},
        "burst_summary": lambda u: {"summary_md": "## Burst\nExplore-then-commit dominated."},
        "profile_claims_screen": screen_all("pass"),
        "profile_integrate": lambda u: {"document_md":
            "## Actual behavior patterns\nExplore-then-commit.\n\n## Evolution log\n- burst"},
        "profile_audit": lambda u: {"ledger": [], "unaccounted": []},
        "frontier_group": lambda u: {"groups": [{
            "group_key": "mode_stuck", "rationale": "stuck on the residual tasks",
            "task_ids": task_ids_from_frontier(u)}]},
        "frontier_narrative": lambda u: {
            "narrative": "The behavior cannot reach the goal structurally.",
            "attribution": "A", "attribution_rationale": "missing mechanism",
            "escalate_to_exploration": False},
        "frontier_integrate": lambda u: {"document_md":
            "## Frontier\nmode_stuck: missing mechanism.\n\n## Evolution log\n- burst"},
        "frontier_audit": lambda u: {"ledger": [], "unaccounted": []},
        "frontier_summary_screen": screen_all("pass"),
        "global_group": lambda u: {"groups": [{
            "group_key": "global_common", "task_ids": global_hard_from_user(u),
            "character": "shared residual"}]},
        "global_reading": lambda u: {"narrative_md": "cross-strategy synthesis",
                                     "common_mechanism": True, "summary": "common",
                                     "priority": 5},
    }
