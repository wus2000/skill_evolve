"""SpreadsheetBench task dataloader (self-contained CSS copy).

Loads a standardised split layout::

    split_dir/
    ├── train/      # training items   (one .json array per split dir)
    ├── val/        # validation / selection items (gate)
    └── test/       # held-out test items

Two entry modes:

- ``split_mode="split_dir"``: consume an existing split directory tree.
- ``split_mode="ratio"``: build a deterministic split directory from a raw
  dataset path using an explicit train:val:test ratio.

Only the load path CSS needs is kept here — the batch/epoch-planning helpers of
the original framework are intentionally omitted (CSS does its own batching in
``css.rollout.batch``). No external framework imports.
"""
from __future__ import annotations

import glob
import json
import os
import random


# Canonical split names expected under split_dir/
SPLIT_NAMES = ("train", "val", "test")

# Maps legacy / trainer split names → canonical directory names
_SPLIT_ALIAS: dict[str, str] = {
    "train": "train",
    "valid_seen": "val",
    "selection": "val",
    "val": "val",
    "valid_unseen": "test",
    "test": "test",
}


def _load_json_or_jsonl(path: str) -> list[dict]:
    """Load a list of items from a JSON or JSONL file."""
    with open(path, encoding="utf-8") as f:
        content = f.read().strip()
    if not content:
        return []

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        data = None

    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        nested = data.get("data")
        if isinstance(nested, list):
            return nested
        return list(data.values())

    items: list[dict] = []
    for line in content.splitlines():
        line = line.strip()
        if line:
            items.append(json.loads(line))
    return items


def _parse_split_ratio(text: str) -> tuple[int, int, int]:
    parts = [part.strip() for part in str(text or "").split(":") if part.strip()]
    if len(parts) != 3:
        raise ValueError(
            f"split_ratio must be in train:val:test form, got {text!r}"
        )
    try:
        train, val, test = (int(part) for part in parts)
    except ValueError as exc:
        raise ValueError(
            f"split_ratio must contain integers, got {text!r}"
        ) from exc
    if min(train, val, test) <= 0:
        raise ValueError(f"split_ratio parts must be positive, got {text!r}")
    return train, val, test


def _compute_split_counts(total: int, ratio: tuple[int, int, int]) -> tuple[int, int, int]:
    weights = list(ratio)
    denom = sum(weights)
    raw = [total * weight / denom for weight in weights]
    counts = [int(value) for value in raw]
    remaining = total - sum(counts)
    order = sorted(
        range(len(raw)),
        key=lambda idx: (raw[idx] - counts[idx], weights[idx]),
        reverse=True,
    )
    for idx in order[:remaining]:
        counts[idx] += 1
    return counts[0], counts[1], counts[2]


class SpreadsheetBenchDataLoader:
    """SpreadsheetBench dataloader.

    Each split directory contains a .json file (JSON array of task items).
    Spreadsheet files referenced by items live under a separate ``data_root``.
    """

    def __init__(
        self,
        split_dir: str = "",
        data_path: str = "",
        split_mode: str = "ratio",
        split_ratio: str = "2:1:7",
        split_seed: int = 42,
        split_output_dir: str = "",
        data_root: str = "",
        seed: int = 42,
        limit: int = 0,
        **kwargs,
    ) -> None:
        self.split_dir = split_dir
        self.data_path = data_path
        self.split_mode = split_mode
        self.split_ratio = split_ratio
        self.split_seed = int(split_seed)
        self.split_output_dir = split_output_dir
        self.seed = seed
        self.limit = limit
        self.data_root = data_root
        self._splits: dict[str, list[dict]] = {}

    # ── Setup ────────────────────────────────────────────────────────────

    def setup(self, cfg: dict) -> None:
        if not self.split_mode:
            self.split_mode = str(cfg.get("split_mode", "ratio") or "ratio")
        if not self.split_dir:
            self.split_dir = cfg.get("split_dir", "")
        if not self.data_path:
            self.data_path = cfg.get("data_path", "")
        if not self.split_output_dir:
            self.split_output_dir = cfg.get("split_output_dir", "")
        if "split_seed" in cfg and not self.split_seed:
            self.split_seed = int(cfg.get("split_seed", 0) or 0)
        if not self.split_seed:
            self.split_seed = self.seed
        if not self.split_ratio:
            self.split_ratio = str(cfg.get("split_ratio", "2:1:7") or "2:1:7")

        mode = str(self.split_mode or "ratio").strip().lower()
        if mode not in {"ratio", "split_dir"}:
            raise ValueError(
                f"{type(self).__name__} split_mode must be 'ratio' or 'split_dir', "
                f"got {self.split_mode!r}"
            )
        self.split_mode = mode

        if self.split_mode == "ratio":
            self.split_dir = self._materialize_ratio_split(cfg)
        if not self.split_dir:
            raise ValueError(
                f"{type(self).__name__} requires either "
                "`split_mode=ratio` with `data_path`, or `split_mode=split_dir` "
                f"with `split_dir` pointing to {'/'.join(SPLIT_NAMES)}/."
            )
        self._load_all_splits()

    def _resolve_split_output_dir(self, cfg: dict) -> str:
        if self.split_output_dir:
            return os.path.abspath(self.split_output_dir)
        out_root = os.path.abspath(str(cfg.get("out_root") or os.getcwd()))
        env_name = str(cfg.get("env") or type(self).__name__.replace("DataLoader", "").lower())
        ratio_tag = str(self.split_ratio or "2:1:7").replace(":", "-")
        return os.path.join(out_root, "_generated_splits", f"{env_name}_{ratio_tag}_seed{self.split_seed}")

    def load_raw_items(self, data_path: str) -> list[dict]:
        """Load raw items from a dataset path before ratio splitting."""
        if os.path.isdir(data_path):
            if any(os.path.isdir(os.path.join(data_path, name)) for name in SPLIT_NAMES):
                raise ValueError(
                    f"{type(self).__name__} got a split directory as data_path. "
                    "Use split_mode=split_dir and pass it as split_dir instead."
                )
            candidates = sorted(glob.glob(os.path.join(data_path, "*.json")))
            candidates += sorted(glob.glob(os.path.join(data_path, "*.jsonl")))
            if len(candidates) != 1:
                raise ValueError(
                    f"{type(self).__name__} expected data_path to be one JSON/JSONL file "
                    f"or a directory containing exactly one such file, got: {data_path}"
                )
            return _load_json_or_jsonl(candidates[0])
        return _load_json_or_jsonl(data_path)

    def write_split_items(self, split_path: str, items: list[dict]) -> None:
        os.makedirs(split_path, exist_ok=True)
        out_path = os.path.join(split_path, "items.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)

    def _materialize_ratio_split(self, cfg: dict) -> str:
        data_path = os.path.abspath(str(self.data_path or "").strip())
        if not data_path:
            raise ValueError(
                f"{type(self).__name__} requires data_path when split_mode=ratio."
            )

        ratio = _parse_split_ratio(self.split_ratio)
        items = self.load_raw_items(data_path)
        if not isinstance(items, list) or not items:
            raise ValueError(f"No raw items available for ratio split from {data_path}")

        shuffled = list(items)
        rng = random.Random(self.split_seed)
        rng.shuffle(shuffled)

        train_n, val_n, test_n = _compute_split_counts(len(shuffled), ratio)
        train_items = shuffled[:train_n]
        val_items = shuffled[train_n: train_n + val_n]
        test_items = shuffled[train_n + val_n: train_n + val_n + test_n]

        split_dir = self._resolve_split_output_dir(cfg)
        manifest = {
            "source_data_path": data_path,
            "split_mode": "ratio",
            "split_ratio": self.split_ratio,
            "split_seed": self.split_seed,
            "counts": {
                "train": len(train_items),
                "val": len(val_items),
                "test": len(test_items),
            },
        }
        os.makedirs(split_dir, exist_ok=True)
        self.write_split_items(os.path.join(split_dir, "train"), train_items)
        self.write_split_items(os.path.join(split_dir, "val"), val_items)
        self.write_split_items(os.path.join(split_dir, "test"), test_items)
        with open(os.path.join(split_dir, "split_manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        print(
            f"  [{type(self).__name__}] generated ratio split {self.split_ratio} "
            f"at {split_dir} from {data_path}"
        )
        return split_dir

    def _load_all_splits(self) -> None:
        for name in SPLIT_NAMES:
            split_path = os.path.join(self.split_dir, name)
            if not os.path.isdir(split_path):
                raise ValueError(
                    f"Missing '{name}/' subdirectory in split_dir: {self.split_dir}"
                )
            items = self.load_split_items(split_path)
            if self.limit:
                items = items[: self.limit]
            self._splits[name] = items

        counts = " ".join(f"{k}={len(v)}" for k, v in self._splits.items())
        print(f"  [{type(self).__name__}] {counts}  (from {self.split_dir})")

    def load_split_items(self, split_path: str) -> list[dict]:
        """Load items from one split directory (e.g. ``split_dir/train/``).

        Finds the first ``.json`` file in the directory and loads it as a JSON
        array.
        """
        json_files = sorted(glob.glob(os.path.join(split_path, "*.json")))
        if not json_files:
            raise FileNotFoundError(
                f"No .json file found in {split_path}"
            )
        with open(json_files[0], encoding="utf-8") as f:
            items = json.load(f)
        if not isinstance(items, list):
            raise ValueError(
                f"Expected JSON array in {json_files[0]}, got {type(items).__name__}"
            )
        return items

    # ── Accessors ────────────────────────────────────────────────────────

    @property
    def train_items(self) -> list[dict]:
        return self._splits.get("train", [])

    @property
    def val_items(self) -> list[dict]:
        return self._splits.get("val", [])

    @property
    def test_items(self) -> list[dict]:
        return self._splits.get("test", [])

    def get_split_items(self, split: str) -> list[dict]:
        """Resolve a split name (including legacy aliases) to its item list."""
        canonical = _SPLIT_ALIAS.get(split, split)
        return list(self._splits.get(canonical, self.val_items))

    def get_train_size(self) -> int:
        return len(self.train_items)
