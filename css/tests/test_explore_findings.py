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


def _router(distill_text, clean_text="Clean findings: a behavioral approach helps."):
    """One optimizer_fn that plays every role, keyed by the system prompt.

    Judge-only screens: purity flags the gold answer on the FIRST distillation;
    the re-distillation (recognizable by the REVISION REQUIRED block in the
    user message) returns ``clean_text``, which then passes both screens.
    """
    state = {"dir": 0, "distill": 0}

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
            if _GOLD in user:
                return json.dumps({"verdict": "revise", "violations": [_GOLD],
                                   "feedback": "remove the leaked gold answer"})
            return json.dumps({"verdict": "pass", "violations": [],
                               "feedback": ""})
        if "ALTITUDE screen" in system:
            return json.dumps({"verdict": "pass", "violated_criteria": [],
                               "feedback": "", "quoted_offense": ""})
        if "You distill" in system:
            state["distill"] += 1
            if "REVISION REQUIRED" in user:
                return clean_text
            return distill_text
        return "?"

    client = StubLLMClient(optimizer_fn=opt)
    client._state = state
    return client


def _explore(tmp_path, client, group_key="groupZ", decision_index=0):
    return get_or_explore(
        group_key, group_tasks=[{"task_id": "g1", "task_description": "A"}],
        neighbor_tasks=[], briefing_md="history", mode="NEW", env=None,
        target_client=None, optimizer_client=client, cfg=_cfg(),
        out_dir=str(tmp_path), decision_index=decision_index)


def test_purity_fail_triggers_redistill_not_rewrite(tmp_path, monkeypatch):
    # Judge/generator separation: the screen only judges; the fix is a fresh
    # DISTILLATION carrying the screen's feedback (never a screen rewrite).
    monkeypatch.setattr(d, "dispatch_probe", _fake_dispatch())
    client = _router("Distilled: the probe leaked %s here." % _GOLD)
    findings = _explore(tmp_path, client)
    assert findings, "findings were produced"
    assert _GOLD not in findings, "the re-distillation dropped the gold answer"
    assert client._state["distill"] == 2, "exactly one critique-driven redistill"
    assert (tmp_path / "global" / "exploration" / "groupZ" / "findings.md").exists()
    meta = json.loads(
        (tmp_path / "global" / "exploration" / "groupZ" / "findings.meta.json").read_text())
    assert meta["stale"] is False
    assert meta["source_sessions"]


def test_screen_exhaustion_discards_findings(tmp_path, monkeypatch):
    # Persistent screen failure -> findings DISCARDED (no cache), never adopted
    # via a screen rewrite; the empty result reaches the caller.
    monkeypatch.setattr(d, "dispatch_probe", _fake_dispatch())
    # Every distillation (fresh and revised) still leaks the gold answer.
    client = _router("Distilled: leak %s." % _GOLD,
                     clean_text="Still leaking %s after revision." % _GOLD)
    findings = _explore(tmp_path, client)
    assert findings == ""
    assert not (tmp_path / "global" / "exploration" / "groupZ" /
                "findings.md").exists()
    assert client._state["distill"] == 3, "1 initial + 2 redistills, then discard"


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
