"""llmfleet.registry — fleet definition: one file, per-node annotations.

A fleet registry is a text file, one OpenAI-compatible base URL per line
(``#`` comments ignored), with optional space-separated ``key=value``
annotations::

    http://a100-host:8888/v1   w=1.0  max_inflight=256
    http://h20-host:8888/v1    w=0.5  max_inflight=320  key=other-token
    http://plain-host:8888/v1  metrics=none

Annotations:
  w             rate PRIOR (relative service speed). The router's adaptive
                busy-time throughput estimate takes over once the node runs
                warm; the prior seeds cold start and the metrics-dead
                fallback. Default 1.0.
  max_inflight  hard per-node concurrency cap — the client-side shadow of
                the server's max_num_seqs; the weak node's tail-latency
                fuse. Default 0 = uncapped.
  key           per-node API key override (heterogeneous auth). Default:
                the client-level key.
  metrics       "vllm" (default: poll /metrics for server-truth load) or
                "none" (skip polling; the router degrades to local
                in-flight + prior weight for this node).

The resolved fleet travels as ONE string (entries comma-separated,
annotations ``|``-encoded) so it fits any single-string config channel::

    http://a:8888/v1|w=1.0|max_inflight=256,http://b:8888/v1|w=0.5
"""
from __future__ import annotations

import os

DEFAULT_ENV_VARS = ("LLMFLEET_ENDPOINTS",)


def encode_line(line: str) -> str:
    """Registry line -> fleet-string entry (whitespace annotations -> ``|``)."""
    return "|".join(part for part in line.split() if part)


def resolve_endpoints(
    default: str = "",
    *,
    registry_path: str = "",
    env_vars: "tuple[str, ...]" = DEFAULT_ENV_VARS,
) -> str:
    """Resolve the fleet to its single-string form.

    Priority: first non-empty env var in ``env_vars`` (comma-separated,
    annotations either ``|``- or space-encoded) > the registry file >
    ``default``.
    """
    for var in env_vars:
        env = (os.environ.get(var) or "").strip()
        if env:
            entries = [encode_line(e.strip()) for e in env.split(",") if e.strip()]
            if entries:
                return ",".join(entries)
    if registry_path and os.path.exists(registry_path):
        entries = []
        try:
            with open(registry_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        entries.append(encode_line(line))
        except OSError:
            entries = []
        if entries:
            return ",".join(entries)
    return default


def parse_fleet(fleet_string: str) -> "list[dict]":
    """Decode a fleet string into per-node dicts.

    Returns ``[{"url", "w", "max_inflight", "key", "metrics"}, ...]``.
    Unknown or malformed annotations are ignored (never fatal).
    """
    fleet = []
    for entry in (fleet_string or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split("|")
        node = {"url": parts[0].strip(), "w": 1.0, "max_inflight": 0,
                "key": "", "metrics": "vllm"}
        for annot in parts[1:]:
            if "=" not in annot:
                continue
            k, _, v = annot.partition("=")
            k, v = k.strip(), v.strip()
            try:
                if k == "w":
                    node["w"] = max(0.01, float(v))
                elif k == "max_inflight":
                    node["max_inflight"] = max(0, int(v))
                elif k == "key":
                    node["key"] = v
                elif k == "metrics":
                    node["metrics"] = v if v in ("vllm", "none") else "vllm"
            except ValueError:
                continue
        if node["url"]:
            fleet.append(node)
    return fleet


def chat_urls(fleet: "list[dict]") -> "list[str]":
    """Per-node chat-completions URLs."""
    out = []
    for node in fleet:
        base = node["url"]
        out.append(base if base.endswith("/chat/completions")
                   else base.rstrip("/") + "/chat/completions")
    return out
