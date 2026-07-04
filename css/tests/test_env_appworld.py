"""Contract tests for the AppWorld env (stub worker; appworld NOT required).

Mirrors test_env_alfworld.py: parse/prompt assertions, split accessors, worker
stdio protocol against a stub script, and run_one end-to-end with a scripted
LLM + in-process fake worker — asserting the trajectory contract, the GT
firewall, the three-way termination attribution, and the cache roundtrip.
"""
from __future__ import annotations

import json
import os
import sys
import textwrap

import pytest

from css.config import CSSConfig
from css.envs.appworld.agent import AppworldWorker, parse_code
from css.envs.appworld.prompts import (
    ACTION_SPACE_DESCRIPTION,
    build_first_user,
    build_system_prompt,
)
from css.envs.appworld.task_interface import AppworldEnv


def _cfg(**extra_overrides) -> CSSConfig:
    extra = {
        "appworld_python": sys.executable,
        "appworld_root": "/tmp/aw_root_unused",
        "appworld_max_interactions": 10,
        "appworld_max_tokens": 512,
        "appworld_temperature": 0.0,
        "appworld_obs_max_chars": 6000,
        "appworld_engine_slots": 4,
        "appworld_gt_mode": "solution",
    }
    extra.update(extra_overrides)
    return CSSConfig(
        env_name="appworld", n_train=0, n_val=0, n_test=0,
        k_rollouts=1, max_api_workers=2, task_timeout_s=120,
        data_root="/tmp/aw_root_unused", extra=extra,
    )


# ── parse_code ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("reply,expected,ok", [
    ("```python\nprint(1)\n```", "print(1)", True),
    ("```py\nx = 2\nprint(x)\n```", "x = 2\nprint(x)", True),
    ("prose then\n```python\napis.a.b()\n```\nmore prose", "apis.a.b()", True),
    ("print('no fence')", "print('no fence')", True),   # official raw-code convention
    ("```python\n```", "", False),                       # empty fence
    ("", "", False),
    ("   \n  ", "", False),
])
def test_parse_code(reply, expected, ok):
    code, parse_ok = parse_code(reply)
    assert (code, parse_ok) == (expected, ok)


# ── Prompts ──────────────────────────────────────────────────────────────────
def test_system_prompt_contains_protocol_and_skill():
    text = build_system_prompt("MY SKILL DOC")
    assert "apis.api_docs.show_app_descriptions()" in text
    assert "complete_task" in text
    assert "ONE fenced Python code block" in text
    assert "MY SKILL DOC" in text
    assert "Skill document" in text
    # GT firewall: nothing about difficulty/gold leaks into the agent prompt.
    assert "difficulty" not in text.lower()
    assert "ground truth" not in text.lower()


def test_system_prompt_empty_skill_has_no_empty_block():
    text = build_system_prompt("")
    assert "Skill document" not in text


def test_first_user_contains_supervisor_and_instruction():
    text = build_first_user("Pay my rent.", {
        "first_name": "A", "last_name": "B",
        "email": "a@b.c", "phone_number": "123"})
    assert "A B" in text and "a@b.c" in text and "123" in text
    assert "Pay my rent." in text


def test_action_space_description_is_structural_only():
    assert "self-declared" in ACTION_SPACE_DESCRIPTION.lower()
    assert "state-based" in ACTION_SPACE_DESCRIPTION.lower()
    assert "api_docs" in ACTION_SPACE_DESCRIPTION


# ── Splits ───────────────────────────────────────────────────────────────────
def _write_datasets(tmp_path):
    d = tmp_path / "data" / "datasets"
    d.mkdir(parents=True)
    (d / "train.txt").write_text("\n".join(f"s{i}_1" for i in range(9)) + "\n")
    (d / "dev.txt").write_text("\n".join(f"d{i}_1" for i in range(5)) + "\n")
    (d / "test_normal.txt").write_text("\n".join(f"tn{i}_1" for i in range(7)) + "\n")
    (d / "test_challenge.txt").write_text("\n".join(f"tc{i}_1" for i in range(4)) + "\n")
    return str(tmp_path)


def test_split_files_and_accessors(tmp_path):
    root = _write_datasets(tmp_path)
    cfg = _cfg()
    env = AppworldEnv(cfg, data_root=root)
    assert [it["id"] for it in env.train_items()][:2] == ["s0_1", "s1_1"]
    assert len(env.train_items()) == 9
    assert len(env.val_items()) == 5          # val == dev
    assert len(env.test_items()) == 7         # test == test_normal
    assert len(env.test_challenge_items()) == 4
    assert env.train_items()[0]["split"] == "train"
    assert env.val_items()[0]["split"] == "val"


def test_split_slicing_knobs(tmp_path):
    root = _write_datasets(tmp_path)
    cfg = _cfg()
    cfg.n_train, cfg.n_val, cfg.n_test = 3, 2, 0
    env = AppworldEnv(cfg, data_root=root)
    assert len(env.train_items()) == 3
    assert len(env.val_items()) == 2
    assert len(env.test_items()) == 7          # 0 = use all
    assert len(env.test_challenge_items()) == 4  # never sliced


def test_registry_builds_appworld(tmp_path):
    from css.envs.registry import build_env
    root = _write_datasets(tmp_path)
    cfg = _cfg()
    cfg.data_root = root
    env = build_env(cfg)
    assert isinstance(env, AppworldEnv)


# ── Worker stdio protocol (stub worker; JSON requests) ──────────────────────
_STUB_WORKER = textwrap.dedent("""
    import json, sys
    print(json.dumps({"event": "ready", "instruction": "Find the answer.",
                      "supervisor": {"first_name": "A", "last_name": "B",
                                     "email": "a@b.c", "phone_number": "1"},
                      "metadata": {"difficulty": 2}}), flush=True)
    completed = False
    for line in sys.stdin:
        line = line.rstrip("\\n")
        if line == "__CLOSE__":
            break
        req = json.loads(line)
        if req["op"] == "execute":
            completed = completed or ("complete_task" in req["code"])
            print(json.dumps({"event": "execute_result",
                              "output": "OUT:" + req["code"][:20],
                              "completed": completed}), flush=True)
        elif req["op"] == "evaluate":
            print(json.dumps({"event": "evaluation", "success": completed,
                              "report": {"failures": [] if completed else ["r1"]}}),
                  flush=True)
        elif req["op"] == "gold":
            print(json.dumps({"event": "gold", "solution_code": "GOLD()",
                              "answer": "42"}), flush=True)
""")


@pytest.fixture()
def stub_worker(tmp_path):
    path = tmp_path / "stub_appworld_worker.py"
    path.write_text(_STUB_WORKER, encoding="utf-8")
    return str(path)


def test_worker_protocol_roundtrip(stub_worker):
    w = AppworldWorker(
        "t1_1", python_exe=sys.executable, worker_script=stub_worker,
        appworld_root="", experiment_name="x", ground_truth_mode="full",
        max_interactions=10, obs_max_chars=6000)
    assert w.instruction == "Find the answer."
    assert w.metadata == {"difficulty": 2}
    ev = w.execute("print(1)")
    assert ev["output"].startswith("OUT:") and not ev["completed"]
    ev = w.execute("apis.supervisor.complete_task()")
    assert ev["completed"]
    ev = w.evaluate()
    assert ev["success"] is True
    gold = w.gold()
    assert gold["solution_code"] == "GOLD()" and gold["answer"] == "42"
    w.close()
    assert w.proc.poll() is not None


def test_worker_fatal_on_bad_script(tmp_path):
    bad = tmp_path / "bad_worker.py"
    bad.write_text("import json\n"
                   "print(json.dumps({'event':'fatal','error':'boom'}), flush=True)\n",
                   encoding="utf-8")
    with pytest.raises(RuntimeError):
        AppworldWorker(
            "t", python_exe=sys.executable, worker_script=str(bad),
            appworld_root="", experiment_name="x", ground_truth_mode="minimal",
            max_interactions=5, obs_max_chars=100)


# ── run_one end-to-end with a scripted client + fake worker ────────────────
class _ScriptedClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def complete_target_messages(self, messages, *, max_tokens=0, temperature=0.0):
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return reply


class _FakeWorker:
    """In-process stand-in for AppworldWorker (no subprocess)."""

    last_kwargs: dict = {}
    eval_success_when_declared = True

    def __init__(self, task_id, **kwargs):
        _FakeWorker.last_kwargs = dict(kwargs, task_id=task_id)
        self.instruction = "Find my spotify password."
        self.supervisor = {"first_name": "A", "last_name": "B",
                           "email": "a@b.c", "phone_number": "1"}
        self.metadata = {"difficulty": 1}
        self._completed = False

    def execute(self, code):
        if "complete_task" in code:
            self._completed = True
        out = "Traceback (most recent call last)\nboom" if "raise" in code \
            else "ok-output"
        return {"output": out, "completed": self._completed}

    def evaluate(self):
        ok = self._completed and _FakeWorker.eval_success_when_declared
        return {"success": ok,
                "report": {"failures": [] if ok else ["requirement r1 failed"]}}

    def gold(self):
        return {"solution_code": "def solution(apis, requester): pass",
                "answer": None}

    def close(self):
        pass


@pytest.fixture()
def fake_worker(monkeypatch):
    import css.envs.appworld.agent as agent_mod
    _FakeWorker.eval_success_when_declared = True
    monkeypatch.setattr(agent_mod, "AppworldWorker", _FakeWorker)


def _train_item():
    return {"id": "82e2fac_1", "split": "train"}


def test_run_one_success_trajectory_gt_firewall_and_cache(tmp_path, fake_worker):
    env = AppworldEnv(_cfg(), items={"train": [_train_item()], "val": [], "test": []})
    client = _ScriptedClient([
        "```python\nprint(apis.api_docs.show_app_descriptions())\n```",
        "```python\napis.supervisor.complete_task(answer='x')\n```",
    ])
    out_dir = str(tmp_path / "out")
    res = env.run_one(_train_item(), "SKILL TEXT", client, out_dir,
                      rollout_index=0, epoch=0, node_id="n0000")
    assert res.hard == 1 and res.passed
    msgs = res.messages
    assert msgs[0]["role"] == "system"
    assert msgs[-1]["role"] == "evaluation"
    # Trajectory contract: flat {role, content:str} throughout.
    assert all(isinstance(m["content"], str) for m in msgs)
    # GT firewall: gold appears ONLY in the evaluation annotation.
    agent_visible = "".join(m["content"] for m in msgs if m["role"] != "evaluation")
    assert "def solution" not in agent_visible
    assert "Gold solution code" in msgs[-1]["content"]
    # Requirement report and counters ride in the annotation detail.
    assert "declared=True" in msgs[-1]["content"]
    # gt firewall input side: worker opened in "full" mode only for train/dev.
    assert _FakeWorker.last_kwargs["ground_truth_mode"] == "full"
    # Cache roundtrip: same skill hits, different skill misses.
    hit = env.load_cached_result(_train_item(), out_dir, rollout_index=0,
                                 skill_hash=__import__("css.envs.common",
                                                       fromlist=["skill_hash"]
                                                       ).skill_hash("SKILL TEXT"))
    assert hit is not None and hit.hard == 1
    miss = env.load_cached_result(_train_item(), out_dir, rollout_index=0,
                                  skill_hash="different")
    assert miss is None


def test_run_one_never_declared(tmp_path, fake_worker):
    cfg = _cfg()
    cfg.extra["appworld_max_interactions"] = 3
    env = AppworldEnv(cfg, items={"train": [_train_item()], "val": [], "test": []})
    client = _ScriptedClient(["```python\nprint('loop')\n```"])
    res = env.run_one(_train_item(), "S", client, str(tmp_path / "o"))
    assert res.hard == 0
    assert "never-declared" in res.fail_reason


def test_run_one_declared_but_failed(tmp_path, fake_worker):
    _FakeWorker.eval_success_when_declared = False
    env = AppworldEnv(_cfg(), items={"train": [_train_item()], "val": [], "test": []})
    client = _ScriptedClient(["```python\napis.supervisor.complete_task()\n```"])
    res = env.run_one(_train_item(), "S", client, str(tmp_path / "o"))
    assert res.hard == 0
    assert res.fail_reason == "declared-but-failed"
    assert "requirement r1 failed" in res.messages[-1]["content"]


def test_run_one_parse_fail_feedback_and_counters(tmp_path, fake_worker):
    cfg = _cfg()
    cfg.extra["appworld_max_interactions"] = 3
    env = AppworldEnv(cfg, items={"train": [_train_item()], "val": [], "test": []})
    client = _ScriptedClient([
        "",  # no code -> parse_fail + corrective feedback, consumes a turn
        "```python\nraise ValueError()\n```",   # -> traceback counted
        "```python\napis.supervisor.complete_task()\n```",
    ])
    res = env.run_one(_train_item(), "S", client, str(tmp_path / "o"))
    assert res.extras["n_parse_fail"] == 1
    assert res.extras["n_tracebacks"] == 1
    assert any("no code" in m["content"] for m in res.messages if m["role"] == "user")


def test_run_one_test_split_opens_minimal_and_no_gold(tmp_path, fake_worker):
    env = AppworldEnv(_cfg(), items={"train": [], "val": [],
                                     "test": [{"id": "t9_1", "split": "test"}]})
    client = _ScriptedClient(["```python\napis.supervisor.complete_task()\n```"])
    res = env.run_one({"id": "t9_1", "split": "test"}, "S", client,
                      str(tmp_path / "o"))
    assert _FakeWorker.last_kwargs["ground_truth_mode"] == "minimal"
    assert "Gold solution code" not in res.messages[-1]["content"]


def test_run_one_gt_mode_tests_omits_gold(tmp_path, fake_worker):
    env = AppworldEnv(_cfg(appworld_gt_mode="tests"),
                      items={"train": [_train_item()], "val": [], "test": []})
    client = _ScriptedClient(["```python\napis.supervisor.complete_task()\n```"])
    res = env.run_one(_train_item(), "S", client, str(tmp_path / "o"))
    ann = res.messages[-1]["content"]
    assert "Gold solution code" not in ann
    assert "Per-requirement evaluation report" in ann


def test_scenario_id_grouping():
    sys.path.insert(0, os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))
    from run_test_eval_appworld import _scenario_id
    assert _scenario_id("82e2fac_1") == "82e2fac"
    assert _scenario_id("82e2fac_12") == "82e2fac"
    assert _scenario_id("noscenario") == "noscenario"
