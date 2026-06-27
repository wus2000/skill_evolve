"""LLM client abstractions for the ReAct agent (CSS-adapted).

CSS uses its own OpenAICompatLLMClient / TracingLLMClient for LLM calls.
This module provides only the data structures and the abstract LLMClient
interface that the ReAct agent loop depends on.  The concrete adapter
(CSSLLMClientAdapter) lives in css.envs.spreadsheetbench.react_adapter.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class RequestContextLengthExceeded(RuntimeError):
    """Raised when a request exceeds the model context window."""


@dataclass
class Message:
    """A single message in a conversation."""
    role: str  # "system", "user", "assistant"
    content: str


@dataclass
class ModelSettings:
    """Settings for LLM generation."""
    temperature: float = 0.7
    max_tokens: int | None = None
    stop: list[str] = field(default_factory=list)
    extra_body: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        result = {"temperature": self.temperature}
        if self.max_tokens:
            result["max_tokens"] = self.max_tokens
        if self.stop:
            result["stop"] = self.stop
        if self.extra_body:
            result["extra_body"] = self.extra_body
        return result


class LLMClient(ABC):
    """Abstract base class for LLM clients used by the ReAct agent."""

    @abstractmethod
    def chat(self, messages: list[Message], settings: ModelSettings | None = None) -> str:
        """Send messages to the LLM and get a response."""
        pass

    @abstractmethod
    async def chat_async(self, messages: list[Message], settings: ModelSettings | None = None) -> str:
        """Async version of chat."""
        pass
