#!/usr/bin/env python3
"""Bootstrap WebArena login state for every (stack, cookie-site) pair.

Run once on the harness host (127) before a WebArena experiment; the env's
refresh hook keeps each jar fresh afterwards. shopping_admin is skipped by
design — it authenticates by HTTP header, not by cookie.

    python3 tools/webarena_login.py [--auth-dir .auth/webarena] [--stack s1]

Verifies each login by visiting an authenticated-only page; raises on failure
rather than writing an anonymous jar.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from css.envs.webarena import auth

# The farm topology, imported from the launcher so the two never drift.
from run_experiment_webarena_server import STACKS


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--auth-dir", default=".auth/webarena")
    ap.add_argument("--stack", default="", help="one stack only (default: all)")
    args = ap.parse_args()

    stacks = ({args.stack: STACKS[args.stack]} if args.stack else STACKS)
    os.makedirs(args.auth_dir, exist_ok=True)
    ok, fail = [], []
    for stack, urls in stacks.items():
        for site in auth.COOKIE_SITES:
            if site not in urls:
                continue
            try:
                auth.generate_state(stack, site, urls[site], args.auth_dir)
                ok.append(f"{stack}/{site}")
            except Exception as exc:  # noqa: BLE001 — report all, fail at the end
                fail.append(f"{stack}/{site}: {exc}")
                print(f"  FAIL {stack}/{site}: {exc}")
    print(f"\nlogged in: {len(ok)}   failed: {len(fail)}")
    for f in fail:
        print("  " + f)
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
