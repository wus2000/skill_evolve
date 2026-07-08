"""``result.json`` carries the trajectory — the cross-module contract.

Regression suite for the 2026-07-08 forensics: SpreadsheetBench stripped the
conversation out of ``result.json`` into a sibling file and re-hydrated it on
its own two read paths, so the THIRD reader (``css.materials``) silently
interpreted 540/540 empty trajectories and hallucinated every reading.

The contract these tests pin down:
  1. every env's persister writes the trajectory INTO result.json;
  2. an incomplete cache entry is rejected (re-roll), never substituted;
  3. the materials loader reports a contract violation loudly;
  4. an empty transcript never reaches an LLM.
"""
from __future__ import annotations

import json
import logging
import os

from css.config import CSSConfig
from css.data.rollout import TaskResult
from css.envs import common
from css.envs.spreadsheetbench.task_interface import SpreadsheetBenchEnv

CONV = [{"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "Action: bash"},
        {"role": "user", "content": "Observation: ok"}]


def _write_rollout(pred_dir: str, *, conversation, n_turns: int,
                   skill_hash: str = "abc", **extra) -> str:
    os.makedirs(pred_dir, exist_ok=True)
    d = {"id": "t1", "hard": 1, "soft": 1.0, "n_turns": n_turns,
         "skill_hash": skill_hash, **extra}
    if conversation is not None:
        d["conversation"] = conversation
    path = os.path.join(pred_dir, "result.json")
    with open(path, "w") as f:
        json.dump(d, f)
    return path


# ── 1. the persister ─────────────────────────────────────────────────────────
def test_spreadsheet_finalize_persists_the_trajectory(tmp_path):
    """The env that broke the contract now writes the trajectory into
    result.json, like every other env."""
    pred = str(tmp_path / "predictions" / "t1" / "r0")
    os.makedirs(pred)
    result = {"id": "t1", "hard": 1, "soft": 1.0, "n_turns": 2,
              "conversation": list(CONV), "skill_hash": "abc"}
    tr = SpreadsheetBenchEnv._finalize(result, pred, rollout_index=0, epoch=1,
                                       node_id="n0000")
    assert tr.messages == CONV, "the returned TaskResult carries the trajectory"
    on_disk = json.load(open(os.path.join(pred, "result.json")))
    assert on_disk["conversation"] == CONV, "and so does result.json"
    assert on_disk["rollout_index"] == 0 and on_disk["node_id"] == "n0000"
    assert not os.path.exists(os.path.join(pred, "conversation.json")), \
        "no sibling file: result.json is the single source of truth"
    assert TaskResult.from_dict(on_disk).messages == CONV, "round-trip"


def test_spreadsheet_finalize_survives_the_pre_agent_paths(tmp_path):
    """no-test-cases / exception returns reach _finalize before the agent ran:
    they must persist an explicit empty trajectory, not omit the key."""
    pred = str(tmp_path / "p")
    os.makedirs(pred)
    result = {"id": "t1", "hard": 0, "soft": 0.0, "n_turns": 0,
              "conversation": [], "fail_reason": "no-test-cases",
              "skill_hash": "abc"}
    tr = SpreadsheetBenchEnv._finalize(result, pred, rollout_index=0, epoch=0,
                                       node_id="n0")
    assert tr.messages == []
    assert "conversation" in json.load(open(os.path.join(pred, "result.json")))


def test_every_env_persister_is_the_shared_one():
    """No env may hand-roll persistence: that is how the contract broke."""
    import inspect
    import css.envs.common as C
    from css.envs.alfworld import task_interface as alf
    from css.envs.appworld import task_interface as aw
    from css.envs.bfcl import task_interface as bfcl
    from css.envs.scienceworld import task_interface as sw
    from css.envs.spreadsheetbench import task_interface as ss
    from css.envs.webarena import env as wa
    for mod in (alf, aw, bfcl, sw, ss, wa):
        src = inspect.getsource(mod)
        assert "persist_result" in src, f"{mod.__name__} must persist via common"
        assert 'if k != "conversation"' not in src, \
            f"{mod.__name__} strips the trajectory out of result.json"
    assert callable(C.persist_result)


# ── 2. the resume cache ──────────────────────────────────────────────────────
def test_cache_hit_returns_the_trajectory(tmp_path):
    _write_rollout(str(tmp_path / "predictions" / "t1" / "r0"),
                   conversation=CONV, n_turns=2)
    tr = common.load_cached_result({"id": "t1"}, str(tmp_path),
                                   rollout_index=0, skill_hash="abc")
    assert tr is not None and tr.messages == CONV


def test_incomplete_cache_entry_is_rejected_not_substituted(tmp_path, caplog):
    """A legacy result.json (turns recorded, trajectory missing) must re-roll.
    Substituting it would feed an empty transcript to every analysis layer."""
    _write_rollout(str(tmp_path / "predictions" / "t1" / "r0"),
                   conversation=None, n_turns=7)
    with caplog.at_level(logging.WARNING):
        tr = common.load_cached_result({"id": "t1"}, str(tmp_path),
                                       rollout_index=0, skill_hash="abc")
    assert tr is None, "incomplete cache entry -> re-roll"
    assert "incomplete cache entry" in caplog.text


def test_zero_turn_rollout_is_a_valid_cache_entry(tmp_path):
    """A setup failure legitimately has no turns and no trajectory."""
    _write_rollout(str(tmp_path / "predictions" / "t1" / "r0"),
                   conversation=[], n_turns=0)
    tr = common.load_cached_result({"id": "t1"}, str(tmp_path),
                                   rollout_index=0, skill_hash="abc")
    assert tr is not None and tr.messages == []


# ── 3. the materials loader ──────────────────────────────────────────────────
def _burst_dir(tmp_path, conversation) -> str:
    exploit = tmp_path / "exploit"
    pred = exploit / "step0" / "rollout" / "predictions" / "t1" / "r0"
    _write_rollout(str(pred), conversation=conversation, n_turns=2)
    return str(exploit)


def test_materials_loader_loads_messages(tmp_path):
    from css.materials.common import load_burst_trajectories
    trajs = load_burst_trajectories(_burst_dir(tmp_path, CONV))
    assert len(trajs) == 1 and trajs[0].result.messages == CONV


def test_materials_loader_screams_on_contract_violation(tmp_path, caplog):
    from css.materials.common import load_burst_trajectories
    with caplog.at_level(logging.ERROR):
        trajs = load_burst_trajectories(_burst_dir(tmp_path, None))
    assert len(trajs) == 1 and trajs[0].result.messages == []
    assert "CONTRACT VIOLATION" in caplog.text
    assert "1/1" in caplog.text


# ── 4. no LLM call on an empty transcript ────────────────────────────────────
def test_interpret_never_spends_a_call_on_an_empty_trajectory(tmp_path, caplog):
    from css.materials.common import LoadedTraj
    from css.materials.interpret import _interpret_one

    class Boom:
        def complete_optimizer(self, *a, **k):
            raise AssertionError("an empty transcript must never reach an LLM")

    traj = LoadedTraj(traj_id="s0_t1_r0", task_id="t1", rollout_index=0, step=0,
                      result=TaskResult.from_dict(
                          {"id": "t1", "hard": 0, "soft": 0.0, "n_turns": 5,
                           "fail_reason": "eval-mismatch", "conversation": []}))
    path = str(tmp_path / "traj.json")
    with caplog.at_level(logging.ERROR):
        rec = _interpret_one(traj, "", path, Boom(),
                             CSSConfig(n_train=1, n_val=1, n_test=1))
    assert rec["interp"]["_error"].startswith("empty trajectory")
    assert rec["interp_prose"] == ""
    assert "EMPTY trajectory" in caplog.text
    assert json.load(open(path))["interp"]["_error"], "the failure is persisted"
