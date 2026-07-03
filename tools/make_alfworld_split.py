#!/usr/bin/env python3
"""Build the versioned ALFWorld split files consumed by ``css/envs/alfworld``.

Split design (decided 2026-07-03, see docs/env_prep/alfworld_PREP.md):

  * ``train``       — official ``train/`` games MINUS the carved val set.
  * ``val``         — ``--val-size`` games carved out of official ``train/``,
                      stratified by task_type (proportional allocation), used
                      exclusively as the L0 gate / selection set.
  * ``test_seen``   — official ``valid_seen/`` (140): same rooms as train,
                      new task instances → in-domain generalization.
  * ``test_unseen`` — official ``valid_unseen/`` (134): rooms never seen in
                      train → out-of-domain generalization; the community-
                      standard eval split. The mechanism's ``test`` split.

The official ``valid_train/`` (200) is deliberately unused (it is the authors'
debug set; other papers may have tuned on it).

Item ids are content-stable: assigned from the deterministic sorted order of
gamefile paths BEFORE shuffling, so an id always denotes the same game across
regenerations. Each split list is then shuffled (seeded) because the env's
``slice_split`` convention treats a prefix as a random sample.

Usage:
  python tools/make_alfworld_split.py \
      [--data-root ~/.cache/alfworld] [--out data/alfworld_split_seed42] \
      [--val-size 400] [--seed 42]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import random
from collections import Counter, defaultdict

_SOURCE_DIRS = {
    "train": "json_2.1.1/train",
    "test_seen": "json_2.1.1/valid_seen",
    "test_unseen": "json_2.1.1/valid_unseen",
}

_ID_PREFIX = {
    "train": "atrain",
    "val": "aval",
    "test_seen": "aseen",
    "test_unseen": "aunseen",
}


def _enumerate_games(data_root: str, rel_dir: str) -> list[dict]:
    """All games under one source dir, deterministically sorted by path."""
    pattern = os.path.join(data_root, rel_dir, "**", "game.tw-pddl")
    games = sorted(glob.glob(pattern, recursive=True))
    items = []
    for path in games:
        rel = os.path.relpath(path, data_root)
        task_dir = os.path.basename(os.path.dirname(os.path.dirname(path)))
        trial = os.path.basename(os.path.dirname(path))
        parts = task_dir.split("-")
        items.append({
            "gamefile": rel,
            "task_type": parts[0],
            "scene": parts[-1],
            "task_dir": task_dir,
            "trial": trial,
        })
    return items


def _stratified_val(train: list[dict], val_size: int, rng: random.Random) -> set[int]:
    """Indices of the val carve-out, stratified by task_type (proportional)."""
    by_type: dict[str, list[int]] = defaultdict(list)
    for i, item in enumerate(train):
        by_type[item["task_type"]].append(i)
    total = len(train)
    picked: set[int] = set()
    # Largest-remainder proportional allocation so per-type counts sum to val_size.
    quotas = {t: val_size * len(ix) / total for t, ix in by_type.items()}
    alloc = {t: int(q) for t, q in quotas.items()}
    remainder = val_size - sum(alloc.values())
    for t in sorted(quotas, key=lambda t: quotas[t] - alloc[t], reverse=True)[:remainder]:
        alloc[t] += 1
    for t, ix in sorted(by_type.items()):
        picked.update(rng.sample(ix, alloc[t]))
    return picked


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-root", default=os.path.expanduser("~/.cache/alfworld"))
    ap.add_argument("--out", default="data/alfworld_split_seed42")
    ap.add_argument("--val-size", type=int, default=400)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    rng = random.Random(args.seed)

    train_all = _enumerate_games(args.data_root, _SOURCE_DIRS["train"])
    splits: dict[str, list[dict]] = {
        "test_seen": _enumerate_games(args.data_root, _SOURCE_DIRS["test_seen"]),
        "test_unseen": _enumerate_games(args.data_root, _SOURCE_DIRS["test_unseen"]),
    }

    # Content-stable ids from the pre-shuffle sorted order of the train pool;
    # the val carve keeps the id it was assigned as a train-pool member.
    for i, item in enumerate(train_all):
        item["id"] = f"{_ID_PREFIX['train']}_{i:04d}"
    val_idx = _stratified_val(train_all, args.val_size, rng)
    splits["val"] = [item for i, item in enumerate(train_all) if i in val_idx]
    splits["train"] = [item for i, item in enumerate(train_all) if i not in val_idx]
    for split in ("test_seen", "test_unseen"):
        for i, item in enumerate(splits[split]):
            item["id"] = f"{_ID_PREFIX[split]}_{i:04d}"

    # Shuffle every split (seeded): the env convention is that a prefix of a
    # split file is a random sample (slice_split / subset knobs rely on it).
    for split, items in splits.items():
        rng.shuffle(items)

    meta = {"seed": args.seed, "val_size": args.val_size, "data_root_note":
            "gamefile paths are relative to $ALFWORLD_DATA", "counts": {}}
    os.makedirs(args.out, exist_ok=True)
    for split, items in splits.items():
        d = os.path.join(args.out, split)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "items.json"), "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=1)
        meta["counts"][split] = len(items)
        types = Counter(i["task_type"] for i in items)
        print("%-12s n=%-5d %s" % (split, len(items),
              {t: types[t] for t in sorted(types)}))
    with open(os.path.join(args.out, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print("written to", args.out)


if __name__ == "__main__":
    main()
