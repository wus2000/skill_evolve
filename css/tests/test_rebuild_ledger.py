"""Rebuild tool: backfill the coverage ledger from run artifacts."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from css.coverage import load_coverage

_TOOL = str(Path(__file__).resolve().parents[2] / "tools" / "rebuild_coverage_ledger.py")


def _result(root: Path, rel: str, hard: int) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"hard": hard, "soft": float(hard)}))


def _seed_run(root: Path) -> None:
    # l0 rollouts: n0000 fails t1 twice, solves t2; n0001 solves t1.
    _result(root, "nodes/n0000/burst_0000/exploit/step0/rollout/predictions/t1/r0/result.json", 0)
    _result(root, "nodes/n0000/burst_0001/exploit/step2/rollout/predictions/t1/r1/result.json", 0)
    _result(root, "nodes/n0000/burst_0000/exploit/step0/rollout/predictions/t2/r0/result.json", 1)
    _result(root, "nodes/n0001/burst_0000/exploit/step1/rollout/predictions/t1/r0/result.json", 1)
    # verify rollout: n0000 fails t3 under a candidate.
    _result(root, "nodes/n0000/burst_0001/exploit/step3/verify/edit_0/predictions/t3/r0/result.json", 0)
    # MUST-IGNORE artifacts: val baseline and an exploration probe.
    _result(root, "nodes/n0000/val_baseline/predictions/t9/r0/result.json", 1)
    _result(root, "global/exploration/sessions/session_0001/probes/probe_000_rollouts/predictions/t1/r0/result.json", 1)


def test_rebuild_scans_train_sources_and_ignores_val_and_probes(tmp_path):
    _seed_run(tmp_path)
    out = subprocess.run([sys.executable, _TOOL, str(tmp_path)],
                         capture_output=True, text=True, check=True)
    assert "wrote" in out.stdout
    led = load_coverage(str(tmp_path))
    # t1: n0000 failed twice (l0), n0001 solved -> paradigm-sensitive.
    assert led.stats("n0000", "t1")["attempts"] == 2
    assert led.stats("n0000", "t1")["kinds"] == {"l0": 2}
    assert led.solved_set("n0001") == {"t1"}
    # verify rollout recorded with its kind.
    assert led.stats("n0000", "t3")["kinds"] == {"verify": 1}
    assert led.global_unsolved() == {"t3"}
    assert led.paradigm_sensitive() == {"t1"}
    # val task t9 and the probe pass on t1 must be ABSENT from the book:
    assert led.stats("n0000", "t9")["attempts"] == 0
    # (the probe would have added a third t1 attempt / a phantom node)
    assert set(led.node_ids()) == {"n0000", "n0001"}


def test_rebuild_dry_run_writes_nothing(tmp_path):
    _seed_run(tmp_path)
    out = subprocess.run([sys.executable, _TOOL, str(tmp_path), "--dry-run"],
                         capture_output=True, text=True, check=True)
    assert "dry run" in out.stdout
    assert not load_coverage(str(tmp_path)).has_data()
