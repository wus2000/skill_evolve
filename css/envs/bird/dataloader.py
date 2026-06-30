"""Bird split loader: read ``items.json`` per split and resolve each item's DB.

Items are plain dicts forwarded verbatim to ``BirdEnv.run_one``. This loader
stamps two keys the env relies on:
  * ``id``       - a stable, unique task id (from ``question_id`` / ``id`` / index)
  * ``db_path``  - the resolved path to the item's SQLite file

The standard BIRD on-disk layout is ``<db_root>/<db_id>/<db_id>.sqlite``.
"""
from __future__ import annotations

import json
import os


def _resolve_db_path(db_root: str, db_id: str) -> str:
    """Resolve the SQLite file for ``db_id`` under ``db_root`` (BIRD layout)."""
    if not db_root or not db_id:
        return ""
    candidates = [
        os.path.join(db_root, db_id, f"{db_id}.sqlite"),
        os.path.join(db_root, db_id, f"{db_id}.db"),
        os.path.join(db_root, f"{db_id}.sqlite"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    # Default to the canonical location even if missing — run_one reports the
    # absent-DB failure with the path, which is the actionable diagnostic.
    return candidates[0]


class BirdDataLoader:
    """Loads Bird task items for a split and attaches ``id`` / ``db_path``."""

    def __init__(self, split_dir: str, db_root: str) -> None:
        self.split_dir = split_dir
        self.db_root = db_root
        self._cache: dict[str, list[dict]] = {}

    def load(self, split: str) -> list[dict]:
        if split in self._cache:
            return self._cache[split]
        items_path = os.path.join(self.split_dir, split, "items.json")
        if not os.path.exists(items_path):
            raise FileNotFoundError(f"Bird split not found: {items_path}")
        with open(items_path, encoding="utf-8") as f:
            raw_items = json.load(f)

        items: list[dict] = []
        for i, raw in enumerate(raw_items):
            item = dict(raw)
            item["id"] = str(item.get("question_id", item.get("id", i)))
            item["db_path"] = _resolve_db_path(self.db_root, item.get("db_id", ""))
            items.append(item)
        self._cache[split] = items
        return items
