"""One WebArena browser episode: playwright loop over the vendored harness.

Contract (consumed by env.run_one): ``run_episode(item, skill_text,
target_client, cfg, lease, workdir) -> {"messages", "n_turns",
"agent_response"}``. The episode records ``network.har`` and writes
``agent_response.json`` into ``workdir`` — exactly the two inputs the
WebArena-Verified offline evaluator needs.

Prompt regime (agreed 2026-07-08, after the wa_0784 full/latest A-B probe):
SINGLE-TURN reconstruction — each turn's prompt is [system, user] where the
user carries the objective, the harness-maintained TRAJECTORY HISTORY block
(mechanical effects + env-side scribe facts; css/envs/webarena/history.py),
and the CURRENT observation in full. Past observations never re-enter the
prompt (peak context stays flat vs the measured 98.6k-token growth of the
resend-everything regime).

Archival is two-track:
- ``turns.jsonl.gz`` in ``workdir``: the byte-level record — per turn the
  full observation, rendered history block, verbatim reply, effect and
  scribe I/O (L1 deep reads + audits).
- the returned ``messages``: the LEARNING-LAYER canonical transcript — per
  turn a compact user message (previous effect + scribe facts + current
  page) and the verbatim assistant reply. This IS the action-focused
  rendering the obs survey prescribes for reflection prompts, so the L0
  budget-decay fallback should rarely trigger on WebArena trajectories.

Vendored-harness invariants (vendor/VENDOR_NOTES.md): device_scale_factor=1
and page viewport == processor viewport (CDP assert); a FRESH CDP session per
step for the active page; ``execute_action`` returns the authoritative active
page (tab actions may swap it); element ids in the observation are the ids
the action grammar consumes.
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import re
from typing import TYPE_CHECKING, Any

from css.envs.webarena import auth, prompts
from css.envs.webarena.history import (Scribe, TrajectoryHistory,
                                       effect_signature, short_path)
from css.envs.webarena.scoring import write_agent_response

if TYPE_CHECKING:  # pragma: no cover
    from css.config import CSSConfig
    from css.envs.webarena.scheduler import Lease

_log = logging.getLogger("css.webarena")

VIEWPORT = {"width": 1280, "height": 720}
_FENCE = re.compile(r"```+\s*([^`]+?)\s*```+", re.DOTALL)


def extract_action_str(reply: str) -> str:
    """Last fenced block wins (official convention); fallback = last line."""
    blocks = _FENCE.findall(reply or "")
    if blocks:
        return blocks[-1].strip().splitlines()[-1].strip()
    lines = [ln.strip() for ln in (reply or "").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def resolve_start_url(item: dict, lease: "Lease") -> str:
    """Map the record's first ``__SITE__`` placeholder to this lease's URL."""
    urls = item.get("start_urls") or []
    raw = urls[0] if urls else "__SHOPPING__"
    for site, base in lease.urls.items():
        raw = raw.replace(f"__{site.upper()}__", base.rstrip("/"))
    return raw


def build_canonical_user(*, turn: int, url: str, objective: str = "",
                         prev_effect: str = "",
                         prev_facts: "list[str] | None" = None) -> str:
    """One compact user message of the learning-layer transcript: the
    previous step's mechanical effect and scribe facts stand in for the raw
    observation (survey ruling: raw AXTree never outlives its step)."""
    parts: list[str] = []
    if objective:
        parts.append(f"OBJECTIVE: {objective}")
    if prev_effect:
        parts.append(f"RESULT OF t{turn - 1}: {prev_effect}")
    if prev_facts:
        parts.append(f"KEY FACTS FROM t{turn - 1}'S PAGE:\n"
                     + "\n".join(f"- {f}" for f in prev_facts))
    parts.append(f"[t{turn}] now at: {short_path(url)}")
    return "\n".join(parts)


def run_episode(item: dict, skill_text: str, target_client: Any,
                cfg: "CSSConfig", lease: "Lease", workdir: str) -> dict:
    from playwright.sync_api import sync_playwright  # heavy import stays local
    from css.envs.webarena.vendor import (
        ActionTypes, TextObervationProcessor, create_id_based_action,
        execute_action)

    extra = getattr(cfg, "extra", {}) or {}
    max_turns = int(getattr(cfg, "max_turns", 0) or 30)
    har_content = str(extra.get("webarena_har_content", "omit"))
    nav_timeout_ms = int(extra.get("webarena_nav_timeout_ms", 30000))
    scribe_on = bool(extra.get("webarena_scribe", True))

    # Authentication (css/envs/webarena/auth.py). Over half the benchmark acts
    # as a logged-in user: cookie jars for shopping/reddit/gitlab, an auto-login
    # header for shopping_admin. A read-only task that pinned no sites still
    # gets the lease's stack jars — upstream starts every task authenticated.
    auth_sites = tuple(lease.sites) or tuple(lease.urls)
    headers = dict(extra.get("webarena_extra_headers", {}) or {})
    headers.update(auth.extra_headers(auth_sites))
    auth_dir = str(extra.get("webarena_auth_dir", "") or "")
    storage_state = (auth.merged_state(auth_dir, lease.stack, auth_sites)
                     if auth_dir else None)

    system = prompts.build_system_prompt(skill_text)
    objective = item.get("intent", "")
    history = TrajectoryHistory(
        budget_tokens=int(extra.get("webarena_history_budget_tokens", 3000)),
        count_tokens=getattr(target_client, "count_tokens", None))
    scribe = Scribe(target_client) if scribe_on else None

    canonical: list[dict] = [{"role": "system", "content": system}]
    stop_payload: "dict | None" = None
    n_turns = 0
    last_result = ""
    prev_effect, prev_facts = "", []

    os.makedirs(workdir, exist_ok=True)
    turns_path = os.path.join(workdir, "turns.jsonl.gz")

    def archive(rec: dict) -> None:
        with gzip.open(turns_path, "at", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    with sync_playwright() as p:
        # channel="chromium" = run the full chromium binary in new-headless
        # mode — avoids the separate chromium-headless-shell download, whose
        # installer no-opped on the harness host (playwright 1.61, 2026-07-06).
        browser = p.chromium.launch(headless=True, channel="chromium")
        ctx = browser.new_context(
            viewport=VIEWPORT, device_scale_factor=1,
            storage_state=storage_state,
            record_har_path=os.path.join(workdir, "network.har"),
            record_har_content=har_content)
        ctx.set_default_timeout(nav_timeout_ms)
        if headers:
            ctx.set_extra_http_headers(headers)
        try:
            page = ctx.new_page()
            # domcontentloaded, not the default 'load': gitlab sub-resources
            # stall the load event past 30s (two probe launches failed on it,
            # 2026-07-08) while the CDP AXTree snapshot only needs the DOM —
            # matching the post-action wait below.
            page.goto(resolve_start_url(item, lease),
                      wait_until="domcontentloaded")
            proc = TextObervationProcessor("accessibility_tree", True, VIEWPORT)

            try:
                obs = proc.process(page, ctx.new_cdp_session(page))
            except Exception as exc:  # noqa: BLE001 — obs failure ends episode
                _log.warning("webarena/agent — landing observation failed: %s",
                             exc)
                obs = None

            while obs is not None and n_turns < max_turns:
                n_turns += 1
                url_before = page.url
                hist_block = history.render(turn_now=n_turns,
                                            max_turns=max_turns)
                user = prompts.build_turn(objective, hist_block, url_before,
                                          obs, last_result)
                reply = target_client.complete_target_messages(
                    [{"role": "system", "content": system},
                     {"role": "user", "content": user}]) or ""
                canonical.append({"role": "user", "content": build_canonical_user(
                    turn=n_turns, url=url_before,
                    objective=objective if n_turns == 1 else "",
                    prev_effect=prev_effect, prev_facts=prev_facts)})
                canonical.append({"role": "assistant", "content": reply})

                action_str = extract_action_str(reply)
                last_result = ""
                invalid, exec_error, stopped = False, "", False
                try:
                    action = create_id_based_action(action_str)
                except Exception as exc:  # noqa: BLE001 — ActionParsingError et al.
                    invalid = True
                    last_result = (f"Invalid action '{action_str[:120]}': {exc}. "
                                   "Reply with exactly one action from the "
                                   "action space, fenced in triple backticks.")
                else:
                    if action["action_type"] == ActionTypes.STOP:
                        stopped = True
                        stop_payload = prompts.parse_stop_payload(
                            action.get("answer", ""))
                    else:
                        try:
                            page = execute_action(action, page, ctx, proc)
                            page.wait_for_load_state("domcontentloaded")
                        except Exception as exc:  # noqa: BLE001 — surface to the agent
                            exec_error = f"{type(exc).__name__}: {exc}"[:400]
                            last_result = f"Action failed: {exec_error}"

                obs_after = None
                if not stopped:
                    if invalid:
                        obs_after = obs   # page untouched — reuse the snapshot
                    else:
                        try:
                            obs_after = proc.process(
                                page, ctx.new_cdp_session(page))
                        except Exception as exc:  # noqa: BLE001 — obs failure ends episode
                            _log.warning(
                                "webarena/agent — observation failed: %s", exc)

                effect, page_changed = (
                    ("episode ended by stop", False) if stopped else
                    effect_signature(url_before, page.url, obs,
                                     obs_after or "", invalid=invalid,
                                     exec_error=exec_error))
                rec = history.append(turn=n_turns, action=action_str,
                                     url=url_before, effect=effect,
                                     page_changed=page_changed)
                scribe_io: "dict | None" = None
                # A stop step writes no history for later turns to read.
                if scribe is not None and not stopped and history.should_scribe():
                    rec.intent, rec.facts = scribe.transcribe(
                        objective=objective, url=url_before, observation=obs,
                        reasoning=reply, action_str=action_str, effect=effect)
                    scribe_io = ({**scribe.telemetry[-1],
                                  "intent": rec.intent, "facts": rec.facts}
                                 if scribe.telemetry else None)
                archive({"turn": n_turns, "url": url_before,
                         "history_block": hist_block, "observation": obs,
                         "reply": reply, "action": action_str,
                         "effect": effect, "page_changed": page_changed,
                         "scribe": scribe_io})
                prev_effect, prev_facts = effect, list(rec.facts)
                if stopped:
                    break
                obs = obs_after
        finally:
            ctx.close()   # flushes network.har — required before scoring
            browser.close()

    if stop_payload is None:
        stop_payload = {"task_type": "retrieve", "status": "UNKNOWN_ERROR",
                        "retrieved_data": None,
                        "error_details": f"no stop action within {max_turns} turns"}
    write_agent_response(workdir, stop_payload)
    return {"messages": canonical, "n_turns": n_turns,
            "agent_response": stop_payload,
            "har_path": os.path.join(workdir, "network.har")}
