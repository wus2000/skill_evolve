"""Findings distillation (double screen) + get_or_explore caching / staleness.

Everything is scripted: the fake optimizer routes by system prompt (director /
distill / purity / altitude); ``dispatch_probe`` is faked so no rollout runs. No
network.
"""
from __future__ import annotations

import json

import css.explore.api as api
import css.explore.director as d
import css.explore.findings as fnd
from css.config import CSSConfig
from css.explore.api import get_or_explore, mark_stale
from css.model.client import StubLLMClient

_GOLD = "SECRET_GOLD_42"


def _cfg() -> CSSConfig:
    return CSSConfig(explore_probe_guardrail=48, explore_probe_k_max=2)


def _fake_dispatch():
    def fn(spec, *, menu, env, target_client, optimizer_client, cfg,
           session_dir, probe_index, decision_index=0, leads_path=""):
        tid = spec.get("task_id")
        return {"probe_index": probe_index, "task_id": tid, "k": 1,
                "behavior_prompt": spec.get("behavior_prompt", ""),
                "purpose": spec.get("purpose", ""),
                "verdicts": [{"rollout_index": 0, "passed": False, "soft": 0.0,
                              "fail_reason": "x"}],
                "narration": "probe narration"}
    return fn


def _router(distill_text):
    """One optimizer_fn that plays every role, keyed by the system prompt."""
    state = {"dir": 0}

    def opt(system, user):
        if "=== YOUR MISSION ===" in system:
            i = state["dir"]
            state["dir"] += 1
            if i == 0:
                return json.dumps({"action": "dispatch", "probes": [
                    {"behavior_prompt": "x", "task_id": "g1", "k": 1, "purpose": "p"}]})
            return json.dumps({"action": "report",
                               "report_markdown": "Report mentioning %s." % _GOLD})
        if "content-purity" in system:
            return json.dumps({"verdict": "revise", "violations": [_GOLD],
                               "rewritten": "Clean findings: a behavioral approach helps."})
        if "ALTITUDE screen" in system:
            return json.dumps({"verdict": "pass", "violated_criteria": [],
                               "feedback": "", "quoted_offense": "", "rewritten": ""})
        if "You distill" in system:
            return distill_text
        return "?"

    return StubLLMClient(optimizer_fn=opt)


def _explore(tmp_path, client, group_key="groupZ", decision_index=0):
    return get_or_explore(
        group_key, group_tasks=[{"task_id": "g1", "task_description": "A"}],
        neighbor_tasks=[], briefing_md="history", mode="NEW", env=None,
        target_client=None, optimizer_client=client, cfg=_cfg(),
        out_dir=str(tmp_path), decision_index=decision_index)


def test_purity_screen_strips_planted_gold(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "dispatch_probe", _fake_dispatch())
    client = _router("Distilled: the probe leaked %s here." % _GOLD)
    findings = _explore(tmp_path, client)
    assert findings, "findings were produced"
    assert _GOLD not in findings, "content-purity screen stripped the gold answer"
    assert (tmp_path / "global" / "exploration" / "groupZ" / "findings.md").exists()
    meta = json.loads(
        (tmp_path / "global" / "exploration" / "groupZ" / "findings.meta.json").read_text())
    assert meta["stale"] is False
    assert meta["source_sessions"]


def test_findings_cached_then_reexplored_on_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "dispatch_probe", _fake_dispatch())
    sessions = {"n": 0}
    real = api.run_director_session

    def counting(**kw):
        sessions["n"] += 1
        return real(**kw)

    monkeypatch.setattr(api, "run_director_session", counting)
    client = _router("Distilled findings without any leak.")

    f1 = _explore(tmp_path, client, decision_index=0)
    assert f1 and sessions["n"] == 1

    # Fresh cache: a second call (different decision) reuses it, no new session.
    f2 = _explore(tmp_path, client, decision_index=1)
    assert f2 == f1
    assert sessions["n"] == 1, "cache hit must not run another session"

    # Failure mode changed -> stale -> the next call re-explores at a new decision.
    mark_stale("groupZ", str(tmp_path), reason="failure mode changed")
    f3 = _explore(tmp_path, client, decision_index=2)
    assert sessions["n"] == 2, "stale cache re-runs exploration"
    assert f3


def test_resume_skips_finished_session(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "dispatch_probe", _fake_dispatch())
    sessions = {"n": 0}
    real = api.run_director_session

    def counting(**kw):
        sessions["n"] += 1
        return real(**kw)

    monkeypatch.setattr(api, "run_director_session", counting)

    # First run distills to EMPTY findings (nothing cached), so the next call is a
    # cache miss — but the session at this decision is already finished and must be
    # reused rather than re-run (stage-level resume).
    empty_client = _router("")   # distill returns empty -> no findings cached
    f0 = _explore(tmp_path, empty_client, decision_index=0)
    assert f0 == "" and sessions["n"] == 1

    good_client = _router("Now a proper distilled findings paragraph.")
    f1 = _explore(tmp_path, good_client, decision_index=0)
    assert f1, "second attempt distills real findings"
    assert sessions["n"] == 1, "the finished session was resumed, not re-run"


def test_get_or_explore_never_raises(tmp_path, monkeypatch):
    # A distiller that raises must degrade to "" (never propagate). api imported
    # build_findings by value, so patch the name api actually calls.
    monkeypatch.setattr(d, "dispatch_probe", _fake_dispatch())

    def boom(*a, **k):
        raise RuntimeError("distill exploded")

    monkeypatch.setattr(api, "build_findings", boom)
    client = _router("irrelevant")
    out = _explore(tmp_path, client)
    assert out == ""
