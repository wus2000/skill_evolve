#!/usr/bin/env python3
"""Backfill the coverage ledger from an existing run directory.

Pre-ledger runs (e.g. the live AppWorld run) have every train rollout's
outcome on disk but no ``global/coverage/ledger.json``. This tool rebuilds the
book from the rollout artifacts so a resumed run starts with full evidence
(docs/L1_actions_redesign.md §1.1).

Sources scanned (deployed-configuration TRAIN rollouts only):
  * ``nodes/<node>/burst_*/exploit/step*/rollout/predictions/<task>/r*/result.json``
    -> kind ``l0`` (the on-policy rollout under the node's CURRENT rules)

Deliberately NOT scanned:
  * ``.../verify/edit_*/...`` — verify rollouts run CANDIDATE rules (possibly
    gate-rejected, never deployed); booking their passes as solved re-creates
    the probe deadlock (code-review ruling 2026-07-07);
  * ``val_baseline`` / ``test_baseline`` / any val-side artifacts (val stays
    aggregate-only);
  * ``global/exploration`` probe rollouts (leads, never solved-state).

The registered task universe is the union of tasks seen in the scan plus any
previously registered ids; tasks the run never touched will register on the
next live burst (env.train_items()), so ``uncharted`` may under-report until
then — printed in the summary.

Usage:
    python tools/rebuild_coverage_ledger.py <run_dir> [--min-attempts N] [--dry-run]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from css.coverage import CoverageLedger, coverage_path  # noqa: E402

_L0_GLOB = "nodes/*/burst_*/exploit/step*/rollout/predictions/*/r*/result.json"
_NODE_RE = re.compile(r"nodes/([^/]+)/burst_(\d+)/")
_TASK_RE = re.compile(r"predictions/([^/]+)/r\d+/result\.json$")


def _scan(run_dir: str, pattern: str, kind: str, ledger: CoverageLedger) -> int:
    n = 0
    for path in glob.iglob(os.path.join(run_dir, pattern)):
        rel = os.path.relpath(path, run_dir).replace(os.sep, "/")
        node_m = _NODE_RE.search(rel)
        task_m = _TASK_RE.search(rel)
        if not node_m or not task_m:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        passed = False
        try:
            passed = int(d.get("hard", 0)) >= 1
        except (TypeError, ValueError):
            pass
        # decision_index sentinel: artifacts carry only the node-local burst
        # ordinal; mixing it with live global decision indices would corrupt
        # the last_decision scale (code-review 2026-07-07).
        ledger.record(node_m.group(1), task_m.group(1), passed, kind=kind,
                      decision_index=-1)
        n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run_dir", help="run directory (contains nodes/, global/)")
    ap.add_argument("--min-attempts", type=int, default=1,
                    help="ledger m threshold (default 1)")
    ap.add_argument("--dry-run", action="store_true",
                    help="scan and summarize without writing the ledger")
    args = ap.parse_args()

    run_dir = os.path.abspath(args.run_dir)
    if not os.path.isdir(os.path.join(run_dir, "nodes")):
        print("error: %s has no nodes/ directory" % run_dir, file=sys.stderr)
        return 2

    path = coverage_path(run_dir)
    prior = CoverageLedger.load(path, min_attempts=args.min_attempts)
    ledger = CoverageLedger(min_attempts=args.min_attempts, path=path)
    if prior.has_data():
        print("note: existing ledger found (%d nodes); rebuilding from scratch "
              "and re-registering its task universe" % len(prior.node_ids()))
    ledger.register_tasks(prior.uncharted() | prior.attempted_anywhere()
                          | prior.solved_anywhere())

    n_l0 = _scan(run_dir, _L0_GLOB, "l0", ledger)

    unsolved = sorted(ledger.global_unsolved())
    print("scanned: %d l0 rollouts (verify/candidate rollouts excluded by "
          "design)" % n_l0)
    print("nodes: %s" % ", ".join(ledger.node_ids()))
    print("tasks seen: %d | global_unsolved: %d | paradigm_sensitive: %d | "
          "uncharted (lower bound): %d"
          % (len(ledger.attempted_anywhere() | ledger.solved_anywhere()),
             len(unsolved), len(ledger.paradigm_sensitive()),
             len(ledger.uncharted())))
    if unsolved:
        print("global_unsolved: %s" % ", ".join(unsolved[:20])
              + (" ..." if len(unsolved) > 20 else ""))
    print("signature: %s" % ledger.signature())

    if args.dry_run:
        print("dry run: ledger NOT written")
        return 0
    ledger.save()
    print("wrote %s" % path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
