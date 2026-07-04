"""llmfleet CLI — ``llmfleet probe|proxy|snapshot``."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import urllib.request

from llmfleet.registry import chat_urls, parse_fleet, resolve_endpoints


def _fleet_string(args) -> str:
    fleet = resolve_endpoints(
        getattr(args, "endpoints", "") or "",
        registry_path=getattr(args, "registry", "") or "",
    )
    if not fleet:
        print("no endpoints: pass --endpoints/--registry or set LLMFLEET_ENDPOINTS")
        sys.exit(1)
    return fleet


def cmd_probe(args) -> int:
    fleet = parse_fleet(_fleet_string(args))
    urls = chat_urls(fleet)
    print("Fleet (%d endpoint%s):" % (len(fleet), "s" if len(fleet) > 1 else ""))
    ok = True
    for node, url in zip(fleet, urls):
        payload = {
            "model": args.model,
            "messages": [{"role": "user", "content": "Say OK."}],
            "max_tokens": 8, "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        headers = {"Content-Type": "application/json"}
        key = node.get("key") or args.api_key
        if key:
            headers["Authorization"] = "Bearer %s" % key
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=args.timeout) as resp:
                data = json.loads(resp.read().decode())
            text = ((data.get("choices") or [{}])[0].get("message") or {}
                    ).get("content") or ""
            print("  %-45s OK   %.2fs  model=%s  reply=%r"
                  % (node["url"], time.time() - t0, data.get("model", "?"),
                     text[:24]))
        except Exception as e:  # noqa: BLE001
            ok = False
            print("  %-45s FAIL %s: %s" % (node["url"], type(e).__name__, e))
    return 0 if ok else 2


def cmd_proxy(args) -> int:
    from llmfleet.proxy import serve

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s")
    host, _, port = args.listen.rpartition(":")
    server = serve(_fleet_string(args), host=host or "127.0.0.1",
                   port=int(port), api_key=args.api_key,
                   load_factor=args.load_factor)
    print("llmfleet proxy listening on %s — point clients at http://%s/v1"
          % (args.listen, args.listen))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_snapshot(args) -> int:
    url = "http://%s/llmfleet/snapshot" % args.listen
    with urllib.request.urlopen(url, timeout=5) as resp:
        print(resp.read().decode())
    return 0


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(prog="llmfleet", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("probe", help="end-to-end health probe of every node")
    p.add_argument("--registry", default="")
    p.add_argument("--endpoints", default="", help="comma-separated override")
    p.add_argument("--model", default="qwen3.6-35b-a3b")
    p.add_argument("--api-key", default="")
    p.add_argument("--timeout", type=float, default=60.0)
    p.set_defaults(fn=cmd_probe)

    p = sub.add_parser("proxy", help="run the zero-code-change sidecar")
    p.add_argument("--registry", default="")
    p.add_argument("--endpoints", default="")
    p.add_argument("--listen", default="127.0.0.1:9000")
    p.add_argument("--api-key", default="")
    p.add_argument("--load-factor", type=float, default=1.25)
    p.set_defaults(fn=cmd_proxy)

    p = sub.add_parser("snapshot", help="dump a running proxy's router state")
    p.add_argument("--listen", default="127.0.0.1:9000")
    p.set_defaults(fn=cmd_snapshot)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
