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
  Items whose majority verdict differs from the ledger's majority form the
  discordant set D. The screen is allowed to be asymmetric (fresh candidate
  vs ledger estimate) — it only SELECTS items; its data never enters the
  accept decision.
Stage 2 (verdict, strictly symmetric): every item in D gets ``gate_escalation_k``
  FRESH rollouts on BOTH sides. Per-item verdict compares pass counts over
  these equal fresh trials only; ties are dropped. This preserves the exact
  cand<->inc swap symmetry the sign test requires, regardless of how biased
  the screen was.
Decision: one-sided exact binomial sign test on (n_pos, n_neg):
  p = P(Bin(n_pos+n_neg, 1/2) >= n_pos); ACCEPT iff p <= ``gate_paired_alpha``.

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
from dataclasses import dataclass
from math import comb
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from css.config import CSSConfig
    from css.data.tree import TreeNode
    from css.envs.base import TaskEnv
    from css.model.client import LLMClient

_log = logging.getLogger("css")

__all__ = ["PairedGateResult", "run_paired_gate", "binom_sf_half"]


@dataclass
class PairedGateResult:
    """Outcome of one two-stage paired gate evaluation."""

    accept: bool
    p_value: float
    n_pos: int                    # discordant items where candidate won stage 2
    n_neg: int                    # discordant items where incumbent won stage 2
    n_screen_discordant: int      # |D| after stage-1 screen
    cand_mean: float              # candidate screen mean pass rate (bookkeeping scalar)
    ledger_bootstrapped: int      # items bootstrapped into the ledger this call

    def to_dict(self) -> dict:
        return {
            "accept": self.accept,
            "p_value": self.p_value,
            "n_pos": self.n_pos,
            "n_neg": self.n_neg,
            "n_screen_discordant": self.n_screen_discordant,
            "cand_mean": self.cand_mean,
            "ledger_bootstrapped": self.ledger_bootstrapped,
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
    if missing:
        _log.info("paired gate: bootstrapping incumbent ledger for %d item(s)",
                  len(missing))
        boot = _roll_items(
            env, inc_text, [item_by_id[i] for i in missing], target_client,
            screen_k, os.path.join(out_dir, "gate_bootstrap_inc"), cfg,
        )
        for iid, (p, t) in boot.items():
            ledger[iid] = {"passes": p, "trials": t}

    # ── Stage 1: candidate screen on all val items ────────────────────────
    cand_screen = _roll_items(
        env, cand_text, list(item_by_id.values()), target_client,
        screen_k, os.path.join(out_dir, "gate_screen_cand"), cfg,
    )
    rates = [p / t for (p, t) in cand_screen.values() if t > 0]
    cand_mean = sum(rates) / len(rates) if rates else 0.0

    discordant: list[str] = []
    for iid in item_by_id:
        c = cand_screen.get(iid)
        l = ledger.get(iid)
        if not c or not l or c[1] <= 0 or l.get("trials", 0) <= 0:
            continue
        cand_maj = c[0] * 2 > c[1]
        inc_maj = l["passes"] * 2 > l["trials"]
        if cand_maj != inc_maj:
            discordant.append(iid)

    # ── Stage 2: symmetric fresh escalation on discordant items ──────────
    n_pos = n_neg = 0
    esc_cand: dict[str, tuple[int, int]] = {}
    if discordant:
        d_items = [item_by_id[i] for i in discordant]
        esc_cand = _roll_items(
            env, cand_text, d_items, target_client, esc_k,
            os.path.join(out_dir, "gate_esc_cand"), cfg,
        )
        esc_inc = _roll_items(
            env, inc_text, d_items, target_client, esc_k,
            os.path.join(out_dir, "gate_esc_inc"), cfg,
        )
        for iid in discordant:
            cp, ct = esc_cand.get(iid, (0, 0))
            ip, it = esc_inc.get(iid, (0, 0))
            if ct <= 0 or it <= 0:
                continue
            if cp > ip:
                n_pos += 1
            elif ip > cp:
                n_neg += 1
        # Fold incumbent escalation into the ledger (precision accumulates).
        for iid, (p, t) in esc_inc.items():
            entry = ledger.setdefault(iid, {"passes": 0, "trials": 0})
            entry["passes"] += p
            entry["trials"] += t

    p_value = binom_sf_half(n_pos, n_pos + n_neg)
    accept = p_value <= alpha

    _log.info(
        "paired gate: screen_discordant=%d -> stage2 n+=%d n-=%d p=%.4f "
        "alpha=%.2f -> %s (cand_mean=%.4f)",
        len(discordant), n_pos, n_neg, p_value, alpha,
        "ACCEPT" if accept else "reject", cand_mean,
    )

    # ── Ledger transition on accept: candidate becomes the incumbent ─────
    if accept:
        new_ledger: dict = {
            iid: {"passes": p, "trials": t}
            for iid, (p, t) in cand_screen.items() if t > 0
        }
        for iid, (p, t) in esc_cand.items():
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
    )
