"""L0 exploitation fixes (2026-07-05 root-cause investigation).

Covers: verify harm-veto admission, merger divide-and-conquer retry,
adjudicate repair empty-wipe guard, and the unused-ledger carry-over loader.
"""
from __future__ import annotations

import json
import os

from css.optimizer.exploitation import _evaluate_edit_criterion


def _tr(status, inc_pr=0.0, cand_pr=0.0):
    return {"status": status, "inc_pr": inc_pr, "cand_pr": cand_pr,
            "inc_solvable": False, "cand_solvable": False}


# ── verify: harm-veto vs full ───────────────────────────────────────────────
def test_harm_veto_passes_null_edits():
    # No measurable effect (the 79-83% kill class) -> PASS; gate arbitrates.
    tasks = {"a": _tr("STILL_UNSOLVED"), "b": _tr("RETAINED")}
    assert _evaluate_edit_criterion(tasks, k_rollouts=3, mode="harm_veto")
    assert not _evaluate_edit_criterion(tasks, k_rollouts=3, mode="full")


def test_harm_veto_kills_measured_net_harm():
    tasks = {"a": _tr("LOST"), "b": _tr("RETAINED")}
    assert not _evaluate_edit_criterion(tasks, k_rollouts=3, mode="harm_veto")
    tasks2 = {"a": _tr("LOST"), "b": _tr("GAINED"), "c": _tr("LOST")}
    assert not _evaluate_edit_criterion(tasks2, k_rollouts=3, mode="harm_veto")


def test_harm_veto_neutral_and_gain_pass():
    assert _evaluate_edit_criterion(
        {"a": _tr("LOST"), "b": _tr("GAINED")}, k_rollouts=3, mode="harm_veto")
    assert _evaluate_edit_criterion(
        {"a": _tr("GAINED")}, k_rollouts=3, mode="harm_veto")


def test_harm_veto_continuous_drift_veto():
    # No binary flips but strongly negative continuous drift -> veto.
    tasks = {"a": _tr("STILL_UNSOLVED", inc_pr=0.67, cand_pr=0.0),
             "b": _tr("RETAINED", inc_pr=1.0, cand_pr=0.67)}
    assert not _evaluate_edit_criterion(tasks, k_rollouts=3, mode="harm_veto")


def test_full_mode_unchanged():
    assert _evaluate_edit_criterion(
        {"a": _tr("GAINED")}, k_rollouts=3, mode="full")
    assert not _evaluate_edit_criterion(
        {"a": _tr("GAINED"), "b": _tr("LOST")}, k_rollouts=3, mode="full")


# ── merger: divide-and-conquer on unparseable output ────────────────────────
def test_merger_split_retry(monkeypatch):
    from css.data.edit import Edit, Patch, RawPatch
    import css.optimizer.editpipe.merger as M

    def _rp(subject):
        return RawPatch(patch=Patch(edits=[Edit(
            op="add_point", content="always check %s" % subject,
            target="Checks", subject=subject, source_tasks=["t1"])]))

    patches = [_rp("alpha"), _rp("beta"), _rp("gamma"), _rp("delta")]
    calls = {"n": 0}

    def fake_complete(client, system, user, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # full merge: unparseable
        return [{"kind": "add_point", "subject": "s%d" % calls["n"],
                 "content": "do the thing", "placement": {"section": "Checks"},
                 "source_raw_edits": [1], "rationale": "r"}]

    monkeypatch.setattr(M, "complete_optimizer_json", fake_complete)
    edits, viols, audits, stats = M.run_merger(None, "## Checks\n- x", patches)
    assert stats.get("split_retry") is True
    assert calls["n"] == 3               # 1 failed full + 2 half merges
    assert len(edits) >= 1               # halves delivered edits


# ── unused-ledger carry-over loader ─────────────────────────────────────────
def test_previous_unused_loader(tmp_path):
    from css.optimizer.editpipe.pipeline import _load_previous_unused
    prev = tmp_path / "step3"
    cur = tmp_path / "step4"
    prev.mkdir()
    cur.mkdir()
    (prev / "edit_audit.json").write_text(json.dumps(
        {"unused_raw_edits": [{"raw_edit": 7, "subject": "s",
                               "content_head": "c", "source_tasks": ["t"]}]}))
    got = _load_previous_unused(str(cur / "edit_audit.json"))
    assert got and got[0]["raw_edit"] == 7
    # step0 has no predecessor; missing files are empty, never raise
    assert _load_previous_unused(str(tmp_path / "step0" / "edit_audit.json")) == []
    assert _load_previous_unused(None) == []
