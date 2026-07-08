"""Shared, single-sourced helpers for implementing a concrete ``TaskEnv``.

Every convention the mechanism relies on but cannot enforce through the
:class:`css.envs.base.TaskEnv` protocol signature alone lives here, exactly
once:

  * :func:`slice_split`        — the ``n_train/n_val/n_test`` sizing convention
  * :func:`skill_hash`         — the rollout-cache validity key (must match
                                 ``css.rollout.batch._hash_skill``)
  * :func:`prediction_dir`     — the canonical per-(task, rollout) artifact dir
  * :func:`persist_result`     — stamp provenance + write ``result.json`` +
                                 build the :class:`TaskResult`
  * :func:`load_cached_result` — the resume fast-path loader (same-skill check)

New environments should build on these instead of re-implementing them; the
template env (``css/envs/template``) shows the intended usage. The two
original envs (bird / spreadsheetbench) predate this module and carry local
copies — behaviorally identical, kept untouched for run stability.

EXECUTION-SUBSTRATE MENU. The mechanism's batch orchestration (scheduling,
caching, timeouts, resume, tracing) is env-agnostic and never overridden;
what varies per environment is the execution substrate inside one rollout
slot, in increasing isolation:

  (a) in-thread              — engine is thread-safe (Bird, SpreadsheetBench)
  (b) per-episode worker     — engine not thread-safe / needs another
                               interpreter / monkey-patches process globals
                               (ALFWorld, AppWorld) -> ``.subprocess_worker``
  (c) persistent worker pool — (b) with amortized interpreter startup
  (d) shared server + per-slot session — one heavyweight in-process resource
                               shared read-only across slots (WebShop-style)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import TYPE_CHECKING, Any  # noqa: F401 — Any kept for env imports

from css.data.rollout import TaskResult

_log = logging.getLogger("css.envs")

if TYPE_CHECKING:
    from css.config import CSSConfig


def skill_hash(skill_text: str) -> str:
    """Rollout-cache validity key for a skill text.

    MUST stay byte-compatible with ``css.rollout.batch._hash_skill`` — the
    batch layer computes the hash it passes to ``load_cached_result`` with
    that function, and a cached ``result.json`` is only reused when the
    stored hash matches.
    """
    return hashlib.sha256((skill_text or "").encode("utf-8")).hexdigest()[:16]


def item_id(item: dict) -> str:
    """Canonical task-id extraction from an item dict.

    The mechanism identifies tasks by ``item["id"]`` (fallback
    ``item["task_id"]``). Every item an env returns from its split accessors
    MUST carry a non-empty, unique id under one of these keys.
    """
    return str(item.get("id", item.get("task_id", "")))


def slice_split(items: list[dict], cfg: "CSSConfig", split: str) -> list[dict]:
    """Apply the ``n_train / n_val / n_test`` sizing convention to a split.

    ``0`` or a negative knob means "use the whole split"; a positive knob
    truncates (the split files are pre-shuffled, so a prefix is a sample).
    """
    limit = {
        "train": getattr(cfg, "n_train", 0),
        "val": getattr(cfg, "n_val", 0),
        "test": getattr(cfg, "n_test", 0),
    }.get(split, 0)
    if isinstance(limit, int) and limit > 0:
        return list(items[:limit])
    return list(items)


def prediction_dir(out_dir: str, task_id: str, rollout_index: int) -> str:
    """Canonical artifact directory for one (task, rollout) execution.

    Layout: ``<out_dir>/predictions/<task_id>/r<rollout_index>/``. The batch
    layer's resume cache and several observability tools assume this shape;
    do not deviate.
    """
    return os.path.join(out_dir, "predictions", task_id, f"r{rollout_index}")


def persist_result(
    result: dict,
    pred_dir: str,
    *,
    rollout_index: int,
    epoch: int,
    node_id: str,
) -> TaskResult:
    """Stamp provenance, persist ``result.json``, and build the ``TaskResult``.

    ``result`` is the env's raw result dict. It must already contain the
    generic outcome fields (``hard``, ``soft``, ``conversation`` or
    ``messages``, ``skill_hash``, ...); any extra keys are absorbed into
    ``TaskResult.extras`` automatically. Persistence is best-effort — a write
    failure never fails the rollout.
    """
    result["rollout_index"] = rollout_index
    result["epoch"] = epoch
    result["node_id"] = node_id
    try:
        os.makedirs(pred_dir, exist_ok=True)
        with open(os.path.join(pred_dir, "result.json"), "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    except Exception:  # noqa: BLE001 - persistence is best-effort
        pass
    return TaskResult.from_dict(result)


def load_cached_result(
    item: dict,
    out_dir: str,
    *,
    rollout_index: int,
    skill_hash: str,
) -> "TaskResult | None":
    """The resume fast-path: reload a same-skill rollout from disk, or ``None``.

    Called by ``css.rollout.batch`` before every ``run_one``. Returns a
    reconstructed :class:`TaskResult` only when a ``result.json`` exists for
    this (task, rollout_index) AND it was produced under the SAME skill text
    (``skill_hash`` match — a changed skill invalidates the cache). Any error
    degrades to ``None`` (re-roll); the cache is never a failure source.

    A cache entry that recorded turns but carries no trajectory is INCOMPLETE
    and is rejected (re-roll) rather than substituted: reusing it would feed an
    empty trajectory to every downstream analysis layer. This is the shape a
    pre-2026-07-08 SpreadsheetBench ``result.json`` has (the conversation used
    to be stripped out into a sibling file); such runs re-roll instead of
    silently poisoning L1.
    """
    tid = item_id(item)
    if not tid:
        return None
    result_path = os.path.join(
        prediction_dir(out_dir, tid, rollout_index), "result.json"
    )
    if not os.path.exists(result_path):
        return None
    try:
        with open(result_path, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(d, dict) or d.get("skill_hash") != skill_hash:
        return None
    if int(d.get("n_turns") or 0) > 0 and not (
            d.get("messages") or d.get("conversation")):
        _log.warning(
            "envs/common — incomplete cache entry (n_turns=%s, no trajectory): "
            "%s — re-rolling", d.get("n_turns"), result_path)
        return None
    try:
        return TaskResult.from_dict(d)
    except Exception:  # noqa: BLE001
        return None


def resolve_items(
    items: "dict | list | None",
) -> "dict[str, list[dict]] | None":
    """Normalize an explicit ``items`` constructor argument.

    Accepts ``{"train": [...], "val": [...], "test": [...]}`` (missing splits
    default to empty) or a flat list (treated as the train split). ``None``
    means "load from disk via the env's own loader".
    """
    if items is None:
        return None
    if isinstance(items, dict):
        return {
            "train": list(items.get("train", [])),
            "val": list(items.get("val", [])),
            "test": list(items.get("test", [])),
        }
    return {"train": list(items), "val": [], "test": []}
