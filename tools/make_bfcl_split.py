#!/usr/bin/env python3
"""Generate BFCL multi-turn split manifests for CSS (seed-deterministic).

Split design (agreed 2026-07-05, see docs/env_prep/bfcl_PREP.md §11): the four
challenge categories MIRROR each other by scenario index — base_i / miss_func_i /
miss_param_i / long_context_i (and composite_i) are the SAME scenario with
different perturbations (verified: involved_classes identical 200/200). Splitting
by row would leak scenarios across train/test, so we split by SCENARIO INDEX,
domain-stratified, taking all category variants of an index together:

  * 200 scenario indices, each owned by exactly one PRIMARY backend domain
    (GorillaFileSystem / VehicleControlAPI / TradingBot / TravelAPI — 50 each);
  * within each domain, seed-42 shuffle then 60/20/20 -> 30/10/10 indices;
  * emit all 4 active categories for each index:
        train 120 idx x4 = 480,  val 40 x4 = 160,  test 40 x4 = 160;
  * composite_probe = composite entries on the TEST indices only (~40) — a sealed
    probe (composite shares scenarios by index, so it is only clean on test idx).

Every challenge type appears in every split; zero scenario leakage. Each manifest
item carries the per-turn gold call sequences under ``ground_truth`` — the
OPTIMIZER-ONLY eval annotation (the agent never sees it; GT firewall).

Reads the PINNED checkout (commit 6ea57973c7a6097fd7c5915698c54c17c5b1b6c8):
  env_candidates/gorilla/berkeley-function-call-leaderboard/bfcl_eval/data
Run: python3 tools/make_bfcl_split.py
"""
from __future__ import annotations

import json
import os
import random

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(
    REPO, "env_candidates", "gorilla", "berkeley-function-call-leaderboard",
    "bfcl_eval", "data")
OUT = os.path.join(REPO, "data", "bfcl_split_seed42")
SEED = 42
ACTIVE = ["base", "miss_func", "miss_param", "long_context"]
PRIMARIES = ["GorillaFileSystem", "VehicleControlAPI", "TradingBot", "TravelAPI"]
PINNED_COMMIT = "6ea57973c7a6097fd7c5915698c54c17c5b1b6c8"


def _jsonl(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _load(cat: str) -> tuple[dict[int, dict], dict[int, list]]:
    if cat == "composite":  # composite ships under unused_datasets/
        q_path = os.path.join(DATA, "unused_datasets", "question",
                              "BFCL_v4_multi_turn_composite.json")
        gt_path = os.path.join(DATA, "unused_datasets", "possible_answer",
                               "BFCL_v4_multi_turn_composite.json")
    else:
        q_path = os.path.join(DATA, f"BFCL_v4_multi_turn_{cat}.json")
        gt_path = os.path.join(DATA, "possible_answer", f"BFCL_v4_multi_turn_{cat}.json")
    q = {_idx(e["id"]): e for e in _jsonl(q_path)}
    g = {_idx(e["id"]): e["ground_truth"] for e in _jsonl(gt_path)}
    return q, g


def _idx(entry_id: str) -> int:
    return int(entry_id.rsplit("_", 1)[1])


def _category(entry_id: str) -> str:
    return entry_id[len("multi_turn_"):].rsplit("_", 1)[0]


def _make_item(entry: dict, gt: list, cat: str) -> dict:
    return {
        "id": entry["id"],
        "category": cat,
        "scenario_index": _idx(entry["id"]),
        "involved_classes": entry["involved_classes"],
        "initial_config": entry["initial_config"],
        "question": entry["question"],
        "missed_function": entry.get("missed_function", {}),
        "long_context": cat in ("long_context", "composite"),
        "ground_truth": gt,          # OPTIMIZER-ONLY (GT firewall); agent never sees it
    }


def main() -> None:
    cats = {c: _load(c) for c in ACTIVE}
    comp_q, comp_g = _load("composite")

    # base defines the scenario universe + primary-domain ownership.
    base_q = cats["base"][0]
    indices = sorted(base_q)
    domain_of: dict[int, str] = {}
    for i in indices:
        prims = [c for c in base_q[i]["involved_classes"] if c in PRIMARIES]
        if len(prims) != 1:
            raise SystemExit(f"index {i}: expected exactly 1 primary domain, got {prims}")
        domain_of[i] = prims[0]

    rng = random.Random(SEED)
    split_idx: dict[str, list[int]] = {"train": [], "val": [], "test": []}
    for dom in PRIMARIES:
        dom_idx = sorted(i for i in indices if domain_of[i] == dom)
        rng.shuffle(dom_idx)
        n = len(dom_idx)
        n_tr, n_va = int(round(n * 0.60)), int(round(n * 0.20))
        split_idx["train"] += dom_idx[:n_tr]
        split_idx["val"] += dom_idx[n_tr:n_tr + n_va]
        split_idx["test"] += dom_idx[n_tr + n_va:]

    manifests: dict[str, list[dict]] = {}
    for split in ("train", "val", "test"):
        items = []
        for i in sorted(split_idx[split]):
            for cat in ACTIVE:
                q, g = cats[cat]
                if i in q and i in g:
                    items.append(_make_item(q[i], g[i], cat))
        manifests[split] = items
    # composite_probe: composite on the TEST indices only (sealed probe).
    manifests["composite_probe"] = [
        _make_item(comp_q[i], comp_g[i], "composite")
        for i in sorted(split_idx["test"]) if i in comp_q and i in comp_g
    ]

    for split, items in manifests.items():
        d = os.path.join(OUT, split)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "items.json"), "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=1)

    meta = {
        "pinned_commit": PINNED_COMMIT, "seed": SEED,
        "n_scenario_indices": len(indices),
        "split_index_counts": {k: len(v) for k, v in split_idx.items()},
        "manifest_counts": {k: len(v) for k, v in manifests.items()},
        "per_category_per_split": {
            split: {cat: sum(1 for it in manifests[split] if it["category"] == cat)
                    for cat in ACTIVE}
            for split in ("train", "val", "test")},
    }
    with open(os.path.join(OUT, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
