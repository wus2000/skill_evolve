#!/usr/bin/env python3
"""Render a wa_context_ab_test arm directory into a verbatim Markdown
transcript. No elision, no truncation: every LLM request/response appears in
full. The system prompt is identical across calls, so it is printed once with
an explicit note (information-equivalent; calls.json stays the byte-level
record).

Usage: python3 tools/wa_ab_render_md.py <arm_dir> <out_md>
"""
from __future__ import annotations

import json
import sys


def main() -> None:
    arm_dir, out_md = sys.argv[1], sys.argv[2]
    meta = json.load(open(f"{arm_dir}/result.json"))
    calls = json.load(open(f"{arm_dir}/calls.json"))

    mode = meta["mode"]
    lines: list[str] = []
    w = lines.append
    w(f"# wa_{meta['task_id']:04d} context A/B probe — arm `{mode.upper()}`")
    w("")
    title = ("all past observations retained in the prompt (current mechanism)"
             if mode == "full" else
             "only the CURRENT observation in the prompt; history = plain "
             "action list (survey field standard)")
    w(f"**Strategy:** {title}")
    w("")
    w(f"- intent: {meta['intent']}")
    exp = (meta.get("expected_for_reference_only") or [{}])[0].get(
        "expected", {})
    w(f"- reference answer (never shown to the agent): "
      f"`{exp.get('retrieved_data')}`")
    ar = meta.get("agent_response") or {}
    w(f"- outcome: status=`{ar.get('status')}` "
      f"retrieved_data=`{ar.get('retrieved_data')}` "
      f"turns={meta.get('n_turns')} llm_calls={meta['n_llm_calls']} "
      f"wall={meta['wall_s']}s")
    u = meta["usage_totals"]
    w(f"- tokens: prompt total={u['prompt_tokens']:,} "
      f"completion total={u['completion_tokens']:,}")
    w("")
    w("## Per-call prompt size (tokens, from endpoint usage)")
    w("")
    w("| call | prompt_tokens | completion_tokens | wall_s |")
    w("|---|---|---|---|")
    for c in calls:
        cu = c.get("usage", {})
        w(f"| {c['call_index']} | {cu.get('prompt_tokens', 0):,} | "
          f"{cu.get('completion_tokens', 0):,} | {c.get('t_wall_s')} |")
    w("")

    system = calls[0]["request_messages"][0]["content"] if calls else ""
    w("## System prompt (IDENTICAL in every call of this arm — printed once)")
    w("")
    w("````text")
    w(system)
    w("````")
    w("")

    if mode == "full":
        w("## Conversation (verbatim, complete)")
        w("")
        w("The FULL arm keeps ONE growing message list; call *k*'s request is "
          "exactly the messages up to and including turn *k*'s user message. "
          "The final message list below therefore reconstructs every request "
          "without loss.")
        w("")
        msgs = meta["final_messages"]
        turn = 0
        for m in msgs:
            role = m["role"]
            if role == "system":
                continue  # printed above
            if role == "user":
                turn += 1
                w(f"### Turn {turn} — user (verbatim)")
            else:
                w(f"### Turn {turn} — assistant (verbatim)")
            w("")
            w("````text")
            w(m["content"])
            w("````")
            w("")
    else:
        w("## Calls (verbatim, complete)")
        w("")
        w("The LATEST arm rebuilds the prompt every call: [system, user]. "
          "Each call's user message is printed in full below (they differ "
          "call to call).")
        w("")
        for c in calls:
            k = c["call_index"]
            user = c["request_messages"][-1]["content"]
            w(f"### Call {k} — user (verbatim)")
            w("")
            w("````text")
            w(user)
            w("````")
            w("")
            w(f"### Call {k} — assistant response (verbatim)")
            w("")
            w("````text")
            w(c["response"])
            w("````")
            w("")
        w("## Final action history (as accumulated by the agent)")
        w("")
        w("````text")
        w("\n".join(meta.get("action_history", [])))
        w("````")
        w("")

    with open(out_md, "w") as f:
        f.write("\n".join(lines))
    print(f"wrote {out_md} ({len(lines)} lines)")


if __name__ == "__main__":
    main()
