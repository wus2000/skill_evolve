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
    urls = [n["url"] for n in ep.parse_fleet(ep.resolve_base_url(""))]
    assert "http://10.77.110.162:8888/v1" in urls
    assert "http://10.77.110.162:8889/v1" in urls


def test_annotated_registry_lines_encode_and_parse(monkeypatch, tmp_path):
    reg = tmp_path / "llm_endpoints.txt"
    reg.write_text(
        "http://a100:8888/v1  w=1.0  max_inflight=256\n"
        "http://h20:8890/v1   w=0.5  max_inflight=320\n"
        "http://plain:8891/v1\n",
        encoding="utf-8")
    monkeypatch.setattr(ep, "registry_path", lambda: str(reg))
    monkeypatch.delenv("CSS_LLM_ENDPOINTS", raising=False)
    base = ep.resolve_base_url("")
    assert base == ("http://a100:8888/v1|w=1.0|max_inflight=256,"
                    "http://h20:8890/v1|w=0.5|max_inflight=320,"
                    "http://plain:8891/v1")
    fleet = ep.parse_fleet(base)
    assert (fleet[0]["url"], fleet[0]["w"], fleet[0]["max_inflight"]) == \
        ("http://a100:8888/v1", 1.0, 256)
    assert (fleet[1]["url"], fleet[1]["w"], fleet[1]["max_inflight"]) == \
        ("http://h20:8890/v1", 0.5, 320)
    assert (fleet[2]["w"], fleet[2]["max_inflight"]) == (1.0, 0)


def test_parse_fleet_ignores_malformed_annotations():
    fleet = ep.parse_fleet("http://x/v1|w=abc|bogus|max_inflight=-5,http://y/v1|w=2")
    assert (fleet[0]["url"], fleet[0]["w"], fleet[0]["max_inflight"]) == \
        ("http://x/v1", 1.0, 0)
    assert fleet[1]["w"] == 2.0
