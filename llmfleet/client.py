"""llmfleet.client — library-mode integration (Python projects).

``FleetClient`` is a minimal OpenAI-compatible chat client with the full
routing stack behind it: two-tier session scheduling, server-truth load,
health cooldowns, per-node keys. Non-streaming (proxy mode streams).

    from llmfleet import FleetClient
    client = FleetClient("http://a:8888/v1|w=1.0,http://b:8888/v1|w=0.5",
                         api_key="token-abc123")
    data = client.post_chat({"model": "m", "messages": [...]})

Projects with their own HTTP transport can use the router directly::

    idx = client.router.acquire(session_key)
    try:   ... send to client.chat_urls[idx] ...
    finally: client.router.release(idx, elapsed, ok=succeeded)
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from llmfleet.registry import chat_urls, parse_fleet
from llmfleet.routing import ReplicaRouter
from llmfleet.session import session_key_from_payload


class FleetClient:
    def __init__(
        self,
        fleet: "str | list[dict]",
        *,
        api_key: str = "",
        timeout_s: float = 300.0,
        retries: int = 5,
        load_factor: float = 1.25,
        cooldown_s: float = 15.0,
        enable_poller: bool = True,
        extra_headers: "dict[str, str] | None" = None,
    ) -> None:
        self.fleet = parse_fleet(fleet) if isinstance(fleet, str) else list(fleet)
        if not self.fleet:
            raise ValueError("FleetClient needs at least one endpoint")
        self.chat_urls = chat_urls(self.fleet)
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.retries = max(1, int(retries))
        self.extra_headers = dict(extra_headers or {})
        self.router = ReplicaRouter(
            self.chat_urls,
            weights=[n["w"] for n in self.fleet],
            max_inflights=[n["max_inflight"] for n in self.fleet],
            metrics_enabled=[n.get("metrics", "vllm") != "none" for n in self.fleet],
            load_factor=load_factor,
            cooldown_s=cooldown_s,
            enable_poller=enable_poller,
        )

    def _headers(self, index: int) -> "dict[str, str]":
        headers = {"Content-Type": "application/json"}
        key = self.fleet[index].get("key") or self.api_key
        if key:
            headers["Authorization"] = "Bearer %s" % key
        headers.update(self.extra_headers)
        return headers

    def post_chat(
        self,
        payload: "dict[str, Any]",
        *,
        timeout: "float | None" = None,
        session_key: "str | None" = None,
    ) -> "dict[str, Any]":
        """Route + POST one chat completion; returns the parsed response dict.

        Raises on HTTP 4xx immediately; retries connection failures and 5xx
        with exponential backoff across (possibly different) replicas.
        """
        key = session_key if session_key is not None \
            else session_key_from_payload(payload)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        effective_timeout = timeout or self.timeout_s
        last_err: "Exception | None" = None
        for attempt in range(self.retries):
            index = self.router.acquire(key)
            req = urllib.request.Request(
                self.chat_urls[index], data=body,
                headers=self._headers(index), method="POST")
            t0 = time.time()
            ok = False
            try:
                with urllib.request.urlopen(req, timeout=effective_timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                ok = True
                return data
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8", errors="replace")
                last_err = RuntimeError(
                    "fleet endpoint returned HTTP %s: %s" % (e.code, err_body))
                if 400 <= e.code < 500:
                    raise last_err
            except (urllib.error.URLError, OSError) as e:
                self.router.mark_unhealthy(index)
                last_err = RuntimeError("fleet endpoint request failed: %s" % e)
            finally:
                self.router.release(index, time.time() - t0, ok=ok)
            time.sleep(min(2 ** attempt, 30))
        raise last_err  # type: ignore[misc]

    def snapshot(self) -> dict:
        return self.router.snapshot()
