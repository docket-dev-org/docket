#!/usr/bin/env python3
"""A deliberately ordinary agent: it reads the model endpoint from the environment.

Nothing here knows Docket exists. That is the point of the example — the boundary is a
base-URL change, so any program that talks to an OpenAI-shaped API can be supervised without
modifying a line of it.

The fourth call is the interesting one: by then the meter has crossed the cap, so the boundary
refuses it with a 403 and a `docket_cap_exceeded` code, and the agent stops cleanly.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import urllib.error
import urllib.request

PROMPTS = [
    "Read the failing test in tests/test_parser.py.",
    "Propose a fix and apply it.",
    "Re-read the whole repository for context before running the tests.",  # the expensive one
    "Try again with even more context.",
]


def main() -> int:
    base = os.environ.get("OPENAI_BASE_URL") or os.environ.get("DOCKET_PROXY_URL", "")
    if not base:
        print("agent: no OPENAI_BASE_URL in the environment; was this run under `docket run`?", file=sys.stderr)
        return 2
    base = base.rstrip("/")
    endpoint = f"{base}/chat/completions" if base.endswith("/v1") else f"{base}/v1/chat/completions"

    for index, prompt in enumerate(PROMPTS, start=1):
        body = json.dumps(
            {
                "model": "gpt-6-luna",
                "messages": [{"role": "user", "content": prompt}],
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=body,
            headers={"content-type": "application/json", "authorization": "Bearer not-a-real-key"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            code = "refused"
            with contextlib.suppress(json.JSONDecodeError, KeyError, TypeError):
                code = json.loads(detail)["error"].get("code") or code
            print(f"  agent: call {index} refused at the boundary ({exc.code}, {code})", flush=True)
            print("  agent: stopping cleanly. Without the boundary I would have kept looping.", flush=True)
            return 0
        usage = payload.get("usage", {})
        print(
            f"  agent: call {index} ok — {usage.get('prompt_tokens', 0):,} in, "
            f"{usage.get('completion_tokens', 0):,} out",
            flush=True,
        )
    print("  agent: finished all four calls", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
