"""Rollout / trajectory data model.

A *rollout* is one execution of one task by the frozen task agent under a given
skill document. CSS runs ``K`` rollouts per task (default K=3) to (a) reduce LLM
sampling noise, (b) produce natural success/failure contrastive pairs on the
same task, and (c) give pattern-occurrence-rate confidence.

The field layout mirrors SkillOpt's ``RolloutResult`` and the per-task result
dict produced by ``skillopt/envs/spreadsheetbench/rollout.py::process_one`` so
that the SpreadsheetBench task interface (Phase 2) can adapt to it with a thin
mapping layer.

Key conventions (inherited from SkillOpt):
  * ``hard``  — 1 if all test cases pass, else 0 (the headline pass metric).
  * ``soft``  — fraction of test cases passed in [0, 1].
  * ``messages`` — the agent conversation = the trajectory itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TaskResult:
    """Result + trajectory of a single (task, rollout) execution.

    One task run produces one ``TaskResult``. ``rollout_index`` distinguishes
    the K repeats of the same ``task_id``.
    """

    task_id: str
    rollout_index: int = 0

    # ── Outcome ──────────────────────────────────────────────────────────
    hard: int = 0                      # 1 = all test cases pass
    soft: float = 0.0                  # fraction of cases passed
    n_cases: int = 0
    n_pass: int = 0
    fail_reason: str = ""

    # ── Trajectory ───────────────────────────────────────────────────────
    # The conversation IS the trajectory: ordered list of {role, content[, ...]}
    # messages, exactly as the task agent produced them.
    messages: list[dict] = field(default_factory=list)
    n_turns: int = 0

    # ── Task metadata (carried for analysis / prompt construction) ────────
    task_type: str = ""                # e.g. "cell_level" | "sheet_level"
    task_description: str = ""
    instruction_type: str = ""

    # ── Provenance ────────────────────────────────────────────────────────
    epoch: int = -1                    # which epoch produced this rollout
    node_id: str = ""                  # which tree node's skill doc was used

    # Token usage for this rollout (budget tracking). First-class per lead Q5;
    # typically {"prompt_tokens", "completion_tokens", "total_tokens"}.
    token_usage: dict[str, int] = field(default_factory=dict)

    # ── SpreadsheetBench-specific, promoted to first-class (lead Q5) ──────
    # Every Phase 2-3 analysis stage reads these, so they are typed fields
    # rather than extras lookups.
    phase: str = ""                    # last pipeline phase reached (setup/llm/exec/eval/...)
    spreadsheet_preview: str = ""      # workbook preview shown to the agent
    target_system_prompt: str = ""     # the system prompt the task agent saw
    target_user_prompt: str = ""       # the user prompt the task agent saw

    # Env-specific overflow (e.g. predicted_answer, cases, error).
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return bool(self.hard)

    # ``is_correct`` / ``score`` are the lead-requested (Q5) ergonomic names;
    # they alias the canonical ``passed`` (over ``hard``) and ``soft`` fields
    # rather than duplicating storage.
    @property
    def is_correct(self) -> bool:
        return bool(self.hard)

    @property
    def score(self) -> float:
        return self.soft

    @classmethod
    def from_dict(cls, d: dict) -> "TaskResult":
        known = {
            "task_id", "id", "rollout_index", "hard", "soft", "n_cases", "n_pass",
            "fail_reason", "messages", "conversation", "n_turns", "task_type",
            "task_description", "instruction_type", "epoch", "node_id",
            "token_usage", "phase", "spreadsheet_preview",
            "target_system_prompt", "target_user_prompt", "extras",
        }
        extras = dict(d.get("extras", {}))
        # Absorb any unknown keys into extras for forward-compat.
        for k, v in d.items():
            if k not in known:
                extras[k] = v
        # SkillOpt's process_one stores the trajectory under "conversation";
        # CSS stores it under "messages". Accept either so the SpreadsheetBench
        # result dict adapts directly without dropping the trajectory.
        messages = d.get("messages")
        if not messages:
            messages = d.get("conversation", [])
        return cls(
            task_id=str(d.get("task_id", d.get("id", ""))),
            rollout_index=int(d.get("rollout_index", 0)),
            hard=int(d.get("hard", 0)),
            soft=float(d.get("soft", 0.0)),
            n_cases=int(d.get("n_cases", 0)),
            n_pass=int(d.get("n_pass", 0)),
            fail_reason=str(d.get("fail_reason", "")),
            messages=list(messages),
            n_turns=int(d.get("n_turns", 0)),
            task_type=str(d.get("task_type", "")),
            task_description=str(d.get("task_description", "")),
            instruction_type=str(d.get("instruction_type", "")),
            epoch=int(d.get("epoch", -1)),
            node_id=str(d.get("node_id", "")),
            token_usage=dict(d.get("token_usage", {})),
            phase=str(d.get("phase", "")),
            spreadsheet_preview=str(d.get("spreadsheet_preview", "")),
            target_system_prompt=str(d.get("target_system_prompt", "")),
            target_user_prompt=str(d.get("target_user_prompt", "")),
            extras=extras,
        )

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "task_id": self.task_id,
            "rollout_index": self.rollout_index,
            "hard": self.hard,
            "soft": self.soft,
            "n_cases": self.n_cases,
            "n_pass": self.n_pass,
            "messages": self.messages,
            "n_turns": self.n_turns,
            "epoch": self.epoch,
            "node_id": self.node_id,
        }
        for attr in (
            "fail_reason", "task_type", "task_description", "instruction_type",
            "phase", "spreadsheet_preview", "target_system_prompt", "target_user_prompt",
        ):
            val = getattr(self, attr)
            if val:
                d[attr] = val
        if self.token_usage:
            d["token_usage"] = self.token_usage
        if self.extras:
            d["extras"] = self.extras
        return d


@dataclass
class TaskRolloutGroup:
    """The K rollouts of one task, grouped for contrastive analysis.

    The K=3 design intentionally surfaces same-task success/failure pairs, which
    are the most valuable analysis material (see design D5 / Phase 2.5).
    """

    task_id: str
    rollouts: list[TaskResult] = field(default_factory=list)

    @property
    def successes(self) -> list[TaskResult]:
        return [r for r in self.rollouts if r.passed]

    @property
    def failures(self) -> list[TaskResult]:
        return [r for r in self.rollouts if not r.passed]

    @property
    def pass_rate(self) -> float:
        """Majority-vote style task pass rate across the K rollouts."""
        if not self.rollouts:
            return 0.0
        return sum(r.hard for r in self.rollouts) / len(self.rollouts)

    @property
    def mean_soft(self) -> float:
        if not self.rollouts:
            return 0.0
        return sum(r.soft for r in self.rollouts) / len(self.rollouts)

    def contrastive_pairs(self) -> list[tuple[TaskResult, TaskResult]]:
        """All (success, failure) pairs from this task's K rollouts.

        These pairs feed Layer 1 contrastive analysis: the same task with
        different outcomes isolates the *cognitive* difference that mattered.
        """
        return [(s, f) for s in self.successes for f in self.failures]

    def is_persistent_fail(self) -> bool:
        """True if no rollout passed — a candidate persistent-fail task."""
        return len(self.rollouts) > 0 and not self.successes

    @classmethod
    def from_results(cls, task_id: str, results: list[TaskResult]) -> "TaskRolloutGroup":
        return cls(task_id=task_id, rollouts=list(results))

    @classmethod
    def from_dict(cls, d: dict) -> "TaskRolloutGroup":
        return cls(
            task_id=str(d.get("task_id", "")),
            rollouts=[TaskResult.from_dict(r) for r in d.get("rollouts", [])],
        )

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "rollouts": [r.to_dict() for r in self.rollouts],
        }


def group_rollouts(results: list[TaskResult]) -> list[TaskRolloutGroup]:
    """Group a flat list of rollouts by ``task_id`` (order preserved)."""
    order: list[str] = []
    buckets: dict[str, list[TaskResult]] = {}
    for r in results:
        if r.task_id not in buckets:
            buckets[r.task_id] = []
            order.append(r.task_id)
        buckets[r.task_id].append(r)
    return [TaskRolloutGroup.from_results(tid, buckets[tid]) for tid in order]


def aggregate_scores(results: list[TaskResult]) -> dict[str, float | int]:
    """Compute headline scores over a flat list of rollouts.

    Returns ``hard`` (mean all-pass rate over rollouts), ``soft`` (mean fraction
    over rollouts), and ``task_hard`` (fraction of *tasks* with majority pass).
    ``task_hard`` is the fair node-comparison metric used by the tree.
    """
    if not results:
        return {"hard": 0.0, "soft": 0.0, "task_hard": 0.0, "n_rollouts": 0, "n_tasks": 0}
    hard = sum(r.hard for r in results) / len(results)
    soft = sum(r.soft for r in results) / len(results)
    groups = group_rollouts(results)
    task_hard = sum(1 for g in groups if g.pass_rate >= 0.5) / len(groups)
    return {
        "hard": hard,
        "soft": soft,
        "task_hard": task_hard,
        "n_rollouts": len(results),
        "n_tasks": len(groups),
    }
