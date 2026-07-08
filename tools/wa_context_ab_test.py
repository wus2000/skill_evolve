#!/usr/bin/env python3
"""A/B context-strategy probe on ONE WebArena task (no mechanism change).

Arm "full"   — the CURRENT css.envs.webarena.agent.run_episode, called
               untouched: every turn resends the whole message history,
               so every past observation stays in the prompt.
Arm "latest" — the survey's field-standard shape (WebArena/BrowserGym/SteP):
               the prompt carries ONLY the current observation; history is a
               plain action list (with per-step failure notes verbatim),
               never past observations. Everything else — system prompt,
               action grammar, invalid-action feedback, stop contract,
               browser handling — is identical to the "full" arm, so the
               retained-observation policy is the single controlled variable.

Both arms run bare (empty skill document) with the same client settings as
run_experiment_webarena_server.py. Every LLM request/response is recorded
VERBATIM (no elision, no truncation) to calls.json for offline inspection.

Usage (on the harness host, repo root):
    python3 tools/wa_context_ab_test.py --mode full   --out runs/wa_ctx_ab/full
    python3 tools/wa_context_ab_test.py --mode latest --out runs/wa_ctx_ab/latest
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from types import SimpleNamespace

from css.model.client import OpenAICompatLLMClient
from css.envs.webarena import prompts
from css.envs.webarena.agent import (VIEWPORT, extract_action_str,
                                     resolve_start_url, run_episode)
from css.envs.webarena.scheduler import Lease
from css.envs.webarena.scoring import write_agent_response

STACKS = {
    "s1": {"shopping": "http://localhost:7770",
           "shopping_admin": "http://localhost:7780",
           "reddit": "http://localhost:9999",
           "gitlab": "http://localhost:8023"},
    "s2": {"shopping": "http://localhost:17770",
           "shopping_admin": "http://localhost:17780",
           "reddit": "http://localhost:19999",
           "gitlab": "http://localhost:18023"},
    "s3": {"shopping": "http://localhost:27770",
           "shopping_admin": "http://localhost:27780",
           "reddit": "http://localhost:29999",
           "gitlab": "http://localhost:28023"},
}

BASE_URL = ("http://10.77.110.162:8888/v1,http://10.77.110.162:8889/v1,"
            "http://127.0.0.1:8888/v1,http://127.0.0.1:8889/v1")


class RecordingClient:
    """Wraps the real client; snapshots every target request/response verbatim.

    ``stream`` labels each call: "agent" (decision calls) vs "scribe" (the
    env-side history scribe goes through complete_target)."""

    def __init__(self, inner: OpenAICompatLLMClient) -> None:
        self.inner = inner
        self.calls: list[dict] = []

    def _record(self, stream: str, messages: list[dict], reply: str,
                t0: float) -> None:
        self.calls.append({
            "call_index": len(self.calls),
            "stream": stream,
            "t_wall_s": round(time.time() - t0, 2),
            "request_messages": json.loads(json.dumps(messages)),
            "response": reply,
            "usage": self.inner.pop_last_usage() or {},
        })

    def complete_target_messages(self, messages: list[dict], **kw) -> str:
        t0 = time.time()
        reply = self.inner.complete_target_messages(messages, **kw)
        self._record("agent", messages, reply, t0)
        return reply

    def complete_target(self, system: str, user: str, **kw) -> str:
        t0 = time.time()
        reply = self.inner.complete_target(system, user, **kw)
        self._record("scribe", [{"role": "system", "content": system},
                                {"role": "user", "content": user}], reply, t0)
        return reply

    def count_tokens(self, text: str, **kw):
        return self.inner.count_tokens(text, **kw)


def build_latest_turn(objective: str, action_history: list[str], url: str,
                      observation: str, last_result: str) -> str:
    """Field-standard turn: current observation only + plain action history."""
    hist = "\n".join(action_history) if action_history else "(none yet)"
    parts = [
        f"OBJECTIVE: {objective}",
        ("ACTION HISTORY (oldest first — only your past actions are retained; "
         "earlier observations are NOT shown):\n" + hist),
        f"CURRENT URL: {url}",
        f"OBSERVATION:\n{observation}",
    ]
    if last_result:
        parts.append(f"PREVIOUS ACTION RESULT: {last_result}")
    return "\n\n".join(parts)


def run_episode_latest(item: dict, skill_text: str, client: RecordingClient,
                       cfg, lease, workdir: str) -> dict:
    """agent.run_episode's exact loop skeleton with ONE change: the prompt is
    rebuilt each turn as [system, current-turn user] — no past observations."""
    from playwright.sync_api import sync_playwright
    from css.envs.webarena.vendor import (ActionTypes, TextObervationProcessor,
                                          create_id_based_action,
                                          execute_action)

    extra = getattr(cfg, "extra", {}) or {}
    max_turns = int(getattr(cfg, "max_turns", 0) or 30)
    har_content = str(extra.get("webarena_har_content", "omit"))
    nav_timeout_ms = int(extra.get("webarena_nav_timeout_ms", 30000))

    system = prompts.build_system_prompt(skill_text)
    objective = item.get("intent", "")
    action_history: list[str] = []
    stop_payload = None
    n_turns = 0
    last_result = ""

    os.makedirs(workdir, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel="chromium")
        ctx = browser.new_context(
            viewport=VIEWPORT, device_scale_factor=1,
            record_har_path=os.path.join(workdir, "network.har"),
            record_har_content=har_content)
        ctx.set_default_timeout(nav_timeout_ms)
        try:
            page = ctx.new_page()
            page.goto(resolve_start_url(item, lease))
            proc = TextObervationProcessor("accessibility_tree", True, VIEWPORT)

            for _ in range(max_turns):
                n_turns += 1
                try:
                    cdp = ctx.new_cdp_session(page)
                    obs = proc.process(page, cdp)
                except Exception as exc:  # noqa: BLE001 — obs failure ends episode
                    logging.warning("latest-arm observation failed: %s", exc)
                    break
                user = build_latest_turn(objective, action_history, page.url,
                                         obs, last_result)
                messages = [{"role": "system", "content": system},
                            {"role": "user", "content": user}]
                reply = client.complete_target_messages(messages)

                action_str = extract_action_str(reply)
                last_result = ""
                hist_line = f"{len(action_history) + 1}. {action_str}"
                try:
                    action = create_id_based_action(action_str)
                except Exception as exc:  # noqa: BLE001 — ActionParsingError et al.
                    last_result = (f"Invalid action '{action_str[:120]}': {exc}. "
                                   "Reply with exactly one action from the "
                                   "action space, fenced in triple backticks.")
                    action_history.append(hist_line + f"\n   result: {last_result}")
                    continue
                if action["action_type"] == ActionTypes.STOP:
                    stop_payload = prompts.parse_stop_payload(
                        action.get("answer", ""))
                    action_history.append(hist_line)
                    break
                try:
                    page = execute_action(action, page, ctx, proc)
                    page.wait_for_load_state("domcontentloaded")
                    action_history.append(hist_line)
                except Exception as exc:  # noqa: BLE001 — surface to the agent
                    last_result = f"Action failed: {type(exc).__name__}: {exc}"[:400]
                    action_history.append(hist_line + f"\n   result: {last_result}")
        finally:
            ctx.close()
            browser.close()

    if stop_payload is None:
        stop_payload = {"task_type": "retrieve", "status": "UNKNOWN_ERROR",
                        "retrieved_data": None,
                        "error_details": f"no stop action within {max_turns} turns"}
    write_agent_response(workdir, stop_payload)
    return {"n_turns": n_turns, "agent_response": stop_payload,
            "action_history": action_history}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", type=int, default=784)
    ap.add_argument("--mode", choices=["full", "latest", "history"],
                    required=True,
                    help="full/latest = frozen probe arms; history = the "
                         "PRODUCTION single-turn + trajectory-history agent "
                         "(css.envs.webarena.agent as of 2026-07-08)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--stack", default="s1")
    ap.add_argument("--nav-timeout-ms", type=int, default=30000,
                    help="initial goto waits for 'load'; gitlab sub-resources "
                         "can stall past 30s (observed 2026-07-08)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s")

    record = None
    for split in ("train", "val", "test"):
        with open(f"data/webarena_splits/{split}.json") as f:
            for r in json.load(f):
                if r.get("task_id") == args.task:
                    record, found_split = r, split
                    break
        if record:
            break
    if record is None:
        sys.exit(f"task {args.task} not found in sealed splits")

    os.makedirs(args.out, exist_ok=True)
    lease = Lease(stack=args.stack, urls=STACKS[args.stack], sites=(),
                  exclusive=False)
    cfg = SimpleNamespace(max_turns=30,
                          extra={"webarena_har_content": "omit",
                                 "webarena_nav_timeout_ms": args.nav_timeout_ms})
    inner = OpenAICompatLLMClient(
        base_url=BASE_URL, api_key="token-abc123",
        target_model="qwen3.6-35b-a3b", optimizer_model="qwen3.6-35b-a3b",
        max_tokens=24576, temperature=0.7, enable_thinking=False,
        timeout_seconds=1800)
    client = RecordingClient(inner)

    t0 = time.time()
    if args.mode == "full":
        # The resend-everything loop was REPLACED in css.envs.webarena.agent
        # on 2026-07-08; its probe data lives in runs/wa_ctx_ab_0708/full and
        # the frozen implementation in git history (commit faf11e3 and prior).
        sys.exit("--mode full retired: run_episode is now the single-turn "
                 "history agent; see runs/wa_ctx_ab_0708/full for the "
                 "archived arm")
    if args.mode == "history":
        result = run_episode(record, "", client, cfg, lease, args.out)
        payload = {"n_turns": result["n_turns"],
                   "agent_response": result["agent_response"],
                   "canonical_messages": result["messages"]}
    else:
        result = run_episode_latest(record, "", client, cfg, lease, args.out)
        payload = {"n_turns": result["n_turns"],
                   "agent_response": result["agent_response"],
                   "action_history": result["action_history"]}
    wall_s = round(time.time() - t0, 1)

    with open(os.path.join(args.out, "calls.json"), "w") as f:
        json.dump(client.calls, f, ensure_ascii=False, indent=1)
    meta = {
        "task_id": args.task, "split": found_split, "mode": args.mode,
        "stack": args.stack, "intent": record.get("intent"),
        "expected_for_reference_only": record.get("eval"),
        "wall_s": wall_s, "n_llm_calls": len(client.calls),
        "usage_totals": {
            "prompt_tokens": sum(c["usage"].get("prompt_tokens", 0)
                                 for c in client.calls),
            "completion_tokens": sum(c["usage"].get("completion_tokens", 0)
                                     for c in client.calls),
        },
        "per_call_prompt_tokens": [c["usage"].get("prompt_tokens", 0)
                                   for c in client.calls],
        "client": {"temperature_effective":
                   "complete_target_messages default (0.0), same as the real "
                   "experiment's agent path", "max_tokens": 16384,
                   "enable_thinking": False},
    }
    meta.update(payload)
    with open(os.path.join(args.out, "result.json"), "w") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    logging.info("DONE mode=%s turns=%s calls=%d wall=%ss status=%s",
                 args.mode, payload["n_turns"], len(client.calls), wall_s,
                 payload["agent_response"].get("status"))


if __name__ == "__main__":
    main()
