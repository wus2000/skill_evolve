"""ScienceWorld ``TaskEnv`` — dual-protocol elementary-science agent tasks.

Two evaluation protocols share one env (config approved 2026-07-04; see
docs/env_prep/scienceworld_ONBOARDING.md and the launcher):
  * ORIGINAL (train / val / secondary test): the agent optimizes and is
    secondary-reported on ScienceWorld's native 0-100 score.
  * AGENTBOARD (PRIMARY test): the 90-instance AgentBoard subset scored by latched
    regex subgoal matching → Success Rate (headline) + Progress Rate.
Both draw from the SAME 15 AgentBoard task types (the subset's measured
coverage), under the SAME unified AgentBoard simplification set (no ``easy``
preset anywhere — training with teleport while the primary test lacks it would
teach skills that break).

Execution substrate: an IN-PROCESS JVM env pool (css/envs/scienceworld/pool.py) —
ScienceWorld's engine is already an isolated JVM per env and is importable under
the harness Python, so no subprocess-worker layer is needed. A per-episode
watchdog force-closes a wedged JVM (py4j has no read timeout) and the pool
recycles it.

Env-specific knobs (``cfg.extra``, all optional):
  scienceworld_step_limit_original    native-track step budget (default 50)
  scienceworld_step_limit_agentboard  AgentBoard-track budget (default 30, fidelity)
  scienceworld_simplification         unified simplification string (default the
                                      AgentBoard set — NOT the ``easy`` preset)
  scienceworld_max_tokens             per-turn completion cap (default 512)
  scienceworld_pool_size              max live JVMs (default cfg.max_api_workers)
  scienceworld_pool_recycle_episodes  recycle an env every N episodes (default 200)
  scienceworld_java_tool_options      JVM flags via JAVA_TOOL_OPTIONS (default -Xmx256m)
  scienceworld_gt_mode                eval-annotation GT: "live" (gold action
                                      sequence; offline file + live-gen fallback) |
                                      "checklist" (path-free goal-progress structure)
  scienceworld_gold_actions           offline gold-action lookup json (default
                                      <split_dir>/gold_actions.json)
"""
from __future__ import annotations

import json
import os
import threading
from typing import TYPE_CHECKING

from css.envs import common
from css.envs.scienceworld.agent import run_scienceworld_agent
from css.envs.scienceworld.pool import ScienceWorldPool
from css.envs.scienceworld.prompts import ACTION_SPACE_DESCRIPTION
from css.envs.scienceworld.scoring import parse_subgoals
from css.trajectory import eval_annotation_message

if TYPE_CHECKING:
    from css.config import CSSConfig
    from css.data.rollout import TaskResult
    from css.model.client import LLMClient

# split name -> manifest subdirectory
_SPLIT_DIRS = {
    "train": "train",
    "val": "val",
    "test": "test_agentboard",        # PRIMARY test = AgentBoard subset
    "test_secondary": "test_secondary",
}
_AGENTBOARD_SIMPL = "selfWateringFlowerPots,openContainers,openDoors,noElectricalAction"


class ScienceworldEnv:
    """Concrete :class:`css.envs.base.TaskEnv` for ScienceWorld."""

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
        self._items = _resolve_items(items)
        extra = getattr(cfg, "extra", {}) or {}
        self.step_limit_original = int(extra.get("scienceworld_step_limit_original", 50))
        self.step_limit_agentboard = int(extra.get("scienceworld_step_limit_agentboard", 30))
        self.simplification = str(extra.get("scienceworld_simplification", _AGENTBOARD_SIMPL))
        # Rollout sampling temperature is a single agreed value owned by the
        # client (user ruling 2026-07-08); no per-env override exists.
        self.max_tokens = int(extra.get("scienceworld_max_tokens", 512))
        self.gt_mode = str(extra.get("scienceworld_gt_mode", "live"))
        pool_size = int(extra.get("scienceworld_pool_size", getattr(cfg, "max_api_workers", 128)))
        recycle = int(extra.get("scienceworld_pool_recycle_episodes", 200))
        java_opts = str(extra.get("scienceworld_java_tool_options", "-Xmx256m"))
        # SW envStepLimit is a simulator-side cap; keep it above the agent budgets
        # so the AGENT's per-protocol budget always binds first.
        pool_step_limit = max(self.step_limit_original, self.step_limit_agentboard) + 10
        self._pool = ScienceWorldPool(
            pool_size, pool_step_limit,
            recycle_episodes=recycle, java_tool_options=java_opts)
        # Offline gold-action lookup (optional accelerator produced by the split
        # generator); live generation fills any miss (e.g. measure-melting-unknown).
        gold_path = str(extra.get("scienceworld_gold_actions", "")) \
            or os.path.join(self.split_dir, "gold_actions.json")
        self._gold = _load_gold_actions(gold_path)
        self._gold_memo: "dict[str, str]" = {}
        self._gold_lock = threading.Lock()

    # ── Split accessors ────────────────────────────────────────────────────
    def _load_split_file(self, name: str) -> "list[dict]":
        path = os.path.join(self.split_dir, _SPLIT_DIRS[name], "items.json")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return list(data) if isinstance(data, list) else []

    def _split(self, name: str) -> "list[dict]":
        if self._items is not None:
            items = list(self._items.get(name, []))
        else:
            items = self._load_split_file(name)
        # slice_split only knows train/val/test; the secondary split is a fixed
        # reporting sample and is never truncated by the size knobs.
        slice_key = name if name in ("train", "val", "test") else "test_secondary"
        return common.slice_split(items, self.cfg, slice_key)

    def train_items(self) -> "list[dict]":
        return self._split("train")

    def val_items(self) -> "list[dict]":
        return self._split("val")

    def test_items(self) -> "list[dict]":
        """PRIMARY test = the AgentBoard subset (its SR is the mechanism test_score)."""
        return self._split("test")

    def test_secondary_items(self) -> "list[dict]":
        """SECONDARY test = native 0-100 score on the same task universe."""
        return self._split("test_secondary")

    # ── Optional reporting hooks (see css/envs/base.py) ────────────────────
    def eval_splits(self) -> "list[tuple[str, list[dict]]]":
        """AgentBoard primary first (its task_hard = SR = the mechanism test_score),
        then the native secondary (reporting-only)."""
        return [
            ("agentboard", self.test_items()),
            ("native_secondary", self.test_secondary_items()),
        ]

    def extra_metrics(self, results: "list") -> "dict[str, float]":
        """SR + PR (with easy/hard breakdown) for the AgentBoard split; average
        native score for the secondary split. Partitions by result tag so it is
        correct whether called per-split or on a mixed list.

        Aggregation note (alignment audit, 2026-07-04): SR/PR here are MEANS
        over all K rollouts (matches the mechanism's task_hard convention).
        WorldEvolver reports best-of-5 instead; the comparison aggregation is
        deliberately left undecided for now (user call) — best-of-K can be
        recomputed OFFLINE from the persisted per-rollout results at paper
        time, no re-run needed.
        """
        ab = [r for r in results if _get_extra(r, "agentboard_sr") is not None]
        native = [r for r in results if _get_extra(r, "agentboard_sr") is None]
        out: "dict[str, float]" = {}
        if ab:
            out["SR"] = _mean(r.hard for r in ab)
            out["PR"] = _mean(r.soft for r in ab)
            for label in ("easy", "hard"):
                sub = [r for r in ab if _get_extra(r, "difficulty") == label]
                if sub:
                    out["SR_%s" % label] = _mean(r.hard for r in sub)
                    out["PR_%s" % label] = _mean(r.soft for r in sub)
        if native:
            out["avg_score"] = _mean(r.soft for r in native) * 100.0
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
        protocol = str(item.get("protocol", "original"))
        task_name = str(item.get("task_name", ""))
        variation = int(item.get("variation", 0))
        if protocol == "agentboard":
            budget = self.step_limit_agentboard
            goal = str(item.get("goal", ""))
            subgoals = parse_subgoals(item.get("subgoals"))
        else:
            budget = self.step_limit_original
            goal = ""
            subgoals = []

        deadline = max(60.0, float(getattr(self.cfg, "task_timeout_s", 1800)) - 60.0)
        pe = self._pool.acquire()
        broken = True
        # Hard backstop for a WEDGED py4j call (the agent's own soft deadline
        # cannot interrupt a blocked socket read). Fires only past the deadline.
        watchdog = threading.Timer(deadline + 60.0, pe.force_close)
        watchdog.daemon = True
        watchdog.start()
        try:
            result = run_scienceworld_agent(
                target_client, pe,
                task_name=task_name, variation=variation,
                simplification=self.simplification, step_budget=budget,
                protocol=protocol, goal=goal, subgoals=subgoals,
                skill_text=skill_text,
        max_tokens=self.max_tokens, 
                deadline_s=deadline,
            )
            broken = bool(result.get("engine_broken"))
            gold_ref = self._gold_reference(task_name, variation, None if broken else pe)
        finally:
            watchdog.cancel()
            self._pool.release(pe, broken=broken)

        # Post-rollout eval annotation (optimizer-only; the agent never saw it).
        conversation = result.pop("conversation")
        conversation.append(eval_annotation_message(
            outcome=_outcome_str(result, protocol),
            ground_truth=gold_ref,
            detail=_annotation_detail(result),
        ))

        result.update({
            "id": task_id,
            "task_type": task_name,          # science task family -> per-task breakdowns
            "task_description": goal,          # agent-visible goal (agentboard) or ""
            "protocol": protocol,
            "difficulty": str(item.get("difficulty", "")),
            "variation": variation,
            "conversation": conversation,
            "skill_hash": common.skill_hash(skill_text),
        })
        return common.persist_result(
            result, pred_dir,
            rollout_index=rollout_index, epoch=epoch, node_id=node_id,
        )

    # ── Gold reference (env policy; mechanism defines only the annotation slot) ─
    def _gold_reference(self, task_name: str, variation: int, pe) -> str:
        """Compose the optimizer-only GT reference per ``scienceworld_gt_mode``.

        "live": the gold action sequence (offline lookup, else live-generated on
        the pooled env, memoized per (task, variation)). "checklist": the native
        goal-progress structure (path-free ablation). Best-effort — never a
        rollout failure source."""
        if self.gt_mode == "checklist":
            if pe is None:
                return ""
            try:
                progress = pe.env.get_goal_progress()
            except Exception:  # noqa: BLE001
                return ""
            return ("Task goal structure (subgoal checklist — analysis only):\n%s"
                    % progress) if progress else ""

        key = "%s::%d" % (task_name, variation)
        with self._gold_lock:
            if key in self._gold_memo:
                return self._gold_memo[key]
        seq = self._gold.get(key)
        if seq is None and pe is not None:
            seq = self._live_gold(task_name, variation, pe)
        ref = ""
        if seq:
            ref = ("Gold action sequence (a reference path that solves this task):\n"
                   + "\n".join("%2d. %s" % (i, a) for i, a in enumerate(seq, 1)))
        with self._gold_lock:
            self._gold_memo[key] = ref
        return ref

    def _live_gold(self, task_name: str, variation: int, pe) -> "list[str] | None":
        """Live-generate the gold action sequence on the pooled env (best-effort).

        Re-loads the env with ``generateGoldPath=True`` AFTER the rollout (the env
        is about to be released), so the agent never saw it. Returns None on any
        failure (the env is then flagged broken so the pool recycles it)."""
        try:
            pe.env.load(task_name, int(variation), self.simplification,
                        generateGoldPath=True)
            seq = list(pe.env.get_gold_action_sequence())
            if seq and str(seq[0]).startswith("ERROR"):
                return None
            return seq
        except Exception:  # noqa: BLE001 — degrade to no gold; recycle the env
            pe.broken = True
            return None

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

    def close(self) -> None:
        """Tear down the JVM pool (idempotent; called at process end)."""
        self._pool.close_all()


# ── Module helpers ──────────────────────────────────────────────────────────
def _resolve_items(items: "dict | list | None") -> "dict[str, list[dict]] | None":
    """Normalize the constructor ``items`` (4-way: train/val/test/test_secondary)."""
    if items is None:
        return None
    if isinstance(items, dict):
        return {k: list(items.get(k, [])) for k in _SPLIT_DIRS}
    return {"train": list(items), "val": [], "test": [], "test_secondary": []}


def _load_gold_actions(path: str) -> "dict[str, list[str]]":
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return {str(k): list(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _get_extra(r, key: str):
    extras = getattr(r, "extras", None) or {}
    return extras.get(key)


def _mean(xs) -> float:
    xs = list(xs)
    return (sum(xs) / len(xs)) if xs else 0.0


def _outcome_str(result: dict, protocol: str) -> str:
    if protocol == "agentboard":
        done = result.get("agentboard_subgoals_done")
        total = result.get("agentboard_subgoals_total")
        return "SR=%d PR=%.2f (%s/%s subgoals) native_score=%s" % (
            int(result.get("hard", 0)), float(result.get("soft", 0.0)),
            done, total, result.get("native_score"))
    passed = bool(result.get("hard"))
    return "score=%s (%s)" % (
        result.get("native_score"), "solved" if passed else result.get("fail_reason", "unsolved"))


def _annotation_detail(result: dict) -> str:
    return (
        "turns=%s, invalid_actions=%s, format_failures=%s, check_valid_actions=%s"
        % (result.get("n_turns"), result.get("n_invalid"),
           result.get("n_parse_fail"), result.get("n_check_valid")))
