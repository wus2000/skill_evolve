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
browser. Each turn you receive the task objective, a TRAJECTORY HISTORY block
(bookkeeping maintained by the harness: your past actions with their
mechanical effects, your stated intent at each step, and key facts
transcribed from earlier pages), the current page URL, the page's
accessibility tree (elements prefixed with numeric ids in brackets), and the
result of your previous action.

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


def build_turn(objective: str, history_block: str, url: str, observation: str,
               last_result: str) -> str:
    """One SINGLE-TURN prompt (survey field standard, agreed 2026-07-08):
    the current observation is the only observation; the past arrives as the
    harness-maintained TRAJECTORY HISTORY block. The previous action and its
    mechanical effect live inside that block; ``last_result`` carries only
    the verbatim error feedback of a failed/invalid previous action."""
    parts = [f"OBJECTIVE: {objective}"]
    if history_block:
        parts.append(history_block)
    parts += [f"CURRENT URL: {url}", f"OBSERVATION:\n{observation}"]
    if last_result:
        parts.append(f"PREVIOUS ACTION RESULT: {last_result}")
    return "\n\n".join(parts)


# ── env-side trajectory scribe (agreed 2026-07-08) ───────────────────────────
# The scribe is ENVIRONMENT bookkeeping, deliberately un-optimized like the
# base prompt above: it transcribes, it never coaches. Strategy (when to note
# what, how to break loops) is the skill document's learning target — do not
# fold advice-shaped language into this prompt.

SCRIBE_SYSTEM = """\
You are a neutral trajectory scribe for a web-browsing agent. After a step
the agent takes, you write the step's log entry for a running history the
agent will see in later turns. You are part of the environment's
bookkeeping — you are not the agent and not its coach.

You receive: the task objective, the page observation the agent acted on
(an accessibility tree), the agent's verbatim reasoning for this step, the
action it issued, and the mechanical result of executing it.

Reply with EXACTLY this line format and nothing else:
INTENT: <one line restating what the agent was trying to achieve this step>
FACT: <one key fact worth remembering, if any>
FACT: <another, if any — at most 3 FACT lines>

Rules for FACT lines:
1. Record ONLY page content that later turns are likely to need and that
   will likely no longer be visible then (the agent moves between pages).
   Exactly three kinds qualify:
   - data directly relevant to the objective (names, numbers, emails,
     titles, prices — copy them EXACTLY as shown, with just enough context
     to use them later);
   - structural findings that affect navigation (a link's exact href, "the
     list has 5 pages", "only 20 rows are shown at a time");
   - an outcome that contradicts what the agent's reasoning expected.
2. Most steps contribute NO new fact. Omitting all FACT lines (INTENT line
   only) is the correct and expected output for routine steps. Never pad,
   never restate the objective, never repeat a fact already implied by the
   action itself.
3. Copy from the page verbatim where possible; keep each FACT to one short
   line; one fact per line, never merged.
4. FACTS describe the page, not the agent. The mechanical result is already
   recorded by the harness — do not restate it. Never write advice,
   evaluations, predictions, or suggestions (no "should", "consider",
   "seems", "unreliable"). Strategy is not your job.
5. INTENT restates the agent's own stated goal for THIS step in one neutral
   line. If the reasoning states no goal, write exactly: INTENT: (not stated)
"""


def build_scribe_user(*, objective: str, url: str, observation: str,
                      reasoning: str, action_str: str, effect: str) -> str:
    return "\n\n".join([
        f"OBJECTIVE: {objective}",
        f"PAGE URL: {url}",
        f"OBSERVATION THE AGENT ACTED ON:\n{observation}",
        f"AGENT'S REASONING (verbatim):\n{reasoning or '(empty)'}",
        f"ACTION ISSUED: {action_str}",
        f"MECHANICAL RESULT: {effect}",
    ])


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
