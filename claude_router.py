#!/usr/bin/env python3
"""claude-router — model-name routing proxy for Claude Code.

Claude Code points ANTHROPIC_BASE_URL at this proxy. Requests are routed by
the "model" field in the JSON body:

  - claude-* (and anything unmatched) -> api.anthropic.com, headers passed
    through UNCHANGED (subscription OAuth keeps working: web search, betas,
    cache TTL, usage endpoints are bit-identical to a direct connection).
  - kimi-* / glm-* / gpt-* etc.       -> the vendor's Anthropic-compatible
    endpoint, with Authorization swapped to the vendor key and the OAuth
    beta flag stripped.

All backends speak the Anthropic Messages API, so no payload translation is
performed — this is a pure streaming forwarder.

Config: TOML (see config.example.toml). Secrets live only in the config file
(chmod 600); they are never logged.
"""

from __future__ import annotations

import argparse
import http.client
import json
import ssl
import sys
import tomllib
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

UPSTREAM_TIMEOUT = 3600  # generation can be very long; Claude Code sets its own timeout

# Model that runs rerouted server-tool calls (override: server_tool_model in config).
SERVER_TOOL_MODEL = "claude-sonnet-5"

# Hop-by-hop headers never forwarded in either direction (RFC 9110 §7.6.1).
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",  # recomputed for the forwarded body
}


@dataclass
class Backend:
    name: str
    scheme: str
    host: str
    base_path: str  # "" or "/coding" etc., no trailing slash
    auth_token: str | None  # None => passthrough (keep client auth headers)
    model_prefixes: list[str] = field(default_factory=list)
    strip_beta: list[str] = field(default_factory=list)
    # Shown in Claude Code's /model picker via the gateway-models cache
    # (claude-integ writes ~/.claude/cache/gateway-models.json from these).
    picker_models: list[dict] = field(default_factory=list)


def load_config(path: str) -> tuple[str, int, list[Backend], Backend, str]:
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    backends: dict[str, Backend] = {}
    for name, spec in raw.get("backends", {}).items():
        url = urlsplit(spec["url"])
        if url.scheme not in ("http", "https") or not url.hostname:
            raise ValueError(f"backend {name}: bad url {spec['url']!r}")
        netloc = url.hostname if url.port is None else f"{url.hostname}:{url.port}"
        backends[name] = Backend(
            name=name,
            scheme=url.scheme,
            host=netloc,
            base_path=url.path.rstrip("/"),
            auth_token=spec.get("auth_token"),
            model_prefixes=list(spec.get("model_prefixes", [])),
            strip_beta=list(spec.get("strip_beta", [])),
            picker_models=[
                {"id": m["id"], "display_name": m.get("display_name", m["id"])}
                for m in spec.get("picker_models", [])
            ],
        )

    default_name = raw.get("default_backend", "anthropic")
    if default_name not in backends:
        raise ValueError(f"default_backend {default_name!r} not defined")
    return (
        raw.get("listen_host", "127.0.0.1"),
        int(raw.get("listen_port", 8399)),
        list(backends.values()),
        backends[default_name],
        raw.get("server_tool_model", SERVER_TOOL_MODEL),
    )


class Router:
    def __init__(self, backends: list[Backend], default: Backend):
        self.default = default
        # longest prefix wins across all backends
        self.prefixes: list[tuple[str, Backend]] = sorted(
            ((p, b) for b in backends for p in b.model_prefixes),
            key=lambda t: len(t[0]),
            reverse=True,
        )

    def pick(self, model: str | None) -> Backend:
        if model:
            for prefix, backend in self.prefixes:
                if model.startswith(prefix):
                    return backend
        return self.default


def _wants_server_tool(parsed: dict | None) -> bool:
    """True if the request declares an Anthropic server tool (web_search_*).

    Only the tools array is inspected: the same marker shows up in ordinary
    conversation text, which must keep its own model-based routing.
    """
    tools = (parsed or {}).get("tools")
    if not isinstance(tools, list):
        return False
    return any(
        isinstance(t, dict) and str(t.get("type", "")).startswith("web_search_")
        for t in tools
    )


ROUTER: Router | None = None
SSL_CTX = ssl.create_default_context()


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "claude-router/1.0"

    # ---- request body -------------------------------------------------
    def _read_body(self) -> bytes:
        if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            chunks = []
            while True:
                size_line = self.rfile.readline(65536).split(b";")[0].strip()
                size = int(size_line, 16)
                if size == 0:
                    self.rfile.readline(65536)  # trailing CRLF
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.read(2)  # chunk CRLF
            return b"".join(chunks)
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    # ---- proxying -----------------------------------------------------
    def _proxy(self):
        body = self._read_body()
        parsed = None
        model = None
        max_tokens = None
        if body[:1] == b"{":
            try:
                obj = json.loads(body)
                if isinstance(obj, dict):
                    parsed = obj
                    model = obj.get("model")
                    max_tokens = obj.get("max_tokens")
            except Exception:
                pass
        backend = ROUTER.pick(model)

        # Claude Code runs WebSearch as a separate /v1/messages call carrying
        # Anthropic's web_search_* server tool, tagged with the session's model.
        # A vendor backend has no such tool, answers from its own weights, and
        # the CLI reports searchCount=0 while showing that invented text as
        # search results. Hand those calls to Anthropic instead.
        if backend.auth_token is not None and _wants_server_tool(parsed):
            parsed["model"] = SERVER_TOOL_MODEL
            body = json.dumps(parsed).encode()
            model, backend = SERVER_TOOL_MODEL, ROUTER.default

        headers = self._upstream_headers(backend)
        path = backend.base_path + self.path

        conn_cls = http.client.HTTPSConnection if backend.scheme == "https" else http.client.HTTPConnection
        kwargs = {"timeout": UPSTREAM_TIMEOUT}
        if backend.scheme == "https":
            kwargs["context"] = SSL_CTX
        conn = conn_cls(backend.host, **kwargs)
        try:
            conn.request(self.command, path, body=body if body else None, headers=headers)
            resp = conn.getresponse()
            self._relay_response(resp)
            status = resp.status
        except (OSError, http.client.HTTPException) as exc:
            self._send_gateway_error(backend, exc)
            status = 502
        finally:
            conn.close()

        # Body size and max_tokens are the two numbers needed to diagnose
        # "input exceeds the context window" 400s (kB is a rough proxy for
        # prompt size; no body content is ever logged).
        self.log_message(
            "%s %s model=%s -> %s [%s] req=%dkB max_tokens=%s",
            self.command,
            self.path.split("?")[0],
            model or "-",
            backend.name,
            status,
            len(body) // 1024,
            max_tokens if max_tokens is not None else "-",
        )

    def _upstream_headers(self, backend: Backend) -> dict[str, str]:
        out: dict[str, str] = {}
        beta_values: list[str] = []
        for name, value in self.headers.items():
            lname = name.lower()
            if lname in HOP_BY_HOP:
                continue
            if lname == "anthropic-beta":
                beta_values.append(value)
                continue
            if backend.auth_token is not None and lname in ("authorization", "x-api-key"):
                continue  # replaced below
            out[name] = value

        if beta_values:
            betas = [b.strip() for v in beta_values for b in v.split(",") if b.strip()]
            betas = [b for b in betas if b not in backend.strip_beta]
            if betas:
                out["anthropic-beta"] = ",".join(betas)

        if backend.auth_token is not None:
            out["Authorization"] = f"Bearer {backend.auth_token}"
        out["Connection"] = "close"
        return out

    def _relay_response(self, resp: http.client.HTTPResponse):
        self.send_response_only(resp.status, resp.reason)
        content_length = None
        for name, value in resp.getheaders():
            lname = name.lower()
            if lname in ("connection", "keep-alive", "transfer-encoding", "content-length"):
                continue
            self.send_header(name, value)
        raw_cl = resp.getheader("Content-Length")
        chunked = raw_cl is None
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            content_length = int(raw_cl)
            self.send_header("Content-Length", raw_cl)
        self.end_headers()
        self.wfile.flush()

        sent = 0
        while True:
            data = resp.read1(65536)
            if not data:
                break
            if chunked:
                self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
            else:
                self.wfile.write(data)
                sent += len(data)
            self.wfile.flush()  # SSE events must reach the client immediately
            if content_length is not None and sent >= content_length:
                break
        if chunked:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

    def _send_gateway_error(self, backend: Backend, exc: Exception):
        payload = json.dumps(
            {
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": f"claude-router: upstream {backend.name} unreachable: {type(exc).__name__}: {exc}",
                },
            }
        ).encode()
        try:
            self.send_response_only(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except OSError:
            pass

    # ---- entry points -------------------------------------------------
    def _handle(self):
        if self.path == "/claude-router/models":
            # Catalog for the /model picker (consumed by claude-integ, which
            # writes it into Claude Code's gateway-models cache).
            models, seen = [], set()
            for _, b in ROUTER.prefixes:  # backend may list several prefixes
                for m in b.picker_models:
                    if m["id"] not in seen:
                        seen.add(m["id"])
                        models.append(m)
            payload = json.dumps({"models": models}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path == "/claude-router/health":
            info = {
                "status": "ok",
                "routes": {p: b.name for p, b in ROUTER.prefixes},
                "default": ROUTER.default.name,
            }
            payload = json.dumps(info).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        try:
            self._proxy()
        except (BrokenPipeError, ConnectionResetError):
            pass  # client went away; upstream conn is closed in _proxy's finally

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_DELETE = _handle
    do_PATCH = _handle
    do_HEAD = _handle
    do_OPTIONS = _handle

    def log_message(self, fmt, *args):  # journald via stderr; never logs headers/bodies
        sys.stderr.write("%s\n" % (fmt % args))
        sys.stderr.flush()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", required=True)
    args = ap.parse_args()

    global ROUTER, SERVER_TOOL_MODEL
    host, port, backends, default, SERVER_TOOL_MODEL = load_config(args.config)
    ROUTER = Router(backends, default)

    server = ThreadingHTTPServer((host, port), ProxyHandler)
    server.daemon_threads = True
    routes = ", ".join(f"{p}*->{b.name}" for p, b in ROUTER.prefixes)
    sys.stderr.write(f"claude-router listening on {host}:{port} ({routes}; default={default.name})\n")
    server.serve_forever()


if __name__ == "__main__":
    main()
