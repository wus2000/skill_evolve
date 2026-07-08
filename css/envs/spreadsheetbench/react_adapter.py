"""Adapter bridging CSS's LLM client to the react_agent's LLMClient interface.

The CSS rollout infrastructure passes a ``target_client`` (TargetOnlyClient or
TracingLLMClient wrapping one) to ``run_one``.  The ReAct agent loop expects
a ``react_agent.LLMClient`` with ``chat()`` / ``chat_async()``.  This adapter
bridges the two by converting react_agent ``Message`` lists into the
``complete_target_messages`` call that CSS's client understands.

This also produces the system prompt template and task prompt that match
Trace2Skill's ``cli_skill_preloaded`` agent setting, ensuring experimental
alignment with Trace2Skill's SpreadsheetBench evaluation.
"""
from __future__ import annotations

import os

from css.envs.spreadsheetbench.agent.models import (
    LLMClient,
    Message,
    ModelSettings,
    RequestContextLengthExceeded,
)


# ── System prompt template ──────────────────────────────────────────────────
# Verbatim from Trace2Skill's cli_skill_preloaded_full_system_v1.txt,
# with {tool_definitions}, {skill_content}, and {skill_dir} placeholders.

SYSTEM_TEMPLATE = r"""You are a spreadsheet expert who can manipulate spreadsheets through Python code.

You need to solve the given spreadsheet manipulation question, which contains the following information:
- working_directory: The absolute path to your working directory where files are located.
- instruction: The question about spreadsheet manipulation.
- spreadsheet_path: The absolute path of the spreadsheet file you need to manipulate.
- spreadsheet_content: The first few rows of the content of spreadsheet file.
- instruction_type: There are two values (Cell-Level Manipulation, Sheet-Level Manipulation) used to indicate whether the answer to this question applies only to specific cells or to the entire worksheet.
- answer_position: The position need to be modified or filled. For Cell-Level Manipulation questions, this field is filled with the cell position; for Sheet-Level Manipulation, it is the maximum range of cells you need to modify. You only need to modify or fill in values within the cell range specified by answer_position.
- output_path: The absolute path where you must save the modified spreadsheet.

## CRITICAL RESTRICTIONS

You can ONLY read and write files within the allowed directories specified in your task context.

- **Input file**: Read from spreadsheet_path
- **Output file**: Write to output_path (use the EXACT path provided)
- **Temporary files**: Create only in working_directory

Do NOT access files outside the allowed directories. Always use absolute paths as provided.

Your goal is to produce the modified spreadsheet at output_path.

To do so, you have been given access to a **bash tools**, helping you interact with the file system.

The tool call you write is an action: after you output an Action, the tool will be executed and the user will provide the result as an "Observation:" message. You must wait for this observation before continuing - do NOT generate observations yourself.
This Action/Observation can repeat N times, you should take several steps when needed.

You can think step-by-step before taking an action.

## CRITICAL: Action Format Requirements

Thought: <current evidence + next step>

Action:
{
    "name": "bash",
    "arguments": {"command": "YOUR_COMMAND_HERE"}
}

IMPORTANT:
- The word "Action:" must appear on its own line, followed by a JSON object
- The JSON must have "name" (string) and "arguments" (object) fields
- Do NOT use markdown code blocks (```json or ```bash) around actions
- Do NOT write raw commands or code outside of the Action JSON format
- Any other format will NOT be parsed and the tool will NOT execute

## Completing the Task (STRICT)

When you have finished the task, signal completion by outputting exactly:

ACTION: TASK_COMPLETE

This tells the system you are done. Do NOT use any other method to end the task.

{tool_definitions}

## Action Examples

### Execute Python code:

Action:
{
    "name": "bash",
    "arguments": {"command": "python -c \"PYTHON_CODE_HERE\""}
}

### Write and execute a solution script:

Action:
{
    "name": "bash",
    "arguments": {"command": "cat <<'EOF' > solution.py\nPYTHON_CODE_HERE\nEOF\npython solution.py"}
}

### Edit a file with sed:

Action:
{
    "name": "bash",
    "arguments": {"command": "sed -i 's/TO_REPLACE/REPLACEMENT_STRING/g' TARGET_FILE"}
}

### Signal task completion:

When you have successfully created the output file:

ACTION: TASK_COMPLETE

Note: The above examples are just reference actions for inspiration. You should adapt your actions based on context and take any action that you deem appropriate.

Action:
{
    "name": "bash",
    "arguments": {"command": "# Any other command you deem appropriate"}
}

Remember:
- Use "Action:" followed by a JSON object to call a tool - no other format works
- Wait for "Observation:" to see the result before proceeding
- When you have finished the task, output "ACTION: TASK_COMPLETE"
- Think step-by-step before taking action when the problem is complex

## Relevant Skill

The following skill is loaded for your reference.

### What are Skills?
- Skills are local instructions stored in SKILL.md files that provide detailed guidance for specific tasks.
- The following <skill_content> shows the content of SKILL.md file.

### Mandatory Skill Usage Protocol
- First analyze the task and spreadsheet_content, plan how you will use the skill.
- During your task execution, if the skill contains relevant guidance, you must follow it.
- Only act on your own judgement if:
    - No skill is relevant to the task, OR
    - The skill does not cover the specific operation you need to perform.

<skill_content>
{skill_content}
</skill_content>"""


def build_system_template(skill_content: str) -> str:
    """Build the full system template with skill content injected.

    The ``{tool_definitions}`` placeholder is preserved for the ReAct
    converter to fill in at runtime.
    """
    return SYSTEM_TEMPLATE.replace("{skill_content}", skill_content)


def build_task_prompt(
    instruction: str,
    working_dir: str,
    input_file: str,
    output_file: str,
    spreadsheet_content: str,
    instruction_type: str = "",
    answer_position: str = "",
) -> str:
    """Build the user task prompt matching Trace2Skill's format."""
    return f"""Below is the spreadsheet manipulation question you need to solve:

### working_directory
{working_dir}

### instruction
{instruction}

### spreadsheet_path
{input_file}

### spreadsheet_content
{spreadsheet_content}

### instruction_type
{instruction_type}

### answer_position
{answer_position}

### output_path
{output_file}

---
**REMINDER**: Write files ONLY in `{working_dir}`. Save output to exact path: `{output_file}`
---

Solve the question and save the modified spreadsheet to the exact output_path shown above."""


# ── CSS LLM Client → react_agent LLMClient adapter ─────────────────────────


class CSSLLMClientAdapter(LLMClient):
    """Adapts a CSS target_client to the react_agent.LLMClient interface.

    The ReAct agent calls ``chat(messages, settings)`` with a list of
    ``Message(role, content)`` objects.  This adapter converts them to the
    ``list[dict]`` format CSS's ``complete_target_messages`` expects.

    The ``stop`` sequences from ``ModelSettings`` are NOT forwarded because
    CSS's ``OpenAICompatLLMClient`` does not support stop sequences in its
    ``_call`` method.  Instead the agent relies on the ReAct converter's
    post-hoc parsing of ``Observation:`` markers.
    """

    def __init__(self, target_client, *, max_tokens: int = 16384,
                 temperature: "float | None" = None):
        self._client = target_client
        self._max_tokens = max_tokens
        self._temperature = temperature

    def chat(self, messages: list[Message], settings: ModelSettings | None = None) -> str:
        dict_messages = [{"role": m.role, "content": m.content} for m in messages]
        max_tokens = self._max_tokens
        # The vendored ReAct loop's ModelSettings carries its own temperature
        # default (0.7). It must NOT override the agreed rollout temperature —
        # that policy belongs to the client (user ruling 2026-07-08). Only
        # max_tokens is honored from settings.
        temperature = self._temperature
        if settings and settings.max_tokens:
            max_tokens = settings.max_tokens
        try:
            return self._client.complete_target_messages(
                dict_messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except RuntimeError as e:
            msg = str(e).lower()
            if "context length" in msg or "maximum input length" in msg or "input_tokens" in msg:
                raise RequestContextLengthExceeded(str(e)) from e
            raise

    async def chat_async(self, messages: list[Message], settings: ModelSettings | None = None) -> str:
        return self.chat(messages, settings)
