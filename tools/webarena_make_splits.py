"""Seal WebArena train/val/test split manifests from the Verified dataset.

Scope and rules (agreed 2026-07-06, evidence in docs/env_prep/webarena_PREP.md):
- Source of truth: WebArena-Verified dataset JSON (ServiceNow) — corrected
  references + deterministic evaluator specs; task identity == original 812.
- Scope: the 684-task / 5-category protocol (WebChoreArena -> ReasoningBank
  chain): drop every task whose ``sites`` touch ``map``.
- Split unit: ``intent_template_id`` GROUPS within each site category —
  sibling tasks are parameter swaps of one procedure; task-level splits leak
  (AWM: "highly overlapping canonical trajectories").
- Targets 65/10/25 with a val trim pass toward an absolute val-size target
  (gate cost: every val task costs K rollouts per gate side).

Usage:
    python tools/webarena_make_splits.py \
        --dataset /path/to/webarena-verified.json \
        --out data/webarena_splits [--seed 20260706] [--val-target 68]
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import random

CATEGORIES = ["shopping", "shopping_admin", "gitlab", "reddit", "multi"]
TARGETS = {"train": 0.65, "val": 0.10, "test": 0.25}


def category(rec: dict) -> str:
    return "multi" if len(rec["sites"]) > 1 else rec["sites"][0]


def load_pool(dataset_path: str) -> list[dict]:
    with open(dataset_path) as f:
        records = json.load(f)
    pool = [r for r in records if "map" not in r["sites"]]
    if len(pool) != 684:
        raise SystemExit(f"expected 684 map-free tasks, got {len(pool)} — "
                         "dataset drifted; re-audit before sealing")
    return pool


def assign_splits(pool: list[dict], seed: int) -> dict[str, list[dict]]:
    """Per-category template-group draw toward TARGETS (leakage-free)."""
    rng = random.Random(seed)
    per_cat: dict[str, dict[int, list[dict]]] = collections.defaultdict(
        lambda: collections.defaultdict(list))
    for r in pool:
        per_cat[category(r)][r["intent_template_id"]].append(r)

    splits: dict[str, list[dict]] = {s: [] for s in TARGETS}
    for cat in CATEGORIES:
        groups = per_cat[cat]
        total = sum(len(v) for v in groups.values())
        fill = {s: 0 for s in TARGETS}
        # Guarantee each split holds >=1 group per category: seed the three
        # LARGEST groups round-robin (train/test/val order keeps val lean).
        order = sorted(groups, key=lambda g: -len(groups[g]))
        seeded, rest = order[:3], order[3:]
        for i, g in enumerate(seeded):
            s = ["train", "test", "val"][i % 3]
            splits[s] += groups[g]
            fill[s] += len(groups[g])
        rng.shuffle(rest)
        for g in rest:
            s = min(TARGETS, key=lambda s: fill[s] / (TARGETS[s] * total + 1e-9))
            splits[s] += groups[g]
            fill[s] += len(groups[g])
    return splits


def trim_val(splits: dict[str, list[dict]], val_target: int) -> None:
    """Move smallest val template groups to train until val <= target.

    Never removes a category's LAST val group (each category must keep >=1
    val group where it had one).
    """
    while len(splits["val"]) > val_target:
        by_group: dict[tuple, list[dict]] = collections.defaultdict(list)
        for r in splits["val"]:
            by_group[(category(r), r["intent_template_id"])].append(r)
        cat_counts = collections.Counter(c for c, _ in by_group)
        movable = [(len(v), k) for k, v in by_group.items() if cat_counts[k[0]] > 1]
        if not movable:
            break
        n, key = min(movable)
        if len(splits["val"]) - n < val_target - 6:  # would overshoot far past target
            break
        moved = by_group[key]
        splits["val"] = [r for r in splits["val"] if r not in moved]
        splits["train"] += moved


def verify(splits: dict[str, list[dict]]) -> dict:
    seen: dict[int, str] = {}
    for s, items in splits.items():
        for r in items:
            t = r["intent_template_id"]
            if seen.setdefault(t, s) != s:
                raise SystemExit(f"LEAK: template {t} spans {seen[t]} and {s}")
    total = sum(len(v) for v in splits.values())
    if total != 684:
        raise SystemExit(f"split total {total} != 684")
    report = {}
    for s, items in splits.items():
        cats = collections.Counter(category(r) for r in items)
        ttypes = collections.Counter(
            exp.get("task_type") for r in items for e in r["eval"]
            if isinstance((exp := e.get("expected") or {}), dict) and exp.get("task_type"))
        report[s] = {"n": len(items),
                     "templates": len({r["intent_template_id"] for r in items}),
                     "per_category": {c: cats.get(c, 0) for c in CATEGORIES},
                     "task_types": dict(ttypes)}
    return report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True,
                    help="webarena-verified.json (assets/dataset/)")
    ap.add_argument("--out", default="data/webarena_splits")
    ap.add_argument("--seed", type=int, default=20260706)
    ap.add_argument("--val-target", type=int, default=68)
    args = ap.parse_args()

    with open(args.dataset, "rb") as f:
        dataset_sha = hashlib.sha256(f.read()).hexdigest()

    pool = load_pool(args.dataset)
    splits = assign_splits(pool, args.seed)
    trim_val(splits, args.val_target)
    report = verify(splits)

    os.makedirs(args.out, exist_ok=True)
    for s, items in splits.items():
        items = sorted(items, key=lambda r: r["task_id"])
        rng = random.Random(args.seed + hash(s) % 1000)
        rng.shuffle(items)  # pre-shuffled per convention; deterministic
        for r in items:
            r["id"] = f"wa_{r['task_id']:04d}"  # stable unique id (guide §2.1)
        with open(os.path.join(args.out, f"{s}.json"), "w") as f:
            json.dump(items, f, indent=1)
    manifest = {
        "dataset_sha256": dataset_sha,
        "seed": args.seed,
        "val_target": args.val_target,
        "scope": "684 = 812 minus map-touching (WebChoreArena->ReasoningBank chain)",
        "split_unit": "intent_template_id groups within site category",
        "report": report,
    }
    with open(os.path.join(args.out, "MANIFEST.json"), "w") as f:
        json.dump(manifest, f, indent=1)
    print(json.dumps(report, indent=1))
    print(f"sealed under {args.out} (dataset sha256 {dataset_sha[:16]}…)")


if __name__ == "__main__":
    main()
