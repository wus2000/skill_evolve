"""Reflect proposer context-budget contract.

Long-observation envs must never blow the model context: a WebArena minibatch
(8 trajectories x ~30 turns of AXTree observations) rendered ~250k TOKENS at
fixed tool_trunc and the proposer call died with HTTP 400 — swallowed into
"no raw edits to merge", silently disabling the whole L0 loop (2026-07-06
smoke #2). These tests pin the fix: per-message cap decay against the
context_cap * context_use_frac budget, loud trajectory shedding as the last
resort, and a proposer failure yielding an empty patch instead of a crash.
"""
from __future__ import annotations

import unittest
from unittest import mock

from css.config import CSSConfig
from css.data.rollout import TaskResult
from css.optimizer import reflect as R


def _result(tid: str, n_turns: int = 30, obs_chars: int = 9000) -> TaskResult:
    msgs = []
    for t in range(n_turns):
        msgs.append({"role": "user", "content": f"[{t}] " + "o" * obs_chars})
        msgs.append({"role": "assistant", "content": f"click [{t}]"})
    msgs.append({"role": "evaluation", "content": "Outcome: FAIL"})
    return TaskResult(task_id=tid, rollout_index=0, hard=0, soft=0.0,
                      messages=msgs, n_turns=n_turns, fail_reason="wrong page")


def _cfg(**kw) -> CSSConfig:
    base = dict(env_name="spreadsheetbench", n_train=0, n_val=0, n_test=0)
    base.update(kw)
    return CSSConfig(**base)


def _est(text: str) -> int:
    return int(len(text) / R._FALLBACK_CHARS_PER_TOKEN) + 1


class TestFitRenderToBudget(unittest.TestCase):
    def test_decays_until_fit(self):
        calls = []

        def render(cap):
            calls.append(cap)
            return "x" * (cap * 100)

        text, tokens, fitted = R._fit_render_to_budget(render, 8000, 75_000, _est)
        self.assertTrue(fitted)
        self.assertLessEqual(tokens, 75_000)
        self.assertGreater(len(calls), 1)          # decay actually engaged

    def test_within_budget_untouched(self):
        text, tokens, fitted = R._fit_render_to_budget(
            lambda cap: "y" * 100, 8000, 75_000, _est)
        self.assertTrue(fitted)
        self.assertEqual(text, "y" * 100)

    def test_floor_reached_reports_unfit(self):
        text, tokens, fitted = R._fit_render_to_budget(
            lambda cap: "z" * 1_000_000, 8000, 75_000, _est)
        self.assertFalse(fitted)

    def test_exact_counter_preferred_over_estimate(self):
        class Client:
            def count_tokens(self, text):
                return 7
        self.assertEqual(R._count_tokens(Client(), "x" * 1000), 7)
        # no counter -> conservative estimate
        self.assertEqual(R._count_tokens(object(), "x" * 1000), _est("x" * 1000))


class TestProposerBudget(unittest.TestCase):
    def _run(self, cfg, rollouts):
        captured = {}

        def fake_complete(client, system, user, parse, max_tokens, stage):
            captured["user"] = user
            return []

        with mock.patch.object(R, "complete_optimizer_json", fake_complete):
            rp = R._run_minibatch_proposer(
                object(), "strategy", "### Rules\n- be careful", rollouts,
                "SYSTEM", "failure", cfg=cfg)
        return rp, captured

    def test_webarena_scale_minibatch_fits_default_context(self):
        # 8 x 30-turn x 9k-char observations ~= 2.2M chars raw — the exact
        # shape that 400'd in the smoke. Must fit the default 256k*0.8 budget
        # (token math on the conservative fallback estimator here).
        rollouts = [_result(f"wa_{i:04d}") for i in range(8)]
        rp, cap = self._run(_cfg(), rollouts)
        budget_tokens = (_cfg().effective_context_threshold
                         - R._PROPOSER_MAX_TOKENS)
        self.assertLess(_est(cap["user"]), budget_tokens)
        self.assertIn("[truncated", cap["user"])   # elision, not omission
        self.assertEqual(cap["user"].count("### Trajectory"), 8)  # none dropped

    def test_extreme_budget_sheds_trajectories_loudly(self):
        # Tiny context clamps the budget to the 40k floor; even floor-capped
        # messages overflow, so whole trajectories must be shed (not a 400).
        rollouts = [_result(f"wa_{i:04d}") for i in range(8)]
        cfg = _cfg(context_cap=20_000)
        with self.assertLogs("css.optimizer.reflect", level="WARNING") as logs:
            rp, cap = self._run(cfg, rollouts)
        self.assertLess(cap["user"].count("### Trajectory"), 8)
        self.assertGreaterEqual(cap["user"].count("### Trajectory"), 1)
        self.assertTrue(any("dropped" in m for m in logs.output))

    def test_proposer_failure_yields_empty_patch(self):
        def boom(*a, **k):
            raise RuntimeError("HTTP 400: context length exceeded")

        with mock.patch.object(R, "complete_optimizer_json", boom):
            with self.assertLogs("css.optimizer.reflect", level="WARNING"):
                rp = R._run_minibatch_proposer(
                    object(), "s", "r", [_result("t1", n_turns=2, obs_chars=50)],
                    "SYSTEM", "failure", cfg=_cfg())
        self.assertEqual(rp.patch.edits, [])


if __name__ == "__main__":
    unittest.main()
