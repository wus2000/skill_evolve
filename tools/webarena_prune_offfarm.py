#!/usr/bin/env python3
"""ONE-OFF: drop tasks whose sites the farm does not host.

The sealed splits were cut from the Verified 684 pool ("812 minus map-touching"),
but 6 train tasks still reference ``wikipedia`` as a second site — the farm hosts
only shopping / shopping_admin / reddit / gitlab. Their ``__WIKIPEDIA__``
placeholder never resolves, so they are permanently unsolvable: they poison the
coverage ledger's unsolved/fragile accounting and feed meaningless failures into
the minibatch. Removing them is the honest fix; the loader also asserts the
invariant so this can never silently return.

    python3 tools/webarena_prune_offfarm.py [--apply]
"""
from __future__ import annotations

import json
import os
import sys

SPLIT_DIR = "data/webarena_splits"
FARM_SITES = {"shopping", "shopping_admin", "reddit", "gitlab"}


def main() -> None:
    apply = "--apply" in sys.argv[1:]
    manifest_path = os.path.join(SPLIT_DIR, "MANIFEST.json")
    manifest = json.load(open(manifest_path, encoding="utf-8"))
    dropped_all: dict[str, list] = {}

    for split in ("train", "val", "test"):
        path = os.path.join(SPLIT_DIR, f"{split}.json")
        recs = json.load(open(path, encoding="utf-8"))
        keep, drop = [], []
        for r in recs:
            off = sorted(set(r.get("sites", [])) - FARM_SITES)
            (drop if off else keep).append(r if not off else
                                           {"task_id": r["task_id"],
                                            "sites": r["sites"],
                                            "off_farm": off})
        dropped_all[split] = drop
        print(f"{split:6s} {len(recs):3d} -> {len(keep):3d}   dropped {len(drop)}")
        for d in drop:
            print(f"         task {d['task_id']:4d} sites={d['sites']} "
                  f"off-farm={d['off_farm']}")
        if apply and drop:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(keep, f, ensure_ascii=False, indent=1)
            manifest.setdefault("report", {}).setdefault(split, {})["n"] = len(keep)

    if apply:
        manifest["farm_sites"] = sorted(FARM_SITES)
        manifest["pruned_off_farm"] = {
            "reason": "farm hosts only the core four sites; __WIKIPEDIA__ never "
                      "resolves, so these tasks are permanently unsolvable",
            "dropped": {k: [d["task_id"] for d in v]
                        for k, v in dropped_all.items() if v},
        }
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=1)
        print("\nsplits + MANIFEST rewritten")
    else:
        total = sum(len(v) for v in dropped_all.values())
        print(f"\nwould drop {total} task(s); re-run with --apply to write")


if __name__ == "__main__":
    main()
