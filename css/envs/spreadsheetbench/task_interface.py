"""SpreadsheetBench task interface for CSS rollouts.

This module is the *only* place CSS touches the SpreadsheetBench environment.
It exposes a narrow ``TaskEnv`` protocol so the rest of the rollout
infrastructure (``css.rollout.batch`` / ``css.rollout.selection_eval``) is
benchmark-agnostic and unit-testable without the heavyweight
``skillopt`` / ``openpyxl`` dependencies or the dataset on disk.

Design notes
------------
* **First Law (LLM-Code division).** ``run_one`` receives only a
  target-capable ``LLMClient`` and drives the frozen task agent through
  ``target_client.complete_target(...)``. It NEVER touches an optimizer
  client and never reaches SkillOpt's process-global backend router — the
  injected client *is* the backend. This keeps the frozen task agent
  call-isolated from the optimizer.
* **Faithful reuse.** ``SpreadsheetBenchEnv.run_one`` replicates the
  result-dict shape of ``skillopt.envs.spreadsheetbench.rollout``'s
  ``process_one_codegen`` (hard / soft / n_cases / n_pass / n_turns /
  fail_reason / phase / cases / spreadsheet_preview / target_*_prompt) and
  reuses SkillOpt's prompt builders, executor, and evaluator verbatim — but
  it sources the single LLM completion from the injected client rather than
  the global router. The combined skill text is injected exactly where
  SkillOpt injects ``skill_content``: ``_build_system(skill_content)``.
* **Lazy imports.** Every ``skillopt`` / ``openpyxl`` import lives inside a
  method body so that importing this module (and therefore the whole
  ``css.rollout`` stack) works without those packages installed. The test
  double ``FakeTaskEnv`` imports nothing heavy.
* **Per-rollout isolation.** Each rollout writes under
  ``<out_dir>/predictions/<task_id>/r<rollout_index>/`` so the K repeats of a
  task never clobber each other's artifacts or ``conversation.json``.
"""
from __future__ import annotations

import json
import os
import traceback
from typing import Any, Protocol, runtime_checkable

from css.config import CSSConfig
from css.data.rollout import TaskResult


# ── Trajectory hydration ────────────────────────────────────────────────────


def hydrate_trajectory(result: dict, prediction_dir: str, task_id: str) -> dict:
    """Populate ``result["conversation"]`` from a written ``conversation.json``.

    SkillOpt's ``process_one_codegen`` does not return the conversation in its
    result dict; it *writes* it to ``<prediction_dir>/conversation.json`` (and
    re-writes an enriched copy after evaluation). CSS needs the trajectory in
    the result so ``TaskResult.from_dict`` can map it onto ``messages``.

    If the file is present and decodes to a list, it is attached under the
    ``"conversation"`` key (which ``TaskResult.from_dict`` already understands).
    Missing / unreadable files leave ``result`` untouched. Returns ``result``.
    """
    conv_path = os.path.join(prediction_dir, "conversation.json")
    if not os.path.exists(conv_path):
        return result
    try:
        with open(conv_path, encoding="utf-8") as f:
            conversation = json.load(f)
    except Exception:  # noqa: BLE001 - corrupt/partial file: leave result as-is
        return result
    if isinstance(conversation, list):
        result["conversation"] = conversation
    return result


# ── Task environment protocol ───────────────────────────────────────────────


@runtime_checkable
class TaskEnv(Protocol):
    """The narrow surface CSS rollout code depends on.

    Implementations supply task items per split and execute one (task, rollout)
    under a given skill document, returning a :class:`TaskResult`.
    """

    def train_items(self) -> list[dict]: ...

    def val_items(self) -> list[dict]: ...

    def test_items(self) -> list[dict]: ...

    def run_one(
        self,
        item: dict,
        skill_text: str,
        target_client: "LLMClient",  # noqa: F821 - structural; see css.model.client
        out_dir: str,
        *,
        rollout_index: int = 0,
        epoch: int = -1,
        node_id: str = "",
    ) -> TaskResult: ...


# ── Concrete SpreadsheetBench environment ───────────────────────────────────


class SpreadsheetBenchEnv:
    """Concrete :class:`TaskEnv` backed by SkillOpt's SpreadsheetBench env.

    Items can be supplied three ways (checked in order):

    1. ``items`` — an explicit ``{"train": [...], "val": [...], "test": [...]}``
       mapping (or a flat list, treated as the train split). No data loading.
    2. ``split_dir`` — an existing ``train/`` ``val/`` ``test/`` split tree,
       loaded lazily via SkillOpt's ``SpreadsheetBenchDataLoader``.
    3. ``cfg.split_dir`` / ``cfg.data_path`` — fall back to the config's
       split / dataset settings.

    All heavy imports (``skillopt``, ``openpyxl``) happen lazily inside the
    methods that need them, so constructing / importing this class is cheap and
    dependency-free.
    """

    def __init__(
        self,
        cfg: CSSConfig,
        *,
        data_root: str = "",
        split_dir: str = "",
        items: dict | None = None,
    ) -> None:
        self.cfg = cfg
        self.data_root = data_root or cfg.data_root
        self.split_dir = split_dir or cfg.split_dir
        # Explicitly provided items short-circuit all dataset loading.
        if items is None:
            self._items: dict[str, list[dict]] | None = None
        elif isinstance(items, dict):
            self._items = {
                "train": list(items.get("train", [])),
                "val": list(items.get("val", [])),
                "test": list(items.get("test", [])),
            }
        else:
            # A flat list is treated as the train split.
            self._items = {"train": list(items), "val": [], "test": []}
        self._loader: Any = None  # lazily built SpreadsheetBenchDataLoader

    # ── Split accessors ──────────────────────────────────────────────────

    def _ensure_loader(self) -> Any:
        """Lazily build + set up the SkillOpt SplitDataLoader (no-op if items given)."""
        if self._loader is not None:
            return self._loader
        # Lazy import: only needed when items were not supplied directly.
        from skillopt.envs.spreadsheetbench.dataloader import (  # noqa: PLC0415
            SpreadsheetBenchDataLoader,
        )

        loader = SpreadsheetBenchDataLoader(
            split_dir=self.split_dir,
            data_path=self.cfg.data_path,
            split_mode="split_dir" if self.split_dir else "ratio",
            split_ratio="2:1:7",
            split_seed=self.cfg.seed,
            data_root=self.data_root,
            seed=self.cfg.seed,
        )
        # SplitDataLoader.setup() resolves split_mode and loads all splits.
        loader.setup(self.cfg.to_dict())
        self._loader = loader
        return loader

    def train_items(self) -> list[dict]:
        if self._items is not None:
            return list(self._items.get("train", []))
        return list(self._ensure_loader().train_items)

    def val_items(self) -> list[dict]:
        if self._items is not None:
            return list(self._items.get("val", []))
        return list(self._ensure_loader().val_items)

    def test_items(self) -> list[dict]:
        if self._items is not None:
            return list(self._items.get("test", []))
        return list(self._ensure_loader().test_items)

    # ── Single rollout ───────────────────────────────────────────────────

    def run_one(
        self,
        item: dict,
        skill_text: str,
        target_client: "LLMClient",  # noqa: F821
        out_dir: str,
        *,
        rollout_index: int = 0,
        epoch: int = -1,
        node_id: str = "",
    ) -> TaskResult:
        """Run one (task, rollout) of SpreadsheetBench codegen and score it.

        Mirrors ``skillopt...rollout.process_one_codegen`` (single mode) but:
          * uses the injected ``target_client.complete_target`` for the one LLM
            call (instance injection; never the global router / optimizer), and
          * writes artifacts under ``predictions/<task_id>/r<rollout_index>/`` so
            the K rollouts of one task stay isolated.

        Returns ``TaskResult.from_dict(result)`` with ``rollout_index`` /
        ``epoch`` / ``node_id`` stamped and the hydrated conversation attached.
        """
        # Lazy: SkillOpt prompt builders / executor / evaluator + openpyxl.
        from skillopt.envs.spreadsheetbench.rollout import (  # noqa: PLC0415
            _find_test_cases,
        )
        from skillopt.envs.spreadsheetbench.codegen_agent import (  # noqa: PLC0415
            _build_system,
            _build_user,
            _preview_workbook,
            extract_code,
        )
        from skillopt.envs.spreadsheetbench.executor import (  # noqa: PLC0415
            run_generated_code,
        )
        from skillopt.envs.spreadsheetbench.evaluator import evaluate  # noqa: PLC0415

        task_id = str(item["id"])
        instruction = item["instruction"]
        instruction_type = item.get("instruction_type", "")
        answer_position = item.get("answer_position", "")
        answer_sheet = item.get("answer_sheet", "")
        if answer_position and answer_sheet and "!" not in answer_position:
            answer_position_eval = f"{answer_sheet}!{answer_position}"
        else:
            answer_position_eval = answer_position

        itype_lower = (instruction_type or "").lower()
        if "cell" in itype_lower:
            task_type = "cell_level"
        elif "sheet" in itype_lower:
            task_type = "sheet_level"
        else:
            task_type = "other"

        sp = item.get("spreadsheet_path", f"spreadsheet/{task_id}")
        task_dir = sp if os.path.isabs(sp) else os.path.join(self.data_root, sp)

        result: dict[str, Any] = {
            "id": task_id,
            "ok": False,
            "instruction_type": instruction_type,
            "task_type": task_type,
            "task_description": instruction,
            "phase": "setup",
            "fail_reason": "",
            "llm_ok": False,
            "code_ok": False,
            "exec_ok": False,
            "n_cases": 0,
            "n_exec_pass": 0,
            "n_pass": 0,
            "soft": 0.0,
            "hard": 0,
            "n_turns": 0,
            "cases": [],
            "error": "",
        }

        # Per-rollout prediction directory: predictions/<task_id>/r<idx>/
        prediction_dir = os.path.join(
            out_dir, "predictions", task_id, f"r{rollout_index}"
        )

        try:
            cases = _find_test_cases(task_dir)
            result["n_cases"] = len(cases)
            if not cases:
                result["fail_reason"] = "no-test-cases"
                return self._finalize(
                    result, prediction_dir, task_id, rollout_index, epoch, node_id
                )

            os.makedirs(prediction_dir, exist_ok=True)

            first_input = cases[0][1]
            try:
                preview_text = _preview_workbook(first_input)
            except Exception:  # noqa: BLE001
                preview_text = "(preview failed)"

            # The combined skill text goes exactly where SkillOpt injects it.
            target_system = _build_system(skill_text)
            target_user = _build_user(
                instruction,
                first_input,
                instruction_type,
                answer_position_eval,
            )

            with open(os.path.join(prediction_dir, "spreadsheet_preview.txt"), "w") as f:
                f.write(preview_text)
            with open(os.path.join(prediction_dir, "target_system_prompt.txt"), "w") as f:
                f.write(target_system)
            with open(os.path.join(prediction_dir, "target_user_prompt.txt"), "w") as f:
                f.write(target_user)

            result["spreadsheet_preview"] = preview_text
            result["target_system_prompt"] = target_system
            result["target_user_prompt"] = target_user

            # ── LLM phase: single completion via the INJECTED target client ──
            result["phase"] = "llm"
            try:
                raw = target_client.complete_target(
                    target_system,
                    target_user,
                    max_tokens=16384,
                    temperature=0.0,
                )
            except Exception as e:  # noqa: BLE001
                result["fail_reason"] = f"llm-call-failed: {type(e).__name__}: {e}"
                result["error"] = traceback.format_exc()
                return self._finalize(
                    result, prediction_dir, task_id, rollout_index, epoch, node_id
                )

            result["llm_ok"] = True
            result["n_turns"] = 1
            code = extract_code(raw)
            conversation: list[dict] = [{"role": "assistant", "content": raw}]

            with open(os.path.join(prediction_dir, "code.py"), "w") as f:
                f.write(code)
            with open(os.path.join(prediction_dir, "raw.txt"), "w") as f:
                f.write(raw)
            with open(os.path.join(prediction_dir, "conversation.json"), "w") as f:
                json.dump(conversation, f, ensure_ascii=False, indent=2)

            if not code.strip():
                result["phase"] = "extract"
                result["fail_reason"] = "empty-code-block"
                return self._finalize(
                    result, prediction_dir, task_id, rollout_index, epoch, node_id
                )
            result["code_ok"] = True

            # ── Exec + eval per test case ───────────────────────────────────
            result["phase"] = "exec"
            all_exec = True
            enrichment_parts: list[str] = []

            for no, ip, ap in cases:
                pred_path = os.path.join(prediction_dir, f"{no}_pred.xlsx")

                if not os.path.exists(pred_path):
                    ok_exec, err = run_generated_code(code, ip, pred_path)
                    if not ok_exec:
                        all_exec = False
                        result["cases"].append(
                            {"no": no, "stage": "exec", "ok": False, "error": err[:500]}
                        )
                        if not result["fail_reason"]:
                            tail = (
                                err.strip().splitlines()[-1][:200]
                                if err.strip()
                                else "unknown"
                            )
                            result["fail_reason"] = f"exec-error: {tail}"
                        # D7: keep the FULL error in the trajectory message —
                        # it is the most diagnostic signal for failure rollouts
                        # and the contrastive analyst reads this conversation.
                        # The err[:500]/[:200] clips above stay on metadata only;
                        # css.trajectory.truncate_tool_results applies the sole
                        # sanctioned reduction (>= tool_trunc) downstream.
                        enrichment_parts.append(
                            f"## Execution (case {no})\nERROR: {err}"
                        )
                        continue

                if not os.path.exists(pred_path):
                    all_exec = False
                    result["cases"].append(
                        {"no": no, "stage": "exec", "ok": False, "error": "output-not-found"}
                    )
                    if not result["fail_reason"]:
                        result["fail_reason"] = "output-not-found"
                    continue

                result["n_exec_pass"] += 1
                try:
                    ev = evaluate(pred_path, ap, instruction_type, answer_position_eval)
                except Exception as e:  # noqa: BLE001
                    ev = {"ok": False, "reason": f"eval-exception: {type(e).__name__}: {e}"}

                if ev["ok"]:
                    result["n_pass"] += 1
                else:
                    if not result["fail_reason"]:
                        result["fail_reason"] = f"eval-mismatch: {ev['reason'][:200]}"
                result["cases"].append(
                    {"no": no, "stage": "eval", "ok": ev["ok"], "reason": ev.get("reason", "")}
                )

                if answer_position_eval:
                    from skillopt.envs.spreadsheetbench.rollout import (  # noqa: PLC0415
                        _auto_verify_output,
                    )

                    verify_report = _auto_verify_output(pred_path, ap, answer_position_eval)
                    enrichment_parts.append(
                        f"## Eval Result (case {no}): {'PASS' if ev['ok'] else 'FAIL'}\n"
                        f"{ev.get('reason', '')}\n\n{verify_report}"
                    )

            result["exec_ok"] = all_exec

            # Enrich + re-save the conversation with post-execution verification.
            if enrichment_parts:
                enrichment_msg = "\n\n---\n\n".join(enrichment_parts)
                conversation.append(
                    {
                        "role": "system",
                        "content": f"[POST-EXECUTION VERIFICATION]\n\n{enrichment_msg}",
                    }
                )
                with open(os.path.join(prediction_dir, "conversation.json"), "w") as f:
                    json.dump(conversation, f, ensure_ascii=False, indent=2)

            n_cases = result["n_cases"]
            n_pass = result["n_pass"]
            result["soft"] = (n_pass / n_cases) if n_cases else 0.0
            result["hard"] = 1 if (n_cases > 0 and n_pass == n_cases) else 0
            result["ok"] = bool(result["hard"])
            if result["ok"]:
                result["fail_reason"] = ""
            return self._finalize(
                result, prediction_dir, task_id, rollout_index, epoch, node_id
            )

        except Exception as e:  # noqa: BLE001
            result["fail_reason"] = f"unexpected: {type(e).__name__}: {e}"
            result["error"] = traceback.format_exc()
            return self._finalize(
                result, prediction_dir, task_id, rollout_index, epoch, node_id
            )

    @staticmethod
    def _finalize(
        result: dict,
        prediction_dir: str,
        task_id: str,
        rollout_index: int,
        epoch: int,
        node_id: str,
    ) -> TaskResult:
        """Hydrate the trajectory, stamp provenance, and build the TaskResult."""
        hydrate_trajectory(result, prediction_dir, task_id)
        result["rollout_index"] = rollout_index
        result["epoch"] = epoch
        result["node_id"] = node_id
        return TaskResult.from_dict(result)


# ── Test double ──────────────────────────────────────────────────────────────


class FakeTaskEnv:
    """Deterministic :class:`TaskEnv` test double — no skillopt / openpyxl.

    ``run_one`` returns a canned :class:`TaskResult` carrying a non-empty
    2-message conversation and hard/soft scores from an injected ``scorer``.

    The ``scorer`` is ``scorer(item, rollout_index) -> hard`` (an int in
    ``{0, 1}`` or a float fraction). When it returns an int the soft score
    mirrors hard; when it returns a fraction, ``hard`` is 1 iff the fraction is
    >= 1.0. With no scorer, every rollout passes.
    """

    def __init__(self, items, scorer=None) -> None:
        if isinstance(items, dict):
            self._items = {
                "train": list(items.get("train", [])),
                "val": list(items.get("val", [])),
                "test": list(items.get("test", [])),
            }
        else:
            self._items = {"train": list(items), "val": [], "test": []}
        self.scorer = scorer

    def train_items(self) -> list[dict]:
        return list(self._items.get("train", []))

    def val_items(self) -> list[dict]:
        return list(self._items.get("val", []))

    def test_items(self) -> list[dict]:
        return list(self._items.get("test", []))

    def run_one(
        self,
        item: dict,
        skill_text: str,
        target_client: "LLMClient",  # noqa: F821 - unused; double does no LLM call
        out_dir: str,
        *,
        rollout_index: int = 0,
        epoch: int = -1,
        node_id: str = "",
    ) -> TaskResult:
        task_id = str(item.get("id", item.get("task_id", "")))
        instruction = item.get("instruction", item.get("task_description", ""))

        if self.scorer is None:
            score = 1
        else:
            score = self.scorer(item, rollout_index)

        if isinstance(score, bool) or isinstance(score, int):
            hard = 1 if int(score) >= 1 else 0
            soft = float(hard)
        else:
            soft = float(score)
            hard = 1 if soft >= 1.0 else 0

        messages = [
            {
                "role": "user",
                "content": f"[FAKE TASK {task_id}] {instruction}",
            },
            {
                "role": "assistant",
                "content": (
                    f"[FAKE ROLLOUT r{rollout_index}] "
                    f"skill_len={len(skill_text)} "
                    f"-> {'PASS' if hard else 'FAIL'}"
                ),
            },
        ]

        return TaskResult.from_dict(
            {
                "id": task_id,
                "rollout_index": rollout_index,
                "hard": hard,
                "soft": soft,
                "n_cases": 1,
                "n_pass": hard,
                "fail_reason": "" if hard else "fake-fail",
                "messages": messages,
                "n_turns": 1,
                "task_type": str(item.get("instruction_type", "")) or "other",
                "task_description": instruction,
                "instruction_type": str(item.get("instruction_type", "")),
                "epoch": epoch,
                "node_id": node_id,
            }
        )
