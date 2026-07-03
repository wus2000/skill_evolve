"""Contract tests for the ALFWorld env — runnable WITHOUT textworld installed.

The engine-dependent half (worker.py's actual game loop) is exercised by the
server/local smoke script; here we verify everything mechanism-facing:
action parsing, split loading, the stdio worker protocol (via a stub worker
script), run_one's TaskResult construction, the GT firewall, and the resume
cache round-trip.
"""
from __future__ import annotations

import json
import os
import sys
import textwrap

import pytest

from css.config import CSSConfig
from css.envs.alfworld.agent import AlfredWorker, parse_action
from css.envs.alfworld.prompts import build_system_prompt
from css.envs.alfworld.task_interface import AlfworldEnv
from css.envs.registry import build_env

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SPLIT_DIR = os.path.join(REPO_ROOT, "data", "alfworld_split_seed42")


def _cfg(**overrides) -> CSSConfig:
    base = dict(
        env_name="alfworld",
        n_train=0, n_val=0, n_test=0,
        split_dir=SPLIT_DIR,
        data_root="/nonexistent/alfworld_data",
    )
    base.update(overrides)
    return CSSConfig(**base)


# ── Action parsing ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("reply,expected,ok", [
    ("<reasoning>x</reasoning><action>go to fridge 1</action>", "go to fridge 1", True),
    ("<think>x</think><action>go to fridge 1</action>", "go to fridge 1", True),
    ("<ACTION>Open Fridge 1</ACTION>", "open fridge 1", True),
    ("<action> 'take bread 1 from countertop 3'. </action>", "take bread 1 from countertop 3", True),
    ("<action>\n\nmove bread 1 to countertop 1\nextra line</action>", "move bread 1 to countertop 1", True),
    ("<action>cool bread 1 with fridge 1</action><action>look</action>", "cool bread 1 with fridge 1", True),
    ("no tags at all", "look", False),
    ("<action>   </action>", "look", False),
])
def test_parse_action(reply, expected, ok):
    command, parse_ok = parse_action(reply)
    assert command == expected
    assert parse_ok is ok


# ── Prompt composition ─────────────────────────────────────────────────────
def test_system_prompt_contains_grammar_and_skill():
    text = build_system_prompt("## Strategy\nSearch systematically.")
    assert "move (object) to (receptacle)" in text
    assert "Search systematically." in text
    assert "<action>" in text
    # <reasoning>, NOT <think>: the endpoint strips the Qwen reserved token.
    assert "<reasoning>" in text and "<think>" not in text
    # No admissible-commands leakage in the static prompt.
    assert "admissible" not in text.lower()


def test_system_prompt_empty_skill_has_no_empty_block():
    text = build_system_prompt("")
    assert "Skill document" not in text


# ── Split loading ──────────────────────────────────────────────────────────
@pytest.mark.skipif(not os.path.isdir(SPLIT_DIR), reason="split files not generated")
def test_split_counts_and_ids():
    env = AlfworldEnv(_cfg())
    train, val, test = env.train_items(), env.val_items(), env.test_items()
    assert len(train) == 3153 and len(val) == 400 and len(test) == 134
    assert len(env.test_seen_items()) == 140
    ids = [i["id"] for i in train + val + test]
    assert len(ids) == len(set(ids))
    # val was carved from the train pool: ids share the train prefix,
    # and no gamefile overlaps between train and val.
    assert all(i["id"].startswith("atrain_") for i in val)
    assert not {i["gamefile"] for i in train} & {i["gamefile"] for i in val}
    assert all(i["id"].startswith("aunseen_") for i in test)


@pytest.mark.skipif(not os.path.isdir(SPLIT_DIR), reason="split files not generated")
def test_split_slicing_knobs():
    env = AlfworldEnv(_cfg(n_train=100, n_val=40, n_test=20))
    assert len(env.train_items()) == 100
    assert len(env.val_items()) == 40
    assert len(env.test_items()) == 20


def test_registry_builds_alfworld():
    env = build_env(_cfg(), items={"train": [], "val": [], "test": []})
    assert isinstance(env, AlfworldEnv)
    assert "one command per turn" in env.action_space_description().lower() or \
        "<action>" in env.action_space_description()


# ── Worker stdio protocol (stub worker, no textworld needed) ───────────────
_STUB_WORKER = textwrap.dedent("""
    import json, sys
    print(json.dumps({"event": "ready", "obs": "You are in a room. Your task is to: test.",
                      "admissible": ["look", "go to desk 1"]}), flush=True)
    for line in sys.stdin:
        cmd = line.strip()
        if cmd == "__CLOSE__":
            break
        won = cmd == "use lamp 1"
        print(json.dumps({"event": "step", "obs": "OK: " + cmd, "done": won,
                          "won": won, "admissible": ["look"]}), flush=True)
""")


@pytest.fixture()
def stub_worker(tmp_path):
    path = tmp_path / "stub_worker.py"
    path.write_text(_STUB_WORKER, encoding="utf-8")
    return str(path)


def test_worker_protocol_roundtrip(stub_worker):
    w = AlfredWorker("game.tw-pddl", python_exe=sys.executable, max_steps=50,
                     data_root="", worker_script=stub_worker)
    assert "Your task is to" in w.observation
    assert w.admissible == ["look", "go to desk 1"]
    ev = w.step("go to desk 1")
    assert ev["obs"] == "OK: go to desk 1" and not ev["done"]
    ev = w.step("use lamp 1")
    assert ev["done"] and ev["won"]
    w.close()
    assert w.proc.poll() is not None


_STUB_GOLD_WORKER = textwrap.dedent("""
    import json, sys
    assert sys.argv[3] == "--gold", sys.argv
    print(json.dumps({"event": "gold", "won": True, "steps": [
        {"action": "go to desk 1", "obs": "You arrive at desk 1."},
        {"action": "use lamp 1", "obs": "You turn on the lamp 1."},
    ]}), flush=True)
""")


def test_gold_replay_protocol(tmp_path):
    from css.envs.alfworld.agent import run_gold_replay
    path = tmp_path / "stub_gold.py"
    path.write_text(_STUB_GOLD_WORKER, encoding="utf-8")
    gold = run_gold_replay("game.tw-pddl", python_exe=sys.executable,
                           data_root="", worker_script=str(path))
    assert gold["won"] is True and len(gold["steps"]) == 2
    assert gold["steps"][1]["action"] == "use lamp 1"


def test_worker_fatal_on_bad_script(tmp_path):
    bad = tmp_path / "bad_worker.py"
    bad.write_text("import json,sys\n"
                   "print(json.dumps({'event':'fatal','error':'boom'}), flush=True)\n",
                   encoding="utf-8")
    with pytest.raises(RuntimeError):
        AlfredWorker("g", python_exe=sys.executable, max_steps=5,
                     data_root="", worker_script=str(bad))


# ── run_one end-to-end with a scripted client + fake worker ────────────────
class _ScriptedClient:
    """Deterministic LLM double: replays a fixed action script."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def complete_target_messages(self, messages, *, max_tokens=0, temperature=0.0):
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return reply


class _FakeWorker:
    """In-process stand-in for AlfredWorker (no subprocess)."""

    def __init__(self, gamefile, **kwargs):
        self.observation = ("-= Welcome to TextWorld, ALFRED! =-\n\n"
                            "You are in a room. Your task is to: examine the lamp.")
        self.admissible = ["look", "go to desk 1", "use lamp 1"]
        self._steps = 0

    def step(self, command):
        self._steps += 1
        if command == "use lamp 1":
            return {"event": "step", "obs": "You use the lamp 1.", "done": True,
                    "won": True, "admissible": []}
        if command == "look":
            return {"event": "step", "obs": "Nothing happens.", "done": False,
                    "won": False, "admissible": self.admissible}
        return {"event": "step", "obs": "OK: " + command, "done": False,
                "won": False, "admissible": self.admissible}

    def close(self):
        pass


@pytest.fixture()
def fake_worker(monkeypatch):
    import css.envs.alfworld.agent as agent_mod
    monkeypatch.setattr(agent_mod, "AlfredWorker", _FakeWorker)


def _item():
    return {"id": "atrain_0000", "gamefile": "json_2.1.1/train/x/trial_1/game.tw-pddl",
            "task_type": "look_at_obj_in_light", "scene": "301"}


def test_run_one_success_trajectory_and_cache(tmp_path, fake_worker):
    env = AlfworldEnv(_cfg(), items={"train": [_item()], "val": [], "test": []})
    client = _ScriptedClient([
        "<think>go</think><action>go to desk 1</action>",
        "garbage without tags",                                # -> fallback look
        "<think>use it</think><action>use lamp 1</action>",
    ])
    res = env.run_one(_item(), "SKILLTEXT", client, str(tmp_path))
    assert res.hard == 1 and res.n_turns == 3
    assert res.task_id == "atrain_0000"
    # Trajectory contract: system first, eval annotation last, GT firewalled.
    roles = [m["role"] for m in res.messages]
    assert roles[0] == "system" and roles[-1] == "evaluation"
    agent_visible = "\n".join(m["content"] for m in res.messages[:-1])
    assert "Ground truth" not in agent_visible
    assert res.messages[-1]["content"].count("format_failures=1")
    # Cache round-trip: same skill hash reloads, different one misses.
    from css.envs.common import skill_hash
    cached = env.load_cached_result(_item(), str(tmp_path), rollout_index=0,
                                    skill_hash=skill_hash("SKILLTEXT"))
    assert cached is not None and cached.hard == 1
    missed = env.load_cached_result(_item(), str(tmp_path), rollout_index=0,
                                    skill_hash=skill_hash("OTHER"))
    assert missed is None


def test_run_one_gt_mode_episode_annotation(tmp_path, fake_worker, monkeypatch):
    import css.envs.alfworld.task_interface as ti_mod
    monkeypatch.setattr(ti_mod, "run_gold_replay", lambda *a, **k: {
        "won": True,
        "steps": [{"action": "go to desk 1", "obs": "You arrive at desk 1."}],
    })
    cfg = _cfg()
    cfg.extra["alfworld_gt_mode"] = "episode"
    env = AlfworldEnv(cfg, items={"train": [_item()], "val": [], "test": []})
    client = _ScriptedClient(["<reasoning>u</reasoning><action>use lamp 1</action>"])
    res = env.run_one(_item(), "", client, str(tmp_path))
    annotation = res.messages[-1]["content"]
    assert "Gold episode replay" in annotation
    assert "go to desk 1 -> You arrive at desk 1." in annotation
    # Memoized: second rollout must not re-invoke the replay.
    monkeypatch.setattr(ti_mod, "run_gold_replay",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("re-invoked")))
    res2 = env.run_one(_item(), "", client, str(tmp_path), rollout_index=1)
    assert "Gold episode replay" in res2.messages[-1]["content"]


def test_run_one_step_limit_failure(tmp_path, fake_worker):
    cfg = _cfg()
    cfg.extra["alfworld_max_steps"] = 4
    env = AlfworldEnv(cfg, items={"train": [_item()], "val": [], "test": []})
    client = _ScriptedClient(["<action>go to desk 1</action>"])
    res = env.run_one(_item(), "", client, str(tmp_path))
    assert res.hard == 0 and res.n_turns == 4
    assert "step-limit" in res.fail_reason
