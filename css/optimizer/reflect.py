"""L0 Reflect stage — per-minibatch trajectory analysis into raw edit patches.

This is step (1) of the L0 EXPLOITATION inner loop (design §4.1 / D7). The
epoch's trajectories (collected by Phase 2 ``batch_rollout``) are split into
minibatches and analysed *failures and successes separately*. Each minibatch
becomes one optimizer call whose job is to propose tactical edits to
``rules.md`` (the editable, tactical WHAT-to-do document) while respecting a
FIXED cognitive ``strategy.md`` injected as read-only context.

Adapted from SkillOpt ``skillopt/gradient/reflect.py`` (minibatch split +
analyst-prompt construction + dispatcher) and ``skillopt/utils/json_utils.py``
(robust JSON extraction from noisy LLM output). We deliberately do NOT port
SkillOpt's lossy ``fmt_trajectory`` 500/800/2000-char clips
(``reflect.py:49-102``); per design D7 we render trajectories through
``css.trajectory.format_trajectory`` with ``cfg.tool_trunc`` so only oversized
single tool payloads are elided.

The output of each analyst call is a ``RawPatch`` carrying provenance
(``source_type`` and ``batch_size``); the hierarchical aggregation stage
(``css/optimizer/aggregate.py``) merges these into a single ``Patch`` with
``support_count``.

All SkillOpt imports are avoided here — this module depends only on Phase 1/2
CSS types so it imports cleanly and is fully exercisable under
``StubLLMClient``.
"""
from __future__ import annotations

import json
import random
import re
from typing import TYPE_CHECKING

from css.data.edit import EDIT_OPS, Edit, Patch, RawPatch
from css.model.json_repair import complete_optimizer_json
from css.trajectory import format_trajectory

if TYPE_CHECKING:
    from css.config import CSSConfig
    from css.data.rollout import TaskResult, TaskRolloutGroup
    from css.data.step_buffer import StepBuffer
    from css.model.client import LLMClient


# ── Minibatch splitting ──────────────────────────────────────────────────────


def split_minibatches(
    results: list["TaskResult"], minibatch_size: int
) -> tuple[list[list["TaskResult"]], list[list["TaskResult"]]]:
    """Partition rollouts into failure / success minibatches.

    Trajectories are separated by ``TaskResult.passed`` (failures first), then
    each class is chunked into minibatches of at most ``minibatch_size`` while
    preserving input order (deterministic; no shuffle — the caller controls
    ordering / seeding upstream).

    Returns ``(failure_batches, success_batches)``. A ``minibatch_size <= 0`` is
    treated as a single batch per class (never zero-width chunks).
    """
    failures = [r for r in results if not r.passed]
    successes = [r for r in results if r.passed]
    return _chunk(failures, minibatch_size), _chunk(successes, minibatch_size)


def _chunk(items: list["TaskResult"], size: int) -> list[list["TaskResult"]]:
    if not items:
        return []
    if size <= 0:
        return [list(items)]
    return [items[i : i + size] for i in range(0, len(items), size)]


# ── Prompt construction ──────────────────────────────────────────────────────


_SYSTEM_FAILURE = """\
You are the L0 tactical optimizer in a Cognitive Strategy Search system.

Your single responsibility is to improve a document called `rules.md` for a
FROZEN task agent. `rules.md` holds tactical, concrete WHAT-to-do guidance:
actionable rules, checklists, gotchas, and procedures the agent consults while
solving tasks. You edit ONLY `rules.md`.

You are given `strategy.md` as READ-ONLY context. It encodes the agent's FIXED
cognitive strategy (HOW the agent thinks / decomposes problems). You MUST NOT
contradict, restate, or attempt to change the strategy. Your edits must operate
strictly within it — make the tactical layer better at executing the existing
strategy, never propose a different strategy.

You are analyzing a minibatch of FAILED trajectories. Diagnose the RECURRING
tactical failure patterns across these trajectories — mistakes that better
`rules.md` guidance would have prevented. Do not over-fit to a single idiosyncratic
trajectory; prefer patterns that appear in more than one trajectory.

Output a JSON list of edits to apply to `rules.md`. Each edit is an object:
  {"op": <one of append|insert_after|replace|delete|add_section|rewrite_section|delete_section>,
   "target": <anchor text in rules.md for insert_after/replace/delete, OR the
              ### heading text for rewrite_section/delete_section; ignored for
              append/add_section>,
   "content": <text to add or substitute; ignored for delete/delete_section>,
   "reason": <one concise sentence on the failure pattern this addresses>}

Edit op semantics on the free-form `rules.md` text:
  - append:          add `content` at the end of rules.md.
  - insert_after:    insert `content` on the line after the first line containing `target`.
  - replace:         replace the first occurrence of `target` with `content`.
  - delete:          remove the first occurrence of `target`.
  - add_section:     append a new `### Section` with its content at the end of rules.md.
  - rewrite_section: locate the `###` section whose heading contains `target` and
                     replace its entire content (heading through next heading) with `content`.
  - delete_section:  remove the entire `###` section whose heading contains `target`.

Rules for good edits:
  - Be specific and actionable; write rules the agent can mechanically follow.
  - Keep edits minimal and targeted; do not rewrite unrelated guidance.
  - Do NOT re-propose any edit listed under "Previously rejected edits" — those
    were already tried and failed the acceptance gate; pick a different angle.
  - Directly address every item under "Recent unresolved failure patterns".
  - If you genuinely have no high-confidence improvement, return an empty list [].

Edit granularity — rules.md organization:
  - rules.md is organized as `###` sections. Each section contains free-form
    markdown (paragraphs, bullets, sub-bullets, code blocks, etc.).
  - PREFER section-level ops (add_section / rewrite_section / delete_section) for
    adding new thematic guidance or substantially reworking existing guidance.
  - Use line-level ops (append / insert_after / replace / delete) for small,
    surgical changes within an existing section.
  - Before proposing an edit, check if the same guidance already exists in
    rules.md under different wording. If so, use `rewrite_section` or `replace`
    to improve the existing section instead of adding a duplicate.

Respond with ONLY the JSON list — no prose, no markdown fences, no commentary."""


_SYSTEM_SUCCESS = """\
You are the L0 tactical optimizer in a Cognitive Strategy Search system.

Your single responsibility is to improve a document called `rules.md` for a
FROZEN task agent. `rules.md` holds tactical, concrete WHAT-to-do guidance:
actionable rules, checklists, gotchas, and procedures the agent consults while
solving tasks. You edit ONLY `rules.md`.

You are given `strategy.md` as READ-ONLY context. It encodes the agent's FIXED
cognitive strategy (HOW the agent thinks / decomposes problems). You MUST NOT
contradict, restate, or attempt to change the strategy. Your edits must operate
strictly within it.

You are analyzing a minibatch of SUCCESSFUL trajectories. Identify the RECURRING
tactical behaviors that drove these successes — effective techniques, ordering,
verification habits — and propose edits that CODIFY them into `rules.md` so the
agent reliably reproduces them on future tasks. Prefer patterns shared across
multiple trajectories over one-off lucky moves.

Output a JSON list of edits to apply to `rules.md`. Each edit is an object:
  {"op": <one of append|insert_after|replace|delete|add_section|rewrite_section|delete_section>,
   "target": <anchor text in rules.md for insert_after/replace/delete, OR the
              ### heading text for rewrite_section/delete_section; ignored for
              append/add_section>,
   "content": <text to add or substitute; ignored for delete/delete_section>,
   "reason": <one concise sentence on the success pattern this codifies>}

Edit op semantics on the free-form `rules.md` text:
  - append:          add `content` at the end of rules.md.
  - insert_after:    insert `content` on the line after the first line containing `target`.
  - replace:         replace the first occurrence of `target` with `content`.
  - delete:          remove the first occurrence of `target`.
  - add_section:     append a new `### Section` with its content at the end of rules.md.
  - rewrite_section: locate the `###` section whose heading contains `target` and
                     replace its entire content (heading through next heading) with `content`.
  - delete_section:  remove the entire `###` section whose heading contains `target`.

Rules for good edits:
  - Be specific and actionable; codify the winning behavior, not a vague platitude.
  - Keep edits minimal and targeted; do not rewrite unrelated guidance.
  - Do NOT re-propose any edit listed under "Previously rejected edits".
  - If the current rules already capture the winning behavior, return an empty list [].

Edit granularity — rules.md organization:
  - rules.md is organized as `###` sections. Each section contains free-form
    markdown (paragraphs, bullets, sub-bullets, code blocks, etc.).
  - PREFER section-level ops (add_section / rewrite_section / delete_section) for
    adding new thematic guidance or substantially reworking existing guidance.
  - Use line-level ops (append / insert_after / replace / delete) for small,
    surgical changes within an existing section.
  - Before proposing an edit, check if the same guidance already exists in
    rules.md under different wording. If so, use `rewrite_section` or `replace`
    to improve the existing section instead of adding a duplicate.

Respond with ONLY the JSON list — no prose, no markdown fences, no commentary."""


def _render_minibatch(minibatch: list["TaskResult"], tool_trunc: int) -> str:
    """Render a minibatch of trajectories into analyst-readable text.

    Each trajectory gets a header (task id / type / description / outcome) and
    its conversation rendered via :func:`css.trajectory.format_trajectory` —
    the only D7-sanctioned truncation path.
    """
    parts: list[str] = []
    for idx, r in enumerate(minibatch, 1):
        header_lines = [
            f"### Trajectory {idx} (task_id={r.task_id}, rollout={r.rollout_index})",
        ]
        if r.task_type:
            header_lines.append(f"Task type: {r.task_type}")
        if r.task_description:
            header_lines.append(f"Task: {r.task_description}")
        outcome = "PASS" if r.passed else "FAIL"
        header_lines.append(
            f"Outcome: {outcome} (hard={r.hard}, soft={r.soft:.3f}, "
            f"cases={r.n_pass}/{r.n_cases}, turns={r.n_turns})"
        )
        if r.fail_reason:
            header_lines.append(f"Failure reason: {r.fail_reason}")
        header = "\n".join(header_lines)
        traj = format_trajectory(r.messages, tool_trunc=tool_trunc, include_system=False)
        parts.append(f"{header}\n\n{traj}")
    return "\n\n---\n\n".join(parts)


def _render_rejected_edits(edits: list["Edit"]) -> str:
    """Compact, de-duplicated rendering of recently rejected edits."""
    seen: set[str] = set()
    lines: list[str] = []
    for e in edits:
        key = _edit_norm_key(e)
        if key in seen:
            continue
        seen.add(key)
        target = (e.target or "").strip().replace("\n", " ")
        content = (e.content or "").strip().replace("\n", " ")
        if len(target) > 120:
            target = target[:120] + "..."
        if len(content) > 200:
            content = content[:200] + "..."
        piece = f"- op={e.op}"
        if target:
            piece += f" | target={target!r}"
        if content:
            piece += f" | content={content!r}"
        lines.append(piece)
    return "\n".join(lines)


def _edit_norm_key(e: "Edit") -> str:
    return "".join(
        (
            (e.op or "").strip().lower(),
            (e.target or "").strip(),
            (e.content or "").strip(),
        )
    )


def _extract_section_index(rules: str) -> str:
    """Extract section headings from rules.md as a structural index for the optimizer.

    rules.md is organized as ``###`` sections (each rendered under the ``## Rules``
    heading in the final skill document), so the index keys on ``###`` headings.
    """
    if not rules or not rules.strip():
        return ""
    headings = [line.strip() for line in rules.split('\n') if line.strip().startswith('### ')]
    if not headings:
        return ""
    indexed = '\n'.join(f'  {i+1}. "{h}"' for i, h in enumerate(headings))
    return indexed


def build_l0_prompt(
    strategy: str,
    rules: str,
    minibatch: list["TaskResult"],
    step_buffer: "StepBuffer",
    *,
    source_type: str,
    tool_trunc: int = 8000,
    recent_window: int = 10,
) -> tuple[str, str]:
    """Build the (system, user) prompt for one minibatch analyst call.

    ``source_type`` selects the failure vs success system role. The user message
    injects: the READ-ONLY ``strategy.md``, the editable ``rules.md`` (edit
    target), the step_buffer's recent failure patterns + rejected edits (so the
    optimizer avoids dead ends), and the rendered minibatch trajectories.
    """
    system = _SYSTEM_SUCCESS if source_type == "success" else _SYSTEM_FAILURE

    traj_label = "SUCCESSFUL" if source_type == "success" else "FAILED"
    trajectories_text = _render_minibatch(minibatch, tool_trunc)

    rejected = step_buffer.recent_rejected_edits(recent_window) if step_buffer else []
    failure_patterns = (
        step_buffer.recent_failure_patterns(recent_window) if step_buffer else []
    )

    sections: list[str] = []
    sections.append(
        "## strategy.md (READ-ONLY — the FIXED cognitive strategy; do NOT contradict it)\n"
        + (strategy.strip() if strategy and strategy.strip() else "(empty)")
    )
    sections.append(
        "## rules.md (EDIT TARGET — your edits apply to this exact text)\n"
        + (rules if rules and rules.strip() else "(empty — rules.md has no content yet)")
    )

    section_index = _extract_section_index(rules)
    if section_index:
        sections.append(
            "## rules.md section index (use these headings as `target` for insert_after)\n"
            + section_index
        )

    if failure_patterns:
        fp_text = "\n".join(f"- {p}" for p in failure_patterns)
        sections.append(
            "## Recent unresolved failure patterns (address these directly)\n" + fp_text
        )

    if rejected:
        sections.append(
            "## Previously rejected edits (do NOT re-propose these — they failed the gate)\n"
            + _render_rejected_edits(rejected)
        )

    sections.append(
        f"## {traj_label} Trajectories ({len(minibatch)} total)\n"
        + (trajectories_text if trajectories_text.strip() else "(no trajectories)")
    )

    sections.append(
        "## Your task\n"
        "Analyze the trajectories above and output a JSON list of edits to "
        "`rules.md` as specified in your instructions. Output ONLY the JSON list."
    )

    user = "\n\n".join(sections)
    return system, user


# ── Robust JSON edit-list extraction ─────────────────────────────────────────


def _parse_edit_list(text: str) -> list[dict]:
    """Extract a JSON list of edit dicts from noisy LLM output.

    Tolerates: a bare JSON array, an array wrapped in a ```json fence, or an
    object that contains the edits under ``"edits"`` / ``"patch"`` (in which
    case the inner list is used). Returns ``[]`` when nothing parseable is
    found rather than raising — a malformed analyst response simply yields no
    edits for that minibatch.
    """
    if not text:
        return []

    candidates: list[str] = []
    # 1) fenced ```json ... ``` block (array or object)
    for m in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.DOTALL):
        candidates.append(m.group(1).strip())
    # 2) the whole text (already-clean array/object)
    candidates.append(text.strip())
    # 3) first bare [...] array
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        candidates.append(m.group(0))
    # 4) first bare {...} object
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        candidates.append(m.group(0))

    for cand in candidates:
        if not cand:
            continue
        try:
            obj = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        edits = _coerce_edit_list(obj)
        if edits is not None:
            return edits
    return []


def _coerce_edit_list(obj) -> list[dict] | None:
    """Coerce a parsed JSON value into a list of edit dicts, or ``None``."""
    if isinstance(obj, list):
        return [e for e in obj if isinstance(e, dict)]
    if isinstance(obj, dict):
        for key in ("edits", "patch"):
            inner = obj.get(key)
            if isinstance(inner, dict):  # patch may be {"edits": [...]}
                inner = inner.get("edits")
            if isinstance(inner, list):
                return [e for e in inner if isinstance(e, dict)]
        # A single edit object emitted bare.
        if obj.get("op") in EDIT_OPS:
            return [obj]
    return None


def _edit_from_dict(d: dict, source_type: str) -> "Edit | None":
    """Build a validated :class:`Edit` from an analyst dict, tagging provenance."""
    op = str(d.get("op", "")).strip().lower()
    if op not in EDIT_OPS:
        return None
    edit = Edit(
        op=op,  # type: ignore[arg-type]
        content=str(d.get("content", "") or ""),
        target=str(d.get("target", "") or ""),
        source_type=source_type if source_type in ("failure", "success", "contrastive", "synthesized") else "failure",
        reason=str(d.get("reason", "") or d.get("rationale", "") or ""),
    )
    # An edit that needs an anchor but has none, and is not an append, is junk.
    if edit.op in ("insert_after", "replace", "delete") and not edit.target:
        return None
    return edit


# ── Analyst dispatch ─────────────────────────────────────────────────────────


def run_minibatch_analyst(
    client: "LLMClient",
    strategy: str,
    rules: str,
    minibatch: list["TaskResult"],
    step_buffer: "StepBuffer",
    *,
    source_type: str,
    cfg: "CSSConfig",
) -> "RawPatch":
    """Run one optimizer call over a minibatch, returning a :class:`RawPatch`.

    Builds the prompt via :func:`build_l0_prompt`, calls
    ``client.complete_optimizer``, robustly parses the JSON edit list, and wraps
    the resulting edits in a ``Patch`` (each ``Edit.source_type=source_type``).
    Never raises on a malformed analyst response — it yields an empty patch.
    """
    system, user = build_l0_prompt(
        strategy,
        rules,
        minibatch,
        step_buffer,
        source_type=source_type,
        tool_trunc=cfg.tool_trunc,
        recent_window=cfg.W,
    )
    try:
        raw_edits = complete_optimizer_json(
            client, system, user, parse=_parse_edit_list, stage="reflect",
        )
    except Exception:  # noqa: BLE001 — an analyst failure must not crash the loop
        raw_edits = []

    edits: list[Edit] = []
    for d in raw_edits:
        edit = _edit_from_dict(d, source_type)
        if edit is not None:
            edits.append(edit)

    patch = Patch(edits=edits)
    return RawPatch(patch=patch, source_type=source_type, batch_size=len(minibatch))


def reflect_epoch(
    client: "LLMClient",
    strategy: str,
    rules: str,
    results: list["TaskResult"],
    step_buffer: "StepBuffer",
    *,
    cfg: "CSSConfig",
) -> list["RawPatch"]:
    """Run the full Reflect stage over an epoch's rollouts.

    Dispatches to one of three pipelines based on ``cfg.reflect_mode``:

      * ``"legacy"`` — original flat fail/success split (no per-task grouping).
      * ``"plan_a"`` — three-way analysis → unified edit_generator (two-stage).
      * ``"plan_b"`` — success insights as context injection into fail/contrastive.
    """
    mode = getattr(cfg, "reflect_mode", "legacy")
    if mode == "plan_a":
        return _reflect_epoch_plan_a(client, strategy, rules, results, step_buffer, cfg=cfg)
    elif mode == "plan_b":
        return _reflect_epoch_plan_b(client, strategy, rules, results, step_buffer, cfg=cfg)
    return _reflect_epoch_legacy(client, strategy, rules, results, step_buffer, cfg=cfg)


def _reflect_epoch_legacy(
    client: "LLMClient",
    strategy: str,
    rules: str,
    results: list["TaskResult"],
    step_buffer: "StepBuffer",
    *,
    cfg: "CSSConfig",
) -> list["RawPatch"]:
    """Original flat fail/success split (no per-task grouping)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    failure_batches, success_batches = split_minibatches(results, cfg.minibatch_size)

    tasks: list[tuple[list, str]] = []
    for batch in failure_batches:
        tasks.append((batch, "failure"))
    for batch in success_batches:
        tasks.append((batch, "success"))

    max_workers = getattr(cfg, "max_api_workers", 32)

    def _run_one(batch_and_type):
        batch, source_type = batch_and_type
        return run_minibatch_analyst(
            client, strategy, rules, batch, step_buffer,
            source_type=source_type, cfg=cfg,
        )

    raw_patches: list[RawPatch] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_run_one, t) for t in tasks]
        for fut in as_completed(futures):
            raw_patches.append(fut.result())
    return raw_patches


# ═══════════════════════════════════════════════════════════════════════════════
# Per-task grouping utilities (shared by plan_a and plan_b)
# ═══════════════════════════════════════════════════════════════════════════════

def _group_by_task(results: list["TaskResult"]) -> list["TaskRolloutGroup"]:
    """Group flat rollout results into per-task groups."""
    from css.data.rollout import group_rollouts
    return group_rollouts(results)


def _triage_groups(
    groups: list["TaskRolloutGroup"],
) -> tuple[list["TaskRolloutGroup"], list["TaskRolloutGroup"], list["TaskRolloutGroup"]]:
    """Split groups into (pure_pass, pure_fail, mixed)."""
    pure_pass: list["TaskRolloutGroup"] = []
    pure_fail: list["TaskRolloutGroup"] = []
    mixed: list["TaskRolloutGroup"] = []
    for g in groups:
        if not g.rollouts:
            continue
        if g.is_persistent_fail():
            pure_fail.append(g)
        elif not g.failures:
            pure_pass.append(g)
        else:
            mixed.append(g)
    return pure_pass, pure_fail, mixed


# ═══════════════════════════════════════════════════════════════════════════════
# Success analyst — produces SuccessInsights (structured, no edits)
# ═══════════════════════════════════════════════════════════════════════════════

_SYSTEM_SUCCESS_ANALYST = """\
You are analyzing SUCCESSFUL agent execution trajectories. Your job is to deeply understand WHY these trajectories succeeded — what decisions, strategies, techniques, and adaptations the agent employed that drove task completion.

You are given `strategy.md` (the agent's high-level cognitive strategy) and `rules.md` (current tactical rules). Use these as context to understand the agent's operating framework, but do NOT organize your analysis around them. Analyze what the agent ACTUALLY DID, not what rules it was supposed to follow.

ANALYSIS DEPTH — mine each trajectory at multiple levels:

1. COGNITIVE LEVEL: How did the agent THINK about the problem? What mental model did it construct? How did it decompose the task? What assumptions drove its planning?

2. BEHAVIORAL LEVEL: What APPROACH did the agent take? How did it sequence its actions? How did it adapt when encountering unexpected situations? What verification or error-recovery strategies did it use?

3. TECHNICAL LEVEL: What specific IMPLEMENTATION choices did it make? Which tools/methods/APIs did it use and why? What data handling techniques proved effective?

For every observation:
- Ground it in SPECIFIC EVIDENCE from the trajectory (quote the agent's reasoning, cite specific actions and their outcomes)
- Explain the CAUSAL MECHANISM — why did this contribute to success? What would have gone wrong without it?
- Assess ROBUSTNESS — is this a reliable pattern, or did it work here due to specific input characteristics?

When analyzing multiple trajectories, identify both per-task insights and cross-task patterns. Patterns recurring across tasks are especially informative.

Your output is the ONLY window the downstream consumer has into these trajectories — they will NOT see the original executions. Describe what happened with sufficient detail to preserve essential information, and provide thorough analysis of why it matters.

OUTPUT FORMAT:

Return a JSON object with exactly one key. Depth and specificity are valued over brevity.

{
  "success_factors": [
    {
      "factor": "<one sentence: what the agent did/decided/adapted that drove success>",
      "analysis": "<thorough analysis: why this contributed to success, the causal mechanism, what would have gone wrong without it>",
      "evidence": "<specific quotes from agent reasoning, concrete actions taken and their outcomes, decision points cited from the trajectory>",
      "tasks": ["task_001", "task_015"],
      "robustness": "<how reliable is this factor across different inputs/conditions, what assumptions does it depend on>"
    }
  ]
}

Output ONLY the JSON object — no prose, no markdown fences, no commentary."""


def _run_success_analyst(
    client: "LLMClient",
    strategy: str,
    rules: str,
    pure_pass_groups: list["TaskRolloutGroup"],
    *,
    tool_trunc: int = 8000,
) -> str:
    """Analyze pure-pass groups and return SuccessInsights as JSON text.

    Returns the raw JSON string for injection into downstream prompts.
    On any failure, returns an empty string (graceful degradation).
    """
    if not pure_pass_groups:
        return ""

    # Render one representative rollout per pure-pass task (the first one).
    parts: list[str] = []
    for g in pure_pass_groups:
        r = g.rollouts[0]
        header = f"### Task {r.task_id} (K={len(g.rollouts)}, all PASS, soft_mean={g.mean_soft:.3f})"
        if r.task_description:
            header += f"\nTask: {r.task_description}"
        traj = format_trajectory(r.messages, tool_trunc=tool_trunc, include_system=False)
        parts.append(f"{header}\n\n{traj}")

    trajectories_text = "\n\n---\n\n".join(parts)

    sections: list[str] = [
        "## strategy.md (READ-ONLY)\n" + (strategy.strip() or "(empty)"),
        "## rules.md (current tactical rules)\n"
        + (rules.strip() if rules and rules.strip() else "(empty)"),
        f"## SUCCESSFUL Trajectories ({len(pure_pass_groups)} tasks, all K rollouts passed)\n"
        + trajectories_text,
        "## Your task\n"
        "Analyze the successful trajectories and produce the JSON object "
        "described in your instructions. Output ONLY the JSON object.",
    ]

    user = "\n\n".join(sections)
    try:
        text, _usage = client.complete_optimizer(_SYSTEM_SUCCESS_ANALYST, user, max_tokens=8192)
    except Exception:
        return ""
    return text.strip() if text else ""


# ═══════════════════════════════════════════════════════════════════════════════
# Contrastive analyst — per-task (success vs failure) edit generation
# ═══════════════════════════════════════════════════════════════════════════════

_SYSTEM_CONTRASTIVE = """\
You are the L0 contrastive tactical optimizer in a Cognitive Strategy Search system.

Your single responsibility is to improve `rules.md` for a FROZEN task agent. \
You are given `strategy.md` as READ-ONLY context.

You are analyzing a MIXED task — the same task was run K times, and some rollouts \
PASSED while others FAILED. Because the task, instruction, and skill document \
are identical across rollouts, any difference in outcome comes from differences \
in the agent's run-time decisions. This is the strongest signal for targeted \
rule improvements.

Your analysis MUST:
  1. Identify the DECISIVE behavioral difference between the passing and \
     failing rollouts — trace what each run actually did at the critical moment.
  2. Propose edits to `rules.md` that CODIFY the successful behavior and \
     PREVENT the failing behavior. Each edit should directly address the \
     divergence you found.

Output a JSON list of edits:
  {"op": <append|insert_after|replace|delete>,
   "target": <anchor text in rules.md; required for insert_after/replace/delete>,
   "content": <text to add or substitute>,
   "reason": <the behavioral divergence this edit addresses>}

Edit rules: be specific, one rule per edit, no section headings in content, \
check for existing similar rules before adding. If no high-confidence edit \
exists, return [].

Respond with ONLY the JSON list — no prose, no markdown fences, no commentary."""


def _render_contrastive_group(group: "TaskRolloutGroup", tool_trunc: int) -> str:
    """Render a mixed-task group showing success vs failure rollouts."""
    parts: list[str] = []

    parts.append(f"Task ID: {group.task_id}  |  "
                 f"Pass rate: {group.pass_rate:.0%} ({len(group.successes)}P / {len(group.failures)}F)")

    for label, rollouts in [("PASSING", group.successes), ("FAILING", group.failures)]:
        for r in rollouts:
            header = f"### {label} rollout (rollout_index={r.rollout_index}, soft={r.soft:.3f})"
            if r.task_description:
                header += f"\nTask: {r.task_description}"
            if r.fail_reason:
                header += f"\nFail reason: {r.fail_reason}"
            traj = format_trajectory(r.messages, tool_trunc=tool_trunc, include_system=False)
            parts.append(f"{header}\n\n{traj}")

    return "\n\n---\n\n".join(parts)


# ═══════════════════════════════════════════════════════════════════════════════
# Plan A: three-way analysis → unified edit_generator (two-stage)
# ═══════════════════════════════════════════════════════════════════════════════

_SYSTEM_FAILURE_ANALYST_A = """\
You are analyzing FAILED agent execution trajectories. Your job is to deeply understand WHY these trajectories failed — what decisions, assumptions, or missing capabilities led to task failure.

You are given `strategy.md` and `rules.md` as context for understanding the agent's operating framework. Analyze what the agent ACTUALLY DID and where it went wrong, not merely which rules it violated.

ANALYSIS DEPTH — diagnose each failure thoroughly:

1. ROOT CAUSE vs SYMPTOM: Distinguish the surface-level error (wrong output, crash, timeout) from the underlying cause (wrong mental model, missing verification step, incorrect assumption about the data). Always dig to the root.

2. FAILURE CHAIN: Trace how the failure unfolded step by step. Identify the CRITICAL DECISION POINT — the earliest moment where a different choice would have changed the outcome. Often one wrong decision cascades into subsequent errors; map this chain.

3. RECOVERY BEHAVIOR: If the agent detected an error and attempted recovery, analyze why the recovery failed. Was the recovery strategy itself flawed? Did the agent give up too early? Did it misdiagnose the problem?

4. WHAT SHOULD DIFFER: Describe what the agent should have done differently at the behavioral level. Not as a rule to write, but as a concrete alternative action or strategy at the point of failure.

5. SYSTEMATIC vs INCIDENTAL: When multiple tasks fail, determine whether they share a common failure mechanism or fail for independent reasons. Shared mechanisms are especially important to surface.

Ground every diagnosis in SPECIFIC EVIDENCE from the trajectory. Assess whether each failure reflects a CORRECTABLE behavioral issue or a FUNDAMENTAL capability gap.

Your output is the ONLY window the downstream consumer has into these trajectories — they will NOT see the original executions. Describe what happened with sufficient detail to preserve essential information, and provide thorough analysis of why it matters.

OUTPUT FORMAT:

Return a JSON object. Depth and specificity are valued over brevity.

{
  "failure_diagnoses": [
    {
      "diagnosis": "<one sentence: what went wrong and the root cause>",
      "analysis": "<thorough analysis: failure chain from initial wrong decision to final failure, critical decision point, recovery attempts and why they failed, what should have been done differently>",
      "evidence": "<specific quotes from agent reasoning, concrete actions that led to failure, error messages and agent responses>",
      "tasks": ["task_002", "task_019"],
      "severity": "correctable | fundamental_gap"
    }
  ]
}

Output ONLY the JSON object — no prose, no markdown fences, no commentary."""


_SYSTEM_CONTRASTIVE_ANALYST_A = """\
You are performing CONTRASTIVE ANALYSIS on agent execution trajectories. For each task, you are given multiple rollouts of the SAME task under the SAME rules — some succeeded, some failed. Your job is to deeply analyze WHY the same agent, facing the same task, produced different outcomes.

This is the most powerful signal available: same task, same rules, different results. The difference must lie in the agent's specific decisions, action sequences, or environmental interactions during execution.

You are given `strategy.md` and `rules.md` as context.

ANALYSIS FOCUS:

1. DIVERGENCE POINT: Identify the earliest moment where the successful and failing rollouts took different paths. What did the successful rollout do that the failing one did not, or vice versa? There may be multiple divergence points — identify all significant ones.

2. PATH COMPARISON: Trace what happened AFTER the divergence in each path. How did the successful path's choice lead to task completion? How did the failing path's choice cascade into failure?

3. CAUSE OF DIVERGENCE: Why did the agent make different choices across rollouts? Was it due to stochastic variation in reasoning? Different initial interpretations of the task? Different tool outputs or error encounters? Understanding the SOURCE of divergence is as important as the divergence itself.

4. DETERMINISTIC vs STOCHASTIC: Assess whether the success was reliably reproducible (2/3 or 3/3 pass) or a lucky outlier (1/3 pass). This informs how learnable the success pattern is.

Your output is the ONLY information the downstream consumer has about these trajectories — they will NOT see the original executions. Therefore:
- `divergence`: Describe with full context WHERE and HOW the trajectories diverged — what situation the agent was facing, what choice it made in the successful vs failing rollout, and what immediately followed.
- `success_path` / `failure_path`: Provide a faithful, detailed account of what the agent did at and after the divergence. Include the agent's reasoning, specific actions, tool outputs, and how the path unfolded toward success or failure. A reader who has never seen the trajectory should be able to follow the narrative.
- `analysis`: Go beyond description — explain WHY the divergence occurred, WHY it led to different outcomes, and how deterministic vs stochastic the success pattern is.
- `evidence`: Cite specific quotes and actions from both rollouts to make the contrast concrete.

Ground every observation in SPECIFIC EVIDENCE — quote from both the successful and failing rollouts.

OUTPUT FORMAT:

{
  "contrastive_analyses": [
    {
      "task_id": "task_005",
      "pass_rate": "2/3",
      "divergence": "<where and how the trajectories diverged, with full context>",
      "success_path": "<detailed account of what the successful rollout(s) did at and after the divergence>",
      "failure_path": "<detailed account of what the failing rollout(s) did at and after the divergence>",
      "analysis": "<why the divergence occurred, why it led to different outcomes, how deterministic vs stochastic the success is>",
      "evidence": "<specific quotes/actions from both passing and failing rollouts>"
    }
  ]
}

Output ONLY the JSON object — no prose, no markdown fences, no commentary."""


_SYSTEM_EDIT_GENERATOR_A = """\
You are an edit generator in a skill optimization system. Your job is to propose concrete edits to `rules.md` that will improve the agent's task performance.

You are given:
- `strategy.md`: the agent's high-level cognitive strategy (READ-ONLY, do not propose changes to this)
- `rules.md`: the current tactical rules (your edit target)
- Analytical reports from trajectory analysts: success factors from successful executions, failure diagnoses from failed executions, and contrastive analyses comparing successful vs failed rollouts of the same task

Based on these analytical inputs, propose edits to `rules.md` that:
1. ADDRESS diagnosed failure patterns — add or modify rules that would prevent the observed failure modes
2. CODIFY effective behaviors — if the analysts identified success factors not yet captured in rules, consider adding them
3. PRESERVE what works — do not propose changes that would suppress validated success behaviors
4. MAINTAIN COHERENCE — new rules should be consistent with existing rules and with `strategy.md`

Each edit must specify:
- `op`: one of "append", "insert_after", "replace", "delete", "add_section", "rewrite_section", "delete_section"
- `target`: for insert_after/replace/delete, the anchor text being targeted; for rewrite_section/delete_section, the `###` heading text to locate (first 80 chars to identify it); ignored for append/add_section
- `content`: the new or replacement text (for append/insert_after/replace/add_section/rewrite_section)
- `rationale`: why this edit will improve performance, grounded in the analytical evidence you received

Edit op semantics on the free-form `rules.md` text:
- append:          add `content` at the end of rules.md.
- insert_after:    insert `content` on the line after the first line containing `target`.
- replace:         replace the first occurrence of `target` with `content`.
- delete:          remove the first occurrence of `target`.
- add_section:     append a new `### Section` with its content at the end of rules.md.
- rewrite_section: locate the `###` section whose heading contains `target` and replace its entire content (heading through next heading) with `content`.
- delete_section:  remove the entire `###` section whose heading contains `target`.

Edit granularity — rules.md organization:
- rules.md is organized as `###` sections. Each section contains free-form markdown (paragraphs, bullets, sub-bullets, code blocks, etc.).
- PREFER section-level ops (add_section / rewrite_section / delete_section) for adding new thematic guidance or substantially reworking existing guidance.
- Use line-level ops (append / insert_after / replace / delete) for small, surgical changes within an existing section.
- Before proposing an edit, check if the same guidance already exists in rules.md under different wording. If so, use `rewrite_section` or `replace` to improve the existing section instead of adding a duplicate.

Prioritize edits that address CORRECTABLE failure patterns with clear analytical support. Avoid speculative edits not grounded in the evidence.

OUTPUT FORMAT:

{
  "edits": [
    {
      "op": "append | insert_after | replace | delete | add_section | rewrite_section | delete_section",
      "target": "<anchor text for insert_after/replace/delete, or the ### heading for rewrite_section/delete_section; omit for append/add_section>",
      "content": "<the new or replacement rule text>",
      "rationale": "<why this edit is proposed, citing specific evidence from the analytical reports>"
    }
  ]
}

Output ONLY the JSON object — no prose, no markdown fences, no commentary."""


# ── Per-minibatch fused proposers (plan_a v2) ────────────────────────────────
# Each minibatch / contrastive unit DIRECTLY proposes a few MINIMAL, single-theme,
# gap-filling edits — the small scope (one ~8-trajectory batch) keeps edits small.
# Cross-minibatch dedup + independence is the merge coordinator's job
# (css.optimizer.aggregate.llm_merge_coordinator). This replaces the v1 two-stage
# "rich analysts -> N whole-document generators", which produced redundant stacked
# playbooks because each global generator wrote a complete document.

# The shared edit-production spec used by all three proposers. The only thing that
# differs per source is the analysis lens and the diagnosis field (below).
_EDIT_SPEC = """\
`rules.md` is the agent's tactical playbook: `###` sections, one theme each,
free-form markdown inside. You are shown the current `rules.md` and its section
index. `strategy.md` is READ-ONLY context — `rules.md` executes that strategy;
never restate or contradict it.

## Edit operations — two tiers
Structural (establish or restructure a theme):
  - add_section     — a new `### Theme` section (the theme is not present yet).
  - rewrite_section — rewrite one existing section in place, keeping its heading
                      (the section is substantially wrong or disorganized).
  - delete_section  — remove an obsolete or harmful section.
Refinement (a small change inside an existing section):
  - insert_after    — add a point after a given spot.
  - replace         — fix a phrase or rule.
  - delete          — remove a line.
  - append          — add at the end (last resort, when no section fits).
Use a structural op to scaffold or restructure a theme; once a relevant section
exists, refine inside it with a refinement op.

## Rules for every edit
- ONE edit = ONE theme. Never bundle multiple themes.
- Gap-fill: add only what is missing, fix only what is wrong. Never restate
  guidance already in `rules.md`; if a section already covers the theme, improve
  it — do not add a duplicate.
- `target` is a SEMANTIC pointer for the apply tool: give the section heading, or
  describe and approximately quote the spot. It is resolved by meaning, so be
  clear — you need not copy exact text. (Omit `target` for add_section / append.)
- Generalizable tactics only; never hardcode task-specific values (file paths,
  cell addresses, expected values, entity names).
- Direct and actionable: address the agent ("When you …, do …"), mechanically
  followable — not commentary.

## Budget
Produce AT MOST L edits; fewer is better; emit an EMPTY list if `rules.md` already
covers this batch."""

_SYSTEM_FAILURE_PROPOSER = """\
You optimize the tactical playbook (`rules.md`) of a frozen task agent. You are
given a SMALL BATCH of FAILED trajectories. Find the systematic mistakes they
share and propose the fewest, smallest edits that would prevent them.

""" + _EDIT_SPEC + """

## Analysis
Identify the 1-3 most COMMON failure mechanisms across the batch — each appearing
in two or more trajectories; ignore one-off quirks. Classify each as one of:
rule_missing | rule_wrong | rule_ignored | data_exploration | code_error | other.

## Output — only this JSON object (no fences, no prose)
{
  "failure_summary": [{"type": "<one of the above>", "count": <int>, "description": "<one line>"}],
  "edits": [{"op": "...", "target": "<omit for add_section/append>", "content": "<markdown, one theme; omit for delete/delete_section>", "rationale": "<the pattern this fixes + which trajectories show it>"}]
}"""

_SYSTEM_SUCCESS_PROPOSER = """\
You optimize the tactical playbook (`rules.md`) of a frozen task agent. You are
given a SMALL BATCH of SUCCESSFUL trajectories. Codify the recurring effective
behaviours that drove them, for any not already in `rules.md`, so the agent
reproduces them reliably.

""" + _EDIT_SPEC + """

## Analysis
Identify the 1-3 winning behaviours SHARED across the batch — each appearing in
two or more trajectories; ignore one-off lucky moves.

## Output — only this JSON object (no fences, no prose)
{
  "success_patterns": [{"count": <int>, "description": "<one line>"}],
  "edits": [{"op": "...", "target": "<omit for add_section/append>", "content": "<markdown, one theme; omit for delete/delete_section>", "rationale": "<the behaviour this codifies + which trajectories show it>"}]
}"""

_SYSTEM_CONTRASTIVE_PROPOSER = """\
You optimize the tactical playbook (`rules.md`) of a frozen task agent. You are
given multiple rollouts of the SAME task under the SAME rules — some passed, some
failed. The difference lies in what the agent did, not in the task: this contrast
is the strongest tactical signal. Codify what the passing rollout did that the
failing one did not.

""" + _EDIT_SPEC + """

## Analysis
Find the decisive DIVERGENCE: what the passing rollout(s) did differently that led
to success while the failing one(s) went wrong.

## Output — only this JSON object (no fences, no prose)
{
  "divergence": "<one line: what the passing rollout did that the failing did not>",
  "edits": [{"op": "...", "target": "<omit for add_section/append>", "content": "<markdown, one theme; omit for delete/delete_section>", "rationale": "<the divergence this codifies, citing both paths>"}]
}"""


def _run_minibatch_proposer(
    client: "LLMClient",
    strategy: str,
    rules: str,
    rollouts: list["TaskResult"],
    system_prompt: str,
    source_type: str,
    *,
    cfg: "CSSConfig",
    contrastive_group: "TaskRolloutGroup | None" = None,
    rejected: "list[Edit] | None" = None,
    failure_pats: "list[str] | None" = None,
) -> "RawPatch":
    """One minibatch/unit → a RawPatch of AT MOST ``cfg.l0_edit_budget`` small edits.

    Fuses analysis + edit-generation in a single call (SkillOpt-style): the small
    scope of one minibatch keeps the proposed edits small and single-theme. Never
    raises — a malformed/failed call yields an empty patch.
    """
    if contrastive_group is not None:
        traj = _render_contrastive_group(contrastive_group, cfg.tool_trunc)
    else:
        traj = _render_minibatch(rollouts, cfg.tool_trunc)

    budget = max(1, int(getattr(cfg, "l0_edit_budget", 3)))
    sections: list[str] = [
        "## strategy.md (READ-ONLY)\n" + (strategy.strip() or "(empty)"),
        "## rules.md (EDIT TARGET — current)\n"
        + (rules.strip() if rules and rules.strip() else "(empty)"),
    ]
    section_index = _extract_section_index(rules)
    if section_index:
        sections.append(
            "## rules.md section index (existing themes — do NOT duplicate these)\n"
            + section_index
        )
    sections.append(
        f"## Edit budget\nProduce AT MOST L={budget} minimal single-theme edits. "
        "Fewer is better; empty list if already covered."
    )
    sections.append("## Trajectories\n" + traj)
    if failure_pats:
        sections.append(
            "## Recent unresolved failure patterns\n"
            + "\n".join(f"- {p}" for p in failure_pats)
        )
    if rejected:
        sections.append(
            "## Previously rejected edits (do NOT re-propose)\n"
            + _render_rejected_edits(rejected)
        )
    user = "\n\n".join(sections)

    try:
        raw_edits = complete_optimizer_json(
            client, system_prompt, user, parse=_parse_edit_list,
            max_tokens=8192, stage="proposer",
        )
    except Exception:  # noqa: BLE001 — a proposer failure must not crash the step
        raw_edits = []

    edits: list[Edit] = []
    for d in raw_edits[:budget]:  # enforce the per-minibatch budget
        edit = _edit_from_dict(d, source_type)
        if edit is not None:
            edits.append(edit)
    return RawPatch(
        patch=Patch(edits=edits), source_type=source_type, batch_size=len(rollouts)
    )


def _reflect_epoch_plan_a(
    client: "LLMClient",
    strategy: str,
    rules: str,
    results: list["TaskResult"],
    step_buffer: "StepBuffer",
    *,
    cfg: "CSSConfig",
) -> list["RawPatch"]:
    """Plan A v2: per-minibatch fused proposers → raw patches (merged downstream).

    Each minibatch of failures, each minibatch of successes, and each contrastive
    (mixed-outcome) task directly emits a few minimal, single-theme, gap-filling
    edits. The small per-minibatch scope keeps edits small; cross-minibatch dedup,
    gap-alignment against the current rules.md, and independence are handled by the
    merge coordinator in the aggregate stage.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from css.tracing import log_event

    groups = _group_by_task(results)
    pure_pass, pure_fail, mixed = _triage_groups(groups)
    max_workers = getattr(cfg, "max_api_workers", 32)
    rejected = step_buffer.recent_rejected_edits(cfg.W) if step_buffer else []
    failure_pats = step_buffer.recent_failure_patterns(cfg.W) if step_buffer else []

    # Build the per-unit task list: (system_prompt, rollouts, source_type, contrastive_group)
    units: list[tuple[str, list, str, object]] = []
    if pure_fail:
        fail_rollouts = [r for g in pure_fail for r in g.rollouts]
        for batch in _chunk(fail_rollouts, cfg.minibatch_size):
            units.append((_SYSTEM_FAILURE_PROPOSER, batch, "failure", None))
    if pure_pass:
        succ_rollouts = [r for g in pure_pass for r in g.rollouts]
        for batch in _chunk(succ_rollouts, cfg.minibatch_size):
            units.append((_SYSTEM_SUCCESS_PROPOSER, batch, "success", None))
    for g in mixed:
        units.append((_SYSTEM_CONTRASTIVE_PROPOSER, g.rollouts, "contrastive", g))

    raw_patches: list[RawPatch] = []
    if not units:
        return raw_patches

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = [
            pool.submit(
                _run_minibatch_proposer, client, strategy, rules, roll, sysp, src,
                cfg=cfg, contrastive_group=cg, rejected=rejected, failure_pats=failure_pats,
            )
            for (sysp, roll, src, cg) in units
        ]
        for fut in as_completed(futs):
            rp = fut.result()
            raw_patches.append(rp)
            log_event("reflect_plan_a_edits", n_edits=len(rp.patch.edits), source=rp.source_type)

    return raw_patches


def _run_analyst_structured(
    client: "LLMClient",
    strategy: str,
    rules: str,
    rollouts: list["TaskResult"],
    system_prompt: str,
    tool_trunc: int,
    *,
    contrastive_group: "TaskRolloutGroup | None" = None,
) -> str:
    """Run a structured analyst (produces JSON insights, not edits). Returns raw text."""
    if contrastive_group is not None:
        trajectories_text = _render_contrastive_group(contrastive_group, tool_trunc)
    else:
        trajectories_text = _render_minibatch(rollouts, tool_trunc)

    sections: list[str] = [
        "## strategy.md (READ-ONLY)\n" + (strategy.strip() or "(empty)"),
        "## rules.md (current)\n" + (rules.strip() if rules and rules.strip() else "(empty)"),
        "## Trajectories\n" + trajectories_text,
        "## Your task\nAnalyze the trajectories above and produce the JSON "
        "object described in your instructions. Output ONLY the JSON.",
    ]
    user = "\n\n".join(sections)
    try:
        text, _usage = client.complete_optimizer(system_prompt, user, max_tokens=8192)
    except Exception:
        return ""
    return text.strip() if text else ""


# ═══════════════════════════════════════════════════════════════════════════════
# Plan B: success insights as context injection into fail/contrastive reflect
# ═══════════════════════════════════════════════════════════════════════════════

def _reflect_epoch_plan_b(
    client: "LLMClient",
    strategy: str,
    rules: str,
    results: list["TaskResult"],
    step_buffer: "StepBuffer",
    *,
    cfg: "CSSConfig",
) -> list["RawPatch"]:
    """Plan B: success insights injected as context into fail/contrastive reflect."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    groups = _group_by_task(results)
    pure_pass, pure_fail, mixed = _triage_groups(groups)
    max_workers = getattr(cfg, "max_api_workers", 32)

    # ── Step 1: run success analyst (can run in parallel with step 2) ───
    success_insights = _run_success_analyst(
        client, strategy, rules, pure_pass, tool_trunc=cfg.tool_trunc,
    ) if pure_pass else ""

    # ── Step 2: failure + contrastive reflect WITH success context ──────
    raw_patches: list[RawPatch] = []

    def _run_failure_batch(batch):
        return _run_minibatch_with_insights(
            client, strategy, rules, batch, step_buffer,
            source_type="failure",
            success_insights=success_insights,
            cfg=cfg,
        )

    def _run_contrastive(group):
        return _run_contrastive_analyst_b(
            client, strategy, rules, group, step_buffer,
            success_insights=success_insights,
            cfg=cfg,
        )

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures: list = []

        # Pure-fail tasks: chunked into minibatches
        fail_rollouts = [r for g in pure_fail for r in g.rollouts]
        for batch in _chunk(fail_rollouts, cfg.minibatch_size):
            futures.append(pool.submit(_run_failure_batch, batch))

        # Mixed tasks: one contrastive call per group
        for g in mixed:
            futures.append(pool.submit(_run_contrastive, g))

        for fut in as_completed(futures):
            raw_patches.append(fut.result())

    return raw_patches


def _run_minibatch_with_insights(
    client: "LLMClient",
    strategy: str,
    rules: str,
    minibatch: list["TaskResult"],
    step_buffer: "StepBuffer",
    *,
    source_type: str,
    success_insights: str,
    cfg: "CSSConfig",
) -> "RawPatch":
    """Like run_minibatch_analyst but injects success_insights into the prompt."""
    system = _SYSTEM_FAILURE

    trajectories_text = _render_minibatch(minibatch, cfg.tool_trunc)
    rejected = step_buffer.recent_rejected_edits(cfg.W) if step_buffer else []
    failure_patterns = step_buffer.recent_failure_patterns(cfg.W) if step_buffer else []

    sections: list[str] = [
        "## strategy.md (READ-ONLY)\n" + (strategy.strip() or "(empty)"),
        "## rules.md (EDIT TARGET)\n"
        + (rules.strip() if rules and rules.strip() else "(empty)"),
    ]
    section_index = _extract_section_index(rules)
    if section_index:
        sections.append("## rules.md section index\n" + section_index)

    if success_insights:
        sections.append(
            "## Success Insights from pure-pass tasks (use as positive reference)\n"
            "The following insights come from tasks where ALL rollouts succeeded. "
            "Use these to guide your edit direction — move failing behavior toward "
            "proven successful behavior. Do NOT replace/delete rules flagged as "
            "high-value.\n\n" + success_insights
        )

    if failure_patterns:
        sections.append(
            "## Recent unresolved failure patterns\n"
            + "\n".join(f"- {p}" for p in failure_patterns)
        )
    if rejected:
        sections.append(
            "## Previously rejected edits (do NOT re-propose)\n"
            + _render_rejected_edits(rejected)
        )
    sections.append(
        f"## FAILED Trajectories ({len(minibatch)} total)\n" + trajectories_text
    )
    sections.append(
        "## Your task\n"
        "Analyze the failed trajectories above, informed by the success insights, "
        "and output a JSON list of edits to `rules.md`. Output ONLY the JSON list."
    )

    user = "\n\n".join(sections)
    try:
        raw_edits = complete_optimizer_json(
            client, system, user, parse=_parse_edit_list, stage="reflect_b",
        )
    except Exception:
        raw_edits = []

    edits: list[Edit] = []
    for d in raw_edits:
        edit = _edit_from_dict(d, source_type)
        if edit is not None:
            edits.append(edit)

    return RawPatch(patch=Patch(edits=edits), source_type=source_type, batch_size=len(minibatch))


def _run_contrastive_analyst_b(
    client: "LLMClient",
    strategy: str,
    rules: str,
    group: "TaskRolloutGroup",
    step_buffer: "StepBuffer",
    *,
    success_insights: str,
    cfg: "CSSConfig",
) -> "RawPatch":
    """Plan B contrastive: per-task GRPO-style analysis, producing edits directly."""
    trajectories_text = _render_contrastive_group(group, cfg.tool_trunc)
    rejected = step_buffer.recent_rejected_edits(cfg.W) if step_buffer else []

    sections: list[str] = [
        "## strategy.md (READ-ONLY)\n" + (strategy.strip() or "(empty)"),
        "## rules.md (EDIT TARGET)\n"
        + (rules.strip() if rules and rules.strip() else "(empty)"),
    ]
    section_index = _extract_section_index(rules)
    if section_index:
        sections.append("## rules.md section index\n" + section_index)

    if success_insights:
        sections.append(
            "## Success Insights (positive reference from pure-pass tasks)\n"
            + success_insights
        )

    if rejected:
        sections.append(
            "## Previously rejected edits (do NOT re-propose)\n"
            + _render_rejected_edits(rejected)
        )
    sections.append(
        f"## MIXED TASK — Same task, different outcomes (task_id={group.task_id})\n"
        + trajectories_text
    )
    sections.append(
        "## Your task\n"
        "Analyze the passing vs failing rollouts of this same task. "
        "Identify the decisive divergence and propose edits to `rules.md` "
        "that codify the successful behavior. Output ONLY the JSON list."
    )

    user = "\n\n".join(sections)
    try:
        raw_edits = complete_optimizer_json(
            client, _SYSTEM_CONTRASTIVE, user, parse=_parse_edit_list,
            stage="contrastive",
        )
    except Exception:
        raw_edits = []

    edits: list[Edit] = []
    for d in raw_edits:
        edit = _edit_from_dict(d, "contrastive")
        if edit is not None:
            edits.append(edit)

    return RawPatch(
        patch=Patch(edits=edits),
        source_type="contrastive",
        batch_size=len(group.rollouts),
    )
