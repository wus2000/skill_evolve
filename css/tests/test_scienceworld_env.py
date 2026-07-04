"""Contract tests for the ScienceWorld env — runnable WITHOUT scienceworld/JVM.

The JVM half is exercised by the live integration check
(env_candidates/scienceworld_smoke/integration_check/); here we verify everything
mechanism-facing with a fake in-process env injected into the pool: prompt
composition, split loading + determinism, both scoring protocols through run_one,
the eval_splits/extra_metrics hooks, the GT firewall, and the resume cache.
"""
from __future__ import annotations

import os

import pytest

from css.config import CSSConfig
from css.data.rollout import TaskResult
from css.envs.registry import build_env
from css.envs.scienceworld.prompts import build_system_prompt
from css.envs.scienceworld.task_interface import ScienceworldEnv


def _cfg(**overrides) -> CSSConfig:
    base = dict(
        env_name="scienceworld",
        n_train=0, n_val=0, n_test=0,
        split_dir="/nonexistent/sw_split",
        data_root="/nonexistent/sw_data",
        max_api_workers=4,
    )
    base.update(overrides)
    cfg = CSSConfig(**base)
    cfg.extra.setdefault("scienceworld_pool_size", 2)
    return cfg


# ── Fake in-process env (no JVM) ────────────────────────────────────────────
class _FakeSWEnv:
    """Scripted stand-in for ScienceWorldEnv. ``steps`` = [(obs, score, done)]."""

    def __init__(self, steps, *, gold=("focus on water", "activate stove"),
                 desc="Your task is to test."):
        self.steps = list(steps)
        self.i = 0
        self._gold = gold
        self.desc = desc
        self.loaded = None
        self.gold_gen = False

    def load(self, task, var, simpl, generateGoldPath=False):
        self.loaded = (task, var, simpl)
        self.gold_gen = generateGoldPath

    def reset(self):
        return "You are in the hallway.", {"score": 0}

    def inventory(self):
        return "You have nothing."

    def get_task_description(self):
        return self.desc

    def get_possible_actions(self):
        return ["look around", "focus on OBJ", "activate OBJ", "reset the task"]

    def get_goal_progress(self):
        return "Subgoal 1: (done)\nSubgoal 2: (not done)"

    def get_gold_action_sequence(self):
        return list(self._gold) if self.gold_gen else ["ERROR: not generated"]

    def step(self, action):
        obs, score, done = self.steps[min(self.i, len(self.steps) - 1)]
        self.i += 1
        return obs, 0, bool(done), {"score": score}

    def close(self):
        pass


def _env_with_fake(items, steps, **cfg_over):
    env = ScienceworldEnv(_cfg(**cfg_over), items=items)
    fake = _FakeSWEnv(steps)
    env._pool._factory = lambda step_limit: fake
    return env, fake


_NATIVE = {"id": "tr1", "task_name": "boil", "variation": 3,
           "protocol": "original", "task_type": "boil"}
_AB = {"id": "ab1", "task_name": "find-animal", "variation": 5,
       "protocol": "agentboard", "task_type": "find-animal", "difficulty": "hard",
       "goal": "find the animal with the longest life span",
       "subgoals": ["You move to the outside", "You focus on the crocodile egg"]}


class _ScriptedClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def complete_target_messages(self, messages, *, max_tokens=0, temperature=0.0):
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return reply


# ── Prompt composition ──────────────────────────────────────────────────────
def test_system_prompt_contains_grammar_and_skill():
    text = build_system_prompt("## Strategy\nFocus deliberately.")
    assert "focus on OBJ" in text
    assert "Focus deliberately." in text
    assert "<action>" in text
    assert "<reasoning>" in text and "<think>" not in text


def test_system_prompt_empty_skill_has_no_block():
    assert "Skill document" not in build_system_prompt("")


def test_registry_builds_scienceworld():
    env = build_env(_cfg(), items={"train": [], "val": [], "test": []})
    assert isinstance(env, ScienceworldEnv)
    assert "action" in env.action_space_description().lower()


# ── Split loading + determinism + no overlap ────────────────────────────────
def test_split_loading_determinism_and_no_overlap():
    items = {
        "train": [{"id": "t%d" % i, "task_name": "boil", "variation": i,
                   "protocol": "original"} for i in range(5)],
        "val": [{"id": "v1", "task_name": "boil", "variation": 20, "protocol": "original"}],
        "test": [dict(_AB)],
        "test_secondary": [{"id": "s1", "task_name": "boil", "variation": 90,
                            "protocol": "original"}],
    }
    env = ScienceworldEnv(_cfg(), items=items)
    assert [i["id"] for i in env.train_items()] == [i["id"] for i in env.train_items()]
    assert len(env.train_items()) == 5 and len(env.val_items()) == 1
    assert len(env.test_items()) == 1 and len(env.test_secondary_items()) == 1
    train_pairs = {(i["task_name"], i["variation"]) for i in env.train_items()}
    test_pairs = {(i["task_name"], i["variation"]) for i in env.test_items()}
    sec_pairs = {(i["task_name"], i["variation"]) for i in env.test_secondary_items()}
    assert not (train_pairs & test_pairs) and not (train_pairs & sec_pairs)


def test_split_slicing_knob():
    items = {"train": [{"id": "t%d" % i, "task_name": "boil", "variation": i,
                        "protocol": "original"} for i in range(10)]}
    env = ScienceworldEnv(_cfg(n_train=4), items=items)
    assert len(env.train_items()) == 4


# ── run_one: native success / negative / agentboard ─────────────────────────
def test_run_one_native_success_and_gt_firewall(tmp_path):
    env, _ = _env_with_fake(
        {"train": [_NATIVE]},
        [("You focus on the water.", 50, False), ("The water is now boiling.", 100, True)])
    client = _ScriptedClient([
        "<reasoning>focus</reasoning><action>focus on water</action>",
        "<reasoning>heat</reasoning><action>activate stove</action>"])
    res = env.run_one(_NATIVE, "SKILLTEXT", client, str(tmp_path))
    assert res.hard == 1 and res.soft == 1.0 and res.n_turns == 2
    roles = [m["role"] for m in res.messages]
    assert roles[0] == "system" and roles[-1] == "evaluation"
    agent_visible = "\n".join(m["content"] for m in res.messages[:-1])
    assert "Gold action sequence" not in agent_visible          # firewalled during rollout
    assert "Gold action sequence" in res.messages[-1]["content"]  # exposed to analysis only
    assert res.extras["native_score"] == 100


def test_run_one_native_negative_score_is_failure(tmp_path):
    env, _ = _env_with_fake({"train": [_NATIVE]},
                            [("That was wrong.", -100, True)])
    client = _ScriptedClient(["<action>focus on lava</action>"])
    res = env.run_one(_NATIVE, "", client, str(tmp_path))
    assert res.hard == 0 and res.soft == 0.0
    assert "score=-100" in res.messages[-1]["content"]


def test_run_one_agentboard_sr_pr(tmp_path):
    env, _ = _env_with_fake(
        {"test": [_AB]},
        [("You move to the outside.", 0, False),
         ("You focus on the crocodile egg.", 0, False)])
    client = _ScriptedClient([
        "<action>go to outside</action>", "<action>focus on crocodile egg</action>"])
    res = env.run_one(_AB, "", client, str(tmp_path))
    assert res.hard == 1 and res.soft == pytest.approx(1.0)
    assert res.extras["agentboard_sr"] == 1 and res.extras["agentboard_pr"] == pytest.approx(1.0)
    assert res.extras["protocol"] == "agentboard" and res.extras["difficulty"] == "hard"


def test_run_one_agentboard_partial_progress(tmp_path):
    env, _ = _env_with_fake(
        {"test": [_AB]},
        [("You move to the outside.", 0, False), ("Nothing happens.", 0, False)])
    client = _ScriptedClient(["<action>go to outside</action>", "<action>look around</action>"])
    res = env.run_one(_AB, "", client, str(tmp_path))
    assert res.hard == 0 and res.soft == pytest.approx(0.5)   # 1/2 subgoals


def test_run_one_check_valid_actions_intercepted(tmp_path):
    env, fake = _env_with_fake(
        {"train": [_NATIVE]},
        [("You focus on the water.", 100, True)])
    client = _ScriptedClient([
        "<action>check valid actions</action>",           # intercepted: no sim move
        "<action>focus on water</action>"])
    res = env.run_one(_NATIVE, "", client, str(tmp_path))
    assert res.extras["n_check_valid"] == 1
    # the check-valid turn produced a templates message, no env step consumed
    contents = [m["content"] for m in res.messages if m["role"] == "user"]
    assert any("Choose an action from these valid actions" in c for c in contents)
    assert res.hard == 1  # the following real action solved it


def test_run_one_engine_error_is_failed_rollout(tmp_path):
    env = ScienceworldEnv(_cfg(), items={"train": [_NATIVE]})

    class _Boom(_FakeSWEnv):
        def load(self, *a, **k):
            raise RuntimeError("jvm boom")

    env._pool._factory = lambda step_limit: _Boom([])
    res = env.run_one(_NATIVE, "", _ScriptedClient(["x"]), str(tmp_path))
    assert res.hard == 0 and "engine-error" in res.messages[-1]["content"].lower()


# ── Reporting hooks ─────────────────────────────────────────────────────────
def test_eval_splits_primary_first():
    env = ScienceworldEnv(_cfg(), items={"test": [dict(_AB)],
                                         "test_secondary": [{"id": "s1", "task_name": "boil",
                                                             "variation": 1, "protocol": "original"}]})
    splits = env.eval_splits()
    assert [name for name, _ in splits] == ["agentboard", "native_secondary"]


def test_extra_metrics_sr_pr_and_avg_score():
    env = ScienceworldEnv(_cfg(), items={"train": []})
    ab = [
        TaskResult(task_id="a", hard=1, soft=1.0, extras={"agentboard_sr": 1, "difficulty": "easy"}),
        TaskResult(task_id="b", hard=0, soft=0.5, extras={"agentboard_sr": 0, "difficulty": "hard"}),
    ]
    m = env.extra_metrics(ab)
    assert m["SR"] == pytest.approx(0.5) and m["PR"] == pytest.approx(0.75)
    assert m["SR_easy"] == 1.0 and m["PR_hard"] == 0.5
    native = [TaskResult(task_id="n", hard=0, soft=0.6, extras={})]
    assert env.extra_metrics(native)["avg_score"] == pytest.approx(60.0)


# ── Resume cache round-trip ─────────────────────────────────────────────────
def test_cache_round_trip(tmp_path):
    env, _ = _env_with_fake({"train": [_NATIVE]},
                            [("The water is boiling.", 100, True)])
    from css.envs.common import skill_hash
    env.run_one(_NATIVE, "SKILLTEXT", _ScriptedClient(["<action>activate stove</action>"]),
                str(tmp_path))
    cached = env.load_cached_result(_NATIVE, str(tmp_path), rollout_index=0,
                                    skill_hash=skill_hash("SKILLTEXT"))
    assert cached is not None and cached.hard == 1
    assert env.load_cached_result(_NATIVE, str(tmp_path), rollout_index=0,
                                  skill_hash=skill_hash("OTHER")) is None
