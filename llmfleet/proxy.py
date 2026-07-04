"""llmfleet.proxy — the zero-code-change sidecar.

Run one proxy per machine; point ANY OpenAI-compatible client (openai SDK,
litellm, curl, any language) at it::

    llmfleet proxy --registry endpoints.txt --listen 127.0.0.1:9000
    # then everywhere: base_url = http://127.0.0.1:9000/v1

Behavior:
  * POST /v1/... — parses the JSON body for the session key (system + first
    user message; the ``X-LLMFleet-Session`` header overrides), routes with
    the full two-tier stack, and forwards the request verbatim. Streaming
    (``"stream": true``) responses are relayed chunk-by-chunk (SSE-safe).
  * Upstream HTTP errors are forwarded verbatim (status + body) — the proxy
    never masks application errors; only connection-level failures fail over
    to another replica (up to 3 placements).
  * GET /llmfleet/snapshot — router observability JSON.
  * Auth: the client's Authorization header is forwarded as-is; if absent,
    the per-node ``key=`` annotation (or --api-key) is applied.

One proxy per MACHINE also centralizes router state for every local process:
shared in-flight truth, one /metrics poller — the strongest multi-process
coordination short of a dedicated global router.
"""
from __future__ import annotations

import json
import logging
import threading  # noqa: F401 — ThreadingHTTPServer uses threads per request
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from llmfleet.registry import parse_fleet
from llmfleet.routing import ReplicaRouter
from llmfleet.session import SESSION_HEADER, session_key_from_payload

_log = logging.getLogger("llmfleet.proxy")

_HOP_HEADERS = {  # not forwarded in either direction
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}
_PLACEMENT_ATTEMPTS = 3
_CHUNK = 16384


class FleetProxy:
    """Router + upstream forwarding shared by all handler threads."""

    def __init__(self, fleet_string: str, *, api_key: str = "",
                 load_factor: float = 1.25, cooldown_s: float = 15.0) -> None:
        self.fleet = parse_fleet(fleet_string)
        if not self.fleet:
            raise ValueError("proxy needs at least one endpoint")
        # Node roots: strip a trailing /v1 (client paths arrive with /v1/...).
        self.roots = []
        for node in self.fleet:
            url = node["url"].rstrip("/")
            self.roots.append(url[:-3] if url.endswith("/v1") else url)
        self.api_key = api_key
        self.router = ReplicaRouter(
            [n["url"] for n in self.fleet],
            weights=[n["w"] for n in self.fleet],
            max_inflights=[n["max_inflight"] for n in self.fleet],
            metrics_enabled=[n.get("metrics", "vllm") != "none" for n in self.fleet],
            load_factor=load_factor,
            cooldown_s=cooldown_s,
        )

    def node_auth(self, index: int) -> str:
        key = self.fleet[index].get("key") or self.api_key
        return ("Bearer %s" % key) if key else ""


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    proxy: FleetProxy = None  # type: ignore[assignment]

    def log_message(self, fmt, *args):  # noqa: D102 — quiet access log
        _log.debug(fmt, *args)

    # ── Observability ───────────────────────────────────────────────────────
    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/") == "/llmfleet/snapshot":
            body = json.dumps(self.proxy.router.snapshot(), ensure_ascii=False,
                              indent=1).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._plain(404, b'{"error": "llmfleet proxy: unknown path"}')

    # ── The forwarding path ─────────────────────────────────────────────────
    def do_POST(self):  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
        except Exception:  # noqa: BLE001
            self._plain(400, b'{"error": "unreadable body"}')
            return

        session_key = self.headers.get(SESSION_HEADER) or ""
        payload = None
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:  # noqa: BLE001 — non-JSON bodies still forward
            pass
        if not session_key and payload is not None:
            session_key = session_key_from_payload(payload)
        wants_stream = bool(isinstance(payload, dict) and payload.get("stream"))

        router = self.proxy.router
        last_err = "no endpoint available"
        for _ in range(_PLACEMENT_ATTEMPTS):
            index = router.acquire(session_key)
            url = self.proxy.roots[index] + self.path
            t0 = time.time()
            ok = False
            try:
                upstream_headers = {
                    k: v for k, v in self.headers.items()
                    if k.lower() not in _HOP_HEADERS
                }
                if "Authorization" not in upstream_headers:
                    auth = self.proxy.node_auth(index)
                    if auth:
                        upstream_headers["Authorization"] = auth
                req = urllib.request.Request(
                    url, data=body, headers=upstream_headers, method="POST")
                with urllib.request.urlopen(req, timeout=1800) as resp:
                    self._relay(resp, streaming=wants_stream)
                ok = True
                return
            except urllib.error.HTTPError as e:
                # Upstream RESPONDED: forward its error verbatim, do not mask.
                err_body = e.read()
                self._plain(e.code, err_body,
                            e.headers.get("Content-Type", "application/json"))
                ok = True  # the node is alive; not a health event
                return
            except (urllib.error.URLError, OSError) as e:
                router.mark_unhealthy(index)
                last_err = str(e)
            finally:
                router.release(index, time.time() - t0, ok=ok)
        self._plain(502, json.dumps(
            {"error": "llmfleet proxy: all placements failed: %s" % last_err}
        ).encode("utf-8"))

    # ── Response relaying ───────────────────────────────────────────────────
    def _relay(self, resp, *, streaming: bool) -> None:
        if streaming:
            self.send_response(resp.status)
            for k, v in resp.headers.items():
                if k.lower() not in _HOP_HEADERS:
                    self.send_header(k, v)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            while True:
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                self.wfile.write(b"%x\r\n" % len(chunk))
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            return
        data = resp.read()
        self.send_response(resp.status)
        for k, v in resp.headers.items():
            if k.lower() not in _HOP_HEADERS:
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _plain(self, code: int, body: bytes,
               content_type: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(fleet_string: str, *, host: str = "127.0.0.1", port: int = 9000,
          api_key: str = "", load_factor: float = 1.25,
          cooldown_s: float = 15.0) -> ThreadingHTTPServer:
    """Build (and return) the proxy server; caller runs serve_forever()."""
    proxy = FleetProxy(fleet_string, api_key=api_key,
                       load_factor=load_factor, cooldown_s=cooldown_s)
    handler = type("BoundHandler", (_Handler,), {"proxy": proxy})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    _log.info("llmfleet proxy on %s:%d over %d node(s)", host, port,
              len(proxy.fleet))
    return server
