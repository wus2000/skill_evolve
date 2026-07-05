"""Narrator: mechanical turn coverage (regen + stub), verbatim detail, sections.

Scripted fake optimizer client; no rollout, no network.
"""
from __future__ import annotations

import css.explore.narrator as nr
from css.config import CSSConfig
from css.data.rollout import TaskResult, TaskRolloutGroup
from css.model.client import StubLLMClient


def _cfg() -> CSSConfig:
    return CSSConfig(tool_trunc=4000)


def _traj():
    # system + eval framing are stripped; 3 turns remain: t0 (task), t1, t2.
    return [
        {"role": "system", "content": "BEHAVIOR PROMPT + action protocol"},
        {"role": "user", "content": "Task: do X on item WIDGET-42"},
        {"role": "assistant", "content": "I will inspect WIDGET-42"},
        {"role": "tool", "content": "OK inspected"},
        {"role": "assistant", "content": "Now compute the result"},
        {"role": "tool", "content": "Error: KeyError 'foo'"},
        {"role": "evaluation", "content": "[POST-ROLLOUT EVALUATION] Outcome: fail"},
    ]


def test_extract_turns_strips_scaffold_and_pairs():
    turns = nr._extract_turns(_traj())
    assert len(turns) == 3
    assert turns[0]["action"]["role"] == "user"        # leading task prompt = t0
    assert turns[1]["action"]["content"].startswith("I will inspect")
    assert turns[1]["obs"][0]["content"] == "OK inspected"


def test_coverage_regen_then_stub(monkeypatch):
    r = TaskResult(task_id="g1", messages=_traj(), hard=0, soft=0.0)
    grp = TaskRolloutGroup(task_id="g1", rollouts=[r])
    calls = {"n": 0}

    def opt(system, user):
        calls["n"] += 1
        return "The agent begins [t0], then acts at [t1]."  # [t2] always missing

    client = StubLLMClient(optimizer_fn=opt)
    out = nr.narrate_probe(grp, purpose="p", optimizer_client=client, env=None,
                           item={"task_id": "g1"}, cfg=_cfg())
    assert calls["n"] == 2, "one narration + exactly one regeneration"
    assert "[t2]" in out, "still-missing turn is covered by a mechanical stub"
    assert "mechanical stub" in out
    # The stub is built from the raw trajectory, so the short error survives verbatim.
    assert "KeyError 'foo'" in out
    assert "## Evaluation" in out


def test_verbatim_error_passthrough(monkeypatch):
    r = TaskResult(task_id="g1", messages=_traj())
    grp = TaskRolloutGroup(task_id="g1", rollouts=[r])

    def opt(system, user):
        return ("[t0] agent reads the task. [t1] it inspects WIDGET-42. "
                "[t2] it computes and hits Error: KeyError 'foo'.")

    client = StubLLMClient(optimizer_fn=opt)
    out = nr.narrate_probe(grp, purpose="p", optimizer_client=client, env=None,
                           item={}, cfg=_cfg())
    assert "KeyError 'foo'" in out
    assert "WIDGET-42" in out
    assert "mechanical stub" not in out, "full coverage => no stub fallback"


def test_reference_section_only_when_hook_present(monkeypatch):
    class EnvWithGold:
        def gold_approach_gist(self, item):
            return "aggregate rows then filter"

    r = TaskResult(task_id="g1", messages=_traj())
    grp = TaskRolloutGroup(task_id="g1", rollouts=[r])
    client = StubLLMClient(optimizer_fn=lambda s, u: "[t0] [t1] [t2] narration")

    out_gold = nr.narrate_probe(grp, purpose="p", optimizer_client=client,
                                env=EnvWithGold(), item={"task_id": "g1"}, cfg=_cfg())
    assert "Reference approach (instance-specific)" in out_gold
    assert "aggregate rows then filter" in out_gold

    out_plain = nr.narrate_probe(grp, purpose="p", optimizer_client=client,
                                 env=object(), item={}, cfg=_cfg())
    assert "Reference approach" not in out_plain


def test_evaluation_section_lists_all_verdicts(monkeypatch):
    r0 = TaskResult(task_id="g1", rollout_index=0, hard=1, soft=1.0, messages=_traj())
    r1 = TaskResult(task_id="g1", rollout_index=1, hard=0, soft=0.2,
                    fail_reason="bad output", messages=_traj())
    grp = TaskRolloutGroup(task_id="g1", rollouts=[r0, r1])
    client = StubLLMClient(optimizer_fn=lambda s, u: "[t0] [t1] [t2]")
    out = nr.narrate_probe(grp, purpose="p", optimizer_client=client, env=None,
                           item={}, cfg=_cfg())
    assert "## Evaluation" in out
    assert "1 of 2 rollout(s) passed" in out
    assert "rollout 0: PASS" in out
    assert "rollout 1: FAIL" in out
    assert "bad output" in out


def test_narration_survives_optimizer_failure(monkeypatch):
    def boom(system, user):
        raise RuntimeError("optimizer down")

    r = TaskResult(task_id="g1", messages=_traj())
    grp = TaskRolloutGroup(task_id="g1", rollouts=[r])
    client = StubLLMClient(optimizer_fn=boom)
    out = nr.narrate_probe(grp, purpose="p", optimizer_client=client, env=None,
                           item={}, cfg=_cfg())
    # Every turn still covered by the mechanical stub; sections still present.
    for anchor in ("[t0]", "[t1]", "[t2]"):
        assert anchor in out
    assert "## Evaluation" in out
