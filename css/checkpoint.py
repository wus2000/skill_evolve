"""Stage-level checkpoint / resume for CSS runs.

A run is a sequence of STAGES — cold start, then round 0, round 1, …  Each stage
consumes the prior stage's output state and produces the next.  The entire state
needed to continue is small and already serializable:

  * the :class:`~css.data.tree.SearchTree` — every node carries its strategy,
    rules, scores, step buffer, and pattern library (``TreeNode.to_dict``);
  * the :class:`~css.data.negative_archive.NegativeArchive`;
  * a few scalars — ``baseline_score``, the next round index, and the RNG state.

Persisting that bundle at each stage boundary lets a re-run **resume from a
stage**, skipping every completed stage (and all of its rollouts / LLM calls)
without re-executing them.  Because completed stages are *loaded*, not replayed,
there is none of the determinism / concurrency / side-effect fragility a
call-level replay would face.

A ``config_fingerprint`` guards the restore: resuming onto a different config
(different models, splits, k_rollouts, …) is refused, so a checkpoint can never
be silently grafted onto an incompatible run.

The per-rollout result cache (``css.rollout.batch``) complements this at a finer
grain: within a stage that was interrupted mid-way, already-computed rollouts
are loaded from disk instead of re-run.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from css.data.negative_archive import NegativeArchive
from css.data.tree import SearchTree

if TYPE_CHECKING:  # pragma: no cover - type-only
    from css.config import CSSConfig

CHECKPOINT_DIR = "checkpoints"
_LATEST = "LATEST"

# Config fields whose change would invalidate a checkpoint (they alter what is
# computed). Cosmetic / runtime-only knobs (concurrency, timeouts, paths other
# than the data split) are deliberately excluded so a resume can re-tune them.
_FINGERPRINT_FIELDS = (
    "target_model", "optimizer_model",
    "n_train", "n_val", "n_test", "split_dir", "data_root",
    "k_rollouts", "N", "W", "K", "seed",
    "minibatch_size", "min_l0_epochs", "max_l0_epochs", "reflect_mode",
    "max_l1_iterations", "l1_diagnostic_tasks", "l1_regression_tasks",
    "merger_granularity", "gate_mode", "gate_paired_alpha",
    "gate_screen_k", "gate_escalation_k",
    "verify_floor_divisor", "verify_min_net_flips",
)


def config_fingerprint(cfg: "CSSConfig") -> str:
    """A short hash of the compute-affecting config fields (resume guard)."""
    payload = {k: getattr(cfg, k, None) for k in _FINGERPRINT_FIELDS}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def capture_rng_state() -> list:
    """Snapshot the global ``random`` state in a JSON-serializable form."""
    version, internal, gauss = random.getstate()
    return [version, list(internal), gauss]


def restore_rng_state(state: Any) -> None:
    """Restore a snapshot from :func:`capture_rng_state` (best-effort)."""
    if not state:
        return
    try:
        version, internal, gauss = state
        random.setstate((version, tuple(internal), gauss))
    except Exception:  # noqa: BLE001 - a bad snapshot must not block resume
        pass


@dataclass
class Checkpoint:
    """A complete, resumable snapshot of a CSS run at a stage boundary."""

    stage: str                       # "coldstart" | "round_0000" | ...
    next_round: int                  # the round index to run next
    baseline_score: float
    tree: SearchTree
    archive: NegativeArchive
    config_fingerprint: str
    rng_state: Any = None
    rounds: list[dict] = field(default_factory=list)  # RoundResult metadata
    created_ts: float = 0.0
    ledger: dict = field(default_factory=dict)        # TaskDifficultyLedger.to_dict()

    def to_dict(self) -> dict:
        return {
            "stage": self.stage,
            "next_round": self.next_round,
            "baseline_score": self.baseline_score,
            "config_fingerprint": self.config_fingerprint,
            "rng_state": self.rng_state,
            "rounds": self.rounds,
            "created_ts": self.created_ts,
            "ledger": self.ledger,
            # Embeddings are kept: the archive's recall and any restored pattern
            # state must survive the round-trip intact.
            "tree": self.tree.to_dict(include_embeddings=True),
            "archive": self.archive.to_dict(include_embeddings=True),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Checkpoint":
        return cls(
            stage=str(d.get("stage", "")),
            next_round=int(d.get("next_round", 0)),
            baseline_score=float(d.get("baseline_score", 0.0)),
            tree=SearchTree.from_dict(d.get("tree", {})),
            archive=NegativeArchive.from_dict(d.get("archive", {})),
            config_fingerprint=str(d.get("config_fingerprint", "")),
            rng_state=d.get("rng_state"),
            rounds=list(d.get("rounds", [])),
            created_ts=float(d.get("created_ts", 0.0)),
            ledger=dict(d.get("ledger", {})),
        )


def save_checkpoint(ckpt: Checkpoint, out_dir: str) -> str:
    """Atomically write ``ckpt`` to ``{out_dir}/checkpoints/ckpt_{stage}.json``.

    Also updates a ``LATEST`` pointer file so resume can find the newest
    checkpoint without parsing round numbers. Writes via a ``.tmp`` + ``os.replace``
    so a crash mid-write never corrupts an existing checkpoint.
    """
    cdir = os.path.join(out_dir, CHECKPOINT_DIR)
    os.makedirs(cdir, exist_ok=True)
    path = os.path.join(cdir, f"ckpt_{ckpt.stage}.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(ckpt.to_dict(), f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, path)
    with open(os.path.join(cdir, _LATEST), "w", encoding="utf-8") as f:
        f.write(os.path.basename(path))
    return path


def load_checkpoint(path: str) -> Checkpoint:
    """Load a :class:`Checkpoint` from a JSON file."""
    with open(path, encoding="utf-8") as f:
        return Checkpoint.from_dict(json.load(f))


def latest_checkpoint(out_dir: str) -> str | None:
    """Return the path to the newest checkpoint in ``out_dir``, or ``None``.

    Prefers the ``LATEST`` pointer; falls back to picking the highest-numbered
    ``ckpt_round_*.json`` (or ``ckpt_coldstart.json``) so resume still works if
    the pointer is missing.
    """
    cdir = os.path.join(out_dir, CHECKPOINT_DIR)
    if not os.path.isdir(cdir):
        return None
    pointer = os.path.join(cdir, _LATEST)
    if os.path.exists(pointer):
        try:
            name = open(pointer, encoding="utf-8").readline().strip()
            cand = os.path.join(cdir, name)
            if name and os.path.exists(cand):
                return cand
        except Exception:  # noqa: BLE001
            pass
    files = [f for f in os.listdir(cdir) if f.startswith("ckpt_") and f.endswith(".json")]
    if not files:
        return None

    def _order(fname: str) -> tuple[int, int]:
        if "coldstart" in fname:
            return (0, -1)
        try:
            return (1, int(fname[len("ckpt_round_"):-len(".json")]))
        except ValueError:
            return (2, 0)

    files.sort(key=_order)
    return os.path.join(cdir, files[-1])
