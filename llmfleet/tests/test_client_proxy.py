"""End-to-end tests: FleetClient and the sidecar proxy against stub upstreams.

Real localhost HTTP servers (stdlib) — these are the integration guarantees
other projects rely on when they adopt llmfleet.
"""
from __future__ import annotations

import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from llmfleet import FleetClient, SESSION_HEADER
from llmfleet.proxy import serve


class _StubUpstream(BaseHTTPRequestHandler):
    """Minimal OpenAI-compatible upstream; records what it saw."""

    protocol_version = "HTTP/1.1"
    name = "stub"
    seen: list = []

    def log_message(self, *a):  # noqa: D102
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length).decode())
        type(self).seen.append({
            "path": self.path,
            "auth": self.headers.get("Authorization", ""),
            "session": self.headers.get(SESSION_HEADER, ""),
            "stream": bool(payload.get("stream")),
        })
        if payload.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for piece in ("data: one\n\n", "data: two\n\n", "data: [DONE]\n\n"):
                chunk = piece.encode()
                self.wfile.write(b"%x\r\n" % len(chunk))
                self.wfile.write(chunk + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            return
        body = json.dumps({
            "model": "stub-model", "served_by": type(self).name,
            "choices": [{"message": {"content": "OK from %s" % type(self).name}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _start_upstream(name: str) -> "tuple[ThreadingHTTPServer, str]":
    handler = type("Stub_%s" % name, (_StubUpstream,), {"name": name, "seen": []})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, "http://127.0.0.1:%d/v1" % server.server_address[1]


@pytest.fixture()
def upstreams():
    servers = []
    urls = []
    handlers = []
    for name in ("alpha", "beta"):
        server, url = _start_upstream(name)
        servers.append(server)
        urls.append(url)
        handlers.append(server.RequestHandlerClass)
    yield urls, handlers
    for s in servers:
        s.shutdown()


def test_fleet_client_roundtrip_and_session_stickiness(upstreams):
    urls, handlers = upstreams
    client = FleetClient(",".join(urls), api_key="tok", enable_poller=False)
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}]
    first = client.post_chat({"model": "m", "messages": msgs})
    assert first["choices"][0]["message"]["content"].startswith("OK from")
    served = first["served_by"]
    # Same session (same head) sticks to the same upstream across turns.
    for _ in range(4):
        again = client.post_chat({"model": "m", "messages": msgs + [
            {"role": "assistant", "content": "x"}, {"role": "user", "content": "y"}]})
        assert again["served_by"] == served
    total_seen = sum(len(h.seen) for h in handlers)
    assert total_seen == 5
    # Auth header propagated.
    assert all(rec["auth"] == "Bearer tok"
               for h in handlers for rec in h.seen)


def test_fleet_client_failover_on_dead_node(upstreams):
    urls, handlers = upstreams
    dead = "http://127.0.0.1:1/v1"  # nothing listens there
    client = FleetClient(dead + "," + urls[0], enable_poller=False, retries=3)
    out = client.post_chat({"model": "m", "messages": [
        {"role": "system", "content": "s"}, {"role": "user", "content": "u"}]})
    assert out["served_by"] == "alpha"
    snap = client.snapshot()
    assert snap["counters"]["cold"] >= 1


def test_proxy_forwards_sessions_and_snapshot(upstreams):
    urls, handlers = upstreams
    proxy = serve(",".join(urls), host="127.0.0.1", port=0, api_key="ptok")
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    paddr = "http://127.0.0.1:%d" % proxy.server_address[1]
    try:
        payload = {"model": "m", "messages": [
            {"role": "system", "content": "S"}, {"role": "user", "content": "U"}]}
        req = urllib.request.Request(
            paddr + "/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        outs = []
        for _ in range(3):
            with urllib.request.urlopen(req, timeout=10) as resp:
                outs.append(json.loads(resp.read().decode())["served_by"])
        assert len(set(outs)) == 1  # session stickiness through the proxy
        # Per-node key applied when the client sent no Authorization.
        assert all(rec["auth"] == "Bearer ptok"
                   for h in handlers for rec in h.seen)
        # Explicit session header overrides the body heuristic.
        req2 = urllib.request.Request(
            paddr + "/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     SESSION_HEADER: "custom-key"}, method="POST")
        with urllib.request.urlopen(req2, timeout=10) as resp:
            json.loads(resp.read().decode())
        # Observability endpoint.
        with urllib.request.urlopen(paddr + "/llmfleet/snapshot", timeout=5) as resp:
            snap = json.loads(resp.read().decode())
        assert snap["sessions"] >= 2 and len(snap["urls"]) == 2
    finally:
        proxy.shutdown()


def test_proxy_streams_sse_passthrough(upstreams):
    urls, _ = upstreams
    proxy = serve(urls[0], host="127.0.0.1", port=0)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    paddr = "http://127.0.0.1:%d" % proxy.server_address[1]
    try:
        payload = {"model": "m", "stream": True, "messages": [
            {"role": "user", "content": "hi"}]}
        req = urllib.request.Request(
            paddr + "/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode()
        assert "data: one" in body and "data: [DONE]" in body
    finally:
        proxy.shutdown()
