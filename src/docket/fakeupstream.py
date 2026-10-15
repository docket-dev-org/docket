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

import json
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
    def start(self) -> tuple[str, int]:
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # noqa: D102
                return

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

        upstream._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        upstream._server.daemon_threads = True
        upstream._thread = threading.Thread(target=upstream._server.serve_forever, daemon=True)
        upstream._thread.start()
        host, port = upstream._server.server_address[:2]
        return host, port

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]  # type: ignore[union-attr]
        return f"http://{host}:{port}"

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
