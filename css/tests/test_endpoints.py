"""Tests for css/model/endpoints.py (fleet registry resolution)."""
from __future__ import annotations

import css.model.endpoints as ep


def test_env_var_wins(monkeypatch, tmp_path):
    reg = tmp_path / "llm_endpoints.txt"
    reg.write_text("http://file:1/v1\n", encoding="utf-8")
    monkeypatch.setattr(ep, "registry_path", lambda: str(reg))
    monkeypatch.setenv("CSS_LLM_ENDPOINTS", " http://env:1/v1 , http://env:2/v1 ")
    assert ep.resolve_base_url("http://default:1/v1") == \
        "http://env:1/v1,http://env:2/v1"


def test_registry_file_wins_over_default(monkeypatch, tmp_path):
    reg = tmp_path / "llm_endpoints.txt"
    reg.write_text(
        "# fleet\n\nhttp://a:8888/v1\nhttp://b:8888/v1\n  \n# tail comment\n",
        encoding="utf-8")
    monkeypatch.setattr(ep, "registry_path", lambda: str(reg))
    monkeypatch.delenv("CSS_LLM_ENDPOINTS", raising=False)
    assert ep.resolve_base_url("http://default:1/v1") == \
        "http://a:8888/v1,http://b:8888/v1"


def test_default_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(ep, "registry_path",
                        lambda: str(tmp_path / "missing.txt"))
    monkeypatch.delenv("CSS_LLM_ENDPOINTS", raising=False)
    assert ep.resolve_base_url("http://default:1/v1") == "http://default:1/v1"


def test_empty_registry_falls_through(monkeypatch, tmp_path):
    reg = tmp_path / "llm_endpoints.txt"
    reg.write_text("# only comments\n\n", encoding="utf-8")
    monkeypatch.setattr(ep, "registry_path", lambda: str(reg))
    monkeypatch.delenv("CSS_LLM_ENDPOINTS", raising=False)
    assert ep.resolve_base_url("http://default:1/v1") == "http://default:1/v1"


def test_repo_registry_matches_current_fleet():
    """The committed registry must resolve to the live dual-replica fleet."""
    urls = ep.resolve_base_url("").split(",")
    assert "http://10.77.110.162:8888/v1" in urls
    assert "http://10.77.110.162:8889/v1" in urls
