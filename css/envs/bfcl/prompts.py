"""BFCL agent prompts + action-space description.

The base system prompt is deliberately MINIMAL — it describes only the mechanics
(multi-turn tool calling; stop calling when a turn is done). It does NOT tell the
model to ask-when-ambiguous or refuse-absent-tools: those are exactly the
procedural conventions the SKILL DOCUMENT is meant to install, so baking them in
would erase the headroom we optimize against (the zero-skill probe showed the
frozen model fails all 4/4 refrain checkpoints without them). The skill document
is appended verbatim below the mechanics.
"""
from __future__ import annotations

# Upstream verbatim (bfcl_eval DEFAULT_USER_PROMPT_FOR_ADDITIONAL_FUNCTION_FC):
# injected on a miss_func holdout turn, where the withheld tools become available
# and the scripted user message is empty.
CANNED_HOLDOUT_PROMPT = "I have updated some more functions you can choose from. What about now?"

_BASE_SYSTEM = (
    "You are an assistant that completes tasks by calling the tools provided to you. "
    "The conversation may span multiple user turns. For each user turn, call the tools you "
    "need to fulfill that turn's request, reading each tool's result before deciding the next "
    "call. When the current turn's request has been handled, stop calling tools; the next user "
    "turn (if any) will then be sent."
)


def build_system(skill_text: str) -> str:
    """Base mechanics prompt + the skill document (if any), appended verbatim."""
    skill = (skill_text or "").strip()
    if skill:
        return _BASE_SYSTEM + "\n\n# Skill document\n" + skill
    return _BASE_SYSTEM


ACTION_SPACE_DESCRIPTION = (
    "The agent is a native function-calling assistant driving a short multi-turn dialog "
    "(mean ~4 user turns). At the start of each user turn it is given a set of tool schemas "
    "(the functions of 1-2 simulated backends among: a file system, a trading bot, a travel "
    "booking API, a vehicle control API, a math API, a Twitter/posting API, a messaging API, and "
    "a ticketing API — 128 functions total). Interaction loop per turn:\n"
    "- The model emits zero or more tool calls (name + JSON arguments); each call is executed "
    "against the live backend and its result is returned as a tool observation.\n"
    "- The model keeps calling until it stops emitting calls, which ends the turn (capped at 20 "
    "steps; exceeding the cap fails the entry).\n"
    "- The next scripted user turn is then delivered. Backend state persists across turns.\n"
    "Two special challenge shapes occur: (1) a needed tool may be ABSENT from the tool list "
    "until a later turn (the correct move is to not fabricate it and wait); (2) a user request "
    "may UNDER-SPECIFY a required argument (the correct move is to ask, not guess). Scoring is "
    "deterministic: after each turn the backend state and the sequence of executed calls must "
    "match a reference solution; every turn must pass."
)
