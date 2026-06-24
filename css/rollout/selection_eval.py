"""Selection-set evaluation — scoring a candidate skill on the validation set.

This module answers a single question the optimizers and the tree both need to
ask: *given a candidate skill document, how good is it?* It does so by running
the frozen task agent over the held-out selection (validation) set and reducing
the K rollouts per task to one comparable scalar.

Two entry points:
  * :func:`selection_set_rollout` — the raw evaluator. Runs ``k_rollouts`` per
    validation item with a fixed ``skill_text`` and returns ``task_hard``: the
    fraction of *tasks* whose majority of rollouts passed. ``task_hard`` (not the
    per-rollout ``hard``) is the fair node-comparison metric (see
    :func:`css.data.rollout.aggregate_scores`), because it weights every task
    equally regardless of how noisy its K rollouts were.
  * :func:`evaluate_candidate` — the optimizer-facing wrapper. It splices a
    candidate ``rules`` body onto a node's existing ``strategy`` into a transient
    :class:`~css.skill_document.SkillDocument`, renders the exact text the agent
    would see via :meth:`SkillDocument.combined_skill_text`, and scores it.

Both functions receive ONLY a target-capable client. The frozen task agent must
be call-isolated from the optimizer (design First Law): the optimizer never
appears on this code path, so a candidate cannot be evaluated by the model that
proposed it.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from css.data.rollout import aggregate_scores
from css.rollout.batch import grouped_batch_rollout
from css.skill_document import SkillDocument

if TYPE_CHECKING:  # avoid runtime coupling / heavy imports
    from css.config import CSSConfig
    from css.data.rollout import TaskResult
    from css.data.tree import TreeNode
    from css.envs.spreadsheetbench.task_interface import TaskEnv
    from css.model.client import LLMClient


def selection_set_rollout(
    env: "TaskEnv",
    candidate_doc: "SkillDocument",
    val_items: list[dict],
    target_client: "LLMClient",
    *,
    k_rollouts: int,
    out_dir: str,
    max_workers: int = 32,
    task_timeout: int = 600,
) -> float:
    """Score a candidate skill document on the selection (validation) set.

    Runs ``k_rollouts`` rollouts per validation item with the candidate's
    ``combined_skill_text()`` injected as the agent's skill content, then reduces
    to the fair node-comparison metric ``task_hard`` in ``[0, 1]``.

    Args:
        env: The task environment providing ``run_one`` (target-only path).
        candidate_doc: The skill document to evaluate. Only its rendered
            ``combined_skill_text()`` reaches the agent.
        val_items: The selection-set task items to roll out.
        target_client: A target-capable client; the optimizer never appears here.
        k_rollouts: Rollouts per task (K, default 3 per design D5).
        out_dir: Directory for per-task rollout artifacts (predictions, etc.).
        max_workers: Parallel rollout workers.
        task_timeout: Per-task timeout in seconds.

    Returns:
        ``task_hard`` — fraction of tasks whose majority of K rollouts passed.
    """
    skill_text = candidate_doc.combined_skill_text()
    groups = grouped_batch_rollout(
        env,
        val_items,
        skill_text,
        target_client,
        k_rollouts=k_rollouts,
        out_dir=out_dir,
        max_workers=max_workers,
        task_timeout=task_timeout,
    )
    # Flatten the grouped rollouts back to a per-rollout list so aggregate_scores
    # can compute task_hard (it re-groups internally).
    flat_results: list["TaskResult"] = [r for g in groups for r in g.rollouts]
    scores = aggregate_scores(flat_results)
    return float(scores["task_hard"])


def evaluate_candidate(
    env: "TaskEnv",
    node: "TreeNode",
    candidate_rules: str,
    val_items: list[dict],
    target_client: "LLMClient",
    cfg: "CSSConfig",
    out_dir: str,
) -> float:
    """Evaluate a candidate L0 ``rules`` body against the node's strategy.

    Builds a transient :class:`~css.skill_document.SkillDocument` that pairs the
    node's frozen ``strategy`` (L1, unchanged) with the proposed ``candidate_rules``
    (L0), then scores it on the selection set. This is the acceptance gate the L0
    EXPLOITATION optimizer queries: a candidate is kept only if its ``task_hard``
    improves on the node's incumbent.

    The transient document is bound to ``out_dir`` but is never persisted here
    (no ``save()`` call); ``skill_dir`` only matters for path-derived behavior in
    the env, and the candidate must not overwrite the node's on-disk files.

    Args:
        env: The task environment.
        node: The tree node whose strategy is held fixed.
        candidate_rules: The proposed rules.md body to test.
        val_items: The selection-set task items.
        target_client: A target-capable client (First Law isolation).
        cfg: Run config; supplies ``k_rollouts``, ``max_api_workers``,
            ``task_timeout_s``.
        out_dir: Directory for this candidate's rollout artifacts. **Callers MUST
            pass a UNIQUE out_dir per candidate** — the SpreadsheetBench env skips
            re-execution when ``predictions/<task_id>/r<idx>/<no>_pred.xlsx`` already
            exists (a resume optimization), so reusing an out_dir across different
            ``candidate_rules`` would silently score the previous candidate's stale
            predictions. Namespace by L0 step / candidate hash.

    Returns:
        ``task_hard`` in ``[0, 1]`` for the candidate document.
    """
    candidate_doc = SkillDocument(
        skill_dir=out_dir,
        strategy=node.strategy,
        rules=candidate_rules,
    )
    return selection_set_rollout(
        env,
        candidate_doc,
        val_items,
        target_client,
        k_rollouts=cfg.k_rollouts,
        out_dir=out_dir,
        max_workers=cfg.max_api_workers,
        task_timeout=cfg.task_timeout_s,
    )
