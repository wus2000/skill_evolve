#!/usr/bin/env python3
"""Reusable experiment monitor — dump a comprehensive status report for a run.

Usage:  .venv/bin/python monitor.py [RUN_DIR]
If RUN_DIR omitted, uses the newest runs/spreadsheetbench_* dir.
Prints: process liveness, trace event counts, llm_call latency profile,
fail_reason distribution, coldstart strategy/rules, node/round artifacts,
and recent error-ish log lines.
"""
import collections
import glob
import json
import os
import statistics
import subprocess
import sys


def sh(cmd: str) -> str:
    return subprocess.run(["bash", "-c", cmd], capture_output=True, text=True).stdout.strip()


def main() -> None:
    if len(sys.argv) > 1:
        R = sys.argv[1]
    else:
        dirs = sorted(glob.glob("runs/spreadsheetbench_*"))
        R = dirs[-1] if dirs else ""
    if not R or not os.path.isdir(R):
        print("no run dir:", R)
        return
    print(f"=== RUN {R} ===")

    pid = sh("pgrep -f run_experiment_server.py | grep -v pgrep | tail -1")
    print("process:", f"ALIVE pid={pid}" if pid else "DEAD")

    tj = os.path.join(R, "trace.jsonl")
    evts = []
    if os.path.exists(tj):
        evts = [json.loads(l) for l in open(tj) if l.strip()]
    c = collections.Counter(e.get("event") for e in evts)
    print("trace events:", dict(c))

    calls = [e for e in evts if e.get("event") == "llm_call" and "duration_s" in e]
    by_role = collections.Counter(e.get("role") for e in calls)
    ds = sorted(e["duration_s"] for e in calls)
    if ds:
        print(f"llm_call n={len(ds)} roles={dict(by_role)} "
              f"med={statistics.median(ds):.1f} p90={ds[int(len(ds)*.9)]:.1f} "
              f"max={ds[-1]:.1f} >300s={sum(1 for d in ds if d>300)} >900s={sum(1 for d in ds if d>900)}")

    # fail_reason distribution from enriched conversation dirs (read result-ish fields if present)
    preds = sh(f"find {R} -name conversation.json | wc -l")
    print("predictions(conversation.json):", preds)
    # scan for timeout / failure markers in prediction text
    to = sh(f"grep -rlE 'timeout after|API request failed|llm-call-failed|task-timeout' {R} 2>/dev/null | wc -l")
    print("files with timeout/req-failed markers:", to)

    # coldstart / node strategy + rules
    for f in ["coldstart/strategy.md", "coldstart/rules.md"]:
        p = os.path.join(R, f)
        if os.path.exists(p):
            t = open(p).read()
            print(f"--- {f} ({len(t)} chars) ---")
            print(t[:1500])

    # node dirs + their rules.md sizes (main loop)
    nodes = sh(f"find {R} -maxdepth 2 -type d -name 'node*' 2>/dev/null | sort")
    if nodes:
        print("--- nodes ---")
        for nd in nodes.splitlines():
            rules = os.path.join(nd, "rules.md")
            strat = os.path.join(nd, "strategy.md")
            rsz = os.path.getsize(rules) if os.path.exists(rules) else 0
            ssz = os.path.getsize(strat) if os.path.exists(strat) else 0
            print(f"  {nd}  rules={rsz}B strategy={ssz}B")

    # round summaries
    rounds = sh(f"find {R} -maxdepth 2 -name 'round*.json' -o -maxdepth 2 -name 'rounds.json' 2>/dev/null")
    if rounds:
        print("--- round artifacts ---")
        print(rounds)

    # error-ish log
    errn = sh("grep -icE 'error|exception|traceback|request failed|timeout after|task-timeout' launch.log")
    print("launch.log error-ish lines:", errn)
    if errn and errn != "0":
        print(sh("grep -iE 'error|exception|traceback|request failed|timeout after|task-timeout' launch.log | tail -8"))

    print("--- log tail ---")
    print(sh("grep -vE 'Data Validation|warn.msg|SyntaxWarning|PatternRecord' launch.log | tail -8"))


if __name__ == "__main__":
    main()
