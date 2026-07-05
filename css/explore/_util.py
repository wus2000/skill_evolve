"""Small shared helpers for the exploration subsystem (atomic writes, ids).

Kept dependency-free (stdlib only) so every explore module can import it without
risking an import cycle back into the mechanism.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any


def item_id(item: dict) -> str:
    """Best-effort stable task id for an env item dict.

    Mirrors :func:`css.rollout.batch._item_id` so a task_id resolves to the same
    string the rollout layer and the analysis pipeline use.
    """
    return str(item.get("task_id", item.get("id", "")))


def item_descriptor(item: dict, *, max_chars: int = 200) -> str:
    """One-line human descriptor for a menu entry (never multi-line)."""
    for key in ("task_description", "description", "instruction", "query", "question"):
        val = item.get(key)
        if isinstance(val, str) and val.strip():
            text = " ".join(val.split())
            return text[:max_chars] + ("…" if len(text) > max_chars else "")
    ttype = str(item.get("task_type", "")).strip()
    return ttype or "(no description)"


def safe_name(name: str, *, max_len: int = 80) -> str:
    """Filesystem-safe slug for a group_key used as a directory name.

    Non-word characters collapse to ``_``; the original key is preserved in the
    cache meta, so this only needs to be stable and collision-resistant, not
    reversible.
    """
    slug = re.sub(r"[^\w.-]+", "_", (name or "").strip()).strip("._-")
    slug = slug or "group"
    if len(slug) > max_len:
        # Keep a readable head plus a short hash tail so distinct long keys
        # do not collapse onto one directory.
        import hashlib

        tail = hashlib.sha1((name or "").encode("utf-8")).hexdigest()[:8]
        slug = slug[: max_len - 9] + "_" + tail
    return slug


def atomic_write_text(path: str, text: str) -> None:
    """Write ``text`` to ``path`` atomically (tmp + os.replace)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def atomic_write_json(path: str, obj: Any) -> None:
    """Write ``obj`` as pretty JSON to ``path`` atomically."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, path)


def append_jsonl(path: str, obj: dict) -> None:
    """Append one JSON record as a line to ``path`` (creating parents)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")


def read_text(path: str) -> str:
    """Read a text file, returning ``""`` on any error."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:  # noqa: BLE001 — cache reads never fatal
        return ""


def read_json(path: str) -> Any:
    """Read a JSON file, returning ``None`` on any error."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None
