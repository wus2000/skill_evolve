"""AppWorld task agent — full-history coding loop over a worker subprocess.

RETURN CONTRACT (see css/envs/template/agent.py): a plain dict with
``hard/soft/n_turns/fail_reason/conversation`` + env extras. The trajectory is
the canonical flat ``[{role, content:str}, ...]`` transcript; the eval
annotation is appended by ``task_interface.run_one`` (NOT here).

Protocol (agreed design 2026-07-04):
  * one LLM call per env step; the reply must be ONE fenced Python code block;
    fence-first extraction, whole-reply fallback (the official minimal agent
    treats the entire reply as code), EMPTY reply counts as ``parse_fail`` and
    consumes a turn (no silent repair — format failures are learnable signal);
  * termination is SELF-DECLARED (apis.supervisor.complete_task); the episode
    otherwise runs to the interaction cap or the wall-clock deadline. The
    three-way ``fail_reason`` (declared-but-failed / never-declared /
    episode-deadline) is deliberately distinct — each is a different failure
    class for the analyzer;
  * per-turn outputs arrive already truncated by the worker
    (``appworld_obs_max_chars``) — huge API dumps must not blow the context.

GROUND-TRUTH FIREWALL: the gold solution is fetched AFTER the episode loop
(train/dev only, ``ground_truth_mode="full"``) and never enters the agent's
messages; it rides in the result dict for the optimizer-only annotation.
"""
from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Any

from css.envs.appworld.prompts import build_first_user, build_system_prompt
from css.envs.common.subprocess_worker import SubprocessWorkerHost

if TYPE_CHECKING:
    from css.model.client import LLMClient

_CODE_FENCE_RE = re.compile(r"```(?:python|py)?[ \t]*\n(.*?)```", re.DOTALL)
# AppWorld's shell reports errors as "Execution failed. Traceback:\n..." (no
# "(most recent call last)") — match the common prefix only.
_TRACEBACK_MARKER = "Traceback"
_NO_CODE_FEEDBACK = (
    "Your reply contained no code. Respond with exactly ONE fenced Python "
    "code block:\n```python\n<your code>\n```"
)


class AppworldWorker:
    """One episode's world, isolated in a worker subprocess (JSON requests)."""

    def __init__(
        self,
        task_id: str,
        *,
        python_exe: str,
        worker_script: str,
        appworld_root: str,
        experiment_name: str,
        ground_truth_mode: str,
        max_interactions: int,
        obs_max_chars: int,
        start_timeout: float = 300.0,
        step_timeout: float = 240.0,
        stderr_path: str = "",
    ) -> None:
        import os
        import sys

        worker_script = worker_script or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "worker.py")
        env = dict(os.environ)
        if appworld_root:
            env["APPWORLD_ROOT"] = appworld_root
        self.step_timeout = step_timeout
        self.host = SubprocessWorkerHost(
            [python_exe or sys.executable, worker_script, task_id,
             experiment_name, ground_truth_mode, str(max_interactions),
             str(obs_max_chars)],
            env=env,
            stderr_path=stderr_path,
            name="appworld-worker",
        )
        self.proc = self.host.proc  # back-compat/diagnostic handle
        ready = self.host.read_event(start_timeout)
        if ready.get("event") != "ready":
            raise RuntimeError(
                "appworld worker failed to start: %s" % ready.get("error", ready))
        self.instruction = str(ready.get("instruction", ""))
        self.supervisor = dict(ready.get("supervisor", {}))
        self.metadata = dict(ready.get("metadata", {}))

    def _request(self, payload: dict, timeout: "float | None" = None) -> dict:
        self.host.send_json(payload)
        event = self.host.read_event(timeout or self.step_timeout)
        if event.get("event") == "fatal":
            raise RuntimeError("appworld worker fatal: %s" % event.get("error", ""))
        return event

    def execute(self, code: str) -> dict:
        return self._request({"op": "execute", "code": code})

    def evaluate(self) -> dict:
        return self._request({"op": "evaluate"})

    def gold(self) -> dict:
        return self._request({"op": "gold"})

    def close(self) -> None:
        self.host.close()


def parse_code(response: str) -> "tuple[str, bool]":
    """Extract the Python code from an assistant reply.

    Returns ``(code, parse_ok)``. First fenced block wins; a fenceless reply
    is treated as raw code in full (official minimal-agent convention — prose
    then fails visibly as a SyntaxError traceback, which is learnable signal).
    Only an EMPTY reply is a parse failure.
    """
    text = (response or "").strip()
    m = _CODE_FENCE_RE.search(response or "")
    if m:
        code = m.group(1).strip()
        return (code, True) if code else ("", False)
    if text:
        return text, True
    return "", False


def run_appworld_agent(
    client: "LLMClient",
    task_id: str,
    skill_text: str,
    *,
    python_exe: str,
    appworld_root: str,
    experiment_name: str,
    ground_truth_mode: str = "minimal",
    max_interactions: int = 50,
    obs_max_chars: int = 6000,
    max_tokens: int = 4096,
    temperature: float = 0.4,
    deadline_s: float = 1740.0,
    worker_script: str = "",
    fetch_gold: bool = False,
    stderr_path: str = "",
) -> "dict[str, Any]":
    """Run one AppWorld episode; return the result dict (contract above)."""
    started = time.time()
    conversation: "list[dict]" = []
    n_turns = 0
    n_parse_fail = 0
    n_tracebacks = 0
    declared = False
    success = False
    fail_reason = ""
    eval_report: dict = {}
    gold_code = ""
    gold_answer = None
    metadata: dict = {}
    worker = None

    try:
        worker = AppworldWorker(
            task_id,
            python_exe=python_exe,
            worker_script=worker_script,
            appworld_root=appworld_root,
            experiment_name=experiment_name,
            ground_truth_mode=ground_truth_mode,
            max_interactions=max_interactions,
            obs_max_chars=obs_max_chars,
            stderr_path=stderr_path,
        )
        metadata = worker.metadata
        conversation.append(
            {"role": "system", "content": build_system_prompt(skill_text)})
        conversation.append(
            {"role": "user",
             "content": build_first_user(worker.instruction, worker.supervisor)})

        while n_turns < max_interactions:
            if time.time() - started > deadline_s:
                fail_reason = "episode-deadline (%.0fs)" % deadline_s
                break
            reply = client.complete_target_messages(
                conversation, max_tokens=max_tokens, temperature=temperature)
            conversation.append({"role": "assistant", "content": reply or ""})
            n_turns += 1
            code, parse_ok = parse_code(reply or "")
            if not parse_ok:
                n_parse_fail += 1
                conversation.append({"role": "user", "content": _NO_CODE_FEEDBACK})
                continue
            event = worker.execute(code)
            output = str(event.get("output", ""))
            if _TRACEBACK_MARKER in output:
                n_tracebacks += 1
            conversation.append(
                {"role": "user", "content": output if output.strip() else "(no output)"})
            if event.get("completed"):
                declared = True
                break

        evaluation = worker.evaluate()
        success = bool(evaluation.get("success"))
        eval_report = dict(evaluation.get("report", {}))

        if not success and not fail_reason:
            if declared:
                fail_reason = "declared-but-failed"
            else:
                fail_reason = "never-declared (interaction cap %d)" % max_interactions

        # GT firewall: gold is fetched post-episode, never enters `conversation`.
        if fetch_gold:
            try:
                gold = worker.gold()
                gold_code = str(gold.get("solution_code", "") or "")
                gold_answer = gold.get("answer")
            except Exception:  # noqa: BLE001 — annotation is best-effort
                pass
    except Exception as e:  # noqa: BLE001 - an agent/env crash is a failed rollout
        fail_reason = "agent-error: %s: %s" % (type(e).__name__, e)
    finally:
        if worker is not None:
            worker.close()

    hard = int(success)
    return {
        "hard": hard,
        "soft": float(hard),
        "n_cases": 1,
        "n_pass": hard,
        "n_turns": n_turns,
        "fail_reason": "" if success else fail_reason,
        "conversation": conversation,
        # Env extras (absorbed into TaskResult.extras):
        "declared": declared,
        "n_parse_fail": n_parse_fail,
        "n_tracebacks": n_tracebacks,
        "eval_report": eval_report,
        "task_metadata": metadata,
        "gold_solution_code": gold_code,
        "gold_answer": gold_answer,
        "elapsed_s": round(time.time() - started, 1),
    }
