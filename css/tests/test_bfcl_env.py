"""BfclEnv end-to-end: gold replay (hard=1), ground-truth firewall, hard/soft +
force-termination mapping, and the function-calling {content, tool_calls} shape.

Uses stub clients (no LLM) that emit OpenAI ``tool_calls`` — the same shape the
real client's XML fallback (commit 9a1823b) produces on a parser-less endpoint,
so this also exercises the FC path end-to-end.
"""
from __future__ import annotations

import ast
import json
import os
from types import SimpleNamespace

import pytest

from css.envs.bfcl.agent import _CLASS_FUNCS
from css.envs.bfcl.task_interface import BfclEnv

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SPLIT_DIR = os.path.join(REPO, "data", "bfcl_split_seed42")
_HAVE = os.path.exists(os.path.join(SPLIT_DIR, "test", "items.json"))
pytestmark = pytest.mark.skipif(not _HAVE, reason="bfcl manifests not generated")

_PARAM_ORDER = {f["name"]: list(f.get("parameters", {}).get("properties", {}).keys())
                for funcs in _CLASS_FUNCS.values() for f in funcs}


def _gold_to_args(call_str):
    node = ast.parse(call_str.strip(), mode="eval").body
    name = node.func.id
    args = {k.arg: ast.literal_eval(k.value) for k in node.keywords}
    for i, a in enumerate(node.args):
        order = _PARAM_ORDER.get(name, [])
        if i < len(order):
            args[order[i]] = ast.literal_eval(a)
    return name, args


class _GoldStub:
    """Replay an item's gold call sequence as tool_calls, one turn at a time."""

    def __init__(self, gt):
        self.gt = gt

    def complete_target_tools(self, messages, tools, **kw):
        last_user = max(i for i, m in enumerate(messages) if m["role"] == "user")
        if any(m["role"] == "assistant" for m in messages[last_user + 1:]):
            return {"content": "done", "tool_calls": []}
        turn = sum(1 for m in messages if m["role"] == "user") - 1
        gold = self.gt[turn] if turn < len(self.gt) else []
        tcs = []
        for i, c in enumerate(gold):
            name, args = _gold_to_args(c)
            tcs.append({"id": "c%d_%d" % (turn, i),
                        "function": {"name": name, "arguments": json.dumps(args)}})
        return {"content": None, "tool_calls": tcs}


class _AlwaysCallStub:
    """Always call the first available tool -> the agent never stops -> 20-step cap."""

    def complete_target_tools(self, messages, tools, **kw):
        name = tools[0]["function"]["name"]
        return {"content": None,
                "tool_calls": [{"id": "x", "function": {"name": name, "arguments": "{}"}}]}


class _ContentOnlyStub:
    """Never call a tool -> every turn ends immediately (exercises the FC shape)."""

    def complete_target_tools(self, messages, tools, **kw):
        return {"content": "I am not sure which tool to use.", "tool_calls": []}


def _env():
    cfg = SimpleNamespace(split_dir=SPLIT_DIR, data_root="",
                          extra={"bfcl_temperature": 0.0}, task_timeout_s=600)
    return BfclEnv(cfg)


def _items_by_cat(env, n=2):
    out = []
    seen = {}
    for it in env.test_items():
        c = it["category"]
        if seen.get(c, 0) < n:
            out.append(it)
            seen[c] = seen.get(c, 0) + 1
    return out


def test_gold_replay_hard1_all_categories(tmp_path):
    env = _env()
    items = _items_by_cat(env, n=2)
    cats_seen = set()
    for it in items:
        r = env.run_one(it, "", _GoldStub(it["ground_truth"]), str(tmp_path), rollout_index=0)
        assert r.hard == 1, "%s: gold replay should pass, got %s" % (it["id"], r.fail_reason)
        assert r.soft == 1.0
        cats_seen.add(r.task_type)
    assert cats_seen == {"base", "miss_func", "miss_param", "long_context"}


def test_ground_truth_firewall(tmp_path):
    env = _env()
    it = next(x for x in env.test_items() if x["category"] == "miss_func")
    r = env.run_one(it, "", _GoldStub(it["ground_truth"]), str(tmp_path))
    # gold appears ONLY in the final evaluation annotation, never agent-visible
    assert r.messages[-1]["role"] == "evaluation"
    assert "Gold call sequence" in r.messages[-1]["content"]
    assert not any("Gold call sequence" in m.get("content", "") for m in r.messages[:-1])


def test_force_termination_maps_to_fail(tmp_path):
    env = _env()
    it = next(x for x in env.test_items() if x["category"] == "base")
    r = env.run_one(it, "", _AlwaysCallStub(), str(tmp_path))
    assert r.hard == 0
    assert r.extras.get("force_terminated") is True
    assert "force-terminated" in r.fail_reason


def test_content_only_ends_turns_cleanly(tmp_path):
    env = _env()
    it = next(x for x in env.test_items() if x["category"] == "base")
    r = env.run_one(it, "", _ContentOnlyStub(), str(tmp_path))
    assert r.hard == 0  # did nothing -> state never matches
    assert r.messages and r.n_turns == len(it["question"])  # ran all turns, no crash


def test_task_result_shape(tmp_path):
    env = _env()
    it = next(iter(env.test_items()))
    r = env.run_one(it, "SKILL", _GoldStub(it["ground_truth"]), str(tmp_path))
    assert r.task_id == it["id"]
    assert r.task_type == it["category"]
    assert 0.0 <= r.soft <= 1.0
    assert r.extras.get("skill_hash")
    assert isinstance(r.messages, list) and r.messages[0]["role"] == "system"
