"""Shared plumbing for the materials subsystem (L1_tree_mechanism_design.md §2).

Everything the interpretation / mining / dossier / frontier / global stages
share lives here exactly once:

  * path helpers for the per-burst analysis tree and the node dossier;
  * atomic disk IO + resume-skip primitives (a stage writes its product BEFORE
    proceeding and skips itself if the product already exists);
  * the burst-trajectory loader (reads a burst's on-policy step rollouts back
    from disk — the materials pass runs post-burst and the ``BurstResult`` only
    carries the ``exploit_dir``, not the in-memory trajectories);
  * robust JSON extraction from noisy optimizer output;
  * :func:`run_json_stage` — the SINGLE optimizer-JSON call seam every stage
    funnels through, so a test can drive the whole pipeline by monkeypatching
    this one function and routing on its ``stage`` argument.

This module imports only leaf helpers (``css.tree_search`` layout helpers,
``css.model.json_repair``, ``css.data.rollout``) and is imported by every other
materials module, so it must never import them back (no cycle).
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from css.model.json_repair import complete_optimizer_json
from css.tree_search import dossier_dir, node_dir

if TYPE_CHECKING:  # pragma: no cover - type-only
    from css.config import CSSConfig
    from css.data.rollout import TaskResult

_log = logging.getLogger("css.materials")


# ──────────────────────────────────────────────────────────────────────────
# Layout (design §2.1). Per-burst pipeline products live under
# nodes/<id>/analysis/burst_XXXX/; the node's living dossier under
# nodes/<id>/dossier/. Input trajectories come from the burst's exploit dir
# (nodes/<id>/burst_XXXX/exploit/), which the core loop already populated.
# ──────────────────────────────────────────────────────────────────────────
def analysis_dir(out_dir: str, node_id: str) -> str:
    return os.path.join(node_dir(out_dir, node_id), "analysis")


def analysis_burst_dir(out_dir: str, node_id: str, burst_index: int) -> str:
    return os.path.join(analysis_dir(out_dir, node_id), f"burst_{burst_index:04d}")


def interpretations_dir(out_dir: str, node_id: str, burst_index: int) -> str:
    return os.path.join(analysis_burst_dir(out_dir, node_id, burst_index),
                        "interpretations")


def group_analyses_dir(out_dir: str, node_id: str, burst_index: int) -> str:
    return os.path.join(analysis_burst_dir(out_dir, node_id, burst_index),
                        "group_analyses")


def global_unsolved_dir(out_dir: str) -> str:
    return os.path.join(out_dir, "global", "unsolved")


def dossier_path(out_dir: str, node_id: str, leaf: str) -> str:
    return os.path.join(dossier_dir(out_dir, node_id), leaf)


# ──────────────────────────────────────────────────────────────────────────
# Atomic IO + resume-skip
# ──────────────────────────────────────────────────────────────────────────
def write_json_atomic(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, path)


def write_text_atomic(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text or "")
    os.replace(tmp, path)


def append_jsonl(path: str, obj: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")


def read_json(path: str) -> Any:
    """Read a JSON file, or ``None`` on any failure (missing / corrupt)."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001 - a missing/corrupt product simply re-runs
        return None


def read_jsonl(path: str) -> list[dict]:
    out: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(d, dict):
                    out.append(d)
    except Exception:  # noqa: BLE001
        return out
    return out


def read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:  # noqa: BLE001
        return None


def exists(path: str) -> bool:
    return os.path.exists(path)


def safe_id(s: str) -> str:
    """Filesystem-safe rendering of an env task id (which may hold / or spaces)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(s))[:120] or "id"


# ──────────────────────────────────────────────────────────────────────────
# Burst trajectory loader
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class LoadedTraj:
    """One on-policy step rollout of a burst, loaded back from disk.

    ``step`` is the global L0 step ordinal (a task recurs across the burst's
    steps, so identity is (step, task_id, rollout_index) — not (task, k)).
    """

    traj_id: str
    task_id: str
    rollout_index: int
    step: int
    result: "TaskResult"

    @property
    def passed(self) -> bool:
        return bool(self.result.hard)


def _ord_suffix(name: str, prefix: str) -> int:
    m = re.search(r"(\d+)", name[len(prefix):] if name.startswith(prefix) else name)
    return int(m.group(1)) if m else -1


def load_burst_trajectories(exploit_dir: str) -> list[LoadedTraj]:
    """Load every on-policy step rollout of a burst as :class:`LoadedTraj`.

    Layout (written by ``run_exploitation_epoch`` / the shared env persister):
    ``<exploit_dir>/step<N>/rollout/predictions/<task_id>/r<k>/result.json``.
    Verification rollouts (``step<N>/verify/...``) are candidate-side and are
    NOT on-policy behavior — they are deliberately excluded.
    """
    from css.data.rollout import TaskResult

    out: list[LoadedTraj] = []
    if not exploit_dir or not os.path.isdir(exploit_dir):
        return out
    steps = sorted(
        (d for d in os.listdir(exploit_dir) if d.startswith("step")),
        key=lambda s: _ord_suffix(s, "step"),
    )
    for step_name in steps:
        step = _ord_suffix(step_name, "step")
        pred_root = os.path.join(exploit_dir, step_name, "rollout", "predictions")
        if not os.path.isdir(pred_root):
            continue
        for task_id in sorted(os.listdir(pred_root)):
            task_dir = os.path.join(pred_root, task_id)
            if not os.path.isdir(task_dir):
                continue
            for rdir in sorted(os.listdir(task_dir),
                               key=lambda s: _ord_suffix(s, "r")):
                d = read_json(os.path.join(task_dir, rdir, "result.json"))
                if not isinstance(d, dict):
                    continue
                try:
                    tr = TaskResult.from_dict(d)
                except Exception:  # noqa: BLE001
                    continue
                k = tr.rollout_index
                out.append(LoadedTraj(
                    traj_id=f"s{step}_{safe_id(task_id)}_r{k}",
                    task_id=tr.task_id or task_id,
                    rollout_index=k,
                    step=step,
                    result=tr,
                ))
    return out


def cap_trajectories(trajs: list[LoadedTraj], cfg: "CSSConfig") -> list[LoadedTraj]:
    """Apply ``cfg.interp_max_per_burst`` (0 = interpret ALL — the user default).

    When capping, keep a task-spread deterministic prefix (sorted by task then
    step then rollout) rather than a single step's worth, so the cap does not
    collapse onto the earliest step. Reserved de-homogenization knob (design §2.3).
    """
    cap = int(getattr(cfg, "interp_max_per_burst", 0) or 0)
    if cap <= 0 or len(trajs) <= cap:
        return trajs
    ordered = sorted(trajs, key=lambda t: (t.task_id, t.step, t.rollout_index))
    return ordered[:cap]


# ──────────────────────────────────────────────────────────────────────────
# JSON extraction + the single optimizer-JSON call seam
# ──────────────────────────────────────────────────────────────────────────
def extract_json(text: str) -> Any:
    """Best-effort parse of a JSON value from noisy LLM output (object/array).

    Tries: fenced ```json blocks, the whole string, then the first bare object
    and the first bare array. Returns ``None`` if nothing parses.
    """
    if not text:
        return None
    candidates: list[str] = []
    for m in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.DOTALL):
        candidates.append(m.group(1).strip())
    candidates.append(text.strip())
    for pat in (r"\{.*\}", r"\[.*\]"):
        m = re.search(pat, text, re.DOTALL)
        if m:
            candidates.append(m.group(0))
    for cand in candidates:
        if not cand:
            continue
        try:
            return json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def parse_object(text: str) -> dict | None:
    obj = extract_json(text)
    return obj if isinstance(obj, dict) else None


def parse_list_field(field: str) -> Callable[[str], list | None]:
    """Parser for an object-wrapped list ``{"<field>": [...]}``.

    All list-shaped materials outputs are object-wrapped: the optimizer
    endpoint's ``response_format={"type":"json_object"}`` grammar forbids a
    top-level array (it silently flattens "output a JSON list" prompts to one
    element), so every stage that yields a list returns it under a named key.
    A bare top-level array is still tolerated as a fallback.
    """
    def _p(text: str) -> list | None:
        obj = extract_json(text)
        if isinstance(obj, dict):
            v = obj.get(field)
            return v if isinstance(v, list) else None
        if isinstance(obj, list):
            return obj
        return None
    return _p


MATERIALS_MAX_TOKENS = 16384  # unified stage-output cap (user ruling 2026-07-06)


def run_json_stage(
    client: Any,
    system: str,
    user: str,
    *,
    parse: Callable[[str], Any],
    stage: str,
    cfg: "CSSConfig | None" = None,
    required: Callable[[Any], list] | None = None,
    ok: Callable[[Any], bool] | None = None,
    max_tokens: "int | None" = None,
) -> Any:
    """The one optimizer-JSON call every materials stage funnels through.

    ``max_tokens=None`` (the norm) resolves to the ONE unified materials cap:
    ``cfg.materials_max_tokens`` if set, else ``MATERIALS_MAX_TOKENS`` (16384;
    user ruling 2026-07-06 — per-stage hardcoded caps are gone). Input sizes
    are bounded upstream (interpret: tool_trunc per observation; mining:
    materials_group_chunk=12 narratives per call), so 16K output keeps every
    stage well inside the 256K context window.

    A thin pass-through to :func:`css.model.json_repair.complete_optimizer_json`
    (so production keeps context-aware repair). Tests monkeypatch THIS function
    and route on ``stage`` to script the whole pipeline without a network.
    """
    if max_tokens is None:
        max_tokens = int(getattr(cfg, "materials_max_tokens", 0) or MATERIALS_MAX_TOKENS)
    del cfg  # accepted for a uniform call shape / future budget hooks
    return complete_optimizer_json(
        client, system, user, parse=parse, ok=ok, required=required,
        max_tokens=max_tokens, stage=stage,
    )
