"""Trajectory rendering and lossless-ish truncation for analysis prompts.

Per design D7 the optimizer must see trajectories *faithfully*: reasoning and
action text is never clipped. The ONLY allowed reduction is eliding a single
oversized tool/observation message (content length >= ``tool_trunc``), and even
then we preserve a head and a tail with an explicit elision marker so the
optimizer can tell how much was removed. We deliberately do NOT port SkillOpt's
``fmt_minibatch_trajectories`` 500/800/2000-char clips (reflect.py:72-90), which
are lossy across every message type.

A message is a ``{"role": ..., "content": ...}`` dict. ``content`` may be a
string or a list of content parts; only string content is measured/elided —
structured content is passed through verbatim (its textual length is not the
single dimension D7 targets, and clipping it would corrupt structure).
"""
from __future__ import annotations

from typing import Any


def _content_str(content: Any) -> str | None:
    """Return ``content`` if it is a plain string, else ``None``.

    Only plain-string content participates in truncation; list/structured
    content is left untouched.
    """
    return content if isinstance(content, str) else None


def truncate_tool_results(messages: list[dict], tool_trunc: int) -> list[dict]:
    """Elide only oversized single tool/observation messages.

    A message is replaced ONLY when its ``content`` is a string of length
    ``>= tool_trunc``. In that case the content becomes::

        head[:tool_trunc // 2] + "\\n...[truncated N chars]...\\n" + tail[-tool_trunc // 2:]

    where ``N`` is the number of characters dropped. Every shorter message and
    all non-string (structured) content is copied byte-identically. The input
    list is not mutated; new dicts are returned for elided messages and the
    originals are reused otherwise.

    ``tool_trunc <= 0`` disables truncation (returns messages unchanged) rather
    than nuking every message to a bare marker — a misconfigured threshold must
    never silently destroy content.
    """
    if tool_trunc <= 0:
        return list(messages)
    half = tool_trunc // 2
    out: list[dict] = []
    for msg in messages:
        content = _content_str(msg.get("content"))
        if content is not None and len(content) >= tool_trunc:
            head = content[:half]
            tail = content[-half:] if half > 0 else ""
            dropped = len(content) - len(head) - len(tail)
            new_content = f"{head}\n...[truncated {dropped} chars]...\n{tail}"
            new_msg = dict(msg)
            new_msg["content"] = new_content
            out.append(new_msg)
        else:
            out.append(msg)
    return out


def format_trajectory(messages: list[dict], *, tool_trunc: int = 4000) -> str:
    """Render messages as a readable transcript for analysis prompts.

    Applies :func:`truncate_tool_results` first, then emits ``"[role]\\ncontent"``
    blocks joined by blank lines. Structured (list) content is stringified for
    display only.
    """
    truncated = truncate_tool_results(messages, tool_trunc)
    blocks: list[str] = []
    for msg in truncated:
        role = str(msg.get("role", ""))
        content = msg.get("content", "")
        if not isinstance(content, str):
            content = str(content)
        blocks.append(f"[{role}]\n{content}")
    return "\n\n".join(blocks)
