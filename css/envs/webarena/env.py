"""WebArena task environment (Verified scoring, farm-scheduled episodes).

Wiring (PREP §7): items come from the sealed split manifests produced by
``tools/webarena_make_splits.py`` (Verified records: task_id / sites / intent
/ intent_template_id / start_urls / eval). ``run_one`` leases a (stack, site)
lane from the mutation-aware scheduler, runs one browser episode (agent.py),
scores OFFLINE via the WebArena-Verified evaluator, and persists through the
shared env conventions (css/envs/common). Ground-truth firewall: the record's
``eval`` block (expected answers) never reaches the agent — it exists only in
the scorer inputs and the post-hoc eval annotation.
"""
from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, Any

from css.envs import common
from css.envs.webarena.scheduler import MUTATING_TASK_TYPE, SiteLeaseManager
from css.envs.webarena.scoring import VerifiedScorer
from css.trajectory import eval_annotation_message

if TYPE_CHECKING:  # pragma: no cover
    from css.config import CSSConfig

_log = logging.getLogger("css.webarena")


def _build_refresh_fn(extra: dict):
    """Lane-refresh hook from config: a shell template with {stack}/{site}.

    Production value (agreed topology): an ssh into the farm host running
    ``farm.sh refresh <stack> <site>`` — recreates ONE site container from its
    golden image. Empty/missing template -> no-op refresh (single-stack P0).
    Blocking by design: the scheduler refreshes lazily, right before granting
    the next mutating lease on a dirty lane.
    """
    template = str(extra.get("webarena_refresh_cmd", "") or "")
    timeout_s = int(extra.get("webarena_refresh_timeout_s", 300))
    if not template:
        return None

    def refresh(stack: str, site: str) -> None:
        import subprocess
        cmd = template.format(stack=stack, site=site)
        proc = subprocess.run(cmd, shell=True, capture_output=True,
                              text=True, timeout=timeout_s)
        if proc.returncode != 0:
            raise RuntimeError(
                f"refresh {stack}/{site} rc={proc.returncode}: "
                f"{(proc.stderr or proc.stdout)[-300:]}")
        # farm.sh prints readiness telemetry ("ready <site> after Ns");
        # surface it so refresh cost stays visible in the run log.
        tail = (proc.stdout or "").strip().splitlines()
        _log.info("webarena/refresh — %s/%s done (%s)", stack, site,
                  tail[-1] if tail else "no output")
    return refresh


def _expected(record: dict) -> dict:
    for e in record.get("eval", []):
        exp = e.get("expected")
        if isinstance(exp, dict) and exp.get("task_type"):
            return exp
    return {}


def task_type_of(record: dict) -> str:
    return _expected(record).get("task_type") or "retrieve"


class WebArenaEnv:
    """TaskEnv implementation for WebArena-Verified (684-task scope)."""

    def __init__(self, cfg: "CSSConfig", items: "dict | list | None" = None,
                 scorer: "VerifiedScorer | None" = None,
                 leases: "SiteLeaseManager | None" = None,
                 episode_fn: "Any | None" = None) -> None:
        self.cfg = cfg
        extra = getattr(cfg, "extra", {}) or {}
        self._items = common.resolve_items(items) or self._load_splits(cfg, extra)

        stacks = extra.get("webarena_stacks")
        if leases is not None:
            self.leases = leases
        else:
            if not stacks:
                raise ValueError(
                    "cfg.extra['webarena_stacks'] required: "
                    "{stack_name: {site: base_url}}")
            self.leases = SiteLeaseManager(
                stacks, refresh_fn=_build_refresh_fn(extra))

        if scorer is not None:
            self.scorer = scorer
        else:
            cli = extra.get("webarena_verified_cli", "")
            env_config = extra.get("webarena_env_config", "")
            self.scorer = (VerifiedScorer(cli, env_config)
                           if cli and env_config else None)
        # episode_fn injection keeps unit tests free of playwright.
        self._episode_fn = episode_fn
        # Browser concurrency is a separate budget from LLM concurrency
        # (max_api_workers): chromium instances are the CPU/RAM hogs on the
        # harness host, so run_one gates episodes on this semaphore while the
        # batch layer may hold many more task threads.
        import threading
        self._browser_slots = threading.BoundedSemaphore(
            int(extra.get("webarena_max_browsers", 24)))

    # -- splits --------------------------------------------------------------
    @staticmethod
    def _load_splits(cfg: "CSSConfig", extra: dict) -> "dict[str, list[dict]]":
        split_dir = extra.get("webarena_split_dir") or os.path.join(
            getattr(cfg, "data_root", "data"), "webarena_splits")
        out: dict[str, list[dict]] = {}
        for split in ("train", "val", "test"):
            path = os.path.join(split_dir, f"{split}.json")
            with open(path, encoding="utf-8") as f:
                out[split] = json.load(f)
        return out

    def train_items(self) -> "list[dict]":
        return common.slice_split(self._items["train"], self.cfg, "train")

    def val_items(self) -> "list[dict]":
        return common.slice_split(self._items["val"], self.cfg, "val")

    def test_items(self) -> "list[dict]":
        return common.slice_split(self._items["test"], self.cfg, "test")

    def action_space_description(self) -> str:
        from css.envs.webarena.prompts import ACTION_SPACE_DESCRIPTION
        return ACTION_SPACE_DESCRIPTION

    # -- execution -----------------------------------------------------------
    def load_cached_result(self, item: dict, out_dir: str, *,
                           rollout_index: int, skill_hash: str):
        return common.load_cached_result(
            item, out_dir, rollout_index=rollout_index, skill_hash=skill_hash)

    def run_one(self, item: dict, skill_text: str, target_client: Any,
                out_dir: str, *, rollout_index: int = 0, epoch: int = 0,
                node_id: str = "") -> Any:
        tid = common.item_id(item)
        pred_dir = common.prediction_dir(out_dir, tid, rollout_index)
        os.makedirs(pred_dir, exist_ok=True)
        ttype = task_type_of(item)
        skill_h = common.skill_hash(skill_text)

        # Ground-truth firewall: the agent-visible record must not carry the
        # evaluator block (expected answers / reference URLs).
        agent_item = {k: v for k, v in item.items() if k != "eval"}

        result: dict = {
            "id": tid, "task_id": tid, "skill_hash": skill_h,
            "task_type": ttype, "task_description": item.get("intent", ""),
            "hard": 0, "soft": 0.0, "n_turns": 0, "conversation": [],
        }
        lease = None
        try:
            lease = self.leases.acquire(item.get("sites", []), ttype)
            episode_fn = self._episode_fn
            if episode_fn is None:
                from css.envs.webarena.agent import run_episode as episode_fn
            with self._browser_slots:
                episode = episode_fn(agent_item, skill_text, target_client,
                                     self.cfg, lease, pred_dir)
            result["conversation"] = episode.get("messages", [])
            result["n_turns"] = int(episode.get("n_turns", 0))
            result["agent_response"] = episode.get("agent_response")
            if self.scorer is None:
                result["fail_reason"] = "no scorer configured"
                verdict = {"hard": 0, "detail": {"error": "no scorer"}}
            else:
                verdict = self.scorer.score(int(item["task_id"]), pred_dir)
            result["hard"] = int(verdict.get("hard", 0))
            result["soft"] = float(result["hard"])
            detail = verdict.get("detail", {})
            if not result["hard"]:
                result.setdefault(
                    "fail_reason",
                    str(detail.get("error") or detail.get("status") or
                        "evaluator scored 0")[:300])
            result["eval_detail"] = detail
        except Exception as exc:  # noqa: BLE001 — run_one must not raise
            _log.exception("webarena/run_one — task %s failed", tid)
            result["fail_reason"] = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            if lease is not None:
                self.leases.release(lease)

        outcome = "PASS" if result["hard"] else "FAIL"
        result["conversation"] = list(result["conversation"]) + [
            eval_annotation_message(
                outcome=outcome,
                ground_truth=json.dumps(_expected(item), ensure_ascii=False),
                detail=json.dumps(result.get("eval_detail", {}),
                                  ensure_ascii=False, default=str)[:2000])]
        return common.persist_result(result, pred_dir,
                                     rollout_index=rollout_index,
                                     epoch=epoch, node_id=node_id)
