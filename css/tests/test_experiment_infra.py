"""Tests for experiment infrastructure: OpenAICompatLLMClient + multi-turn + build_clients."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from css.config import CSSConfig
from css.model.client import (
    LLMClient,
    OpenAICompatLLMClient,
    OptimizerOnlyClient,
    RouterLLMClient,
    StubLLMClient,
    TargetOnlyClient,
    build_clients,
)


# ── OpenAICompatLLMClient ─────────────────────────────────────────────────────


class TestOpenAICompatLLMClient:
    def _make_client(self, **overrides):
        defaults = dict(
            base_url="http://localhost:8888/v1",
            api_key="test-key",
            target_model="test-target",
            optimizer_model="test-optimizer",
        )
        defaults.update(overrides)
        return OpenAICompatLLMClient(**defaults)

    def test_chat_url_plain(self):
        c = self._make_client(base_url="http://host:8888/v1")
        assert c._chat_url() == "http://host:8888/v1/chat/completions"

    def test_chat_url_trailing_slash(self):
        c = self._make_client(base_url="http://host:8888/v1/")
        assert c._chat_url() == "http://host:8888/v1/chat/completions"

    def test_chat_url_already_full(self):
        c = self._make_client(base_url="http://host:8888/v1/chat/completions")
        assert c._chat_url() == "http://host:8888/v1/chat/completions"

    def test_protocol_compliance(self):
        c = self._make_client()
        assert isinstance(c, LLMClient)

    def test_complete_target_payload(self):
        """Verify the request payload includes chat_template_kwargs."""
        c = self._make_client(enable_thinking=False)
        captured = {}

        def mock_post(payload, timeout=None, **kwargs):
            captured.update(payload)
            return {
                "choices": [{"message": {"content": "hello"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }

        c._post = mock_post
        result = c.complete_target("sys", "usr", max_tokens=1024, temperature=0.0)

        assert result == "hello"
        assert captured["model"] == "test-target"
        assert captured["temperature"] == 0.0
        assert captured["chat_template_kwargs"] == {"enable_thinking": False}
        assert captured["max_tokens"] == 1024
        assert len(captured["messages"]) == 2
        assert captured["messages"][0]["role"] == "system"
        assert captured["messages"][1]["role"] == "user"

    def test_complete_target_messages_payload(self):
        c = self._make_client()
        captured = {}

        def mock_post(payload, timeout=None, **kwargs):
            captured.update(payload)
            return {
                "choices": [{"message": {"content": "multi-turn reply"}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
            }

        c._post = mock_post
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
        ]
        result = c.complete_target_messages(msgs, max_tokens=2048, temperature=0.0)

        assert result == "multi-turn reply"
        assert len(captured["messages"]) == 4
        assert captured["model"] == "test-target"

    def test_optimizer_and_target_use_their_own_temperatures(self):
        """Two sampling domains (user ruling 2026-07-08): optimizer calls are
        greedy, rollouts sample — and neither reads the other's value."""
        c = self._make_client(optimizer_temperature=0.0, target_temperature=0.6)
        captured = {}

        def mock_post(payload, timeout=None, **kwargs):
            captured.update(payload)
            return {
                "choices": [{"message": {"content": "opt reply"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
            }

        c._post = mock_post
        text, usage = c.complete_optimizer("sys", "usr")
        assert text == "opt reply"
        assert captured["temperature"] == 0.0
        assert captured["model"] == "test-optimizer"
        assert usage["total_tokens"] == 10

        captured.clear()
        c.complete_target_messages([{"role": "user", "content": "hi"}])
        assert captured["temperature"] == 0.6, "rollouts sample by default"
        assert captured["model"] == "test-target"

        captured.clear()
        c.complete_target_messages([{"role": "user", "content": "hi"}],
                                   temperature=0.0)
        assert captured["temperature"] == 0.0, "an explicit value still wins"

    def test_complete_optimizer_messages(self):
        c = self._make_client()
        captured = {}

        def mock_post(payload, timeout=None, **kwargs):
            captured.update(payload)
            return {
                "choices": [{"message": {"content": "opt-msg reply"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            }

        c._post = mock_post
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        text, usage = c.complete_optimizer_messages(msgs, max_tokens=512)

        assert text == "opt-msg reply"
        assert captured["model"] == "test-optimizer"

    def test_enable_thinking_true(self):
        c = self._make_client(enable_thinking=True)
        captured = {}

        def mock_post(payload, timeout=None, **kwargs):
            captured.update(payload)
            return {
                "choices": [{"message": {"content": "thinking reply"}}],
                "usage": {},
            }

        c._post = mock_post
        c.complete_target("s", "u")
        assert captured["chat_template_kwargs"] == {"enable_thinking": True}

    def test_max_tokens_capped(self):
        c = self._make_client(max_tokens=100)
        captured = {}

        def mock_post(payload, timeout=None, **kwargs):
            captured.update(payload)
            return {"choices": [{"message": {"content": "x"}}], "usage": {}}

        c._post = mock_post
        c.complete_target("s", "u", max_tokens=9999)
        assert captured["max_tokens"] == 100

    def test_null_content_returns_empty(self):
        c = self._make_client()

        def mock_post(payload, timeout=None, **kwargs):
            return {"choices": [{"message": {"content": None}}], "usage": {}}

        c._post = mock_post
        result = c.complete_target("s", "u")
        assert result == ""

    def test_no_choices_raises(self):
        c = self._make_client()

        def mock_post(payload, timeout=None, **kwargs):
            return {"choices": []}

        c._post = mock_post
        with pytest.raises(RuntimeError, match="no choices"):
            c.complete_target("s", "u")


# ── complete_target_messages on all client types ──────────────────────────────


class TestCompleteTargetMessages:
    def test_stub_client(self):
        c = StubLLMClient(target_fn=lambda s, u: f"echo:{u}")
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
            {"role": "user", "content": "follow-up"},
        ]
        result = c.complete_target_messages(msgs)
        assert "hello" in result or "follow-up" in result

    def test_target_only_delegates(self):
        inner = StubLLMClient(target_fn=lambda s, u: "delegated")
        view = TargetOnlyClient(inner)
        result = view.complete_target_messages(
            [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        )
        assert result == "delegated"

    def test_optimizer_only_raises(self):
        inner = StubLLMClient()
        view = OptimizerOnlyClient(inner)
        with pytest.raises(RuntimeError, match="optimizer-only"):
            view.complete_target_messages(
                [{"role": "user", "content": "x"}]
            )

    def test_protocol_includes_method(self):
        assert hasattr(LLMClient, "complete_target_messages")


# ── build_clients with openai_compat ──────────────────────────────────────────


class TestBuildClients:
    def test_openai_compat_backend(self):
        cfg = CSSConfig(
            target_model="qwen-test",
            optimizer_model="qwen-test",
            extra={
                "llm_backend": "openai_compat",
                "base_url": "http://10.0.0.1:8888/v1",
                "api_key": "key123",
                "max_tokens": 4096,
                "temperature": 0.5,
                "enable_thinking": False,
            },
        )
        target_view, opt_view = build_clients(cfg)
        assert isinstance(target_view, TargetOnlyClient)
        assert isinstance(opt_view, OptimizerOnlyClient)
        # build_clients bakes the tracing wrap in (audit-everywhere, 2026-07-10);
        # a sink-less TracingLLMClient is a transparent no-op.
        from css.tracing import TracingLLMClient
        wrap = target_view._inner
        assert isinstance(wrap, TracingLLMClient)
        inner = wrap._inner
        assert isinstance(inner, OpenAICompatLLMClient)
        assert inner.target_model == "qwen-test"
        assert inner.base_url == "http://10.0.0.1:8888/v1"
        assert inner.api_key == "key123"
        assert inner.enable_thinking is False

    def test_default_backend_is_router(self):
        cfg = CSSConfig(target_model="m", optimizer_model="m")
        # RouterLLMClient will fail on _resolve_backend without skillopt,
        # but construction should succeed.
        target_view, opt_view = build_clients(cfg)
        assert isinstance(target_view._inner._inner, RouterLLMClient)


# ── CSSConfig.max_turns ───────────────────────────────────────────────────────


class TestCSSConfigMaxTurns:
    def test_default_max_turns(self):
        cfg = CSSConfig()
        assert cfg.max_turns == 30

    def test_custom_max_turns(self):
        cfg = CSSConfig(max_turns=5)
        assert cfg.max_turns == 5

    def test_round_trip(self):
        cfg = CSSConfig(max_turns=10)
        d = cfg.to_dict()
        cfg2 = CSSConfig.from_dict(d)
        assert cfg2.max_turns == 10


# ── Standalone runner ─────────────────────────────────────────────────────────


def _run_all():
    import inspect

    passed = failed = 0
    test_classes = [
        TestOpenAICompatLLMClient,
        TestCompleteTargetMessages,
        TestBuildClients,
        TestCSSConfigMaxTurns,
    ]
    for cls in test_classes:
        obj = cls()
        for name, method in inspect.getmembers(obj, predicate=inspect.ismethod):
            if not name.startswith("test_"):
                continue
            try:
                method()
                passed += 1
            except Exception as exc:
                failed += 1
                print(f"  FAIL {cls.__name__}.{name}: {exc}")
    print(f"\n{passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if _run_all() else 1)
