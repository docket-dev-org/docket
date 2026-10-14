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

The boundary is also where the run's *failure* behaviour lives, because that is the only
place that sees the truth about both sides:

* an unusable upstream trips a circuit breaker (:mod:`docket.resilience`) instead of
  being hammered request after request;
* a runaway agent is throttled by a token bucket rather than by the invoice;
* tests and game days can make the boundary itself misbehave with ``--fault-inject``,
  and every injection is written to the ledger so a receipt from a chaos run can never
  be mistaken for a receipt from a normal one;
* ``/docket/healthz``, ``/docket/readyz`` and ``/docket/metrics`` expose liveness,
  readiness (can this boundary still record evidence?) and counters.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from . import events as ev
from .canon import digest_of, num, usd
from .ledger import Ledger, LedgerFullError
from .policy import Policy
from .pricing import CostMeter, PriceTable, estimate_tokens_from_text
from .resilience import (
    CircuitBreaker,
    FaultInjector,
    HealthState,
    RateLimiter,
    readiness,
)

#: Free-space floor for the volume holding the ledger. A run that fills a disk turns a
#: budget problem into an outage, so readiness reports it before it happens.
MIN_FREE_BYTES = 16 * 1024 * 1024

#: Statuses that indicate the upstream is unhappy rather than the request being wrong.
UPSTREAM_FAILURE_STATUSES = frozenset({500, 502, 503, 504})

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

#: Largest request body the boundary will accept. A model request is text plus base64
#: images; 32 MiB is far beyond any real prompt and far below the point where one agent
#: can exhaust the machine. Without a ceiling, `Content-Length: 99999999999` is a
#: one-line denial of service against the process that is supposed to be the guardrail.
MAX_REQUEST_BODY = 32 * 1024 * 1024

#: Deepest JSON nesting the boundary will parse out of a request body. Python's parser
#: raises RecursionError somewhere around a thousand levels, and recursion also happens
#: later — in canonicalisation and in the request fingerprint — so the limit is enforced
#: on the raw bytes, before any consumer can be surprised. Real model requests nest a
#: handful of levels; 64 is generous.
MAX_BODY_DEPTH = 64

#: Largest per-response token count Docket will accept from a provider as *reported* usage.
#: Usage is a measurement of what the provider says it did; one API response cannot plausibly
#: contain a quadrillion tokens, and a count that large is either a broken upstream or a
#: provider testing what the recorder does with absurd numbers. Past the ceiling the fields
#: are treated as unreadable and the event says the tokens were estimated, so no invented
#: measurement is ever filed as reported.
MAX_REPORTED_TOKENS_PER_CALL = 10**15


def _json_depth_exceeded(blob: bytes) -> bool:
    """True when the JSON nesting in ``blob`` is deeper than :data:`MAX_BODY_DEPTH`.

    A scanner rather than a parser on purpose: it must be safe to run on input that would
    break a parser, including unterminated strings and trailing junk.
    """
    import json as _json

    depth = 0
    in_string = False
    escaped = False
    for byte in blob:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:  # backslash
                escaped = True
            elif byte == 0x22:  # quote
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in (0x7B, 0x5B):  # { [
            depth += 1
            if depth > MAX_BODY_DEPTH:
                return True
        elif byte in (0x7D, 0x5D):  # } ]
            depth = max(0, depth - 1)
    del _json
    return False
DENIAL_TYPE = "docket_policy_denied"


@dataclass
class ProxyStats:
    model_calls: int = 0
    model_errors: int = 0
    denied: int = 0
    tunnels_allowed: int = 0
    tunnels_denied: int = 0
    bytes_from_upstream: int = 0
    max_cost_seen: float = 0.0
    denials: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_calls": self.model_calls,
            "model_errors": self.model_errors,
            "denied": self.denied,
            "tunnels_allowed": self.tunnels_allowed,
            "tunnels_denied": self.tunnels_denied,
            "bytes_from_upstream": self.bytes_from_upstream,
        }


def _token_field(usage: dict[str, Any], key: str, problems: list[str]) -> int:
    """Read one token count from a provider's ``usage`` block; always a non-negative int.

    A provider is not a trusted party here: it is a remote service whose numbers are being
    recorded precisely so that they can be disputed later, and the recorder must not be
    takeable down by the thing it records. ``int("abc")`` used to raise ``ValueError``
    straight out of the request handler, and a forty-digit count used to raise
    ``InvalidOperation`` inside the meter — either one turns a hostile or merely broken
    upstream into an internal error in the run's own evidence.

    A field that is not a whole number, is negative, or exceeds the plausibility ceiling
    (``MAX_REPORTED_TOKENS_PER_CALL``) is recorded in ``problems`` and read as 0; the caller
    then treats the whole usage block as unusable and *says so on the event*, so the tokens
    are marked ``estimated`` instead of an invented measurement being passed off as reported.
    """
    value = usage.get(key)
    if value is None:
        return 0
    if isinstance(value, bool):
        problems.append(f"{key}={value!r} is not a token count")
        return 0
    if isinstance(value, float) and not value.is_integer():
        problems.append(f"{key}={value!r} is not a whole number of tokens")
        return 0
    try:
        count = int(str(value).strip())
    except (TypeError, ValueError):
        problems.append(f"{key}={value!r} is not a whole number of tokens")
        return 0
    if count < 0:
        problems.append(f"{key}={count} is negative")
        return 0
    if count > MAX_REPORTED_TOKENS_PER_CALL:
        problems.append(f"{key}={count} exceeds the plausible maximum {MAX_REPORTED_TOKENS_PER_CALL}")
        return 0
    return count


def _extract_usage(
    provider: str,
    payload: dict[str, Any],
    problems: list[str] | None = None,
) -> tuple[int, int, int] | None:
    """Pull (input, cached_input, output) tokens out of a provider response.

    Total by construction: it returns a triple, or ``None`` when the response carries no
    usable usage block, and never raises. ``problems`` (when supplied) collects a
    human-readable reason for every unreadable field, which the caller discloses on the
    event — an unusable number is reported, not silently dropped.
    """
    notes = problems if problems is not None else []
    if not isinstance(payload, dict):
        return None
    usage = payload.get("usage") or payload.get("usageMetadata")
    if not isinstance(usage, dict):
        return None
    if (provider == "anthropic" or {"input_tokens", "output_tokens"} & set(usage)) and (
        "input_tokens" in usage or "output_tokens" in usage
    ):
        triple = (
            _token_field(usage, "input_tokens", notes),
            _token_field(usage, "cache_read_input_tokens", notes),
            _token_field(usage, "output_tokens", notes),
        )
    elif {"prompt_tokens", "completion_tokens"} & set(usage):
        details = usage.get("prompt_tokens_details") or {}
        cached = _token_field(details, "cached_tokens", notes) if isinstance(details, dict) else 0
        if not isinstance(details, dict) and details:
            notes.append(f"prompt_tokens_details={details!r} is not an object")
        triple = (
            _token_field(usage, "prompt_tokens", notes),
            cached,
            _token_field(usage, "completion_tokens", notes),
        )
    elif {"promptTokenCount", "candidatesTokenCount"} & set(usage):
        triple = (
            _token_field(usage, "promptTokenCount", notes),
            _token_field(usage, "cachedContentTokenCount", notes),
            _token_field(usage, "candidatesTokenCount", notes),
        )
    else:
        return None
    if notes:
        # All or nothing: one unreadable field makes the block a non-measurement, and a
        # measurement that is half invented is worse than an estimate that is labelled.
        return None
    return triple


def _parse_sse_usage(
    provider: str,
    text: str,
    problems: list[str] | None = None,
) -> tuple[int, int, int] | None:
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
        usage = _extract_usage(provider, payload, problems)
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


def _unsafe_path(path: str) -> str | None:
    """Reason a request path must be refused, or ``None`` when it is acceptable.

    The raw path *and* its percent-decoded form are checked: a request line carrying
    ``%00`` or ``%0d%0a`` is not itself dangerous to forward, but any component between
    Docket and the provider that decodes it before parsing would turn it into a null byte
    or a header break. Refusing both spellings costs nothing and removes the ambiguity.
    """
    if any(char in path for char in "\r\n\x00"):
        return "the path contains control characters"
    decoded = path
    with contextlib.suppress(Exception):
        import urllib.parse

        decoded = urllib.parse.unquote(path)
    if decoded != path and any(char in decoded for char in "\r\n\x00"):
        return "the path contains percent-encoded control characters"
    for candidate in (path, decoded):
        segments = candidate.replace("\\", "/").split("/")
        if ".." in segments:
            return "the path contains a '..' segment"
    return None


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "docket"
    sys_version = ""

    proxy: RecordingProxy  # set by the server factory

    # -- logging ---------------------------------------------------------- #
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D102 - silence stderr
        if self.proxy.verbose:
            super().log_message(fmt, *args)

    # -- helpers ---------------------------------------------------------- #
    def _refuse_and_close(
        self, status: int, payload: dict[str, Any], *, reason: str, **detail: Any
    ) -> None:
        """Answer a request whose body was *not* consumed, then hang up.

        This is not cosmetic. A server reuses a keep-alive connection by parsing whatever
        is left in the socket as the next request; if a body was refused but left unread,
        the body's own bytes are parsed as a request line, which produces confusing
        errors — and, in the general case, is the request-smuggling primitive. Refusing
        and closing is the honest answer: this connection cannot continue.

        The ledger is written *before* the client is answered. The order is the promise:
        a refusal that has been sent has already been recorded, so an observer can never
        see a refusal that the record does not explain.
        """
        self.close_connection = True
        self.proxy.record(
            "net.denied",
            {
                "reason": reason,
                "status": status,
                "connection": "closed (the refused body was not consumed)",
                "path": self.path,
                **detail,
            },
        )
        self._send_json(status, payload)

    def _read_body(self) -> bytes | None:
        """Read the request body, or refuse with a status the client can act on.

        Returns ``None`` when the request has already been answered with an error. Three
        cases matter, and all three are answered rather than guessed at:

        * a declared length beyond :data:`MAX_REQUEST_BODY` — refused with 413 *before*
          reading, so a hostile length cannot allocate anything;
        * a chunked request — refused with 411, because Docket cannot see the body it
          would be forwarding. Silently forwarding an empty body would corrupt the
          record: the provider would be billed for a request the agent did not make;
        * an unparsable ``Content-Length`` — refused with 400.
        """
        transfer_encoding = (self.headers.get("Transfer-Encoding") or "").lower()
        if transfer_encoding.strip():
            self._refuse_and_close(
                411,
                {
                    "error": {
                        "type": "docket_unsupported_encoding",
                        "message": (
                            f"Transfer-Encoding: {transfer_encoding!r} is not supported by the boundary. "
                            "Send a Content-Length instead: Docket records the exact bytes it forwarded, "
                            "and it will not forward a body it cannot measure."
                        ),
                        "code": "chunked_unsupported",
                    }
                },
                reason="chunked request body refused: the boundary cannot measure what it would forward",
                transfer_encoding=transfer_encoding,
            )
            return None
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return b""
        try:
            length = int(raw_length)
        except ValueError:
            self._refuse_and_close(
                400,
                {
                    "error": {
                        "type": "docket_bad_request",
                        "message": f"Content-Length is not a number: {raw_length!r}",
                        "code": "bad_content_length",
                    }
                },
                reason="Content-Length is not a number",
                content_length=raw_length,
            )
            return None
        if length < 0:
            self._refuse_and_close(
                400,
                {
                    "error": {
                        "type": "docket_bad_request",
                        "message": f"Content-Length must not be negative: {length}",
                        "code": "bad_content_length",
                    }
                },
                reason="Content-Length is negative",
                content_length=length,
            )
            return None
        if length == 0:
            return b""
        if length > MAX_REQUEST_BODY:
            self.proxy.stats.denied += 1
            self.proxy.health.counter("denied")
            self._refuse_and_close(
                413,
                {
                    "error": {
                        "type": "docket_request_too_large",
                        "message": (
                            f"request body of {length} bytes exceeds the boundary's limit of "
                            f"{MAX_REQUEST_BODY} bytes; split the request or raise the limit deliberately"
                        ),
                        "code": "body_too_large",
                        "limit_bytes": MAX_REQUEST_BODY,
                    }
                },
                reason="request body over the boundary's limit",
                declared_bytes=length,
                limit_bytes=MAX_REQUEST_BODY,
            )
            return None
        # Read in bounded chunks: a short read from a client that dies mid-body must not
        # silently produce a shorter body than the one that will be recorded.
        chunks: list[bytes] = []
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)
        if len(body) != length:
            self._send_json(
                400,
                {
                    "error": {
                        "type": "docket_bad_request",
                        "message": (
                            f"request body ended after {len(body)} of {length} declared bytes; "
                            "the connection was closed mid-request"
                        ),
                        "code": "truncated_request",
                    }
                },
            )
            return None
        return body

    def _relay_header(self, name: str, value: str) -> None:
        """Relay an upstream header, minus anything that could forge a response.

        Header values from a provider are untrusted input as far as the boundary is
        concerned: a value containing CRLF would let a compromised provider inject its
        own status line or extra headers into the agent's reply. `http.server` writes
        whatever it is given, so the check has to happen here.
        """
        clean_name = name.strip()
        clean_value = value.strip()
        if not clean_name or any(char in clean_name for char in "\r\n: \t"):
            self.proxy.record(
                "note", {"message": f"dropped an invalid response header name: {clean_name[:40]!r}"}
            )
            return
        if any(char in clean_value for char in "\r\n\x00"):
            self.proxy.record(
                "note",
                {
                    "message": "dropped a response header whose value contained a line break",
                    "header": clean_name,
                    "bytes": len(clean_value),
                },
            )
            return
        self.send_header(clean_name, clean_value)

    def _send_json(self, status: int, payload: dict[str, Any], extra_headers: dict[str, str] | None = None) -> None:
        blob = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for name, value in (extra_headers or {}).items():
            self._relay_header(name, value)
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
        self.proxy.record(
            ev.MODEL_DENIED,
            {"provider": provider, "model": model, "reason": reason, "cap": cap, "status": 403},
        )
        self._send_json(403, body)

    def _throttle(
        self,
        reason: str,
        *,
        status: int,
        code: str,
        model: str,
        provider: str,
        retry_after: float | None = None,
    ) -> None:
        """Refuse without contacting the upstream, saying exactly why.

        ``429`` and ``503`` here are honest: the request genuinely was not forwarded, and
        the refusal is recorded as a ``model.denied`` event so the receipt accounts for a
        call the agent believes it made.
        """
        self.proxy.stats.denied += 1
        detail = {"provider": provider, "model": model, "reason": reason, "code": code}
        self.proxy.stats.denials.append(detail)
        self.proxy.record(
            ev.MODEL_DENIED,
            {"provider": provider, "model": model, "reason": reason, "cap": None, "code": code, "status": status},
        )
        headers = {}
        if retry_after is not None:
            headers["Retry-After"] = str(max(1, int(round(retry_after))))
        self.proxy.stats.model_errors += 1 if status >= 500 else 0
        self._send_json(
            status,
            {
                "error": {
                    "type": DENIAL_TYPE,
                    "message": f"docket did not send this request: {reason}",
                    "code": code,
                    "docket_session": self.proxy.session_id,
                }
            },
            extra_headers=headers,
        )

    def _respond_error(self, status: int, payload: dict[str, Any]) -> None:
        """Relay an upstream failure faithfully, including its body."""
        blob = payload.get("bytes", b"")
        if not isinstance(blob, (bytes, bytearray)) or not blob:
            blob = json.dumps(
                {"error": {"type": "docket_upstream_error", "message": payload.get("error") or "no response body"}}
            ).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        with contextlib.suppress(OSError):  # pragma: no cover - client hung up
            self.wfile.write(blob)

    # -- HTTP methods ------------------------------------------------------ #
    def _guarded(self, with_body: bool) -> None:
        """Run a request, and if Docket itself fails, say so on the wire *and* on the record.

        An exception inside the boundary used to close the socket with nothing written and
        nothing logged: the agent sees a transport error, the operator sees nothing, and
        the failure leaves no trace in the artefact that is supposed to be a trace. Now an
        internal error is recorded as a ledger event and returned as a 502 that names the
        bug, because a silent boundary is the one failure this tool cannot tolerate in
        itself.
        """
        try:
            self._handle(with_body=with_body)
        except Exception as exc:  # noqa: BLE001 - the point is to catch everything
            import traceback

            trace = traceback.format_exc(limit=6)
            with contextlib.suppress(Exception):
                self.proxy.record(
                    ev.MODEL_ERROR,
                    {
                        "provider": None,
                        "model": None,
                        "status": 502,
                        "error": f"docket internal error: {type(exc).__name__}: {exc}",
                        "traceback": trace.splitlines()[-8:],
                        "path": self.path,
                        "note": "this is a bug in docket, not in the agent; please report it with "
                        "`docket doctor --bundle`",
                    },
                )
            with contextlib.suppress(Exception):
                self.proxy.health.counter("errors")
            with contextlib.suppress(Exception):
                self._send_json(
                    502,
                    {
                        "error": {
                            "type": "docket_internal_error",
                            "message": f"{type(exc).__name__}: {exc}",
                            "code": "internal_error",
                            "note": "recorded in the ledger; report with `docket doctor --bundle`",
                        }
                    },
                )

    def do_POST(self) -> None:  # noqa: N802
        self._guarded(with_body=True)

    def do_GET(self) -> None:  # noqa: N802
        self._guarded(with_body=False)

    def do_PATCH(self) -> None:  # noqa: N802
        self._guarded(with_body=True)

    def do_PUT(self) -> None:  # noqa: N802
        self._guarded(with_body=True)

    def do_DELETE(self) -> None:  # noqa: N802
        self._guarded(with_body=False)

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
        body_bytes: bytes | None = None
        if with_body:
            body_bytes = self._read_body()
            if body_bytes is None:
                return  # already answered with a status the client can act on
        body_bytes = body_bytes or b""
        if self.path.startswith("/docket/"):
            route = self.path[len("/docket/") :].split("?", 1)[0].strip("/")
            if route in {"healthz", "readyz", "metrics"}:
                self._handle_ops(route)
                return
            self._handle_instrumentation(body_bytes)
            return
        unsafe = _unsafe_path(self.path)
        if unsafe is not None:
            # A request line is attacker-controlled input. `..` segments would let a
            # prompt-injected agent aim the boundary at endpoints the operator never
            # intended, and control characters would be headers in a body's clothing.
            self.proxy.stats.denied += 1
            self.proxy.record(
                "net.denied", {"reason": unsafe, "path": self.path[:200], "code": "unsafe_path"}
            )
            self._send_json(
                400,
                {
                    "error": {
                        "type": "docket_bad_request",
                        "message": f"refusing this request path: {unsafe}",
                        "code": "unsafe_path",
                    }
                },
            )
            return
        provider, upstream_path = proxy.resolve(self.path)
        if provider is None:
            self._send_json(404, {"error": {"type": "docket_unknown_route", "message": self.path}})
            return
        if body_bytes and _json_depth_exceeded(body_bytes):
            self.proxy.stats.denied += 1
            self.proxy.record(
                "net.denied",
                {
                    "reason": "request body nested deeper than the boundary's limit",
                    "limit_depth": MAX_BODY_DEPTH,
                    "bytes": len(body_bytes),
                    "path": self.path,
                },
            )
            self._refuse_and_close(
                400,
                {
                    "error": {
                        "type": "docket_bad_request",
                        "message": (
                            f"request body nests more than {MAX_BODY_DEPTH} levels deep; a model request "
                            "never legitimately does, and parsing one is how a guardrail gets crashed"
                        ),
                        "code": "body_too_deep",
                        "limit_depth": MAX_BODY_DEPTH,
                    }
                },
                reason="request body nested deeper than the boundary's limit",
                limit_depth=MAX_BODY_DEPTH,
                bytes=len(body_bytes),
            )
            return
        try:
            parsed_body: dict[str, Any] | None = json.loads(body_bytes) if body_bytes else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            parsed_body = None
        except RecursionError:
            # A deeply nested body is a way to make a parser die rather than a request to
            # forward. It is answered, not crashed into: the record keeps the reason.
            self.proxy.record(
                "net.denied", {"reason": "request body nesting exceeded the parser's depth", "path": self.path}
            )
            self._send_json(
                400,
                {
                    "error": {
                        "type": "docket_bad_request",
                        "message": "request body is nested too deeply to parse",
                        "code": "body_too_deep",
                    }
                },
            )
            return

        model = _model_from_request(provider, self.path, parsed_body)
        streaming = bool(isinstance(parsed_body, dict) and parsed_body.get("stream"))
        proxy.health.counter("requests")
        proxy.health.counter("in_flight")

        try:
            # -- admission control, cheapest and most terminal checks first ---- #
            if proxy.ledger_full.is_set():
                self._throttle(
                    "the ledger for this run has reached its configured limit, so no further "
                    "model calls will be recorded or forwarded",
                    status=503,
                    code="max_events",
                    model=model,
                    provider=provider,
                )
                return
            limit_ok, retry_after = proxy.limiter.acquire()
            if not limit_ok:
                proxy.health.counter("rate_limited")
                proxy.record(
                    "x-docket.rate-limited",
                    {
                        "provider": provider,
                        "model": model,
                        "retry_after_seconds": num(retry_after),
                        "limiter": proxy.limiter.snapshot(),
                    },
                )
                self._throttle(
                    f"rate limit reached for this run "
                    f"({proxy.limiter.snapshot()['per_minute'] or '—'}/minute, "
                    f"{proxy.limiter.snapshot()['per_second'] or '—'}/second); this request was not sent upstream",
                    status=429,
                    code="rate_limited",
                    model=model,
                    provider=provider,
                    retry_after=retry_after,
                )
                return

            allowed, reason = proxy.policy.models.decide(model)
            if not allowed:
                self._deny(reason, model, provider)
                return
            cap = proxy.cap_breach()
            if cap:
                self._deny("a spending cap was already crossed earlier in this run", model, provider, cap)
                return

            # -- is the upstream worth talking to? ----------------------------- #
            if proxy.breaker is not None:
                breaker_ok, breaker_reason = proxy.breaker.allow(provider)
                if not breaker_ok:
                    proxy.health.counter("breaker_refusals")
                    self._throttle(
                        f"not sent upstream: {breaker_reason}. The boundary keeps probing; "
                        "the state is recorded in the ledger and the receipt.",
                        status=503,
                        code="circuit_open",
                        model=model,
                        provider=provider,
                    )
                    return

            self._forward_and_record(provider, upstream_path, body_bytes, parsed_body, model, streaming, started)
        finally:
            proxy.health.counter("in_flight", -1)

    def _forward_and_record(
        self,
        provider: str,
        upstream_path: str,
        body_bytes: bytes,
        parsed_body: dict[str, Any] | None,
        model: str,
        streaming: bool,
        started: float,
    ) -> None:
        """The forwarding half of :meth:`_handle`, so admission control can return early."""
        proxy = self.proxy

        # -- chaos engineering: fault injection, when explicitly configured ---- #
        if proxy.injector is not None and proxy.injector.enabled:
            delay, delays = proxy.injector.next_delay()
            for fault in delays:
                proxy.record("x-docket.fault", {"kind": fault.kind, "detail": fault.describe()})
            if delay:
                time.sleep(delay)
            dropped = proxy.injector.before_forward()
            if dropped is not None:
                fault_status = 502 if dropped.kind == "reset" else 503
                fault_code = "injected_reset" if dropped.kind == "reset" else "injected_drop"
                proxy.record(
                    "x-docket.fault",
                    {"kind": dropped.kind, "detail": dropped.describe(), "effect": "upstream not contacted"},
                )
                # The call failed, so it is recorded as a failed call as well as an
                # injected fault: one place to count failures (`model.error`), one place
                # to explain them (`x-docket.fault`), and no category where a failure is
                # invisible. The agent never reached a provider, so there is no response
                # and no usage — recording either would be inventing data.
                proxy.record(
                    ev.MODEL_ERROR,
                    {
                        "provider": provider,
                        "model": model,
                        "status": fault_status,
                        "error": f"upstream not contacted (injected fault: {dropped.describe()})",
                        "code": fault_code,
                        "injected_fault": True,
                    },
                )
                proxy.stats.model_errors += 1
                if dropped.kind == "reset":
                    self._send_json(
                        502,
                        {
                            "error": {
                                "type": "docket_upstream_error",
                                "message": f"connection reset before a response (injected fault: {dropped.describe()})",
                                "code": "injected_reset",
                            }
                        },
                    )
                else:
                    self._send_json(
                        503,
                        {
                            "error": {
                                "type": "docket_upstream_error",
                                "message": f"request dropped before the upstream (injected fault: {dropped.describe()})",
                                "code": "injected_drop",
                            }
                        },
                    )
                if proxy.breaker is not None:
                    proxy.breaker.record_failure(provider)
                proxy.health.counter("errors")
                return

        fingerprint = ev.request_fingerprint({"body": parsed_body}) if parsed_body else digest_of(self.path)[:16]
        request_data: dict[str, Any] = {
            "provider": provider,
            "model": model,
            "method": self.command,
            "path": upstream_path,
            "streaming": streaming,
            "fingerprint": fingerprint,
            "request_sha256": digest_of(
                {
                    "body": parsed_body if parsed_body is not None else body_bytes.decode("utf-8", "replace"),
                    "path": self.path,
                }
            ),
            "body_bytes": len(body_bytes),
        }
        if proxy.record_bodies and parsed_body is not None:
            request_data["body"] = parsed_body
        proxy.record(ev.MODEL_REQUEST, request_data)

        # -- forward ------------------------------------------------------ #
        status, headers, payload = self._forward(provider, upstream_path, body_bytes, streaming)
        latency_ms = int((time.time() - started) * 1000)

        # -- chaos engineering: rewrite the response, when configured ------ #
        injected: str | None = None
        if proxy.injector is not None and proxy.injector.enabled:
            fault = proxy.injector.after_response(status or 0)
            if fault is not None:
                injected = fault.describe()
                proxy.record(
                    "x-docket.fault",
                    {"kind": fault.kind, "detail": fault.describe(), "effect": f"applied to the {status!r} response"},
                )
                if fault.kind == "slow":
                    time.sleep(fault.seconds)
                    status = None
                elif fault.kind == "status":
                    status = fault.status
                    payload = {
                        "bytes": json.dumps(
                            {
                                "error": {
                                    "type": "injected_fault",
                                    "message": f"forced status {fault.status} (injected fault: {fault.describe()})",
                                }
                            }
                        ).encode()
                    }
                elif fault.kind == "truncate":
                    blob = payload.get("bytes", b"")
                    payload = {"bytes": blob[: max(1, int(len(blob) * fault.fraction))]}
                elif fault.kind == "corrupt":
                    blob = bytearray(payload.get("bytes", b""))
                    for index in range(0, len(blob), max(1, len(blob) // 8 or 1)):
                        blob[index] ^= 0x20
                    payload = {"bytes": bytes(blob)}

        # -- a failed call is an error, not a model response ----------------- #
        # Any non-2xx is a failed call. A 401 (bad key) or a provider 429 is not a model
        # response with zero tokens: filing it as one would understate the cost of a bad
        # run and misdescribe, to a reader, what the agent actually received.
        if status is None or not (200 <= status < 300):
            error_message = payload.get("error")
            reported_usage: dict[str, int] | None = None
            if not error_message:
                with contextlib.suppress(json.JSONDecodeError, UnicodeDecodeError, AttributeError):
                    parsed = json.loads(payload.get("bytes", b"").decode("utf-8", "replace"))
                    if isinstance(parsed, dict):
                        detail = parsed.get("error")
                        if isinstance(detail, dict):
                            error_message = detail.get("message") or detail.get("type")
                        elif isinstance(detail, str):
                            error_message = detail
                        # Some providers report usage even on an error. It is disclosed on
                        # the event but deliberately excluded from totals and cap
                        # accounting: a usage row is only provable against a successful
                        # response (spec §6), so counting it in totals would create a
                        # number the verifier cannot reproduce.
                        error_usage_problems: list[str] = []
                        found = _extract_usage(provider, parsed, error_usage_problems)
                        if found is not None:
                            reported_usage = {
                                "input_tokens": found[0],
                                "cached_input_tokens": found[1],
                                "output_tokens": found[2],
                            }
            error_data: dict[str, Any] = {
                "provider": provider,
                "model": model,
                "status": status,
                "latency_ms": latency_ms,
                "error": str(error_message)[:600] if error_message else "upstream returned no response",
                "injected_fault": injected,
                "circuit_state": proxy.breaker.state_of(provider) if proxy.breaker else None,
            }
            if reported_usage is not None:
                error_data["reported_usage"] = reported_usage
                error_data["usage_note"] = (
                    "the provider reported usage inside an error response; disclosed here, and "
                    "excluded from totals because verification can only recompute usage bound "
                    "to a successful response"
                )
            proxy.stats.model_errors += 1
            proxy.health.counter("errors")
            if proxy.breaker is not None:
                proxy.breaker.record_failure(provider)
            proxy.record(ev.MODEL_ERROR, error_data)
            self._respond_error(status or 502, payload)
            return

        if proxy.breaker is not None:
            proxy.breaker.record_success(provider)

        usage = None
        usage_source = "reported"
        usage_problems: list[str] = []
        if streaming:
            text = payload.get("text", "")
            usage = _parse_sse_usage(provider, text, usage_problems)
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
                usage = _extract_usage(provider, parsed_response, usage_problems)
                if usage is None:
                    usage_source = "estimated"
                    usage = (
                        estimate_tokens_from_text(body_bytes.decode("utf-8", "replace")),
                        0,
                        estimate_tokens_from_text(
                            json.dumps(parsed_response.get("content") or parsed_response.get("choices") or "")
                        ),
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

        response_data: dict[str, Any] = {
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
            "injected_fault": injected,
        }
        if usage_problems:
            # The provider's usage block was present but unreadable, and the tokens above are
            # therefore an estimate. Say which numbers were refused, on the event itself: a
            # silently substituted estimate would be indistinguishable from a measurement.
            response_data["usage_note"] = (
                "the provider's usage fields were unreadable ("
                + "; ".join(usage_problems[:4])
                + "); the token counts recorded here are estimated from the request and response text, not reported"
            )
        proxy.record(ev.MODEL_RESPONSE, response_data)

        proxy._check_warning()
        breach = proxy.cap_breach()
        if breach and not proxy.breached.is_set():
            proxy.record(
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
            with contextlib.suppress(OSError):  # pragma: no cover - client hung up
                self.wfile.write(blob)

    def _handle_ops(self, route: str) -> bool:
        """``/docket/healthz``, ``/docket/readyz``, ``/docket/metrics``. Liveness,
        readiness and counters for whatever is supervising this run."""
        proxy = self.proxy
        if route == "healthz":
            self._send_json(
                200,
                {
                    "status": "ok",
                    "session": proxy.session_id,
                    "uptime_seconds": round(proxy.health.uptime_seconds(), 3),
                    "events": len(proxy.ledger),
                    "breached": proxy.breached.is_set(),
                },
            )
            return True
        if route == "readyz":
            report = proxy.readiness()
            self._send_json(200 if report["ready"] else 503, report)
            return True
        if route == "metrics":
            body = proxy.metrics_text().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return True
        return False

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
        data = dict(payload) if isinstance(payload, dict) else {"value": payload}
        if kind == "event":
            # Generic route: any language, any event in the vocabulary. The agent is
            # asserting the event, so it is labelled as such; the boundary keeps the
            # single writer role, which is what keeps the chain valid.
            event_type = str(data.pop("type", ""))
            if event_type not in ev.KNOWN_TYPES:
                self._send_json(
                    400,
                    {
                        "error": {
                            "type": "docket_unknown_event",
                            "message": f"unknown event type {event_type!r}; see docs/concepts.md",
                        }
                    },
                )
                return
            nested = data.pop("data", None)
            data = dict(nested) if isinstance(nested, dict) else data
        else:
            event_type = mapping.get(kind)
            if event_type is None:
                self._send_json(404, {"error": {"type": "docket_unknown_route", "message": self.path}})
                return
        data["reported_by"] = "agent"
        event = self.proxy.ledger.append(event_type, data)
        self._send_json(200, {"ok": True, "seq": event.seq, "session": self.proxy.session_id})

    def _copy_headers(self, headers: list[tuple[str, str]]) -> None:
        for key, value in headers:
            if key.lower() in HOP_BY_HOP:
                continue
            self._relay_header(key, value)

    def _send_stream_head(self, headers: list[tuple[str, str]]) -> None:
        self.send_response(200)
        for key, value in headers:
            if key.lower() in HOP_BY_HOP or key.lower() == "content-type":
                continue
            self._relay_header(key, value)
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
        # The upstream timeout is the boundary's own watchdog: a provider that accepts a
        # connection and then says nothing must not hold the agent (and the ledger) open
        # forever. It is configurable because slow models are legitimate; 300s is the
        # default ceiling, not a target.
        conn = conn_cls(parts.hostname, parts.port or (443 if secure else 80), timeout=self.proxy.upstream_timeout)
        target = (parts.path.rstrip("/") + path) if parts.path and parts.path != "/" else path
        headers = {
            k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP and k.lower() != "authorization"
        }
        if self.headers.get("Authorization"):
            headers["Authorization"] = self.headers["Authorization"]
        if self.headers.get("x-api-key"):
            headers["x-api-key"] = self.headers["x-api-key"]
        try:
            conn.request(self.command, target, body=body, headers=headers)
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            # HTTPException covers the ways a *malformed* response breaks the parser —
            # a bad status line, an oversized header line, a truncated body — which are
            # exactly what a hostile or broken provider returns. All of them are recorded
            # as a failed call and answered with 502, because an exception escaping here
            # would leave the agent with a closed socket and the ledger with no reason.
            detail = f"{type(exc).__name__}: {exc}"
            kind = "upstream_timeout" if isinstance(exc, TimeoutError) else "bad_upstream_response"
            self.proxy.stats.model_errors += 1
            self.proxy.ledger.append(
                ev.MODEL_ERROR, {"provider": provider, "path": path, "error": detail, "code": kind}
            )
            return (
                502,
                [],
                {
                    "bytes": json.dumps(
                        {
                            "error": {
                                "type": "docket_upstream_error",
                                "message": f"the provider did not deliver a usable response: {detail}",
                                "code": kind,
                            }
                        }
                    ).encode()
                },
            )

        # Reading the response is as untrusted as writing the request. A provider that
        # stops sending mid-body, or that sends a header block the parser rejects, is a
        # provider failure — not a Docket bug — so every read is wrapped and every failure
        # comes back as a named upstream error the agent and the ledger can both see.
        status = resp.status
        resp_headers = resp.getheaders()
        content_type = resp.getheader("Content-Type") or ""
        try:
            if streaming or "text/event-stream" in content_type:
                chunks: list[bytes] = []
                buffered = 0
                truncated = False
                try:
                    while True:
                        chunk = resp.read(4096)
                        if not chunk:
                            break
                        if buffered < MAX_RESPONSE_BUFFER:
                            chunks.append(chunk)
                            buffered += len(chunk)
                except (OSError, http.client.HTTPException):
                    # A stream that dies mid-flight is recorded as a partial stream: the
                    # bytes received so far are still evidence, and losing them would
                    # hide the fact that the provider cut the response short.
                    truncated = True
                return status, resp_headers, {
                    "text": b"".join(chunks).decode("utf-8", "replace"),
                    "truncated": truncated,
                }
            data = resp.read()
        except (OSError, http.client.HTTPException) as exc:
            # Named for what it is: a slow or malformed *provider*, answered in the
            # provider's own vocabulary rather than as an internal Docket error.
            kind = "upstream_timeout" if isinstance(exc, TimeoutError) else "bad_upstream_response"
            detail = f"{type(exc).__name__}: {exc}"
            self.proxy.stats.model_errors += 1
            self.proxy.ledger.append(
                ev.MODEL_ERROR,
                {"provider": provider, "path": path, "error": detail, "code": kind},
            )
            return (
                502,
                [],
                {
                    "bytes": json.dumps(
                        {
                            "error": {
                                "type": "docket_upstream_error",
                                "message": f"the provider did not deliver a usable response: {detail}",
                                "code": kind,
                            }
                        }
                    ).encode()
                },
            )
        finally:
            with contextlib.suppress(Exception):
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
        breaker: CircuitBreaker | None = None,
        limiter: RateLimiter | None = None,
        injector: FaultInjector | None = None,
        health: HealthState | None = None,
        min_free_bytes: int = MIN_FREE_BYTES,
        disk_free: Callable[[], int | None] | None = None,
        upstream_timeout: float = 300.0,
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
        self.breaker = breaker
        self.limiter = limiter or RateLimiter()
        self.injector = injector
        self.health = health or HealthState()
        self.min_free_bytes = min_free_bytes
        self.disk_free = disk_free or self._default_disk_free
        self.upstream_timeout = upstream_timeout
        self.ledger_full = threading.Event()
        self._warned = False
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- evidence ---------------------------------------------------------- #
    def record(self, event_type: str, data: dict[str, Any]) -> Any:
        """Append to the ledger, converting "the record is complete" into a refusal.

        ``LedgerFullError`` is a policy outcome rather than an environment failure, so it
        is not swallowed: the run is told, the boundary starts refusing further model
        calls, and the supervisor stops the run with the cap named. Any *other* failure
        (a full disk, say) is handled by the ledger's evidence guard, which either
        tolerates it and marks the receipt or stops the run, depending on configuration —
        never silently.
        """
        try:
            return self.ledger.append(event_type, data)
        except LedgerFullError as exc:
            if not self.ledger_full.is_set():
                self.ledger_full.set()
                self.health.counter("errors")
                with contextlib.suppress(LedgerFullError):  # pragma: no cover - reserves are bounded
                    # The cause is always recordable (see ledger.ALWAYS_RECORDABLE). If even
                    # that fails there is nothing left to say: the cap event is what could not
                    # be written, and the caller learns about it from `ledger_full`. A silence
                    # that is deliberate is still a silence, so it is written down here.
                    self.ledger.append(
                        ev.CAP_EXCEEDED,
                        {
                            "cap": "max_events" if exc.reason.startswith("ledger event") else "max_ledger_bytes",
                            "observed": exc.observed,
                            "limit": exc.limit,
                            "action": "refusing further model calls",
                        },
                    )
            return None

    def _default_disk_free(self) -> int | None:
        target = self.ledger.path.parent if self.ledger.path is not None else None
        if target is None:
            return None
        try:
            import shutil as _shutil

            return int(_shutil.disk_usage(target).free)
        except OSError:  # pragma: no cover - platform dependent
            return None

    def readiness(self) -> dict[str, Any]:
        """Readiness: can this boundary still record evidence, with room to spare?"""
        result = readiness(
            ledger_ok=not self.ledger.degraded,
            ledger_error=self.ledger.unwritten[-1]["error"] if self.ledger.unwritten else None,
            free_bytes=self.disk_free(),
            min_free_bytes=self.min_free_bytes,
        )
        result["breaker_open"] = bool(self.breaker and self.breaker.opened())
        result["ledger_degraded"] = self.ledger.degraded
        return result

    def metrics_text(self) -> str:
        """Prometheus text exposition. Counters only, plus a few gauges."""
        stats = self.stats
        lines: list[str] = []
        counters = {
            "docket_boundary_requests_total": self.health.requests,
            "docket_model_calls_total": stats.model_calls,
            "docket_model_denied_total": stats.denied,
            "docket_model_error_total": self.health.errors,
            "docket_rate_limited_total": self.limiter.refused,
            "docket_breaker_refusals_total": self.health.breaker_refusals,
            "docket_ledger_write_failures_total": len(self.ledger.unwritten),
            "docket_tokens_input_total": self.meter.input_tokens,
            "docket_tokens_cached_input_total": self.meter.cached_input_tokens,
            "docket_tokens_output_total": self.meter.output_tokens,
            "docket_cost_microusd_total": int(self.meter.cost * 1_000_000),
            "docket_events_total": len(self.ledger),
            "docket_bytes_from_upstream_total": stats.bytes_from_upstream,
        }
        if self.injector is not None and self.injector.enabled:
            counters["docket_faults_injected_total"] = len(self.injector.injected)
        for name, value in counters.items():
            lines.append(f"# TYPE {name} counter")
            lines.append(f"{name} {int(value)}")
        gauges = {
            "docket_in_flight": self.health.in_flight,
            "docket_breaker_open": 1 if (self.breaker and self.breaker.opened()) else 0,
            "docket_ledger_degraded": 1 if self.ledger.degraded else 0,
            "docket_uptime_seconds": round(self.health.uptime_seconds(), 3),
            "docket_cost_usd": str(usd(self.meter.cost)),
        }
        for name, value in gauges.items():
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name} {value}")
        breaker = self.breaker.snapshot() if self.breaker else {}
        for key, state in breaker.items():
            lines.append(f'docket_breaker_state{{upstream="{key}"}} {1 if state["state"] != "closed" else 0}')
        return "\n".join(lines) + "\n"

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
