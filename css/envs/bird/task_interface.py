"""Bird Text-to-SQL environment — a concrete :class:`css.envs.base.TaskEnv`.

``run_one`` runs the function-calling SQL agent (``css.envs.bird.agent``) on one
question under a skill document, scores it by BIRD Execution Accuracy, and
returns a :class:`~css.data.rollout.TaskResult` in the mechanism's canonical
shape. All Bird-specific concerns (the agent framework, its tools, its prompts,
SQL execution, schema rendering) live in this package; the mechanism sees only
the generic ``TaskEnv`` surface.

Ground-truth handling: the gold SQL is never shown to the agent (not in any
prompt, not in the trajectory). It is used only by the EX evaluator and is
carried in ``TaskResult.extras['gold_sql']`` for auditing.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import TYPE_CHECKING, Any

from css.data.rollout import TaskResult

if TYPE_CHECKING:
    from css.config import CSSConfig
    from css.model.client import LLMClient


def _skill_hash(skill_text: str) -> str:
    """Rollout-cache validity key — must match css.rollout.batch._hash_skill."""
    return hashlib.sha256((skill_text or "").encode("utf-8")).hexdigest()[:16]


class BirdEnv:
    """Concrete :class:`TaskEnv` for the BIRD Text-to-SQL benchmark.

    Items can be supplied two ways (checked in order):
      1. ``items`` - an explicit ``{"train": [...], "val": [...], "test": [...]}``
         mapping (or a flat list, treated as the train split). No data loading.
      2. ``split_dir`` / ``cfg.split_dir`` + ``db_root`` / ``cfg.data_root`` -
         loaded lazily via :class:`css.envs.bird.dataloader.BirdDataLoader`.
    """

    def __init__(
        self,
        cfg: "CSSConfig",
        *,
        db_root: str = "",
        split_dir: str = "",
        items: dict | None = None,
    ) -> None:
        self.cfg = cfg
        self.db_root = db_root or cfg.data_root
        self.split_dir = split_dir or cfg.split_dir
        if items is None:
            self._items: dict[str, list[dict]] | None = None
        elif isinstance(items, dict):
            self._items = {
                "train": list(items.get("train", [])),
                "val": list(items.get("val", [])),
                "test": list(items.get("test", [])),
            }
        else:
            self._items = {"train": list(items), "val": [], "test": []}
        self._loader: Any = None

    # ── Split accessors ──────────────────────────────────────────────────

    def _ensure_loader(self) -> Any:
        if self._loader is not None:
            return self._loader
        from css.envs.bird.dataloader import BirdDataLoader  # noqa: PLC0415

        self._loader = BirdDataLoader(self.split_dir, self.db_root)
        return self._loader

    def _split(self, split: str) -> list[dict]:
        if self._items is not None:
            items = list(self._items.get(split, []))
        else:
            items = list(self._ensure_loader().load(split))
        # Slice the pre-made split to the configured size (0 / negative = all),
        # so cfg.n_train/n_val/n_test control the Bird experiment size the same
        # way they do for SpreadsheetBench.
        limit = {
            "train": getattr(self.cfg, "n_train", 0),
            "val": getattr(self.cfg, "n_val", 0),
            "test": getattr(self.cfg, "n_test", 0),
        }.get(split, 0)
        if isinstance(limit, int) and limit > 0:
            items = items[:limit]
        return items

    def train_items(self) -> list[dict]:
        return self._split("train")

    def val_items(self) -> list[dict]:
        return self._split("val")

    def test_items(self) -> list[dict]:
        return self._split("test")

    # ── Rollout ──────────────────────────────────────────────────────────

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
    ) -> TaskResult:
        """Run one (question, rollout) via the SQL agent and score it by EX."""
        from css.envs.bird.agent import run_bird_agent  # noqa: PLC0415

        cfg = self.cfg
        extra = getattr(cfg, "extra", {}) or {}
        task_id = str(item.get("id", item.get("question_id", "")))
        question = str(item.get("question", ""))
        evidence = str(item.get("evidence", "") or "")
        # Task description for the optimizer's view — question (+ evidence); NEVER gold.
        task_description = question + (f" [Evidence: {evidence}]" if evidence else "")
        difficulty = str(item.get("difficulty", "") or "")

        prediction_dir = os.path.join(out_dir, "predictions", task_id, f"r{rollout_index}")
        os.makedirs(prediction_dir, exist_ok=True)

        agent_out = run_bird_agent(
            target_client,
            item,
            skill_text,
            max_turns=int(extra.get("bird_max_turns", 10)),
            exec_timeout=float(extra.get("bird_exec_timeout", 30.0)),
            max_tokens=int(extra.get("bird_max_tokens", 8192)),
            temperature=float(extra.get("bird_temperature", 0.0)),
        )

        hard = int(agent_out["hard"])
        result: dict[str, Any] = {
            "id": task_id,
            "task_description": task_description,
            "task_type": difficulty or "other",
            "hard": hard,
            "soft": float(agent_out["soft"]),
            "n_cases": 1,
            "n_pass": hard,
            "n_turns": int(agent_out["n_turns"]),
            "fail_reason": str(agent_out["fail_reason"]),
            "conversation": agent_out["conversation"],
            "skill_hash": _skill_hash(skill_text),
            # Env-specific overflow (absorbed into TaskResult.extras):
            "predicted_sql": agent_out["predicted_sql"],
            "gold_sql": agent_out["gold_sql"],
            "db_id": str(item.get("db_id", "")),
            "difficulty": difficulty,
        }
        return self._finalize(result, prediction_dir, rollout_index, epoch, node_id)

    @staticmethod
    def _finalize(
        result: dict,
        prediction_dir: str,
        rollout_index: int,
        epoch: int,
        node_id: str,
    ) -> TaskResult:
        """Stamp provenance, persist result.json (incl. conversation), build TaskResult."""
        result["rollout_index"] = rollout_index
        result["epoch"] = epoch
        result["node_id"] = node_id
        try:
            with open(os.path.join(prediction_dir, "result.json"), "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2, default=str)
        except Exception:  # noqa: BLE001 - persistence is best-effort
            pass
        return TaskResult.from_dict(result)

    def load_cached_result(
        self,
        item: dict,
        out_dir: str,
        *,
        rollout_index: int,
        skill_hash: str,
    ) -> "TaskResult | None":
        """Load a previously-computed rollout for resume, or ``None``.

        Returns a reconstructed :class:`TaskResult` only when a ``result.json``
        exists for this (task, rollout_index) AND it was produced under the SAME
        skill (``skill_hash`` match). Any error degrades to ``None`` (re-roll).
        """
        task_id = str(item.get("id", item.get("question_id", item.get("task_id", ""))))
        if not task_id:
            return None
        result_path = os.path.join(
            out_dir, "predictions", task_id, f"r{rollout_index}", "result.json"
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
        try:
            return TaskResult.from_dict(d)
        except Exception:  # noqa: BLE001
            return None
