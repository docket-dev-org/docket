"""A scripted, offline, provider-shaped upstream for demos and tests.

This exists so that Docket can be *demonstrated and tested without an API key*:
the same code path that talks to a real provider talks to this instead, which means
the receipts produced in CI and in ``docket demo`` are produced by the real
supervisor, the real proxy, the real ledger and the real verifier.

It is intentionally not a mock: it is an HTTP server that speaks the OpenAI and
Anthropic response shapes, including SSE streaming, and reports token usage the
way the real APIs do.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass
class Step:
    """One scripted upstream response."""

    model: str = "claude-sonnet-5"
    input_tokens: int = 1_000
    cached_input_tokens: int = 0
    output_tokens: int = 200
    status: int = 200
    text: str = "ok"
    delay_seconds: float = 0.0
    #: Return exactly this JSON body instead of a provider-shaped one. Used by the robustness
    #: tests (and by anyone demonstrating what Docket does with a broken provider) to script a
    #: hostile response: a usage block full of strings, a forty-digit token count, a body of
    #: the wrong type entirely. Non-streaming responses only.
    body_override: dict[str, Any] | None = None
    #: Return exactly these bytes, parsed by nobody. `body_override` is serialised with
    #: `json.dumps`, which cannot express a document with a repeated key or a non-finite number
    #: — the very documents worth scripting — so those cases are written here as wire bytes.
    raw_body: bytes | None = None


@dataclass
class FakeUpstream:
    """Cycles through ``steps`` and records every request it received."""

    steps: list[Step] = field(default_factory=lambda: [Step()])
    requests: list[dict[str, Any]] = field(default_factory=list)
    _server: ThreadingHTTPServer | None = None
    _thread: threading.Thread | None = None
    _index: int = 0

    # -- lifecycle --------------------------------------------------------- #
    def start(self, port: int = 0, host: str = "127.0.0.1") -> tuple[str, int]:
        """Serve on loopback and return ``(host, port)``.

        ``port=0`` (the default) asks the kernel for a free port, which is what a test wants. A
        *fixed* port is what a container wants: a scripted provider published at a stable address is
        how `deploy/compose.yaml` and the Kubernetes manifest give an agent something to call
        without an API key and without a network.

        ``host`` defaults to loopback — in a Kubernetes pod two containers share a network
        namespace, so loopback reaches both, and nothing else on the network can answer as a
        provider. Binding elsewhere is for container runtimes that give each container its own
        namespace (Docker Compose), where the server would otherwise be unreachable; it prints a
        warning, because a scripted provider on a routable address is a bad neighbour.
        """
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # noqa: D102
                return

            def do_GET(self) -> None:  # noqa: N802
                """Liveness for a container: `/healthz` (and `/`) answer without touching a script.

                A probe that POSTs a chat completion to check liveness is a probe that costs a call
                and can fail for reasons that have nothing to do with the process being alive. This
                answers the only question a probe should ask, and says how many responses it has
                served so an operator can see it working.
                """
                if self.path.split("?")[0] not in {"/healthz", "/"}:
                    payload = b'{"error":{"message":"not found","type":"invalid_request_error"}}'
                    self.send_response(404)
                else:
                    payload = json.dumps(
                        {"status": "ok", "served": upstream._index, "steps": len(upstream.steps)}
                    ).encode()
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    body = {}
                step = upstream._next_step()
                upstream.requests.append({"path": self.path, "body": body, "headers": dict(self.headers)})
                if step.delay_seconds:
                    time.sleep(step.delay_seconds)
                anthropic = "/messages" in self.path
                model = body.get("model") or step.model
                if step.status != 200:
                    # A scripted error can carry a *provider-shaped* body, which is not a
                    # decoration: providers put their own codes in `error.code`, and the
                    # difference between "the provider rejected this" and "Docket refused this"
                    # is exactly what a client has to get right (see `policy.denial_code`).
                    if step.raw_body is not None:
                        payload = step.raw_body
                    elif step.body_override is not None:
                        payload = json.dumps(step.body_override, ensure_ascii=False).encode("utf-8")
                    else:
                        payload = json.dumps(
                            {"error": {"type": "overloaded_error", "message": "scripted upstream error"}}
                        ).encode()
                    self.send_response(step.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if body.get("stream"):
                    self._stream(step, model, anthropic)
                    return
                payload = (
                    step.raw_body
                    if step.raw_body is not None
                    else json.dumps(_response_payload(step, model, anthropic), ensure_ascii=False).encode("utf-8")
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _stream(self, step: Step, model: str, anthropic: bool) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                chunks = _stream_chunks(step, model, anthropic)
                for chunk in chunks:
                    self.wfile.write(chunk.encode("utf-8"))
                    self.wfile.flush()
                    time.sleep(0.001)

        try:
            upstream._server = ThreadingHTTPServer((host, port), Handler)
        except OSError as exc:
            raise OSError(f"cannot bind the scripted provider to {host}:{port}: {exc}") from exc
        upstream._server.daemon_threads = True
        upstream._thread = threading.Thread(target=upstream._server.serve_forever, daemon=True)
        upstream._thread.start()
        address = upstream._server.server_address
        return str(address[0]), int(address[1])

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    @property
    def url(self) -> str:
        # `server_address` is typed loosely by `socketserver`: the host half may be `bytes` on
        # some platforms, and an f-string over bytes would render `b'127.0.0.1'`. Decode it.
        address = self._server.server_address[:2]  # type: ignore[union-attr]
        host = address[0].decode() if isinstance(address[0], bytes) else str(address[0])
        return f"http://{host}:{address[1]}"

    def _next_step(self) -> Step:
        step = self.steps[min(self._index, len(self.steps) - 1)]
        self._index += 1
        return step


def _response_payload(step: Step, model: str, anthropic: bool) -> dict[str, Any]:
    if step.body_override is not None:
        return dict(step.body_override)
    if anthropic:
        return {
            "id": "msg_fake",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": step.text}],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": step.input_tokens,
                "output_tokens": step.output_tokens,
                "cache_read_input_tokens": step.cached_input_tokens,
            },
        }
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": step.text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": step.input_tokens,
            "completion_tokens": step.output_tokens,
            "prompt_tokens_details": {"cached_tokens": step.cached_input_tokens},
        },
    }


def _stream_chunks(step: Step, model: str, anthropic: bool) -> list[str]:
    words = step.text.split(" ")
    out: list[str] = []
    if anthropic:
        out.append(
            "event: message_start\ndata: "
            + json.dumps(
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_fake",
                        "model": model,
                        "usage": {"input_tokens": step.input_tokens, "output_tokens": 0},
                    },
                }
            )
            + "\n\n"
        )
        for word in words:
            out.append(
                "event: content_block_delta\ndata: "
                + json.dumps({"type": "content_block_delta", "delta": {"type": "text_delta", "text": word + " "}})
                + "\n\n"
            )
        out.append(
            "event: message_delta\ndata: "
            + json.dumps(
                {
                    "type": "message_delta",
                    "usage": {
                        "output_tokens": step.output_tokens,
                        "cache_read_input_tokens": step.cached_input_tokens,
                    },
                }
            )
            + "\n\n"
        )
        out.append('event: message_stop\ndata: {"type": "message_stop"}\n\n')
        return out
    out.append(
        "data: "
        + json.dumps(
            {
                "id": "chatcmpl-fake",
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            }
        )
        + "\n\n"
    )
    for word in words:
        out.append(
            "data: "
            + json.dumps(
                {
                    "id": "chatcmpl-fake",
                    "object": "chat.completion.chunk",
                    "model": model,
                    "choices": [{"index": 0, "delta": {"content": word + " "}, "finish_reason": None}],
                }
            )
            + "\n\n"
        )
    out.append(
        "data: "
        + json.dumps(
            {
                "id": "chatcmpl-fake",
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
        )
        + "\n\n"
    )
    out.append(
        "data: "
        + json.dumps(
            {
                "id": "chatcmpl-fake",
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [],
                "usage": {
                    "prompt_tokens": step.input_tokens,
                    "completion_tokens": step.output_tokens,
                    "prompt_tokens_details": {"cached_tokens": step.cached_input_tokens},
                },
            }
        )
        + "\n\n"
    )
    out.append("data: [DONE]\n\n")
    return out


def main(argv: list[str] | None = None) -> int:
    """Run the scripted provider in the foreground: ``python -m docket.fakeupstream``.

    This exists so the provider can be a *service* rather than part of a test process: a container
    or a second terminal that an agent calls over the network. It is the same object the tests use,
    so a containerised smoke test exercises the same wire format as `docket demo`.

    The address is printed on stdout as JSON, because that is what a container health check or a
    shell script can read without parsing prose. A port that is already taken is reported as such
    and exits `2` — the same contract as the boundary's own fixed port.
    """
    parser = argparse.ArgumentParser(
        prog="python -m docket.fakeupstream",
        description="A scripted, offline, provider-shaped upstream for Docket.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="address to bind (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=9100, help="port to listen on (default 9100)")
    parser.add_argument(
        "--steps",
        type=int,
        default=1000,
        help="how many responses to script before the last one repeats forever (default 1000)",
    )
    parser.add_argument("--model", default="claude-sonnet-5", help="model name to report")
    parser.add_argument("--input-tokens", type=int, default=1_000, help="input tokens per response")
    parser.add_argument("--output-tokens", type=int, default=200, help="output tokens per response")
    parser.add_argument(
        "--delay-ms",
        type=int,
        default=0,
        help="artificial delay before each response, in milliseconds (0 = none)",
    )
    args = parser.parse_args(argv)

    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    if args.steps < 1:
        parser.error("--steps must be at least 1")

    step = Step(
        model=args.model,
        input_tokens=args.input_tokens,
        output_tokens=args.output_tokens,
        delay_seconds=args.delay_ms / 1000.0,
    )
    upstream = FakeUpstream(steps=[step] * args.steps)
    if args.host != "127.0.0.1":
        # The default is loopback, and anything else is a deliberate act with a consequence: this
        # server answers every request with scripted numbers. It is for wiring a container network
        # or a pod to a provider that costs nothing — never for a host with a route to the internet.
        print(
            f"fakeupstream: WARNING binding {args.host}, not loopback: this server answers with "
            "scripted usage and must not be reachable beyond the network you control",
            file=sys.stderr,
            flush=True,
        )
    try:
        host, port = upstream.start(args.port, host=args.host)
    except OSError as exc:
        print(f"fakeupstream: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "listening": f"http://{host}:{port}",
                "port": port,
                "steps": args.steps,
                "model": args.model,
            }
        ),
        flush=True,
    )
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:  # pragma: no cover - interactive path
        pass
    finally:
        upstream.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
