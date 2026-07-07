"""The Altitude Screen — a reusable batched judge (design §2.3).

Judges items in batches of ``cfg.screen_batch_size`` against four criteria
(subject is ways-of-behaving not specific actions; claims generalize; narrative
sufficiency; no tactical prescriptions).

JUDGE ONLY (decision log #14): the screen never rewrites an item. Callers that
can regenerate an item through its SOURCE pipeline use
:func:`screen_with_regeneration`, which routes a "revise" verdict's feedback to
the caller-supplied ``regenerate_fn`` (the original pipeline re-produces the
item), re-judges the regenerated text once, and rejects on continued failure.
Callers without a regeneration path treat any non-pass verdict as rejected.
Rejected items are kept (marked), excluded downstream.

Reused by the interpretation gate (Layer 1 -> Layer 2) and the profile / frontier
document updates; the generation and exploration packages import
:func:`screen_items` for their own products.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from css.materials import common, prompts

_log = logging.getLogger("css.materials")



def _normalize_verdict(v: Any, index: int, item: str) -> dict:
    """One raw judge verdict -> a normalized record (fail-open on a missing one)."""
    if not isinstance(v, dict):
        return {"index": index, "first_verdict": "pass", "verdict": "pass",
                "violated_criteria": [], "feedback": "", "quoted_offense": "",
                "revised": False, "text": item,
                "note": "screen-missing-defaulted-pass"}
    fv = str(v.get("verdict", "pass")).strip().lower()
    if fv not in ("pass", "revise", "reject"):
        fv = "pass"
    final = {"pass": "pass", "revise": "revise", "reject": "rejected"}[fv]
    crit = v.get("violated_criteria", []) or []
    return {"index": index, "first_verdict": fv, "verdict": final,
            "violated_criteria": crit if isinstance(crit, list) else [crit],
            "feedback": str(v.get("feedback", "")),
            "quoted_offense": str(v.get("quoted_offense", "")),
            "revised": False, "text": item}


def _judge_batch(chunk: list[str], client: Any, cfg: Any, stage: str) -> list[dict]:
    user = prompts.build_screen_user(chunk, stage)
    raw = common.run_json_stage(
        client, prompts.SCREEN_SYSTEM, user,
        parse=common.parse_list_field("verdicts"), stage=f"{stage}_screen", cfg=cfg,
        ok=lambda r: isinstance(r, list) and bool(r),
    )
    raw = raw if isinstance(raw, list) else []
    # Prefer the model's 1-based "index" field; fall back to positional order.
    by_index: dict[int, dict] = {}
    for v in raw:
        if isinstance(v, dict) and isinstance(v.get("index"), int):
            j = v["index"] - 1
            if 0 <= j < len(chunk):
                by_index[j] = v
    out: list[dict] = []
    for i in range(len(chunk)):
        v = by_index.get(i)
        if v is None and i < len(raw) and isinstance(raw[i], dict):
            v = raw[i]
        out.append(_normalize_verdict(v, i, chunk[i]))
    return out


def _judge_all(items: list[str], client: Any, cfg: Any, stage: str) -> list[dict]:
    batch_size = max(1, int(getattr(cfg, "screen_batch_size", 10)))
    verdicts: list[dict] = []
    for start in range(0, len(items), batch_size):
        verdicts.extend(_judge_batch(items[start:start + batch_size], client, cfg, stage))
    for i, v in enumerate(verdicts):
        v["index"] = i
    return verdicts


def screen_items(items: list[str], optimizer_client: Any, cfg: Any, stage: str) -> list[dict]:
    """Judge ``items`` at altitude; return one verdict dict per item, in order.

    PURE JUDGE — never rewrites. Verdict dict: ``{index, verdict:
    pass|revise|rejected, first_verdict, violated_criteria, feedback,
    quoted_offense, text: <the original item, unchanged>}``. Callers with a
    regeneration path handle ``revise`` via :func:`screen_with_regeneration`;
    callers without one treat any non-pass as rejected.
    """
    if not items:
        return []
    return _judge_all(items, optimizer_client, cfg, stage)


def screen_with_regeneration(
    items: list[str], optimizer_client: Any, cfg: Any, stage: str,
    regenerate_fn,
) -> list[dict]:
    """Judge; route "revise" feedback to the SOURCE pipeline; re-judge once.

    ``regenerate_fn(index, feedback) -> str | None`` re-produces item ``index``
    through the pipeline that originally generated it (decision log #14: the
    screen only judges — content regeneration belongs to the source). ``None``
    (regeneration unavailable/failed) rejects the item. A regenerated item is
    re-judged once; continued failure rejects it. On success the verdict
    carries ``regenerated: True`` and ``text`` = the regenerated item.
    """
    if not items:
        return []
    verdicts = _judge_all(items, optimizer_client, cfg, stage)
    revise_idx = [i for i, v in enumerate(verdicts)
                  if v["first_verdict"] == "revise"]
    if not revise_idx:
        return verdicts

    regenerated: dict[int, "str | None"] = {}
    max_workers = max(1, int(getattr(cfg, "max_api_workers", 32)))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {pool.submit(regenerate_fn, i, verdicts[i]["feedback"]): i
                for i in revise_idx}
        for fut in as_completed(futs):
            i = futs[fut]
            try:
                regenerated[i] = fut.result()
            except Exception:  # noqa: BLE001 — a failed regeneration = reject
                _log.exception("materials/screen[%s] — regeneration failed for "
                               "item %d", stage, i)
                regenerated[i] = None

    alive = [i for i in revise_idx
             if isinstance(regenerated.get(i), str) and regenerated[i].strip()]
    rejudged = _judge_all([regenerated[i] for i in alive],
                          optimizer_client, cfg, stage) if alive else []
    re_by_idx = {i: rejudged[pos] for pos, i in enumerate(alive)
                 if pos < len(rejudged)}
    for i in revise_idx:
        rv = re_by_idx.get(i)
        if rv is not None and rv["first_verdict"] == "pass":
            verdicts[i]["verdict"] = "pass"
            verdicts[i]["regenerated"] = True
            verdicts[i]["text"] = regenerated[i]
        else:
            verdicts[i]["verdict"] = "rejected"
            verdicts[i]["regenerated"] = i in re_by_idx
            if rv is not None and rv.get("feedback"):
                verdicts[i]["feedback"] = rv["feedback"]

    n_rejected = sum(1 for v in verdicts if v["verdict"] == "rejected")
    _log.info("materials/screen[%s] — %d items, %d regenerated via source "
              "pipeline, %d rejected",
              stage, len(items), len(revise_idx), n_rejected)
    return verdicts
