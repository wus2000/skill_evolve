"""ALFWorld ``TaskEnv`` — text-mode ALFRED household tasks.

Split design (data/alfworld_split_seed42, tools/make_alfworld_split.py):
  train        3,153  official train minus the val carve
  val            400  carved from train, stratified by task_type (gate set)
  test         = test_unseen 134 (community-standard OOD eval; the split the
                 mechanism's round-end test eval uses)
  test_seen      140  extra in-domain eval, exposed via ``test_seen_items()``
                 for standalone/offline evals only (never touched by rounds).

Env-specific knobs (``cfg.extra``, all optional):
  alfworld_python         worker interpreter (REQUIRED on the server where the
                          css process runs a Python without textworld;
                          default: sys.executable)
  alfworld_max_steps      episode step cap (default 50, community standard)
  alfworld_temperature    agent sampling temperature (default 0.4)
  alfworld_max_tokens     per-call completion cap (default 16384)

Concurrency: each ``run_one`` isolates its episode in a worker subprocess
(css/envs/alfworld/worker.py) — the engine is not thread-safe. The css
process itself NEVER imports textworld/alfworld.
"""
from __future__ import annotations

import json
import os
import sys
from typing import TYPE_CHECKING

from css.envs import common
from css.envs.alfworld.agent import run_alfworld_agent
from css.envs.alfworld.prompts import ACTION_SPACE_DESCRIPTION
from css.trajectory import eval_annotation_message

if TYPE_CHECKING:
    from css.config import CSSConfig
    from css.data.rollout import TaskResult
    from css.model.client import LLMClient

_SPLIT_DIRNAMES = {"train": "train", "val": "val", "test": "test_unseen"}


class AlfworldEnv:
    """Concrete :class:`css.envs.base.TaskEnv` for ALFWorld text mode."""

    def __init__(
        self,
        cfg: "CSSConfig",
        *,
        split_dir: str = "",
        data_root: str = "",
        items: dict | list | None = None,
    ) -> None:
        self.cfg = cfg
        self.split_dir = split_dir or cfg.split_dir
        # data_root == $ALFWORLD_DATA (contains json_2.1.1/ and logic/).
        self.data_root = data_root or cfg.data_root
        self._items = common.resolve_items(items)
        extra = getattr(cfg, "extra", {}) or {}
        self.python_exe = str(extra.get("alfworld_python", "") or sys.executable)
        self.max_steps = int(extra.get("alfworld_max_steps", 50))
        self.temperature = float(extra.get("alfworld_temperature", 0.4))
        self.max_tokens = int(extra.get("alfworld_max_tokens", 16384))

    # ── Split accessors ────────────────────────────────────────────────────
    def _load_split_file(self, dirname: str) -> list[dict]:
        path = os.path.join(self.split_dir, dirname, "items.json")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return list(data) if isinstance(data, list) else []

    def _split(self, split: str) -> list[dict]:
        if self._items is not None:
            items = list(self._items.get(split, []))
        else:
            items = self._load_split_file(_SPLIT_DIRNAMES[split])
        return common.slice_split(items, self.cfg, split)

    def train_items(self) -> list[dict]:
        return self._split("train")

    def val_items(self) -> list[dict]:
        return self._split("val")

    def test_items(self) -> list[dict]:
        """The community-standard OOD test split (valid_unseen, 134)."""
        return self._split("test")

    def test_seen_items(self) -> list[dict]:
        """In-domain eval split (valid_seen, 140) — offline/standalone use only.

        Not part of the ``TaskEnv`` contract; the mechanism never calls it.
        """
        return self._load_split_file("test_seen")

    # ── Action space (L1 paradigm-design context) ──────────────────────────
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
        gamefile = os.path.join(self.data_root, str(item.get("gamefile", "")))

        deadline = max(60.0, float(getattr(self.cfg, "task_timeout_s", 1800)) - 60.0)
        result = run_alfworld_agent(
            target_client,
            gamefile,
            skill_text,
            python_exe=self.python_exe,
            data_root=self.data_root,
            max_steps=self.max_steps,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            deadline_s=deadline,
        )

        # Post-rollout eval annotation (optimizer-only; the agent never saw it).
        conversation = result.pop("conversation")
        won = bool(result["hard"])
        detail = (
            "steps=%d, invalid_commands=%d, format_failures=%d"
            % (result["n_turns"], result["n_invalid"], result["n_parse_fail"])
        )
        if not won and result.get("final_admissible"):
            detail += "\nAdmissible commands at the final state (reference): " + \
                json.dumps(result["final_admissible"])
        conversation.append(eval_annotation_message(
            outcome="won=%s (%s)" % (won, "pass" if won else result["fail_reason"]),
            ground_truth=self._gold_plan(item),
            detail=detail,
        ))

        result.update({
            "id": task_id,
            "task_type": str(item.get("task_type", "")),
            "instruction_type": str(item.get("task_type", "")),
            "gamefile": str(item.get("gamefile", "")),
            "scene": str(item.get("scene", "")),
            "conversation": conversation,
            "skill_hash": common.skill_hash(skill_text),
        })
        return common.persist_result(
            result, pred_dir,
            rollout_index=rollout_index, epoch=epoch, node_id=node_id,
        )

    def _gold_plan(self, item: dict) -> str:
        """High-level gold plan from traj_data.json (optimizer-only reference).

        Best-effort: an unreadable file yields an empty reference rather than a
        failed rollout. The gold plan is the ALFRED planner's macro sequence,
        e.g. ``GotoLocation(fridge) -> PickupObject(bread) -> ...``.
        """
        try:
            traj_path = os.path.join(
                self.data_root,
                os.path.dirname(str(item.get("gamefile", ""))),
                "traj_data.json",
            )
            with open(traj_path, encoding="utf-8") as f:
                traj = json.load(f)
            steps = []
            for hp in traj.get("plan", {}).get("high_pddl", []):
                da = hp.get("discrete_action", {})
                action = str(da.get("action", ""))
                if action in ("", "NoOp"):
                    continue
                args = ",".join(str(a) for a in da.get("args", []) if a)
                steps.append("%s(%s)" % (action, args))
            return " -> ".join(steps)
        except Exception:  # noqa: BLE001
            return ""

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
