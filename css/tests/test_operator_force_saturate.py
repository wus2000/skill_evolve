"""Operator FORCE_SATURATE flag (experiment-ops override, 2026-07-05).

The flag file lives at the run root; exploitation reads it at step boundaries
(non-consuming) and the SYNC branch pass consumes it (one-shot rename) after
steering the decision to PROPOSAL. Outside the resume fingerprint by
construction (it is a file, not a config field).
"""
import os

from css.optimizer.exploitation import operator_force_saturate


def test_absent_flag_is_false(tmp_path):
    assert operator_force_saturate(str(tmp_path)) is False


def test_flag_found_at_ancestor(tmp_path):
    (tmp_path / "FORCE_SATURATE").write_text("")
    deep = tmp_path / "n0000" / "round_0001" / "exploit"
    deep.mkdir(parents=True)
    assert operator_force_saturate(str(deep)) is True
    # non-consuming read leaves the flag in place
    assert (tmp_path / "FORCE_SATURATE").exists()


def test_consume_renames_one_shot(tmp_path):
    (tmp_path / "FORCE_SATURATE").write_text("")
    assert operator_force_saturate(str(tmp_path), consume=True) is True
    assert not (tmp_path / "FORCE_SATURATE").exists()
    assert (tmp_path / "FORCE_SATURATE.consumed").exists()
    # second read: flag gone -> False (later rounds unaffected)
    assert operator_force_saturate(str(tmp_path)) is False


def test_search_depth_bounded(tmp_path):
    (tmp_path / "FORCE_SATURATE").write_text("")
    too_deep = tmp_path
    for i in range(7):
        too_deep = too_deep / ("d%d" % i)
    too_deep.mkdir(parents=True)
    assert operator_force_saturate(str(too_deep)) is False
