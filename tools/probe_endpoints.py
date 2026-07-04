#!/usr/bin/env python3
"""Probe every LLM endpoint in this repo's fleet registry (llmfleet wrapper).

Back-compat entry point: resolves exactly like the CSS launchers
(CSS_LLM_ENDPOINTS env > config/llm_endpoints.txt > none) and delegates to
``llmfleet probe``. Direct package equivalent:

  python3 -m llmfleet.cli probe --registry config/llm_endpoints.txt
"""
from __future__ import annotations  # server runs Python 3.8

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from css.model.endpoints import registry_path, resolve_base_url  # noqa: E402
from llmfleet.cli import main as llmfleet_main  # noqa: E402


def main() -> int:
    fleet = resolve_base_url("")
    if not fleet:
        print("No endpoints resolved (env CSS_LLM_ENDPOINTS unset and %s missing)"
              % registry_path())
        return 1
    argv = ["probe", "--endpoints", fleet]
    passthrough = sys.argv[1:]
    if "--api-key" not in passthrough:
        argv += ["--api-key", "token-abc123"]
    return llmfleet_main(argv + passthrough)


if __name__ == "__main__":
    sys.exit(main())
