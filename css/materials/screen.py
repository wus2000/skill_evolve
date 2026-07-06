"""The Altitude Screen — a reusable batched judge (design §2.3).

Judges items in batches of ``cfg.screen_batch_size`` against four criteria
(subject is ways-of-behaving not specific actions; claims generalize; narrative
sufficiency; no tactical prescriptions). One revise round: an item the judge
marks "revise" is regenerated once with the feedback and re-judged; a "reject"
is excluded immediately; anything still failing after the revise round is
rejected. Rejected items are kept (marked), excluded downstream.

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


def _revise_item(item: str, feedback: str, client: Any, cfg: Any, stage: str) -> str:
    obj = common.run_json_stage(
        client, prompts.SCREEN_REVISE_SYSTEM,
        prompts.build_screen_revise_user(item, feedback),
        parse=common.parse_object, stage=f"{stage}_revise", cfg=cfg,
        ok=lambda r: isinstance(r, dict) and bool(str(r.get("revised", "")).strip()),
    )
    if isinstance(obj, dict) and str(obj.get("revised", "")).strip():
        return str(obj["revised"])
    return item  # revision unavailable -> keep original (the re-judge will decide)


def screen_items(items: list[str], optimizer_client: Any, cfg: Any, stage: str) -> list[dict]:
    """Judge ``items`` at altitude; return one verdict dict per item, in order.

    Verdict dict: ``{index, verdict: pass|rejected, first_verdict, revised: bool,
    text: <final text>, violated_criteria, feedback, quoted_offense}``. ``text``
    is the revised text when a revise round produced an accepted rewrite, else
    the original — the caller adopts it for the item it screened.
    """
    if not items:
        return []
    verdicts = _judge_all(items, optimizer_client, cfg, stage)

    revise_idx = [i for i, v in enumerate(verdicts) if v["first_verdict"] == "revise"]
    if not revise_idx:
        return verdicts

    revised: dict[int, str] = {}
    max_workers = max(1, int(getattr(cfg, "max_api_workers", 32)))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {pool.submit(_revise_item, items[i], verdicts[i]["feedback"],
                            optimizer_client, cfg, stage): i for i in revise_idx}
        for fut in as_completed(futs):
            i = futs[fut]
            try:
                revised[i] = fut.result()
            except Exception:  # noqa: BLE001
                revised[i] = items[i]

    re_items = [revised[i] for i in revise_idx]
    rejudged = _judge_all(re_items, optimizer_client, cfg, stage)
    for pos, i in enumerate(revise_idx):
        rv = rejudged[pos] if pos < len(rejudged) else None
        verdicts[i]["revised"] = True
        verdicts[i]["text"] = revised[i]
        if rv is not None and rv["first_verdict"] == "pass":
            verdicts[i]["verdict"] = "pass"
        else:
            verdicts[i]["verdict"] = "rejected"
            if rv is not None and rv.get("feedback"):
                verdicts[i]["feedback"] = rv["feedback"]

    n_rejected = sum(1 for v in verdicts if v["verdict"] == "rejected")
    _log.info("materials/screen[%s] — %d items, %d revised, %d rejected",
              stage, len(items), len(revise_idx), n_rejected)
    return verdicts
