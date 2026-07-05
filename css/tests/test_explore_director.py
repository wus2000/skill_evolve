"""Director session loop: dispatch->report, silent guardrail, telemetry counts.

The loop is exercised with a fully scripted fake optimizer client (director JSON
turns) and a fake ``dispatch_probe`` (no rollout, no narrator LLM). No network.
"""
from __future__ import annotations

import json

import css.explore.director as d
from css.config import CSSConfig
from css.model.client import StubLLMClient


def _cfg(**kw) -> CSSConfig:
    base = dict(explore_probe_guardrail=48, explore_probe_k_max=2,
                max_api_workers=2, task_timeout_s=5)
    base.update(kw)
    return CSSConfig(**base)


def _tasks():
    group = [{"task_id": "g1", "task_description": "unsolved A"},
             {"task_id": "g2", "task_description": "unsolved B"}]
    neighbor = [{"task_id": "n1", "task_description": "solved neighbor"}]
    return group, neighbor


def _fake_dispatch():
    """Stand-in for probe.dispatch_probe: canned record, no rollout/narrator."""
    def fn(spec, *, menu, env, target_client, optimizer_client, cfg,
           session_dir, probe_index, decision_index=0):
        tid = spec.get("task_id")
        prompt = spec.get("behavior_prompt", "")
        if tid not in menu:
            return {"probe_index": probe_index, "task_id": tid, "k": 1,
                    "behavior_prompt": prompt, "purpose": spec.get("purpose", ""),
                    "error": "unknown task_id", "verdicts": [],
                    "narration": "ERROR: unknown task_id"}
        k = max(1, min(int(spec.get("k", 1)), int(cfg.explore_probe_k_max)))
        return {"probe_index": probe_index, "task_id": tid, "k": k,
                "behavior_prompt": prompt, "purpose": spec.get("purpose", ""),
                "verdicts": [{"rollout_index": 0, "passed": False, "soft": 0.0,
                              "fail_reason": "x"}],
                "narration": "probe narration for %s" % tid}
    return fn


def _scripted_client(turns):
    """Optimizer client that returns ``turns[i]`` on the i-th director call."""
    state = {"i": 0}

    def opt(system, user):
        i = state["i"]
        state["i"] = min(i + 1, len(turns) - 1)
        return turns[i]

    return StubLLMClient(optimizer_fn=opt)


def test_dispatch_then_report_loop(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "dispatch_probe", _fake_dispatch())
    group, neighbor = _tasks()
    client = _scripted_client([
        json.dumps({"action": "dispatch", "probes": [
            {"behavior_prompt": "be terse", "task_id": "g1", "k": 1,
             "purpose": "see effect"}]}),
        json.dumps({"action": "report", "report_markdown": "# Findings\nProbe shows X."}),
    ])
    out = d.run_director_session(
        group_key="grpA", mode="NEW", group_tasks=group, neighbor_tasks=neighbor,
        briefing_md="history here", env=None, target_client=None,
        optimizer_client=client, cfg=_cfg(), session_dir=str(tmp_path / "s0"),
        decision_index=0)
    meta = out["meta"]
    assert meta["n_probes"] == 1
    assert meta["stop_reason"] == "self"
    assert meta["n_turns"] == 2
    assert "# Findings" in out["report_md"]
    for name in ("plan.json", "briefing.md", "transcript.jsonl", "report.md",
                 "session_meta.json"):
        assert (tmp_path / "s0" / name).exists(), name


def test_guardrail_trips_and_forces_report(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "dispatch_probe", _fake_dispatch())
    group, neighbor = _tasks()

    def opt(system, user):
        # Silent until the guardrail note appears, then a report.
        if "Resources are exhausted" in user:
            return json.dumps({"action": "report", "report_markdown": "forced report"})
        return json.dumps({"action": "dispatch", "probes": [
            {"behavior_prompt": "x", "task_id": "g1", "k": 1, "purpose": "p"}]})

    client = StubLLMClient(optimizer_fn=opt)
    out = d.run_director_session(
        group_key="grpA", mode="NEW", group_tasks=group, neighbor_tasks=neighbor,
        briefing_md="h", env=None, target_client=None, optimizer_client=client,
        cfg=_cfg(explore_probe_guardrail=2), session_dir=str(tmp_path / "s0"))
    meta = out["meta"]
    assert meta["n_probes"] == 2, "guardrail caps real probes at the limit"
    assert meta["stop_reason"] == "guardrail"
    assert out["report_md"] == "forced report"


def test_guardrail_language_absent_from_director_prompt():
    # The 48-probe guardrail is silent by design — no budget/quota words leak into
    # the director's system prompt (tendency observation must stay clean).
    for mode in ("NEW", "REFINE"):
        sysp = d.director_system_prompt(mode).lower()
        assert "guardrail" not in sysp
        assert "resources are exhausted" not in sysp
        assert "48" not in sysp


def test_contrastive_pair_telemetry(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "dispatch_probe", _fake_dispatch())
    group, neighbor = _tasks()
    client = _scripted_client([
        json.dumps({"action": "dispatch", "probes": [
            {"behavior_prompt": "A", "task_id": "g1", "k": 1, "purpose": "p1"},
            {"behavior_prompt": "B", "task_id": "g1", "k": 1, "purpose": "p2"}]}),
        json.dumps({"action": "report", "report_markdown": "done"}),
    ])
    out = d.run_director_session(
        group_key="g", mode="NEW", group_tasks=group, neighbor_tasks=neighbor,
        briefing_md="h", env=None, target_client=None, optimizer_client=client,
        cfg=_cfg(), session_dir=str(tmp_path / "s"))
    meta = out["meta"]
    assert meta["n_contrastive_pairs"] == 1, "two prompts on one task in one turn"
    assert meta["n_probes"] == 2
    assert meta["tasks_covered"] == 1


def test_repeat_k_and_replication_telemetry(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "dispatch_probe", _fake_dispatch())
    group, neighbor = _tasks()
    client = _scripted_client([
        json.dumps({"action": "dispatch", "probes": [
            {"behavior_prompt": "HIST_STRAT", "task_id": "g1", "k": 2,
             "purpose": "replicate a past strategy"}]}),
        json.dumps({"action": "report", "report_markdown": "r"}),
    ])
    out = d.run_director_session(
        group_key="g", mode="REFINE", group_tasks=group, neighbor_tasks=neighbor,
        briefing_md="h", env=None, target_client=None, optimizer_client=client,
        cfg=_cfg(), session_dir=str(tmp_path / "s"), history_texts=["HIST_STRAT"])
    meta = out["meta"]
    assert meta["n_repeat_k"] == 1, "k>1 counted"
    assert meta["n_replications"] == 1, "behavior_prompt == a historical strategy text"


def test_unknown_task_id_does_not_count_as_probe(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "dispatch_probe", _fake_dispatch())
    group, neighbor = _tasks()
    client = _scripted_client([
        json.dumps({"action": "dispatch", "probes": [
            {"behavior_prompt": "x", "task_id": "NOPE", "k": 1, "purpose": "p"}]}),
        json.dumps({"action": "report", "report_markdown": "done"}),
    ])
    out = d.run_director_session(
        group_key="g", mode="NEW", group_tasks=group, neighbor_tasks=neighbor,
        briefing_md="h", env=None, target_client=None, optimizer_client=client,
        cfg=_cfg(), session_dir=str(tmp_path / "s"))
    # An error spec is archived + shown to the director, but is not a real probe.
    assert out["meta"]["n_probes"] == 0
    assert out["meta"]["stop_reason"] == "self"
