"""Atomic filesystem helpers for persisted generation artifacts.

Every generation-pipeline step persists its product under ``nodes/<child>/gen/``
BEFORE proceeding and skips the step when the product already exists (design
§4: step-granular resume). All writes go through a tmp-file + ``os.replace`` so a
crash mid-write never leaves a half-written product that a resume would mistake
for a completed step.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional


def ensure_dir(path: str) -> None:
    if path:
        os.makedirs(path, exist_ok=True)


def read_text(path: str) -> "Optional[str]":
    """Return the file's text, or ``None`` if it does not exist / cannot be read."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, IOError):
        return None


def read_json(path: str) -> Any:
    """Return the parsed JSON value, or ``None`` on missing/invalid file."""
    txt = read_text(path)
    if txt is None:
        return None
    try:
        return json.loads(txt)
    except (ValueError, TypeError):
        return None


def write_text_atomic(path: str, text: str) -> None:
    ensure_dir(os.path.dirname(path))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def write_json_atomic(path: str, obj: Any) -> None:
    ensure_dir(os.path.dirname(path))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, path)


def exists(path: str) -> bool:
    return bool(path) and os.path.exists(path)
