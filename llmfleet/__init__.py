"""llmfleet — KV-cache-aware client routing over an OpenAI-compatible fleet.

Standalone, stdlib-only, Python >= 3.8. Drop-in for any project in two modes:

LIBRARY (one line changed)::

    from llmfleet import FleetClient
    client = FleetClient.from_registry("endpoints.txt", api_key="...")
    data = client.post_chat({"model": "m", "messages": [...]})

SIDECAR PROXY (zero code changed — any client, any language)::

    llmfleet proxy --registry endpoints.txt --listen 127.0.0.1:9000
    # everywhere: base_url = http://127.0.0.1:9000/v1

What you get (see README.md for the full design):
  * two-tier scheduling — cold sessions (no KV cache anywhere) place by
    weighted least expected wait; warm turns stick to their home replica
    for prefix-cache hits, with bounded spill and re-homing;
  * server-truth load — a background poll of each vLLM node's /metrics
    (global running/waiting + real token throughput), which also coordinates
    multiple independent client processes with zero IPC;
  * heterogeneous fleets — per-node rate priors and hard concurrency caps
    in the registry; adaptive busy-time throughput estimation on top;
  * reliability — exponential-backoff cooldowns, slow-start ramps, latency
    health vetoes, staged degradation when signals disappear.
"""
from __future__ import annotations

from llmfleet.client import FleetClient
from llmfleet.registry import (
    chat_urls,
    encode_line,
    parse_fleet,
    resolve_endpoints,
)
from llmfleet.routing import ReplicaRouter, parse_metrics_text
from llmfleet.session import (
    SESSION_HEADER,
    session_key_from_messages,
    session_key_from_payload,
)

__version__ = "0.1.0"

FleetRouter = ReplicaRouter  # public alias


def from_registry(path: str, **kwargs) -> FleetClient:
    """Build a :class:`FleetClient` from a registry file."""
    return FleetClient(resolve_endpoints("", registry_path=path), **kwargs)


FleetClient.from_registry = staticmethod(from_registry)  # type: ignore[attr-defined]

__all__ = [
    "FleetClient", "FleetRouter", "ReplicaRouter",
    "resolve_endpoints", "parse_fleet", "chat_urls", "encode_line",
    "parse_metrics_text",
    "SESSION_HEADER", "session_key_from_messages", "session_key_from_payload",
    "from_registry", "__version__",
]
