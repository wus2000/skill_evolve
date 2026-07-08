"""BFCL multi-turn ``TaskEnv`` — native function-calling over simulated backends.

Split design (agreed 2026-07-05, see docs/env_prep/bfcl_PREP.md §11 + tools/
make_bfcl_split.py): a SCENARIO-INDEX split (categories mirror by index, so a
row-level split would leak), domain-stratified 120/40/40 indices -> train 480 /
val 160 / test 160 (every challenge type in every split, zero scenario leakage).
The composite-200 set is a SEALED probe: only its entries on the TEST indices
(~40) are exposed, via ``composite_probe_items`` — reporting-only, mirrors
AppWorld's test_challenge discipline (never in config paths).

Env-specific knobs (``cfg.extra``, all optional):
  bfcl_max_tokens         per-step completion cap (default 1024)
  bfcl_max_steps_per_turn per-turn tool-calling step cap (default 20 = official)
  bfcl_gt_mode            eval-annotation GT: "gold_calls" (default — per-turn
                          gold call sequences) | "none" (path-free ablation)

Execution substrate: IN-PROCESS. Each ``run_one`` builds its OWN fresh backend
instances (css/envs/bfcl/checker.make_instances) — never the upstream
``globals()`` registry, which cross-contaminates concurrent rollouts of the same
entry (probe: 1/8; test_bfcl_checker.py locks isolation). No worker pool needed.

GROUND-TRUTH FIREWALL: gold call sequences live only in the post-rollout
annotation (optimizer-only); the agent sees tools + scripted user turns only.
"""
from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

from css.envs import common
from css.envs.bfcl.agent import run_bfcl_agent
from css.envs.bfcl.prompts import ACTION_SPACE_DESCRIPTION
from css.trajectory import eval_annotation_message

if TYPE_CHECKING:
    from css.config import CSSConfig
    from css.data.rollout import TaskResult
    from css.model.client import LLMClient

_ACTIVE_CATEGORIES = ("base", "miss_func", "miss_param", "long_context")


class BfclEnv:
    """Concrete :class:`css.envs.base.TaskEnv` for BFCL multi-turn."""

    def __init__(
        self,
        cfg: "CSSConfig",
        *,
        split_dir: str = "",
        data_root: str = "",
        items: "dict | list | None" = None,
    ) -> None:
        self.cfg = cfg
        self.split_dir = split_dir or cfg.split_dir
        self.data_root = data_root or cfg.data_root
        self._items = common.resolve_items(items)
        extra = getattr(cfg, "extra", {}) or {}
        # Rollout sampling temperature is a single agreed value owned by the
        # client (user ruling 2026-07-08); no per-env override exists.
        self.max_tokens = int(extra.get("bfcl_max_tokens", 1024))
        self.max_steps_per_turn = int(extra.get("bfcl_max_steps_per_turn", 20))
        self.gt_mode = str(extra.get("bfcl_gt_mode", "gold_calls"))

    # ── Split accessors ────────────────────────────────────────────────────
    def _load_split_file(self, name: str) -> "list[dict]":
        path = os.path.join(self.split_dir, name, "items.json")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return list(data) if isinstance(data, list) else []

    def _split(self, name: str) -> "list[dict]":
        items = list(self._items.get(name, [])) if self._items is not None \
            else self._load_split_file(name)
        return common.slice_split(items, self.cfg, name)

    def train_items(self) -> "list[dict]":
        return self._split("train")

    def val_items(self) -> "list[dict]":
        return self._split("val")

    def test_items(self) -> "list[dict]":
        """Mechanism reporting split: 160 entries (40 idx x 4 categories)."""
        return self._split("test")

    def composite_probe_items(self) -> "list[dict]":
        """Sealed composite probe (~40) on the TEST indices — reporting-only,
        never sliced, never in a gate/selection path."""
        if self._items is not None:
            return list(self._items.get("composite_probe", []))
        return self._load_split_file("composite_probe")

    # ── Optional reporting hooks (see css/envs/base.py) ────────────────────
    def eval_splits(self) -> "list[tuple[str, list[dict]]]":
        """test first (primary: its task_hard is the mechanism's test_score),
        then the sealed composite probe (agreed 2026-07-05: reporting-only)."""
        return [
            ("test", self.test_items()),
            ("composite_probe", self.composite_probe_items()),
        ]

    def extra_metrics(self, results: "list") -> "dict[str, float]":
        """Per-category accuracy (base / miss_func / miss_param / long_context /
        composite) + overall, computed over a split's flat rollout results.

        Accuracy = mean ``hard`` over rollouts (matches the mechanism's task_hard
        convention at K rollouts). Partitions by the ``category`` extra so it is
        correct whether called per-split or on a mixed list.
        """
        by_cat: "dict[str, list[int]]" = {}
        for r in results:
            cat = str((getattr(r, "extras", None) or {}).get("category", "") or "other")
            by_cat.setdefault(cat, []).append(int(getattr(r, "hard", 0)))
        out: "dict[str, float]" = {}
        for cat, hs in by_cat.items():
            if hs:
                out["acc_%s" % cat] = sum(hs) / len(hs)
        allh = [h for hs in by_cat.values() for h in hs]
        if allh:
            out["acc_overall"] = sum(allh) / len(allh)
        return out

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
        deadline = max(60.0, float(getattr(self.cfg, "task_timeout_s", 600)) - 60.0)

        result = run_bfcl_agent(
            target_client, item, skill_text,
            max_steps_per_turn=self.max_steps_per_turn,
      max_tokens=self.max_tokens, 
            deadline_s=deadline,
        )

        # Post-rollout eval annotation (optimizer-only; the agent never saw it).
        conversation = result.pop("conversation")
        success = bool(result["hard"])
        conversation.append(eval_annotation_message(
            outcome="pass=%s (%s)" % (
                success, "all turns passed" if success else result["fail_reason"]),
            ground_truth=self._gold_reference(item),
            detail=self._annotation_detail(result),
        ))

        result.update({
            "id": task_id,
            "task_type": str(item.get("category", "")),   # per-category breakdowns
            "task_description": _first_user(item),          # agent-visible; NEVER gold
            "conversation": conversation,
            "skill_hash": common.skill_hash(skill_text),
        })
        return common.persist_result(
            result, pred_dir,
            rollout_index=rollout_index, epoch=epoch, node_id=node_id,
        )

    # ── Annotation composition (env policy; mechanism defines only the slot) ─
    def _gold_reference(self, item: dict) -> str:
        """Per-turn gold call sequences per ``bfcl_gt_mode`` ("gold_calls" | "none")."""
        if self.gt_mode != "gold_calls":
            return ""
        gt = item.get("ground_truth") or []
        if not gt:
            return ""
        lines = ["Gold call sequence (a reference solution, one line per user turn):"]
        for t, calls in enumerate(gt):
            body = "; ".join(calls) if calls else "(no call — the correct move is to hold off / ask)"
            lines.append("  turn %d: %s" % (t, body))
        return "\n".join(lines)

    def _annotation_detail(self, result: dict) -> str:
        detail = ("turns_passed=%s/%s, steps=%s, force_terminated=%s"
                  % (result.get("turns_passed"), result.get("gt_turns"),
                     result.get("n_steps"), result.get("force_terminated")))
        notes = result.get("refrain_notes") or []
        if notes:
            summary = "; ".join(
                "turn %d: %s" % (n["turn"], "acted" if n["emitted_call"] else "held/asked")
                for n in notes)
            detail += "\nRefrain checkpoints (empty-GT turns): " + summary
        return detail

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


def _first_user(item: dict) -> str:
    """The first scripted user message (agent-visible task framing; never gold)."""
    q = item.get("question") or []
    if q and q[0]:
        return str(q[0][0].get("content", ""))
    return ""
