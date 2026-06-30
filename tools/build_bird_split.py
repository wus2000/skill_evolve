#!/usr/bin/env python3
"""Build a deterministic BIRD Text-to-SQL split for CSS.

  * train + val  come from the BIRD-23 FILTERED train set (a .jsonl of
    {db_id, question, evidence, SQL}). They are made reproducible by a
    sort-then-shuffle (``sort(db_id, question, SQL)`` -> ``random.Random(seed)
    .shuffle``) and split 5:1 (train:val).
  * test         is the held-out BIRD DEV set (dev.json), used verbatim.

Items carry ``db_id`` (NOT an absolute ``db_path``): each split's databases live
under a different root (train_databases vs dev_databases), so the env resolves
the path at load time from the per-split db_root. This keeps the split portable
across machines.

Usage:
    python tools/build_bird_split.py \
        --train-jsonl data/bird23-train-filtered/data/train-00000-of-00001.jsonl \
        --dev-json    /path/to/bird_raw/dev/dev.json \
        --out-dir     data/bird_split_filtered_seed42 \
        --seed 42 --val-ratio 0.16666667
"""
from __future__ import annotations

import argparse
import json
import os
import random


def _load_jsonl(path: str) -> list[dict]:
    items: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def _norm_train(raw: dict, stable_id: str) -> dict:
    """Canonical train/val item — gold SQL kept under 'SQL' (env never shows it)."""
    return {
        "id": stable_id,
        "db_id": raw.get("db_id", ""),
        "question": raw.get("question", ""),
        "evidence": raw.get("evidence", ""),
        "SQL": raw.get("SQL", raw.get("sql", "")),
    }


def _norm_dev(raw: dict) -> dict:
    qid = raw.get("question_id", "")
    return {
        "id": f"dev_{qid}",
        "question_id": qid,
        "db_id": raw.get("db_id", ""),
        "question": raw.get("question", ""),
        "evidence": raw.get("evidence", ""),
        "SQL": raw.get("SQL", raw.get("sql", "")),
        "difficulty": raw.get("difficulty", ""),
    }


def _write_split(out_dir: str, name: str, items: list[dict]) -> None:
    d = os.path.join(out_dir, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "items.json"), "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--train-jsonl", required=True)
    ap.add_argument("--dev-json", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--val-ratio", type=float, default=1.0 / 6.0,
                    help="fraction of the filtered train set used for val (5:1 -> 1/6)")
    args = ap.parse_args()

    # ── train + val: filtered train, sort-then-shuffle, 5:1 ─────────────────
    raw_train = _load_jsonl(args.train_jsonl)
    # Stable order BEFORE shuffle so the split is reproducible regardless of the
    # source file's row order; the stable id is anchored to this sorted index.
    raw_train.sort(key=lambda r: (r.get("db_id", ""), r.get("question", ""),
                                  r.get("SQL", r.get("sql", ""))))
    train_items = [_norm_train(r, f"ftrain_{i}") for i, r in enumerate(raw_train)]
    random.Random(args.seed).shuffle(train_items)

    n = len(train_items)
    val_n = round(n * args.val_ratio)
    train_split = train_items[val_n:]
    val_split = train_items[:val_n]

    # ── test: BIRD dev, verbatim ────────────────────────────────────────────
    raw_dev = json.load(open(args.dev_json, encoding="utf-8"))
    test_split = [_norm_dev(r) for r in raw_dev]

    os.makedirs(args.out_dir, exist_ok=True)
    _write_split(args.out_dir, "train", train_split)
    _write_split(args.out_dir, "val", val_split)
    _write_split(args.out_dir, "test", test_split)

    manifest = {
        "source_train": os.path.basename(args.train_jsonl),
        "source_test": os.path.basename(args.dev_json),
        "method": (
            "train+val: filtered train -> sort(db_id,question,SQL) -> "
            f"random.Random({args.seed}).shuffle -> split 5:1 (train:val); "
            "test: BIRD dev verbatim"
        ),
        "shuffle_seed": args.seed,
        "val_ratio": args.val_ratio,
        "counts": {
            "train": len(train_split),
            "val": len(val_split),
            "test": len(test_split),
        },
        "db_roots": {
            "train": "BIRD train_databases (<db_id>/<db_id>.sqlite)",
            "val": "BIRD train_databases (<db_id>/<db_id>.sqlite)",
            "test": "BIRD dev_databases (<db_id>/<db_id>.sqlite)",
        },
        "note": (
            "Items carry db_id, not db_path. train/val use the BIRD TRAIN "
            "databases; test uses the BIRD DEV databases — set the env's "
            "db_root (train/val) and test_db_root (test) accordingly."
        ),
    }
    with open(os.path.join(args.out_dir, "split_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"train={len(train_split)}  val={len(val_split)}  test={len(test_split)}")
    print(f"written to {args.out_dir}")


if __name__ == "__main__":
    main()
