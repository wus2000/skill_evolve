"""Global configuration for CSS (Cognitive Strategy Search).

A single dataclass holds the parameter table from design §10 / D13, plus paths,
data-split sizes, embedding settings, and LLM/runtime knobs. All values are the
documented initial values; the design states they are calibrated via pilot
experiments, so every field is overridable and the config round-trips to JSON.

Field groups:
  * Saturation / branching:      N, W, K, remedy_threshold
  * Tree management:             min_steps, alpha, beta, prune_*
  * Data:                        k_rollouts, split sizes, held_out_rotation_T
  * Context / truncation:        context_cap, context_use_frac, tool_trunc
  * Analysis / clustering:       embedding_model, eps_dbscan, min_samples, ...
  * Layer 5 thresholds:          coverage_low, coverage_high, neg_archive_top_k
  * Runtime:                     paths, concurrency, seeds, LLM model names
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from typing import Any


@dataclass
class CSSConfig:
    # ── Saturation & branching (design D7 / D13) ─────────────────────────
    N: int = 5                          # consecutive-reject saturation threshold
    W: int = 10                         # accept-rate trend window (= 2N)
    K: int = 3                          # REFINE→PROPOSAL escalation count
    remedy_threshold: int = 3           # remedy_resistance needed for L1 signal

    # ── Tree management: SELECT / PRUNE (design D11 / D13) ───────────────
    min_steps: int = 10                 # PRUNE minimum investment
    alpha: float = 0.5                  # SELECT accept_slope weight (pilot-tuned)
    beta: float = 0.5                   # SELECT exploration weight (pilot-tuned)
    prune_bootstrap_resamples: int = 1000
    prune_ci: float = 0.95              # confidence level for paired bootstrap

    # ── Data usage (design D5 / §9) ──────────────────────────────────────
    k_rollouts: int = 3                 # rollouts per task (K=3)
    n_train: int = 80
    n_val: int = 40
    n_test: int = 20
    held_out_rotation_T: int = 0        # re-split every T epochs (0 = never)

    # ── Context & truncation (design D7) ─────────────────────────────────
    context_cap: int = 256_000          # global context window (tokens)
    context_use_frac: float = 0.80      # effective threshold = cap * frac
    tool_trunc: int = 8_000             # truncate single tool result >= this

    # ── L0 EXPLOITATION ──────────────────────────────────────────────────
    minibatch_size: int = 8             # trajectories per L0 minibatch
    max_l0_steps_per_epoch: int = 20    # safety cap on L0 steps within an epoch
    batch_size: int = 40                # tasks per batch in batch-step architecture
    num_generators: int = 3             # deprecated (plan_a v1 N-generator stage); unused by per-minibatch plan_a
    l0_edit_budget: int = 3             # max edits each minibatch proposer may emit (L)
    max_edits_per_step: int = 6         # max edits applied per step after the merge coordinator
    exploitation_val_k: int = 1          # K for val gate in exploitation (1=fast; train K stays at k_rollouts)
    test_k_rollouts: int = 1             # K for test-set evaluation (bare baseline + per-round test)
    merger_inject_history: bool = True   # Inject per-edit verification history into merger prompt
    merger_history_window: int = 3       # Max recent steps of edit verification history shown to merger
    rules_max_chars: int = 60_000       # Soft cap for rules.md (log warning, no truncation)

    # ── Dataset-size subsets (knob >= split size OR <= 0  =>  use the WHOLE set;
    #    only when 0 < knob < split size is the set subsampled). Lets a large
    #    dataset be loaded in full (n_train/n_val/n_test) while keeping each
    #    expensive operation cheap. ─────────────────────────────────────────
    coldstart_train_size: int = 0       # cold-start bare-rollout train subset (uniform)
    exploitation_val_size: int = 0      # RUN-FIXED val subset: baseline + L0 gate + node val_score
                                        # (same tasks for every node so val_scores stay comparable)
    analysis_train_size: int = 0        # post-exploitation analysis train-rollout subset
    # Difficulty-weighted analysis sampling proportions (used once the global task
    # ledger has data; uniform fallback otherwise). Normalized; need not sum to 1.
    analysis_frac_frontier: float = 0.45  # mixed (0<solve_rate<1): contrastive-pair gold
    analysis_frac_hard: float = 0.30      # learnable-hard (solve_rate~0, ever-solved / not stuck)
    analysis_frac_flipped: float = 0.15   # recently cracked or regressed (freshest signal)
    analysis_frac_mastered: float = 0.10  # solved (regression watch)
    analysis_ceiling_rounds: int = 4      # unsolved for >= this many rounds => likely
                                          # capability ceiling; down-weighted in analysis

    # ── L1 STRATEGY CYCLE (diverse-iterate + objective lift selection) ────
    max_l1_iterations: int = 8          # max diverse-iterate rounds (hard cap)
    l1_target_effective: int = 3        # stop once this many EFFECTIVE (lift>0) strategies collected
    l1_diagnostic_tasks: int = 24       # R: residual tasks (baseline 0/K); 24 unless fewer available
    l1_regression_tasks: int = 12       # G: robustly-passing tasks (baseline K/K) — harm guard
    l1_diagnosis_per_category: int = 5  # max per-task trajectory analyses per category (cracked/regressed/still)

    # ── Reflect mode ─────────────────────────────────────────────────────
    # "legacy"  — original flat fail/success split (no per-task grouping)
    # "plan_a"  — three-way analysis → unified edit_generator (two-stage)
    # "plan_b"  — success insights as context injection into fail/contrastive
    reflect_mode: str = "plan_a"

    # ── Analysis / clustering (design D8 / §8) ───────────────────────────
    embedding_model: str = "Qwen3-Embedding-0.6B"
    embedding_dim: int = 1024
    eps_dbscan: float = 0.0             # 0.0 = adaptive (k-distance elbow)
    min_samples: int = 2                # DBSCAN minimum cluster size
    l1_min_task_fraction: float = 0.15  # deprecated: L1 now triggers on L0 saturation

    # ── Layer 5 thresholds (design D4 / D12) ─────────────────────────────
    coverage_low: float = 0.20          # < this → reconsider root cause
    coverage_high: float = 0.30         # > this → proceed to rollout
    neg_archive_top_k: int = 5          # negative-archive recall size

    # ── Runtime ──────────────────────────────────────────────────────────
    seed: int = 42
    concurrency_limit: int = 4          # parallel tree nodes per round
    max_api_workers: int = 32           # parallel task rollouts
    task_timeout_s: int = 600           # whole-rollout (multi-turn) wall-clock budget
    bash_timeout_s: int = 180           # per single bash command (3 min); decoupled
                                        # from task_timeout_s so one hung command
                                        # (catastrophic regex / infinite loop) is
                                        # killed fast instead of stalling the run.
    max_turns: int = 30                 # multi-turn conversation limit per rollout

    # LLM model names (dependency-injected; see Phase 2/3 LLM client).
    target_model: str = "claude-sonnet-4-6"   # frozen task agent
    optimizer_model: str = "claude-sonnet-4-6" # L0/L1 optimizer + analysts

    # ── Environment ──────────────────────────────────────────────────────
    env_name: str = "spreadsheetbench"  # which TaskEnv to run; selects the env
                                        # implementation + its data adapter via
                                        # css.envs.registry.build_env

    # ── Paths ────────────────────────────────────────────────────────────
    out_root: str = "runs/css_default"
    data_root: str = ""                 # raw task data root (env-specific layout)
    split_dir: str = ""                 # existing train/val/test split dir
    data_path: str = ""                 # raw dataset for ratio splitting

    # Free-form overflow for experiment-specific knobs without schema churn.
    extra: dict[str, Any] = field(default_factory=dict)

    # ── Derived ──────────────────────────────────────────────────────────
    @property
    def effective_context_threshold(self) -> int:
        """Actual token budget before trajectory trimming kicks in."""
        return int(self.context_cap * self.context_use_frac)

    # ── Validation ─────────────────────────────────────────────────────────
    def validate(self) -> None:
        """Raise ``ValueError`` on inconsistent parameters."""
        problems: list[str] = []
        if self.N < 1:
            problems.append("N must be >= 1")
        if self.W < self.N:
            problems.append("W should be >= N (trend window covers saturation length)")
        if self.K < 1:
            problems.append("K must be >= 1")
        if self.min_steps < 1:
            problems.append("min_steps must be >= 1")
        if not (0.0 < self.context_use_frac <= 1.0):
            problems.append("context_use_frac must be in (0, 1]")
        if self.k_rollouts < 1:
            problems.append("k_rollouts must be >= 1")
        if self.max_l1_iterations < 1:
            problems.append("max_l1_iterations must be >= 1")
        if self.l1_diagnostic_tasks < 1:
            problems.append("l1_diagnostic_tasks must be >= 1")
        if self.l1_regression_tasks < 0:
            problems.append("l1_regression_tasks must be >= 0")
        if self.min_samples < 1:
            problems.append("min_samples must be >= 1")
        if not (0.0 <= self.coverage_low <= self.coverage_high <= 1.0):
            problems.append("require 0 <= coverage_low <= coverage_high <= 1")
        if not (0.0 < self.prune_ci < 1.0):
            problems.append("prune_ci must be in (0, 1)")
        if problems:
            raise ValueError("Invalid CSSConfig:\n  - " + "\n  - ".join(problems))

    # ── Serialization ────────────────────────────────────────────────────
    @classmethod
    def from_dict(cls, d: dict) -> "CSSConfig":
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in d.items() if k in known}
        extra = {k: v for k, v in d.items() if k not in known}
        cfg = cls(**kwargs)
        if extra:
            cfg.extra.update(extra)
        return cfg

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json_file(cls, path: str) -> "CSSConfig":
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f))

    def to_json_file(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
