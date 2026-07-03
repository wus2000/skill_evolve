"""ALFWorld task agent — full-history ReAct loop over a worker subprocess.

RETURN CONTRACT (see css/envs/template/agent.py): a plain dict with
``hard/soft/n_turns/fail_reason/conversation`` + env extras. The trajectory is
the canonical flat ``[{role, content:str}, ...]`` transcript; the eval
annotation is appended by ``task_interface.run_one`` (NOT here).

Protocol (design discussion 2026-07-03):
  * one LLM call per env step: ``<think>...</think><action>...</action>``;
  * the action tag is the only rigid element — first match wins, content is
    conservatively normalized (strip / unquote / first line / lowercase), a
    missing tag falls back to the safe no-op ``look`` and is COUNTED, never
    silently repaired (format failures are learnable signal);
  * NO semantic repair and NO fuzzy matching against admissible commands —
    ungrammatical actions must reach the env and fail visibly
    ("Nothing happens.") so reflection can attribute them;
  * the per-step admissible list is recorded for the optimizer-only eval
    annotation but never shown to the agent.

GROUND-TRUTH FIREWALL: nothing in this module reads gold plans or expert
actions; the agent sees only observations and its own history.
"""
from __future__ import annotations

import json
import os
import re
import select
import subprocess
import sys
import time
from typing import TYPE_CHECKING, Any

from css.envs.alfworld.prompts import build_system_prompt
from css.envs.alfworld.worker import CLOSE_SENTINEL

if TYPE_CHECKING:
    from css.model.client import LLMClient

_ACTION_RE = re.compile(r"<action>(.*?)</action>", re.DOTALL | re.IGNORECASE)
_FALLBACK_ACTION = "look"
_NOTHING_HAPPENS = "Nothing happens."


# ── Worker subprocess management ───────────────────────────────────────────
class AlfredWorker:
    """One episode's environment, isolated in a worker subprocess.

    The worker interpreter is configurable (``alfworld_python``) so the css
    process never needs textworld importable. stderr is discarded (the engine
    is noisy); all diagnostics arrive as protocol events on stdout.
    """

    def __init__(
        self,
        gamefile: str,
        *,
        python_exe: str,
        max_steps: int,
        data_root: str,
        start_timeout: float = 240.0,
        step_timeout: float = 120.0,
        worker_script: str = "",
    ) -> None:
        worker_script = worker_script or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "worker.py")
        env = dict(os.environ)
        if data_root:
            env["ALFWORLD_DATA"] = data_root
        self.step_timeout = step_timeout
        self.proc = subprocess.Popen(
            [python_exe or sys.executable, worker_script, gamefile, str(max_steps)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        ready = self._read_event(start_timeout)
        if ready.get("event") != "ready":
            raise RuntimeError(
                "alfworld worker failed to start: %s" % ready.get("error", ready))
        self.observation = str(ready.get("obs", ""))
        self.admissible = list(ready.get("admissible", []))

    def _read_event(self, timeout: float) -> dict:
        """Read one protocol line with a hard timeout (POSIX select)."""
        stdout = self.proc.stdout
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError("alfworld worker timed out after %.0fs" % timeout)
            readable, _, _ = select.select([stdout], [], [], min(remaining, 5.0))
            if not readable:
                if self.proc.poll() is not None:
                    raise RuntimeError(
                        "alfworld worker exited (code %s) without a protocol line"
                        % self.proc.returncode)
                continue
            line = stdout.readline()
            if not line:
                raise RuntimeError(
                    "alfworld worker closed stdout (code %s)" % self.proc.poll())
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue  # stray non-protocol output; keep reading

    def step(self, command: str) -> dict:
        """Execute one command; returns the step event dict."""
        assert self.proc.stdin is not None
        self.proc.stdin.write(command.replace("\n", " ") + "\n")
        self.proc.stdin.flush()
        event = self._read_event(self.step_timeout)
        if event.get("event") == "fatal":
            raise RuntimeError("alfworld worker fatal: %s" % event.get("error", ""))
        self.observation = str(event.get("obs", ""))
        self.admissible = list(event.get("admissible", []))
        return event

    def close(self) -> None:
        try:
            if self.proc.poll() is None and self.proc.stdin is not None:
                self.proc.stdin.write(CLOSE_SENTINEL + "\n")
                self.proc.stdin.flush()
                self.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        finally:
            if self.proc.poll() is None:
                self.proc.kill()


# ── Action parsing (deterministic, conservative) ───────────────────────────
def parse_action(response: str) -> tuple[str, bool]:
    """Extract the command from an assistant reply.

    Returns ``(command, parse_ok)``. First ``<action>`` match wins; the
    content is stripped of surrounding quotes/backticks and trailing periods,
    reduced to its first non-empty line, and lowercased (the grammar's
    vocabulary is all-lowercase, so this is loss-free). A missing/empty tag
    falls back to ``look`` with ``parse_ok=False``.
    """
    m = _ACTION_RE.search(response or "")
    if not m:
        return _FALLBACK_ACTION, False
    content = m.group(1).strip()
    for line in content.splitlines():
        prev = None
        while line != prev:  # peel nested quotes/periods/whitespace to a fixpoint
            prev = line
            line = line.strip().strip("`'\"").rstrip(".")
        if line:
            return line.lower(), True
    return _FALLBACK_ACTION, False


# ── Episode loop ───────────────────────────────────────────────────────────
def run_alfworld_agent(
    client: "LLMClient",
    gamefile: str,
    skill_text: str,
    *,
    python_exe: str,
    data_root: str,
    max_steps: int = 50,
    max_tokens: int = 16384,
    temperature: float = 0.4,
    deadline_s: float = 1500.0,
) -> dict[str, Any]:
    """Play ONE episode; returns the RETURN CONTRACT dict."""
    system = build_system_prompt(skill_text)
    conversation: list[dict] = [{"role": "system", "content": system}]

    n_invalid = 0
    n_parse_fail = 0
    won = False
    done = False
    fail_reason = ""
    n_turns = 0
    admissible_trace: list[list[str]] = []
    started = time.time()

    worker: AlfredWorker | None = None
    try:
        worker = AlfredWorker(
            gamefile,
            python_exe=python_exe,
            max_steps=max_steps,
            data_root=data_root,
        )
        obs = worker.observation.replace("-= Welcome to TextWorld, ALFRED! =-\n\n", "")
        conversation.append({"role": "user", "content": obs})

        for _ in range(max_steps):
            if time.time() - started > deadline_s:
                fail_reason = "episode-deadline (%.0fs)" % deadline_s
                break
            reply = client.complete_target_messages(
                conversation, max_tokens=max_tokens, temperature=temperature)
            conversation.append({"role": "assistant", "content": reply or ""})
            command, parse_ok = parse_action(reply or "")
            if not parse_ok:
                n_parse_fail += 1

            admissible_trace.append(list(worker.admissible))
            event = worker.step(command)
            n_turns += 1
            feedback = str(event.get("obs", ""))
            if feedback.strip() == _NOTHING_HAPPENS:
                n_invalid += 1
            conversation.append({"role": "user", "content": feedback})
            if event.get("done"):
                done = True
                won = bool(event.get("won"))
                break

        if not fail_reason and not won:
            fail_reason = (
                "step-limit-reached (%d steps)" % n_turns if done or n_turns >= max_steps
                else "episode-ended-unwon")
    except Exception as e:  # noqa: BLE001 - an agent/env crash is a failed rollout
        fail_reason = "agent-error: %s: %s" % (type(e).__name__, e)
    finally:
        if worker is not None:
            worker.close()

    hard = int(won)
    return {
        "hard": hard,
        "soft": float(hard),
        "n_cases": 1,
        "n_pass": hard,
        "n_turns": n_turns,
        "fail_reason": "" if won else fail_reason,
        "conversation": conversation,
        # Env extras (absorbed into TaskResult.extras):
        "n_invalid": n_invalid,
        "n_parse_fail": n_parse_fail,
        "final_admissible": admissible_trace[-1] if admissible_trace else [],
        "elapsed_s": round(time.time() - started, 1),
    }
