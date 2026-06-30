"""The mechanism<->environment contract: the ``TaskEnv`` protocol.

Every task environment (SpreadsheetBench, Bird, ...) implements this narrow
protocol. The CSS mechanism (rollout, optimizer, analysis, orchestrator) depends
ONLY on these methods; it never imports any concrete environment. Everything
task-specific (the execution agent, its tools, its prompts, its evaluation)
lives inside each environment's ``run_one`` and is invisible to the mechanism.

The contract is intentionally minimal:
  * ``train_items`` / ``val_items`` / ``test_items`` return the raw task items
    (plain dicts) for each split. Each item carries whatever the env needs;
    the mechanism only forwards it back to ``run_one``.
  * ``run_one`` executes ONE (task, rollout) under a skill document and returns
    a :class:`~css.data.rollout.TaskResult`. How it does so (which agent
    framework, which tools, which prompts) is entirely the env's business.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from css.data.rollout import TaskResult
    from css.model.client import LLMClient


@runtime_checkable
class TaskEnv(Protocol):
    """The narrow surface CSS rollout code depends on.

    Implementations supply task items per split and execute one (task, rollout)
    under a given skill document, returning a :class:`TaskResult`.
    """

    def train_items(self) -> list[dict]: ...

    def val_items(self) -> list[dict]: ...

    def test_items(self) -> list[dict]: ...

    def run_one(
        self,
        item: dict,
        skill_text: str,
        target_client: "LLMClient",
        out_dir: str,
        *,
        rollout_index: int = 0,
        epoch: int = -1,
        node_id: str = "",
    ) -> "TaskResult": ...
