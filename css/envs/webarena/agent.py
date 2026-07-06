"""One WebArena browser episode: playwright loop over the vendored harness.

Contract (consumed by env.run_one): ``run_episode(item, skill_text,
target_client, cfg, lease, workdir) -> {"messages", "n_turns",
"agent_response"}``. The episode records ``network.har`` and writes
``agent_response.json`` into ``workdir`` — exactly the two inputs the
WebArena-Verified offline evaluator needs — and returns the canonical flat
transcript for the optimizer's analysis layers.

Vendored-harness invariants (vendor/VENDOR_NOTES.md): device_scale_factor=1
and page viewport == processor viewport (CDP assert); a FRESH CDP session per
step for the active page; ``execute_action`` returns the authoritative active
page (tab actions may swap it); element ids in the observation are the ids
the action grammar consumes.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import TYPE_CHECKING, Any

from css.envs.webarena import prompts
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


def run_episode(item: dict, skill_text: str, target_client: Any,
                cfg: "CSSConfig", lease: "Lease", workdir: str) -> dict:
    from playwright.sync_api import sync_playwright  # heavy import stays local
    from css.envs.webarena.vendor import (
        ActionTypes, TextObervationProcessor, create_id_based_action,
        execute_action)

    extra = getattr(cfg, "extra", {}) or {}
    max_turns = int(getattr(cfg, "max_turns", 0) or 30)
    har_content = str(extra.get("webarena_har_content", "omit"))
    headers = dict(extra.get("webarena_extra_headers", {}) or {})
    nav_timeout_ms = int(extra.get("webarena_nav_timeout_ms", 30000))

    system = prompts.build_system_prompt(skill_text)
    messages: list[dict] = [{"role": "system", "content": system}]
    objective = item.get("intent", "")
    stop_payload: "dict | None" = None
    n_turns = 0
    last_action, last_result = "", ""

    os.makedirs(workdir, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport=VIEWPORT, device_scale_factor=1,
            record_har_path=os.path.join(workdir, "network.har"),
            record_har_content=har_content)
        ctx.set_default_timeout(nav_timeout_ms)
        if headers:
            ctx.set_extra_http_headers(headers)
        try:
            page = ctx.new_page()
            page.goto(resolve_start_url(item, lease))
            proc = TextObervationProcessor("accessibility_tree", True, VIEWPORT)

            for _ in range(max_turns):
                n_turns += 1
                try:
                    client = ctx.new_cdp_session(page)
                    obs = proc.process(page, client)
                except Exception as exc:  # noqa: BLE001 — obs failure ends episode
                    _log.warning("webarena/agent — observation failed: %s", exc)
                    break
                user = prompts.build_turn(objective, page.url, obs,
                                          last_action, last_result)
                messages.append({"role": "user", "content": user})
                reply = target_client.complete_target_messages(messages)
                messages.append({"role": "assistant", "content": reply or ""})

                action_str = extract_action_str(reply)
                last_action, last_result = action_str, ""
                try:
                    action = create_id_based_action(action_str)
                except Exception as exc:  # noqa: BLE001 — ActionParsingError et al.
                    last_result = (f"Invalid action '{action_str[:120]}': {exc}. "
                                   "Reply with exactly one action from the "
                                   "action space, fenced in triple backticks.")
                    continue
                if action["action_type"] == ActionTypes.STOP:
                    stop_payload = prompts.parse_stop_payload(action.get("answer", ""))
                    break
                try:
                    page = execute_action(action, page, ctx, proc)
                    page.wait_for_load_state("domcontentloaded")
                except Exception as exc:  # noqa: BLE001 — surface to the agent
                    last_result = f"Action failed: {type(exc).__name__}: {exc}"[:400]
        finally:
            ctx.close()   # flushes network.har — required before scoring
            browser.close()

    if stop_payload is None:
        stop_payload = {"task_type": "retrieve", "status": "UNKNOWN_ERROR",
                        "retrieved_data": None,
                        "error_details": f"no stop action within {max_turns} turns"}
    write_agent_response(workdir, stop_payload)
    return {"messages": messages, "n_turns": n_turns,
            "agent_response": stop_payload,
            "har_path": os.path.join(workdir, "network.har")}
