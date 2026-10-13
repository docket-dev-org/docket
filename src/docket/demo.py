"""``docket demo`` — the whole product in about three seconds, offline.

This module is not a simulation of Docket. It runs the *real* supervisor against the
*real* boundary proxy, with the *real* ledger, the *real* verifier and a scripted
provider so that you can watch a runaway agent get stopped, and then check the
result yourself, without an API key, an account or a network connection.

The scenario is modelled on the failure pattern documented in public incident
reports: an agent retries a step it cannot fix, re-sends the same request, and
burns money linearly until something stops it. Here, something does.
"""

from __future__ import annotations

import contextlib
import json
import os
import random
import shutil
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from .fakeupstream import FakeUpstream, Step
from .ledger import Ledger
from .policy import Caps, Files, Policy
from .report import render_receipt
from .supervisor import EXIT_CAP_EXCEEDED, Supervisor
from .verify import verify_receipt

PLAN_MODEL = "claude-opus-4-8"
WORK_MODEL = "claude-sonnet-5"
DEFAULT_MAX_USD = Decimal("2.00")

SCENARIO: list[Step] = [
    Step(model=PLAN_MODEL, input_tokens=900, output_tokens=220, text="plan: inspect the ledger service, then patch it"),
    Step(
        model=WORK_MODEL,
        input_tokens=122_000,
        output_tokens=400,
        text="read: ledger.py is 4,181 lines; the failing assertion is in reconcile()",
    ),
    # From here the agent repeats the same decision over and over: the classic loop.
    Step(model=WORK_MODEL, input_tokens=40_000, output_tokens=300, text="retrying reconcile() with the same input"),
]


def _write_workspace(root: Path) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "ledger.py").write_text(
        "def reconcile(rows):\n    return sum(r['amount'] for r in rows)\n", encoding="utf-8"
    )
    (root / "README.md").write_text("# demo workspace\n\nFor `docket demo`. Safe to delete.\n", encoding="utf-8")


def child_main() -> int:
    """The scripted agent that ``docket demo`` supervises.

    Runs as a separate process on purpose: caps are enforced by terminating a real
    process group, and a demo that faked that would be lying about the mechanism.
    """
    import urllib.error
    import urllib.request

    base = os.environ.get("OPENAI_BASE_URL")
    proxy_root = os.environ.get("DOCKET_PROXY_URL")
    workdir = Path(os.environ.get("DOCKET_DEMO_WORKDIR", "."))
    if not base:
        print("demo child: OPENAI_BASE_URL is not set", file=sys.stderr)
        return 2

    def call(model: str, prompt: str, *, stream: bool = True, big_tool: bool = False) -> dict[str, Any] | None:
        body = {
            "model": model,
            "stream": stream,
            "messages": [
                {"role": "system", "content": "You are a careful maintenance agent."},
                {"role": "user", "content": prompt},
            ],
        }
        request = urllib.request.Request(
            f"{base}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer demo-key"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            code = "policy"
            with contextlib.suppress(json.JSONDecodeError, KeyError, TypeError):
                code = json.loads(detail)["error"].get("code") or "policy"
            print(f"    agent: the boundary refused this call ({exc.code}, {code})", file=sys.stderr, flush=True)
            return None
        if proxy_root:
            _report(proxy_root, "note", {"message": f"step complete: {model}"})
            if big_tool:
                _report(
                    proxy_root,
                    "tool/call",
                    {"name": "read_file", "args_digest": "b" * 8, "result_bytes": 122_000},
                )
        return {"raw": raw}

    def _report(root: str, kind: str, payload: dict[str, Any]) -> None:
        request = urllib.request.Request(
            f"{root}/docket/{kind}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        # Instrumentation must never break the agent being recorded.
        with contextlib.suppress(Exception):  # pragma: no cover
            urllib.request.urlopen(request, timeout=5).read()

    # 1. plan (small call on an expensive model)
    if call(PLAN_MODEL, "Plan the fix for the failing reconcile() assertion.", stream=False) is None:
        return 3

    # 2. edit files (including one inside a protected path) before the run goes bad,
    #    so the filesystem evidence is complete and the demo is deterministic.
    target = workdir / "src" / "ledger.py"
    target.write_text(
        "def reconcile(rows, tolerance=0.0):\n"
        "    return sum(r['amount'] for r in rows if abs(r['amount']) > tolerance)\n",
        encoding="utf-8",
    )
    (workdir / "src" / "reconcile_fix.py").write_text("TOLERANCE = 0.0\n", encoding="utf-8")
    (workdir / ".env").write_text(
        "# written by the agent, inside the deny_write scope\n" "AWS_SECRET_ACCESS_KEY=demo-not-a-real-key\n",
        encoding="utf-8",
    )

    # 3. read a huge tool result, which bloats the context from here on
    if call(WORK_MODEL, "Here is the file: " + ("ledger-row " * 9_000), big_tool=True) is None:
        return 3

    # 4. the loop: identical request, no new information, forever
    loop_prompt = "Re-run reconcile() with the same input and report the same failure."
    for _ in range(40):
        if call(WORK_MODEL, loop_prompt) is None:
            # The boundary refused a call: this is the moment the run is bounded,
            # and the moment this agent's own logic proves it would never stop.
            print("    agent: stopping here. It would have kept going.", file=sys.stderr, flush=True)
            break

    _report(proxy_root or "", "note", {"message": "agent finished its pass"})
    return 0


def run_demo(
    *,
    max_usd: Decimal = DEFAULT_MAX_USD,
    keep: bool = False,
    tamper: bool = False,
    color: bool = True,
    out: Any = None,
) -> dict[str, Any]:
    """Run the offline demo end to end and print a narrative report."""
    stream = out if out is not None else sys.stdout
    workspace = Path(tempfile.mkdtemp(prefix="docket-demo-"))
    _write_workspace(workspace)
    upstream = FakeUpstream(steps=SCENARIO)
    host, port = upstream.start()

    epoch = 1_792_000_000.0  # a fixed clock keeps the demo reproducible

    def clock() -> str:
        clock.tick += 0.41
        return time.strftime("%H:%M:%S", time.gmtime(epoch + clock.tick))

    clock.tick = 0.0  # type: ignore[attr-defined]

    monotonic_value = {"t": 0.0}

    def monotonic() -> float:
        monotonic_value["t"] += 0.02
        return monotonic_value["t"]

    policy = Policy(
        name="demo: stop a runaway agent",
        caps=Caps(max_usd=max_usd, warn_at=0.7, on_breach="kill", grace_seconds=2.0),
        files=Files(watch=["."], ignore=[".docket/**"], deny_write=[".env", "**/.env"]),
    )

    print(file=stream)
    print("  Scenario: an agent is asked to fix one failing test.", file=stream)
    print("  It plans on an expensive model, reads a huge file, then gets stuck re-sending", file=stream)
    print("  the same request. Nothing inside the agent will ever stop it.", file=stream)
    print(file=stream)
    print(f"  Policy: max_usd = ${max_usd}, deny_write = [.env], watch = [.]", file=stream)
    print(file=stream)
    print("  running (real supervisor, real boundary, scripted provider)…", file=stream)
    stream.flush()

    package_root = str(Path(__file__).resolve().parents[1])
    existing = os.environ.get("PYTHONPATH") or ""
    supervisor = Supervisor(
        [sys.executable, "-m", "docket.demo", "--child"],
        policy=policy,
        docket_dir=workspace / ".docket",
        cwd=workspace,
        upstreams={"openai": f"http://{host}:{port}"},
        clock=clock,
        monotonic=monotonic,
        rng=random.Random(20261013),
        env_extra={
            "DOCKET_DEMO_WORKDIR": str(workspace),
            "PYTHONPATH": os.pathsep.join([package_root, existing]) if existing else package_root,
        },
        label="demo",
    )
    result = supervisor.run()
    upstream.stop()

    receipt = result.receipt
    print(file=stream)
    print(render_receipt(receipt, color=color), file=stream)
    print(file=stream)
    verification = verify_receipt(receipt, ledger_events=Ledger.read(result.ledger_path))
    print(verification.render(color=color), file=stream)

    outcome: dict[str, Any] = {
        "workspace": str(workspace),
        "receipt_path": str(result.receipt_path),
        "ledger_path": str(result.ledger_path),
        "exit_code": result.exit_code,
        "breached": result.breached,
        "verify_ok": verification.ok,
        "cost_usd": receipt.get("totals", {}).get("cost_usd"),
        "stopped_as_expected": result.exit_code == EXIT_CAP_EXCEEDED,
    }

    if tamper:
        print(file=stream)
        print("  Now tampering with one byte of the ledger and verifying again:", file=stream)
        lines = Path(result.ledger_path).read_text(encoding="utf-8").splitlines()
        target = min(5, len(lines) - 1)
        payload = json.loads(lines[target])
        payload["data"]["model"] = "claude-haiku-forged" if payload.get("type") == "model.response" else payload["data"]
        lines[target] = json.dumps(payload)
        Path(result.ledger_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
        broken = verify_receipt(receipt, ledger_events=Ledger.read(result.ledger_path))
        print(broken.render(color=color), file=stream)
        outcome["tamper_detected"] = not broken.ok
        outcome["tamper_failures"] = broken.failures

    if keep:
        print(file=stream)
        print(f"  Workspace kept at {workspace}", file=stream)
    else:
        shutil.rmtree(workspace, ignore_errors=True)
    print(file=stream)
    print("  Try it on your own agent:", file=stream)
    print("    docket run --max-usd 5 -- my-agent --flag", file=stream)
    print(file=stream)
    return outcome


if __name__ == "__main__":  # pragma: no cover - exercised through `docket demo`
    if "--child" in sys.argv:
        raise SystemExit(child_main())
    raise SystemExit(0)
