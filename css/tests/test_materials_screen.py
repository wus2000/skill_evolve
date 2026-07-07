"""The Altitude Screen: pure judging + source-pipeline regeneration routing."""
from __future__ import annotations

from css.config import CSSConfig
from css.materials import common, screen
from css.tests import materials_helpers as H


def _cfg(**kw) -> CSSConfig:
    base = dict(max_api_workers=2, screen_batch_size=10)
    base.update(kw)
    return CSSConfig(**base)


def _rule(text: str) -> str:
    if "TODO" in text:
        return "reject"
    if "At step" in text or "at step" in text:
        return "revise"
    return "pass"


def test_screen_items_is_a_pure_judge(monkeypatch):
    # Decision log #14: the screen never rewrites — screen_items only judges.
    items = [
        "Exploring broadly before committing improves outcomes here.",   # pass
        "At step 4 the agent should call parse_header first.",            # revise
        "It failed. TODO list of fixes for this exact task instance.",    # reject
    ]
    fake = H.FakeStage({"interp_screen": H.screen_by_rule(_rule)})
    monkeypatch.setattr(common, "run_json_stage", fake)

    verdicts = screen.screen_items(items, object(), _cfg(), stage="interp")
    assert [v["verdict"] for v in verdicts] == ["pass", "revise", "rejected"]
    # The judge returns the ORIGINAL text untouched, and no revise stage ran.
    assert verdicts[1]["text"] == items[1]
    assert fake.count("interp_screen") == 1


def test_regeneration_routes_revise_to_the_source_pipeline(monkeypatch):
    items = ["At step 4 the agent should call parse_header first."]
    fake = H.FakeStage({"interp_screen": H.screen_by_rule(_rule)})
    monkeypatch.setattr(common, "run_json_stage", fake)
    seen = {}

    def regen(i, feedback):
        seen["index"], seen["feedback"] = i, feedback
        return ("Committing only after broad exploration is the behavioral "
                "principle that matters.")

    verdicts = screen.screen_with_regeneration(
        items, object(), _cfg(), "interp", regen)
    assert seen["index"] == 0 and seen["feedback"], "feedback reached the source"
    assert verdicts[0]["verdict"] == "pass"
    assert verdicts[0]["regenerated"] is True
    assert "At step" not in verdicts[0]["text"]


def test_regeneration_still_failing_is_rejected(monkeypatch):
    items = ["At step 4 do X."]
    fake = H.FakeStage({"interp_screen": H.screen_by_rule(
        lambda t: "revise" if "step" in t else "pass")})
    monkeypatch.setattr(common, "run_json_stage", fake)
    verdicts = screen.screen_with_regeneration(
        items, object(), _cfg(), "interp",
        lambda i, fb: "at step 4 do X again")     # regeneration stays tactical
    assert verdicts[0]["verdict"] == "rejected"


def test_regeneration_unavailable_is_rejected(monkeypatch):
    items = ["At step 4 do X."]
    fake = H.FakeStage({"interp_screen": H.screen_by_rule(
        lambda t: "revise" if "step" in t else "pass")})
    monkeypatch.setattr(common, "run_json_stage", fake)
    verdicts = screen.screen_with_regeneration(
        items, object(), _cfg(), "interp", lambda i, fb: None)
    assert verdicts[0]["verdict"] == "rejected"
    assert fake.count("interp_screen") == 1, "nothing to re-judge"


def test_batching_covers_all_items(monkeypatch):
    items = [f"clean behavioral claim number {i}" for i in range(23)]
    fake = H.FakeStage({"interp_screen": H.screen_all("pass")})
    monkeypatch.setattr(common, "run_json_stage", fake)
    verdicts = screen.screen_items(items, object(), _cfg(screen_batch_size=10),
                                   stage="interp")
    assert len(verdicts) == 23
    assert all(v["verdict"] == "pass" for v in verdicts)
    # 23 items / batch 10 -> 3 judge calls
    assert fake.count("interp_screen") == 3
    assert [v["index"] for v in verdicts] == list(range(23))


def test_missing_verdicts_fail_open(monkeypatch):
    items = ["a", "b", "c"]
    # judge returns no verdicts at all -> every item defaults to pass (the
    # screen is a safety net, not the gate of record), with an audit note.
    fake = H.FakeStage({"interp_screen": lambda u: []})
    monkeypatch.setattr(common, "run_json_stage", fake)
    verdicts = screen.screen_items(items, object(), _cfg(), stage="interp")
    assert [v["verdict"] for v in verdicts] == ["pass", "pass", "pass"]
    assert all(v.get("note") == "screen-missing-defaulted-pass" for v in verdicts)
