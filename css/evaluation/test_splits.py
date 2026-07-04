"""Reporting-time evaluation over env-declared test splits.

Shared by the cold-start bare baseline and the per-round best-skill test.
The env optionally declares ``eval_splits()`` (named splits; first is
primary) and ``extra_metrics(results)`` (env-specific aggregates such as
AppWorld's TGC/SGC). Mechanism semantics are unchanged: the PRIMARY split's
``task_hard`` is the ``test_score`` used everywhere; additional splits and
metrics are reported and persisted, never consumed by gates, selection, or
tree decisions.
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from css.data.rollout import aggregate_scores

if TYPE_CHECKING:
    from css.envs.base import TaskEnv
    from css.model.client import LLMClient

_log = logging.getLogger(__name__)


def env_eval_splits(env: "TaskEnv") -> "list[tuple[str, list[dict]]]":
    """The env's declared test splits, defaulting to [("test", test_items())]."""
    fn = getattr(env, "eval_splits", None)
    if callable(fn):
        try:
            splits = list(fn())
            if splits:
                return splits
        except Exception:
            _log.warning("eval_splits() failed; falling back to test_items",
                         exc_info=True)
    return [("test", list(env.test_items()))]


def env_extra_metrics(env: "TaskEnv", flat_results: list) -> "dict[str, float]":
    fn = getattr(env, "extra_metrics", None)
    if callable(fn):
        try:
            return dict(fn(flat_results) or {})
        except Exception:
            _log.warning("extra_metrics() failed; reporting task_hard only",
                         exc_info=True)
    return {}


def _fmt_extra(extra: "dict[str, float]") -> str:
    if not extra:
        return ""
    return "  " + "  ".join(
        "%s=%.4f" % (k, v) for k, v in sorted(extra.items()))


def evaluate_test_splits(
    env: "TaskEnv",
    skill_text: str,
    target_client: "LLMClient",
    cfg: Any,
    out_dir: str,
    *,
    epoch: int,
    node_id: str,
    label: str,
) -> "dict[str, Any]":
    """Roll out every declared split; return a per-split report dict.

    Returns {split_name: {"score": task_hard, "extra": {...}, "groups": [...],
    "n_items": int}} with the primary split FIRST (dict order preserved).
    Prediction caching inside grouped_batch_rollout makes re-entry after a
    resume cheap (cache hits by skill_hash + task + rollout).
    """
    from css.rollout.batch import grouped_batch_rollout

    test_k = getattr(cfg, "test_k_rollouts", 1) or 1
    report: "dict[str, Any]" = {}
    for name, items in env_eval_splits(env):
        split_dir = os.path.join(out_dir, name)
        groups = grouped_batch_rollout(
            env,
            list(items),
            skill_text,
            target_client,
            k_rollouts=test_k,
            out_dir=split_dir,
            max_workers=cfg.max_api_workers,
            task_timeout=cfg.task_timeout_s,
            epoch=epoch,
            node_id=node_id,
        )
        flat = [r for g in groups for r in g.rollouts]
        score = float(aggregate_scores(flat).get("task_hard", 0.0))
        extra = env_extra_metrics(env, flat)
        report[name] = {
            "score": score,
            "extra": extra,
            "groups": groups,
            "n_items": len(items),
            "n_passed": sum(1 for r in flat if r.passed),
            "n_rollouts": len(flat),
        }
        _log.info(
            "%s [%s] task_hard=%.4f (%d/%d passed, %d tasks)%s",
            label, name, score, report[name]["n_passed"], len(flat),
            len(items), _fmt_extra(extra))
    return report
