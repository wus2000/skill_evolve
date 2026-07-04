"""LLM endpoint fleet resolution — one registry, every launcher.

PURE INFRASTRUCTURE: this module (like css/model/routing.py) exists only to
speed experiments up. It is NOT part of the CSS mechanism and plays no role
in the paper's method; the mechanism is transport-agnostic.

Adding/removing a vLLM replica used to mean editing base_url strings in every
launcher. Launchers now call :func:`resolve_base_url`, which resolves the
fleet from (in priority order):

  1. ``CSS_LLM_ENDPOINTS`` env var — comma-separated, ad-hoc override
     (e.g. pointing one experiment at a private replica);
  2. ``config/llm_endpoints.txt`` at the repo root — the fleet registry,
     one URL per line, ``#`` comments ignored;
  3. the launcher's built-in default — keeps launchers self-contained when
     the registry is absent (fresh checkouts, unit tests).

Heterogeneous-fleet annotations (all optional): a registry line may carry
space-separated ``key=value`` pairs after the URL —

    http://a100:8888/v1  w=1.0  max_inflight=256
    http://h20:8890/v1   w=0.5  max_inflight=320

``w``            rate PRIOR (relative service speed; the router's adaptive
                 throughput estimate takes over once warm — see routing.py)
``max_inflight`` hard per-node concurrency cap (the client-side shadow of the
                 node's max_num_seqs; tail-latency fuse for weak nodes)

The resolved base_url string encodes annotations with ``|`` so the whole
fleet travels through the existing single ``extra["base_url"]`` channel:
``http://a100:8888/v1|w=1.0|max_inflight=256,http://h20:8890/v1|w=0.5``.
:func:`parse_fleet` decodes that string back into per-node dicts; plain URLs
without annotations behave exactly as before.

The resolved string is written into the run's ``config.json`` as usual, so a
run's provenance still records exactly which endpoints it used. The resume
fingerprint (css/checkpoint.py) deliberately excludes endpoint config —
growing the fleet never invalidates a checkpoint.
"""
from __future__ import annotations

import os

_ENV_VAR = "CSS_LLM_ENDPOINTS"
_REGISTRY_RELPATH = os.path.join("config", "llm_endpoints.txt")


def parse_fleet(base_url: str) -> "list[dict]":
    """Decode a (possibly annotated) base_url string into per-node dicts.

    Returns ``[{"url": str, "w": float, "max_inflight": int}, ...]`` with
    defaults ``w=1.0`` and ``max_inflight=0`` (0 = uncapped). Unknown or
    malformed annotations are ignored (never fatal).
    """
    fleet = []
    for entry in (base_url or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split("|")
        node = {"url": parts[0].strip(), "w": 1.0, "max_inflight": 0}
        for annot in parts[1:]:
            if "=" not in annot:
                continue
            key, _, val = annot.partition("=")
            key, val = key.strip(), val.strip()
            try:
                if key == "w":
                    node["w"] = max(0.01, float(val))
                elif key == "max_inflight":
                    node["max_inflight"] = max(0, int(val))
            except ValueError:
                continue
        if node["url"]:
            fleet.append(node)
    return fleet


def _repo_root() -> str:
    # css/model/endpoints.py -> css/model -> css -> repo root
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def registry_path() -> str:
    """Absolute path of the fleet registry file (may not exist)."""
    return os.path.join(_repo_root(), _REGISTRY_RELPATH)


def _encode_line(line: str) -> str:
    """Registry line -> base_url entry: whitespace-separated annotations -> ``|``."""
    return "|".join(part for part in line.split() if part)


def resolve_base_url(default: str = "") -> str:
    """Resolve the endpoint fleet to a comma-separated base_url string.

    Registry-line annotations (``w=... max_inflight=...``) are encoded with
    ``|`` so they survive the single-string config channel; see
    :func:`parse_fleet` for decoding.
    """
    env = (os.environ.get(_ENV_VAR) or "").strip()
    if env:
        urls = [_encode_line(u.strip()) for u in env.split(",") if u.strip()]
        if urls:
            return ",".join(urls)
    path = registry_path()
    if os.path.exists(path):
        urls = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        urls.append(_encode_line(line))
        except OSError:
            urls = []
        if urls:
            return ",".join(urls)
    return default
