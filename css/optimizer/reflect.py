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
import logging
import random
import re
from typing import TYPE_CHECKING

from css.data.edit import EDIT_OPS, Edit, Patch, RawPatch
from css.model.json_repair import complete_optimizer_json
from css.trajectory import format_trajectory

_log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from css.config import CSSConfig
    from css.data.rollout import TaskResult, TaskRolloutGroup
    from css.data.step_buffer import StepBuffer
    from css.model.client import LLMClient


# ── Minibatch splitting ──────────────────────────────────────────────────────


def _chunk(items: list["TaskResult"], size: int) -> list[list["TaskResult"]]:
    if not items:
        return []
    if size <= 0:
        return [list(items)]
    return [items[i : i + size] for i in range(0, len(items), size)]


# Proposer output reservation (8192 hit its ceiling ~1-1.5% of calls, 2026-07-05).
_PROPOSER_MAX_TOKENS = 12288
# Per-message elision floor for budget decay: typical action/reasoning text is
# shorter than this, so D7's "never clip reasoning/action" holds in practice
# even at the floor; only oversized observations keep shrinking.
_TRUNC_FLOOR = 600
# Fallback chars-per-token when no tokenizer is reachable. MEASURED via vLLM
# /tokenize on real WebArena trajectories (2026-07-06): AXTree transcripts run
# 2.23-2.55 chars/token — far denser than prose. 2.0 overestimates the token
# count, keeping the fallback strictly conservative.
_FALLBACK_CHARS_PER_TOKEN = 2.0


def _count_tokens(client: Any, text: str) -> int:
    """Token count for budget math: EXACT via the client's tokenizer endpoint
    when available, conservative chars-based estimate otherwise. All budget
    decisions run on tokens (user ruling 2026-07-06) — chars-per-token guesses
    mislead by 2x across content types."""
    fn = getattr(client, "count_tokens", None)
    if fn is not None:
        try:
            count = fn(text)
            if isinstance(count, int) and count > 0:
                return count
        except Exception:  # noqa: BLE001 — fall back to the estimate
            pass
    return int(len(text) / _FALLBACK_CHARS_PER_TOKEN) + 1


def _fit_render_to_budget(render, tool_trunc: int, token_budget: int,
                          count) -> "tuple[str, int, bool]":
    """Render a trajectory block, decaying the per-message elision cap until it
    fits ``token_budget``. Returns ``(text, tokens, fitted)``.

    Long-observation envs break the fixed-cap arithmetic: a WebArena minibatch
    (8 traj x ~30 turns of AXTree at tool_trunc=8k) rendered ~250k tokens and
    the proposer call died with HTTP 400 context overflow (2026-07-06 smoke).
    The cap halves down to ``_TRUNC_FLOOR``; the caller decides what to do if
    even the floor render does not fit (e.g. drop trajectories, loudly).
    """
    text = render(tool_trunc)
    tokens = count(text)
    if token_budget <= 0 or tokens <= token_budget:
        return text, tokens, True
    cap = tool_trunc if tool_trunc > 0 else 8_000
    while cap > _TRUNC_FLOOR:
        cap = max(_TRUNC_FLOOR, cap // 2)
        text = render(cap)
        tokens = count(text)
        if tokens <= token_budget:
            _log.info(
                "reflect: trajectory block fit at per-message cap %d "
                "(%d/%d tokens)", cap, tokens, token_budget)
            return text, tokens, True
    return text, tokens, False


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
        locator = (e.subject or e.target or "").strip().replace("\n", " ")
        anchor = (e.anchor or "").strip().replace("\n", " ")
        content = (e.content or "").strip().replace("\n", " ")
        if len(locator) > 120:
            locator = locator[:120] + "..."
        if len(anchor) > 120:
            anchor = anchor[:120] + "..."
        if len(content) > 200:
            content = content[:200] + "..."
        piece = f"- op={e.op}"
        if locator:
            piece += f" | subject={locator!r}"
        if anchor:
            piece += f" | anchor={anchor!r}"
        if content:
            piece += f" | body={content!r}"
        lines.append(piece)
    return "\n".join(lines)


def _edit_norm_key(e: "Edit") -> str:
    return "".join(
        (
            (e.op or "").strip().lower(),
            (e.subject or e.target or "").strip(),
            (e.anchor or "").strip(),
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
        if obj.get("op") in EDIT_OPS or obj.get("kind") in EDIT_OPS:
            return [obj]
    return None


def _edit_from_dict(d: dict, source_type: str) -> "Edit | None":
    """Build a validated :class:`Edit` from an analyst dict, tagging provenance.

    Accepts the v2 vocabulary (op + subject/anchor/body) and the legacy one
    (op + target/content). Degradations preserve content: a refinement op
    without any locator but WITH content demotes to add_point (the apply
    stage appends within/creates the section); only a locator-less,
    content-less edit is junk.
    """
    from css.data.edit import LEGACY_OP_TO_KIND

    op = str(d.get("op", "") or d.get("kind", "")).strip().lower()
    if op not in EDIT_OPS:
        return None
    op = LEGACY_OP_TO_KIND.get(op, op)
    edit = Edit(
        op=op,  # type: ignore[arg-type]
        content=str(d.get("body", "") or d.get("content", "") or ""),
        target=str(d.get("target", "") or ""),
        source_type=source_type if source_type in ("failure", "success", "contrastive", "synthesized") else "failure",
        reason=str(d.get("reason", "") or d.get("rationale", "") or ""),
        subject=str(d.get("subject", "") or ""),
        anchor=str(d.get("anchor", "") or ""),
    )
    # Parse source_tasks provenance from the LLM output.
    source_tasks = d.get("source_tasks", [])
    if not isinstance(source_tasks, list):
        source_tasks = []
    edit.source_tasks = [str(t) for t in source_tasks if t]

    point_ops = ("add_point", "edit_point", "remove_point")
    has_locator = bool(edit.subject or edit.anchor or edit.target)
    if edit.op in point_ops and not has_locator:
        if edit.content.strip():
            edit.op = "add_point"  # content survives; apply appends it
        else:
            return None  # nothing to locate AND nothing to say
    if edit.op in ("remove_point", "remove_section") \
            and not has_locator and not edit.content.strip():
        return None
    return edit


def reflect_epoch(
    client: "LLMClient",
    strategy: str,
    rules: str,
    results: list["TaskResult"],
    step_buffer: "StepBuffer",
    *,
    cfg: "CSSConfig",
) -> list["RawPatch"]:
    """Run the full Reflect stage over an epoch's rollouts."""
    return _reflect_epoch_plan_a(client, strategy, rules, results, step_buffer, cfg=cfg)


# ═══════════════════════════════════════════════════════════════════════════════
# Per-task grouping utilities
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


# ── Per-minibatch fused proposers (plan_a) ─────────────────────────────────


# The shared edit-production spec used by all three proposers. The only thing that
# differs per source is the analysis lens and the diagnosis field (below).
_EDIT_SPEC = """\
`rules.md` is the agent's tactical playbook: `###` sections, one theme each,
free-form markdown inside. You are shown the current `rules.md` and its section
index. `strategy.md` describes the agent's task-solving approach (phase \
structure). It is READ-ONLY. `rules.md` provides execution details within that \
approach — specific techniques, formats, edge cases; never restate or contradict it.

## Edit operations — two tiers
Structural (establish or restructure a theme):
  - add_section     — a new section (the theme is not present yet).
  - rewrite_section — replace one existing section's content, keeping its name
                      (the section is substantially wrong or disorganized).
  - remove_section  — remove an obsolete or harmful section.
Refinement (a small change inside an existing section):
  - add_point       — add a rule inside an existing section.
  - edit_point      — fix a phrase or rule inside an existing section.
  - remove_point    — remove a line from an existing section.
Use a structural op to scaffold or restructure a theme; once a relevant section
exists, refine inside it with a refinement op.

## Edit fields — identity vs position
- `subject` — WHICH section this edit defines or modifies. An IDENTITY, never
  a position. For add_section, invent a descriptive name for the NEW section
  from the edit's own theme (never reuse another section's name). For every
  other op, copy an existing section name from the section index (no `###`
  marker, no numeric prefixes: "Input Parsing", not "2. Input Parsing").
- `anchor` — refinement ops only: a SEMANTIC pointer to the spot inside the
  subject section (describe or approximately quote it; resolved by meaning).
- `body` — the content (for add_*/edit_* ops). NEVER include a markdown
  heading line in the body; the section heading is rendered from `subject`.

## Rules for every edit
- ONE edit = ONE theme. Never bundle multiple themes.
- Gap-fill: add only what is missing, fix only what is wrong. Never restate
  guidance already in `rules.md`; if a section already covers the theme, improve
  it — do not add a duplicate.
- Generalizable tactics only; never hardcode task-specific values (literal
  values, identifiers, or paths specific to a single task).
- Direct and actionable: address the agent ("When you …, do …"), mechanically
  followable — not commentary.

## Budget
Produce AT MOST L edits; fewer is better; emit an EMPTY list if `rules.md` already
covers this batch."""

_SYSTEM_FAILURE_PROPOSER = """\
You optimize the tactical playbook (`rules.md`) of a frozen task agent. The \
agent's approach (`strategy.md`) defines the overall phase structure; your rules \
refine the execution details within those phases. You are
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
  "edits": [{"op": "...", "subject": "<section name — new for add_section, existing otherwise>", "anchor": "<refinement ops only: the spot inside the section>", "body": "<markdown, one theme, no heading lines; omit for remove_*>", "rationale": "<the pattern this fixes + which trajectories show it>", "source_tasks": ["task_id_1", "task_id_2"]}]
}
"source_tasks": list of task_ids from the trajectories above that this edit is derived from."""

_SYSTEM_SUCCESS_PROPOSER = """\
You optimize the tactical playbook (`rules.md`) of a frozen task agent. The \
agent's approach (`strategy.md`) defines the overall phase structure; your rules \
refine the execution details within those phases. You are
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
  "edits": [{"op": "...", "subject": "<section name — new for add_section, existing otherwise>", "anchor": "<refinement ops only: the spot inside the section>", "body": "<markdown, one theme, no heading lines; omit for remove_*>", "rationale": "<the behaviour this codifies + which trajectories show it>", "source_tasks": ["task_id_1", "task_id_2"]}]
}
"source_tasks": list of task_ids from the trajectories above that this edit is derived from."""

_SYSTEM_CONTRASTIVE_PROPOSER = """\
You optimize the tactical playbook (`rules.md`) of a frozen task agent. The \
agent's approach (`strategy.md`) defines the overall phase structure; your rules \
refine the execution details within those phases. You are
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
  "edits": [{"op": "...", "subject": "<section name — new for add_section, existing otherwise>", "anchor": "<refinement ops only: the spot inside the section>", "body": "<markdown, one theme, no heading lines; omit for remove_*>", "rationale": "<the divergence this codifies, citing both paths>", "source_tasks": ["task_id_1", "task_id_2"]}]
}
"source_tasks": list of task_ids from the trajectories above that this edit is derived from."""


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
    if rules and rules.strip():
        n_sections = len([ln for ln in rules.split("\n") if ln.strip().startswith("### ")])
        sections.append(
            f"## rules.md maturity\n"
            f"Sections: {n_sections} | Size: {len(rules)} chars\n"
            f"Prefer REFINEMENTS within existing sections over new sections. "
            f"Only propose a new section for genuinely UNCOVERED failure mechanisms."
        )
    sections.append(
        f"## Edit budget\nProduce AT MOST L={budget} minimal single-theme edits. "
        "Fewer is better; empty list if already covered."
    )
    tail_sections: list[str] = []
    if failure_pats:
        tail_sections.append(
            "## Recent unresolved failure patterns\n"
            + "\n".join(f"- {p}" for p in failure_pats)
        )
    if rejected:
        tail_sections.append(
            "## Previously rejected edits (do NOT re-propose)\n"
            + _render_rejected_edits(rejected)
        )

    # Trajectory block gets whatever context the fixed sections leave over
    # (first real consumer of context_cap * context_use_frac). All budget math
    # runs on TOKENS — exact via the endpoint tokenizer when reachable,
    # conservative estimate otherwise. 600 tokens of slack cover separators +
    # the injected firewall clause.
    count = lambda text: _count_tokens(client, text)  # noqa: E731
    threshold = int(getattr(cfg, "effective_context_threshold", 204_800))
    fixed_tokens = count(
        system_prompt + "\n\n".join(sections + tail_sections)) + 600
    token_budget = max(
        20_000, threshold - _PROPOSER_MAX_TOKENS - fixed_tokens)

    rolls = list(rollouts)
    if contrastive_group is not None:
        traj, traj_tokens, fitted = _fit_render_to_budget(
            lambda cap: _render_contrastive_group(contrastive_group, cap),
            cfg.tool_trunc, token_budget, count)
    else:
        traj, traj_tokens, fitted = _fit_render_to_budget(
            lambda cap: _render_minibatch(rolls, cap),
            cfg.tool_trunc, token_budget, count)
        while not fitted and len(rolls) > 1:
            # Even floor-capped messages overflow: shed whole trajectories,
            # loudly, rather than send a call that 400s into silence.
            rolls = rolls[: max(1, len(rolls) // 2)]
            traj, traj_tokens, fitted = _fit_render_to_budget(
                lambda cap: _render_minibatch(rolls, cap),
                cfg.tool_trunc, token_budget, count)
        if len(rolls) < len(rollouts):
            _log.warning(
                "reflect: dropped %d/%d trajectories to fit context budget "
                "(%d tokens)", len(rollouts) - len(rolls), len(rollouts),
                token_budget)
    if not fitted:
        _log.warning(
            "reflect: trajectory block still over budget at floor cap "
            "(%d > %d tokens); proposer call may overflow the context",
            traj_tokens, token_budget)

    user = "\n\n".join(sections + ["## Trajectories\n" + traj] + tail_sections)

    try:
        raw_edits = complete_optimizer_json(
            client, system_prompt, user, parse=_parse_edit_list,
            max_tokens=_PROPOSER_MAX_TOKENS, stage="proposer",
        )
    except Exception:  # noqa: BLE001 — a proposer failure must not crash the step
        _log.warning(
            "reflect: proposer LLM call failed (source=%s, %d rollouts); "
            "unit yields no edits", source_type, len(rollouts), exc_info=True)
        raw_edits = []

    # The edit budget (L) is a PROMPT guideline, not a mechanical truncation:
    # positionally beheading over-budget proposals is an unaudited signal
    # drop, and de-duplication is the merger's job (which keeps a full
    # unused-signal ledger). Over-budget output is kept and logged.
    if len(raw_edits) > budget:
        _log.info(
            "reflect: proposer emitted %d edits (budget guideline L=%d); "
            "keeping all — the merger de-duplicates with an audit trail",
            len(raw_edits), budget,
        )
    edits: list[Edit] = []
    for d in raw_edits:
        edit = _edit_from_dict(d, source_type)
        if edit is not None:
            edits.append(edit)

    # Fallback: if the LLM omitted source_tasks, attribute to the entire minibatch.
    minibatch_task_ids = list({r.task_id for r in rollouts})
    for edit in edits:
        if not edit.source_tasks:
            edit.source_tasks = list(minibatch_task_ids)

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
