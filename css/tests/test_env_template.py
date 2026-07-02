"""Contract test for the template env — the mechanism<->env surface, verified.

This test doubles as living documentation: it exercises every behavior the
mechanism relies on from a concrete ``TaskEnv``. When onboarding a new env,
copy this file alongside it and make every assertion pass — a green run means
the env honors the full contract (splits, run_one, trajectory shape, eval
annotation, GT firewall, persistence, resume cache, batch integration,
registry). If the mechanism contract ever evolves, this test forces the
template (and, by imitation, new envs) to keep up.
"""
from __future__ import annotations

import json
import os
import tempfile

import pytest

from css.config import CSSConfig
from css.data.rollout import TaskResult
from css.envs.base import TaskEnv
from css.envs.template.task_interface import TemplateEnv
from css.trajectory import POST_ROLLOUT_EVAL_MARKER, POST_ROLLOUT_EVAL_ROLE


# ── Stub target client ───────────────────────────────────────────────────────

_ANSWERS = {
    "What is 2+2?": "4",
    "Capital of France?": "Paris",
    "What is 3*3?": "8",  # deliberately wrong -> failed rollout
}


class StubTargetClient:
    """Scripted single-shot target client (no LLM, no network)."""

    def complete_target(self, system, user, *, max_tokens=4096, temperature=0.0):
        answer = _ANSWERS.get(user.strip(), "unknown")
        return f"Let me think.\nFINAL ANSWER: {answer}"


def _items():
    return {
        "train": [
            {"id": "t1", "question": "What is 2+2?", "answer": "4"},
            {"id": "t2", "question": "Capital of France?", "answer": "Paris"},
            {"id": "t3", "question": "What is 3*3?", "answer": "9"},
        ],
        "val": [{"id": "v1", "question": "What is 2+2?", "answer": "4"}],
        "test": [{"id": "s1", "question": "Capital of France?", "answer": "Paris"}],
    }


def _env(cfg=None, items=None):
    return TemplateEnv(cfg or CSSConfig(), items=items or _items())


# ── 1. Protocol conformance ─────────────────────────────────────────────────

def test_template_env_satisfies_taskenv_protocol():
    assert isinstance(_env(), TaskEnv)


# ── 2. Split accessors: determinism + sizing convention ─────────────────────

def test_splits_deterministic_and_sliced():
    env = _env()
    assert [i["id"] for i in env.train_items()] == ["t1", "t2", "t3"]
    assert env.train_items() == env.train_items()  # deterministic

    # n_train/n_val/n_test knobs slice; 0 = whole split.
    cfg = CSSConfig(n_train=2, n_val=0, n_test=1)
    env2 = _env(cfg=cfg)
    assert [i["id"] for i in env2.train_items()] == ["t1", "t2"]
    assert len(env2.val_items()) == 1
    assert len(env2.test_items()) == 1

    # every item carries a unique id — the mechanism's task identity
    ids = [i["id"] for split in ("train", "val", "test")
           for i in getattr(env, f"{split}_items")()]
    assert len(ids) == len(set(ids))


# ── 3. run_one: TaskResult shape, trajectory contract, GT firewall ──────────

def test_run_one_contract():
    env = _env()
    client = StubTargetClient()
    with tempfile.TemporaryDirectory() as td:
        item = _items()["train"][0]
        res = env.run_one(item, "SKILL_TEXT", client, td,
                          rollout_index=0, epoch=2, node_id="n0001")

        # Outcome fields the mechanism consumes
        assert isinstance(res, TaskResult)
        assert res.task_id == "t1"
        assert res.hard == 1 and res.passed
        assert 0.0 <= res.soft <= 1.0
        assert res.epoch == 2 and res.node_id == "n0001"

        # Trajectory contract: flat {role, content:str} transcript
        assert res.messages, "trajectory must not be empty"
        for msg in res.messages:
            assert isinstance(msg.get("role"), str) and msg["role"]
            assert isinstance(msg.get("content"), str)

        # Skill injection: the skill text reaches the agent's system prompt
        assert "SKILL_TEXT" in res.messages[0]["content"]

        # Eval annotation: LAST message, distinct role, marker + ground truth
        last = res.messages[-1]
        assert last["role"] == POST_ROLLOUT_EVAL_ROLE
        assert POST_ROLLOUT_EVAL_MARKER in last["content"]
        assert "4" in last["content"]  # gold answer exposed for analysis

        # GT firewall: gold never appears in agent-visible messages.
        # (Agent-visible = everything except the trailing eval annotation.
        # The gold "4" can legitimately appear in the assistant's own answer;
        # firewall means no message CONTAINS the gold as provided reference —
        # check system + user prompts specifically.)
        for msg in res.messages[:2]:  # system + user
            assert "Gold" not in msg["content"]
            assert "answer\": \"4" not in msg["content"]


def test_run_one_failure_is_result_not_exception():
    env = _env()
    client = StubTargetClient()
    with tempfile.TemporaryDirectory() as td:
        item = _items()["train"][2]  # scripted wrong answer
        res = env.run_one(item, "S", client, td, rollout_index=0)
        assert res.hard == 0 and not res.passed
        assert res.fail_reason  # diagnostic present


# ── 4. Persistence + resume cache ────────────────────────────────────────────

def test_persistence_and_cache_roundtrip():
    from css.envs.common import skill_hash

    env = _env()
    client = StubTargetClient()
    with tempfile.TemporaryDirectory() as td:
        item = _items()["train"][0]
        res = env.run_one(item, "SKILL_A", client, td, rollout_index=1)

        # Canonical prediction layout: <out>/predictions/<task_id>/r<i>/result.json
        rp = os.path.join(td, "predictions", "t1", "r1", "result.json")
        assert os.path.exists(rp)
        with open(rp, encoding="utf-8") as f:
            persisted = json.load(f)
        assert persisted["skill_hash"] == skill_hash("SKILL_A")

        # Same-skill cache hit reproduces the result
        cached = env.load_cached_result(
            item, td, rollout_index=1, skill_hash=skill_hash("SKILL_A"))
        assert cached is not None
        assert cached.task_id == res.task_id and cached.hard == res.hard
        assert cached.messages[-1]["role"] == POST_ROLLOUT_EVAL_ROLE

        # Different skill -> cache invalid -> None (re-roll)
        assert env.load_cached_result(
            item, td, rollout_index=1, skill_hash=skill_hash("SKILL_B")) is None
        # Different rollout index -> None
        assert env.load_cached_result(
            item, td, rollout_index=0, skill_hash=skill_hash("SKILL_A")) is None


# ── 5. Batch layer integration: exact counts + cache reuse ──────────────────

def test_batch_rollout_integration():
    from css.rollout.batch import batch_rollout, grouped_batch_rollout

    env = _env()
    client = StubTargetClient()
    with tempfile.TemporaryDirectory() as td:
        items = _items()["train"]
        results = batch_rollout(
            env, items, "SKILL", client,
            k_rollouts=2, out_dir=td, max_workers=2, task_timeout=30,
            epoch=0, node_id="n0000",
        )
        # Exact counts: K * M results, no drops
        assert len(results) == 2 * len(items)
        by_task = {}
        for r in results:
            by_task.setdefault(r.task_id, []).append(r)
        assert set(by_task) == {"t1", "t2", "t3"}
        assert all(len(v) == 2 for v in by_task.values())

        # Second run over the same out_dir + same skill = pure cache hits
        # (same results, no new agent calls — client would answer identically
        # anyway, so verify via persisted mtimes staying put)
        rp = os.path.join(td, "predictions", "t1", "r0", "result.json")
        mtime_before = os.path.getmtime(rp)
        results2 = batch_rollout(
            env, items, "SKILL", client,
            k_rollouts=2, out_dir=td, max_workers=2, task_timeout=30,
        )
        assert len(results2) == 2 * len(items)
        assert os.path.getmtime(rp) == mtime_before

        # grouped variant: one group per task with its K rollouts
        groups = grouped_batch_rollout(
            env, items, "SKILL", client,
            k_rollouts=2, out_dir=td, max_workers=2, task_timeout=30,
        )
        assert len(groups) == 3
        assert all(len(g.rollouts) == 2 for g in groups)


# ── 6. Registry + action space ───────────────────────────────────────────────

def test_registry_builds_template_env():
    from css.envs.registry import build_env, canonical_env_name

    assert canonical_env_name("template") == "template"
    cfg = CSSConfig(env_name="template")
    env = build_env(cfg, items=_items())
    assert isinstance(env, TemplateEnv)
    assert len(env.train_items()) == 3


def test_action_space_description_nonempty():
    desc = _env().action_space_description()
    assert isinstance(desc, str) and len(desc) > 50
