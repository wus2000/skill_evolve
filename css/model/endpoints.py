"""LLM endpoint fleet resolution — one registry, every launcher.

Adding/removing a vLLM replica used to mean editing base_url strings in every
launcher. Launchers now call :func:`resolve_base_url`, which resolves the
fleet from (in priority order):

  1. ``CSS_LLM_ENDPOINTS`` env var — comma-separated, ad-hoc override
     (e.g. pointing one experiment at a private replica);
  2. ``config/llm_endpoints.txt`` at the repo root — the fleet registry,
     one URL per line, ``#`` comments ignored;
  3. the launcher's built-in default — keeps launchers self-contained when
     the registry is absent (fresh checkouts, unit tests).

The resolved string is written into the run's ``config.json`` as usual, so a
run's provenance still records exactly which endpoints it used. The resume
fingerprint (css/checkpoint.py) deliberately excludes endpoint config —
growing the fleet never invalidates a checkpoint.
"""
from __future__ import annotations

import os

_ENV_VAR = "CSS_LLM_ENDPOINTS"
_REGISTRY_RELPATH = os.path.join("config", "llm_endpoints.txt")


def _repo_root() -> str:
    # css/model/endpoints.py -> css/model -> css -> repo root
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def registry_path() -> str:
    """Absolute path of the fleet registry file (may not exist)."""
    return os.path.join(_repo_root(), _REGISTRY_RELPATH)


def resolve_base_url(default: str = "") -> str:
    """Resolve the endpoint fleet to a comma-separated base_url string."""
    env = (os.environ.get(_ENV_VAR) or "").strip()
    if env:
        urls = [u.strip() for u in env.split(",") if u.strip()]
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
                        urls.append(line)
        except OSError:
            urls = []
        if urls:
            return ",".join(urls)
    return default
