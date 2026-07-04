"""LLM endpoint fleet resolution for CSS launchers (shim over ``llmfleet``).

PURE INFRASTRUCTURE — not part of the CSS mechanism. The generic registry
machinery lives in the standalone ``llmfleet`` package; this shim binds it to
this repo's conventions:

  * registry file: ``config/llm_endpoints.txt`` at the repo root;
  * env override: ``CSS_LLM_ENDPOINTS`` (kept for back-compat) or the generic
    ``LLMFLEET_ENDPOINTS``.

The resolved string is written into each run's ``config.json`` (provenance);
the resume fingerprint excludes endpoint config, so fleet changes never
invalidate checkpoints. See config/llm_endpoints.txt for the annotation
syntax (w= / max_inflight= / key= / metrics=).
"""
from __future__ import annotations

import os

from llmfleet.registry import parse_fleet  # noqa: F401  (re-export)
from llmfleet.registry import resolve_endpoints

_REGISTRY_RELPATH = os.path.join("config", "llm_endpoints.txt")


def _repo_root() -> str:
    # css/model/endpoints.py -> css/model -> css -> repo root
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def registry_path() -> str:
    """Absolute path of the fleet registry file (may not exist)."""
    return os.path.join(_repo_root(), _REGISTRY_RELPATH)


def resolve_base_url(default: str = "") -> str:
    """Resolve the endpoint fleet to a comma-separated base_url string."""
    return resolve_endpoints(
        default,
        registry_path=registry_path(),
        env_vars=("CSS_LLM_ENDPOINTS", "LLMFLEET_ENDPOINTS"),
    )
