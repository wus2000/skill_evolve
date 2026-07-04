"""Tests for llmfleet.registry (fleet resolution + annotations)."""
from __future__ import annotations

import llmfleet.registry as reg


def test_env_var_priority(monkeypatch, tmp_path):
    f = tmp_path / "endpoints.txt"
    f.write_text("http://file:1/v1\n", encoding="utf-8")
    monkeypatch.setenv("LLMFLEET_ENDPOINTS", "http://env:1/v1 w=2, http://env:2/v1")
    out = reg.resolve_endpoints("http://d/v1", registry_path=str(f))
    assert out == "http://env:1/v1|w=2,http://env:2/v1"


def test_registry_then_default(monkeypatch, tmp_path):
    monkeypatch.delenv("LLMFLEET_ENDPOINTS", raising=False)
    f = tmp_path / "endpoints.txt"
    f.write_text("# fleet\nhttp://a:1/v1 w=1.5 max_inflight=64 key=tok metrics=none\n",
                 encoding="utf-8")
    out = reg.resolve_endpoints("http://d/v1", registry_path=str(f))
    assert out == "http://a:1/v1|w=1.5|max_inflight=64|key=tok|metrics=none"
    assert reg.resolve_endpoints("http://d/v1",
                                 registry_path=str(tmp_path / "nope")) == "http://d/v1"


def test_parse_fleet_full_annotations():
    fleet = reg.parse_fleet(
        "http://a:1/v1|w=1.5|max_inflight=64|key=tok|metrics=none,"
        "http://b:2/v1|metrics=bogus,"
        "http://c:3/v1")
    assert fleet[0] == {"url": "http://a:1/v1", "w": 1.5, "max_inflight": 64,
                        "key": "tok", "metrics": "none"}
    assert fleet[1]["metrics"] == "vllm"  # unknown value -> default
    assert fleet[2] == {"url": "http://c:3/v1", "w": 1.0, "max_inflight": 0,
                        "key": "", "metrics": "vllm"}


def test_chat_urls():
    fleet = reg.parse_fleet("http://a:1/v1,http://b:2/v1/chat/completions")
    assert reg.chat_urls(fleet) == [
        "http://a:1/v1/chat/completions",
        "http://b:2/v1/chat/completions",
    ]
