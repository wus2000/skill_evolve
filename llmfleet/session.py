"""llmfleet.session — session-key extraction.

The session key names a multi-turn conversation so the router can keep it on
one replica (every turn re-sends a strictly growing prefix -> prefix-cache
hits). The default heuristic — system + first user message — is shared by
every turn of an episode and by same-task sibling rollouts, and differs
between tasks. Explicit keys (the ``X-LLMFleet-Session`` header in proxy
mode, or the ``session_key`` argument in library mode) override it.
"""
from __future__ import annotations

SESSION_HEADER = "X-LLMFleet-Session"


def session_key_from_messages(messages: "list[dict]") -> str:
    """Default heuristic: the conversation head (stable across turns)."""
    head = ""
    for m in (messages or [])[:2]:
        try:
            head += str(m.get("role", "")) + "\x00" + str(m.get("content", "")) + "\x01"
        except AttributeError:
            head += "\x01"
    return head


def session_key_from_payload(payload: dict) -> str:
    """Session key for an OpenAI-compatible request body."""
    if not isinstance(payload, dict):
        return ""
    messages = payload.get("messages")
    if isinstance(messages, list) and messages:
        return session_key_from_messages(messages)
    # completions-style: the prompt itself is the only identity there is.
    prompt = payload.get("prompt")
    if isinstance(prompt, str):
        return prompt[:2048]
    return ""
