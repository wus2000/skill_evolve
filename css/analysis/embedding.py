"""Embedding and vector-index foundation for the Layer 2 clustering stage.

This module provides:

  * :class:`Embedder` — a structural ``Protocol`` describing the minimal
    interface the rest of the analysis pipeline depends on: a ``dim`` property
    and an ``embed(texts) -> ndarray`` method returning a ``float32`` matrix of
    L2-normalized row vectors.
  * :class:`Qwen3Embedder` — the concrete production embedder. It loads
    ``cfg.embedding_model`` (default ``Qwen3-Embedding-0.6B``) via
    ``sentence-transformers``. The heavy import is performed lazily on first use
    so that simply importing this module never touches the network or downloads
    a model.
  * :class:`StubEmbedder` — a deterministic, dependency-free test double. Each
    text is mapped to a fixed vector seeded by a stable hash of the text, so the
    SAME text always yields the SAME vector (and similar/identical texts cluster
    deterministically). No model, no network.
  * :class:`VectorIndex` — a cosine-similarity index over normalized vectors.
    Because the vectors are L2-normalized, cosine similarity equals the inner
    product. It uses faiss ``IndexFlatIP`` when faiss is importable, otherwise it
    falls back to an exact numpy implementation. ``backend`` reports which path
    is active.
  * :func:`build_index` and :func:`cosine_similarity` convenience helpers.

Design references: ``design_final_en.md`` §8 (Technical Stack) and
``training_mechanism_v6.md`` D8 (embedding) / D13.
"""
from __future__ import annotations

import hashlib
from typing import Protocol, runtime_checkable

import numpy as np

# A small epsilon to guard against division-by-zero when normalizing a vector
# that is (numerically) all zeros.
_EPS = 1e-12


# ──────────────────────────────────────────────────────────────────────────
# Normalization helpers
# ──────────────────────────────────────────────────────────────────────────
def _l2_normalize(matrix: np.ndarray) -> np.ndarray:
    """Return ``matrix`` with each row scaled to unit L2 norm (float32)."""
    matrix = np.asarray(matrix, dtype=np.float32)
    if matrix.ndim == 1:
        matrix = matrix.reshape(1, -1)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms = np.maximum(norms, _EPS)
    return (matrix / norms).astype(np.float32)


# ──────────────────────────────────────────────────────────────────────────
# Embedder protocol + concrete implementations
# ──────────────────────────────────────────────────────────────────────────
@runtime_checkable
class Embedder(Protocol):
    """Structural interface for any text embedder used by the pipeline."""

    @property
    def dim(self) -> int:
        """Dimensionality of the produced vectors."""
        ...

    def embed(self, texts: list[str]) -> "np.ndarray":
        """Embed ``texts`` into a ``(len(texts), dim)`` float32, L2-normalized matrix."""
        ...


class Qwen3Embedder:
    """Concrete embedder backed by ``sentence-transformers`` (lazy load).

    The ``sentence_transformers`` import and the model load are deferred until
    the first :meth:`embed` call, so constructing the object — or merely
    importing this module — performs no network access and no model download.
    """

    _MAX_CHARS = 4096

    def __init__(self, model_name: str = "Qwen3-Embedding-0.6B", dim: int = 1024) -> None:
        self.model_name = model_name
        self._dim = dim
        self._model = None  # lazily instantiated SentenceTransformer

    @property
    def dim(self) -> int:
        return self._dim

    def _ensure_model(self) -> None:
        if self._model is None:
            # Lazy, local import: only paid when an actual embedding is requested.
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.model_name)

    def embed(self, texts: list[str]) -> "np.ndarray":
        if len(texts) == 0:
            return np.zeros((0, self._dim), dtype=np.float32)
        self._ensure_model()
        truncated = [t[:self._MAX_CHARS] for t in texts]
        vectors = self._model.encode(
            truncated,
            convert_to_numpy=True,
            normalize_embeddings=True,
            batch_size=32,
        )
        vectors = np.asarray(vectors, dtype=np.float32)
        # Re-normalize defensively in case the backend skipped it for any text.
        return _l2_normalize(vectors)


class StubEmbedder:
    """Deterministic, network-free embedder for tests.

    Each text is hashed (SHA-256) to seed a NumPy ``default_rng``; the resulting
    vector is therefore a pure function of the text. Identical texts produce
    bit-identical vectors, so clustering / matching behavior in tests is fully
    reproducible without any model or network access.
    """

    def __init__(self, dim: int = 16) -> None:
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    def _embed_one(self, text: str) -> np.ndarray:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        seed = int.from_bytes(digest[:8], "big", signed=False)
        rng = np.random.default_rng(seed)
        return rng.standard_normal(self._dim).astype(np.float32)

    def embed(self, texts: list[str]) -> "np.ndarray":
        if len(texts) == 0:
            return np.zeros((0, self._dim), dtype=np.float32)
        matrix = np.stack([self._embed_one(t) for t in texts]).astype(np.float32)
        return _l2_normalize(matrix)


# ──────────────────────────────────────────────────────────────────────────
# Vector index — faiss IndexFlatIP (the designated persistent index)
# ──────────────────────────────────────────────────────────────────────────
# Design decision (lead): the production index backend is faiss-cpu, single
# code path — NO numpy fallback inside the index, to avoid an untested
# production/test divergence. faiss is REQUIRED to instantiate a VectorIndex.
#
# Note: Phase-4 in-memory similarity (incremental pattern matching, counterpart
# pairing, duplicate merge) does NOT use VectorIndex — it uses the exact numpy
# :func:`cosine_similarity` primitive directly (the design's "exact search is
# sufficient at ~300-5000 vectors", §8.2). That is the single similarity path
# exercised in both tests and production. VectorIndex is the persistent
# inner-product index for Phase-5 negative-archive recall and other large /
# reused-query retrieval, where building a faiss index pays off.
def _import_faiss():
    """Import and return faiss, or raise a clear, actionable error."""
    try:
        import faiss  # type: ignore

        return faiss
    except Exception as exc:  # noqa: BLE001
        raise ImportError(
            "VectorIndex requires faiss-cpu (the production vector index). "
            "Install it with `pip install faiss-cpu`. (Phase-4 in-memory "
            "similarity does not need it — use cosine_similarity instead.)"
        ) from exc


class VectorIndex:
    """Exact cosine-similarity index over L2-normalized vectors, backed by faiss.

    With normalized vectors, cosine similarity == inner product, so faiss
    ``IndexFlatIP`` is exact. A parallel id list maps row positions back to the
    caller's string ids. Requires faiss-cpu (no numpy fallback by design).
    """

    def __init__(self, dim: int) -> None:
        self._dim = int(dim)
        self._ids: list[str] = []
        self._faiss = _import_faiss()
        self._backend = "faiss"
        self._index = self._faiss.IndexFlatIP(self._dim)

    @property
    def backend(self) -> str:
        return self._backend

    @property
    def dim(self) -> int:
        return self._dim

    def __len__(self) -> int:
        return len(self._ids)

    def add(self, vectors: "np.ndarray", ids: list[str]) -> None:
        vectors = np.asarray(vectors, dtype=np.float32)
        if vectors.ndim == 1:
            vectors = vectors.reshape(1, -1)
        if vectors.shape[0] == 0:
            return
        if vectors.shape[1] != self._dim:
            raise ValueError(
                f"vector dim {vectors.shape[1]} != index dim {self._dim}"
            )
        if len(ids) != vectors.shape[0]:
            raise ValueError(
                f"#ids ({len(ids)}) != #vectors ({vectors.shape[0]})"
            )
        # Ensure normalization so inner product == cosine.
        vectors = _l2_normalize(vectors)
        self._index.add(vectors)
        self._ids.extend(ids)

    def search(self, query: "np.ndarray", top_k: int) -> list[tuple[str, float]]:
        if len(self._ids) == 0 or top_k <= 0:
            return []
        query = np.asarray(query, dtype=np.float32)
        if query.ndim == 1:
            query = query.reshape(1, -1)
        query = _l2_normalize(query)
        k = min(top_k, len(self._ids))
        scores, idxs = self._index.search(query, k)
        results: list[tuple[str, float]] = []
        for score, idx in zip(scores[0], idxs[0]):
            if idx < 0:
                continue
            results.append((self._ids[int(idx)], float(score)))
        return results


def build_index(dim: int) -> "VectorIndex":
    """Construct a faiss-backed :class:`VectorIndex` (requires faiss-cpu).

    This is the persistent inner-product index for Phase-5 negative-archive
    recall and large-scale retrieval. Phase-4 in-memory matching uses
    :func:`cosine_similarity` directly and does not call this.
    """
    return VectorIndex(dim)


def cosine_similarity(a: "np.ndarray", b: "np.ndarray") -> float:
    """Cosine similarity between two 1-D vectors."""
    a = np.asarray(a, dtype=np.float32).reshape(-1)
    b = np.asarray(b, dtype=np.float32).reshape(-1)
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom < _EPS:
        return 0.0
    return float(np.dot(a, b) / denom)
