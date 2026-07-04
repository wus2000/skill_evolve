"""Thin shim: the replica router lives in the standalone ``llmfleet`` package.

PURE INFRASTRUCTURE — not part of the CSS mechanism. The router was extracted
into ``llmfleet/`` (repo top level; vendorable, pip-installable, reusable by
other projects). This shim keeps existing ``css.model.routing`` imports
working; new code should import from :mod:`llmfleet` directly.
"""
from __future__ import annotations

from llmfleet.routing import (  # noqa: F401
    BOUND_FLOOR,
    BUSY_THRESHOLD,
    COOLDOWN_BASE_S,
    COOLDOWN_MAX_S,
    DEGRADED_FACTOR,
    DEGRADED_MIN_SAMPLES,
    LATENCY_ALPHA,
    LATENCY_TAU_S,
    POLL_S,
    RATE_HALFLIFE_S,
    RATE_MIN_SAMPLES,
    REHOME_AFTER_SPILLS,
    REHOME_LOCK_S,
    SESSION_TABLE_MAX,
    SESSION_TTL_S,
    SLOWSTART_S,
    STALE_POLLS,
    ReplicaRouter,
    parse_metrics_text,
)
