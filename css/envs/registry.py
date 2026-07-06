"""Environment registry: build the :class:`~css.envs.base.TaskEnv` selected by
``cfg.env_name``.

This keeps the mechanism and the runner scripts free of hard-coded concrete-env
imports — they call :func:`build_env` and get back whatever environment the
config selects. Each concrete env is imported lazily inside its own branch so
optional / heavy dependencies (openpyxl for SpreadsheetBench, sqlite for Bird)
load only for the env actually in use.

To add a new environment: implement ``TaskEnv`` in ``css/envs/<name>/`` and add
one branch here. Nothing in the mechanism layer changes.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from css.config import CSSConfig
    from css.envs.base import TaskEnv

# Accepted aliases -> canonical env name.
_ALIASES = {
    "spreadsheetbench": "spreadsheetbench",
    "spreadsheet": "spreadsheetbench",
    "ssb": "spreadsheetbench",
    "bird": "bird",
    "alfworld": "alfworld",
    "alfred": "alfworld",
    "appworld": "appworld",
    "scienceworld": "scienceworld",
    "sciworld": "scienceworld",
    "bfcl": "bfcl",
    "berkeley_function_calling": "bfcl",
    "webarena": "webarena",
    "wa": "webarena",
    # Scaffold env (css/envs/template) — toy QA task; used for mechanism smoke
    # tests and as the copy-me starting point for new benchmarks. See
    # docs/env_integration_guide.md.
    "template": "template",
}


def canonical_env_name(name: str) -> str:
    """Resolve an ``env_name`` (case-insensitive, alias-aware) to its canonical form."""
    key = (name or "").strip().lower()
    if key not in _ALIASES:
        raise ValueError(
            f"Unknown env_name {name!r}; known: "
            + ", ".join(sorted(set(_ALIASES.values())))
        )
    return _ALIASES[key]


def build_env(cfg: "CSSConfig", **kwargs) -> "TaskEnv":
    """Construct the ``TaskEnv`` named by ``cfg.env_name``.

    Extra keyword arguments (e.g. ``items``, ``split_dir``, ``data_root``) are
    forwarded to the concrete env constructor. Raises ``ValueError`` for an
    unknown ``env_name``.
    """
    name = canonical_env_name(cfg.env_name)
    if name == "spreadsheetbench":
        from css.envs.spreadsheetbench.task_interface import SpreadsheetBenchEnv
        return SpreadsheetBenchEnv(cfg, **kwargs)
    if name == "bird":
        from css.envs.bird.task_interface import BirdEnv
        return BirdEnv(cfg, **kwargs)
    if name == "alfworld":
        from css.envs.alfworld.task_interface import AlfworldEnv
        return AlfworldEnv(cfg, **kwargs)
    if name == "appworld":
        from css.envs.appworld.task_interface import AppworldEnv
        return AppworldEnv(cfg, **kwargs)
    if name == "scienceworld":
        from css.envs.scienceworld.task_interface import ScienceworldEnv
        return ScienceworldEnv(cfg, **kwargs)
    if name == "bfcl":
        from css.envs.bfcl.task_interface import BfclEnv
        return BfclEnv(cfg, **kwargs)
    if name == "webarena":
        from css.envs.webarena.env import WebArenaEnv
        return WebArenaEnv(cfg, **kwargs)
    if name == "template":
        from css.envs.template.task_interface import TemplateEnv
        return TemplateEnv(cfg, **kwargs)
    # Unreachable: canonical_env_name already validated the name.
    raise ValueError(f"Unhandled env_name {cfg.env_name!r}")
