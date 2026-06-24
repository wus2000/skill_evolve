"""Parallel task rollout (Phase 2: Task Execution & Rollout Infrastructure).

This module is the concurrency layer between the optimizer loop and a concrete
task environment. Given a list of task items and a frozen skill text, it runs
``K = k_rollouts`` independent rollouts of each item through ``env.run_one`` and
returns a flat list of :class:`~css.data.rollout.TaskResult` objects.

Design constraints honoured here:
  * **Exact counts.** A batch over ``M`` items always returns exactly
    ``k_rollouts * M`` results. A per-rollout exception or timeout does NOT drop
    the unit; it is recorded as a *failed* ``TaskResult`` (``hard=0``,
    ``soft=0``, ``fail_reason`` set). This keeps pass-rate denominators correct.
  * **First Law (LLM-Code division).** The rollout layer receives only a
    target-capable client; it never touches the optimizer. ``target_client`` is
    forwarded verbatim to ``env.run_one`` and is otherwise opaque here.
  * **Bounded concurrency + per-task timeout.** Adapted from SkillOpt's
    ``run_spreadsheet_batch_codegen`` (skillopt/envs/spreadsheetbench/
    rollout.py:793) ThreadPoolExecutor + ``wait(..., FIRST_COMPLETED)`` polling
    pattern, but the unit of work is a single (item, rollout_index) pair rather
    than a whole item, so timeouts are enforced per rollout.

``grouped_batch_rollout`` simply wraps the flat result through Phase 1's
``group_rollouts`` so callers that need same-task success/failure pairs (the K=3
contrastive design) get :class:`TaskRolloutGroup` objects directly.
"""
from __future__ import annotations

import os
import time
from concurrent.futures import (
    FIRST_COMPLETED,
    ThreadPoolExecutor,
    TimeoutError as FuturesTimeoutError,
    wait,
)
from typing import TYPE_CHECKING

from css.data.rollout import TaskResult, group_rollouts

if TYPE_CHECKING:  # pragma: no cover - typing only
    from css.data.rollout import TaskRolloutGroup
    from css.envs.spreadsheetbench.task_interface import TaskEnv
    from css.model.client import LLMClient


def _item_id(item: dict) -> str:
    """Best-effort stable task id for an env item dict."""
    return str(item.get("task_id", item.get("id", "")))


def _failed_result(
    item: dict,
    rollout_index: int,
    fail_reason: str,
    *,
    epoch: int,
    node_id: str,
) -> TaskResult:
    """Construct a failed (but counted) TaskResult for a dropped unit.

    Used for both per-rollout exceptions and per-rollout timeouts so that the
    returned list always has exactly ``k_rollouts * len(items)`` entries.
    """
    return TaskResult(
        task_id=_item_id(item),
        rollout_index=rollout_index,
        hard=0,
        soft=0.0,
        n_cases=0,
        n_pass=0,
        fail_reason=fail_reason,
        messages=[],
        n_turns=0,
        task_type=str(item.get("task_type", "")),
        instruction_type=str(item.get("instruction_type", "")),
        epoch=epoch,
        node_id=node_id,
    )


def batch_rollout(
    env: "TaskEnv",
    items: list[dict],
    skill_text: str,
    target_client: "LLMClient",
    *,
    k_rollouts: int,
    out_dir: str,
    max_workers: int = 32,
    task_timeout: int = 600,
    epoch: int = -1,
    node_id: str = "",
) -> list["TaskResult"]:
    """Run ``k_rollouts`` rollouts of every item through ``env.run_one``.

    Each (item, rollout_index) pair is submitted as one future to a
    ``ThreadPoolExecutor`` bounded by ``max_workers``. The submission order is
    item-major then rollout-minor, so results within a task stay contiguous and
    in ascending ``rollout_index`` (subject to completion interleaving, which is
    later normalised by ``group_rollouts``).

    A per-rollout hard timeout of ``task_timeout`` seconds is enforced by polling
    with ``wait(..., FIRST_COMPLETED)`` and comparing each in-flight future's
    elapsed time against its recorded start. On timeout or any exception, a
    failed ``TaskResult`` is recorded instead of dropping the unit.

    Returns exactly ``k_rollouts * len(items)`` TaskResults.
    """
    os.makedirs(out_dir, exist_ok=True)

    if k_rollouts <= 0 or not items:
        return []

    # Build the full work list: (item, rollout_index), item-major.
    units: list[tuple[dict, int]] = [
        (item, r) for item in items for r in range(k_rollouts)
    ]
    total = len(units)

    started_at: dict[int, float] = {}

    def _run_unit(unit_id: int, item: dict, rollout_index: int) -> "TaskResult":
        started_at[unit_id] = time.time()
        return env.run_one(
            item,
            skill_text,
            target_client,
            out_dir,
            rollout_index=rollout_index,
            epoch=epoch,
            node_id=node_id,
        )

    results: list[TaskResult] = []
    t0 = time.time()

    ex = ThreadPoolExecutor(max_workers=max(1, max_workers))
    try:
        # unit_id -> (future-key) mapping so we can resolve item/index on completion.
        fut_meta: dict = {}
        futs = {}
        for uid, (item, rollout_index) in enumerate(units):
            fut = ex.submit(_run_unit, uid, item, rollout_index)
            futs[fut] = uid
            fut_meta[uid] = (item, rollout_index)

        pending_futs = set(futs)
        while pending_futs:
            done, _ = wait(pending_futs, timeout=5, return_when=FIRST_COMPLETED)
            now = time.time()
            timed_out = [
                fut
                for fut in pending_futs - done
                if futs[fut] in started_at
                and now - started_at[futs[fut]] >= task_timeout
            ]
            for fut in done:
                pending_futs.remove(fut)
                uid = futs[fut]
                item, rollout_index = fut_meta[uid]
                try:
                    res = fut.result()
                except FuturesTimeoutError:
                    res = _failed_result(
                        item,
                        rollout_index,
                        f"task-timeout-{task_timeout}s",
                        epoch=epoch,
                        node_id=node_id,
                    )
                except Exception as e:  # noqa: BLE001 - any rollout failure is recorded, not raised
                    res = _failed_result(
                        item,
                        rollout_index,
                        f"unexpected: {type(e).__name__}: {e}",
                        epoch=epoch,
                        node_id=node_id,
                    )
                results.append(res)
            for fut in timed_out:
                pending_futs.remove(fut)
                fut.cancel()
                uid = futs[fut]
                item, rollout_index = fut_meta[uid]
                results.append(
                    _failed_result(
                        item,
                        rollout_index,
                        f"task-timeout-{task_timeout}s",
                        epoch=epoch,
                        node_id=node_id,
                    )
                )
    finally:
        ex.shutdown(wait=False, cancel_futures=True)

    # Invariant: every submitted unit produced exactly one result.
    assert len(results) == total, (
        f"batch_rollout count mismatch: got {len(results)}, expected {total}"
    )
    n_pass = sum(r.hard for r in results)
    print(
        f"  [batch_rollout] {total} units ({len(items)} tasks x k={k_rollouts}) "
        f"pass={n_pass}/{total} in {time.time() - t0:.0f}s"
    )
    return results


def grouped_batch_rollout(
    env: "TaskEnv",
    items: list[dict],
    skill_text: str,
    target_client: "LLMClient",
    *,
    k_rollouts: int,
    out_dir: str,
    max_workers: int = 32,
    task_timeout: int = 600,
    epoch: int = -1,
    node_id: str = "",
) -> list["TaskRolloutGroup"]:
    """``group_rollouts(batch_rollout(...))`` — the K rollouts grouped per task.

    Convenience wrapper returning :class:`TaskRolloutGroup` objects (one per
    task, with its ``k_rollouts`` rollouts) for contrastive / persistent-fail
    analysis. Grouping preserves first-seen task order.
    """
    results = batch_rollout(
        env,
        items,
        skill_text,
        target_client,
        k_rollouts=k_rollouts,
        out_dir=out_dir,
        max_workers=max_workers,
        task_timeout=task_timeout,
        epoch=epoch,
        node_id=node_id,
    )
    return group_rollouts(results)
