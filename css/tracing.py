"""Unified tracing and persistent logging for CSS experiment runs.

Records structured events (JSONL) covering every significant mechanism step:
LLM calls (prompts + responses), decisions, scores, gate outcomes, and
intermediate results. Each event is a self-contained JSON line written to
``<out_dir>/trace.jsonl`` — human-reviewable, grep-friendly, and trivially
loadable via ``pandas.read_json(..., lines=True)``.

Two usage patterns:

1. **LLM call wrapping** — :class:`TracingLLMClient` wraps any :class:`LLMClient`
   and records every ``complete_target*`` / ``complete_optimizer*`` call with
   full prompt text, response text, usage, and timing.

2. **Structured event logging** — :func:`log_event` writes an arbitrary event
   dict; callers in orchestrator / analysis / proposal modules use it to record
   decisions, gate outcomes, scores, and intermediate results.

Thread-safe: the JSONL writer uses a threading lock so concurrent rollout
workers (256+) never interleave lines.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Callable


# ── JSONL writer (thread-safe) ───────────────────────────────────────────────

class _JSONLWriter:
    """Append-only, thread-safe JSONL file writer."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        # Touch the file so it exists even if no events are written.
        with open(path, "a", encoding="utf-8"):
            pass

    def write(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line + "\n")


# ── Global trace sinks ──────────────────────────────────────────────────────

_writer: _JSONLWriter | None = None
_llm_writer: _JSONLWriter | None = None
_trace_lock = threading.Lock()
_call_counter = 0
_call_counter_lock = threading.Lock()


def _next_call_id() -> str:
    global _call_counter
    with _call_counter_lock:
        cid = _call_counter
        _call_counter += 1
    return f"call_{cid:06d}"


def init_trace(out_dir: str) -> str:
    """Initialize the global trace sinks. Returns the trace file path.

    Creates two files:
      * ``trace.jsonl`` — compact event log (metadata + summaries, < 1 KB/event)
      * ``llm_calls.jsonl`` — full LLM prompts + responses (can be large)

    The split keeps trace.jsonl fast to grep/load while preserving every LLM
    call verbatim for deep auditability. Events in both files share a ``call_id``
    for cross-reference.
    """
    global _writer, _llm_writer, _call_counter
    path = os.path.join(out_dir, "trace.jsonl")
    llm_path = os.path.join(out_dir, "llm_calls.jsonl")
    with _trace_lock:
        _writer = _JSONLWriter(path)
        _llm_writer = _JSONLWriter(llm_path)
        _call_counter = 0
    return path


def log_event(
    event_type: str,
    *,
    round_index: int = -1,
    node_id: str = "",
    epoch: int = -1,
    step: int = -1,
    **kwargs: Any,
) -> None:
    """Write a structured event to the trace log.

    Silently no-ops if :func:`init_trace` has not been called (unit tests).
    """
    if _writer is None:
        return
    record: dict[str, Any] = {
        "ts": time.time(),
        "event": event_type,
    }
    if round_index >= 0:
        record["round"] = round_index
    if node_id:
        record["node_id"] = node_id
    if epoch >= 0:
        record["epoch"] = epoch
    if step >= 0:
        record["step"] = step
    record.update(kwargs)
    _writer.write(record)


# ── LLM call logging ────────────────────────────────────────────────────────

def _truncate(text: str, limit: int = 50_000) -> str:
    """Soft-truncate long text for trace readability (full text in artifacts)."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[TRUNCATED at {limit}/{len(text)} chars]"


def log_llm_call(
    role: str,
    method: str,
    *,
    system: str = "",
    user: str = "",
    messages: list[dict] | None = None,
    response: str = "",
    usage: dict | None = None,
    duration_s: float = 0.0,
    model: str = "",
    max_tokens: int = 0,
    temperature: "float | None" = -1.0,
    node_id: str = "",
    round_index: int = -1,
    stage: str = "",
    error: str = "",
) -> None:
    """Record a single LLM call — summary to trace.jsonl, full content to llm_calls.jsonl.

    The two files share ``call_id`` for cross-reference. ``trace.jsonl`` stays
    compact (one ~200-byte line per call); ``llm_calls.jsonl`` carries the full
    prompt+response for deep audit.
    """
    call_id = _next_call_id()

    # ── Compact summary → trace.jsonl ──────────────────────────────────
    summary: dict[str, Any] = {
        "call_id": call_id,
        "role": role,
        "method": method,
        "model": model,
        "stage": stage,
        "duration_s": round(duration_s, 3),
        "system_chars": len(system),
        "user_chars": len(user),
        "response_chars": len(response),
    }
    if usage:
        summary["usage"] = usage
    if max_tokens:
        summary["max_tokens"] = max_tokens
    # ``None`` = "the client's configured temperature" (target path); there is
    # nothing call-specific to record.
    if temperature is not None and temperature >= 0:
        summary["temperature"] = temperature
    if error:
        summary["error"] = error
    if messages:
        summary["n_messages"] = len(messages)
        summary["total_message_chars"] = sum(len(str(m.get("content", ""))) for m in messages)

    log_event(
        "llm_call",
        node_id=node_id,
        round_index=round_index,
        **summary,
    )

    # ── Full content → llm_calls.jsonl ─────────────────────────────────
    if _llm_writer is not None:
        full: dict[str, Any] = {
            "ts": time.time(),
            "call_id": call_id,
            "role": role,
            "method": method,
            "model": model,
            "stage": stage,
            "duration_s": round(duration_s, 3),
        }
        if node_id:
            full["node_id"] = node_id
        if round_index >= 0:
            full["round"] = round_index
        if system:
            full["system"] = system
        if user:
            full["user"] = user
        if messages:
            full["messages"] = [
                {"role": m.get("role", ""), "content": str(m.get("content", ""))}
                for m in messages
            ]
        if response:
            full["response"] = response
        if usage:
            full["usage"] = usage
        if error:
            full["error"] = error
        _llm_writer.write(full)


class _NoopContext:
    """No-op context manager for non-tracing clients."""
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        return False


def stage_context(client: Any, stage: str):
    """Get a stage context from a client, safe for any LLMClient type.

    Returns a ``with_stage`` context if the client supports it
    (``TracingLLMClient``), otherwise a no-op context. Callers can
    always write ``with stage_context(client, "label_group"): ...``.
    """
    if hasattr(client, "with_stage"):
        return client.with_stage(stage)
    return _NoopContext()


class _StageContext:
    """Context manager for TracingLLMClient.with_stage()."""

    def __init__(self, client: "TracingLLMClient", stage: str) -> None:
        self._client = client
        self._stage = stage

    def __enter__(self):
        self._client._stage_stack.append(self._stage)
        return self._client

    def __exit__(self, *exc):
        self._client._stage_stack.pop()
        return False


# ── TracingLLMClient wrapper ────────────────────────────────────────────────

class TracingLLMClient:
    """Transparent wrapper that logs every LLM call to the trace sink.

    Wraps any :class:`LLMClient` (including :class:`OpenAICompatLLMClient`
    or :class:`StubLLMClient`). The wrapper is invisible to callers — it
    delegates all calls and adds timing + logging.

    Use :meth:`with_stage` to temporarily tag calls with a semantic label::

        with client.with_stage("layer1_annotate"):
            client.complete_optimizer(system, user)
        # stage reverts automatically
    """

    def __init__(self, inner: Any, *, role: str = "") -> None:
        self._inner = inner
        self._role = role
        self._stage_stack: list[str] = []

    def with_stage(self, stage: str):
        """Context manager that temporarily overrides the stage label for trace."""
        return _StageContext(self, stage)

    def count_tokens(self, text: str, **kwargs):
        fn = getattr(self._inner, "count_tokens", None)
        return fn(text, **kwargs) if fn is not None else None

    def _pop_inner_usage(self) -> "dict | None":
        """Real token usage of the target call that just returned (the target
        interface hands back bare text, so the inner client stashes usage
        thread-locally; None on backends without the stash or on errors)."""
        fn = getattr(self._inner, "pop_last_usage", None)
        return fn() if fn is not None else None

    @property
    def _current_stage(self) -> str:
        return self._stage_stack[-1] if self._stage_stack else self._role

    @property
    def target_model(self) -> str:
        return getattr(self._inner, "target_model", "")

    @property
    def optimizer_model(self) -> str:
        return getattr(self._inner, "optimizer_model", "")

    def complete_target(
        self, system: str, user: str, *, max_tokens: int = 16384,
        temperature: "float | None" = None
    ) -> str:
        t0 = time.time()
        error = ""
        response = ""
        try:
            response = self._inner.complete_target(
                system, user, max_tokens=max_tokens, temperature=temperature
            )
            return response
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            raise
        finally:
            log_llm_call(
                "target", "complete_target",
                system=system, user=user, response=response,
                usage=self._pop_inner_usage(),
                duration_s=time.time() - t0,
                model=self.target_model,
                max_tokens=max_tokens, temperature=temperature,
                stage=self._current_stage, error=error,
            )

    def complete_target_messages(
        self, messages: list[dict], *, max_tokens: int = 16384,
        temperature: "float | None" = None
    ) -> str:
        t0 = time.time()
        error = ""
        response = ""
        try:
            response = self._inner.complete_target_messages(
                messages, max_tokens=max_tokens, temperature=temperature
            )
            return response
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            raise
        finally:
            log_llm_call(
                "target", "complete_target_messages",
                messages=messages, response=response,
                usage=self._pop_inner_usage(),
                duration_s=time.time() - t0,
                model=self.target_model,
                max_tokens=max_tokens, temperature=temperature,
                stage=self._current_stage, error=error,
            )

    def complete_target_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        tool_choice: str = "auto",
        max_tokens: int = 16384,
        temperature: "float | None" = None,
    ) -> dict:
        t0 = time.time()
        error = ""
        result: dict = {}
        try:
            result = self._inner.complete_target_tools(
                messages, tools, tool_choice=tool_choice,
                max_tokens=max_tokens, temperature=temperature,
            )
            return result
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            raise
        finally:
            log_llm_call(
                "target", "complete_target_tools",
                messages=messages, response=str(result),
                usage=self._pop_inner_usage(),
                duration_s=time.time() - t0,
                model=self.target_model,
                max_tokens=max_tokens, temperature=temperature,
                stage=self._current_stage, error=error,
            )

    def complete_optimizer(
        self, system: str, user: str, *, max_tokens: int = 16384
    ) -> tuple[str, dict]:
        t0 = time.time()
        error = ""
        response = ""
        usage: dict = {}
        try:
            response, usage = self._inner.complete_optimizer(
                system, user, max_tokens=max_tokens
            )
            return response, usage
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            raise
        finally:
            log_llm_call(
                "optimizer", "complete_optimizer",
                system=system, user=user, response=response,
                usage=usage, duration_s=time.time() - t0,
                model=self.optimizer_model,
                max_tokens=max_tokens,
                stage=self._current_stage, error=error,
            )

    def complete_tool_call(
        self, system: str, user: str, tool: dict, *, max_tokens: int = 16384
    ) -> dict:
        t0 = time.time()
        error = ""
        result: dict = {}
        try:
            result = self._inner.complete_tool_call(
                system, user, tool, max_tokens=max_tokens
            )
            return result
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            raise
        finally:
            log_llm_call(
                "optimizer", "complete_tool_call",
                system=system, user=user, response=str(result)[:2000],
                duration_s=time.time() - t0,
                model=self.optimizer_model,
                max_tokens=max_tokens,
                stage=self._current_stage, error=error,
            )

    def complete_optimizer_messages(
        self, messages: list[dict], *, max_tokens: int = 16384
    ) -> tuple[str, dict]:
        t0 = time.time()
        error = ""
        response = ""
        usage: dict = {}
        try:
            response, usage = self._inner.complete_optimizer_messages(
                messages, max_tokens=max_tokens
            )
            return response, usage
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            raise
        finally:
            log_llm_call(
                "optimizer", "complete_optimizer_messages",
                messages=messages, response=response,
                usage=usage, duration_s=time.time() - t0,
                model=self.optimizer_model,
                max_tokens=max_tokens,
                stage=self._current_stage, error=error,
            )
