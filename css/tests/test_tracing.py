"""Tests for css.tracing — structured event logging and LLM call tracing."""
from __future__ import annotations

import json
import os
import tempfile

import pytest

from css.model.client import StubLLMClient
from css.tracing import (
    TracingLLMClient,
    _JSONLWriter,
    init_trace,
    log_event,
    log_llm_call,
)


class TestJSONLWriter:
    def test_write_creates_file(self, tmp_path):
        path = str(tmp_path / "test.jsonl")
        w = _JSONLWriter(path)
        w.write({"a": 1})
        with open(path) as f:
            lines = f.readlines()
        assert len(lines) == 1
        assert json.loads(lines[0]) == {"a": 1}

    def test_append_mode(self, tmp_path):
        path = str(tmp_path / "test.jsonl")
        w = _JSONLWriter(path)
        w.write({"a": 1})
        w.write({"b": 2})
        with open(path) as f:
            lines = f.readlines()
        assert len(lines) == 2

    def test_unicode(self, tmp_path):
        path = str(tmp_path / "test.jsonl")
        w = _JSONLWriter(path)
        # Non-ASCII round-trip: accented Latin + an emoji exercise UTF-8 encoding
        # without embedding any CJK text in the codebase.
        w.write({"text": "café résumé ☕🌍"})
        with open(path, encoding="utf-8") as f:
            data = json.loads(f.readline())
        assert data["text"] == "café résumé ☕🌍"


class TestLogEvent:
    def test_noop_without_init(self):
        """log_event silently no-ops if init_trace hasn't been called."""
        import css.tracing as t
        old = t._writer
        t._writer = None
        try:
            log_event("test_event", round_index=1)
        finally:
            t._writer = old

    def test_writes_event(self, tmp_path):
        init_trace(str(tmp_path))
        log_event("test_event", round_index=5, node_id="n0001", foo="bar")
        with open(tmp_path / "trace.jsonl") as f:
            data = json.loads(f.readline())
        assert data["event"] == "test_event"
        assert data["round"] == 5
        assert data["node_id"] == "n0001"
        assert data["foo"] == "bar"
        assert "ts" in data

    def test_omits_negative_indices(self, tmp_path):
        init_trace(str(tmp_path))
        log_event("sparse", round_index=-1, node_id="")
        with open(tmp_path / "trace.jsonl") as f:
            data = json.loads(f.readline())
        assert "round" not in data
        assert "node_id" not in data


class TestTracingLLMClient:
    def test_complete_target_logged(self, tmp_path):
        init_trace(str(tmp_path))
        inner = StubLLMClient(target_fn=lambda s, u: f"reply-{u}")
        client = TracingLLMClient(inner, role="target")

        result = client.complete_target("sys", "hello", max_tokens=100)
        assert result == "reply-hello"

        # trace.jsonl has compact summary (no full text)
        with open(tmp_path / "trace.jsonl") as f:
            events = [json.loads(line) for line in f]
        llm_events = [e for e in events if e["event"] == "llm_call"]
        assert len(llm_events) >= 1
        ev = llm_events[-1]
        assert ev["role"] == "target"
        assert ev["method"] == "complete_target"
        assert ev["system_chars"] == 3
        assert ev["response_chars"] > 0
        assert ev["duration_s"] >= 0
        assert "call_id" in ev

        # llm_calls.jsonl has full content
        with open(tmp_path / "llm_calls.jsonl") as f:
            calls = [json.loads(line) for line in f]
        call = [c for c in calls if c["call_id"] == ev["call_id"]][0]
        assert call["system"] == "sys"
        assert call["user"] == "hello"
        assert call["response"] == "reply-hello"

    def test_complete_optimizer_logged(self, tmp_path):
        init_trace(str(tmp_path))
        inner = StubLLMClient(optimizer_fn=lambda s, u: f"opt-{u}")
        client = TracingLLMClient(inner, role="optimizer")

        text, usage = client.complete_optimizer("sys", "query")
        assert text == "opt-query"

        with open(tmp_path / "trace.jsonl") as f:
            events = [json.loads(line) for line in f]
        llm_events = [e for e in events if e["event"] == "llm_call"]
        ev = llm_events[-1]
        assert ev["role"] == "optimizer"
        assert ev["method"] == "complete_optimizer"

    def test_complete_target_messages_logged(self, tmp_path):
        init_trace(str(tmp_path))
        inner = StubLLMClient(target_fn=lambda s, u: "multi-reply")
        client = TracingLLMClient(inner, role="target")

        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
        result = client.complete_target_messages(msgs)
        assert result == "multi-reply"

        # trace.jsonl: compact summary with message count
        with open(tmp_path / "trace.jsonl") as f:
            events = [json.loads(line) for line in f]
        llm_events = [e for e in events if e["event"] == "llm_call"]
        ev = llm_events[-1]
        assert ev["method"] == "complete_target_messages"
        assert ev["n_messages"] == 2

        # llm_calls.jsonl: full messages
        with open(tmp_path / "llm_calls.jsonl") as f:
            calls = [json.loads(line) for line in f]
        call = [c for c in calls if c["call_id"] == ev["call_id"]][0]
        assert len(call["messages"]) == 2

    def test_error_logged(self, tmp_path):
        init_trace(str(tmp_path))

        def fail_fn(s, u):
            raise RuntimeError("boom")

        inner = StubLLMClient(target_fn=fail_fn)
        client = TracingLLMClient(inner, role="target")

        with pytest.raises(RuntimeError, match="boom"):
            client.complete_target("s", "u")

        with open(tmp_path / "trace.jsonl") as f:
            events = [json.loads(line) for line in f]
        llm_events = [e for e in events if e["event"] == "llm_call"]
        ev = llm_events[-1]
        assert "RuntimeError: boom" in ev["error"]

    def test_optimizer_messages_logged(self, tmp_path):
        init_trace(str(tmp_path))
        inner = StubLLMClient(optimizer_fn=lambda s, u: "opt-msgs")
        client = TracingLLMClient(inner, role="optimizer")

        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
        text, usage = client.complete_optimizer_messages(msgs)
        assert text == "opt-msgs"

        with open(tmp_path / "trace.jsonl") as f:
            events = [json.loads(line) for line in f]
        ev = [e for e in events if e["event"] == "llm_call"][-1]
        assert ev["method"] == "complete_optimizer_messages"


class TestTruncation:
    def test_long_text_truncated(self):
        from css.tracing import _truncate
        short = "hello"
        assert _truncate(short) == "hello"
        long = "x" * 100_000
        result = _truncate(long, limit=1000)
        assert len(result) < 1200
        assert "TRUNCATED" in result
