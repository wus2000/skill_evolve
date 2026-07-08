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
Stage 2 (verdict, strictly symmetric, ONE round): every item in D gets
  ``gate_escalation_k`` FRESH rollouts on BOTH sides; per-item verdict
  compares pass counts over these equal fresh trials.
Decision — the item-level net-flip rule (estimated VALUE decides, not
evidence strength; items are the unit the deployment metric counts):
  n+ > n-                            -> ACCEPT
  n+ == n- and cand_mean > inc_mean  -> ACCEPT (mean tiebreak)
  otherwise                          -> reject
The exact sign-flip permutation p (:func:`perm_sf_signflip`, margins
included) is computed as TELEMETRY only — logged and persisted for post-hoc
calibration; no rollout is ever spent chasing significance.

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
    "PairedGateResult", "run_paired_gate", "run_noninferiority_gate",
    "binom_sf_half", "perm_sf_signflip",
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
    escalation_rounds: int = 1    # stage-2 rounds actually run
    inc_mean: float = 0.0         # incumbent ledger mean over the same val items

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
            "inc_mean": self.inc_mean,
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


def run_noninferiority_gate(
    env: "TaskEnv",
    node: "TreeNode",
    incumbent_rules: str,
    candidate_rules: str,
    val_items: list[dict],
    target_client: "LLMClient",
    cfg: "CSSConfig",
    out_dir: str,
) -> PairedGateResult:
    """Symmetric-fresh NON-INFERIORITY gate for the burst-end consolidation.

    The consolidation candidate is a reorganization of ``incumbent_rules``
    (typically ``node.best_rules``): its payoff is on the COST side (a smaller
    document), so the acceptance question is "not worse", never "better".
    Differences from :func:`run_paired_gate`:

      * BOTH sides are screened FRESH on the full val set (no ledger reads —
        the incumbent here may differ from ``node.rules``, whose measurements
        the ledger carries, and a paired comparison wants symmetric variance).
      * Two-layer non-inferiority decision (user ruling 2026-07-08):
          Layer 1 (binary):    n_lost <= n_gained on the escalated per-item
                               verdicts — no net solvability regression;
          Layer 2 (continuous): cand_mean >= inc_mean - margin
                               (``cfg.consolidation_margin``, default 1.5pp
                               ~= 1 sigma of the val mean) — a plain
                               "not lower than before" point comparison would
                               falsely kill ~half of all truly lossless
                               reorganizations.
        ACCEPT requires both.
      * On ACCEPT ``node.val_ledger`` is reset to the candidate's fresh
        measurements (screen + escalations) — the tidied document is the new
        incumbent, same transition as the paired gate.

    The sign-flip permutation p is computed as telemetry only, as elsewhere.
    """
    from css.skill_document import SkillDocument

    screen_k = max(1, getattr(cfg, "gate_screen_k", 1))
    esc_k = max(1, getattr(cfg, "gate_escalation_k", 3))
    margin = float(getattr(cfg, "consolidation_margin", 0.015))

    inc_text = SkillDocument(
        skill_dir="", strategy=node.strategy or "", rules=incumbent_rules or ""
    ).combined_skill_text()
    cand_text = SkillDocument(
        skill_dir="", strategy=node.strategy or "", rules=candidate_rules or ""
    ).combined_skill_text()

    item_by_id = {_item_id(it): it for it in val_items if _item_id(it)}
    items = list(item_by_id.values())

    # ── Stage 1: symmetric fresh screen on the full val set ──────────────
    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_cand = pool.submit(
            _roll_items, env, cand_text, items, target_client, screen_k,
            os.path.join(out_dir, "gate_screen_cand"), cfg)
        fut_inc = pool.submit(
            _roll_items, env, inc_text, items, target_client, screen_k,
            os.path.join(out_dir, "gate_screen_inc"), cfg)
        cand_screen = fut_cand.result()
        inc_screen = fut_inc.result()

    def _mean(screen: dict) -> float:
        rates = [p / t for (p, t) in screen.values() if t > 0]
        return sum(rates) / len(rates) if rates else 0.0

    cand_mean = _mean(cand_screen)
    inc_mean = _mean(inc_screen)

    # ── discordance (same resolution rule as the paired gate) ────────────
    discordant: list[str] = []
    for iid in item_by_id:
        c = cand_screen.get(iid)
        l = inc_screen.get(iid)
        if not c or not l or c[1] <= 0 or l[1] <= 0:
            continue
        if screen_k > 1:
            differs = c[0] * l[1] != l[0] * c[1]
        else:
            differs = (c[0] * 2 > c[1]) != (l[0] * 2 > l[1])
        if differs:
            discordant.append(iid)

    # ── Stage 2: one symmetric fresh escalation round ─────────────────────
    cand_acc: dict[str, list[int]] = {iid: [0, 0] for iid in discordant}
    inc_acc: dict[str, list[int]] = {iid: [0, 0] for iid in discordant}
    p_value = 1.0
    n_pos = n_neg = 0
    if discordant:
        d_items = [item_by_id[i] for i in discordant]
        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_cand = pool.submit(
                _roll_items, env, cand_text, d_items, target_client, esc_k,
                os.path.join(out_dir, "gate_esc_cand_r1"), cfg)
            fut_inc = pool.submit(
                _roll_items, env, inc_text, d_items, target_client, esc_k,
                os.path.join(out_dir, "gate_esc_inc_r1"), cfg)
            esc_cand = fut_cand.result()
            esc_inc = fut_inc.result()
        for iid, (p, t) in esc_cand.items():
            cand_acc[iid][0] += p
            cand_acc[iid][1] += t
        for iid, (p, t) in esc_inc.items():
            inc_acc[iid][0] += p
            inc_acc[iid][1] += t
        deltas = []
        for iid in discordant:
            cp, ct = cand_acc[iid]
            ip, it = inc_acc[iid]
            if ct <= 0 or it <= 0:
                continue
            deltas.append(cp - ip)
            if cp > ip:
                n_pos += 1
            elif ip > cp:
                n_neg += 1
        p_value = perm_sf_signflip(deltas)  # telemetry only

    layer1 = n_neg <= n_pos
    layer2 = cand_mean >= inc_mean - margin
    accept = layer1 and layer2

    _log.info(
        "non-inferiority gate: screen_discordant=%d -> gained=%d lost=%d, "
        "cand_mean=%.4f vs inc_mean=%.4f (margin=%.3f) -> %s "
        "(layer1=%s layer2=%s, telemetry_p=%.4f)",
        len(discordant), n_pos, n_neg, cand_mean, inc_mean, margin,
        "ACCEPT" if accept else "reject", layer1, layer2, p_value)

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
        ledger_bootstrapped=0,
        escalation_rounds=1,
        inc_mean=inc_mean,
    )


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

    # ── Stage 2: ONE symmetric fresh escalation round ─────────────────────
    # Decision = the item-level net-flip rule (estimated VALUE decides, not
    # evidence strength): items are the unit the deployment metric counts, so
    # the per-item verdict count IS the net-improvement estimate.
    #   n+  > n-                              -> ACCEPT
    #   n+ == n-  and cand_mean > inc_mean    -> ACCEPT (mean tiebreak)
    #   otherwise                             -> reject
    # The exact permutation p (margins included) is computed as TELEMETRY
    # only — logged and persisted for post-hoc calibration, never spending a
    # rollout chasing significance.
    cand_acc: dict[str, list[int]] = {iid: [0, 0] for iid in discordant}
    inc_acc: dict[str, list[int]] = {iid: [0, 0] for iid in discordant}
    p_value = 1.0
    n_pos = n_neg = 0

    if discordant:
        d_items = [item_by_id[i] for i in discordant]
        # The two sides are independent (different skill texts, disjoint
        # out_dirs) and each is well under the worker budget — roll them
        # CONCURRENTLY instead of back-to-back.
        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_cand = pool.submit(
                _roll_items, env, cand_text, d_items, target_client, esc_k,
                os.path.join(out_dir, "gate_esc_cand_r1"), cfg,
            )
            fut_inc = pool.submit(
                _roll_items, env, inc_text, d_items, target_client, esc_k,
                os.path.join(out_dir, "gate_esc_inc_r1"), cfg,
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
        p_value = perm_sf_signflip(deltas)  # telemetry only

    # Incumbent mean over the same val items (ledger, escalation folded in) —
    # the tiebreak scalar for the n+ == n- case.
    inc_rates = [
        e["passes"] / e["trials"]
        for iid, e in ledger.items()
        if iid in item_by_id and e.get("trials", 0) > 0
    ]
    inc_mean = sum(inc_rates) / len(inc_rates) if inc_rates else 0.0

    if n_pos > n_neg:
        accept = True
    elif n_pos == n_neg:
        # Tie on item evidence -> the higher mean wins (user ruling 2026-07-05).
        # Compare against the WEAKER of the two incumbent estimates: the
        # accumulated ledger mean AND the node's recorded score. The old rule
        # (ledger mean only) rejected candidates that visibly beat the recorded
        # score (measured: ss step 8 — 4v4, cand 0.7018 vs recorded 0.6842,
        # ledger ~0.71 -> reject), which reads as refusing a visible
        # improvement at neutral item evidence.
        recorded = float(getattr(node, "val_score", inc_mean) or inc_mean)
        accept = cand_mean > min(inc_mean, recorded)
    else:
        accept = False

    _log.info(
        "paired gate: screen_discordant=%d -> n+=%d n-=%d "
        "(cand_mean=%.4f inc_mean=%.4f, telemetry_p=%.4f) -> %s",
        len(discordant), n_pos, n_neg, cand_mean, inc_mean, p_value,
        "ACCEPT" if accept else "reject",
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
        escalation_rounds=1,
        inc_mean=inc_mean,
    )
