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
    alpha: float = 0.5                  # DEPRECATED (L1_actions_redesign §2): the
                                        # SELECT slope term is deleted — it punished
                                        # newborn cold-start settling and rewarded
                                        # basin-top churn. Field kept for old-config
                                        # compatibility; unused.
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
    min_l0_epochs: int = 1             # full train-set passes before saturation check activates (0 = no epoch floor)
    max_l0_epochs: int = 100           # hard cap on train-set passes (effectively unlimited)
    max_l0_steps: int = 0              # absolute per-round L0 step budget (0 = epoch caps only)
    l0_stall_steps: int = 8            # LEGACY (epoch-mode saturation only): consecutive steps
                                       # without accept_new_best. The tree mechanism judges
                                       # saturation by saturation_dry_bursts instead.
    batch_size: int = 40                # tasks per batch in batch-step architecture
    num_generators: int = 3             # deprecated (plan_a v1 N-generator stage); unused by per-minibatch plan_a
    l0_edit_budget: int = 3             # per-minibatch edit guideline (L) injected into the
                                        # proposer prompt ("AT MOST L"); NOT mechanically
                                        # enforced — over-budget proposals are kept and the
                                        # merger de-duplicates with an unused-signal ledger
    max_edits_per_step: int = 0          # deprecated — merger now self-determines edit count
    exploitation_val_k: int = 1          # K for val gate in exploitation (1=fast; train K stays at k_rollouts)
    test_k_rollouts: int = 1             # K for test-set evaluation (bare baseline + per-round test)
    merger_inject_history: bool = True   # Inject per-edit verification history into merger prompt
    merger_history_window: int = 3       # Max recent steps of edit verification history shown to merger
    merger_granularity: str = "point"    # "point" (fine-grained point edits) | "section" (one edit per section)
    edit_pipeline: str = "v2"            # "v3" = plan/draft/review/apply consolidation
                                         #   (single-direction, group-level verify —
                                         #   docs/editpipe_v3_design.md)
                                         # | "v2" = editpipe merge+adjudicate (validated on
                                         #   real step replays, docs/edit_pipeline_v2.md;
                                         #   kept as the fallback during the v3 transition)
                                         # | "legacy" = aggregate merger + whole-doc apply
    rules_max_chars: int = 60_000       # Soft cap for rules.md (log warning, no truncation)

    # ── Per-edit ablation verification (signal floor + pass criterion) ────
    verify_floor_divisor: int = 8        # verification set floor = batch_size // this
                                         # (pad with random batch control tasks — they double
                                         # as regression detectors outside the edit's targets)
    verify_floor_tasks: int = 0          # ABSOLUTE verification floor (tasks per edit);
                                         # 0 = derive from verify_floor_divisor. Takes
                                         # precedence when > 0. Not part of the resume
                                         # fingerprint (reporting/measurement knob).
    verify_min_net_flips: int = 2        # continuous tiebreaker requires >= this many net
                                         # rollout flips (single-flip passes are noise)
    verify_mode: str = "harm_veto"       # "harm_veto" (fix 2026-07-05: the 5-8-task probe
                                         # only VETOES measured net harm; unprovable edits
                                         # join the candidate and the paired gate arbitrates
                                         # — 79-83% of full-mode kills were null/no-effect)
                                         # | "full" (legacy per-edit admission gate)

    # ── L0 val gate (paired sign-test vs legacy mean comparison) ──────────
    gate_mode: str = "paired"            # "paired" (two-stage item-paired sign test) | "mean"
    gate_paired_alpha: float = 0.1       # one-sided binomial significance to ACCEPT
    gate_screen_k: int = 1               # stage-1 screen rollouts per val item (candidate side)
    gate_escalation_k: int = 3           # stage-2 fresh rollouts PER SIDE per round on discordant items
    gate_max_escalation_rounds: int = 3  # adaptive deepening: extra stage-2 rounds while the
                                         # permutation p sits in the ambiguous band (alpha, 0.5]
                                         # — small val sets buy DEPTH where width is capped
    gate_shadow_log: bool = True         # paired mode also logs the counterfactual mean decision

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
    # Analysis prompt-shape flags (per-env launcher opt-in; False preserves the
    # legacy prompt bytes so runs already in flight stay on their own protocol).
    json_list_wrap: bool = False        # wrap list-shaped optimizer outputs in a single
                                        # JSON object ({"observations": [...]} etc.):
                                        # response_format={"type":"json_object"} grammar-
                                        # forbids a top-level array, which silently
                                        # flattened "output a JSON list" prompts to
                                        # exactly 1 element per call (measured 3000/3000
                                        # ALFWorld + 3300/3300 Bird trajectories)
    analysis_env_context: bool = False  # inject env.action_space_description() into
                                        # Layer-1 annotate/contrastive prompts so the
                                        # analyzer knows the environment's semantics
                                        # (e.g. auto-termination vs self-declaration)

    # ── L1 STRATEGY CYCLE (diverse-iterate + objective lift selection) ────
    # LEGACY (v3 eight-round exam) — retained only while the old proposal cycle
    # code exists; the tree-search mechanism below supersedes it.
    max_l1_iterations: int = 8          # max diverse-iterate rounds (hard cap)
    l1_target_effective: int = 3        # stop once this many EFFECTIVE (lift>0) strategies collected
    l1_diagnostic_tasks: int = 24       # R: residual tasks (baseline 0/K); 24 unless fewer available
    l1_regression_tasks: int = 12       # G: robustly-passing tasks (baseline K/K) — harm guard
    l1_diagnosis_per_category: int = 5  # max per-task trajectory analyses per category (cracked/regressed/still)

    # ── L1 TREE SEARCH (burst-granular; L1_tree_mechanism_design.md) ──────
    # Node three-state machine: ACTIVE (selected -> B-step L0 burst),
    # SATURATED (selected -> spawn child + child's first burst, atomic),
    # TERMINAL (out of the pool). Saturation (user ruling 2026-07-05,
    # supersedes the cross-burst stall counter): the last
    # ``saturation_dry_bursts`` bursts were ALL zero-accept.
    burst_steps: int = 5                # B: L0 steps per tree visit (one burst)
    saturation_dry_bursts: int = 2      # consecutive all-reject bursts => saturated
    node_degree: int = 3                # max children per STRATEGY node (root unlimited)
    max_decisions: int = 40             # total tree-decision budget (bursts + spawns);
                                        # NOT fingerprinted — a budget, resume may extend it
    test_eval_on_new_best: bool = True  # run test eval only when the global best improves
                                        # (plus the root baseline anchor at birth)

    # Materials pipeline (per-burst interpretation -> mining -> living dossiers)
    interp_max_per_burst: int = 0       # 0 = interpret ALL burst trajectories (user default);
                                        # >0 caps per burst (reserved de-homogenization knob)
    screen_batch_size: int = 10         # Altitude Screen: items per batched judge call

    # Exploration (probe-based, before each spawn)
    explore_probe_guardrail: int = 48   # silent hard cap on probes per session (anti-runaway;
                                        # NOT in the prompt — tendency observation stays clean)
    explore_probe_k_max: int = 2        # max k per dispatch_probe call
    explore_neighbor_tasks: int = 3     # solved-neighbor tasks offered for contrast probes

    # Coverage ledger + probe leads (L1_actions_redesign §1)
    ledger_min_attempts: int = 1        # attempts before a 0-pass task counts as
                                        # UNSOLVED (m; user-set 2026-07-07, revisit
                                        # after live observation)
    leads_per_task: int = 3             # probe-pass leads kept per task (best
                                        # pass-rate first)

    # Generation pipelines
    gen_novelty_retries: int = 2        # NEW: max regenerations after novelty-confrontation rejects

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
        if self.gate_mode not in ("mean", "paired"):
            problems.append("gate_mode must be 'mean' or 'paired'")
        if self.merger_granularity not in ("point", "section"):
            problems.append("merger_granularity must be 'point' or 'section'")
        if not (0.0 < self.gate_paired_alpha < 1.0):
            problems.append("gate_paired_alpha must be in (0, 1)")
        if self.gate_screen_k < 1:
            problems.append("gate_screen_k must be >= 1")
        if self.gate_escalation_k < 1:
            problems.append("gate_escalation_k must be >= 1")
        if self.gate_max_escalation_rounds < 1:
            problems.append("gate_max_escalation_rounds must be >= 1")
        if self.min_l0_epochs < 0:
            problems.append("min_l0_epochs must be >= 0")
        if self.max_l0_epochs < 1:
            problems.append("max_l0_epochs must be >= 1")
        if self.max_l0_steps < 0:
            problems.append("max_l0_steps must be >= 0")
        if self.l0_stall_steps < 0:
            problems.append("l0_stall_steps must be >= 0")
        if self.verify_floor_divisor < 1:
            problems.append("verify_floor_divisor must be >= 1")
        if self.verify_min_net_flips < 0:
            problems.append("verify_min_net_flips must be >= 0")
        if self.verify_mode not in ("harm_veto", "full"):
            problems.append("verify_mode must be 'harm_veto' or 'full'")
        if self.burst_steps < 1:
            problems.append("burst_steps must be >= 1")
        if self.saturation_dry_bursts < 1:
            problems.append("saturation_dry_bursts must be >= 1")
        if self.node_degree < 1:
            problems.append("node_degree must be >= 1")
        if self.max_decisions < 1:
            problems.append("max_decisions must be >= 1")
        if self.screen_batch_size < 1:
            problems.append("screen_batch_size must be >= 1")
        if self.explore_probe_guardrail < 1:
            problems.append("explore_probe_guardrail must be >= 1")
        if self.explore_probe_k_max < 1:
            problems.append("explore_probe_k_max must be >= 1")
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
