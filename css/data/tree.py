"""Search tree data model — nodes and topology.

Each :class:`TreeNode` is one cognitive strategy under investigation. It owns
the strategy text (L1), the accumulated rules text (L0), the L0 step buffer, a
pattern library, and a learning curve. The tree topology encodes the branching
search: ROOT → (PROPOSAL | REFINE) children.

The node carries the in-memory state; physical persistence of strategy.md /
rules.md / metadata.json is handled by :class:`css.skill_document.SkillDocument`
(Phase 1.4), which a node binds to via ``skill_dir``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from css.data.pattern import PatternLibrary
from css.data.step_buffer import StepBuffer

BranchType = Literal["ROOT", "PROPOSAL", "REFINE", "NEW"]
# Node lifecycle (tree-search mechanism, L1_tree_mechanism_design.md §1.2):
#   active    — selected => run a B-step L0 burst
#   saturated — selected => spawn a child (root->NEW, strategy node->REFINE);
#               the node is NOT dead: its results are locked in global_best and
#               its UCB score prices its next child
#   terminal  — out of the selection pool (degree exhausted / REFINE decline)
#   pruned    — legacy (paired-bootstrap prune); kept for old checkpoints
NodeStatus = Literal["active", "pruned", "saturated", "terminal"]


@dataclass
class LearningCurvePoint:
    """One epoch's evaluation snapshot for a node."""

    epoch: int
    n_steps: int                       # cumulative L0 steps at this epoch
    train_score: float = 0.0
    val_score: float = 0.0
    accept_rate: float = 0.0
    accept_slope: float = 0.0

    @classmethod
    def from_dict(cls, d: dict) -> "LearningCurvePoint":
        return cls(
            epoch=int(d.get("epoch", -1)),
            n_steps=int(d.get("n_steps", 0)),
            train_score=float(d.get("train_score", 0.0)),
            val_score=float(d.get("val_score", 0.0)),
            accept_rate=float(d.get("accept_rate", 0.0)),
            accept_slope=float(d.get("accept_slope", 0.0)),
        )

    def to_dict(self) -> dict:
        return {
            "epoch": self.epoch,
            "n_steps": self.n_steps,
            "train_score": self.train_score,
            "val_score": self.val_score,
            "accept_rate": self.accept_rate,
            "accept_slope": self.accept_slope,
        }


@dataclass
class TreeNode:
    """A single cognitive-strategy node in the search tree."""

    node_id: str
    branch_type: BranchType = "ROOT"
    parent_id: str | None = None
    children_ids: list[str] = field(default_factory=list)

    # ── Skill document content (mirrors the on-disk files) ───────────────
    strategy: str = ""                 # strategy.md body (L1)
    rules: str = ""                    # rules.md body (L0)
    skill_dir: str = ""                # directory backing this node's SkillDocument

    # ── Optimization state ───────────────────────────────────────────────
    step_buffer: StepBuffer = field(default_factory=StepBuffer)
    # The node's cognitive pattern library (design §3.1 calls this field
    # ``pattern_records``). Holds a PatternLibrary keyed by stable pattern_id.
    pattern_records: PatternLibrary = field(default_factory=PatternLibrary)
    learning_curve: list[LearningCurvePoint] = field(default_factory=list)

    val_score: float = 0.0             # latest validation-set score (tree comparison)
    train_score: float = 0.0
    best_score: float = 0.0            # best L0 selection-set score seen at this node
    best_step: int = -1
    # The node's INITIAL strategy (+ initial rules) val score, measured ONCE before
    # any exploitation. It is the node's baseline and the gate's first incumbent, so
    # exploitation gain is measured from the initial strategy (not 0 / a train floor).
    # -1.0 = not yet measured.
    baseline_val_score: float = -1.0
    # The rules.md body that achieved best_score. Threaded by EXPLOITATION so the
    # tree manager (Phase 4+) can recover the best snapshot even after `rules`
    # advances past it on an accept-not-best step. "" until a best is recorded.
    best_rules: str = ""

    # Epochs invested in this node (design D2). Incremented per epoch by the
    # orchestrator (Phase 6). Distinct from ``n_steps`` (L0 steps, used by
    # SELECT/PRUNE) and from ``len(learning_curve)`` (periodic eval points).
    maturity: int = 0
    refine_count: int = 0              # REFINE attempts spawned from this node
    status: NodeStatus = "active"

    created_epoch: int = -1

    # ── Tree-search (burst-granular) state ────────────────────────────────
    # Bursts completed at this node. The UCB exploration term runs on bursts
    # (one burst = one visit); spawn+first-burst is atomic, so every node in
    # the selection pool has n_bursts >= 1 (no inf-UCB fresh nodes).
    n_bursts: int = 0
    # Per-burst gated net val movement (reward history; parallels bursts.jsonl
    # on disk). Kept small: floats only.
    burst_rewards: list[float] = field(default_factory=list)
    # Per-burst gate-accept counts. Saturation (user ruling 2026-07-05): the
    # last ``cfg.saturation_dry_bursts`` (=2) bursts all zero-accept => the
    # node is saturated. Any gate accept — best or not — counts as basin
    # yield and resets the dry streak.
    burst_accepts: list[int] = field(default_factory=list)

    # Paired-gate incumbent ledger: per-val-item measurement record of the
    # CURRENT incumbent rules — {item_id: {"passes": int, "trials": int}}.
    # Bootstrapped lazily by the paired gate, folded with escalation rollouts,
    # and reset to the winning candidate's measurements on accept.
    val_ledger: dict = field(default_factory=dict)

    # Spawn-failure cooldown (2026-07-06 spin fix): a saturated node whose
    # spawn produced no child (e.g. novelty-duplicate children) re-enters the
    # selection pool only after the GLOBAL burst count advances past
    # spawn_block_T — with unchanged materials a retry just regenerates the
    # same duplicate, and the AW run burned one decision/minute reselecting
    # its saturated root. spawn_fail_count trips terminal at the cap (3).
    spawn_fail_count: int = 0
    spawn_block_T: int = -1

    # ── Convenience ────────────────────────────────────────────────────────
    @property
    def n_steps(self) -> int:
        return self.step_buffer.n_steps

    @property
    def is_root(self) -> bool:
        return self.branch_type == "ROOT" or self.parent_id is None

    def is_saturated(self, n_threshold: int, stall_threshold: int = 0) -> bool:
        return self.step_buffer.is_saturated(n_threshold, stall_threshold=stall_threshold)

    def degree_exhausted(self, degree_cap: int) -> bool:
        """True when this STRATEGY node has spent its child quota (root: never)."""
        if self.is_root:
            return False
        return len(self.children_ids) >= max(1, degree_cap)

    def accept_slope(self, window: int) -> float:
        return self.step_buffer.accept_slope(window)

    def record_learning_point(self, point: LearningCurvePoint) -> None:
        self.learning_curve.append(point)

    @classmethod
    def from_dict(cls, d: dict) -> "TreeNode":
        return cls(
            node_id=str(d["node_id"]),
            branch_type=d.get("branch_type", "ROOT"),
            parent_id=d.get("parent_id"),
            children_ids=list(d.get("children_ids", [])),
            strategy=str(d.get("strategy", "")),
            rules=str(d.get("rules", "")),
            skill_dir=str(d.get("skill_dir", "")),
            step_buffer=StepBuffer.from_dict(d.get("step_buffer", {})),
            pattern_records=PatternLibrary.from_dict(
                d.get("pattern_records", d.get("pattern_library", {}))
            ),
            learning_curve=[LearningCurvePoint.from_dict(p) for p in d.get("learning_curve", [])],
            val_score=float(d.get("val_score", 0.0)),
            train_score=float(d.get("train_score", 0.0)),
            best_score=float(d.get("best_score", 0.0)),
            best_step=int(d.get("best_step", -1)),
            baseline_val_score=float(d.get("baseline_val_score", -1.0)),
            best_rules=str(d.get("best_rules", "")),
            maturity=int(d.get("maturity", 0)),
            refine_count=int(d.get("refine_count", 0)),
            status=d.get("status", "active"),
            created_epoch=int(d.get("created_epoch", -1)),
            val_ledger=dict(d.get("val_ledger", {})),
            n_bursts=int(d.get("n_bursts", 0)),
            burst_rewards=[float(x) for x in d.get("burst_rewards", [])],
            burst_accepts=[int(x) for x in d.get("burst_accepts", [])],
            spawn_fail_count=int(d.get("spawn_fail_count", 0)),
            spawn_block_T=int(d.get("spawn_block_T", -1)),
        )

    def to_dict(self, include_embeddings: bool = False) -> dict:
        return {
            "node_id": self.node_id,
            "branch_type": self.branch_type,
            "parent_id": self.parent_id,
            "children_ids": self.children_ids,
            "strategy": self.strategy,
            "rules": self.rules,
            "skill_dir": self.skill_dir,
            "step_buffer": self.step_buffer.to_dict(),
            "pattern_records": self.pattern_records.to_dict(include_embeddings),
            "learning_curve": [p.to_dict() for p in self.learning_curve],
            "val_score": self.val_score,
            "train_score": self.train_score,
            "best_score": self.best_score,
            "best_step": self.best_step,
            "baseline_val_score": self.baseline_val_score,
            "best_rules": self.best_rules,
            "maturity": self.maturity,
            "refine_count": self.refine_count,
            "status": self.status,
            "created_epoch": self.created_epoch,
            "val_ledger": self.val_ledger,
            "n_bursts": self.n_bursts,
            "burst_rewards": self.burst_rewards,
            "burst_accepts": self.burst_accepts,
            "spawn_fail_count": self.spawn_fail_count,
            "spawn_block_T": self.spawn_block_T,
        }


@dataclass
class SearchTree:
    """Container + topology operations over :class:`TreeNode` objects."""

    nodes: dict[str, TreeNode] = field(default_factory=dict)
    root_id: str | None = None
    _next_id: int = 0

    def __len__(self) -> int:
        return len(self.nodes)

    def __contains__(self, node_id: str) -> bool:
        return node_id in self.nodes

    def get(self, node_id: str) -> TreeNode | None:
        return self.nodes.get(node_id)

    def new_node_id(self) -> str:
        nid = f"n{self._next_id:04d}"
        self._next_id += 1
        return nid

    def add_root(self, node: TreeNode) -> TreeNode:
        node.branch_type = "ROOT"
        node.parent_id = None
        self.nodes[node.node_id] = node
        self.root_id = node.node_id
        return node

    def add_child(self, parent_id: str, child: TreeNode) -> TreeNode:
        parent = self.nodes[parent_id]
        child.parent_id = parent_id
        self.nodes[child.node_id] = child
        if child.node_id not in parent.children_ids:
            parent.children_ids.append(child.node_id)
        return child

    # ── Queries ────────────────────────────────────────────────────────────
    def children(self, node_id: str) -> list[TreeNode]:
        node = self.nodes.get(node_id)
        if node is None:
            return []
        return [self.nodes[c] for c in node.children_ids if c in self.nodes]

    def siblings(self, node_id: str) -> list[TreeNode]:
        node = self.nodes.get(node_id)
        if node is None or node.parent_id is None:
            return []
        return [
            self.nodes[c]
            for c in self.nodes[node.parent_id].children_ids
            if c != node_id and c in self.nodes
        ]

    def active_nodes(self) -> list[TreeNode]:
        return [n for n in self.nodes.values() if n.status == "active"]

    def selectable_nodes(self) -> list[TreeNode]:
        """Tree-search selection pool: ACTIVE (will burst) + SATURATED (will spawn).

        TERMINAL / pruned nodes are excluded. Degree exhaustion is handled by
        the loop at transition time (a saturated strategy node whose quota is
        spent is flipped to terminal before the next selection). A saturated
        node under spawn-failure cooldown (spawn_block_T >= current global
        bursts) sits out until any burst lands somewhere — retrying a spawn on
        unchanged materials only regenerates the duplicate child.
        """
        total_t = sum(n.n_bursts for n in self.nodes.values())
        return [
            n for n in self.nodes.values()
            if n.status == "active"
            or (n.status == "saturated" and n.spawn_block_T < total_t)
        ]

    def best_node(self, metric: str = "val_score") -> TreeNode | None:
        if not self.nodes:
            return None
        return max(self.nodes.values(), key=lambda n: getattr(n, metric, 0.0))

    def path_to_root(self, node_id: str) -> list[TreeNode]:
        """Node → ... → root (inclusive), in child-to-ancestor order."""
        path: list[TreeNode] = []
        cur = self.nodes.get(node_id)
        while cur is not None:
            path.append(cur)
            cur = self.nodes.get(cur.parent_id) if cur.parent_id else None
        return path

    @classmethod
    def from_dict(cls, d: dict) -> "SearchTree":
        tree = cls(root_id=d.get("root_id"), _next_id=int(d.get("_next_id", 0)))
        for nd in d.get("nodes", []):
            node = TreeNode.from_dict(nd)
            tree.nodes[node.node_id] = node
        return tree

    def to_dict(self, include_embeddings: bool = False) -> dict[str, Any]:
        return {
            "root_id": self.root_id,
            "_next_id": self._next_id,
            "nodes": [n.to_dict(include_embeddings) for n in self.nodes.values()],
        }
