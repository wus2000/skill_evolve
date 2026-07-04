"""ScienceWorld agent prompts — command grammar, format contract, system template.

Design decisions (integration 2026-07-04; see docs/env_prep/scienceworld_ONBOARDING.md):
  * The generic ACTION TEMPLATES are interface documentation given to the agent
    (like ALFWorld's command grammar / Bird's tool schemas). ScienceWorld's
    per-state *valid* action list (hundreds of object-bound combinations) is NOT
    shown; instead the agent may emit the meta-action ``check valid actions`` to
    receive the template list on demand (AgentBoard-faithful). Dynamic action
    knowledge (which object, which room, task workflow) is the learnable surface
    the skill document fills.
  * Output protocol: exactly two tags, ``<reasoning>`` and ``<action>`` — the
    same contract as ALFWorld. The tag is deliberately NOT ``<think>`` (a Qwen
    reserved thinking token stripped from content at enable_thinking=false).
    Rigidity lives only in ``<action>`` (parsed; missing tag falls back to the
    safe free action ``look around`` and is COUNTED, never silently repaired).
  * No 1-shot example trajectory (AgentBoard's VanillaAgent uses one). Our agent
    is skill-document-driven: strategy lives in the skill doc + rolling history,
    not a baked-in example. This is a deliberate deviation — AgentBoard fidelity
    is about the test SUBSET + SUBGOAL SCORING + STEP BUDGET + SIMPLIFICATIONS,
    not the agent's prompt shape.

GROUND-TRUTH FIREWALL: nothing here reads gold paths; the agent sees only the
task goal, observations, and its own history.
"""
from __future__ import annotations

# The generic action grammar. Faithful to ScienceWorld 1.2.3 + the AgentBoard
# command reference. Object/location placeholders are filled from what the agent
# observes. Kept as agent-facing documentation of the action space.
COMMAND_TEMPLATES = """\
Available actions (fill OBJ/LOC with things you can see; one action per turn):
  Manipulation:
    open OBJ / close OBJ            open or close a container or door
    pick up OBJ / put down OBJ      move an object to/from your inventory
    move OBJ to OBJ                 transfer an object into/onto another
    pour OBJ into OBJ               pour a substance
    dunk OBJ into OBJ               immerse a container into a liquid
    mix OBJ                         chemically combine a container's contents
  Inspection:
    look around                    survey the current room (free action)
    look at OBJ / look in OBJ       examine an object / a container's contents
    read OBJ                        read written content
  Devices:
    activate OBJ / deactivate OBJ   turn a device on/off
    use OBJ [on OBJ]                use an instrument (e.g. a thermometer on a substance)
  Movement:
    go to LOC                      walk to a connected room
  Task-critical:
    focus on OBJ                   declare the object the task is about (often
                                   scored; focusing on the WRONG object can fail
                                   the task, so focus deliberately)
    wait / wait1 / wait DURATION   let time pass (for boiling, melting, growth)
  Information (free actions):
    task                           restate the objective
    inventory                      list what you are carrying
    check valid actions            list the action templates available now"""

SYSTEM_TEMPLATE = """\
You are an agent in the ScienceWorld text environment, a simulated science \
school. Complete the task described in the goal by issuing one action per turn. \
After each action you see the resulting observation. Work step by step.

{command_templates}

An invalid or ungrammatical action returns a message such as "No known action \
matches that input"; treat it as feedback and reformulate. Some tasks penalize \
wrong actions — a careless action can drive the score negative and end the \
episode, so act deliberately.

Respond with exactly:
<reasoning>brief reasoning</reasoning>
<action>one action</action>

{skill_block}"""

SKILL_BLOCK_TEMPLATE = """\
## Skill document (your accumulated strategy and rules — follow it)
{skill_text}"""

# The first user turn: the goal + the opening observation + inventory.
FIRST_USER_TEMPLATE = """\
Goal: {goal}

{observation}

{inventory}"""


def build_system_prompt(skill_text: str) -> str:
    """Compose the agent system prompt with the skill document injected verbatim."""
    skill = (skill_text or "").strip()
    skill_block = SKILL_BLOCK_TEMPLATE.format(skill_text=skill) if skill else ""
    return SYSTEM_TEMPLATE.format(
        command_templates=COMMAND_TEMPLATES,
        skill_block=skill_block,
    ).rstrip() + "\n"


def build_first_user(goal: str, observation: str, inventory: str) -> str:
    return FIRST_USER_TEMPLATE.format(
        goal=(goal or "").strip(),
        observation=(observation or "").strip(),
        inventory=(inventory or "").strip(),
    ).strip()


# L1 paradigm-design context (env contract: action_space_description()).
ACTION_SPACE_DESCRIPTION = """\
Interaction pattern: multi-turn text simulation (ReAct). Each turn the agent
receives an observation as a user message and must reply with
<reasoning>...</reasoning><action>one action</action>. Exactly one action per
turn; a missing/unparseable action tag is executed as 'look around'. The agent
may emit 'check valid actions' to receive the generic action templates for the
current state (the per-state list of valid object-bound actions is otherwise
not shown). The episode ends when the task is solved, at the step budget, or if
the score is driven negative (some tasks penalize wrong actions).

Scoring is continuous: ScienceWorld reports a 0-100 progress score (fraction of
the task's subgoals achieved); many tasks require a specific sequence — navigate
to the object's room, gather instruments, 'focus on' the correct object, perform
the state change / measurement, then read the result. Focusing on the wrong
object can fail the task outright.

""" + COMMAND_TEMPLATES + """

Task families (~20 elementary-science task types across ~8 domains): changes of
state (boil/melt/freeze), measurement (use-thermometer, melting points),
classification (find living/non-living/plant/animal), chemistry (mixing, paints),
biology (lifespans, life stages). Object locations, room layout, and per-task
workflows are NOT documented by the environment — they must be learned."""
