#!/usr/bin/env python3
"""One model call, then exit 0 — the smallest agent a pipeline can gate on.

This exists for `.github/workflows/action-smoke.yml` and for anyone trying the boundary locally. It
makes exactly one call through whatever base URL the environment names, prints the model it saw, and
stops. No API key, no network: point it at the fake provider in `examples/ci/fake_provider.py`.

    python3 examples/ci/fake_provider.py &          # prints a URL
    docket run --policy examples/ci/docket.toml --upstream openai=<that URL> \
      -- python3 examples/ci/agent.py

Requires: Python 3.11 or newer and nothing else — it uses only the standard library.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

#: The model this agent asks for. It must match a pattern in `src/docket/prices.json` and be in the
#: policy's `models.allow`, or the boundary refuses the call and the receipt records a denial rather
#: than a spend.
MODEL = "gpt-6-luna"


def main() -> int:
    base = os.environ.get("OPENAI_BASE_URL") or os.environ.get("DOCKET_PROXY_URL")
    if not base:
        print("no OPENAI_BASE_URL and no DOCKET_PROXY_URL; run this under `docket run`", file=sys.stderr)
        return 2
    base = base.rstrip("/")
    body = json.dumps(
        {
            "model": MODEL,
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "Say the single word: ready."}],
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base}/chat/completions",
        data=body,
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as error:
        # The boundary's refusal is the interesting case, and it is not a crash: exit 42 is what
        # `docket run` uses for a cap that fired.
        detail = error.read().decode("utf-8", "replace")
        print(f"the boundary refused the call: {error.code} {detail}", file=sys.stderr)
        return 42
    print(f"agent saw model: {payload.get('model')}")
    print(f"agent saw text:  {payload.get('choices', [{}])[0].get('message', {}).get('content', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
