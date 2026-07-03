"""ALFWorld agent prompts — grammar docs, format contract, system template.

Protocol decisions (2026-07-03 design discussion):
  * The 13 STATIC command templates are interface documentation (given to the
    agent, like Bird's tool schemas). They are quoted from the environment's
    own built-in ``help`` command (alfred.twl2 grammar), so the doc can never
    drift from the engine. NOTE: the json_2.1.3 grammar uses ``move <obj> to
    <recep>`` where ReAct-era (json_2.1.1) games used ``put <obj> in/on
    <recep>`` — old-paper trajectories are NOT syntax-compatible.
  * The per-step admissible-commands list is NOT shown to the agent: dynamic
    action knowledge (preconditions, search priors, task workflows) is the
    learnable surface the skill document is supposed to fill. A live A/B on
    qwen3.6-35b showed the list is not even beneficial (menu-picking replaced
    planning and lost the episode).
  * Output protocol: exactly two tags, ``<reasoning>`` and ``<action>``.
    Rigidity lives only in ``<action>`` (parsed, fallback ``look`` on failure);
    ``<reasoning>`` is requested but tolerated when missing. No plan/checklist
    tags — cognitive structure belongs to the L1 strategy, not the pipeline.
    The tag is deliberately NOT ``<think>``: that is a Qwen reserved thinking
    token — measured on the production endpoint (enable_thinking=false), a
    literal ``<think>...</think>`` block is stripped from ``content`` entirely
    (1/2645 survival), silently deleting the reasoning from trajectories.
    ``<reasoning>`` survives verbatim (probe-verified 2026-07-03).
"""
from __future__ import annotations

# Quoted from the environment's built-in `help` action (alfred.twl2). Kept
# verbatim so agent-facing docs match the engine grammar exactly.
COMMAND_TEMPLATES = """\
Available commands:
  look:                             look around your current location
  inventory:                        check your current inventory
  go to (receptacle):               move to a receptacle
  open (receptacle):                open a receptacle
  close (receptacle):               close a receptacle
  take (object) from (receptacle):  take an object from a receptacle
  move (object) to (receptacle):    place an object in or on a receptacle
  examine (something):              examine a receptacle or an object
  use (object):                     use an object
  heat (object) with (receptacle):  heat an object using a receptacle
  clean (object) with (receptacle): clean an object using a receptacle
  cool (object) with (receptacle):  cool an object using a receptacle
  slice (object) with (object):     slice an object using a sharp object"""

SYSTEM_TEMPLATE = """\
You are an agent in the ALFRED text-based household environment. Complete the \
task stated after 'Your task is to:' by issuing one command per turn.

{command_templates}

Use exact entity names with their numbers as observed (e.g. 'bread 1', \
'countertop 2'). An invalid command returns 'Nothing happens.'.

Respond with exactly:
<reasoning>brief reasoning</reasoning>
<action>one command</action>

{skill_block}"""

SKILL_BLOCK_TEMPLATE = """\
## Skill document (your accumulated strategy and rules — follow it)
{skill_text}"""


def build_system_prompt(skill_text: str) -> str:
    """Compose the agent system prompt with the skill document injected verbatim."""
    skill = (skill_text or "").strip()
    skill_block = SKILL_BLOCK_TEMPLATE.format(skill_text=skill) if skill else ""
    return SYSTEM_TEMPLATE.format(
        command_templates=COMMAND_TEMPLATES,
        skill_block=skill_block,
    ).rstrip() + "\n"


# L1 paradigm-design context (env contract: action_space_description()).
ACTION_SPACE_DESCRIPTION = """\
Interaction pattern: multi-turn text game (ReAct). Each turn the agent receives
the environment observation as a user message and must reply with
<reasoning>...</reasoning><action>command</action>. Exactly one command per turn;
a missing/unparseable action tag is executed as 'look'. The episode ends when
the task goal is satisfied (success) or after the step limit (failure); there
is no partial credit and no terminal 'answer' action.

The environment is a deterministic, partially observable household simulation.
The opening observation lists the receptacles in the room and the task; object
locations must be discovered by going to receptacles and opening closed ones.
Only one object can be held at a time. An invalid or ungrammatical command
returns 'Nothing happens.' with no explanation.

Command templates (the complete action grammar; entities are bound to names
observed in the environment, e.g. 'bread 1'):
""" + COMMAND_TEMPLATES + """

Task family: 6 task types over kitchen/livingroom/bedroom/bathroom scenes —
put X on Y; put two X on Y; examine X under the desklamp; put a clean X on Y;
put a hot X on Y; put a cool X on Y. (Command preconditions, object-location
priors, and per-task-type workflows are NOT documented by the environment —
they must be learned from experience.)"""
