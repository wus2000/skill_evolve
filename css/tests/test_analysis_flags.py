"""Tests for the analysis prompt-shape flags (json_list_wrap / analysis_env_context).

Regression background: ``response_format={"type": "json_object"}`` grammar-forbids
a top-level JSON array, so every "output a JSON list" optimizer prompt silently
collapsed to exactly ONE element per call (measured 3000/3000 ALFWorld and
3300/3300 Bird layer-1 annotations). ``json_list_wrap`` asks for a wrapped object
instead; ``analysis_env_context`` injects the env's action-space description so
the analyzer knows environment semantics. Both flags default False and must keep
the legacy prompt bytes byte-identical for runs already in flight.
"""
from __future__ import annotations

import json

from css.analysis.layer1 import (
    _SINGLE_SYSTEM,
    _SINGLE_SYSTEM_WRAPPED,
    annotate_contrastive_pair,
    annotate_trajectory,
)
from css.analysis.pipeline import analysis_env_context
from css.config import CSSConfig
from css.data.rollout import TaskResult
from css.model.client import StubLLMClient


def _result(passed: bool = False, task_id: str = "t0", rollout: int = 0) -> TaskResult:
    return TaskResult(
        task_id=task_id,
        rollout_index=rollout,
        hard=1 if passed else 0,
        messages=[
            {"role": "system", "content": "You are an agent."},
            {"role": "assistant", "content": "<action>look</action>"},
            {"role": "user", "content": "Nothing happens."},
        ],
        fail_reason="" if passed else "step-limit",
        task_description="put a mug on the desk",
    )


def _obs(i: int) -> dict:
    return {
        "what": f"behavior {i}",
        "cognitive_aspect": f"aspect {i}",
        "evidence": "e",
        "consequence": "c",
        "polarity": "negative",
        "significance": "notable",
    }


class _Capture:
    def __init__(self, response: str):
        self.response = response
        self.system = ""
        self.user = ""

    def __call__(self, system: str, user: str) -> str:
        self.system = system
        self.user = user
        return self.response


# ── json_list_wrap: prompt shape ─────────────────────────────────────────────


def test_wrap_off_keeps_legacy_prompt_and_single_object_coercion():
    cap = _Capture(json.dumps(_obs(0)))  # bare single object (the json-mode failure shape)
    client = StubLLMClient(optimizer_fn=cap)
    cfg = CSSConfig(k_rollouts=1, max_api_workers=1)
    obs = annotate_trajectory(client, _result(), cfg=cfg)
    assert "Output ONLY a JSON list, each element:" in cap.system
    assert "just the JSON list." in cap.system
    assert "ONLY the JSON list described" in cap.user
    assert "Environment context" not in cap.user
    assert len(obs) == 1  # legacy coercion path unchanged


def test_wrap_on_requests_object_and_parses_full_list():
    cap = _Capture(json.dumps({"observations": [_obs(0), _obs(1), _obs(2)]}))
    client = StubLLMClient(optimizer_fn=cap)
    cfg = CSSConfig(k_rollouts=1, max_api_workers=1, json_list_wrap=True)
    obs = annotate_trajectory(client, _result(), cfg=cfg)
    assert '"observations": [<element>' in cap.system
    assert "just the JSON object." in cap.system
    assert "JSON list" not in cap.system
    assert "ONLY the JSON object described" in cap.user
    assert len(obs) == 3
    assert [o.obs_id for o in obs] == ["t0:r0:0", "t0:r0:1", "t0:r0:2"]


# ── analysis_env_context: prompt injection ───────────────────────────────────


def test_env_context_injected_into_annotate_prompt():
    cap = _Capture(json.dumps([_obs(0)]))
    client = StubLLMClient(optimizer_fn=cap)
    cfg = CSSConfig(k_rollouts=1, max_api_workers=1)
    annotate_trajectory(client, _result(), cfg=cfg, env_context="ENV RULES HERE")
    assert "Environment context (how this environment works):" in cap.user
    assert "ENV RULES HERE" in cap.user
    # placed before the trajectory block
    assert cap.user.index("ENV RULES HERE") < cap.user.index("Trajectory (the agent's")


def test_env_context_injected_into_contrastive_prompt():
    div = {
        "divergence_point": "d",
        "cognitive_difference": "c",
        "is_systematic": True,
        "divergence_level": "approach_difference",
    }
    cap = _Capture(json.dumps(div))
    client = StubLLMClient(optimizer_fn=cap)
    cfg = CSSConfig(k_rollouts=1, max_api_workers=1)
    out = annotate_contrastive_pair(
        client, _result(True, rollout=0), _result(False, rollout=1),
        cfg=cfg, env_context="ENV RULES HERE",
    )
    assert out is not None
    assert cap.user.startswith("Environment context (how this environment works):")
    # empty context keeps the legacy head byte-identical
    cap2 = _Capture(json.dumps(div))
    annotate_contrastive_pair(
        StubLLMClient(optimizer_fn=cap2),
        _result(True, rollout=0), _result(False, rollout=1), cfg=cfg,
    )
    assert cap2.user.startswith("# Contrastive pair")


def test_analysis_env_context_helper_gating():
    class _Env:
        def action_space_description(self) -> str:
            return "DESC"

    off = CSSConfig(k_rollouts=1, max_api_workers=1)
    on = CSSConfig(k_rollouts=1, max_api_workers=1, analysis_env_context=True)
    assert analysis_env_context(_Env(), off) == ""
    assert analysis_env_context(_Env(), on) == "DESC"
    assert analysis_env_context(object(), on) == ""


if __name__ == "__main__":
    import sys
    import traceback

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except Exception:
            failed += 1
            print(f"  FAIL  {fn.__name__}")
            traceback.print_exc()
    print(f"{len(fns) - failed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
