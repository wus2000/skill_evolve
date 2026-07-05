"""Production spawner: mode dispatch, unknown-mode handling, exception containment."""
from __future__ import annotations

from css.data.tree import SearchTree, TreeNode
from css.l1gen import make_spawner
from css.tree_search import SpawnContext, SpawnOutcome


def _ctx(mode="NEW", new_id="n0001") -> SpawnContext:
    tree = SearchTree()
    root = TreeNode(node_id="n0000", branch_type="ROOT")
    tree.add_root(root)
    return SpawnContext(tree=tree, parent=root, mode=mode, new_node_id=new_id, cfg=None,
                        env=None, target_client=None, optimizer_client=None,
                        out_dir="/tmp/unused", decision_index=0, ledger=None)


def test_dispatch_new(monkeypatch):
    sentinel = SpawnOutcome(child=TreeNode(node_id="n0001"), mode="NEW")
    monkeypatch.setattr("css.l1gen.new_pipeline.run_new_pipeline", lambda ctx: sentinel)
    out = make_spawner()(_ctx("NEW"))
    assert out is sentinel


def test_dispatch_refine(monkeypatch):
    sentinel = SpawnOutcome(child=TreeNode(node_id="n0002"), mode="REFINE")
    monkeypatch.setattr("css.l1gen.refine_pipeline.run_refine_pipeline", lambda ctx: sentinel)
    out = make_spawner()(_ctx("REFINE", new_id="n0002"))
    assert out is sentinel


def test_unknown_mode_is_a_non_decline_failure():
    out = make_spawner()(_ctx("WAT"))
    assert out.child is None and out.decline is False
    assert "unknown spawn mode" in out.reason


def test_pipeline_crash_is_contained(monkeypatch):
    def boom(ctx):
        raise ValueError("synthetic pipeline explosion")
    monkeypatch.setattr("css.l1gen.new_pipeline.run_new_pipeline", boom)
    out = make_spawner()(_ctx("NEW"))
    # A crash must never propagate: it becomes a non-decline failure (parent stays
    # saturated, retryable) — never a spurious TERMINAL.
    assert out.child is None and out.decline is False
    assert "crashed" in out.reason and "synthetic pipeline explosion" in out.reason
