"""The Altitude Screen: batched verdicts, one-round revise, immediate reject."""
from __future__ import annotations

from css.config import CSSConfig
from css.materials import common, screen
from css.tests import materials_helpers as H


def _cfg(**kw) -> CSSConfig:
    base = dict(max_api_workers=2, screen_batch_size=10)
    base.update(kw)
    return CSSConfig(**base)


def test_pass_reject_and_revise_once(tmp_path, monkeypatch):
    items = [
        "Exploring broadly before committing improves outcomes here.",   # pass
        "At step 4 the agent should call parse_header first.",            # revise (tactical)
        "It failed. TODO list of fixes for this exact task instance.",    # reject
    ]

    def rule(text: str) -> str:
        if "TODO" in text:
            return "reject"
        if "At step" in text or "at step" in text:
            return "revise"
        return "pass"

    fake = H.FakeStage({
        "interp_screen": H.screen_by_rule(rule),
        # the revise call strips the tactical phrasing -> re-judge passes
        "interp_revise": lambda u: {"revised": "Committing only after broad exploration "
                                    "is the behavioral principle that matters."},
    })
    monkeypatch.setattr(common, "run_json_stage", fake)

    verdicts = screen.screen_items(items, object(), _cfg(), stage="interp")
    assert len(verdicts) == 3

    assert verdicts[0]["verdict"] == "pass" and verdicts[0]["first_verdict"] == "pass"
    assert verdicts[0]["revised"] is False

    # revise -> regenerate once -> re-judge pass
    assert verdicts[1]["first_verdict"] == "revise"
    assert verdicts[1]["verdict"] == "pass"
    assert verdicts[1]["revised"] is True
    assert "At step" not in verdicts[1]["text"]

    # reject is immediate: no revise round, kept-but-excluded
    assert verdicts[2]["first_verdict"] == "reject"
    assert verdicts[2]["verdict"] == "rejected"
    assert verdicts[2]["revised"] is False

    # exactly one revise call happened (only the middle item)
    assert fake.count("interp_revise") == 1


def test_revise_still_failing_is_rejected(monkeypatch):
    items = ["At step 4 do X."]  # tactical

    fake = H.FakeStage({
        "interp_screen": H.screen_by_rule(
            lambda t: "revise" if "step" in t else "pass"),
        # revision keeps the tactical marker -> re-judge still 'revise' -> rejected
        "interp_revise": lambda u: {"revised": "at step 4 do X again"},
    })
    monkeypatch.setattr(common, "run_json_stage", fake)
    verdicts = screen.screen_items(items, object(), _cfg(), stage="interp")
    assert verdicts[0]["verdict"] == "rejected"
    assert verdicts[0]["revised"] is True


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


def test_missing_verdict_fails_open(monkeypatch):
    items = ["a", "b", "c"]
    # judge returns only one verdict for a 3-item batch -> the other two default pass
    fake = H.FakeStage({"interp_screen": lambda u: [
        {"index": 1, "verdict": "reject", "violated_criteria": [1],
         "feedback": "off", "quoted_offense": "a"}]})
    monkeypatch.setattr(common, "run_json_stage", fake)
    verdicts = screen.screen_items(items, object(), _cfg(), stage="interp")
    assert verdicts[0]["verdict"] == "rejected"
    assert verdicts[1]["verdict"] == "pass" and verdicts[2]["verdict"] == "pass"
    assert verdicts[1].get("note") == "screen-missing-defaulted-pass"
