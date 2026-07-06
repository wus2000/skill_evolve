"""WebArena agent prompt contract (base harness — deliberately un-optimized).

The base prompt mirrors the official WebArena harness semantics (AXTree
observation, id-based action DSL, one action per turn, ```action``` fencing)
so our zero-skill baseline is community-comparable. It adds exactly ONE
extension the official prompt lacks: the structured final answer required by
the WebArena-Verified evaluator (task_type/status/retrieved_data JSON inside
``stop [...]``). Do NOT fold tactical advice into this file — that headroom
belongs to the optimized skill document (CSS layers it as a separate system
section), and hand-tuning here would contaminate the "learned" claim.
"""
from __future__ import annotations

import json

ACTION_SPACE_DESCRIPTION = """\
Web-browser agent operating on an accessibility-tree observation. Each
element line looks like `[1234] button 'Add to cart'` where 1234 is the
element id. Exactly one action per turn, chosen from:

Page operations:
- `click [id]` — click element id
- `type [id] [content] [press_enter_after]` — clear the field, type content;
  press_enter_after is 1 (default, submits) or 0
- `hover [id]` — hover over element id
- `press [key_comb]` — keyboard combo (e.g. Ctrl+v)
- `scroll [down]` / `scroll [up]` — scroll the page

Tab management:
- `new_tab`, `tab_focus [tab_index]`, `close_tab`

Navigation:
- `goto [url]`, `go_back`, `go_forward`

Completion (ends the episode):
- `stop [answer]` — answer MUST be one JSON object:
  {"task_type": "retrieve"|"mutate"|"navigate",
   "status": "SUCCESS"|"NOT_FOUND_ERROR"|"PERMISSION_DENIED_ERROR"|
             "DATA_VALIDATION_ERROR"|"ACTION_NOT_ALLOWED_ERROR"|"UNKNOWN_ERROR",
   "retrieved_data": <list of retrieved values for retrieve tasks, else null>}
"""

SYSTEM_PROMPT = """\
You are an autonomous web agent completing a task on a website through a
browser. Each turn you receive the task objective, the current page URL, the
page's accessibility tree (elements prefixed with numeric ids in brackets),
and the result of your previous action.

{action_space}

Rules:
1. Issue exactly ONE action per turn.
2. Only reference element ids present in the CURRENT observation.
3. Think briefly first, then end your reply with the action fenced in triple
   backticks, introduced verbatim as: In summary, the next action I will
   perform is ```action```
4. When the task is complete (or genuinely impossible), issue `stop [...]`
   with the JSON answer contract above. Report honest status: use
   "NOT_FOUND_ERROR" when the requested thing does not exist, an error status
   when the site forbids the operation — do not fabricate SUCCESS.
5. For retrieve tasks put the answer value(s) in `retrieved_data` as a list
   (numbers as plain numbers, strings exactly as shown on the page). For
   mutate/navigate tasks set `retrieved_data` to null.
"""


def build_system_prompt(skill_text: str) -> str:
    """Base contract + the (possibly empty) optimized skill document."""
    base = SYSTEM_PROMPT.format(action_space=ACTION_SPACE_DESCRIPTION)
    skill = (skill_text or "").strip()
    if not skill:
        return base
    return (base + "\n## Acquired skill document (follow where applicable)\n"
            + skill + "\n")


def build_turn(objective: str, url: str, observation: str,
               last_action: str, last_result: str) -> str:
    """One user turn: objective + page state + previous-action feedback."""
    parts = [f"OBJECTIVE: {objective}", f"CURRENT URL: {url}",
             f"OBSERVATION:\n{observation}"]
    if last_action:
        parts.append(f"PREVIOUS ACTION: {last_action}")
    if last_result:
        parts.append(f"PREVIOUS ACTION RESULT: {last_result}")
    return "\n\n".join(parts)


def parse_stop_payload(raw: str) -> dict:
    """Parse the JSON object inside ``stop [...]`` — lenient, never raises.

    Falls back to wrapping the raw text as retrieved_data so a sloppy but
    correct textual answer still reaches the evaluator's normalizer.
    """
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`\n ")
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and "task_type" in obj and "status" in obj:
            obj.setdefault("retrieved_data", None)
            obj.setdefault("error_details", None)
            return obj
    except (json.JSONDecodeError, ValueError):
        pass
    return {"task_type": "retrieve",
            "status": "SUCCESS" if text else "UNKNOWN_ERROR",
            "retrieved_data": [text] if text else None,
            "error_details": None if text else "empty stop payload"}
