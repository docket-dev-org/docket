"""Diagnostics: the artefact a stranger needs to help you, and the probe that tells you
whether a boundary is working before an agent depends on it.

Two things live here and they share a purpose — making a failure explainable.

``write_diagnostic_bundle``
    Collects the minimum set of facts that diagnose most Docket problems (versions,
    platform, policy, ledger *health* rather than ledger *contents*, receipt summaries,
    environment checks) into a single gzipped tarball. It is deliberately not a
    "dump everything": ledger bodies and receipt bodies are omitted, because a
    diagnostic bundle that leaks a customer's prompts is worse than no bundle, and the
    interesting fields for diagnosis — counts, digests, failure classes — are not the
    sensitive ones.

``self_test``
    A synthetic-monitoring probe for the boundary itself. It starts a real recording
    proxy and a real (fake) upstream in this process and exercises the properties the
    project claims: forwarding and recording, policy refusal, cap enforcement, rate
    limiting, circuit breaking, fault injection, ledger write failure handling, receipt
    construction and offline verification. Each probe reports pass/fail with the
    evidence it observed, so a CI job or a cron can assert that a deployment still
    behaves — which is exactly what synthetic monitoring is for.

Both are written to be honest about the partial-failure case: a probe that cannot run
(no port, no disk) reports ``skipped`` with the reason rather than passing quietly.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__
from . import events as ev
from .ledger import Ledger
from .policy import Caps, Policy
from .pricing import CostMeter, PriceTable
from .receipt import build_receipt
from .resilience import CircuitBreaker, FaultInjector, HealthState, RateLimiter, parse_fault_spec

BUNDLE_EXCLUDES = ("*.jsonl", "*.receipt.json", "*.torn-*", "*.lock")

#: A child program that makes its own filesystem writes fail, then reports what the
#: ledger did about it. RLIMIT_FSIZE is used rather than a full or read-only device
#: because it needs no privileges, works on every POSIX platform, and constrains a real
#: regular file — which is the only kind of file a ledger may be (see ledger.Ledger.open).
WRITE_FAILURE_PROBE = """
import contextlib, json, os, resource, signal, sys, tempfile
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from docket.ledger import Ledger

# The kernel raises SIGXFSZ when a write would exceed the limit; ignoring it turns the
# event into the OSError (EFBIG) that a real full disk produces.
signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
resource.setrlimit(resource.RLIMIT_FSIZE, (4096, 4096))
target = Path(tempfile.mkdtemp()) / "limited.jsonl"
ledger = Ledger.open(target, max_bytes=None)
for index in range(40):
    ledger.append("note", {"index": index, "padding": "x" * 200})
closed_cleanly = True
try:
    ledger.close()
except Exception as exc:  # a close that raises would break every orderly shutdown
    closed_cleanly = f"{type(exc).__name__}: {exc}"
print(json.dumps({
    "unwritten": len(ledger.unwritten),
    "first_error": (ledger.unwritten[0]["error"] if ledger.unwritten else None),
    "on_disk_bytes": target.stat().st_size,
    "chain_valid": ledger.chain_error() is None,
    "closed_cleanly": closed_cleanly,
}))
"""


# --------------------------------------------------------------------------- #
# Diagnostic bundle
# --------------------------------------------------------------------------- #


def _ledger_health(path: Path) -> dict[str, Any]:
    _, diagnostics = Ledger.read_with_diagnostics(path)
    return {
        "name": path.name,
        "bytes": diagnostics.get("bytes"),
        "lines": diagnostics.get("lines"),
        "healthy": diagnostics.get("healthy"),
        "torn_tail": bool(diagnostics.get("torn_tail")),
        "bad_lines": len(diagnostics.get("bad_lines") or []),
        "utf8": diagnostics.get("utf8"),
    }


def _receipt_summary(path: Path) -> dict[str, Any]:
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"name": path.name, "unreadable": f"{type(exc).__name__}: {exc}"}
    return {
        "name": path.name,
        "docket_number": receipt.get("docket_number"),
        "created_at": receipt.get("created_at"),
        "format": receipt.get("format"),
        "tool": receipt.get("tool", {}).get("version"),
        "exit_code": receipt.get("session", {}).get("exit_code"),
        "breached": receipt.get("session", {}).get("breached"),
        "totals": {k: receipt.get("totals", {}).get(k) for k in ("calls", "cost_usd", "total_tokens")},
        "ledger": {
            k: receipt.get("ledger", {}).get(k) for k in ("events", "head", "merkle_root", "file_sha256")
        },
        "evidence": receipt.get("evidence"),
        "ops": {
            "chaos_run": (receipt.get("ops") or {}).get("chaos_run"),
            "circuit_breaker": (receipt.get("ops") or {}).get("circuit_breaker"),
            "rate_limiter": (receipt.get("ops") or {}).get("rate_limiter"),
        },
        "finding_rules": [f.get("rule") for f in receipt.get("findings", [])],
        "sealed": bool(receipt.get("seal")),
        "signed": bool(receipt.get("signature")),
    }


def collect_diagnostics(docket_dir: Path) -> dict[str, Any]:
    """Everything a maintainer would ask for first, and nothing that leaks a prompt."""
    runs = sorted((docket_dir / "runs").glob("*.jsonl")) if (docket_dir / "runs").exists() else []
    receipts = (
        sorted((docket_dir / "receipts").glob("*.receipt.json")) if (docket_dir / "receipts").exists() else []
    )
    torn = sorted((docket_dir / "runs").glob("*.torn-*")) if (docket_dir / "runs").exists() else []
    free = None
    try:
        free = shutil.disk_usage(docket_dir if docket_dir.exists() else Path.cwd()).free
    except OSError:  # pragma: no cover - platform dependent
        pass
    return {
        "collected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "docket": {
            "version": __version__,
            "program": "docket",
        },
        "environment": {
            "python": sys.version.split()[0],
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "node": shutil.which("node") or None,
            "cwd": os.getcwd(),
            "free_bytes": free,
        },
        "docket_dir": str(docket_dir),
        "ledgers": [_ledger_health(p) for p in runs],
        "quarantined": [p.name for p in torn],
        "receipts": [_receipt_summary(p) for p in receipts],
        "checks": environment_checks(docket_dir),
        "note": (
            "This bundle contains counts, digests and health classifications only. "
            "Ledger lines, receipts, prompts and file contents are deliberately excluded."
        ),
    }


def environment_checks(docket_dir: Path) -> list[dict[str, Any]]:
    """The same checks `docket doctor` prints, in data form."""
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    add("python >= 3.11", sys.version_info >= (3, 11), sys.version.split()[0])
    node = shutil.which("node")
    add("node present", bool(node), node or "not found (only needed for verify-js)")
    writable = os.access(docket_dir if docket_dir.exists() else Path.cwd(), os.W_OK)
    add("docket dir writable", writable, str(docket_dir))
    try:
        from .signing import ed25519_available

        add("cryptography (signing)", True, "installed" if ed25519_available() else "optional, not installed")
    except Exception as exc:  # pragma: no cover - defensive
        add("cryptography (signing)", True, f"probe failed: {exc}")
    try:
        free = shutil.disk_usage(Path.cwd()).free
        add("free space > 16 MiB", free > 16 * 1024 * 1024, f"{free // (1024 * 1024)} MiB")
    except OSError as exc:  # pragma: no cover
        add("free space", False, str(exc))
    return checks


def write_diagnostic_bundle(docket_dir: Path, destination: Path) -> Path:
    """Write a gzipped tar with ``diagnostics.json`` and a README explaining consent."""
    report = collect_diagnostics(docket_dir)
    destination.parent.mkdir(parents=True, exist_ok=True)
    readme = (
        "Docket diagnostic bundle\n"
        "========================\n\n"
        "diagnostics.json contains: version and platform information, the health of each\n"
        "ledger (parses / torn tail / invalid lines, with byte counts), a summary of each\n"
        "receipt (numbers, digests, finding rule names), and environment checks.\n\n"
        "It does NOT contain: ledger contents, receipt contents, prompts, completions, file\n"
        "contents, API keys or authorisation headers. Read it before sharing it; if you need\n"
        "to share more, copy the specific files deliberately.\n"
    )
    with tarfile.open(destination, "w:gz") as archive:
        payload = json.dumps(report, indent=2, sort_keys=True).encode("utf-8")
        for name, blob in (("diagnostics.json", payload), ("README.txt", readme.encode("utf-8"))):
            info = tarfile.TarInfo(name)
            info.size = len(blob)
            info.mtime = int(time.time())
            archive.addfile(info, io.BytesIO(blob))
    return destination


# --------------------------------------------------------------------------- #
# Self test (synthetic monitoring)
# --------------------------------------------------------------------------- #


@dataclass
class Probe:
    """One assertion, with the evidence that justified it."""

    name: str
    status: str  # pass | fail | skipped
    detail: str
    data: dict[str, Any] = field(default_factory=dict)


def _call(base: str, path: str, body: dict[str, Any] | None = None, timeout: float = 15.0) -> tuple[int, dict[str, Any]]:
    payload = json.dumps(body or {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hi"}]})
    request = urllib.request.Request(
        base + path, data=payload.encode("utf-8"), headers={"content-type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return exc.code, {"raw": raw[:200].decode("utf-8", "replace")}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # Includes RemoteDisconnected: a peer that closed the socket without answering is
        # a result the probe must be able to report, not a crash.
        return 0, {"transport_error": f"{type(exc).__name__}: {exc}"}


class _FailingUpstream:
    """A local upstream that always answers 500, for the breaker probe."""

    def __init__(self) -> None:
        import http.server
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # noqa: D102 - silence
                return

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)  # consume, so keep-alive stays sane
                body = b'{"error":{"message":"probe upstream always fails","type":"probe_failure"}}'
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def self_test(*, keep_workspace: bool = False) -> tuple[list[Probe], Path]:
    """Exercise the boundary's claimed properties in this process. Returns probes + dir.

    Nothing here is simulated at the level that matters: the proxy binds a real socket,
    the child makes real HTTP requests, the ledger is a real file, and the receipt is
    verified by the same code path a user would run. Where a probe cannot be performed
    (an environment without a writable temp directory, say), it reports ``skipped`` with
    the reason instead of claiming success.
    """
    from .fakeupstream import FakeUpstream, Step
    from .proxy import RecordingProxy

    probes: list[Probe] = []
    scripted = FakeUpstream(
        steps=[
            Step(model="claude-sonnet-5", input_tokens=12000, output_tokens=300),
            Step(model="claude-sonnet-5", input_tokens=12000, output_tokens=300),
            Step(model="claude-sonnet-5", input_tokens=12000, output_tokens=300),
        ]
    )
    upstream_host, upstream_port = scripted.start()
    failing = _FailingUpstream()
    workspace = Path(tempfile.mkdtemp(prefix="docket-selftest-"))

    def add(name: str, condition: bool, detail: str, **data: Any) -> None:
        probes.append(Probe(name=name, status="pass" if condition else "fail", detail=detail, data=data))

    table = PriceTable.load()
    ledger_path = workspace / "runs" / "selftest.jsonl"
    ledger = Ledger.open(ledger_path, max_bytes=None)
    policy = Policy(name="selftest", caps=Caps(max_usd=None))

    try:
        # -- 1. forwarding, recording, and readiness ------------------------- #
        proxy = RecordingProxy(
            ledger=ledger,
            policy=policy,
            meter=CostMeter(price_table=table),
            session_id="selftest",
            upstreams={"openai": f"http://{upstream_host}:{upstream_port}"},
            breaker=CircuitBreaker(threshold=2, cooldown_seconds=30),
            limiter=RateLimiter(),
            health=HealthState(),
            upstream_timeout=10,
        )
        host, port = proxy.start()
        base = f"http://{host}:{port}"

        status, payload = _call(base, "/openai/v1/chat/completions")
        add(
            "forwards and records a model call",
            status == 200 and len(ledger) >= 2,
            f"HTTP {status}, {len(ledger)} ledger event(s) after the first call",
            cost=payload.get("usage"),
        )
        usage_recorded = [
            e for e in ledger if e.type == ev.MODEL_RESPONSE and e.data.get("usage", {}).get("input_tokens") == 12000
        ]
        add(
            "records provider-reported usage verbatim",
            bool(usage_recorded),
            f"{len(usage_recorded)} model.response event(s) carry the upstream's own token counts",
        )

        # -- 2. health endpoints -------------------------------------------- #
        health_status, health_body = _call(base, "/docket/healthz")
        add(
            "answers /docket/healthz",
            health_status == 200 and health_body.get("status") == "ok",
            f"HTTP {health_status}, status={health_body.get('status')!r}",
        )
        ready_status, ready_body = _call(base, "/docket/readyz")
        add(
            "readiness reflects writable evidence",
            ready_status == 200 and ready_body.get("ready") is True,
            f"HTTP {ready_status}, problems={ready_body.get('problems')}",
        )
        metrics = urllib.request.urlopen(base + "/docket/metrics", timeout=10).read().decode("utf-8")
        required = {
            "docket_model_calls_total",
            "docket_model_denied_total",
            "docket_model_error_total",
            "docket_cost_microusd_total",
            "docket_breaker_open",
            "docket_uptime_seconds",
        }
        missing = sorted(name for name in required if name not in metrics)
        add(
            "exposes Prometheus metrics for the guarantees it makes",
            not missing,
            f"{len(metrics.splitlines())} metric lines; verified "
            f"{len(required) - len(missing)}/{len(required)} required series"
            + (f"; missing {missing}" if missing else f" including {sorted(required)[0]}"),
        )
        proxy.stop()

        # -- 3. policy refusal ---------------------------------------------- #
        ledger2 = Ledger.open(workspace / "runs" / "policy.jsonl", max_bytes=None)
        denying = Policy(name="deny", caps=Caps(max_usd=None))
        denying.models.deny = ["claude-*"]
        proxy2 = RecordingProxy(
            ledger=ledger2,
            policy=denying,
            meter=CostMeter(price_table=table),
            session_id="policy",
            upstreams={"openai": f"http://{upstream_host}:{upstream_port}"},
            upstream_timeout=10,
        )
        h2, p2 = proxy2.start()
        denied_status, denied_body = _call(f"http://{h2}:{p2}", "/openai/v1/chat/completions")
        denied_events = [e for e in ledger2 if e.type == ev.MODEL_DENIED]
        add(
            "refuses a denied model before forwarding",
            denied_status == 403 and len(denied_events) == 1,
            f"HTTP {denied_status}, code={denied_body.get('error', {}).get('code')!r}, "
            f"{len(denied_events)} model.denied event(s)",
        )
        proxy2.stop()

        # -- 4. cap enforcement --------------------------------------------- #
        ledger3 = Ledger.open(workspace / "runs" / "cap.jsonl", max_bytes=None)
        capped = Policy(name="cap", caps=Caps(max_usd="0.001", on_breach="deny"))
        proxy3 = RecordingProxy(
            ledger=ledger3,
            policy=capped,
            meter=CostMeter(price_table=table),
            session_id="cap",
            upstreams={"openai": f"http://{upstream_host}:{upstream_port}"},
            upstream_timeout=10,
        )
        h3, p3 = proxy3.start()
        first, _ = _call(f"http://{h3}:{p3}", "/openai/v1/chat/completions")
        second, second_body = _call(f"http://{h3}:{p3}", "/openai/v1/chat/completions")
        exceeded = [e for e in ledger3 if e.type == ev.CAP_EXCEEDED]
        add(
            "crosses a cap and refuses the next call",
            first == 200 and second == 403 and len(exceeded) == 1,
            f"first call HTTP {first}, second HTTP {second} "
            f"({second_body.get('error', {}).get('code')}), cap={exceeded[0].data.get('cap') if exceeded else None}",
        )
        proxy3.stop()

        # -- 5. rate limiting ------------------------------------------------ #
        ledger4 = Ledger.open(workspace / "runs" / "rate.jsonl", max_bytes=None)
        proxy4 = RecordingProxy(
            ledger=ledger4,
            policy=policy,
            meter=CostMeter(price_table=table),
            session_id="rate",
            upstreams={"openai": f"http://{upstream_host}:{upstream_port}"},
            limiter=RateLimiter(per_minute=2),
            upstream_timeout=10,
        )
        h4, p4 = proxy4.start()
        codes = [_call(f"http://{h4}:{p4}", "/openai/v1/chat/completions")[0] for _ in range(3)]
        limited = [e for e in ledger4 if e.type == "x-docket.rate-limited"]
        add(
            "throttles a fast agent and records it",
            codes[:2] == [200, 200] and codes[2] == 429 and len(limited) == 1,
            f"statuses {codes}, {len(limited)} rate-limit event(s)",
        )
        proxy4.stop()

        # -- 6. circuit breaker ---------------------------------------------- #
        ledger5 = Ledger.open(workspace / "runs" / "breaker.jsonl", max_bytes=None)
        breaker = CircuitBreaker(threshold=2, cooldown_seconds=30)
        proxy5 = RecordingProxy(
            ledger=ledger5,
            policy=policy,
            meter=CostMeter(price_table=table),
            session_id="breaker",
            upstreams={"openai": failing.url},
            breaker=breaker,
            upstream_timeout=10,
        )
        h5, p5 = proxy5.start()
        breaker_codes = [_call(f"http://{h5}:{p5}", "/openai/v1/chat/completions")[0] for _ in range(3)]
        errors = [e for e in ledger5 if e.type == ev.MODEL_ERROR]
        open_refusals = [e for e in ledger5 if e.type == ev.MODEL_DENIED and e.data.get("code") == "circuit_open"]
        add(
            "opens a circuit after repeated upstream failure",
            breaker_codes[:2] == [500, 500] and breaker_codes[2] == 503 and breaker.state_of("openai") == "open",
            f"statuses {breaker_codes}, {len(errors)} model.error event(s), "
            f"{len(open_refusals)} refusal(s) naming circuit_open",
        )
        add(
            "a failed call is not recorded as a successful one",
            len(errors) == 2 and not [e for e in ledger5 if e.type == ev.MODEL_RESPONSE],
            f"{len(errors)} model.error, "
            f"{len([e for e in ledger5 if e.type == ev.MODEL_RESPONSE])} model.response",
        )
        proxy5.stop()

        # -- 7. fault injection ---------------------------------------------- #
        ledger6 = Ledger.open(workspace / "runs" / "chaos.jsonl", max_bytes=None)
        injector = FaultInjector(parse_fault_spec("status=503:1.0,seed=11"), seed=11)
        proxy6 = RecordingProxy(
            ledger=ledger6,
            policy=policy,
            meter=CostMeter(price_table=table),
            session_id="chaos",
            upstreams={"openai": f"http://{upstream_host}:{upstream_port}"},
            injector=injector,
            upstream_timeout=10,
        )
        h6, p6 = proxy6.start()
        injected_code, injected_body = _call(f"http://{h6}:{p6}", "/openai/v1/chat/completions")
        fault_events = [e for e in ledger6 if e.type == "x-docket.fault"]
        add(
            "injects faults deterministically and records them",
            injected_code == 503
            and injected_body.get("error", {}).get("type") == "injected_fault"
            and len(fault_events) == 1,
            f"HTTP {injected_code}, {len(fault_events)} x-docket.fault event(s) with seed {injector.seed}",
        )
        proxy6.stop()

        # -- 8. ledger write failure ----------------------------------------- #
        # Two distinct failure classes, tested against the real filesystem rather than a
        # patched object: a handle that was merely closed (recoverable) and a device that
        # refuses every write (not recoverable).
        hearse_path = workspace / "runs" / "closed.jsonl"
        reopened = Ledger.open(hearse_path, max_bytes=None)
        reopened.append("note", {"before": "the handle is closed"})
        assert reopened._handle is not None
        reopened._handle.close()  # a reclaimed descriptor, an operator's stray close()
        healed = reopened.append("note", {"after": "writing resumes"})
        on_disk = hearse_path.read_text(encoding="utf-8").count("\n")
        add(
            "recovers when a descriptor was closed under it",
            healed.seq == 1 and not reopened.unwritten and on_disk == 2,
            f"{on_disk} line(s) on disk after the handle was closed and reopened",
        )
        reopened.close()

        # A write failure is produced for real, in a child process, with a real kernel
        # limit on file size: no privileges, no device files, and no mocked exception.
        probe_result = subprocess.run(
            [sys.executable, "-c", WRITE_FAILURE_PROBE, str(Path(__file__).resolve().parents[1])],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if probe_result.returncode != 0:
            add(
                "a failing write degrades the record visibly instead of silently",
                False,
                f"the probe itself failed ({probe_result.returncode}): "
                f"{(probe_result.stderr or '').strip()[-200:]}",
            )
        else:
            facts = json.loads(probe_result.stdout.strip().splitlines()[-1])
            add(
                "a failing write degrades the record visibly instead of silently",
                facts["unwritten"] > 0 and bool(facts["first_error"]) and facts["chain_valid"],
                f"{facts['unwritten']} event(s) recorded as unwritten after the filesystem "
                f"limit was hit; first: {facts['first_error']}; the in-memory chain stayed valid",
            )
            add(
                "closing a ledger on a failing filesystem does not raise",
                facts["closed_cleanly"] is True,
                f"close() returned normally with {facts['on_disk_bytes']} bytes on disk",
            )

        # -- 9. receipt construction and offline verification ---------------- #
        ledger8 = Ledger.open(workspace / "runs" / "receipt.jsonl", max_bytes=None)
        upstream2 = FakeUpstream(steps=[Step(model="claude-sonnet-5", input_tokens=1000, output_tokens=50)])
        uh, up = upstream2.start()
        proxy8 = RecordingProxy(
            ledger=ledger8,
            policy=policy,
            meter=CostMeter(price_table=table),
            session_id="receipt",
            upstreams={"openai": f"http://{uh}:{up}"},
            upstream_timeout=10,
        )
        h8, p8 = proxy8.start()
        _call(f"http://{h8}:{p8}", "/openai/v1/chat/completions")
        proxy8.stop()
        upstream2.stop()
        ledger8.append(ev.SESSION_END, {"exit_code": 0, "breached": None, "interrupted": False})
        receipt = build_receipt(
            events=list(ledger8),
            ledger=ledger8,
            meter=proxy8.meter,
            policy=policy,
            session_id="receipt",
            docket_number="DKT-20260101-SELF01",
            exit_code=0,
            duration_seconds=0.1,
            price_table=table,
            created_at="2026-01-01T00:00:00Z",
        )
        from .verify import verify_receipt

        result = verify_receipt(receipt, ledger_path=ledger8.path)
        add(
            "builds a receipt that verifies offline",
            result.ok,
            f"{sum(1 for c in result.checks if c.status == 'pass')} checks passed, "
            f"{len(result.failures)} failed"
            + (f": {result.failures[:2]}" if result.failures else ""),
        )
        tampered = json.loads(json.dumps(receipt))
        original_total = tampered["totals"]["cost_usd"]
        tampered["totals"]["cost_usd"] = "0.000001"
        verdict = verify_receipt(tampered)
        failed_names = [c.name for c in verdict.checks if c.status == "fail"]
        add(
            "detects a one-field edit to the receipt",
            not verdict.ok,
            f"changing totals.cost_usd from {original_total} to 0.000001 makes verification fail "
            f"{len(failed_names)} check(s): {', '.join(failed_names[:3]) or 'none'}",
        )
    finally:
        scripted.stop()
        failing.stop()
        ledger.close()
        if not keep_workspace:
            shutil.rmtree(workspace, ignore_errors=True)
    return probes, workspace


def render_probes(probes: list[Probe], *, color: bool = False) -> str:
    """Render probe results as a table with evidence, not just verdicts."""
    codes = {"pass": "PASS", "fail": "FAIL", "skipped": "SKIP"}
    if color:
        codes = {
            "pass": "\033[32mPASS\033[0m",
            "fail": "\033[31mFAIL\033[0m",
            "skipped": "\033[33mSKIP\033[0m",
        }
    lines = []
    width = max((len(p.name) for p in probes), default=10)
    for probe in probes:
        lines.append(f"  {codes[probe.status]}  {probe.name.ljust(width)}  {probe.detail}")
    passed = sum(1 for p in probes if p.status == "pass")
    failed = sum(1 for p in probes if p.status == "fail")
    skipped = sum(1 for p in probes if p.status == "skipped")
    lines.append("")
    lines.append(f"  {passed} passed, {failed} failed, {skipped} skipped")
    return "\n".join(lines)


def _gzip_bytes(payload: bytes) -> bytes:  # pragma: no cover - small helper
    return gzip.compress(payload)
