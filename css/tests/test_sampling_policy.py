"""Sampling policy is ONE agreed value per domain, owned by the client.

User ruling 2026-07-08, after the audit found rollout temperature scattered
across envs (0.0 / 0.4, plus a vendored ReAct default of 0.7) while the
launcher's ``temperature`` key only ever reached the optimizer:

  * rollouts (target path) sample at 0.6 — the K repeats of a task must
    actually differ, or contrastive groups and the paired gate's variance
    estimate are measuring vLLM batching noise;
  * every other call is greedy (0.0).

Plus the decoding-collapse detector: truncation has two causes, and only one
of them can be fixed by a bigger cap.
"""
from __future__ import annotations

import glob
import re

from css.checkpoint import config_fingerprint
from css.config import CSSConfig
from css.model.client import OpenAICompatLLMClient, detect_degenerate_tail


def _client(**kw) -> OpenAICompatLLMClient:
    base = dict(base_url="http://x/v1", api_key="k", target_model="m",
                optimizer_model="m")
    base.update(kw)
    return OpenAICompatLLMClient(**base)


# ── the two domains ──────────────────────────────────────────────────────────
def test_client_defaults_are_the_agreed_values():
    c = _client()
    assert c.target_temperature == 0.6
    assert c.optimizer_temperature == 0.0


def test_target_temperature_resolution():
    c = _client(target_temperature=0.6)
    assert c._target_temp(None) == 0.6, "None = the client's agreed value"
    assert c._target_temp(0.0) == 0.0, "an explicit value still wins"


def test_no_env_hard_codes_a_rollout_temperature():
    """The whole point of the ruling: one owner, no per-env drift."""
    offenders = []
    pat = re.compile(r"temperature\s*[=:]\s*0\.\d")
    for path in glob.glob("css/envs/*/agent.py") + \
            glob.glob("css/envs/*/task_interface.py") + \
            glob.glob("css/envs/*/react_adapter.py") + \
            glob.glob("css/envs/webarena/agent.py"):
        for i, line in enumerate(open(path, encoding="utf-8"), 1):
            if line.lstrip().startswith("#"):
                continue
            if pat.search(line):
                offenders.append(f"{path}:{i}: {line.strip()}")
    assert not offenders, "envs must pass temperature=None:\n" + "\n".join(offenders)


def test_every_launcher_sets_both_domains():
    for path in glob.glob("run_experiment_*.py"):
        src = open(path, encoding="utf-8").read()
        assert '"target_temperature": 0.6' in src, f"{path} misses target_temperature"
        assert '"optimizer_temperature": 0.0' in src, f"{path} misses optimizer_temperature"
        assert '"temperature":' not in src, f"{path} still carries the legacy key"


# ── resume guard: a sampling change must invalidate the checkpoint ───────────
def _cfg(**extra) -> CSSConfig:
    return CSSConfig(n_train=1, n_val=1, n_test=1,
                     extra={"target_temperature": 0.6,
                            "optimizer_temperature": 0.0, **extra})


def test_fingerprint_covers_sampling_policy():
    base = config_fingerprint(_cfg())
    assert config_fingerprint(_cfg()) == base, "stable for identical config"
    hotter = _cfg()
    hotter.extra["target_temperature"] = 0.7
    assert config_fingerprint(hotter) != base, \
        "changing rollout sampling must refuse a resume: cached rollouts and " \
        "gate incumbents were drawn from the old distribution"
    greedy_opt = _cfg()
    greedy_opt.extra["optimizer_temperature"] = 0.7
    assert config_fingerprint(greedy_opt) != base


# ── decoding-collapse detector ───────────────────────────────────────────────
def test_detects_single_char_run():
    d = detect_degenerate_tail("real analysis here. " + "\t" * 900)
    assert d and "repeat of '\\t'" in d


def test_detects_short_cycle():
    d = detect_degenerate_tail("prose " + "ab" * 500)
    assert d and "cycle" in d


def test_detects_low_entropy_tail():
    d = detect_degenerate_tail("x" * 20 + "aab" * 300)
    assert d, "a 3-symbol tail is a collapse even without a clean run/cycle"


def test_healthy_prose_is_not_flagged():
    prose = ("The agent inspected the workbook, inferred the transformation "
             "rule from the example, and wrote formulas into the target "
             "range. The evaluator read values, not formulas, so it failed. ")
    assert detect_degenerate_tail(prose * 6) is None


def test_short_or_empty_text_is_never_flagged():
    assert detect_degenerate_tail("") is None
    assert detect_degenerate_tail("\t" * 20) is None, "below the 32-char floor"


def test_indentation_heavy_code_is_not_flagged():
    """JSON/code legitimately repeats spaces — only the TAIL window matters,
    and a real code tail still ends with structure, not a run."""
    code = '{\n' + '\n'.join('    "k%d": %d,' % (i, i) for i in range(80)) + '\n}'
    assert detect_degenerate_tail(code) is None
