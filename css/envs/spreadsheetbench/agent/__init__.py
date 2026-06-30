"""ReAct Agent for CSS — vendored from Trace2Skill's react_agent.

Self-contained ReAct (Reasoning + Acting) agent implementation.
The core loop (agent.py, converter.py, tools.py, prompts.py) is copied
verbatim from Trace2Skill; only models.py is trimmed to remove the
OpenAIClient/ApiChatClient (CSS uses its own LLM client stack).
"""
from __future__ import annotations

from .agent import ReActAgent, AgentConfig, AgentStep, AgentResult
from .tools import Tool, ToolParameter, tool
from .models import LLMClient, Message, ModelSettings, RequestContextLengthExceeded
from .converter import ReActConverter, ParseResult, ParseResultType, truncate_observation
from .prompts import (
    SystemPromptBuilder,
    PromptContext,
    PromptSection,
    PromptSections,
    RoleSection,
    DomainContextSection,
    ActionFormatSection,
    TaskCompletionSection,
    ExamplesSection,
    ToolDefinitionsSection,
    RemindersSection,
)

__all__ = [
    "ReActAgent",
    "AgentConfig",
    "AgentStep",
    "AgentResult",
    "Tool",
    "ToolParameter",
    "tool",
    "LLMClient",
    "Message",
    "ModelSettings",
    "RequestContextLengthExceeded",
    "ReActConverter",
    "ParseResult",
    "ParseResultType",
    "truncate_observation",
    "SystemPromptBuilder",
    "PromptContext",
    "PromptSection",
    "PromptSections",
    "RoleSection",
    "DomainContextSection",
    "ActionFormatSection",
    "TaskCompletionSection",
    "ExamplesSection",
    "ToolDefinitionsSection",
    "RemindersSection",
]
