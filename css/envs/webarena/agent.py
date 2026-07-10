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


def parse_action_from_reply(reply: str):
    """Decode the LLM's chosen action. JSON protocol is PRIMARY (structured,
    schema-tolerant — see json_action.py); the text DSL is a FALLBACK for any
    straggler reply still in the old form. Returns (action | None, action_str,
    err | None); a non-None err means the reply had no valid action (invalid)."""
    from css.envs.webarena.json_action import (
        action_display, create_json_action, extract_action_json)
    from css.envs.webarena.vendor import create_id_based_action
    obj = extract_action_json(reply)
    if obj is not None:
        try:
            return create_json_action(obj), action_display(obj), None
        except Exception as exc:  # noqa: BLE001 — bad schema => invalid action
            return None, action_display(obj), f"{type(exc).__name__}: {exc}"
    dsl = extract_action_str(reply)     # legacy text-DSL fallback
    try:
        return create_id_based_action(dsl), dsl, None
    except Exception as exc:  # noqa: BLE001
        return None, (dsl or reply or "")[:120], f"{type(exc).__name__}: {exc}"


def resolve_start_url(item: dict, lease: "Lease") -> str:
    """Map the record's first ``__SITE__`` placeholder to this lease's URL."""
    urls = item.get("start_urls") or []
    raw = urls[0] if urls else "__SHOPPING__"
    for site, base in lease.urls.items():
        raw = raw.replace(f"__{site.upper()}__", base.rstrip("/"))
    return raw


def _auth_sites_for(item: dict, lease: "Lease") -> tuple:
    """Sites whose login state THIS task needs — from its start_urls'
    ``__SITE__`` placeholders, NOT every site on the stack. The old
    ``lease.sites or lease.urls`` loaded ALL sites for read tasks; shopping AND
    reddit both name their session cookie ``PHPSESSID`` (domain=localhost,
    port-agnostic), so merging them into one browser context collided the two,
    the wrong one won, and magento lost the session (guest). That is exactly why
    read shopping tasks failed while mutate ones (single-site lease) worked.
    Scoping to the task's own site(s) removes the collision."""
    found: list = []
    for u in (item.get("start_urls") or []):
        for m in re.findall(r"__([A-Z_]+)__", u or ""):
            s = m.lower()
            if s in lease.urls and s not in found:
                found.append(s)
    if found:
        return tuple(found)
    decl = tuple(s for s in (item.get("sites") or []) if s in lease.urls)
    if decl:
        return decl
    if lease.sites:
        return tuple(lease.sites)
    return tuple(lease.urls)   # last resort (site unknown) — legacy behavior


# ── A2: transient farm-overload retry (2026-07-10) ───────────────────────────
# A magento under concurrent browser sessions momentarily exhausts its php-fpm/
# nginx workers and returns net::ERR_CONNECTION_RESET / REFUSED / EMPTY_RESPONSE
# until it drains. These are TRANSIENT overload signals, not a dead site —
# retrying the navigation with backoff rides it out instead of failing the
# episode (which would record a fake failure in the coverage ledger). A truly
# dead site exhausts the retries and raises, exactly as before.
_TRANSIENT_NET = ("ERR_CONNECTION_RESET", "ERR_CONNECTION_REFUSED",
                  "ERR_EMPTY_RESPONSE", "ERR_CONNECTION_CLOSED",
                  "ERR_NETWORK_CHANGED", "ERR_ADDRESS_UNREACHABLE",
                  "ERR_CONNECTION_TIMED_OUT", "ERR_TIMED_OUT")


def _is_transient_net(exc: Exception) -> bool:
    m = str(exc)
    return any(t in m for t in _TRANSIENT_NET)


async def _agoto_retry(page, url: str, *, wait_until: str = "domcontentloaded",
                       tries: int = 4, base_delay: float = 2.0):
    """page.goto with exponential backoff on transient farm overload (A2)."""
    import asyncio
    last: "Exception | None" = None
    for i in range(tries):
        try:
            return await page.goto(url, wait_until=wait_until)
        except Exception as exc:  # noqa: BLE001
            if i == tries - 1 or not _is_transient_net(exc):
                raise
            last = exc
            await asyncio.sleep(base_delay * (2 ** i))
    if last:  # pragma: no cover — the loop always returns or raises above
        raise last


def _goto_retry(page, url: str, *, wait_until: str = "domcontentloaded",
                tries: int = 4, base_delay: float = 2.0):
    """Sync twin of _agoto_retry for the non-pool run_episode path (A2)."""
    import time as _t
    last: "Exception | None" = None
    for i in range(tries):
        try:
            return page.goto(url, wait_until=wait_until)
        except Exception as exc:  # noqa: BLE001
            if i == tries - 1 or not _is_transient_net(exc):
                raise
            last = exc
            _t.sleep(base_delay * (2 ** i))
    if last:  # pragma: no cover
        raise last


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
    # Stuck-stop: end the episode after this many consecutive steps that change
    # nothing on the page (invalid/failed/no-effect actions). The loop
    # pathology (2026-07-08 probes: 24-28 no-op steps burning to max_turns) is
    # both wasted budget and a fake capability signal. OpAgent's RDT flags the
    # same redundancy per step; we use it to terminate. 0 disables.
    stuck_stop = int(extra.get("webarena_stuck_stop_steps", 5))

    # Authentication (css/envs/webarena/auth.py). Over half the benchmark acts
    # as a logged-in user: cookie jars for shopping/reddit/gitlab, an auto-login
    # header for shopping_admin. A read-only task that pinned no sites still
    # gets the lease's stack jars — upstream starts every task authenticated.
    auth_sites = _auth_sites_for(item, lease)
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
            _goto_retry(page, resolve_start_url(item, lease))
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

                action, action_str, perr = parse_action_from_reply(reply)
                last_result = ""
                invalid, exec_error, stopped = False, "", False
                if perr is not None:
                    invalid = True
                    last_result = (f"Invalid action ({perr}). Reply with exactly "
                                   'ONE action as a single JSON object '
                                   '{"name": ..., "parameters": {...}} in a ```json block.')
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
                # Hard-stop a stuck episode: N consecutive no-effect steps means
                # the agent is looping and will not recover on its own (it has
                # already seen the repeat alerts in the history block). End it
                # here rather than burn the rest of max_turns.
                streak = history.no_change_streak()
                if stuck_stop and streak >= stuck_stop:
                    _log.info("webarena/agent — stuck-stop at turn %d "
                              "(%d consecutive no-change steps)", n_turns, streak)
                    stop_payload = {
                        "task_type": "retrieve", "status": "UNKNOWN_ERROR",
                        "retrieved_data": None,
                        "error_details": (f"early stop: {streak} consecutive "
                                          "steps with no page change")}
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


async def run_episode_async(pool: Any, item: dict, skill_text: str,
                            target_client: Any, cfg: "CSSConfig",
                            lease: "Lease", workdir: str) -> dict:
    """High-concurrency twin of run_episode: identical turn logic, but the
    browser context comes from the shared async BrowserPool (context-per-episode
    over K reused browsers), every browser op is awaited, and the blocking LLM /
    scribe / archive calls are offloaded off the event loop. Runs as a coroutine
    on the pool's loop; env.run_one submits it and blocks on the future."""
    from css.envs.webarena.vendor import (
        ActionTypes, TextObervationProcessor, create_id_based_action,
        aexecute_action)

    extra = getattr(cfg, "extra", {}) or {}
    max_turns = int(getattr(cfg, "max_turns", 0) or 30)
    scribe_on = bool(extra.get("webarena_scribe", True))
    stuck_stop = int(extra.get("webarena_stuck_stop_steps", 5))

    auth_sites = _auth_sites_for(item, lease)
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

    # ── Throughput probe (benchmarking only; webarena_scripted_probe=0 in
    # production). Exercises the real env hot path — context acquire, page nav,
    # AXTree parse — at full pool concurrency WITHOUT the LLM, to isolate the
    # task-environment throughput ceiling (LLM latency excluded).
    probe_steps = int(extra.get("webarena_scripted_probe", 0) or 0)
    if probe_steps > 0:
        done = 0
        async with pool.acquire(storage_state=storage_state,
                                har_path=os.path.join(workdir, "network.har"),
                                headers=headers) as (ctx, page):
            await _agoto_retry(page, resolve_start_url(item, lease))
            proc = TextObervationProcessor("accessibility_tree", True, VIEWPORT)
            for _ in range(probe_steps):
                try:
                    await proc.aprocess(page, await ctx.new_cdp_session(page))
                    done += 1
                except Exception:  # noqa: BLE001 — parse failure ends the probe
                    break
        return {"messages": [], "n_turns": done, "agent_response": "probe"}

    har_path = os.path.join(workdir, "network.har")
    async with pool.acquire(storage_state=storage_state, har_path=har_path,
                            headers=headers) as (ctx, page):
        await _agoto_retry(page, resolve_start_url(item, lease))
        proc = TextObervationProcessor("accessibility_tree", True, VIEWPORT)
        try:
            obs = await proc.aprocess(page, await ctx.new_cdp_session(page))
        except Exception as exc:  # noqa: BLE001 — obs failure ends episode
            _log.warning("webarena/agent(async) — landing obs failed: %s", exc)
            obs = None

        while obs is not None and n_turns < max_turns:
            n_turns += 1
            url_before = page.url
            hist_block = history.render(turn_now=n_turns, max_turns=max_turns)
            user = prompts.build_turn(objective, hist_block, url_before,
                                      obs, last_result)
            reply = await pool.offload(
                target_client.complete_target_messages,
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}]) or ""
            canonical.append({"role": "user", "content": build_canonical_user(
                turn=n_turns, url=url_before,
                objective=objective if n_turns == 1 else "",
                prev_effect=prev_effect, prev_facts=prev_facts)})
            canonical.append({"role": "assistant", "content": reply})

            action, action_str, perr = parse_action_from_reply(reply)
            last_result = ""
            invalid, exec_error, stopped = False, "", False
            if perr is not None:
                invalid = True
                last_result = (f"Invalid action ({perr}). Reply with exactly ONE "
                               'action as a single JSON object '
                               '{"name": ..., "parameters": {...}} in a ```json block.')
            else:
                if action["action_type"] == ActionTypes.STOP:
                    stopped = True
                    stop_payload = prompts.parse_stop_payload(
                        action.get("answer", ""))
                else:
                    try:
                        page = await aexecute_action(action, page, ctx, proc)
                        await page.wait_for_load_state("domcontentloaded")
                    except Exception as exc:  # noqa: BLE001 — surface to the agent
                        exec_error = f"{type(exc).__name__}: {exc}"[:400]
                        last_result = f"Action failed: {exec_error}"

            obs_after = None
            if not stopped:
                if invalid:
                    obs_after = obs   # page untouched — reuse the snapshot
                else:
                    try:
                        obs_after = await proc.aprocess(
                            page, await ctx.new_cdp_session(page))
                    except Exception as exc:  # noqa: BLE001 — obs failure ends episode
                        _log.warning(
                            "webarena/agent(async) — obs failed: %s", exc)

            effect, page_changed = (
                ("episode ended by stop", False) if stopped else
                effect_signature(url_before, page.url, obs,
                                 obs_after or "", invalid=invalid,
                                 exec_error=exec_error))
            rec = history.append(turn=n_turns, action=action_str,
                                 url=url_before, effect=effect,
                                 page_changed=page_changed)
            scribe_io: "dict | None" = None
            if scribe is not None and not stopped and history.should_scribe():
                rec.intent, rec.facts = await pool.offload(
                    scribe.transcribe, objective=objective, url=url_before,
                    observation=obs, reasoning=reply, action_str=action_str,
                    effect=effect)
                scribe_io = ({**scribe.telemetry[-1],
                              "intent": rec.intent, "facts": rec.facts}
                             if scribe.telemetry else None)
            await pool.offload(archive, {
                "turn": n_turns, "url": url_before, "history_block": hist_block,
                "observation": obs, "reply": reply, "action": action_str,
                "effect": effect, "page_changed": page_changed,
                "scribe": scribe_io})
            prev_effect, prev_facts = effect, list(rec.facts)
            if stopped:
                break
            streak = history.no_change_streak()
            if stuck_stop and streak >= stuck_stop:
                _log.info("webarena/agent(async) — stuck-stop at turn %d "
                          "(%d consecutive no-change steps)", n_turns, streak)
                stop_payload = {
                    "task_type": "retrieve", "status": "UNKNOWN_ERROR",
                    "retrieved_data": None,
                    "error_details": (f"early stop: {streak} consecutive "
                                      "steps with no page change")}
                break
            obs = obs_after

    if stop_payload is None:
        stop_payload = {"task_type": "retrieve", "status": "UNKNOWN_ERROR",
                        "retrieved_data": None,
                        "error_details": f"no stop action within {max_turns} turns"}
    write_agent_response(workdir, stop_payload)
    return {"messages": canonical, "n_turns": n_turns,
            "agent_response": stop_payload,
            "har_path": os.path.join(workdir, "network.har")}
