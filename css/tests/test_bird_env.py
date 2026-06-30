"""End-to-end tests for the BIRD Text-to-SQL environment.

Exercises the whole Bird stack (function-call agent loop -> SQL execution -> EX
scoring -> canonical trajectory -> TaskResult) against a real temp SQLite DB,
with the target model driven by a scripted StubLLMClient. No network / no real
LLM endpoint.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile

import pytest

from css.config import CSSConfig
from css.envs.base import TaskEnv
from css.envs.bird.task_interface import BirdEnv, _skill_hash
from css.model.client import StubLLMClient, TargetOnlyClient
from css.trajectory import format_trajectory


def _make_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.executemany("INSERT INTO t (x) VALUES (?)", [(1,), (2,), (3,)])
    conn.commit()
    conn.close()


def _tool_call(name: str, sql: str) -> dict:
    return {
        "content": None,
        "tool_calls": [
            {
                "id": f"c_{name}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps({"sql": sql})},
            }
        ],
    }


class _ScriptedAgent:
    """Drives complete_target_tools: explore once, then submit ``final_sql``."""

    def __init__(self, final_sql: str) -> None:
        self.final_sql = final_sql
        self.calls = 0

    def __call__(self, messages, tools) -> dict:
        self.calls += 1
        if self.calls == 1:
            return _tool_call("execute_sql", "SELECT name FROM sqlite_master WHERE type='table'")
        return _tool_call("submit_final_sql", self.final_sql)


@pytest.fixture()
def bird_item():
    tmp = tempfile.mkdtemp(prefix="bird_test_")
    db_path = os.path.join(tmp, "testdb.sqlite")
    _make_db(db_path)
    item = {
        "id": "q1",
        "db_id": "testdb",
        "question": "How many rows are in t?",
        "evidence": "",
        "SQL": "SELECT COUNT(*) FROM t",  # gold -> [(3,)]
        "difficulty": "simple",
        "db_path": db_path,
    }
    yield tmp, item
    shutil.rmtree(tmp, ignore_errors=True)


def _run(tmp, item, final_sql):
    env = BirdEnv(CSSConfig(), items={"train": [item], "val": [], "test": []})
    client = TargetOnlyClient(StubLLMClient(target_tools_fn=_ScriptedAgent(final_sql)))
    return env, env.run_one(item, "## Skill\nthink first", client, tmp, rollout_index=0)


def test_bird_env_is_taskenv():
    env = BirdEnv(CSSConfig(), items={"train": [], "val": [], "test": []})
    assert isinstance(env, TaskEnv)


def test_bird_correct_sql_scores_ex_1(bird_item):
    tmp, item = bird_item
    _, res = _run(tmp, item, "SELECT COUNT(*) FROM t")
    assert res.hard == 1 and res.passed
    assert res.n_cases == 1 and res.n_pass == 1
    assert res.extras.get("predicted_sql") == "SELECT COUNT(*) FROM t"
    assert res.extras.get("gold_sql") == "SELECT COUNT(*) FROM t"


def test_bird_wrong_sql_scores_ex_0(bird_item):
    tmp, item = bird_item
    _, res = _run(tmp, item, "SELECT x FROM t")  # {(1,),(2,),(3,)} != gold {(3,)}
    assert res.hard == 0 and not res.passed
    assert res.fail_reason


def test_bird_trajectory_is_canonical_and_renders_actions(bird_item):
    tmp, item = bird_item
    _, res = _run(tmp, item, "SELECT COUNT(*) FROM t")
    # Canonical contract: every message is {role, content:str}.
    assert res.messages and all(
        isinstance(m.get("role"), str) and isinstance(m.get("content"), str)
        for m in res.messages
    )
    text = format_trajectory(res.messages)
    # The agent's flattened actions + observations are visible to the mechanism.
    assert "Action: execute_sql" in text
    assert "Action: submit_final_sql" in text
    assert "Observation:" in text
    # Gold SQL must never appear anywhere in the trajectory.
    assert "SELECT COUNT(*) FROM t" in text  # predicted (allowed)
    # (gold == predicted here; the no-gold guarantee is covered by the prompt test)


def test_bird_no_gold_in_trajectory(bird_item):
    tmp, item = bird_item
    # Distinct gold so we can assert it never leaks into the agent transcript.
    item = dict(item, SQL="SELECT 99 AS hidden_gold_marker")
    _, res = _run(tmp, item, "SELECT x FROM t")
    text = format_trajectory(res.messages)
    assert "hidden_gold_marker" not in text
    assert "99" not in text


def test_split_loads_with_per_split_db_root():
    """The committed split loads; train/val resolve to train DBs, test to dev DBs."""
    base = os.path.join(os.path.dirname(__file__), "..", "..",
                        "data", "bird_split_filtered_seed42")
    if not os.path.exists(base):
        pytest.skip("bird split not present")
    cfg = CSSConfig(
        n_train=5, n_val=3, n_test=4, split_dir=base, data_root="/TRAIN_DB",
        extra={"bird_test_db_root": "/DEV_DB"},
    )
    env = BirdEnv(cfg)
    tr, va, te = env.train_items(), env.val_items(), env.test_items()
    assert len(tr) == 5 and len(va) == 3 and len(te) == 4
    assert tr[0]["db_path"].startswith("/TRAIN_DB/")
    assert va[0]["db_path"].startswith("/TRAIN_DB/")
    assert te[0]["db_path"].startswith("/DEV_DB/")
    # Gold SQL is carried for evaluation (kept out of prompts; see prompt tests).
    assert tr[0].get("SQL") and te[0].get("SQL")


def test_bird_load_cached_result_roundtrip(bird_item):
    tmp, item = bird_item
    env, res = _run(tmp, item, "SELECT COUNT(*) FROM t")
    sh = _skill_hash("## Skill\nthink first")
    cached = env.load_cached_result(item, tmp, rollout_index=0, skill_hash=sh)
    assert cached is not None and cached.hard == 1
    # Wrong skill_hash -> cache miss.
    assert env.load_cached_result(item, tmp, rollout_index=0, skill_hash="deadbeef") is None
