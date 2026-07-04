"""AppWorld agent prompts (agreed design 2026-07-04).

Design decisions (negotiated, see run_experiment_appworld_server.py):
  * OFFICIAL-ONBOARDING style: the system prompt teaches only the REPL
    protocol, the three api_docs meta-APIs, and the completion contract —
    API documentation itself is discovered by the agent at runtime, so
    "how to explore docs" stays a learnable behavior (L1 surface).
  * The official minimal-ReAct worked example (Spotify password) is kept,
    but EMBEDDED in the system prompt as a compact transcript rather than
    as real few-shot messages — our trajectory contract wants the message
    list to contain only the real episode (analysis reads it verbatim).
  * The response contract is ONE fenced Python code block per turn;
    reasoning goes in # comments (fence-first parsing, no silent repair).
  * NOTHING here reveals task difficulty metadata or ground truth — the
    GT firewall keeps those optimizer-only.
"""
from __future__ import annotations

SKILL_BLOCK_TEMPLATE = """

## Skill document (your accumulated strategy and rules — follow it)

{skill_text}
"""

SYSTEM_TEMPLATE = """\
You are an autonomous AI assistant completing your supervisor's day-to-day \
digital tasks in the AppWorld environment by operating a stateful Python REPL.

ENVIRONMENT PROTOCOL:
- Each turn you write ONE Python code block; the environment executes it and \
returns the printed output, or the error traceback if it fails. Variables \
persist across turns (like a notebook) — reuse logins, tokens, and \
intermediate results instead of recomputing them.
- You interact with apps (e.g. amazon, spotify, venmo, splitwise, gmail, \
phone, simple_note, todoist, file_system, and the supervisor app) through \
their APIs, called as: apis.<app_name>.<api_name>(...)
- Discover what exists with the three documentation meta-APIs:
    print(apis.api_docs.show_app_descriptions())
    print(apis.api_docs.show_api_descriptions(app_name='supervisor'))
    print(apis.api_docs.show_api_doc(app_name='supervisor', api_name='show_account_passwords'))
- Your supervisor's account credentials are available through the supervisor \
app. Outputs can be very long — query narrowly (use the APIs' filter and \
pagination parameters) to keep your context focused.
- The task is complete ONLY when you call apis.supervisor.complete_task(). \
If the task asks a question, submit the result with \
apis.supervisor.complete_task(answer=...). The episode never ends on its own.
- Success is judged by the FINAL DATABASE STATE (plus the answer, when one is \
asked): achieve exactly the requested goal and do not make collateral changes \
to unrelated data.

WORKED EXAMPLE — task: "What is the password for my Spotify account?"
  Turn 1:
    ```python
    # Find which supervisor APIs exist.
    print(apis.api_docs.show_api_descriptions(app_name='supervisor'))
    ```
    -> output includes: show_account_passwords : Show your supervisor's account passwords.
  Turn 2:
    ```python
    passwords = apis.supervisor.show_account_passwords()
    print(passwords)
    ```
    -> output: [{{"account_name": "amazon", "password": "..."}}, {{"account_name": "spotify", "password": "dummy_spotify_pass"}}, ...]
  Turn 3:
    ```python
    # Extract the spotify entry and declare the task complete with the answer.
    spotify_password = [p for p in passwords if p["account_name"] == "spotify"][0]["password"]
    apis.supervisor.complete_task(answer=spotify_password)
    ```

RESPONSE FORMAT: respond with exactly ONE fenced Python code block:
```python
<your code>
```
Put any reasoning in # comments inside the block. No prose outside the block.\
{skill_block}"""

FIRST_USER_TMPL = """\
My name is: {first_name} {last_name}. My personal email is {email} and phone \
number is {phone_number}.

Your task is: {instruction}

Begin now. Respond with your first Python code block."""

# L1 paradigm-design / analysis context (structural semantics only; API
# knowledge and conventions are deliberately absent — they must be learned
# from experience).
ACTION_SPACE_DESCRIPTION = """\
AppWorld: an interactive-coding agent environment over 9 simulated day-to-day
apps (amazon, spotify, venmo, splitwise, gmail, phone, simple_note, todoist,
file_system) plus system apps (supervisor: credentials & task completion;
api_docs: documentation) — ~457 APIs backed by the relational databases of a
~100-person simulated world.

Interaction protocol:
- Per turn the agent writes one Python code block; a persistent IPython shell
  executes it and returns printed output or the error traceback. Variables
  persist across turns, so state (logins, fetched data) carries forward.
- APIs are called as apis.<app>.<api>(...). Documentation is discovered AT
  RUNTIME via the api_docs meta-APIs (app list, per-app API list, per-API
  spec); nothing else about API semantics is given up front.
- Termination is SELF-DECLARED: the episode ends only when the agent calls
  apis.supervisor.complete_task(answer=...) — there is no environment-side
  completion signal, and an undeclared episode runs to the interaction cap.
- Evaluation is STATE-BASED: hidden unit tests over the final databases check
  that the goal state was achieved AND that unrelated data was not modified
  (collateral damage fails); answer-type tasks also check the submitted
  answer. No partial credit within a task.

Typical failure surfaces: API semantics discovered only through tracebacks,
pagination/filtering conventions, authentication flows, cross-app data
pipelines, premature or missing complete_task declaration, and collateral
writes to unrelated records."""


def build_system_prompt(skill_text: str) -> str:
    """Compose the system prompt, with the skill document appended if present."""
    skill_block = ""
    if skill_text and skill_text.strip():
        skill_block = SKILL_BLOCK_TEMPLATE.format(skill_text=skill_text.strip())
    return SYSTEM_TEMPLATE.format(skill_block=skill_block)


def build_first_user(instruction: str, supervisor: dict) -> str:
    """Compose the first user message from the task instruction + supervisor."""
    sup = supervisor or {}
    return FIRST_USER_TMPL.format(
        first_name=sup.get("first_name", ""),
        last_name=sup.get("last_name", ""),
        email=sup.get("email", ""),
        phone_number=sup.get("phone_number", ""),
        instruction=instruction or "",
    )
