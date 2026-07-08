#!/usr/bin/env python3
"""ONE-OFF data repair: fold a legacy SpreadsheetBench run's sibling
``conversation.json`` back into its ``result.json``.

Before 2026-07-08 the SpreadsheetBench persister stripped the trajectory out of
``result.json`` (see css/envs/spreadsheetbench/task_interface.py's module
docstring). Runs produced then are unreadable to ``css.materials`` and are
rejected by the resume cache as incomplete. This script repairs those runs on
disk so they can be re-analyzed; it is NOT part of any production code path —
the loader has no fallback and must never grow one.

Idempotent, and a no-op on healthy runs. Writes atomically.

    python3 tools/migrate_spreadsheet_results.py runs/spreadsheetbench_XXXX [--apply]

Without ``--apply`` it only reports what it would change.
"""
from __future__ import annotations

import json
import os
import sys


def repair(run_dir: str, apply: bool) -> tuple[int, int, int]:
    fixed = healthy = orphaned = 0
    for root, _dirs, files in os.walk(run_dir):
        if "result.json" not in files:
            continue
        rpath = os.path.join(root, "result.json")
        try:
            with open(rpath, encoding="utf-8") as f:
                d = json.load(f)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! unreadable {rpath}: {exc}")
            continue
        if not isinstance(d, dict):
            continue
        if d.get("messages") or d.get("conversation"):
            healthy += 1
            continue
        cpath = os.path.join(root, "conversation.json")
        if not os.path.exists(cpath):
            if int(d.get("n_turns") or 0) > 0:
                orphaned += 1
                print(f"  ! turns={d.get('n_turns')} but no conversation.json: {root}")
            continue
        try:
            with open(cpath, encoding="utf-8") as f:
                conversation = json.load(f)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! unreadable {cpath}: {exc}")
            continue
        if not isinstance(conversation, list) or not conversation:
            orphaned += 1
            continue
        fixed += 1
        if apply:
            d["conversation"] = conversation
            tmp = rpath + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=2, default=str)
            os.replace(tmp, rpath)
    return fixed, healthy, orphaned


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    run_dir = sys.argv[1]
    apply = "--apply" in sys.argv[2:]
    if not os.path.isdir(run_dir):
        sys.exit(f"not a directory: {run_dir}")
    fixed, healthy, orphaned = repair(run_dir, apply)
    verb = "repaired" if apply else "would repair"
    print(f"\n{run_dir}\n  {verb}: {fixed}   already healthy: {healthy}   "
          f"unrecoverable: {orphaned}")
    if fixed and not apply:
        print("  (re-run with --apply to write)")


if __name__ == "__main__":
    main()
