"""AppWorld ``TaskEnv`` — interactive coding tasks over simulated apps.

Split design (agreed 2026-07-04, canonical four-way — community-comparable):
  train            90  official train (the optimization pool)
  val            = dev 57 (the paired-gate set; dev's intended model-selection use)
  test           = test_normal 168 (the mechanism's reporting split)
  test_challenge  417  NEVER touched by the mechanism; exposed only via
                  ``test_challenge_items()`` for the standalone eval script
                  (unsealed twice: bare baseline + final best skill).

Env-specific knobs (``cfg.extra``, all optional):
  appworld_python           worker interpreter (REQUIRED on the server: the css
                            process python is 3.8, appworld needs >= 3.11)
  appworld_root             APPWORLD_ROOT data checkout (default: cfg.data_root)
  appworld_max_interactions episode interaction cap (default 50 — the official
                            minimal-ReAct notebook value, = our ALFWorld cap)
  appworld_temperature      agent sampling temperature (default 0.4)
  appworld_max_tokens       per-call completion cap (default 4096)
  appworld_obs_max_chars    per-turn output truncation (default 6000 —
                            PROVISIONAL, to be calibrated from live runs)
  appworld_engine_slots     max concurrent worker processes (default 128;
                            RAM-bound: each loaded world ~300-500MB)
  appworld_gt_mode          eval-annotation ground-truth richness (the ablation
                            knob; annotation content is env policy):
                              "solution" — failed/passed requirement report +
                                           the gold solution code (train/dev)
                              "tests"    — the requirement report only (a true
                                           path-free GT ablation)

Concurrency: each ``run_one`` isolates its episode in a worker subprocess
(css/envs/appworld/worker.py) — AppWorld's unified mode allows ONE world per
OS process (freezegun patches time process-wide). The css process itself
NEVER imports appworld. ``EngineSlotLimiter`` caps live workers globally,
including across concurrent batch_rollout calls (paired gate sides).
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import TYPE_CHECKING

from css.envs import common
from css.envs.appworld.agent import run_appworld_agent
from css.envs.appworld.prompts import ACTION_SPACE_DESCRIPTION
from css.envs.common.subprocess_worker import EngineSlotLimiter
from css.trajectory import eval_annotation_message

if TYPE_CHECKING:
    from css.config import CSSConfig
    from css.data.rollout import TaskResult
    from css.model.client import LLMClient

_SPLIT_FILES = {
    "train": "train.txt",
    "val": "dev.txt",
    "test": "test_normal.txt",
    "test_challenge": "test_challenge.txt",
}
_GOLD_SPLITS = ("train", "val")  # ground truth exists for train/dev only

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


class AppworldEnv:
    """Concrete :class:`css.envs.base.TaskEnv` for AppWorld."""

    def __init__(
        self,
        cfg: "CSSConfig",
        *,
        split_dir: str = "",
        data_root: str = "",
        items: dict | list | None = None,
    ) -> None:
        self.cfg = cfg
        # data_root == $APPWORLD_ROOT (contains data/{datasets,tasks,...}).
        self.data_root = data_root or cfg.data_root
        self.split_dir = split_dir or os.path.join(
            self.data_root, "data", "datasets")
        self._items = common.resolve_items(items)
        extra = getattr(cfg, "extra", {}) or {}
        self.python_exe = str(extra.get("appworld_python", "") or sys.executable)
        self.appworld_root = str(extra.get("appworld_root", "") or self.data_root)
        self.max_interactions = int(extra.get("appworld_max_interactions", 50))
        self.temperature = float(extra.get("appworld_temperature", 0.4))
        self.max_tokens = int(extra.get("appworld_max_tokens", 4096))
        self.obs_max_chars = int(extra.get("appworld_obs_max_chars", 6000))
        self.gt_mode = str(extra.get("appworld_gt_mode", "solution"))
        self._slots = EngineSlotLimiter(int(extra.get("appworld_engine_slots", 128)))

    # ── Split accessors ────────────────────────────────────────────────────
    def _load_split_file(self, split: str) -> list[dict]:
        path = os.path.join(self.split_dir, _SPLIT_FILES[split])
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            ids = [line.strip() for line in f if line.strip()]
        return [{"id": tid, "split": split} for tid in ids]

    def _split(self, split: str) -> list[dict]:
        if self._items is not None:
            items = list(self._items.get(split, []))
        else:
            items = self._load_split_file(split)
        return common.slice_split(items, self.cfg, split)

    def train_items(self) -> list[dict]:
        return self._split("train")

    def val_items(self) -> list[dict]:
        return self._split("val")

    def test_items(self) -> list[dict]:
        """The mechanism's reporting split (= official test_normal, 168)."""
        return self._split("test")

    def test_challenge_items(self) -> list[dict]:
        """The sealed challenge split — standalone eval script use ONLY.

        Not part of the ``TaskEnv`` contract; the mechanism never calls it.
        """
        return self._load_split_file("test_challenge")

    # ── Action space (L1 paradigm-design / analysis context) ───────────────
    def action_space_description(self) -> str:
        return ACTION_SPACE_DESCRIPTION

    # ── Execution ──────────────────────────────────────────────────────────
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
    ) -> "TaskResult":
        task_id = common.item_id(item)
        pred_dir = common.prediction_dir(out_dir, task_id, rollout_index)
        split = str(item.get("split", ""))
        fetch_gold = self.gt_mode == "solution" and split in _GOLD_SPLITS
        # ground_truth_mode "full" only where gold exists (test refuses "full").
        gt_open_mode = "full" if split in _GOLD_SPLITS else "minimal"

        # Each (task, rollout) gets its own experiment dir — AppWorld keys its
        # per-run output state by experiment_name, and K rollouts of the same
        # task would collide on a shared name.
        experiment_name = _SAFE_NAME_RE.sub("_", "css_%s_e%03d_%s_r%d" % (
            node_id or "adhoc", max(epoch, 0), task_id, rollout_index))

        deadline = max(60.0, float(getattr(self.cfg, "task_timeout_s", 1800)) - 60.0)
        with self._slots.slot():
            result = run_appworld_agent(
                target_client,
                task_id,
                skill_text,
                python_exe=self.python_exe,
                appworld_root=self.appworld_root,
                experiment_name=experiment_name,
                ground_truth_mode=gt_open_mode,
                max_interactions=self.max_interactions,
                obs_max_chars=self.obs_max_chars,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                deadline_s=deadline,
                fetch_gold=fetch_gold,
            )

        # Post-rollout eval annotation (optimizer-only; the agent never saw it).
        conversation = result.pop("conversation")
        success = bool(result["hard"])
        conversation.append(eval_annotation_message(
            outcome="success=%s (%s)" % (
                success, "pass" if success else result["fail_reason"]),
            ground_truth=self._gold_reference(result),
            detail=self._annotation_detail(result),
        ))

        # task_type = difficulty bucket -> per-difficulty breakdowns for free
        # (mirrors ALFWorld's task-family stratification in the eval script).
        difficulty = (result.get("task_metadata") or {}).get("difficulty")
        result.update({
            "id": task_id,
            "task_type": ("difficulty_%s" % difficulty)
                         if difficulty not in (None, "") else "",
            "split": split,
            "conversation": conversation,
            "skill_hash": common.skill_hash(skill_text),
        })
        # gold text lives in the annotation; keep result.json lean.
        result.pop("gold_solution_code", None)
        result.pop("gold_answer", None)
        return common.persist_result(
            result, pred_dir,
            rollout_index=rollout_index, epoch=epoch, node_id=node_id,
        )

    # ── Annotation composition (env policy; mechanism defines only the slot) ─
    def _gold_reference(self, result: dict) -> str:
        """Gold reference per ``appworld_gt_mode`` ("solution" | "tests")."""
        if self.gt_mode != "solution":
            return ""
        code = str(result.get("gold_solution_code", "") or "")
        if not code:
            return ""
        parts = ["Gold solution code (reference program that passes this task):",
                 code.strip()]
        answer = result.get("gold_answer")
        if answer is not None:
            parts.append("Expected answer: %s" % json.dumps(answer, ensure_ascii=False))
        return "\n".join(parts)

    def _annotation_detail(self, result: dict) -> str:
        counters = (
            "turns=%d, parse_failures=%d, tracebacks=%d, declared=%s"
            % (result["n_turns"], result["n_parse_fail"],
               result["n_tracebacks"], result["declared"])
        )
        md = result.get("task_metadata") or {}
        if md:
            counters += "\nTask metadata: " + json.dumps(md, ensure_ascii=False)
        report = result.get("eval_report") or {}
        if report:
            text = json.dumps(report, ensure_ascii=False, indent=1)
            if len(text) > 4000:
                text = text[:4000] + "\n... [report truncated]"
            counters += "\nPer-requirement evaluation report:\n" + text
        return counters

    # ── Resume fast-path ───────────────────────────────────────────────────
    def load_cached_result(
        self,
        item: dict,
        out_dir: str,
        *,
        rollout_index: int = 0,
        skill_hash: str = "",
    ) -> "TaskResult | None":
        return common.load_cached_result(
            item, out_dir, rollout_index=rollout_index, skill_hash=skill_hash)
