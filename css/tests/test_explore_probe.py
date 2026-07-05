"""dispatch_probe: menu resolution, behavior_prompt==skill_text, k clamp, archive.

``grouped_batch_rollout`` is monkeypatched; scripted fake narrator optimizer. No
network.
"""
from __future__ import annotations

import json

import css.explore.probe as pr
from css.config import CSSConfig
from css.data.rollout import TaskResult, TaskRolloutGroup
from css.model.client import StubLLMClient


def _cfg(**kw) -> CSSConfig:
    base = dict(explore_probe_k_max=2, max_api_workers=2, task_timeout_s=5,
                tool_trunc=2000)
    base.update(kw)
    return CSSConfig(**base)


def _menu():
    return pr.build_task_menu(
        [{"task_id": "g1", "task_description": "target A"}],
        [{"task_id": "n1", "task_description": "neighbor B"}],
    )


def test_menu_build_and_render():
    menu = _menu()
    assert menu["g1"]["kind"] == "target-unsolved"
    assert menu["n1"]["kind"] == "solved-neighbor"
    text = pr.render_task_menu(menu)
    assert "g1: target A" in text
    assert "n1: neighbor B" in text
    assert "Target group" in text and "Solved neighbors" in text


def test_unknown_task_id_graceful(tmp_path):
    rec = pr.dispatch_probe(
        {"behavior_prompt": "x", "task_id": "ZZ", "k": 1, "purpose": "p"},
        menu=_menu(), env=None, target_client=None, optimizer_client=StubLLMClient(),
        cfg=_cfg(), session_dir=str(tmp_path), probe_index=0)
    assert "error" in rec
    assert "unknown task_id" in rec["narration"]
    assert rec["verdicts"] == []
    assert (tmp_path / "probes" / "probe_000.json").exists()


def test_empty_behavior_prompt_graceful(tmp_path):
    rec = pr.dispatch_probe(
        {"behavior_prompt": "   ", "task_id": "g1", "k": 1, "purpose": "p"},
        menu=_menu(), env=None, target_client=None, optimizer_client=StubLLMClient(),
        cfg=_cfg(), session_dir=str(tmp_path), probe_index=3)
    assert "error" in rec and "empty behavior_prompt" in rec["error"]


def test_dispatch_runs_rollout_and_narrates(tmp_path, monkeypatch):
    captured = {}

    def fake_grouped(env, items, skill_text, target_client, *, k_rollouts, out_dir,
                     max_workers, task_timeout, epoch, node_id):
        captured["skill_text"] = skill_text
        captured["k"] = k_rollouts
        captured["node_id"] = node_id
        msgs = [{"role": "assistant", "content": "act on item"},
                {"role": "tool", "content": "observation"}]
        rolls = [TaskResult(task_id=items[0]["task_id"], rollout_index=i, hard=0,
                            soft=0.0, messages=list(msgs)) for i in range(k_rollouts)]
        return [TaskRolloutGroup(task_id=items[0]["task_id"], rollouts=rolls)]

    monkeypatch.setattr(pr, "grouped_batch_rollout", fake_grouped)
    client = StubLLMClient(optimizer_fn=lambda s, u: "narration [t0]")
    rec = pr.dispatch_probe(
        {"behavior_prompt": "BEHAVE THIS WAY", "task_id": "g1", "k": 5, "purpose": "p"},
        menu=_menu(), env=None, target_client=None, optimizer_client=client,
        cfg=_cfg(), session_dir=str(tmp_path), probe_index=1, decision_index=7)
    assert captured["skill_text"] == "BEHAVE THIS WAY", "behavior_prompt IS skill_text"
    assert captured["k"] == 2, "k clamped to explore_probe_k_max"
    assert captured["node_id"] == "explore"
    assert rec["k"] == 2
    assert len(rec["verdicts"]) == 2
    assert "[t0]" in rec["narration"]
    assert "## Evaluation" in rec["narration"]
    saved = json.loads((tmp_path / "probes" / "probe_001.json").read_text())
    assert saved["behavior_prompt"] == "BEHAVE THIS WAY"
    assert saved["task_id"] == "g1"


def test_rollout_exception_is_narrated_not_raised(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("env exploded")

    monkeypatch.setattr(pr, "grouped_batch_rollout", boom)
    rec = pr.dispatch_probe(
        {"behavior_prompt": "x", "task_id": "g1", "k": 1, "purpose": "p"},
        menu=_menu(), env=None, target_client=None, optimizer_client=StubLLMClient(),
        cfg=_cfg(), session_dir=str(tmp_path), probe_index=2)
    assert "error" in rec
    assert "env exploded" in rec["narration"]
    assert rec["verdicts"] == []
