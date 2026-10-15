"""A load generator that goes *through* the boundary, so the load is evidence too.

This is the piece that turns "we have not measured throughput or endurance" into a number, and
the only way to make such a number mean anything here is to generate the load the same way a
real agent does: as a supervised child process, through ``DOCKET_PROXY_URL``, so every call is
metered, capped, recorded and then verifiable after the fact. A load generator that talks to the
provider directly would measure the provider, not the thing under test.

What it measures, and what it refuses to measure:

* **client latency** — the round trip from this process into the boundary and back out, in whole
  milliseconds, summarised with the *same* nearest-rank rule the receipt uses
  (:func:`docket.latency.percentile_ms`), so a report and a receipt cannot disagree about what
  "p99" means.
* **calls, refusals and errors, counted separately.** A refusal (a cap or the rate limiter
  answering with a ``code``) is the boundary working; an error is the boundary or the provider not
  working. Adding them into one "failure" number would hide which of the two happened.
* **this process's own RSS over time** — the agent's memory, sampled from ``/proc/self/status``.
  The boundary's memory is the *supervisor's*, and that is sampled by the caller
  (``tools/loadtest.py``), because a process cannot honestly report another process's growth.

It refuses to run when ``DOCKET_PROXY_URL`` is unset: outside a supervised run there is no
boundary, and a driver that silently falls back to a provider URL would generate traffic that is
not evidence. Every error is reported, with the first one quoted in full, because a count without
a cause is not a finding.

The report is JSON, written where ``--report`` says, and echoed as a single line on stdout so the
supervisor's log carries it too.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from http.client import HTTPConnection
from typing import Any
from urllib.parse import urlsplit

from .latency import percentile_ms
from .policy import denial_code

__all__ = ["Report", "run_load", "main"]

#: The provider route to drive. The boundary stamps usage and cost on this path regardless of the
#: model named in the body, and the scripted provider answers any model, so a load test needs no
#: real credentials and no network.
DEFAULT_ROUTE = "/openai/v1/chat/completions"

#: A prompt small enough that the boundary and not the provider is the bottleneck, which is the
#: honest target: this is a test of the recorder, not of a model API.
BODY = {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "load"}]}


def _rss_kib() -> int:
    """This process's resident set size in KiB, or -1 when the platform will not say.

    ``/proc`` is Linux-only. Returning -1 rather than 0 matters: 0 would read as "measured, and
    the process used no memory", and a memory claim is exactly the kind of number that must not be
    invented when the platform cannot supply it.
    """
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:  # pragma: no cover - non-Linux
        return -1
    return -1  # pragma: no cover - /proc without VmRSS


class Report:
    """The counters, guarded by one lock. Every thread updates this and nothing else."""

    def __init__(self, concurrency: int, target_rate: float, seconds: float, calls: int) -> None:
        self.lock = threading.Lock()
        self.ok = 0
        self.refused = 0
        self.errors = 0
        self.refused_codes: dict[str, int] = {}
        self.latencies: list[int] = []
        self.bytes_read = 0
        self.issued = 0  # attempts scheduled, so a run can stop on a total, not only on successes
        self.first_error: str | None = None
        self.first_error_status: int | None = None
        self.first_refusal: dict[str, Any] | None = None
        self.concurrency = concurrency
        self.target_rate = target_rate
        self.seconds = seconds
        self.calls = calls
        self.rss_start_kib = _rss_kib()
        self.rss_end_kib = self.rss_start_kib
        self.rss_max_kib = self.rss_start_kib
        self.rss_samples = 0
        # Endurance mode: with a rate set, a soak keeps the load on for the whole window instead
        # of finishing a burst in two seconds and calling that endurance.
        self.deadline = time.monotonic() + seconds if seconds else 0.0

    def note_rss(self) -> None:
        with self.lock:
            current = _rss_kib()
            if current < 0:
                return
            if self.rss_samples == 0:
                self.rss_start_kib = current
            self.rss_end_kib = current
            self.rss_max_kib = max(self.rss_max_kib, current)
            self.rss_samples += 1

    def next_attempt(self) -> bool:
        """True if this thread may issue one more call. Shared by every thread."""
        with self.lock:
            if self.issued >= self.calls:
                return False
            if self.seconds and time.monotonic() >= self.deadline:
                return False
            self.issued += 1
            return True

    def record_ok(self, latency_ms: int, size: int) -> None:
        with self.lock:
            self.ok += 1
            self.latencies.append(latency_ms)
            self.bytes_read += size

    def record_refused(self, code: str, body: dict[str, Any] | None) -> None:
        with self.lock:
            self.refused += 1
            self.refused_codes[code] = self.refused_codes.get(code, 0) + 1
            if self.first_refusal is None:
                self.first_refusal = {"code": code, "body": body}

    def record_error(self, status: int | None, message: str) -> None:
        with self.lock:
            self.errors += 1
            if self.first_error is None:
                self.first_error = message
                self.first_error_status = status


def _one_call(report: Report, connection: HTTPConnection, route: str, timeout: float) -> None:
    """One request, classified. Never raises: a failure here is data, not a crash."""
    started = time.monotonic()
    try:
        connection.request("POST", route, body=json.dumps(BODY), headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        payload = response.read()
        status = response.status
    except Exception as exc:  # noqa: BLE001 - a broken socket is a result, and it is quoted below
        # The connection is in an unknown state after a transport failure; the next call opens a
        # new one rather than reusing a socket that may hold half a request.
        connection.close()
        report.record_error(None, f"{type(exc).__name__}: {exc}")
        return
    elapsed_ms = int(round((time.monotonic() - started) * 1000))
    body: dict[str, Any] | None
    try:
        parsed = json.loads(payload.decode("utf-8", "replace"))
        body = parsed if isinstance(parsed, dict) else None
    except ValueError:
        body = None
    if 200 <= status < 300:
        report.record_ok(elapsed_ms, len(payload))
        return
    code = denial_code(payload)
    if code:
        # A refusal is the boundary enforcing something, and it always names what. `HTTP 403` from
        # a cap and `HTTP 429` from the limiter both land here; a relayed *provider* error does
        # not, which is the distinction the shared reader exists to make.
        report.record_refused(code, body)
        return
    report.record_error(
        status,
        f"HTTP {status} with no refusal code: {payload[:200].decode('utf-8', 'replace')}",
    )


def _parts(base: str) -> tuple[str, int, str]:
    """``(host, port, path prefix)`` for the boundary URL, refusing anything that is not http.

    The driver is handed ``DOCKET_PROXY_URL`` by the supervisor, and on this path the value is
    trusted input — but "trusted" is not a reason to guess, and a URL the driver cannot parse must
    stop the run rather than quietly address something else.
    """
    split = urlsplit(base)
    if split.scheme != "http" or not split.hostname or split.port is None:
        raise ValueError(f"not an http boundary URL: {base!r}")
    return split.hostname, split.port, split.path.rstrip("/")


def _worker(report: Report, base: str, route: str, timeout: float, pace: float) -> None:
    host, port, prefix = _parts(base)
    connection = HTTPConnection(host, port, timeout=timeout)
    route = prefix + route
    next_call = time.monotonic()
    try:
        while report.next_attempt():
            if pace:
                now = time.monotonic()
                if next_call > now:
                    time.sleep(next_call - now)
                next_call = max(next_call + pace, time.monotonic())
            _one_call(report, connection, route, timeout)
    finally:
        connection.close()


def run_load(
    base: str,
    *,
    calls: int = 2000,
    concurrency: int = 8,
    seconds: float = 0.0,
    rate: float = 0.0,
    route: str = DEFAULT_ROUTE,
    timeout: float = 30.0,
    sample_every: float = 1.0,
) -> dict[str, Any]:
    """Drive ``calls`` requests at ``concurrency`` threads and return the report as a dict.

    ``rate`` is a target in calls per second across the whole driver (0 means "as fast as the
    boundary will answer"). ``seconds`` bounds the run in wall-clock time as well as ``calls``,
    which is what makes an endurance run an endurance run: with a rate set, the driver keeps the
    load on for the whole window instead of finishing a burst in two seconds and calling it a soak.
    """
    if calls < 1:
        raise ValueError("--calls must be at least 1")
    if concurrency < 1:
        raise ValueError("--concurrency must be at least 1")
    report = Report(concurrency, rate, seconds, calls)
    # `rate` is the target for the whole driver, so each worker's share is smaller as concurrency
    # grows; pacing per worker against the total would multiply the load by the worker count.
    pace = concurrency / rate if rate else 0.0
    started = time.monotonic()
    threads = [
        threading.Thread(target=_worker, args=(report, base, route, timeout, pace), name=f"load-{i}")
        for i in range(concurrency)
    ]
    for thread in threads:
        thread.start()
    # Sample on a one-second cadence, but poll for the workers in between: sleeping the full
    # interval when the last call finished 20 ms into it would add up to a second of dead time to
    # every short run, and the wall clock in the report is supposed to be the load's, not the
    # sampler's. The cadence of the samples themselves is unchanged, so a soak's series is the
    # same shape as before.
    while any(thread.is_alive() for thread in threads):
        report.note_rss()
        due = time.monotonic() + sample_every
        while time.monotonic() < due and any(thread.is_alive() for thread in threads):
            time.sleep(min(0.02, sample_every))
    for thread in threads:
        thread.join()
    report.note_rss()
    elapsed = time.monotonic() - started
    return _summarise(report, base, route, elapsed)


def _summarise(report: Report, base: str, route: str, elapsed: float) -> dict[str, Any]:
    """The report. Counts are integers; every rate is a fixed-precision string, never a float.

    The integer/string split is the same rule the ledger enforces, applied here because these
    numbers end up in a document people quote: ``600.000001`` and ``600.0`` are the same fact, and
    a fact should be written the same way twice.
    """
    samples = sorted(report.latencies)
    summary: dict[str, Any] = {
        "target": base,
        "route": route,
        "concurrency": report.concurrency,
        "calls_requested": report.calls,
        "attempts_issued": report.issued,
        "calls_ok": report.ok,
        "calls_refused": report.refused,
        "calls_error": report.errors,
        "refused_codes": dict(sorted(report.refused_codes.items())),
        "bytes_read": report.bytes_read,
        "seconds": f"{elapsed:.3f}",
        "achieved_calls_per_second": f"{report.ok / elapsed:.2f}" if elapsed > 0 else "0.00",
        "target_rate_per_second": f"{report.target_rate:.2f}",
        "client_latency": {
            "method": "nearest-rank",
            "measured_calls": len(samples),
            "total_ms": sum(samples),
            "max_ms": samples[-1] if samples else 0,
            "p50_ms": percentile_ms(samples, 50) if samples else 0,
            "p95_ms": percentile_ms(samples, 95) if samples else 0,
            "p99_ms": percentile_ms(samples, 99) if samples else 0,
        },
        "driver_rss_kib": {
            "start": report.rss_start_kib,
            "end": report.rss_end_kib,
            "max": report.rss_max_kib,
            "samples": report.rss_samples,
        },
        "first_refusal": report.first_refusal,
        "first_error_status": report.first_error_status,
        "first_error": report.first_error,
    }
    return summary


def main(argv: list[str] | None = None) -> int:
    """Entry point. Exits 0 when every call was answered, 3 when one was not.

    A non-zero exit is load the *supervisor* sees as a failed run, which is deliberate: a load
    test that dropped calls must not leave a receipt that reads like a clean run.
    """
    parser = argparse.ArgumentParser(prog="python -m docket.loaddriver", description=__doc__.split("\n")[0])
    parser.add_argument("--calls", type=int, default=2000, help="total calls to attempt (default 2000)")
    parser.add_argument("--concurrency", type=int, default=8, help="parallel workers (default 8)")
    parser.add_argument("--seconds", type=float, default=0.0, help="also stop after this long (0 = calls only)")
    parser.add_argument("--rate", type=float, default=0.0, help="target calls/second across all workers (0 = flat out)")
    parser.add_argument("--route", default=DEFAULT_ROUTE, help="provider route to drive")
    parser.add_argument("--timeout", type=float, default=30.0, help="per-request timeout in seconds")
    parser.add_argument("--report", help="write the JSON report here")
    args = parser.parse_args(argv)

    base = os.environ.get("DOCKET_PROXY_URL")
    if not base:
        print(
            "loaddriver: DOCKET_PROXY_URL is not set, so there is no boundary to drive. This "
            "driver only generates evidence when a supervisor started it: run it with "
            "`docket run -- python3 -m docket.loaddriver ...` (or `make load`).",
            file=sys.stderr,
        )
        return 2
    report = run_load(
        base,
        calls=args.calls,
        concurrency=args.concurrency,
        seconds=args.seconds,
        rate=args.rate,
        route=args.route,
        timeout=args.timeout,
    )
    text = json.dumps(report, sort_keys=True)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    print(text)
    if report["calls_error"]:
        print(f"loaddriver: {report['calls_error']} call(s) errored; first: {report['first_error']}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())
