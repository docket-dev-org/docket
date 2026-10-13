"""The boundary proxy: where enforcement happens and evidence is produced.

Docket's proxy is a **local, loopback-only HTTP boundary** that agent SDKs and CLIs
are pointed at (via base-URL environment variables, or via ``HTTP_PROXY`` in
``--http-proxy`` mode). Sitting in the request path is what makes the difference
between advice and enforcement:

* a call is refused **before** it is forwarded when it breaks the model policy or
  when a cap has already been crossed;
* usage reported by the provider is turned into estimated cost *as the run
  happens*, so a cap can be enforced mid-flight rather than discovered on the
  invoice;
* every exchange leaves a hash-chained event behind.

Auth headers pass through to the upstream and are never written to the ledger.
Bodies are not recorded unless ``--record-bodies`` is set; digests are always
recorded, so a receipt can prove *that* a specific request happened without
disclosing *what* it said.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlsplit

from . import events as ev
from .canon import digest_of, num, usd
from .ledger import Ledger
from .policy import Policy
from .pricing import CostMeter, PriceTable, estimate_tokens_from_text

#: Provider prefixes accepted by the proxy and their upstream base URLs.
DEFAULT_UPSTREAMS: dict[str, str] = {
    "openai": "https://api.openai.com",
    "anthropic": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com",
    "deepseek": "https://api.deepseek.com",
    "mistral": "https://api.mistral.ai",
    "openrouter": "https://openrouter.ai/api",
    "groq": "https://api.groq.com/openai",
    "together": "https://api.together.xyz",
    "fireworks": "https://api.fireworks.ai/inference",
    "xai": "https://api.x.ai",
    "ollama": "http://127.0.0.1:11434",
}

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "accept-encoding",
    "host",
}

MAX_RESPONSE_BUFFER = 4 * 1024 * 1024
DENIAL_TYPE = "docket_policy_denied"


@dataclass
class ProxyStats:
    model_calls: int = 0
    denied: int = 0
    tunnels_allowed: int = 0
    tunnels_denied: int = 0
    bytes_from_upstream: int = 0
    max_cost_seen: float = 0.0
    denials: list[dict[str, Any]] = field(default_factory=list)


def _extract_usage(provider: str, payload: dict[str, Any]) -> tuple[int, int, int] | None:
    """Pull (input, cached_input, output) tokens out of a provider response."""
    if not isinstance(payload, dict):
        return None
    usage = payload.get("usage") or payload.get("usageMetadata")
    if not isinstance(usage, dict):
        return None
    if provider == "anthropic" or {"input_tokens", "output_tokens"} & set(usage):
        if "input_tokens" in usage or "output_tokens" in usage:
            return (
                int(usage.get("input_tokens") or 0),
                int(usage.get("cache_read_input_tokens") or 0),
                int(usage.get("output_tokens") or 0),
            )
    if {"prompt_tokens", "completion_tokens"} & set(usage):
        cached = 0
        details = usage.get("prompt_tokens_details") or {}
        if isinstance(details, dict):
            cached = int(details.get("cached_tokens") or 0)
        return (
            int(usage.get("prompt_tokens") or 0),
            cached,
            int(usage.get("completion_tokens") or 0),
        )
    if {"promptTokenCount", "candidatesTokenCount"} & set(usage):
        return (
            int(usage.get("promptTokenCount") or 0),
            int(usage.get("cachedContentTokenCount") or 0),
            int(usage.get("candidatesTokenCount") or 0),
        )
    return None


def _parse_sse_usage(provider: str, text: str) -> tuple[int, int, int] | None:
    """Merge usage across SSE events (OpenAI sends it last; Anthropic spreads it)."""
    totals: dict[str, int] = {}
    found = False
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        body = line[5:].strip()
        if not body or body == "[DONE]":
            continue
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            continue
        usage = _extract_usage(provider, payload)
        if usage is None:
            delta = payload.get("usage")
            if isinstance(delta, dict) and provider == "anthropic":
                for key, target in (
                    ("input_tokens", "input"),
                    ("output_tokens", "output"),
                    ("cache_read_input_tokens", "cached"),
                ):
                    if isinstance(delta.get(key), int):
                        totals[target] = max(totals.get(target, 0), int(delta[key]))
                        found = True
                continue
            continue
        inp, cached, out = usage
        totals["input"] = max(totals.get("input", 0), inp)
        totals["cached"] = max(totals.get("cached", 0), cached)
        totals["output"] = max(totals.get("output", 0), out)
        found = True
    if not found:
        return None
    return totals.get("input", 0), totals.get("cached", 0), totals.get("output", 0)


def _model_from_request(provider: str, path: str, body: dict[str, Any] | None) -> str:
    if isinstance(body, dict) and isinstance(body.get("model"), str):
        return body["model"]
    # Gemini puts the model in the URL: /v1beta/models/<model>:generateContent
    marker = "/models/"
    if marker in path:
        tail = path.split(marker, 1)[1]
        return tail.split(":", 1)[0].split("/", 1)[0]
    return f"unknown-{provider}"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "docket"
    sys_version = ""

    proxy: "RecordingProxy"  # set by the server factory

    # -- logging ---------------------------------------------------------- #
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D102 - silence stderr
        if self.proxy.verbose:
            super().log_message(fmt, *args)

    # -- helpers ---------------------------------------------------------- #
    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        blob = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def _deny(self, reason: str, model: str, provider: str, cap: str | None = None) -> None:
        self.proxy.stats.denied += 1
        detail = {"provider": provider, "model": model, "reason": reason}
        if cap:
            detail["cap"] = cap
        self.proxy.stats.denials.append(detail)
        body = {
            "error": {
                "type": DENIAL_TYPE,
                "message": (
                    f"docket blocked this request: {reason}"
                    + (f" ({cap} cap)" if cap else "")
                    + ". See the run receipt for the full record."
                ),
                "code": cap or "model_policy",
                "docket_session": self.proxy.session_id,
            }
        }
        self.proxy.ledger.append(
            ev.MODEL_DENIED,
            {"provider": provider, "model": model, "reason": reason, "cap": cap},
        )
        self._send_json(403, body)

    # -- HTTP methods ------------------------------------------------------ #
    def do_POST(self) -> None:  # noqa: N802
        self._handle(with_body=True)

    def do_GET(self) -> None:  # noqa: N802
        self._handle(with_body=False)

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle(with_body=True)

    def do_PUT(self) -> None:  # noqa: N802
        self._handle(with_body=True)

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle(with_body=False)

    def do_CONNECT(self) -> None:  # noqa: N802
        """Tunnel (or refuse) arbitrary TLS traffic in ``--http-proxy`` mode."""
        target = self.path
        allowed, reason = self.proxy.decide_egress(target)
        if not allowed:
            self.proxy.stats.tunnels_denied += 1
            self.proxy.ledger.append(ev.NET_DENIED, {"host": target, "reason": reason})
            self._send_json(403, {"error": {"type": DENIAL_TYPE, "message": reason, "host": target}})
            return
        self.proxy.stats.tunnels_allowed += 1
        self.proxy.ledger.append(ev.NET_ALLOWED, {"host": target, "reason": reason, "kind": "tunnel"})
        host, _, port_text = target.partition(":")
        port = int(port_text or 443)
        try:
            upstream = socket.create_connection((host, port), timeout=10)
        except OSError as exc:
            self._send_json(502, {"error": {"type": "docket_upstream_error", "message": str(exc)}})
            return
        self.send_response(200, "Connection Established")
        self.end_headers()
        try:
            self._tunnel(self.connection, upstream)
        finally:
            upstream.close()

    def _tunnel(self, client: socket.socket, upstream: socket.socket) -> None:
        sockets = [client, upstream]
        while True:
            try:
                readable, _, _ = __import__("select").select(sockets, [], [], 1.0)
            except OSError:  # pragma: no cover - socket closed under us
                return
            if not readable:
                if self.proxy.stopping.is_set():
                    return
                continue
            for src in readable:
                dst = upstream if src is client else client
                try:
                    data = src.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                try:
                    dst.sendall(data)
                except OSError:
                    return

    # -- the main path ------------------------------------------------------ #
    def _handle(self, with_body: bool) -> None:
        proxy = self.proxy
        started = time.time()
        body_bytes = self._read_body() if with_body else b""
        if self.path.startswith("/docket/"):
            self._handle_instrumentation(body_bytes)
            return
        provider, upstream_path = proxy.resolve(self.path)
        if provider is None:
            self._send_json(404, {"error": {"type": "docket_unknown_route", "message": self.path}})
            return
        try:
            parsed_body: dict[str, Any] | None = json.loads(body_bytes) if body_bytes else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            parsed_body = None

        model = _model_from_request(provider, self.path, parsed_body)
        streaming = bool(isinstance(parsed_body, dict) and parsed_body.get("stream"))

        allowed, reason = proxy.policy.models.decide(model)
        if not allowed:
            self._deny(reason, model, provider)
            return
        cap = proxy.cap_breach()
        if cap:
            self._deny("a spending cap was already crossed earlier in this run", model, provider, cap)
            return

        fingerprint = ev.request_fingerprint({"body": parsed_body}) if parsed_body else digest_of(self.path)[:16]
        request_data: dict[str, Any] = {
            "provider": provider,
            "model": model,
            "method": self.command,
            "path": upstream_path,
            "streaming": streaming,
            "fingerprint": fingerprint,
            "request_sha256": digest_of({"body": parsed_body if parsed_body is not None else body_bytes.decode("utf-8", "replace"), "path": self.path}),
            "body_bytes": len(body_bytes),
        }
        if proxy.record_bodies and parsed_body is not None:
            request_data["body"] = parsed_body
        proxy.ledger.append(ev.MODEL_REQUEST, request_data)

        # -- forward ------------------------------------------------------ #
        status, headers, payload = self._forward(provider, upstream_path, body_bytes, streaming)
        latency_ms = int((time.time() - started) * 1000)
        status = status or 502

        usage = None
        usage_source = "reported"
        if streaming:
            text = payload.get("text", "")
            usage = _parse_sse_usage(provider, text)
            if usage is None:
                usage = (
                    estimate_tokens_from_text(body_bytes.decode("utf-8", "replace")),
                    0,
                    estimate_tokens_from_text(_sse_text(text)),
                )
                usage_source = "estimated"
        else:
            try:
                parsed_response = json.loads(payload.get("bytes", b"").decode("utf-8", "replace"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                parsed_response = None
            if isinstance(parsed_response, dict):
                usage = _extract_usage(provider, parsed_response)
                if usage is None:
                    usage_source = "estimated"
                    usage = (
                        estimate_tokens_from_text(body_bytes.decode("utf-8", "replace")),
                        0,
                        estimate_tokens_from_text(json.dumps(parsed_response.get("content") or parsed_response.get("choices") or "")),
                    )
                response_model = parsed_response.get("model")
                if isinstance(response_model, str) and response_model:
                    model = response_model
            else:
                usage_source = "estimated"
                usage = (
                    estimate_tokens_from_text(body_bytes.decode("utf-8", "replace")),
                    0,
                    0,
                )

        inp, cached, out = usage or (0, 0, 0)
        cost = proxy.meter.record(model, inp, cached, out, usage_source)
        proxy.stats.model_calls += 1

        proxy.ledger.append(
            ev.MODEL_RESPONSE,
            {
                "provider": provider,
                "model": model,
                "status": status,
                "latency_ms": latency_ms,
                "usage": {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out},
                "usage_source": usage_source,
                "cost_usd": usd(cost),
                "cumulative_cost_usd": usd(proxy.meter.cost),
                "streaming": streaming,
                "response_bytes": len(payload.get("text", "")) if streaming else len(payload.get("bytes", b"")),
            },
        )

        proxy._check_warning()
        breach = proxy.cap_breach()
        if breach:
            if not proxy.breached.is_set():
                proxy.ledger.append(
                    ev.CAP_EXCEEDED,
                    {
                        "cap": breach,
                        "cost_usd": usd(proxy.meter.cost),
                        "tokens": proxy.meter.total_tokens,
                        "calls": proxy.meter.calls,
                        "action": proxy.policy.caps.on_breach,
                    },
                )
                proxy.breached.set()
                if proxy.on_breach:
                    proxy.on_breach(breach)

        # -- respond ------------------------------------------------------ #
        if streaming:
            self._send_stream_head(headers)
            blob = payload.get("text", "").encode("utf-8")
            try:
                self.wfile.write(blob)
                self.wfile.flush()
            except OSError:  # pragma: no cover - client went away
                pass
        else:
            blob = payload.get("bytes", b"")
            self.send_response(status)
            self._copy_headers(headers)
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            try:
                self.wfile.write(blob)
            except OSError:  # pragma: no cover
                pass

    def _handle_instrumentation(self, body_bytes: bytes) -> None:
        """Language-agnostic instrumentation endpoint.

        Any agent, in any language, can report its own tool calls and notes by
        POSTing to the boundary:

            POST {DOCKET_PROXY_URL}/docket/tool/call   {"name": "sql", "args_digest": "…"}
            POST {DOCKET_PROXY_URL}/docket/tool/result {"name": "sql", "ok": true, "result_bytes": 41234}
            POST {DOCKET_PROXY_URL}/docket/note        {"message": "…"}

        Events land in the same hash-chained ledger as everything else, marked with
        their source, so a receipt never silently blends agent-reported facts with
        boundary-observed ones.
        """
        try:
            payload = json.loads(body_bytes) if body_bytes else {}
        except json.JSONDecodeError:
            self._send_json(400, {"error": {"type": "docket_bad_request", "message": "body must be JSON"}})
            return
        kind = self.path[len("/docket/") :].strip("/")
        mapping = {
            "tool/call": ev.TOOL_CALL,
            "tool/result": ev.TOOL_RESULT,
            "note": ev.NOTE,
        }
        event_type = mapping.get(kind)
        if event_type is None:
            self._send_json(404, {"error": {"type": "docket_unknown_route", "message": self.path}})
            return
        data = dict(payload) if isinstance(payload, dict) else {"value": payload}
        data["reported_by"] = "agent"
        event = self.proxy.ledger.append(event_type, data)
        self._send_json(200, {"ok": True, "seq": event.seq, "session": self.proxy.session_id})

    def _copy_headers(self, headers: list[tuple[str, str]]) -> None:
        for key, value in headers:
            if key.lower() in HOP_BY_HOP:
                continue
            self.send_header(key, value)

    def _send_stream_head(self, headers: list[tuple[str, str]]) -> None:
        self.send_response(200)
        for key, value in headers:
            if key.lower() in HOP_BY_HOP or key.lower() == "content-type":
                continue
            self.send_header(key, value)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

    def _forward(
        self, provider: str, path: str, body: bytes, streaming: bool
    ) -> tuple[int | None, list[tuple[str, str]], dict[str, Any]]:
        base = self.proxy.upstreams[provider]
        parts = urlsplit(base)
        secure = parts.scheme == "https"
        conn_cls = http.client.HTTPSConnection if secure else http.client.HTTPConnection
        conn = conn_cls(parts.hostname, parts.port or (443 if secure else 80), timeout=300)
        target = (parts.path.rstrip("/") + path) if parts.path and parts.path != "/" else path
        headers = {
            k: v
            for k, v in self.headers.items()
            if k.lower() not in HOP_BY_HOP and k.lower() != "authorization"
        }
        if self.headers.get("Authorization"):
            headers["Authorization"] = self.headers["Authorization"]
        if self.headers.get("x-api-key"):
            headers["x-api-key"] = self.headers["x-api-key"]
        try:
            conn.request(self.command, target, body=body, headers=headers)
            resp = conn.getresponse()
        except OSError as exc:
            self.proxy.ledger.append(
                ev.MODEL_ERROR, {"provider": provider, "path": path, "error": f"{type(exc).__name__}: {exc}"}
            )
            return None, [], {"bytes": json.dumps({"error": {"type": "docket_upstream_error", "message": str(exc)}}).encode()}

        status = resp.status
        resp_headers = resp.getheaders()
        content_type = resp.getheader("Content-Type") or ""
        if streaming or "text/event-stream" in content_type:
            chunks: list[bytes] = []
            buffered = 0
            try:
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    if buffered < MAX_RESPONSE_BUFFER:
                        chunks.append(chunk)
                        buffered += len(chunk)
            except OSError:  # pragma: no cover
                pass
            finally:
                conn.close()
            return status, resp_headers, {"text": b"".join(chunks).decode("utf-8", "replace")}

        data = resp.read()
        conn.close()
        self.proxy.stats.bytes_from_upstream += len(data)
        return status, resp_headers, {"bytes": data}


def _sse_text(text: str) -> str:
    """Concatenate the assistant-visible text in an SSE transcript (for estimation)."""
    out: list[str] = []
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        body = line[5:].strip()
        if not body or body == "[DONE]":
            continue
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            continue
        for key in ("delta", "message", "content_block"):
            node = payload.get(key)
            if isinstance(node, dict):
                if isinstance(node.get("text"), str):
                    out.append(node["text"])
                for block in node.get("content") or []:
                    if isinstance(block, dict) and isinstance(block.get("text"), str):
                        out.append(block["text"])
        for choice in payload.get("choices") or []:
            delta = choice.get("delta") or {}
            if isinstance(delta.get("content"), str):
                out.append(delta["content"])
    return "".join(out)


class RecordingProxy:
    """A loopback HTTP boundary that records model traffic and enforces policy."""

    def __init__(
        self,
        *,
        ledger: Ledger,
        policy: Policy,
        meter: CostMeter,
        session_id: str,
        upstreams: dict[str, str] | None = None,
        record_bodies: bool = False,
        on_breach: Callable[[str], None] | None = None,
        verbose: bool = False,
    ) -> None:
        self.ledger = ledger
        self.policy = policy
        self.meter = meter
        self.session_id = session_id
        self.upstreams = {**DEFAULT_UPSTREAMS, **(upstreams or {})}
        self.record_bodies = record_bodies
        self.on_breach = on_breach
        self.verbose = verbose
        self.stats = ProxyStats()
        self.breached = threading.Event()
        self.stopping = threading.Event()
        self._warned = False
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle --------------------------------------------------------- #
    def start(self) -> tuple[str, int]:
        handler = type("BoundHandler", (_Handler,), {"proxy": self})
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="docket-proxy", daemon=True)
        self._thread.start()
        host, port = self._server.server_address[0], self._server.server_address[1]
        return host, port

    def stop(self) -> None:
        self.stopping.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    # -- routing ----------------------------------------------------------- #
    def resolve(self, path: str) -> tuple[str | None, str]:
        """Map ``/provider/rest`` to ``(provider, upstream_path)``.

        An absolute-form request line (``http://host/path``, sent by clients using
        Docket as an HTTP proxy) is routed on its host instead.
        """
        if path.startswith("http://") or path.startswith("https://"):
            parts = urlsplit(path)
            host = parts.hostname or ""
            for provider, base in self.upstreams.items():
                if host and host.endswith(urlsplit(base).hostname or "\x00"):
                    return provider, parts.path + (f"?{parts.query}" if parts.query else "")
            return None, path
        cleaned = path.lstrip("/")
        if not cleaned:
            return None, path
        head, _, rest = cleaned.partition("/")
        if head in self.upstreams:
            return head, "/" + rest
        return None, path

    def decide_egress(self, target: str) -> tuple[bool, str]:
        host = target.split(":")[0]
        for provider, base in self.upstreams.items():
            base_host = urlsplit(base).hostname or ""
            if host and base_host and host.endswith(base_host):
                return True, f"model host for provider {provider!r}"
        return self.policy.egress.decide(target)

    # -- caps -------------------------------------------------------------- #
    def cap_breach(self) -> str | None:
        """Name of the first crossed cap, or ``None``. Called before and after forwarding."""
        caps = self.policy.caps
        if self.policy.budget_mode == "usd" and caps.max_usd is not None and self.meter.cost >= caps.max_usd:
            return "max_usd"
        if caps.max_tokens is not None and self.meter.total_tokens >= caps.max_tokens:
            return "max_tokens"
        if caps.max_model_calls is not None and self.meter.calls >= caps.max_model_calls:
            return "max_model_calls"
        return None

    def _check_warning(self) -> None:
        """Emit a single warning event when the soft threshold is crossed."""
        if self._warned:
            return
        caps = self.policy.caps
        if self.policy.budget_mode != "usd" or not caps.max_usd:
            return
        ratio = float(self.meter.cost / caps.max_usd)
        if ratio >= caps.warn_at:
            self._warned = True
            self.ledger.append(
                ev.CAP_WARNING,
                {
                    "cap": "max_usd",
                    "threshold": num(caps.warn_at),
                    "used_pct": num(round(ratio * 100, 1)),
                    "cost_usd": usd(self.meter.cost),
                    "limit_usd": usd(caps.max_usd),
                },
            )


def default_price_table() -> PriceTable:
    return PriceTable.load()
