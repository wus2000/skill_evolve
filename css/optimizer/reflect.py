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
import re
from typing import TYPE_CHECKING

from css.data.edit import EDIT_OPS, Edit, Patch, RawPatch
from css.trajectory import format_trajectory

if TYPE_CHECKING:
    from css.config import CSSConfig
    from css.data.rollout import TaskResult
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
  {"op": <one of append|insert_after|replace|delete>,
   "target": <anchor text already present in rules.md; required for
              insert_after/replace/delete, ignored for append>,
   "content": <text to add or substitute; ignored for delete>,
   "reason": <one concise sentence on the failure pattern this addresses>}

Edit op semantics on the free-form `rules.md` text:
  - append:       add `content` at the end of rules.md.
  - insert_after: insert `content` on the line after the first line containing `target`.
  - replace:      replace the first occurrence of `target` with `content`.
  - delete:       remove the first occurrence of `target`.

Rules for good edits:
  - Be specific and actionable; write rules the agent can mechanically follow.
  - Keep edits minimal and targeted; do not rewrite unrelated guidance.
  - Do NOT re-propose any edit listed under "Previously rejected edits" — those
    were already tried and failed the acceptance gate; pick a different angle.
  - Directly address every item under "Recent unresolved failure patterns".
  - If you genuinely have no high-confidence improvement, return an empty list [].

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
  {"op": <one of append|insert_after|replace|delete>,
   "target": <anchor text already present in rules.md; required for
              insert_after/replace/delete, ignored for append>,
   "content": <text to add or substitute; ignored for delete>,
   "reason": <one concise sentence on the success pattern this codifies>}

Edit op semantics on the free-form `rules.md` text:
  - append:       add `content` at the end of rules.md.
  - insert_after: insert `content` on the line after the first line containing `target`.
  - replace:      replace the first occurrence of `target` with `content`.
  - delete:       remove the first occurrence of `target`.

Rules for good edits:
  - Be specific and actionable; codify the winning behavior, not a vague platitude.
  - Keep edits minimal and targeted; do not rewrite unrelated guidance.
  - Do NOT re-propose any edit listed under "Previously rejected edits".
  - If the current rules already capture the winning behavior, return an empty list [].

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
        traj = format_trajectory(r.messages, tool_trunc=tool_trunc)
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
        source_type=source_type if source_type in ("failure", "success") else "failure",
        reason=str(d.get("reason", "") or ""),
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
        text, _usage = client.complete_optimizer(system, user)
    except Exception:  # noqa: BLE001 — an analyst failure must not crash the loop
        text = ""

    raw_edits = _parse_edit_list(text)
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

    Splits ``results`` into failure / success minibatches and runs every failure
    minibatch (analysed as failures) then every success minibatch (analysed as
    successes), returning one :class:`RawPatch` per minibatch (in that order).
    """
    failure_batches, success_batches = split_minibatches(results, cfg.minibatch_size)

    raw_patches: list[RawPatch] = []
    for batch in failure_batches:
        raw_patches.append(
            run_minibatch_analyst(
                client, strategy, rules, batch, step_buffer,
                source_type="failure", cfg=cfg,
            )
        )
    for batch in success_batches:
        raw_patches.append(
            run_minibatch_analyst(
                client, strategy, rules, batch, step_buffer,
                source_type="success", cfg=cfg,
            )
        )
    return raw_patches
