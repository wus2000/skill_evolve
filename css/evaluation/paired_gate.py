"""Two-stage item-paired validation gate — exact sign test on per-item flips.

Replaces the mean-comparison gate's noisy scalar decision with an item-paired
comparison on the run-fixed validation set. Motivated by the measured failure
mode of the mean gate: per-step true effects (±0.3-1pp) sit inside the mean
comparison's sampling noise (σ≈1.3pp on 1100 items at K=1), so accept/reject
degenerates to coin flips and a lucky incumbent measurement becomes an
unbeatable bar (winner's-curse ratchet).

Protocol
--------
Stage 0 (bootstrap, once per node): items missing from the node's incumbent
  ledger are rolled with the INCUMBENT rules at ``gate_screen_k`` and recorded.
Stage 1 (screen): candidate is rolled on ALL val items at ``gate_screen_k``.
  Discordance rule adapts to the screen's resolution: at K=1 a majority flip
  (a single-draw verdict can't support rate comparison); at K>1 ANY rate
  difference (majority-flip is blind to partial shifts like 2/3 -> 3/3 —
  real signal that reaches the mean but would never reach the decision).
  The screen only SELECTS items; its data never enters the accept decision.
Stage 2 (verdict, strictly symmetric, adaptively deepened): every item in D
  gets ``gate_escalation_k`` FRESH rollouts on BOTH sides per round. The
  decision statistic is an exact sign-flip permutation test on the per-item
  pass-count deltas (:func:`perm_sf_signflip`) — margins count, ties
  contribute zero. While p sits in the ambiguous band (alpha, 0.5], further
  rounds add depth (up to ``gate_max_escalation_rounds``): small val sets
  cannot add width, so the gate buys information depth per item instead.
Decision: ACCEPT iff the permutation p <= ``gate_paired_alpha``.

Ledger maintenance: incumbent-side escalation rollouts are folded into the
ledger (contested items accumulate precision over steps). On ACCEPT the ledger
is reset to the winning candidate's measurements (screen + its escalations).

Resume note: gate decisions are checkpointed by the step loop; a resume that
replays a completed step does not re-run rollouts, so ledger increments from
that original call may be lost if the node checkpoint predates them. This is
benign — the ledger self-heals (bootstrap fills missing items only; lost
escalation counts merely reduce accumulated precision).
"""
from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from math import comb
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from css.config import CSSConfig
    from css.data.tree import TreeNode
    from css.envs.base import TaskEnv
    from css.model.client import LLMClient

_log = logging.getLogger("css")

__all__ = [
    "PairedGateResult", "run_paired_gate", "binom_sf_half", "perm_sf_signflip",
]


@dataclass
class PairedGateResult:
    """Outcome of one two-stage paired gate evaluation."""

    accept: bool
    p_value: float
    n_pos: int                    # discordant items where candidate leads on trials
    n_neg: int                    # discordant items where incumbent leads on trials
    n_screen_discordant: int      # |D| after stage-1 screen
    cand_mean: float              # candidate screen mean pass rate (bookkeeping scalar)
    ledger_bootstrapped: int      # items bootstrapped into the ledger this call
    escalation_rounds: int = 1    # stage-2 rounds actually run (adaptive deepening)

    def to_dict(self) -> dict:
        return {
            "accept": self.accept,
            "p_value": self.p_value,
            "n_pos": self.n_pos,
            "n_neg": self.n_neg,
            "n_screen_discordant": self.n_screen_discordant,
            "cand_mean": self.cand_mean,
            "ledger_bootstrapped": self.ledger_bootstrapped,
            "escalation_rounds": self.escalation_rounds,
        }


def binom_sf_half(n_pos: int, n_total: int) -> float:
    """Exact one-sided p-value: P(X >= n_pos) for X ~ Binomial(n_total, 1/2).

    ``n_total == 0`` returns 1.0 (no evidence).
    """
    if n_total <= 0:
        return 1.0
    n_pos = max(0, min(n_pos, n_total))
    total = sum(comb(n_total, k) for k in range(n_pos, n_total + 1))
    return total / float(2 ** n_total)


def perm_sf_signflip(deltas: "list[int]") -> float:
    """Exact one-sided sign-flip permutation p-value for paired count deltas.

    ``deltas[i]`` = candidate passes − incumbent passes on item ``i`` (equal
    trials per side). Under the strong null (identical per-item distributions,
    equal fresh trials) the two sides are exchangeable, so each delta's sign
    is an independent fair coin. The statistic is ``T = Σ deltas``; the
    p-value is ``P(Σ ε_i·δ_i >= T)`` over all 2^n sign assignments.

    Strictly more powerful than the sign test on the same data — margins
    count (a 3-0 item outweighs a 2-1 item) and zero deltas contribute
    nothing instead of being discarded.

    Computed EXACTLY for any n via dynamic programming over the discrete sum
    distribution (values are small integers; the support is ±Σ|δ|).
    ``deltas`` empty or all-zero returns 1.0 (no evidence).
    """
    nz = [abs(d) for d in deltas if d != 0]
    t_obs = sum(deltas)
    if not nz:
        return 1.0

    # DP over the distribution of Σ ε_i·|δ_i|: dist maps sum -> probability.
    dist: dict[int, float] = {0: 1.0}
    for m in nz:
        nxt: dict[int, float] = {}
        for s, pr in dist.items():
            half = pr * 0.5
            nxt[s + m] = nxt.get(s + m, 0.0) + half
            nxt[s - m] = nxt.get(s - m, 0.0) + half
        dist = nxt

    return sum(pr for s, pr in dist.items() if s >= t_obs)


def _item_id(item: dict) -> str:
    return str(item.get("id", item.get("task_id", "")))


def _roll_items(
    env: "TaskEnv",
    skill_text: str,
    items: list[dict],
    target_client: "LLMClient",
    k: int,
    out_dir: str,
    cfg: "CSSConfig",
) -> dict[str, tuple[int, int]]:
    """Roll ``k`` rollouts per item; return {item_id: (passes, trials)}."""
    from css.rollout.batch import grouped_batch_rollout

    if not items:
        return {}
    groups = grouped_batch_rollout(
        env,
        items,
        skill_text,
        target_client,
        k_rollouts=k,
        out_dir=out_dir,
        max_workers=cfg.max_api_workers,
        task_timeout=cfg.task_timeout_s,
    )
    return {
        g.task_id: (sum(1 for r in g.rollouts if r.passed), len(g.rollouts))
        for g in groups
    }


def _seed_ledger_from_predictions(
    env: "TaskEnv",
    ledger: dict,
    items: list[dict],
    inc_text: str,
    seed_dir: str,
    cfg: "CSSConfig",
) -> int:
    """Seed ledger entries from same-skill predictions persisted on disk.

    Uses the env's own resume-cache loader (``load_cached_result``) with the
    incumbent's skill hash — reuse happens only for results produced under
    the EXACT same skill text, so a stale/different-skill baseline can never
    poison the ledger. Best-effort: any miss just leaves the item for the
    rollout bootstrap. Returns the number of items seeded.
    """
    loader = getattr(env, "load_cached_result", None)
    if loader is None:
        return 0
    try:
        from css.envs.common import skill_hash
        inc_hash = skill_hash(inc_text)
    except Exception:  # noqa: BLE001
        return 0

    max_k = max(1, getattr(cfg, "k_rollouts", 1))
    seeded = 0
    for item in items:
        iid = _item_id(item)
        passes = trials = 0
        for r in range(max_k):
            try:
                res = loader(item, seed_dir, rollout_index=r, skill_hash=inc_hash)
            except Exception:  # noqa: BLE001
                res = None
            if res is None:
                continue
            trials += 1
            passes += 1 if res.passed else 0
        if trials > 0:
            ledger[iid] = {"passes": passes, "trials": trials}
            seeded += 1
    return seeded


def run_paired_gate(
    env: "TaskEnv",
    node: "TreeNode",
    candidate_rules: str,
    val_items: list[dict],
    target_client: "LLMClient",
    cfg: "CSSConfig",
    out_dir: str,
) -> PairedGateResult:
    """Run the full two-stage paired gate for one candidate.

    Mutates ``node.val_ledger`` (bootstrap fills, escalation folds, reset on
    accept). Does NOT mutate ``node.rules`` — the caller owns the accept side
    effects.
    """
    from css.skill_document import SkillDocument

    screen_k = max(1, getattr(cfg, "gate_screen_k", 1))
    esc_k = max(1, getattr(cfg, "gate_escalation_k", 3))
    alpha = getattr(cfg, "gate_paired_alpha", 0.1)

    inc_text = SkillDocument(
        skill_dir="", strategy=node.strategy or "", rules=node.rules or ""
    ).combined_skill_text()
    cand_text = SkillDocument(
        skill_dir="", strategy=node.strategy or "", rules=candidate_rules or ""
    ).combined_skill_text()

    item_by_id = {_item_id(it): it for it in val_items if _item_id(it)}
    ledger: dict = node.val_ledger

    # ── Stage 0: fill missing incumbent ledger entries ───────────────────
    # Seed-first: the round's val_baseline rollout already measured THIS
    # incumbent on the val set (same skill text) at k_rollouts — read those
    # predictions from disk (path + skill_hash checked by the env's cache
    # loader; zero rollouts) before rolling anything.
    missing = [iid for iid in item_by_id if iid not in ledger]
    if missing:
        seed_dir = str(getattr(cfg, "_val_baseline_dir", "") or "")
        seeded = 0
        if seed_dir and os.path.isdir(seed_dir):
            seeded = _seed_ledger_from_predictions(
                env, ledger, [item_by_id[i] for i in missing],
                inc_text, seed_dir, cfg,
            )
            if seeded:
                _log.info(
                    "paired gate: seeded incumbent ledger for %d item(s) from "
                    "val_baseline predictions (no rollouts)", seeded,
                )
            missing = [iid for iid in item_by_id if iid not in ledger]
    # ── Stage 0b + Stage 1: bootstrap (incumbent, missing items) and screen
    # (candidate, all items) are independent rollout batches — run them
    # CONCURRENTLY. Bootstrap is usually empty after seeding.
    if missing:
        _log.info("paired gate: bootstrapping incumbent ledger for %d item(s)",
                  len(missing))
    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_screen = pool.submit(
            _roll_items, env, cand_text, list(item_by_id.values()),
            target_client, screen_k,
            os.path.join(out_dir, "gate_screen_cand"), cfg,
        )
        fut_boot = None
        if missing:
            fut_boot = pool.submit(
                _roll_items, env, inc_text,
                [item_by_id[i] for i in missing], target_client, screen_k,
                os.path.join(out_dir, "gate_bootstrap_inc"), cfg,
            )
        cand_screen = fut_screen.result()
        if fut_boot is not None:
            for iid, (p, t) in fut_boot.result().items():
                ledger[iid] = {"passes": p, "trials": t}
    rates = [p / t for (p, t) in cand_screen.values() if t > 0]
    cand_mean = sum(rates) / len(rates) if rates else 0.0

    # Discordance rule depends on the screen's resolution:
    #   screen_k == 1 — the screen verdict is a single Bernoulli draw; only a
    #     majority flip is meaningful (rate-difference against a multi-trial
    #     ledger would flag nearly every item).
    #   screen_k > 1  — the screen measures a RATE; any rate difference is
    #     signal-bearing. Majority-flip here is blind to partial shifts
    #     (2/3 -> 3/3 contributes to the mean but never reaches the decision
    #     — the measured Spreadsheet step0 failure mode).
    discordant: list[str] = []
    for iid in item_by_id:
        c = cand_screen.get(iid)
        l = ledger.get(iid)
        if not c or not l or c[1] <= 0 or l.get("trials", 0) <= 0:
            continue
        if screen_k > 1:
            # exact cross-multiplied rate comparison (no float tolerance)
            differs = c[0] * l["trials"] != l["passes"] * c[1]
        else:
            differs = (c[0] * 2 > c[1]) != (l["passes"] * 2 > l["trials"])
        if differs:
            discordant.append(iid)

    # ── Stage 2: symmetric fresh escalation with adaptive deepening ──────
    # Each round adds esc_k FRESH rollouts PER SIDE on the discordant items;
    # the decision statistic is an exact sign-flip permutation test on the
    # per-item pass-count deltas (margins count: 3-0 > 2-1; ties contribute
    # zero instead of being discarded). While p sits in the ambiguous band
    # (alpha, 0.5], buy DEPTH — the information-theoretic answer for small
    # val sets whose width is capped.
    max_rounds = max(1, getattr(cfg, "gate_max_escalation_rounds", 3))
    cand_acc: dict[str, list[int]] = {iid: [0, 0] for iid in discordant}
    inc_acc: dict[str, list[int]] = {iid: [0, 0] for iid in discordant}
    esc_rounds = 0
    p_value = 1.0
    n_pos = n_neg = 0

    while discordant and esc_rounds < max_rounds:
        esc_rounds += 1
        d_items = [item_by_id[i] for i in discordant]
        # The two sides are independent (different skill texts, disjoint
        # out_dirs) and each is well under the worker budget — roll them
        # CONCURRENTLY instead of back-to-back (halves escalation wall time).
        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_cand = pool.submit(
                _roll_items, env, cand_text, d_items, target_client, esc_k,
                os.path.join(out_dir, f"gate_esc_cand_r{esc_rounds}"), cfg,
            )
            fut_inc = pool.submit(
                _roll_items, env, inc_text, d_items, target_client, esc_k,
                os.path.join(out_dir, f"gate_esc_inc_r{esc_rounds}"), cfg,
            )
            esc_cand = fut_cand.result()
            esc_inc = fut_inc.result()
        for iid, (p, t) in esc_cand.items():
            cand_acc[iid][0] += p
            cand_acc[iid][1] += t
        for iid, (p, t) in esc_inc.items():
            inc_acc[iid][0] += p
            inc_acc[iid][1] += t
            # Fold incumbent escalation into the ledger (precision accumulates).
            entry = ledger.setdefault(iid, {"passes": 0, "trials": 0})
            entry["passes"] += p
            entry["trials"] += t

        deltas = []
        n_pos = n_neg = 0
        for iid in discordant:
            cp, ct = cand_acc[iid]
            ip, it = inc_acc[iid]
            if ct <= 0 or it <= 0:
                continue
            deltas.append(cp - ip)  # equal trials per side by construction
            if cp > ip:
                n_pos += 1
            elif ip > cp:
                n_neg += 1
        p_value = perm_sf_signflip(deltas)

        _log.info(
            "paired gate: round %d/%d — |D|=%d n+=%d n-=%d perm_p=%.4f",
            esc_rounds, max_rounds, len(discordant), n_pos, n_neg, p_value,
        )
        if p_value <= alpha or p_value > 0.5:
            break  # decided (accept) or trending-worse (no point deepening)

    accept = p_value <= alpha

    _log.info(
        "paired gate: screen_discordant=%d -> %d escalation round(s), "
        "n+=%d n-=%d p=%.4f alpha=%.2f -> %s (cand_mean=%.4f)",
        len(discordant), esc_rounds, n_pos, n_neg, p_value, alpha,
        "ACCEPT" if accept else "reject", cand_mean,
    )

    # ── Ledger transition on accept: candidate becomes the incumbent ─────
    if accept:
        new_ledger: dict = {
            iid: {"passes": p, "trials": t}
            for iid, (p, t) in cand_screen.items() if t > 0
        }
        for iid, (p, t) in cand_acc.items():
            if t > 0:
                entry = new_ledger.setdefault(iid, {"passes": 0, "trials": 0})
                entry["passes"] += p
                entry["trials"] += t
        node.val_ledger = new_ledger

    return PairedGateResult(
        accept=accept,
        p_value=p_value,
        n_pos=n_pos,
        n_neg=n_neg,
        n_screen_discordant=len(discordant),
        cand_mean=cand_mean,
        ledger_bootstrapped=len(missing),
        escalation_rounds=esc_rounds,
    )
