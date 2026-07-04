#!/usr/bin/env python3
"""Probe every LLM endpoint in the fleet registry end-to-end.

Resolves the fleet exactly the way launchers do (css/model/endpoints.py:
CSS_LLM_ENDPOINTS env > config/llm_endpoints.txt > default), sends each
endpoint a tiny chat completion, and reports health + latency + served model.
Run this after adding a node to the registry, before relying on it.

Usage:
  python3 tools/probe_endpoints.py [--model qwen3.6-35b-a3b] [--api-key ...]
"""
from __future__ import annotations  # server runs Python 3.8

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from css.model.endpoints import registry_path, resolve_base_url  # noqa: E402


def probe(base: str, model: str, api_key: str, timeout: float) -> "tuple[bool, str]":
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Say OK."}],
        "max_tokens": 8,
        "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer %s" % api_key},
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        latency = time.time() - t0
        served = data.get("model", "?")
        text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "")
        return True, "OK   %.2fs  model=%s  reply=%r" % (latency, served, text[:24])
    except urllib.error.HTTPError as e:
        return False, "FAIL HTTP %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:120])
    except Exception as e:  # noqa: BLE001
        return False, "FAIL %s: %s" % (type(e).__name__, e)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="qwen3.6-35b-a3b")
    ap.add_argument("--api-key", default="token-abc123")
    ap.add_argument("--timeout", type=float, default=60.0)
    args = ap.parse_args()

    base_urls = [u for u in resolve_base_url("").split(",") if u]
    if not base_urls:
        print("No endpoints resolved (env CSS_LLM_ENDPOINTS unset and %s missing)"
              % registry_path())
        return 1
    print("Fleet (%d endpoint%s):" % (len(base_urls), "s" if len(base_urls) > 1 else ""))
    ok = True
    for base in base_urls:
        healthy, detail = probe(base, args.model, args.api_key, args.timeout)
        ok = ok and healthy
        print("  %-45s %s" % (base, detail))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
