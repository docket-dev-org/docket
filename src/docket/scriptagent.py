"""A small, honest load generator: ``python -m docket.scriptagent``.

``docket chaos`` and ``docket game-day`` need something to supervise, and the
something must behave like a real agent — it must speak HTTP to whatever base URL the
supervisor put in the environment, honour (or refuse to honour) the boundary's
refusals, and be able to keep hammering when a well-behaved agent would stop. This
module is that agent. It is deliberately published rather than hidden in a test file:

* it makes **real** HTTP requests to the configured provider base URL, so a chaos run
  exercises the real boundary, the real ledger and the real supervisor;
* it is configured entirely by environment variables, so it can be pointed at a fake
  upstream (offline, deterministic) or a real provider (with a cap set, which is the
  point of this tool);
* it reports its own steps to the boundary so the agent-reported half of the ledger is
  exercised too.

It is not a model client and does not pretend to be one: it sends fixed prompts and
discards the answers. Its token usage is whatever the provider says it is — the agent
does not estimate, because an agent that estimated its own usage would be exactly the
failure mode Docket exists to make visible.

Configuration (all optional except the base URLs the supervisor sets):

``DOCKET_AGENT_CALLS``      number of model calls to make (default 6)
``DOCKET_AGENT_MODEL``      model name to send (default ``claude-sonnet-5``)
``DOCKET_AGENT_MODE``       ``bounded`` (stop at the first refusal, default) or
                            ``retry`` (ignore refusals and keep calling — the runaway)
``DOCKET_AGENT_SLEEP``      seconds between calls (default 0)
``DOCKET_AGENT_LONG``       seconds to keep the process alive after the last call
                            (used to test what happens when a slow agent is killed)
``DOCKET_AGENT_PROMPT``     the prompt text to send
``DOCKET_AGENT_WRITE``      a path to write inside the watched workspace (tests fsdiff
                            evidence and deny_write policy)
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

DEFAULT_PROMPT = "Summarise the failing test and propose the smallest fix."


def _report(kind: str, payload: dict[str, Any]) -> None:
    root = os.environ.get("DOCKET_PROXY_URL")
    if not root:
        return
    request = urllib.request.Request(
        f"{root}/docket/{kind}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    # Instrumentation must never break the agent being recorded.
    with contextlib.suppress(Exception):
        urllib.request.urlopen(request, timeout=5).read()


def _one_call(base: str, model: str, prompt: str, timeout: float) -> tuple[int, str]:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "metadata": {"source": "docket.scriptagent"},
    }
    request = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer scriptagent"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
            return response.status, ""
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        code = ""
        with contextlib.suppress(json.JSONDecodeError, KeyError, TypeError):
            code = json.loads(detail)["error"].get("code") or ""
        return exc.code, code
    except Exception as exc:  # noqa: BLE001 - the boundary can fail in interesting ways
        return 0, f"{type(exc).__name__}: {exc}"


def main(argv: list[str] | None = None) -> int:
    base = os.environ.get("OPENAI_BASE_URL")
    if not base:
        print("scriptagent: OPENAI_BASE_URL is not set (run me under `docket run`)", file=sys.stderr)
        return 2

    calls = int(os.environ.get("DOCKET_AGENT_CALLS", "6"))
    model = os.environ.get("DOCKET_AGENT_MODEL", "claude-sonnet-5")
    mode = os.environ.get("DOCKET_AGENT_MODE", "bounded")
    sleep_for = float(os.environ.get("DOCKET_AGENT_SLEEP", "0"))
    linger = float(os.environ.get("DOCKET_AGENT_LONG", "0"))
    prompt = os.environ.get("DOCKET_AGENT_PROMPT", DEFAULT_PROMPT)
    write_path = os.environ.get("DOCKET_AGENT_WRITE")

    if write_path:
        target = os.path.abspath(write_path)
        with contextlib.suppress(OSError):
            os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        try:
            with open(target, "a", encoding="utf-8") as handle:
                handle.write("written by docket.scriptagent\n")
            _report("note", {"message": f"wrote {os.path.basename(target)}"})
        except OSError as exc:
            _report("note", {"message": f"could not write {write_path}: {exc}"})

    statuses: list[int] = []
    refusals: list[str] = []
    for index in range(calls):
        status, code = _one_call(base, model, f"{prompt} (step {index + 1}/{calls})", timeout=30)
        statuses.append(status)
        if status == 0:
            # The boundary is unreachable — that is a result, not a crash: record it and
            # keep going, because an agent that dies on the first transport error tells
            # you nothing about blast radius.
            print(f"scriptagent: step {index + 1} transport error: {code}", file=sys.stderr, flush=True)
            _report("note", {"message": f"step {index + 1}: transport error", "detail": code})
            continue
        if status >= 400:
            refusals.append(code or str(status))
            print(f"scriptagent: step {index + 1} refused: HTTP {status} {code}", file=sys.stderr, flush=True)
            if mode == "bounded":
                _report("note", {"message": f"stopped at step {index + 1}: the boundary refused a call"})
                break
        if sleep_for:
            time.sleep(sleep_for)
        _report("note", {"message": f"step {index + 1} done", "status": status})

    _report(
        "note",
        {
            "message": "scriptagent finished",
            "calls": len(statuses),
            "refusals": refusals,
            "mode": mode,
        },
    )
    outcome = {
        "calls": len(statuses),
        "statuses": statuses,
        "refusals": refusals,
        "mode": mode,
    }
    # The outcome is reported to the boundary as well as printed: it is the independent
    # count of what this process saw on the wire, and `docket chaos` cross-checks the
    # ledger against it. An audit that trusts only the boundary's own bookkeeping is a
    # self-portrait, not a check.
    _report("note", {"message": "scriptagent: " + json.dumps(outcome)})
    print("scriptagent: " + json.dumps(outcome), file=sys.stderr, flush=True)
    if linger:
        time.sleep(linger)
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
