"""Scoring + parsing unit tests for the ScienceWorld env (no JVM needed)."""
from __future__ import annotations

import pytest

from css.envs.scienceworld.agent import parse_action, valid_action_templates
from css.envs.scienceworld.scoring import (
    SubgoalMatcher,
    is_invalid_observation,
    native_hard,
    native_soft,
    parse_subgoals,
)


# ── Native predicates (incl. the negative auto-termination case) ────────────
@pytest.mark.parametrize("score,hard,soft", [
    (100, 1, 1.0),
    (99, 0, 0.99),
    (60, 0, 0.60),
    (0, 0, 0.0),
    (-100, 0, 0.0),   # penalized/auto-terminated episode -> failure, soft clamps to 0
])
def test_native_predicates(score, hard, soft):
    assert native_hard(score) == hard
    assert native_soft(score) == pytest.approx(soft)


# ── AgentBoard subgoal matcher (latched regex PR/SR) ────────────────────────
def test_subgoal_matcher_latches_and_progresses():
    subgoals = [
        "You move to the outside",
        "You focus on the crocodile egg",
        r"the thermometer measures a temperature of (-?\d+) degrees celsius",
    ]
    m = SubgoalMatcher(subgoals)
    assert m.pr == 0.0 and m.sr == 0
    m.update("You move to the outside.")
    assert m.pr == pytest.approx(1 / 3) and m.sr == 0
    m.update("You focus on the crocodile egg.")
    assert m.pr == pytest.approx(2 / 3)
    # a later observation that does NOT re-match must not un-latch progress
    m.update("Nothing relevant here.")
    assert m.pr == pytest.approx(2 / 3)
    m.update("the thermometer measures a temperature of -5 degrees celsius")
    assert m.pr == 1.0 and m.sr == 1


def test_subgoal_matcher_partial_stream():
    m = SubgoalMatcher(["a", "b", "c", "d"])
    for obs in ("x a x", "b", "nope"):
        m.update(obs)
    assert m.n_done == 2 and m.pr == 0.5 and m.sr == 0


def test_subgoal_matcher_malformed_regex_degrades_to_literal():
    m = SubgoalMatcher(["(unbalanced"])   # invalid regex
    m.update("here is (unbalanced text")
    assert m.sr == 1


def test_subgoal_matcher_empty():
    m = SubgoalMatcher([])
    assert m.pr == 0.0 and m.sr == 0


# ── Subgoal normalization (both schema forms) ───────────────────────────────
def test_parse_subgoals_string_and_list():
    s = "Subgoal 1: You move to the outside.\nSubgoal 2: You focus on the crocodile egg."
    assert parse_subgoals(s) == ["You move to the outside.", "You focus on the crocodile egg."]
    assert parse_subgoals(["a", "b"]) == ["a", "b"]
    assert parse_subgoals("") == []


# ── Invalid-action detection ────────────────────────────────────────────────
@pytest.mark.parametrize("obs,invalid", [
    ("No known action matches that input.", True),
    ("Ambiguous request: which do you mean?", True),
    ("You move the thermometer to the inventory.", False),
])
def test_is_invalid_observation(obs, invalid):
    assert is_invalid_observation(obs) is invalid


# ── Action parsing ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("reply,expected,ok", [
    ("<reasoning>x</reasoning><action>focus on crocodile egg</action>", "focus on crocodile egg", True),
    ("<ACTION> 'go to kitchen'. </ACTION>", "go to kitchen", True),
    ("<action>\n\nmove metal pot to stove\nextra</action>", "move metal pot to stove", True),
    ("<action>check valid actions</action>", "check valid actions", True),
    ("no tags here", "look around", False),
    ("<action>   </action>", "look around", False),
])
def test_parse_action(reply, expected, ok):
    command, parse_ok = parse_action(reply)
    assert command == expected and parse_ok is ok


def test_parse_action_preserves_case():
    # ScienceWorld referents are not uniformly lowercase — do not fold case.
    cmd, ok = parse_action("<action>use Thermometer on Sodium Chloride</action>")
    assert cmd == "use Thermometer on Sodium Chloride" and ok


def test_valid_action_templates_drops_reset_adds_check():
    class _E:
        def get_possible_actions(self):
            return ["look around", "reset the task", "focus on OBJ"]
    acts = valid_action_templates(_E())
    assert "reset the task" not in acts
    assert "look around" in acts and "check valid actions" in acts
