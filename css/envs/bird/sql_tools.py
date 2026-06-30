"""SQLite tools for the Bird Text-to-SQL environment.

Schema extraction, sandboxed read-only SQL execution with a hard (process-level)
timeout, and the official BIRD Execution-Accuracy (EX) comparison. Ported from
the agent_skills Bird adapter; self-contained (only stdlib).

The execution path is inherently sandboxed: every query runs through the
``sqlite3 -readonly`` CLI in a separate process with a hard timeout, so the task
agent can never mutate the database, escape the query, or hang the run.
"""
from __future__ import annotations

import json
import re
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


# ── Read-only execution with a HARD (process-level) timeout ─────────────────

def _run_query(db_path: str, sql: str, timeout: float | None = None) -> list[tuple]:
    """Run read-only SQL in an isolated subprocess; SIGKILL on timeout."""
    clean_sql = _strip_sql_comments(sql).strip()
    if not clean_sql:
        raise sqlite3.OperationalError("empty SQL after comment removal")

    to = float(timeout) if timeout and float(timeout) > 0 else None
    proc = subprocess.run(
        ["sqlite3", "-readonly", "-json", db_path, clean_sql],
        timeout=to, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise sqlite3.OperationalError(proc.stderr or "sqlite3 error")
    out = (proc.stdout or "").strip()
    if not out:
        return []
    return [tuple(row.values()) for row in json.loads(out)]


def execute_sql(
    db_path: str,
    sql: str,
    timeout: float = 30.0,
    max_rows: int = 30,
) -> dict:
    """Execute SQL read-only with a hard timeout. Returns a result dict.

    The query runs as-is. If the result set exceeds ``max_rows``, only the first
    ``max_rows`` are kept; the total count is always reported accurately.
    """
    try:
        rows = _run_query(db_path, sql, timeout=timeout)
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
