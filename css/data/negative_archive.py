"""Negative archive — meta-auxiliary memory of disproven strategy directions.

The negative archive is tree-global (design D12): it records strategies that
were pruned or that failed PROPOSAL/REFINE rollout validation, so the search
does not blindly re-explore them. It is read during Layer 5 (PROPOSAL/REFINE)
via embedding recall + LLM judgment.

Crucial semantics — *reminder, not prohibition*: a high similarity to an
archived failure does NOT veto a new strategy. The LLM must be able to
articulate how the new direction differs (different root cause, timing, or L0
rule basis). If it cannot, the direction is blocked at Layer 5b.

Phase 1 provides storage + a pluggable similarity-recall hook. The embedding
backend is wired in Phase 4.6; until then ``recall`` accepts an injected
similarity function so the structure stays testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal

NegativeOrigin = Literal[
    "pruned_node",
    "proposal_failed_rollout",
    "refine_failed_rollout",
]


@dataclass
class NegativeArchiveEntry:
    """One disproven strategy attempt."""

    entry_id: str
    strategy_snapshot: str             # the abandoned strategy text (or REFINE diff)
    origin: NegativeOrigin
    root_cause: str = ""               # Layer 4 attribution summary (why it was tried)
    failure_evidence: str = ""         # rollout data / learning curve / comparison
    created_epoch: int = -1
    created_step: int = -1
    source_node_id: str = ""
    embedding: list[float] | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "NegativeArchiveEntry":
        return cls(
            entry_id=str(d.get("entry_id", "")),
            strategy_snapshot=str(d.get("strategy_snapshot", "")),
            origin=d.get("origin", "pruned_node"),
            root_cause=str(d.get("root_cause", "")),
            failure_evidence=str(d.get("failure_evidence", "")),
            created_epoch=int(d.get("created_epoch", -1)),
            created_step=int(d.get("created_step", -1)),
            source_node_id=str(d.get("source_node_id", "")),
            embedding=d.get("embedding"),
        )

    def to_dict(self, include_embedding: bool = False) -> dict:
        d: dict[str, Any] = {
            "entry_id": self.entry_id,
            "strategy_snapshot": self.strategy_snapshot,
            "origin": self.origin,
            "root_cause": self.root_cause,
            "failure_evidence": self.failure_evidence,
            "created_epoch": self.created_epoch,
            "created_step": self.created_step,
            "source_node_id": self.source_node_id,
        }
        if include_embedding and self.embedding is not None:
            d["embedding"] = self.embedding
        return d


@dataclass
class NegativeArchive:
    """Append-only collection of disproven strategies (tree-global)."""

    entries: list[NegativeArchiveEntry] = field(default_factory=list)
    _next_id: int = 0

    def __len__(self) -> int:
        return len(self.entries)

    def new_entry_id(self) -> str:
        eid = f"neg{self._next_id:04d}"
        self._next_id += 1
        return eid

    def add(self, entry: NegativeArchiveEntry) -> None:
        self.entries.append(entry)

    def recall(
        self,
        query_embedding: list[float],
        top_k: int,
        similarity_fn: Callable[[list[float], list[float]], float],
    ) -> list[tuple[NegativeArchiveEntry, float]]:
        """Return the top-``k`` most similar archived entries with scores.

        ``similarity_fn`` is injected (e.g. cosine similarity over Qwen3
        embeddings in Phase 4.6) so this structure has no embedding dependency.
        Entries without an embedding are skipped.
        """
        scored: list[tuple[NegativeArchiveEntry, float]] = []
        for e in self.entries:
            if e.embedding is None:
                continue
            scored.append((e, similarity_fn(query_embedding, e.embedding)))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    @classmethod
    def from_dict(cls, d: dict) -> "NegativeArchive":
        arch = cls(_next_id=int(d.get("_next_id", 0)))
        arch.entries = [NegativeArchiveEntry.from_dict(e) for e in d.get("entries", [])]
        return arch

    def to_dict(self, include_embeddings: bool = True) -> dict:
        # Embeddings ARE the point of persisting the archive: ``recall`` needs
        # them, and they must survive a checkpoint restore. Unlike the (large,
        # recomputable) per-observation embeddings, archive embeddings default to
        # being saved.
        return {
            "_next_id": self._next_id,
            "entries": [e.to_dict(include_embeddings) for e in self.entries],
        }
