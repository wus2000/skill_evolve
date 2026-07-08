"""Node coverage ledger — the sole authority for solved/unsolved task state.

Design: docs/L1_actions_redesign.md §1.1. The ledger records, per
``(node_id, task_id)``, the cumulative attempts/passes of every TRAIN rollout
executed under the node's REAL configuration (strategy + evolving rules): L0
on-policy minibatches, verify target/control rollouts. It deliberately
records NOTHING else:

  * probe rollouts — a behavior prompt is not the node's actual strategy;
    probe reachability must never flip a task's solved state (a lucky probe
    could empty ``global_unsolved``, trigger MERGE over strategies none of
    which solve the task, and permanently orphan it). Probe passes go to the
    leads archive (:mod:`css.explore.leads`) instead.
  * val/test rollouts — val crosses the mechanism boundary only as aggregate
    statistics (gate verdicts, node val_score); val task identities never
    reach generation-side inputs.

Recording is EXPLICIT at each call site (``record_groups``) rather than a
hook inside the rollout layer: which rollouts count is auditable where they
run, and the probe path simply never calls it.

States (m = ``cfg.ledger_min_attempts``):
  * solved(n, t):       passes >= 1
  * unsolved(n, t):     attempts >= m and passes == 0
  * unattempted(n, t):  attempts < m

Derived global sets (union-of-evidence semantics — a task leaves
``global_unsolved`` only by actually being solved somewhere; sampling drift
cannot empty it):
  * global_unsolved     — attempted (>= m) by >= 1 node, solved by NO node
  * paradigm_sensitive  — solved at >= 1 node AND unsolved at >= 1 node
  * uncharted           — no node has attempted >= m times
  * shortfall(n)        — tasks n has unsolved that some other node solved

The partition signature hashes each task's global three-state; it changes
only when a task's state actually flips, which gates downstream recomputation
(global synthesis, exploration-session cache reuse).
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import threading
from typing import Any, Iterable

_log = logging.getLogger("css.coverage")

_LEDGER_REL = os.path.join("global", "coverage", "ledger.json")


def coverage_path(out_dir: str) -> str:
    return os.path.join(out_dir, _LEDGER_REL)


def _atomic_write_json(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        # default=str matches the repo's canonical atomic writers (a local copy
        # is kept to avoid cross-layer imports, not to diverge in tolerance).
        json.dump(obj, f, ensure_ascii=False, indent=1, default=str)
    os.replace(tmp, path)


class CoverageLedger:
    """Cumulative per-(node, task) train outcomes with derived global sets."""

    def __init__(self, task_ids: Iterable[str] = (), *, min_attempts: int = 1,
                 path: str = "", recent_len: int = 12) -> None:
        self.min_attempts = max(1, int(min_attempts))
        self.recent_len = max(1, int(recent_len))
        self.path = path
        # RLock: public readers take the lock too (the verify-era fanout
        # taught us reads can overlap worker writes), and derived-set
        # methods call each other, so the lock must be re-entrant.
        self._lock = threading.RLock()
        # node_id -> task_id -> {"attempts": int, "passes": int,
        #                        "kinds": {kind: n}, "last_decision": int,
        #                        "recent": [[decision, step_in_burst, kind,
        #                                    passed], ...]}  (ring, newest last)
        self._nodes: dict[str, dict[str, dict]] = {}
        self._registered: set[str] = {str(t) for t in task_ids if str(t)}

    # ── registration ──────────────────────────────────────────────────────
    def register_tasks(self, task_ids: Iterable[str]) -> None:
        """Register the full train task universe (the ``uncharted`` domain)."""
        with self._lock:
            self._registered.update(str(t) for t in task_ids if str(t))

    # ── writes ────────────────────────────────────────────────────────────
    def record(self, node_id: str, task_id: str, passed: bool, *,
               kind: str = "", decision_index: int = -1,
               step_in_burst: int = -1) -> None:
        node_id = str(node_id or "")
        task_id = str(task_id or "")
        if not node_id or not task_id:
            return
        with self._lock:
            self._registered.add(task_id)
            cell = self._nodes.setdefault(node_id, {}).setdefault(
                task_id, {"attempts": 0, "passes": 0, "kinds": {},
                          "last_decision": -1, "recent": []})
            cell["attempts"] += 1
            if passed:
                cell["passes"] += 1
            if kind:
                cell["kinds"][kind] = int(cell["kinds"].get(kind, 0)) + 1
            if decision_index >= 0:
                cell["last_decision"] = max(int(cell["last_decision"]),
                                            int(decision_index))
            # Maturity-ordered evidence ring (user ruling 2026-07-08): keep
            # the RAW grain — (decision, step_in_burst, kind, passed) — and
            # apply policy (which attempts count as mature) at read time.
            rec = cell.setdefault("recent", [])
            rec.append([int(decision_index), int(step_in_burst),
                        str(kind), bool(passed)])
            if len(rec) > self.recent_len:
                del rec[: len(rec) - self.recent_len]

    def record_groups(self, node_id: str, groups: Iterable[Any], *,
                      kind: str = "", decision_index: int = -1,
                      step_in_burst: int = -1) -> int:
        """Fold every rollout of ``groups`` (TaskRolloutGroup) in; return count."""
        n = 0
        for g in groups or []:
            for r in getattr(g, "rollouts", []) or []:
                tid = str(getattr(r, "task_id", "") or "")
                if not tid:
                    continue
                self.record(node_id, tid, bool(getattr(r, "passed", False)),
                            kind=kind, decision_index=decision_index,
                            step_in_burst=step_in_burst)
                n += 1
        return n

    # ── per-node reads (all readers take the lock: record() runs on worker
    # threads, and iterating a dict during a concurrent insert raises) ────
    def registered_count(self) -> int:
        """Size of the registered train universe (0 = never registered —
        full coverage must NEVER be declared over an unknown universe)."""
        with self._lock:
            return len(self._registered)

    def stats(self, node_id: str, task_id: str) -> dict:
        with self._lock:
            cell = self._nodes.get(str(node_id), {}).get(str(task_id))
            return dict(cell) if cell else {"attempts": 0, "passes": 0,
                                            "kinds": {}, "last_decision": -1}

    def solved_set(self, node_id: str) -> set[str]:
        with self._lock:
            return {t for t, c in self._nodes.get(str(node_id), {}).items()
                    if c["passes"] >= 1}

    def unsolved_set(self, node_id: str) -> set[str]:
        with self._lock:
            m = self.min_attempts
            return {t for t, c in self._nodes.get(str(node_id), {}).items()
                    if c["attempts"] >= m and c["passes"] == 0}

    # ── global reads ──────────────────────────────────────────────────────
    def solved_anywhere(self) -> set[str]:
        with self._lock:
            out: set[str] = set()
            for cells in self._nodes.values():
                out.update(t for t, c in cells.items() if c["passes"] >= 1)
            return out

    def attempted_anywhere(self) -> set[str]:
        with self._lock:
            m = self.min_attempts
            out: set[str] = set()
            for cells in self._nodes.values():
                out.update(t for t, c in cells.items() if c["attempts"] >= m)
            return out

    def global_unsolved(self) -> set[str]:
        """Attempted (>= m) by >= 1 node, solved by NO node."""
        return self.attempted_anywhere() - self.solved_anywhere()

    def uncharted(self) -> set[str]:
        """Registered tasks neither sufficiently attempted NOR solved.

        solved_anywhere is subtracted explicitly: with min_attempts > 1 a
        task solved on its only attempt is not 'attempted' (1 < m) yet is
        certainly not a blind spot — leaving it here would block the MERGE
        phase transition forever (code-review finding, 2026-07-07)."""
        with self._lock:
            return (self._registered - self.attempted_anywhere()
                    - self.solved_anywhere())

    def paradigm_sensitive(self) -> set[str]:
        with self._lock:
            solved = self.solved_anywhere()
            out: set[str] = set()
            for nid in self._nodes:
                out.update(self.unsolved_set(nid) & solved)
            return out

    def solvers(self, task_id: str) -> list[str]:
        with self._lock:
            tid = str(task_id)
            return sorted(nid for nid, cells in self._nodes.items()
                          if cells.get(tid, {}).get("passes", 0) >= 1)

    def shortfall(self, node_id: str) -> dict[str, list[str]]:
        """Tasks this node has unsolved that OTHER nodes solved -> solvers."""
        me = str(node_id)
        out: dict[str, list[str]] = {}
        for t in self.unsolved_set(me):
            others = [n for n in self.solvers(t) if n != me]
            if others:
                out[t] = others
        return out

    def exclusive_coverage(self, node_id: str) -> set[str]:
        """Tasks solved by this node and by NO other node (MERGE matrix cell)."""
        me = str(node_id)
        mine = self.solved_set(me)
        return {t for t in mine if all(n == me for n in self.solvers(t))}

    def fragile_set(self, *, rate_lt: float = 0.4, mature_step: int = 2,
                    mature_min: int = 3, hist_attempts: int = 4) -> set[str]:
        """Tasks whose MATURE evidence says they are not reliably solved.

        Fragile is a property of (task x mature agent), NOT of the task's
        whole history (user ruling 2026-07-08): an attempt's evidential value
        depends on where in a burst it ran — early-burst failures reflect an
        unsettled agent (fresh edits, un-gated rules), so only attempts at
        ``step_in_burst >= mature_step`` with ``kind == "l0"`` (deployed,
        gate-vetted rules; verify runs candidate configurations) count as
        mature evidence. With ``>= mature_min`` mature attempts the recent
        pass rate decides; otherwise fall back to the whole-history rate
        (a task not drawn recently must not lose frontier status). Pooled
        across nodes. Old ledgers without ``recent`` degrade to the fallback.

        NEW supply reads this; the MERGE phase transition keeps the union
        solved-once semantics (:meth:`global_unsolved`) — the two uses ask
        different temporal questions and are deliberately decoupled.
        """
        with self._lock:
            per_task: dict[str, dict] = {}
            for cells in self._nodes.values():
                for tid, c in cells.items():
                    agg = per_task.setdefault(
                        tid, {"attempts": 0, "passes": 0, "m_att": 0,
                              "m_pass": 0})
                    agg["attempts"] += int(c.get("attempts", 0))
                    agg["passes"] += int(c.get("passes", 0))
                    for rec in c.get("recent", []) or []:
                        try:
                            _dec, step, kind, passed = rec
                        except (TypeError, ValueError):
                            continue
                        if str(kind) == "l0" and int(step) >= mature_step:
                            agg["m_att"] += 1
                            agg["m_pass"] += 1 if passed else 0
            out: set[str] = set()
            for tid, a in per_task.items():
                if a["m_att"] >= mature_min:
                    if a["m_pass"] / a["m_att"] < rate_lt:
                        out.add(tid)
                elif a["attempts"] >= hist_attempts:
                    if a["passes"] / a["attempts"] < rate_lt:
                        out.add(tid)
            return out

    def failure_weight(self, task_id: str) -> int:
        """Total failed attempts across nodes (priority signal for targeting)."""
        with self._lock:
            tid = str(task_id)
            total = 0
            for cells in self._nodes.values():
                c = cells.get(tid)
                if c and c["passes"] == 0:
                    total += int(c["attempts"])
            return total

    def node_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._nodes.keys())

    def has_data(self) -> bool:
        with self._lock:
            return any(self._nodes.values())

    # ── signature ─────────────────────────────────────────────────────────
    def signature(self) -> str:
        """Hash of the global per-task three-state partition.

        Changes only when a task's state flips (first solve, first sufficient
        attempt) — attempts piling up inside a state do NOT move it, so
        downstream consumers (global synthesis, exploration caches) can key
        on it without churn.
        """
        with self._lock:
            solved = self.solved_anywhere()
            attempted = self.attempted_anywhere()
            universe = sorted(self._registered | attempted | solved)
        parts = []
        for t in universe:
            state = ("S" if t in solved
                     else "U" if t in attempted else "N")
            parts.append(f"{t}:{state}")
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]

    # ── persistence ───────────────────────────────────────────────────────
    def save(self, path: str = "") -> str:
        p = path or self.path
        if not p:
            raise ValueError("CoverageLedger.save: no path configured")
        with self._lock:
            # Deep-copy under the lock: the payload must not alias the live
            # dict, or json.dump (outside the lock) races concurrent record()
            # calls from worker threads (code-review finding, 2026-07-07).
            payload = {
                "version": 1,
                "registered_task_ids": sorted(self._registered),
                "nodes": copy.deepcopy(self._nodes),
            }
        _atomic_write_json(p, payload)
        return p

    @classmethod
    def load(cls, path: str, *, min_attempts: int = 1,
             recent_len: int = 12) -> "CoverageLedger":
        led = cls(min_attempts=min_attempts, path=path,
                  recent_len=recent_len)
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return led
        led._registered = {str(t) for t in d.get("registered_task_ids", [])}
        nodes = d.get("nodes", {})
        if isinstance(nodes, dict):
            for nid, cells in nodes.items():
                if not isinstance(cells, dict):
                    continue
                clean: dict[str, dict] = {}
                for tid, c in cells.items():
                    if not isinstance(c, dict):
                        continue
                    recent = []
                    for rec in c.get("recent", []) or []:
                        if isinstance(rec, (list, tuple)) and len(rec) == 4:
                            recent.append([int(rec[0]), int(rec[1]),
                                           str(rec[2]), bool(rec[3])])
                    clean[str(tid)] = {
                        "attempts": int(c.get("attempts", 0)),
                        "passes": int(c.get("passes", 0)),
                        "kinds": dict(c.get("kinds", {})),
                        "last_decision": int(c.get("last_decision", -1)),
                        "recent": recent,
                    }
                led._nodes[str(nid)] = clean
        return led


def load_coverage(out_dir: str, *, min_attempts: int = 1) -> CoverageLedger:
    """Load the run's coverage ledger (empty ledger when absent)."""
    return CoverageLedger.load(coverage_path(out_dir), min_attempts=min_attempts)
