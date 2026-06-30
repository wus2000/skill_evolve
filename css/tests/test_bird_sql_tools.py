"""Reliability tests for the Bird SQL execution core (css.envs.bird.sql_tools).

Covers the three robustness guarantees the infrastructure must hold:
  1. a runaway (CPU-bound, no-output) query is KILLED at the wall-clock timeout;
  2. a runaway (huge-output) query is KILLED at the byte cap, never buffered whole;
  3. many concurrent read-only queries against the same DB all succeed.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from css.envs.bird.sql_tools import (
    ResultTooLargeError,
    _run_query,
    execute_sql,
    format_result,
)

# A recursive CTE that spins on CPU without emitting a row until it finishes
# (it never finishes within the timeout) -> exercises the wall-clock kill.
_SPIN_NO_OUTPUT = (
    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x < 1000000000) "
    "SELECT count(*) FROM c"
)
# A recursive CTE that emits rows forever -> exercises the output byte cap.
_SPIN_HUGE_OUTPUT = (
    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT x FROM c"
)


@pytest.fixture()
def db():
    tmp = tempfile.mkdtemp(prefix="bird_sql_")
    path = os.path.join(tmp, "d.sqlite")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.executemany("INSERT INTO t (x) VALUES (?)", [(i,) for i in range(50)])
    conn.commit()
    conn.close()
    yield path
    shutil.rmtree(tmp, ignore_errors=True)


def test_timeout_kills_cpu_bound_query(db):
    """A spinning query is killed at the timeout and reported, not left hanging."""
    start = time.monotonic()
    res = execute_sql(db, _SPIN_NO_OUTPUT, timeout=2.0)
    elapsed = time.monotonic() - start
    assert res["ok"] is False
    assert "timed out" in res["error"]
    # The kill is prompt: nowhere near the (effectively unbounded) query runtime.
    assert elapsed < 15, f"timeout kill took {elapsed:.1f}s — not prompt"


def test_byte_cap_stops_huge_output(db):
    """A query emitting unbounded rows trips the byte cap (no whole-result buffering)."""
    start = time.monotonic()
    with pytest.raises(ResultTooLargeError):
        _run_query(db, _SPIN_HUGE_OUTPUT, timeout=15, max_output_bytes=100_000)
    elapsed = time.monotonic() - start
    assert elapsed < 15, f"byte-cap kill took {elapsed:.1f}s — not prompt"


def test_execute_sql_reports_oversize_with_guidance(db):
    """execute_sql surfaces the oversize stop as actionable feedback, never raises."""
    res = execute_sql(db, _SPIN_HUGE_OUTPUT, timeout=15)
    # default 16MB cap; the unbounded query trips it
    assert res["ok"] is False
    assert "LIMIT" in res["error"] or "aggregate" in res["error"]


def test_max_rows_truncation_feedback(db):
    """Result-row cap truncates the display and reports the true total."""
    res = execute_sql(db, "SELECT x FROM t ORDER BY x", timeout=10, max_rows=30)
    assert res["ok"] is True
    assert res["n_rows"] == 50 and res["truncated"] is True
    assert len(res["rows"]) == 30
    rendered = format_result(res)
    assert "only showing first 30 rows" in rendered


def test_concurrent_reads_same_db(db):
    """Many concurrent read-only queries against one DB all succeed (multi-reader)."""
    def _q(_i):
        return execute_sql(db, "SELECT COUNT(*) FROM t", timeout=10)

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(_q, range(64)))
    assert all(r["ok"] and r["rows"][0][0] == 50 for r in results)


def test_clean_query_still_works(db):
    """Sanity: a normal query returns correct rows under the hardened path."""
    res = execute_sql(db, "SELECT x FROM t WHERE x < 3 ORDER BY x", timeout=10)
    assert res["ok"] and [r[0] for r in res["rows"]] == [0, 1, 2]
