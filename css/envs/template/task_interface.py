"""Template ``TaskEnv`` — copy this file to onboard a new benchmark.

HOW TO USE THIS TEMPLATE
========================
1. ``cp -r css/envs/template css/envs/<your_env>`` and rename the class.
2. Work through every ``TODO(env)`` marker in this file and ``agent.py``.
3. Register the env in ``css/envs/registry.py`` (one alias + one branch).
4. Copy ``run_experiment_template.py`` to a launcher and fill in paths/knobs.
5. Adapt ``css/tests/test_env_template.py`` into a contract test for your env
   and make it pass — it exercises every mechanism-facing behavior.
Full contract documentation: ``docs/env_integration_guide.md``.

WHAT THE MECHANISM EXPECTS (the complete surface — nothing else is called):

  method                      called by                             when
  ─────────────────────────── ───────────────────────────────────── ─────────
  train_items()               orchestrator, coldstart, exploitation every round (batching), cold-start bare rollout, analysis sampling
  val_items()                 orchestrator, exploitation, paired    val gate / baselines / round eval
  test_items()                orchestrator, coldstart               bare test baseline, per-round test eval
  run_one(...)                css.rollout.batch (ONLY entry point)  every single (task, rollout) execution
  load_cached_result(...)     css.rollout.batch (optional)          before each run_one — resume fast-path
  action_space_description()  orchestrator -> L1 proposal + coldstart  paradigm-design prompts

INVARIANTS THE MECHANISM RELIES ON (violating any breaks the run silently):
  * Item dicts carry a unique, stable ``id`` (fallback key: ``task_id``).
  * Split accessors are DETERMINISTIC across calls and process restarts —
    batching seeds, caches, and ledgers key off stable item order/ids.
  * ``run_one`` NEVER raises for task-level failures — it returns a failed
    ``TaskResult`` (hard=0, fail_reason set). Raising is reserved for
    infrastructure bugs; the batch layer converts those to failed results
    with counts preserved, but you lose the trajectory.
  * ``TaskResult.messages`` follows the canonical trajectory contract
    (``css/trajectory.py``): flat ``{role, content:str}`` transcript, with
    the post-rollout eval annotation appended as the LAST message.
  * The gold answer never appears in agent-visible prompts or messages
    (ground-truth firewall); it appears ONLY in the eval annotation.
  * ``result.json`` is persisted per (task, rollout) under the canonical
    prediction dir with a ``skill_hash`` — this powers crash-resume and the
    prediction-reuse optimizations (val-gate reuse, round-eval reuse).
"""
from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

from css.envs import common
from css.trajectory import eval_annotation_message

if TYPE_CHECKING:
    from css.config import CSSConfig
    from css.data.rollout import TaskResult
    from css.model.client import LLMClient


class TemplateEnv:
    """Concrete :class:`css.envs.base.TaskEnv` scaffold (toy QA, exact match).

    Constructor conventions (all concrete envs follow these):
      * ``cfg`` — the run config. Read env-specific knobs from ``cfg.extra``
        under a ``<env>_*`` namespace (e.g. ``template_max_turns``); never add
        env fields to ``CSSConfig`` itself.
      * ``items`` — optional explicit ``{"train": [...], "val": [...],
        "test": [...]}`` (or flat list = train). Used by tests; skips disk.
      * ``split_dir`` / ``data_root`` — where to load real data from
        (fall back to ``cfg.split_dir`` / ``cfg.data_root``).
    """

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
        self.data_root = data_root or cfg.data_root
        self._items = common.resolve_items(items)

    # ── Split accessors ────────────────────────────────────────────────────
    # WHO calls: orchestrator (round batching, val subset, test eval),
    # coldstart (bare test + bare train), analysis (difficulty-weighted
    # sampling), L1 proposal (diagnostic/regression task selection).
    # CONTRACT: deterministic order; every item has a unique "id"; the
    # n_train/n_val/n_test knobs slice the split (0 = whole split).

    def _load_split(self, split: str) -> list[dict]:
        """TODO(env): load one split from disk.

        The convention used by existing envs: ``split_dir`` contains
        ``{train,val,test}/items.json`` — a JSON list of item dicts, already
        shuffled at split-creation time (so prefix-slicing is sampling).
        Item dicts carry whatever ``run_one`` needs (paths under
        ``data_root``, the question, metadata...) — the mechanism never
        inspects them beyond ``id``.
        """
        path = os.path.join(self.split_dir, split, "items.json")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return list(data) if isinstance(data, list) else []

    def _split(self, split: str) -> list[dict]:
        if self._items is not None:
            items = list(self._items.get(split, []))
        else:
            items = self._load_split(split)
        return common.slice_split(items, self.cfg, split)

    def train_items(self) -> list[dict]:
        return self._split("train")

    def val_items(self) -> list[dict]:
        return self._split("val")

    def test_items(self) -> list[dict]:
        return self._split("test")

    # ── Action space (L1 paradigm-design context) ──────────────────────────
    # WHO calls: orchestrator injects this into the L1 proposal cycle and
    # cold-start paradigm design (as cfg._env_action_space). The optimizer
    # uses it to understand what behavioral building blocks the agent has,
    # so the behavioral paradigms it designs are executable.
    # CONTRACT: describe (1) the interaction pattern (e.g. ReAct loop and its
    # turn cap), (2) every available action/tool, (3) structural constraints
    # (termination, one-action-per-turn, ...). Plain text, no markdown tables.

    def action_space_description(self) -> str:
        # TODO(env): describe YOUR agent's real action space. Keep it faithful
        # — the L1 optimizer designs multi-phase behavioral paradigms around
        # exactly these actions.
        extra = getattr(self.cfg, "extra", {}) or {}
        max_turns = int(extra.get("template_max_turns", 1))
        return (
            "The agent answers in a single turn (up to "
            f"{max_turns} turn(s)). One action is available:\n"
            "- answer(text): produce the final answer, terminated by a "
            "'FINAL ANSWER: <answer>' line. No tools, no retries.\n"
            "The agent sees the task question and must produce an exact "
            "answer (evaluated by normalized string match)."
        )

    # ── Rollout ────────────────────────────────────────────────────────────
    # WHO calls: css.rollout.batch.batch_rollout — the ONLY mechanism entry
    # point for task execution. Everything that rolls tasks (cold start,
    # exploitation batches, per-edit verification, paired gate, round val /
    # test eval, L1 diagnostics) funnels through it.
    # CONTRACT: see module docstring INVARIANTS. The flow below is canonical;
    # customize the marked sections, keep the structure.

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
        from css.envs.template.agent import run_template_agent  # noqa: PLC0415

        extra = getattr(self.cfg, "extra", {}) or {}
        task_id = common.item_id(item)

        # task_description is what the OPTIMIZER sees as "the task" in every
        # analysis prompt. Include the question and agent-visible context;
        # NEVER the gold answer.
        task_description = str(item.get("question", ""))

        pred_dir = common.prediction_dir(out_dir, task_id, rollout_index)

        # (1) Execute the agent (env-private; see agent.py RETURN CONTRACT).
        #     TODO(env): thread your env's knobs from cfg.extra.
        agent_out = run_template_agent(
            target_client,
            item,
            skill_text,
            max_turns=int(extra.get("template_max_turns", 1)),
            max_tokens=int(extra.get("template_max_tokens", 4096)),
            temperature=float(extra.get("template_temperature", 0.0)),
        )

        # (2) Append the unified post-rollout eval annotation — the outcome +
        #     ground truth the optimizer's analysis needs to diagnose failures.
        #     The task agent never saw this (it ran firewalled from gold); the
        #     GROUND_TRUTH_FIREWALL clause (injected into every optimizer
        #     prompt centrally) keeps optimizer OUTPUTS from depending on it.
        hard = int(agent_out["hard"])
        conversation = list(agent_out["conversation"])
        conversation.append(eval_annotation_message(
            outcome=(
                f"score={hard} ({'pass' if hard else 'fail'}), "
                f"soft={float(agent_out['soft']):.3f}"
            ),
            # TODO(env): expose YOUR gold reference (gold SQL, expected cells,
            # reference output ...) for the analyst.
            ground_truth=f"Gold answer: {agent_out.get('gold_answer', '')}",
            detail=str(agent_out.get("fail_reason", "") or ""),
        ))

        # (3) Assemble the canonical result dict. Generic fields first; any
        #     extra keys are absorbed into TaskResult.extras automatically.
        result: dict[str, Any] = {
            "id": task_id,
            "task_description": task_description,
            # task_type: a coarse env-defined category label; the analysis
            # sampler and observability group by it. "other" when unknown.
            "task_type": str(item.get("category", "") or "other"),
            "hard": hard,
            "soft": float(agent_out["soft"]),
            "n_cases": 1,
            "n_pass": hard,
            "n_turns": int(agent_out["n_turns"]),
            "fail_reason": str(agent_out["fail_reason"]),
            "conversation": conversation,
            # skill_hash makes the persisted result resumable ONLY under the
            # same skill text — never omit it.
            "skill_hash": common.skill_hash(skill_text),
            # TODO(env): env-specific extras for auditing/debugging:
            "predicted_answer": agent_out.get("predicted_answer", ""),
            "gold_answer": agent_out.get("gold_answer", ""),
        }

        # (4) Stamp provenance + persist result.json + build the TaskResult.
        return common.persist_result(
            result, pred_dir,
            rollout_index=rollout_index, epoch=epoch, node_id=node_id,
        )

    # ── Resume cache ───────────────────────────────────────────────────────
    # WHO calls: css.rollout.batch before every run_one. Enables (a) crash
    # resume, (b) the orchestrator's prediction-reuse optimizations (round
    # val eval reusing the gate's predictions). Delegating to the common
    # implementation is all a standard env needs.

    def load_cached_result(
        self,
        item: dict,
        out_dir: str,
        *,
        rollout_index: int,
        skill_hash: str,
    ) -> "TaskResult | None":
        return common.load_cached_result(
            item, out_dir, rollout_index=rollout_index, skill_hash=skill_hash,
        )
