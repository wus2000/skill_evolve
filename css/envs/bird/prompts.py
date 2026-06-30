"""Prompts and tool schemas for the Bird Text-to-SQL task-execution agent.

The agent runs a native OpenAI function-calling ReAct loop with two tools:
``execute_sql`` (explore / validate) and ``submit_final_sql`` (commit). The skill
document (strategy.md + rules.md) is injected into the system prompt.
"""
from __future__ import annotations

# ── Tool schemas (OpenAI function-calling) ──────────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "execute_sql",
            "description": (
                "Execute a read-only SQL query against the SQLite database and "
                "return the results. Use this to explore schema "
                "(e.g. SELECT name FROM sqlite_master WHERE type='table'), "
                "inspect data (e.g. SELECT DISTINCT col FROM t LIMIT 20), "
                "test joins, validate candidate queries, or diagnose errors. "
                "The query runs in read-only mode with a timeout."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "The SQL query to execute.",
                    }
                },
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_final_sql",
            "description": (
                "Submit your final SQL answer. Call this exactly once when you "
                "are confident in your solution. The query must be a valid "
                "SQLite SELECT statement that answers the given question. "
                "After calling this, the interaction ends."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "The final SQL query that answers the question.",
                    }
                },
                "required": ["sql"],
            },
        },
    },
]


# ── Prompt templates ────────────────────────────────────────────────────────

_SYSTEM_TEMPLATE = """\
You are an expert Text-to-SQL agent. Your task is to write a correct SQLite \
query that answers a natural-language question about a given database.

{skill_section}## Environment
You are given a SQLite database schema, a natural-language question, and \
optionally expert evidence that provides domain knowledge (term definitions, \
value mappings, formulas). Your goal: produce ONE correct SQL query answering \
the question.

## Available Tools
You have two tools:
1. **execute_sql** - Run any read-only SQL query and see the results (or error). \
Use this freely to: inspect table structures, check column values, test joins, \
validate candidate queries, or investigate errors.
2. **submit_final_sql** - Submit your final answer SQL. Call this exactly once \
when you are confident. The query must be a valid SQLite SELECT.

## Strategy
- Start by understanding the schema and the question.
- Use the evidence to map question terms to database columns, codes, or values.
- Explore the data with execute_sql to ground your understanding.
- Draft and validate your query with execute_sql before submitting.
- If a query returns an error, diagnose the issue and fix it.
- When confident, call submit_final_sql with your answer.\
"""

_USER_TEMPLATE = """\
## Database: {db_id}

## Schema
{schema}

{evidence_section}## Question
{question}

Analyze the schema and question, then use the tools to explore the database \
and build your answer.\
"""


def build_system(skill_text: str) -> str:
    """Build the system prompt, injecting the skill document if present."""
    skill_section = ""
    if skill_text and skill_text.strip():
        skill_section = f"## Skill\n{skill_text.strip()}\n\n"
    return _SYSTEM_TEMPLATE.format(skill_section=skill_section)


def build_user(item: dict, schema: str) -> str:
    """Build the user prompt from the task item and the rendered schema.

    NEVER includes the gold SQL — the agent only ever sees the schema, the
    question, and any expert evidence.
    """
    evidence_section = ""
    evidence = item.get("evidence")
    if evidence:
        evidence_section = f"## Evidence (external knowledge)\n{evidence}\n\n"
    return _USER_TEMPLATE.format(
        db_id=item.get("db_id", ""),
        schema=schema,
        evidence_section=evidence_section,
        question=item.get("question", ""),
    )
