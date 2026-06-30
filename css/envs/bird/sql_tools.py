"""SQLite tools for the Bird Text-to-SQL environment.

Schema extraction, sandboxed read-only SQL execution with a hard (process-level)
timeout, and the official BIRD Execution-Accuracy (EX) comparison. Ported from
the agent_skills Bird adapter; self-contained (only stdlib).

The execution path is inherently sandboxed: every query runs in a separate
``python`` subprocess via the stdlib ``sqlite3`` module opened read-only
(``mode=ro``), under a hard wall-clock timeout + output byte cap. The task agent
can never mutate the database, escape the query, or hang the run. We do NOT
depend on the ``sqlite3`` CLI binary (often absent on servers) — only a Python
interpreter with the stdlib ``sqlite3`` module, which is always present.
"""
from __future__ import annotations

import json
import os
import re
import sys
import signal
import sqlite3
import subprocess
import threading


# ── SQL sanitisation ──────────────────────────────────────────────────────

def _strip_sql_comments(sql: str) -> str:
    """Remove SQL line comments (``-- ...``) the sqlite3 CLI misreads as flags."""
    return re.sub(r"--[^\n]*", "", sql)


# ── Schema extraction ──────────────────────────────────────────────────────

def get_schema(db_path: str, sample_rows: int = 2, max_cols_sample: int = 12) -> str:
    """Return a readable schema: CREATE TABLE statements + a few sample rows."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except Exception:  # noqa: BLE001 - fall back to a plain connection
        conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        tables = cur.fetchall()
        parts: list[str] = []
        for name, create_sql in tables:
            block = (create_sql or f"TABLE {name}").strip()
            parts.append(block + ";")
            if sample_rows > 0:
                try:
                    cur.execute(f'SELECT * FROM "{name}" LIMIT {int(sample_rows)}')
                    cols = [d[0] for d in cur.description][:max_cols_sample]
                    rows = cur.fetchall()
                    if rows:
                        preview_lines = [" | ".join(cols)]
                        for r in rows:
                            preview_lines.append(
                                " | ".join(str(v)[:40] for v in r[:max_cols_sample])
                            )
                        parts.append(
                            f"/* sample rows from {name}:\n"
                            + "\n".join(preview_lines)
                            + "\n*/"
                        )
                except Exception:  # noqa: BLE001 - sampling is best-effort
                    pass
        return "\n\n".join(parts) if parts else "(no tables found)"
    finally:
        conn.close()


# ── Read-only execution: HARD wall-clock timeout + output byte cap ──────────

#: Hard cap on raw sqlite3 stdout bytes. BIRD result sets are tiny; this only
#: trips on pathological queries (runaway cross-join / recursive CTE) and bounds
#: peak memory regardless of how much output the query *would* have produced —
#: the output is streamed and the engine is killed the instant the cap is passed,
#: never materialised whole.
_MAX_OUTPUT_BYTES = 16 * 1024 * 1024  # 16 MB


class ResultTooLargeError(Exception):
    """Raised when a query's output passes the byte cap (the engine is killed)."""


# Read-only query worker run as ``python -c``. Streams each row as one NDJSON
# line so the parent can byte-cap + kill mid-stream. con.execute runs exactly ONE
# statement (a multi-statement string raises -> rejected), and mode=ro forbids
# writes. db_path/sql arrive as argv (no shell, so arbitrary SQL is safe).
_SQLITE_WORKER = r"""
import sys, json, sqlite3
db, sql = sys.argv[1], sys.argv[2]
try:
    con = sqlite3.connect("file:" + db + "?mode=ro", uri=True)
except Exception:
    con = sqlite3.connect(db)
try:
    cur = con.execute(sql)
except Exception as e:
    sys.stderr.write("%s: %s" % (type(e).__name__, e)); sys.exit(3)
out = sys.stdout
try:
    while True:
        rows = cur.fetchmany(1000)
        if not rows:
            break
        out.write("".join(json.dumps(list(r)) + "\n" for r in rows))
        out.flush()
finally:
    con.close()
"""


def _kill_process_group(proc: "subprocess.Popen") -> None:
    """SIGKILL the whole process group of ``proc``. Best-effort; never raises.

    The worker is started with ``start_new_session=True`` so it leads its own
    group; killing the group tears down the interpreter (and the SQLite engine in
    it) immediately and uncatchably.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def _run_query(
    db_path: str,
    sql: str,
    timeout: float | None = None,
    max_output_bytes: int = _MAX_OUTPUT_BYTES,
) -> list[tuple]:
    """Run read-only SQL in an isolated subprocess under two hard bounds.

    1. WALL-CLOCK: the process group is SIGKILLed if it runs past ``timeout``.
    2. OUTPUT SIZE: a reader thread streams stdout and SIGKILLs the process group
       the instant cumulative output passes ``max_output_bytes`` — so a runaway
       result set is bounded, never buffered whole into memory.

    Raises ``subprocess.TimeoutExpired`` on timeout, :class:`ResultTooLargeError`
    on overflow, ``sqlite3.OperationalError`` on a SQL error.
    """
    clean_sql = _strip_sql_comments(sql).strip()
    if not clean_sql:
        raise sqlite3.OperationalError("empty SQL after comment removal")
    to = float(timeout) if timeout and float(timeout) > 0 else None

    proc = subprocess.Popen(
        [sys.executable, "-c", _SQLITE_WORKER, db_path, clean_sql],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    chunks: list[bytes] = []
    state = {"total": 0, "over_cap": False}

    def _drain() -> None:
        try:
            while True:
                buf = proc.stdout.read(65536)
                if not buf:
                    break
                chunks.append(buf)
                state["total"] += len(buf)
                if state["total"] > max_output_bytes:
                    state["over_cap"] = True
                    _kill_process_group(proc)
                    break
        except Exception:  # noqa: BLE001 - reader never propagates
            pass

    reader = threading.Thread(target=_drain, daemon=True)
    reader.start()
    try:
        proc.wait(timeout=to)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        reader.join(timeout=5)
        raise
    reader.join(timeout=5)

    if state["over_cap"]:
        raise ResultTooLargeError(
            f"result exceeded {max_output_bytes // (1024 * 1024)} MB and was stopped"
        )
    try:
        stderr = proc.stderr.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        stderr = ""
    if proc.returncode not in (0, None):
        raise sqlite3.OperationalError(stderr or "sqlite3 error")
    out = b"".join(chunks).decode("utf-8", errors="replace")
    rows: list[tuple] = []
    for line in out.splitlines():
        line = line.strip()
        if line:
            rows.append(tuple(json.loads(line)))
    return rows


def execute_sql(
    db_path: str,
    sql: str,
    timeout: float = 30.0,
    max_rows: int = 30,
) -> dict:
    """Execute SQL read-only with a hard timeout + output cap. Returns a dict.

    The query runs as-is. If the result set exceeds ``max_rows``, only the first
    ``max_rows`` are kept; the total count is always reported accurately.
    Timeout / oversize / SQL errors come back as ``{"ok": False, "error": ...}``
    with actionable guidance for the agent (never raised).
    """
    try:
        rows = _run_query(db_path, sql, timeout=timeout)
    except ResultTooLargeError as e:
        return {
            "ok": False,
            "error": (
                f"{e}. Narrow the query: add a LIMIT, aggregate (COUNT/SUM/...), "
                "or select fewer columns/rows."
            ),
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": (
                f"query timed out after {timeout:g}s and was killed. Simplify it "
                "(avoid large cross joins / unbounded recursion) or add a LIMIT."
            ),
        }
    except Exception as e:  # noqa: BLE001 - surfaced to the agent as an error obs
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {
        "ok": True,
        "rows": rows[:max_rows],
        "n_rows": len(rows),
        "truncated": len(rows) > max_rows,
    }


def format_result(res: dict, max_chars: int = 3000) -> str:
    """Render an :func:`execute_sql` result dict as text for the agent."""
    if not res.get("ok"):
        return f"[SQL ERROR] {res.get('error', 'unknown error')}"
    rows = res.get("rows", [])
    n_total = res.get("n_rows", len(rows))
    n_shown = len(rows)
    if res.get("truncated"):
        head = (
            f"[OK] Total {n_total} rows, result set is large, "
            f"only showing first {n_shown} rows:"
        )
    else:
        head = f"[OK] {n_total} row(s)"
    body = "\n".join(" | ".join(str(v)[:80] for v in r) for r in rows)
    text = head + ("\n" + body if body else "")
    if len(text) > max_chars:
        text = (
            text[:max_chars]
            + f"\n...[display truncated at {max_chars} chars, "
            f"{n_shown} rows shown out of {n_total} total]"
        )
    return text


# ── Official BIRD Execution Accuracy (EX) ──────────────────────────────────

_gold_cache: dict[tuple[str, str], list[tuple]] = {}
_gold_lock = threading.Lock()
_GOLD_CACHE_MAX = 4096


def _gold_rows(db_path: str, gold_sql: str, timeout: float) -> list[tuple]:
    key = (db_path, gold_sql)
    with _gold_lock:
        if key in _gold_cache:
            return _gold_cache[key]
    rows = _run_query(db_path, gold_sql, timeout=timeout)
    with _gold_lock:
        if len(_gold_cache) >= _GOLD_CACHE_MAX:
            _gold_cache.clear()
        _gold_cache[key] = rows
    return rows


def ex_match(
    predicted_sql: str,
    gold_sql: str,
    db_path: str,
    timeout: float = 30.0,
) -> dict:
    """BIRD Execution Accuracy: ``ex=1`` iff the result SETS of pred and gold match.

    ``soft`` is the Jaccard overlap of the two result sets — a graded signal the
    optimizer can use even when EX is 0. Gold rows are cached per (db, sql).
    """
    if not str(predicted_sql or "").strip():
        return {"ex": 0, "soft": 0.0, "error": "empty predicted SQL"}

    try:
        gold_rows = _gold_rows(db_path, gold_sql, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return {"ex": 0, "soft": 0.0, "error": f"gold SQL failed: {e}"}

    try:
        pred_rows = _run_query(db_path, predicted_sql, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return {"ex": 0, "soft": 0.0, "error": f"predicted SQL error: {e}"}

    try:
        pred_set = set(map(tuple, pred_rows))
        gold_set = set(map(tuple, gold_rows))
    except TypeError:
        ex = int(pred_rows == gold_rows)
        return {"ex": ex, "soft": float(ex)}

    ex = int(pred_set == gold_set)
    if not gold_set and not pred_set:
        soft = 1.0
    elif not (gold_set | pred_set):
        soft = 0.0
    else:
        soft = len(pred_set & gold_set) / len(pred_set | gold_set)
    return {"ex": ex, "soft": round(soft, 4)}
