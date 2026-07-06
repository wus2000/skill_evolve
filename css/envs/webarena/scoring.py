"""Offline scoring via the WebArena-Verified evaluator.

Scoring inputs are produced by the episode itself (agent_response.json +
network.har) and evaluation runs DETACHED from the live sites — the property
that makes lane refreshing safe to do immediately after an episode ends
(PREP §5). The evaluator ships as the ``webarena-verified`` package
(py3.11+, CLI: ``webarena-verified eval-tasks``, docs v1.2.3); we invoke the
CLI in a subprocess so the rollout workers keep zero import-time dependency
on it. The exact per-task file layout the CLI expects is pinned during P0
against the installed version; ``layout`` centralizes that knob.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess

_log = logging.getLogger("css.webarena")


class VerifiedScorer:
    """Wraps ``webarena-verified eval-tasks`` for one-task offline scoring."""

    def __init__(self, cli: str, config_path: str,
                 timeout_s: int = 300, layout: str = "flat") -> None:
        # cli: full path to the webarena-verified entry point (venv bin).
        # config_path: environments config JSON (URL placeholder mapping).
        self.cli = cli
        self.config_path = config_path
        self.timeout_s = timeout_s
        self.layout = layout

    def score(self, task_id: int, workdir: str) -> dict:
        """Score one episode; ``workdir`` holds agent_response.json + network.har.

        Returns {"hard": 0|1, "detail": {...}}; never raises for a scoring
        failure (a failed evaluation is a scored-0 with diagnostics — the
        rollout batch layer requires run_one to stay non-throwing).
        """
        cmd = [self.cli, "eval-tasks",
               "--task-ids", str(task_id),
               "--output-dir", workdir,
               "--config", self.config_path]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            return {"hard": 0, "detail": {"error": "evaluator timeout",
                                          "cmd": " ".join(cmd)}}
        except OSError as exc:
            return {"hard": 0, "detail": {"error": f"evaluator launch: {exc}"}}

        result = self._read_eval_result(task_id, workdir)
        if result is None:
            return {"hard": 0, "detail": {
                "error": "no eval_result produced",
                "returncode": proc.returncode,
                "stderr": (proc.stderr or "")[-2000:]}}
        hard = 1 if float(result.get("score", 0.0)) >= 1.0 else 0
        return {"hard": hard, "detail": result}

    def _read_eval_result(self, task_id: int, workdir: str) -> "dict | None":
        # v1.2.3 writes eval_result.json under the task's output dir; accept
        # both flat and per-task-subdir layouts until P0 pins one.
        candidates = [
            os.path.join(workdir, "eval_result.json"),
            os.path.join(workdir, str(task_id), "eval_result.json"),
            os.path.join(workdir, f"task_{task_id}", "eval_result.json"),
        ]
        for path in candidates:
            if os.path.exists(path):
                try:
                    with open(path) as f:
                        return json.load(f)
                except (OSError, json.JSONDecodeError) as exc:
                    _log.warning("webarena/scoring — unreadable %s: %s", path, exc)
        return None


def write_agent_response(workdir: str, payload: dict) -> str:
    """Persist the structured final answer where the evaluator expects it."""
    os.makedirs(workdir, exist_ok=True)
    path = os.path.join(workdir, "agent_response.json")
    with open(path, "w") as f:
        json.dump(payload, f, indent=1)
    return path
