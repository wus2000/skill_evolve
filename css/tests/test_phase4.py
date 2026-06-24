"""Phase 4 smoke tests: the Analysis Pipeline (Layer 1-3).

Runnable two ways:
    python -m css.tests.test_phase4      # standalone, prints PASS/FAIL summary
    pytest css/tests/test_phase4.py      # standard collection

Every test is deterministic and stub-based — no network, no LLM API, no
SpreadsheetBench dataset, no faiss, and no sentence-transformers model download.

  * Embeddings are produced by :class:`StubEmbedder` (a hash-seeded, L2-normalized
    deterministic double); the real :class:`Qwen3Embedder` is never instantiated.
  * The :class:`VectorIndex` runs its numpy exact-cosine fallback because faiss is
    absent in this environment.
  * The optimizer LLM is replaced by :class:`StubLLMClient` whose ``optimizer_fn``
    returns canned observation / cluster / counterpart JSON.
  * ``sklearn``'s DBSCAN is exercised for real (it is installed) on stub vectors
    crafted so the cluster structure is unambiguous.
"""
from __future__ import annotations

import json

import numpy as np

from css.analysis.cluster import (
    build_or_update_library,
    dbscan_cluster,
    embed_observations,
    incremental_match,
)
from css.analysis.embedding import (
    StubEmbedder,
    VectorIndex,
    build_index,
    cosine_similarity,
)
from css.analysis.layer1 import (
    annotate_contrastive_pair,
    annotate_trajectory,
    run_layer1,
)
from css.analysis.longitudinal import detect_l1_signals, record_epoch_occurrences
from css.analysis.pipeline import AnalysisResult, run_analysis_epoch
from css.config import CSSConfig
from css.data.pattern import Observation, OccurrencePoint, PatternLibrary, PatternRecord
from css.data.rollout import TaskResult, TaskRolloutGroup
from css.data.tree import TreeNode
from css.model.client import StubLLMClient


# ── Helpers ───────────────────────────────────────────────────────────────────


def _result(task_id: str, rollout_index: int, hard: int) -> TaskResult:
    """A TaskResult with a minimal but non-empty trajectory."""
    return TaskResult(
        task_id=task_id,
        rollout_index=rollout_index,
        hard=hard,
        soft=float(hard),
        n_cases=1,
        n_pass=hard,
        fail_reason="" if hard else "wrong answer",
        task_description=f"do task {task_id}",
        node_id="n0",
        epoch=0,
        messages=[
            {"role": "user", "content": f"please solve {task_id}"},
            {"role": "assistant", "content": "I will plan then act, then verify."},
        ],
    )


def _obs(
    oid: str,
    task_id: str,
    aspect: str,
    what: str,
    *,
    polarity: str = "failure",
    epoch: int = 0,
    significance: str = "critical",
) -> Observation:
    return Observation(
        obs_id=oid,
        task_id=task_id,
        rollout_index=0,
        node_id="n0",
        epoch=epoch,
        what=what,
        cognitive_aspect=aspect,
        evidence="quoted evidence",
        consequence="led to outcome",
        significance=significance,  # type: ignore[arg-type]
        polarity=polarity,  # type: ignore[arg-type]
    )


_OBS_JSON = json.dumps(
    [
        {
            "what": "committed to the first interpretation without re-reading",
            "cognitive_aspect": "premature-commitment",
            "evidence": "stated 'this must be column B' before inspecting the sheet",
            "consequence": "computed against the wrong column",
            "significance": "critical",
        },
        {
            "what": "did not verify its own output before finishing",
            "cognitive_aspect": "skipped-self-verification",
            "evidence": "submitted without re-checking the formula",
            "consequence": "an avoidable arithmetic slip survived",
            "significance": "notable",
        },
    ]
)

_DIVERGENCE_JSON = json.dumps(
    {
        "divergence_point": "after reading the prompt, before the first tool call",
        "cognitive_difference": (
            "the success run enumerated assumptions and checked them; the failure "
            "run committed to one reading immediately"
        ),
        "is_systematic": True,
    }
)


def _refine_optimizer_fn(system: str, user: str) -> str:
    """Route the canned JSON by which Layer-2 prompt is being answered."""
    low = system.lower()
    if "unify" in low or "unify" in user.lower():
        return json.dumps(
            {
                "name": "premature commitment",
                "description": "commits to one reading before gathering evidence",
                "cognitive_aspect": "premature-commitment",
                "polarity": "failure",
            }
        )
    if "pair each failure" in low or "counterpart" in low:
        return json.dumps({"pairs": []})
    return "{}"


# ── 1. embedding + vector index ────────────────────────────────────────────────


def test_stub_embedder_deterministic_and_normalized():
    e = StubEmbedder(dim=16)
    assert e.dim == 16
    v = e.embed(["hello world", "hello world", "a different thing"])
    assert v.shape == (3, 16)
    assert v.dtype == np.float32
    # Same text -> identical vector.
    assert np.allclose(v[0], v[1])
    # Different text -> different vector.
    assert not np.allclose(v[0], v[2])
    # Every row is L2-normalized.
    norms = np.linalg.norm(v, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-5)
    # Empty input -> well-shaped empty matrix.
    empty = e.embed([])
    assert empty.shape == (0, 16)


def _faiss_available() -> bool:
    import importlib.util
    return importlib.util.find_spec("faiss") is not None


def test_vector_index_requires_faiss_or_works_with_it():
    # Design (lead): VectorIndex is faiss-only — no numpy fallback. When faiss is
    # absent it must raise a clear, actionable error rather than silently using a
    # divergent backend. When present, it does exact cosine via IndexFlatIP.
    if not _faiss_available():
        raised = False
        try:
            build_index(16)
        except ImportError as exc:
            raised = True
            assert "faiss" in str(exc).lower()
        assert raised, "VectorIndex must raise ImportError when faiss is absent"
        return

    idx = build_index(16)
    assert isinstance(idx, VectorIndex)
    assert idx.backend == "faiss"
    e = StubEmbedder(dim=16)
    vecs = e.embed(["alpha pattern", "beta pattern", "gamma pattern"])
    idx.add(vecs, ["alpha", "beta", "gamma"])
    hits = idx.search(vecs[1], top_k=2)
    assert len(hits) == 2
    assert hits[0][0] == "beta"
    assert hits[0][1] >= hits[1][1]  # cosine scores descending
    assert hits[0][1] > 0.99


def test_cosine_similarity_self_is_one():
    e = StubEmbedder(dim=16)
    v = e.embed(["some cognitive observation text"])[0]
    assert abs(cosine_similarity(v, v) - 1.0) < 1e-4


# ── 2. Layer 1: single-trajectory annotation ───────────────────────────────────


def test_annotate_trajectory_parses_open_ended_observations():
    cfg = CSSConfig()
    client = StubLLMClient(optimizer_fn=lambda s, u: _OBS_JSON)
    result = _result("t1", 0, hard=0)  # a failure
    obs = annotate_trajectory(client, result, cfg=cfg)
    assert len(obs) == 2
    # LLM-named cognitive aspects survive verbatim (no fixed taxonomy).
    aspects = {o.cognitive_aspect for o in obs}
    assert aspects == {"premature-commitment", "skipped-self-verification"}
    first = obs[0]
    # Polarity derives from result.passed (a failure -> "failure").
    assert first.polarity == "failure"
    assert first.evidence  # evidence populated
    assert first.significance == "critical"
    # Stable obs_id form: "{task_id}:r{rollout_index}:{i}".
    assert first.obs_id == "t1:r0:0"
    assert obs[1].obs_id == "t1:r0:1"


def test_annotate_trajectory_success_polarity():
    cfg = CSSConfig()
    client = StubLLMClient(optimizer_fn=lambda s, u: _OBS_JSON)
    obs = annotate_trajectory(client, _result("t2", 0, hard=1), cfg=cfg)
    assert obs and all(o.polarity == "success" for o in obs)


def test_annotate_trajectory_malformed_output_returns_empty():
    cfg = CSSConfig()
    client = StubLLMClient(optimizer_fn=lambda s, u: "not json at all <<<>>>")
    obs = annotate_trajectory(client, _result("t3", 0, hard=0), cfg=cfg)
    assert obs == []  # no crash, no observations


# ── 3. Layer 1: contrastive pair ───────────────────────────────────────────────


def test_annotate_contrastive_pair_parses_divergence():
    cfg = CSSConfig()
    client = StubLLMClient(optimizer_fn=lambda s, u: _DIVERGENCE_JSON)
    success = _result("t4", 0, hard=1)
    failure = _result("t4", 1, hard=0)
    div = annotate_contrastive_pair(client, success, failure, cfg=cfg)
    assert div is not None
    assert div.task_id == "t4"
    assert div.success_rollout_index == 0
    assert div.failure_rollout_index == 1
    assert isinstance(div.is_systematic, bool)
    assert div.is_systematic is True
    assert div.cognitive_difference


def test_annotate_contrastive_pair_malformed_returns_none():
    cfg = CSSConfig()
    client = StubLLMClient(optimizer_fn=lambda s, u: "garbage")
    div = annotate_contrastive_pair(
        client, _result("t5", 0, hard=1), _result("t5", 1, hard=0), cfg=cfg
    )
    assert div is None


def test_run_layer1_stamps_node_and_epoch():
    cfg = CSSConfig()

    def opt(system, user):
        return _DIVERGENCE_JSON if "SUCCESS" in user else _OBS_JSON

    client = StubLLMClient(optimizer_fn=opt)
    group = TaskRolloutGroup(
        task_id="t6",
        rollouts=[_result("t6", 0, hard=1), _result("t6", 1, hard=0)],
    )
    observations, divergences = run_layer1(
        client, [group], node_id="nXYZ", epoch=7, cfg=cfg
    )
    assert observations
    assert all(o.node_id == "nXYZ" and o.epoch == 7 for o in observations)
    # One success + one failure rollout -> exactly one contrastive pair.
    assert len(divergences) == 1


# ── 4. Layer 2a: embedding cache + DBSCAN ───────────────────────────────────────


def test_embed_observations_caches_embedding():
    e = StubEmbedder(dim=16)
    obs = [_obs("o0", "t0", "A", "same text"), _obs("o1", "t1", "B", "other text")]
    vecs = embed_observations(e, obs)
    assert vecs.shape == (2, 16)
    assert obs[0].embedding is not None
    assert len(obs[0].embedding) == 16
    # The cached vector matches the returned matrix row.
    assert np.allclose(np.asarray(obs[0].embedding, dtype=np.float32), vecs[0])


def test_dbscan_groups_duplicates_and_isolates_outlier():
    e = StubEmbedder(dim=16)
    # Two identical-text observations + one clearly different.
    obs = [
        _obs("o0", "t0", "A", "identical observation text"),
        _obs("o1", "t1", "A", "identical observation text"),
        _obs("o2", "t2", "Z", "a completely unrelated cognitive note"),
    ]
    vecs = embed_observations(e, obs)
    labels = dbscan_cluster(vecs, eps=0.05, min_samples=2)
    assert len(labels) == 3
    # The two identical observations share a (non-noise) cluster.
    assert labels[0] == labels[1] and labels[0] >= 0
    # The lone outlier is labeled noise.
    assert labels[2] == -1


def test_dbscan_degenerate_inputs():
    assert dbscan_cluster(np.zeros((0, 16), dtype=np.float32), eps=0.5, min_samples=2) == []
    assert dbscan_cluster(np.ones((1, 16), dtype=np.float32), eps=0.5, min_samples=2) == [-1]


# ── 5. Layer 2: build/update library + incremental match ───────────────────────


def test_build_library_assigns_stable_ids_and_incremental_match_attaches():
    cfg = CSSConfig(eps_dbscan=0.05, min_samples=2)
    client = StubLLMClient(optimizer_fn=_refine_optimizer_fn)
    e = StubEmbedder(dim=16)
    lib = PatternLibrary()

    # Epoch 0: two observations with identical clustering text -> one pattern.
    obs0 = [
        _obs("o0", "t0", "A", "premature commitment to one reading"),
        _obs("o1", "t1", "A", "premature commitment to one reading"),
    ]
    build_or_update_library(client, e, lib, obs0, cfg=cfg)
    assert len(lib) == 1
    rec = lib.active()[0]
    # Stable id of the PatternLibrary.new_pattern_id() form.
    assert rec.pattern_id == "p0000"
    assert rec.centroid is not None
    assert rec.support_count == 2
    # Every member observation carries the assigned pattern_id.
    assert all(o.pattern_id == "p0000" for o in rec.observations)

    # Epoch 1: an observation near the existing pattern attaches via
    # incremental_match — no new pattern is created and support grows.
    obs1 = [_obs("o2", "t2", "A", "premature commitment to one reading", epoch=1)]
    build_or_update_library(client, e, lib, obs1, cfg=cfg)
    assert len(lib) == 1
    rec = lib.active()[0]
    assert rec.support_count == 3
    assert obs1[0].pattern_id == "p0000"


def test_incremental_match_splits_matched_unmatched():
    e = StubEmbedder(dim=16)
    lib = PatternLibrary()
    # Seed a pattern with a centroid from one observation's embedding.
    seed = _obs("s0", "t0", "A", "anchored cognitive behavior text")
    embed_observations(e, [seed])
    rec = PatternRecord(
        pattern_id="p0000",
        name="anchor",
        description="d",
        cognitive_aspect="A",
        polarity="failure",
        observations=[seed],
        centroid=list(seed.embedding),  # type: ignore[arg-type]
    )
    lib.add(rec)

    near = _obs("n0", "t1", "A", "anchored cognitive behavior text")  # identical text
    far = _obs("f0", "t2", "Z", "an entirely different thought process")
    matched, unmatched = incremental_match(e, lib, [near, far], sim_threshold=0.6)
    assert near in matched and far in unmatched
    assert near.pattern_id == "p0000"


def test_incremental_match_is_batch_order_independent():
    # Guards the centroid-freeze fix: matching uses centroids frozen at call
    # start, so the same batch matches identically regardless of order.
    def _setup():
        e = StubEmbedder(dim=16)
        lib = PatternLibrary()
        seed = _obs("s0", "t0", "A", "anchored cognitive behavior text")
        embed_observations(e, [seed])
        lib.add(PatternRecord(
            pattern_id="p0000", name="anchor", cognitive_aspect="A",
            polarity="failure", observations=[seed], centroid=list(seed.embedding),
        ))
        a = _obs("a0", "t1", "A", "anchored cognitive behavior text")
        b = _obs("b0", "t2", "A", "anchored cognitive behavior text")
        return e, lib, a, b

    e1, lib1, a1, b1 = _setup()
    m_fwd, _ = incremental_match(e1, lib1, [a1, b1], sim_threshold=0.6)
    e2, lib2, a2, b2 = _setup()
    m_rev, _ = incremental_match(e2, lib2, [b2, a2], sim_threshold=0.6)
    # Both orders attach both observations to the same pattern.
    assert {o.obs_id for o in m_fwd} == {"a0", "b0"}
    assert {o.obs_id for o in m_rev} == {"a0", "b0"}
    assert a1.pattern_id == b1.pattern_id == "p0000"


# ── 6. Layer 3a: per-epoch occurrence recording ─────────────────────────────────


def test_record_epoch_occurrences_rate_and_trend():
    lib = PatternLibrary()
    rec = PatternRecord(
        pattern_id="p0000",
        name="p",
        description="d",
        cognitive_aspect="A",
        polarity="failure",
    )
    lib.add(rec)
    # Epoch 0: 2 distinct tasks out of 4 -> rate 0.5.
    rec.observations.extend(
        [
            _obs("o0", "ta", "A", "w", epoch=0),
            _obs("o1", "tb", "A", "w", epoch=0),
            _obs("o2", "ta", "A", "w", epoch=0),  # duplicate task_id, not double-counted
        ]
    )
    record_epoch_occurrences(lib, rec.observations, epoch=0, n_tasks=4)
    assert len(rec.occurrence_history) == 1
    pt0 = rec.occurrence_history[-1]
    assert pt0.support_count == 2
    assert abs(pt0.occurrence_rate - 0.5) < 1e-9

    # Epoch 1: 3 distinct tasks out of 4 -> rate 0.75 (rising trend).
    rec.observations.extend(
        [
            _obs("o3", "ta", "A", "w", epoch=1),
            _obs("o4", "tb", "A", "w", epoch=1),
            _obs("o5", "tc", "A", "w", epoch=1),
        ]
    )
    record_epoch_occurrences(lib, rec.observations, epoch=1, n_tasks=4)
    assert len(rec.occurrence_history) == 2
    assert abs(rec.occurrence_history[-1].occurrence_rate - 0.75) < 1e-9
    # Rising occurrence -> positive trend slope.
    assert rec.occurrence_trend(window=10) > 0


# ── 7. Layer 3b: L1-signal detection (reuse Phase-1 predicate) ──────────────────


def _flat_history(rate: float, n: int, epochs_start: int = 0) -> list[OccurrencePoint]:
    return [
        OccurrencePoint(epoch=epochs_start + i, occurrence_rate=rate, support_count=3, n_tasks=10)
        for i in range(n)
    ]


def test_detect_l1_signals_persistent_vs_decaying():
    cfg = CSSConfig()  # W=10, l1_min_task_fraction=0.15
    lib = PatternLibrary()

    # Persistent failure: flat, high occurrence over the window.
    persistent = PatternRecord(
        pattern_id="p0000",
        name="persistent",
        description="d",
        cognitive_aspect="A",
        polarity="failure",
        occurrence_history=_flat_history(0.4, cfg.W),
    )
    # Decaying failure: occurrence falls off (declining trend) — not an L1 signal.
    decaying = PatternRecord(
        pattern_id="p0001",
        name="decaying",
        description="d",
        cognitive_aspect="B",
        polarity="failure",
        occurrence_history=[
            OccurrencePoint(epoch=i, occurrence_rate=0.5 - 0.05 * i, support_count=3, n_tasks=10)
            for i in range(cfg.W)
        ],
    )
    lib.add(persistent)
    lib.add(decaying)

    signals = detect_l1_signals(lib, cfg=cfg, l0_saturated=True)
    sig_ids = {p.pattern_id for p in signals}
    assert "p0000" in sig_ids  # persistent qualifies
    assert "p0001" not in sig_ids  # decaying does not

    # L0 not saturated -> nothing qualifies (criterion b fails).
    assert detect_l1_signals(lib, cfg=cfg, l0_saturated=False) == []


# ── 8. Pipeline: end-to-end node-epoch analysis ─────────────────────────────────


def test_run_analysis_epoch_end_to_end():
    cfg = CSSConfig(eps_dbscan=0.05, min_samples=2)

    def opt(system, user):
        if "SUCCESS" in user and "FAILURE" in user:
            return _DIVERGENCE_JSON
        low = system.lower()
        if "unify" in low or "unify" in user.lower():
            return json.dumps(
                {
                    "name": "premature commitment",
                    "description": "commits before gathering evidence",
                    "cognitive_aspect": "premature-commitment",
                    "polarity": "failure",
                }
            )
        if "pair each failure" in low or "counterpart" in low:
            return json.dumps({"pairs": []})
        return _OBS_JSON

    client = StubLLMClient(optimizer_fn=opt)
    e = StubEmbedder(dim=16)
    node = TreeNode(node_id="n0")

    # Two tasks, each with one failing rollout -> shared cognitive observations.
    groups = [
        TaskRolloutGroup(task_id="t0", rollouts=[_result("t0", 0, hard=0)]),
        TaskRolloutGroup(task_id="t1", rollouts=[_result("t1", 0, hard=0)]),
    ]

    res = run_analysis_epoch(
        client, e, node, groups, epoch=0, l0_saturated=True, cfg=cfg
    )
    assert isinstance(res, AnalysisResult)
    assert res.epoch == 0
    assert res.n_observations > 0
    # The node's pattern library was populated in place.
    assert len(node.pattern_records) > 0
    assert res.n_patterns == len(node.pattern_records)
    # Every active pattern recorded an occurrence point this epoch.
    for p in node.pattern_records.active():
        assert p.occurrence_history and p.occurrence_history[-1].epoch == 0
    # l1_signals is computed (a list of PatternRecord).
    assert isinstance(res.l1_signals, list)

    # Determinism: a second identical run from a fresh node yields the same shape.
    node2 = TreeNode(node_id="n0")
    res2 = run_analysis_epoch(
        client, e, node2, groups, epoch=0, l0_saturated=True, cfg=cfg
    )
    assert res2.n_observations == res.n_observations
    assert res2.n_patterns == res.n_patterns


# ── 9. Import smoke (no faiss / no model download) ──────────────────────────────


def test_import_smoke_no_heavy_backends():
    import importlib
    import sys

    for mod in (
        "css.analysis.embedding",
        "css.analysis.layer1",
        "css.analysis.cluster",
        "css.analysis.longitudinal",
        "css.analysis.pipeline",
    ):
        importlib.import_module(mod)
    # Importing the analysis stack must not pull faiss or load an ST model.
    assert "faiss" not in sys.modules
    # sentence_transformers may be importable, but no model is constructed via
    # the StubEmbedder path used throughout these tests.
    e = StubEmbedder(dim=8)
    assert e.embed(["x"]).shape == (1, 8)
    # Keep referenced symbols live.
    assert all(
        fn is not None
        for fn in (run_analysis_epoch, build_or_update_library, detect_l1_signals)
    )


# ── Standalone runner ──────────────────────────────────────────────────────────


def _run_all():
    import inspect

    fns = [
        obj for name, obj in sorted(globals().items())
        if name.startswith("test_") and inspect.isfunction(obj)
    ]
    passed = 0
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            import traceback
            print(f"  FAIL  {fn.__name__}: {exc}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed (of {len(fns)})")
    return failed == 0


if __name__ == "__main__":
    import sys

    sys.exit(0 if _run_all() else 1)
