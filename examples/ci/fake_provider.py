#!/usr/bin/env python3
"""A one-response OpenAI-shaped provider, for trying the boundary without an API key.

This exists so a pipeline can exercise Docket's run, verify and gate path without a credential and
without a network call. It answers `/v1/chat/completions` with a fixed, deterministic response and
refuses everything else, so a mistake in how the boundary is pointed at it shows up as a 404 rather
than as a silently empty receipt.

Run it, point the action's `upstreams` input at the URL it prints, and the run records one model
call against the scripted price table.

    python3 examples/ci/fake_provider.py            # prints the URL, then serves

Requires: Python 3.11 or newer, and nothing else — it uses only the standard library.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

#: The model this provider claims to be. It must match a pattern in `src/docket/prices.json`, or
#: the boundary prices the call at the catch-all and the recorded spend is wrong rather than zero.
MODEL = "gpt-6-luna"

#: A response in the shape an OpenAI-compatible client expects.
RESPONSE = {
    "id": "chatcmpl-fake-provider",
    "object": "chat.completion",
    "model": MODEL,
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "scripted response from the fake provider"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 900, "completion_tokens": 220, "total_tokens": 1120},
}


class Handler(BaseHTTPRequestHandler):
    """Answers the one route a boundary forwards, and nothing else."""

    server_version = "DocketFakeProvider/1"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - the base class names it
        # The boundary is chatty; a provider that logs every probe drowns the run's own output.
        return

    def do_GET(self) -> None:
        if self.path.rstrip("/") in ("/health", "/v1/models"):
            self._json(200, {"object": "list", "data": [{"id": MODEL, "object": "model"}]})
            return
        self._json(404, {"error": {"message": f"no route for {self.path}", "type": "not_found"}})

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            self._json(400, {"error": {"message": "body must be JSON", "type": "invalid_request_error"}})
            return
        if self.path.rstrip("/") not in ("/v1/chat/completions", "/chat/completions"):
            self._json(404, {"error": {"message": f"no route for {self.path}", "type": "not_found"}})
            return
        response = dict(RESPONSE)
        response["model"] = str(payload.get("model") or MODEL)
        self._json(200, response)

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    host, port = server.server_address[0], server.server_address[1]
    # The URL is printed on its own line and flushed, because the caller reads it from a pipe.
    print(f"http://{host}:{port}", flush=True)
    thread = threading.Thread(target=server.serve_forever, name="fake-provider", daemon=True)
    thread.start()
    try:
        thread.join()
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
