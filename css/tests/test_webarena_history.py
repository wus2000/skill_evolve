"""WebArena trajectory history (agreed 2026-07-08): mechanical effect
signatures, no-change folding, scribe triggering/parsing, anti-bloat
telemetry (never truncation), and the two transcripts' shapes."""
from __future__ import annotations

import logging

from css.envs.webarena.agent import build_canonical_user
from css.envs.webarena.history import (Scribe, TrajectoryHistory,
                                       effect_signature, parse_scribe_reply,
                                       short_path)
from css.envs.webarena.prompts import build_scribe_user, build_turn

OBS_A = "[1] link 'Home'\n[2] button 'dev'\n[3] text 'hello'"
OBS_B = "[1] link 'Home'\n[2] button 'dev'\n[9] link 'master'"


# ── mechanical layer ─────────────────────────────────────────────────────────
def test_effect_signature_states():
    nav, ch = effect_signature("http://x/a", "http://x/b", OBS_A, OBS_B)
    assert nav == "navigated to /b" and ch
    upd, ch = effect_signature("http://x/a", "http://x/a", OBS_A, OBS_B)
    assert upd == "URL unchanged; page updated (+1/-1 lines)" and ch
    flat, ch = effect_signature("http://x/a", "http://x/a", OBS_A, OBS_A)
    assert flat == "no visible page change" and not ch
    inv, ch = effect_signature("http://x/a", "http://x/a", OBS_A, OBS_A,
                               invalid=True)
    assert inv == "INVALID action (not executed)" and not ch
    fail, ch = effect_signature("http://x/a", "http://x/a", OBS_A, OBS_A,
                                exec_error="TimeoutError: t")
    assert fail.startswith("action FAILED (TimeoutError: t)") and not ch


def test_short_path_keeps_query_drops_origin():
    assert short_path("http://localhost:8023/a/b?ref=m") == "/a/b?ref=m"
    assert short_path("http://localhost:8023") == "/"


def _hist(**kw) -> TrajectoryHistory:
    return TrajectoryHistory(**kw)


def test_folding_and_scribe_content_blocks():
    h = _hist()
    h.append(turn=1, action="click [1]", url="http://x/a",
             effect="navigated to /b", page_changed=True)
    h.records[-1].intent = "open the list"
    h.records[-1].facts = ["page has 5 pages"]
    for t in (2, 3, 4):                          # contentless no-change run
        h.append(turn=t, action=f"click [{t % 2}]", url="http://x/b",
                 effect="no visible page change", page_changed=False)
    h.append(turn=5, action="click [9]", url="http://x/b",
             effect="URL unchanged; page updated (+2/-0 lines)",
             page_changed=True)
    block = h.render(turn_now=6, max_turns=30)
    assert "--- t2..t4 @ /b ---" in block, "run of 3 folds into one line"
    assert "folded, 3 steps" in block
    assert "--- t1 @ /a ---" in block and "FACTS:" in block
    assert "--- t5 @ /b ---" in block, "page-changing step renders alone"
    assert block.count("--- t") == 3


def test_single_no_change_step_not_folded():
    h = _hist()
    h.append(turn=1, action="click [1]", url="http://x/a",
             effect="no visible page change", page_changed=False)
    block = h.render(turn_now=2, max_turns=30)
    assert "--- t1 @ /a ---" in block and "folded" not in block


def test_should_scribe_follows_previous_page_change():
    h = _hist()
    h.append(turn=1, action="a", url="u", effect="e", page_changed=True)
    assert h.should_scribe(), "landing observation is always fresh"
    h.append(turn=2, action="a", url="u", effect="e", page_changed=False)
    assert h.should_scribe(), "prev step changed the page -> new obs"
    h.append(turn=3, action="a", url="u", effect="e", page_changed=False)
    assert not h.should_scribe(), "prev step changed nothing -> same obs"


def test_alerts_repeat_and_flat_tail():
    h = _hist()
    h.append(turn=1, action="click [7]", url="u", effect="x", page_changed=True)
    for t in (2, 3, 4, 5):
        h.append(turn=t, action="click [7]", url="u",
                 effect="no visible page change", page_changed=False)
    block = h.render(turn_now=6, max_turns=30)
    assert "'click [7]' repeated 4 times with no page change" in block
    h2 = _hist()
    for t, a in enumerate(["a", "b", "a", "b", "a"], start=1):
        h2.append(turn=t, action=f"click [{a}]", url="u",
                  effect="no visible page change", page_changed=False)
    block2 = h2.render(turn_now=6, max_turns=30)
    assert "actions produced no page change" in block2


def test_no_change_streak():
    h = _hist()
    h.append(turn=1, action="click [1]", url="u", effect="navigated to /b",
             page_changed=True)
    assert h.no_change_streak() == 0, "a page-changing step resets the streak"
    for t in (2, 3, 4):
        h.append(turn=t, action="click [9]", url="u",
                 effect="no visible page change", page_changed=False)
    assert h.no_change_streak() == 3
    h.append(turn=5, action="scroll [down]", url="u",
             effect="URL unchanged; page updated (+3/-0 lines)",
             page_changed=True)
    assert h.no_change_streak() == 0, "any real change resets it"
    # invalid / failed steps count as no-change (page untouched)
    h.append(turn=6, action="stop {bad}", url="u",
             effect="INVALID action (not executed)", page_changed=False)
    assert h.no_change_streak() == 1


def test_render_empty_and_header():
    h = _hist()
    assert h.render(turn_now=1, max_turns=30) == ""
    h.append(turn=1, action="click [1]", url="http://x/a",
             effect="navigated to /b", page_changed=True)
    block = h.render(turn_now=2, max_turns=30)
    assert block.startswith("TRAJECTORY HISTORY")
    assert "turn 2/30" in block and "path: /a" in block


def test_budget_telemetry_warns_but_never_truncates(caplog):
    h = _hist(budget_tokens=10, count_tokens=lambda s: 999)
    h.append(turn=1, action="click [1]", url="u",
             effect="navigated to /b", page_changed=True)
    h.records[-1].facts = ["x" * 200]
    with caplog.at_level(logging.WARNING):
        block = h.render(turn_now=2, max_turns=30)
    assert "exceeds the 10-token alert budget" in caplog.text
    assert "x" * 200 in block, "content is never cut"
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        h.render(turn_now=3, max_turns=30)
    assert "alert budget" not in caplog.text, "warned once per episode"


# ── scribe ────────────────────────────────────────────────────────────────────
def test_parse_scribe_reply_shapes():
    intent, facts = parse_scribe_reply(
        "INTENT: switch branch to master\n"
        "FACT: dropdown contains link 'master' (href: /x?ref=master)\n"
        "FACT: chart shows branch 'development'\n")
    assert intent == "switch branch to master" and len(facts) == 2
    assert parse_scribe_reply("INTENT: only intent\n") == ("only intent", [])
    assert parse_scribe_reply("total garbage") == ("", [])
    assert parse_scribe_reply("INTENT: i\nFACT: (none)\nFACT: none") == ("i", [])
    _, many = parse_scribe_reply(
        "INTENT: i\n" + "\n".join(f"FACT: f{k}" for k in range(5)))
    assert many == ["f0", "f1", "f2"], "structural count cap at 3"


def test_scribe_is_greedy_not_a_rollout():
    """The scribe is env bookkeeping: greedy like every non-rollout call, and
    it must never inherit the rollout sampling temperature."""
    seen = {}

    class Spy:
        def complete_target(self, system, user, **kw):
            seen.update(kw)
            return "INTENT: x"
    Scribe(Spy()).transcribe(objective="o", url="u", observation="x",
                             reasoning="r", action_str="a", effect="e")
    assert seen.get("temperature") == 0.0


def test_scribe_failure_is_an_empty_contribution():
    class Boom:
        def complete_target(self, *a, **k):
            raise RuntimeError("endpoint down")
    s = Scribe(Boom())
    assert s.transcribe(objective="o", url="u", observation="x",
                        reasoning="r", action_str="a", effect="e") == ("", [])
    assert s.telemetry and "error" in s.telemetry[-1]


def test_scribe_user_carries_no_eval_channel():
    u = build_scribe_user(objective="o", url="u", observation="x",
                          reasoning="r", action_str="a", effect="e")
    assert "OBJECTIVE: o" in u and "eval" not in u.lower()


# ── prompt / transcript shapes ────────────────────────────────────────────────
def test_build_turn_single_turn_shape():
    t = build_turn("obj", "TRAJECTORY HISTORY (...)", "http://x/a", "OBS",
                   "Action failed: Timeout")
    assert t.index("TRAJECTORY HISTORY") < t.index("CURRENT URL")
    assert t.rstrip().endswith("PREVIOUS ACTION RESULT: Action failed: Timeout")
    assert "TRAJECTORY HISTORY" not in build_turn("obj", "", "u", "OBS", "")


def test_canonical_user_shape():
    first = build_canonical_user(turn=1, url="http://x/a", objective="obj")
    assert first.splitlines()[0] == "OBJECTIVE: obj"
    later = build_canonical_user(turn=4, url="http://x/b",
                                 prev_effect="navigated to /b",
                                 prev_facts=["f1", "f2"])
    assert "RESULT OF t3: navigated to /b" in later
    assert "KEY FACTS FROM t3'S PAGE:" in later and "- f2" in later
    assert later.rstrip().endswith("[t4] now at: /b")
