#!/usr/bin/env python3
"""Measure what Docket costs and how it behaves under sustained load — as a supervised run.

``docs/testing.md`` says two things are *not covered*: endurance (no run longer than minutes) and
throughput (no load curve). This tool is how those stop being gaps: it generates real load through
a real boundary, in the shape the boundary is actually used — a supervisor process, a boundary
inside it, a scripted provider behind it, and an agent child in front of it — and then reads the
evidence back and checks it against the client's own count.

It measures four things and refuses to measure a fifth:

1. **Throughput and client latency** — ``docket/loaddriver`` drives N calls across C threads and
   summarises the round trips with the receipt's own nearest-rank rule.
2. **The recorder's cost** — the supervisor process's RSS is sampled once a second for the whole
   run (``/proc/<pid>/status``), so a soak reports memory over time rather than memory at the end.
   The driver reports its own RSS separately; a process cannot honestly report another's.
3. **Post-load verifiability** — the receipt from a loaded run goes through ``docket verify``
   afterwards. A fast run whose evidence no longer verifies is a defect, not a result.
4. **Agreement between the two sides** — the client's successful-call count and the ledger's usage
   rows must match exactly, and every response row must carry a latency. This is the check that
   makes the report evidence: a load generator that counts its own successes while the recorder
   silently drops events would otherwise produce a flattering number.
5. **Not measured: the provider.** The upstream here is scripted and local, so these numbers are
   about Docket, never about a model API's latency or throughput; the report says so in words.

It is one machine, one process, two cores in the environment this repository was built in, and
those facts are printed with every result. Nothing here is a benchmark of anything but this code,
and a CI runner's number would be a fact about the runner — which is why CI runs the *tests* of
this tool and not the load point itself.

Exit codes: ``0`` the run is usable as evidence, ``1`` it is not (a dropped call, a broken chain,
a receipt that does not verify, counts that disagree), ``2`` the run could not be started.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SRC = ROOT / "src"

#: A cap high enough that the load test never breaches it: this run is measuring the recorder, and
#: a cap breach would end it early and produce exactly the number the tool is trying to avoid
#: reporting. The cap is still *set*, because a load test that runs with the guard off is a load
#: test of a different program.
MAX_USD = "1000000"


#: A process that has not finished mapping its interpreter reports a few KiB, and a report that
#: said "memory grew from 4 KiB to 27 MiB" would be describing the loader, not the recorder. A
#: reading below this floor is discarded rather than used as the start of a trend.
MIN_MAPPED_KIB = 4096


def _proc_status(pid: int, field: str) -> int:
    """One ``/proc/<pid>/status`` field in KiB, or -1 when it cannot be read.

    -1 rather than 0 matters: 0 would read as "measured, and the process used no memory", and a
    memory number is exactly the kind that must not be invented when the platform will not say.
    """
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith(field):
                    return int(line.split()[1])
    except OSError:
        return -1
    return -1


def _rss_kib(pid: int) -> int:
    """Current resident set size of ``pid``, in KiB."""
    return _proc_status(pid, "VmRSS:")


def _rss_peak_kib(pid: int) -> int:
    """The process's high-water mark (``VmHWM``): its peak RSS, which sampling cannot miss.

    Sampling once a second can walk straight past a spike. The kernel's own high-water mark
    cannot, so the peak in the report comes from there and the sampled series is used for the
    trend — two different questions, answered by the two instruments that can answer them.
    """
    return _proc_status(pid, "VmHWM:")


def _env() -> dict[str, str]:
    """The environment for every child: this tree's ``src`` first, on an absolute path.

    A relative ``PYTHONPATH=src`` is correct for a shell sitting in the repository and wrong for
    every process started somewhere else — which is every process this tool starts.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


class Failure(RuntimeError):
    """The run could not be started or produced no usable evidence."""


def start_upstream(work: Path, log: list[str]) -> tuple[subprocess.Popen[str], str]:
    """Start the scripted provider as a *service* — the same entry point a container runs.

    One step, which the provider then repeats: the point is a steady, known response, so that the
    variation in the numbers comes from Docket and not from the script.
    """
    process = subprocess.Popen(
        [sys.executable, "-m", "docket.fakeupstream", "--port", "0", "--steps", "1"],
        cwd=work,
        env=_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if process.stdout is None:  # pragma: no cover - PIPE given above
        process.kill()
        raise Failure("the scripted provider produced no stream to read")
    line = process.stdout.readline()
    if not line.strip():
        process.kill()
        raise Failure(f"the scripted provider printed nothing: {process.stderr.read() if process.stderr else ''}")
    log.append(f"provider: {line.strip()}")
    try:
        payload = json.loads(line)
    except ValueError as exc:
        process.kill()
        raise Failure(f"the scripted provider did not print JSON: {line!r}") from exc
    return process, payload["listening"]


def run_supervised(
    work: Path,
    upstream_url: str,
    drive: list[str],
    *,
    max_minutes: int,
    max_events: int,
    boundary_port: int,
    cache_hits: int | None,
    log: list[str],
) -> dict[str, Any]:
    """Run ``docket run`` with the load driver as the supervised child.

    The child is ``python -m docket.loaddriver``: it goes through ``DOCKET_PROXY_URL`` like any
    other agent, so the load is metered, capped and recorded by the same code path that records a
    real run. The supervisor's RSS is sampled while it runs, and the sample series is returned.
    """
    docket_dir = work / ".docket"
    report_path = work / "load-report.json"
    command = [
        sys.executable,
        "-m",
        "docket",
        "run",
        "--docket-dir",
        str(docket_dir),
        "--upstream",
        f"openai={upstream_url}",
        "--port",
        str(boundary_port),
        "--max-usd",
        MAX_USD,
        "--max-minutes",
        str(max_minutes),
        "--max-events",
        str(max_events),
        # The cache is off unless the operator asks for it, and when it is on the run is
        # a different measurement: the driver still makes N round trips, but only the
        # misses reach the provider, so the two sides of the ledger agree on a different
        # arithmetic (see `judge`).
        *(["--cache", "--cache-max-hits", str(cache_hits)] if cache_hits is not None else []),
        "--label",
        "loadtest",
        "--",
        sys.executable,
        "-m",
        "docket.loaddriver",
        *drive,
        "--report",
        str(report_path),
    ]
    log.append("run: " + " ".join(command))
    process = subprocess.Popen(
        command,
        cwd=work,
        env=_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    series: list[int] = []
    unmapped = 0
    peak = -1
    started = time.monotonic()
    while process.poll() is None:
        reading = _rss_kib(process.pid)
        # The high-water mark has to be read while the process is alive: `/proc/<pid>` is gone the
        # moment it exits, and VmHWM is monotonic, so the last reading before exit is the peak.
        high = _rss_peak_kib(process.pid)
        if high > peak:
            peak = high
        if reading < 0:
            pass  # the process exited between the poll and the read: nothing to record
        elif reading < MIN_MAPPED_KIB:
            unmapped += 1  # interpreter still starting; not a memory measurement
        else:
            series.append(reading)
        time.sleep(1.0)
    output = process.stdout.read() if process.stdout else ""
    elapsed = time.monotonic() - started
    log.append(f"run: exit {process.returncode} after {elapsed:.1f}s, {len(series)} RSS samples")
    if not report_path.exists():
        raise Failure(
            "the load driver wrote no report, so the run produced no usable measurement. "
            f"Supervisor output:\n{output[-2000:]}"
        )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    return {
        "exit_code": process.returncode,
        "seconds": f"{elapsed:.3f}",
        "supervisor_rss_kib": {
            "samples": len(series),
            "discarded_while_starting": unmapped,
            "start": series[0] if series else -1,
            "end": series[-1] if series else -1,
            "max_sampled": max(series) if series else -1,
            "peak_kib": peak,
        },
        "driver": report,
        "output_tail": output[-4000:],
        "docket_dir": str(docket_dir),
    }


def read_evidence(docket_dir: Path, log: list[str]) -> dict[str, Any]:
    """Read the receipt, count the ledger, and run the project's own verifier over the result.

    The counts come from the files the run wrote, not from the driver's report, so the comparison
    later is between two independent sources: what the client thinks it did, and what the recorder
    says happened.
    """
    receipts = sorted((docket_dir / "receipts").glob("*.json"))
    if not receipts:
        raise Failure("the run wrote no receipt")
    receipt_path = receipts[-1]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    # The receipt names the ledger it came from by sha256. Picking the file by that digest rather
    # than by "the only .jsonl in the directory" is the difference between a check and a guess:
    # a ledger that does not hash to the receipt's claim means this run's evidence is not a set.
    claimed = receipt["ledger"]["file_sha256"]
    ledger_path = None
    for candidate in sorted((docket_dir / "runs").glob("*.jsonl")):
        if hashlib.sha256(candidate.read_bytes()).hexdigest() == claimed:
            ledger_path = candidate
            break
    if ledger_path is None:
        raise Failure(f"no ledger in {docket_dir / 'runs'} hashes to the receipt's file_sha256 {claimed[:16]}…")
    rows = [
        json.loads(line)
        for line in ledger_path.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("{")
    ]
    responses = [row for row in rows if row.get("type") == "model.response"]
    cache_hits = [row for row in rows if row.get("type") == "model.cache_hit"]
    verify = subprocess.run(
        [sys.executable, "-m", "docket", "verify", str(receipt_path), "--json"],
        cwd=ROOT,
        env=_env(),
        capture_output=True,
        text=True,
    )
    try:
        verdict = json.loads(verify.stdout)
    except ValueError as exc:
        raise Failure(f"`docket verify` did not print JSON (exit {verify.returncode}): {verify.stdout[:500]}") from exc
    log.append(f"verify: exit {verify.returncode}, ok={verdict.get('ok')}, receipt {receipt_path.name}")
    return {
        "receipt_path": str(receipt_path),
        "ledger_path": str(ledger_path),
        "receipt": {
            "session": receipt.get("session", {}).get("session_id"),
            "events": receipt.get("ledger", {}).get("events"),
            "latency": receipt.get("latency"),
            # The cache block travels with the projection: `judge` compares the hits it
            # claims against the hits the ledger holds, and a projection that dropped it
            # would turn that comparison into "the receipt claims None" — which is how
            # this member came to be added here at all.
            "cache": receipt.get("cache"),
        },
        "ledger": {
            "lines": len(rows),
            "model_responses": len(responses),
            "cache_hits": len(cache_hits),
            "responses_with_latency": sum(
                1
                for row in responses
                if isinstance(row.get("data"), dict) and row["data"].get("latency_ms") is not None
            ),
        },
        "verify": {
            "ok": bool(verdict.get("ok")),
            "passed": sum(1 for check in verdict.get("checks", []) if check.get("status") == "pass"),
            "failed": sum(1 for check in verdict.get("checks", []) if check.get("status") == "fail"),
            "skipped": sum(1 for check in verdict.get("checks", []) if check.get("status") == "skip"),
        },
    }


def judge(run: dict[str, Any], evidence: dict[str, Any]) -> list[str]:
    """Every reason this run is *not* usable as evidence. An empty list is the only good outcome.

    Each check names the two sources it compares, because "the load ran fine" is exactly the kind
    of claim this tool exists to replace with something checkable.
    """
    problems: list[str] = []
    driver, ledger, verify = run["driver"], evidence["ledger"], evidence["verify"]
    hits = ledger.get("cache_hits", 0)
    if driver["calls_error"]:
        problems.append(f"the client reported {driver['calls_error']} errored call(s); first: {driver['first_error']}")
    # A cache changes what the two sides are expected to agree *on*, not whether they
    # agree. Without one, every answered call is a metered response. With one, a call
    # the cache answered is a `model.cache_hit` and no response at all, so the sum is
    # what has to match — and the receipt's own saving figure is checked against the
    # hits the ledger counts rather than believed.
    if driver["calls_ok"] != ledger["model_responses"] + hits:
        problems.append(
            f"the client counted {driver['calls_ok']} answered calls and the ledger records "
            f"{ledger['model_responses']} model responses plus {hits} cache hit(s) — the two sides disagree"
        )
    stated_cache = evidence["receipt"].get("cache") or {}
    if hits and stated_cache.get("hits") != hits:
        problems.append(
            f"the ledger records {hits} cache hit(s) and the receipt claims "
            f"{stated_cache.get('hits')!r} — the receipt does not describe this ledger"
        )
    if ledger["responses_with_latency"] != ledger["model_responses"]:
        problems.append(
            f"{ledger['model_responses'] - ledger['responses_with_latency']} response row(s) carry no "
            "latency_ms, so the receipt's latency summary does not cover every call"
        )
    stated = evidence["receipt"].get("latency") or {}
    # Latency is measured around *metered* calls, so the count it must cover is the
    # responses the ledger records — with or without a cache. The client still measures
    # every round trip it makes (including hits, which are faster by construction), and
    # that difference is exactly what a reader should not mistake for a provider speedup.
    if stated.get("measured_calls") != ledger["model_responses"]:
        problems.append(
            f"the receipt's latency summary covers {stated.get('measured_calls')} call(s) and the ledger "
            f"records {ledger['model_responses']} metered response(s)"
        )
    if not verify["ok"]:
        problems.append("the receipt from this run does not verify")
    if driver["calls_refused"] and driver["calls_ok"] + driver["calls_refused"] != driver["attempts_issued"]:
        problems.append("refusals and successes do not add up to the attempts issued")
    return problems


def build_report(run: dict[str, Any], evidence: dict[str, Any], problems: list[str], argv: list[str]) -> dict[str, Any]:
    """The combined report: the machine, the load, the recorder, the evidence, the verdict."""
    driver = run["driver"]
    return {
        "host": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
            "note": "one machine, one supervisor process; not a benchmark of the provider or of any other host",
        },
        "command": " ".join(argv),
        "load": {
            "concurrency": driver["concurrency"],
            "calls_requested": driver["calls_requested"],
            "attempts_issued": driver["attempts_issued"],
            "calls_ok": driver["calls_ok"],
            "calls_refused": driver["calls_refused"],
            "calls_error": driver["calls_error"],
            "refused_codes": driver["refused_codes"],
            "seconds": driver["seconds"],
            "achieved_calls_per_second": driver["achieved_calls_per_second"],
            "target_rate_per_second": driver["target_rate_per_second"],
            "client_latency": driver["client_latency"],
        },
        "recorder": {
            "supervisor_exit_code": run["exit_code"],
            "cache": evidence["receipt"].get("cache"),
            # The paths are part of the report on purpose: these numbers describe files, and a
            # reader who cannot open them has to take this document's word for everything.
            "receipt_path": evidence["receipt_path"],
            "ledger_path": evidence["ledger_path"],
            "supervisor_rss_kib": run["supervisor_rss_kib"],
            "driver_rss_kib": driver["driver_rss_kib"],
            "ledger": evidence["ledger"],
            "receipt": evidence["receipt"],
        },
        "verification": evidence["verify"],
        "problems": problems,
        "usable_as_evidence": not problems,
    }


def _rss_cell(rss: dict[str, int]) -> str:
    """The memory cell, or an explicit refusal to state one.

    A one-sample run cannot show a trend, and a run whose process never mapped enough memory to
    measure has no memory result at all. Both cases say so instead of printing a number that looks
    like a measurement.
    """
    if rss["samples"] < 2:
        return f"not measured (the run was too short for a trend: {rss['samples']} usable sample(s))"
    growth = rss["end"] - rss["start"]
    peak = f", peak {rss['peak_kib']} KiB by `VmHWM`" if rss["peak_kib"] >= 0 else ""
    return f"start {rss['start']} KiB → end {rss['end']} KiB over {rss['samples']} samples " f"({growth:+d} KiB){peak}"


def markdown(report: dict[str, Any]) -> str:
    """The same numbers as a table someone can paste into a document, with its own caveats."""
    load, recorder, verify = report["load"], report["recorder"], report["verification"]
    latency, rss = load["client_latency"], recorder["supervisor_rss_kib"]
    lines = [
        "| measurement | value | source |",
        "| --- | --- | --- |",
        f"| calls answered | {load['calls_ok']} | client, and {recorder['ledger']['model_responses']} "
        f"model.response rows in the ledger |",
        "| calls refused | "
        + str(load["calls_refused"])
        + (f" {load['refused_codes']}" if load["refused_codes"] else "")
        + " | client, from the boundary's `code` |",
        f"| calls errored | {load['calls_error']} | client |",
        f"| concurrency | {load['concurrency']} threads, one process | the driver's own argv |",
        f"| wall clock | {load['seconds']} s | the orchestrator's clock |",
        f"| throughput | {load['achieved_calls_per_second']} calls/s | calls answered ÷ wall clock |",
        f"| client latency p50 / p95 / p99 | {latency['p50_ms']} / {latency['p95_ms']} / {latency['p99_ms']} ms "
        f"(max {latency['max_ms']} ms) | nearest-rank over {latency['measured_calls']} samples |",
        "| supervisor RSS | " + _rss_cell(rss) + " | `/proc/<pid>/status`, once a second, plus `VmHWM` |",
        *(
            [
                f"| answered from cache | {cache['hits']} call(s), {cache['saved_usd']} not spent "
                f"({cache['avoided_tokens']} tokens avoided) | the receipt's `cache` member, recomputed "
                f"from the origin calls it replays |"
            ]
            if isinstance(cache := report["recorder"].get("cache"), dict)
            else []
        ),
        f"| receipt verifies afterwards | {'yes' if verify['ok'] else 'NO'} "
        f"({verify['passed']} pass / {verify['failed']} fail / {verify['skipped']} skip) | `docket verify --json` |",
        "",
        f"Host: Python {report['host']['python']} on {report['host']['platform']}, "
        f"{report['host']['cpu_count']} cpus. {report['host']['note']}",
    ]
    if report["problems"]:
        lines.append("")
        lines.append("Problems (this run is not usable as evidence):")
        lines.extend(f"* {problem}" for problem in report["problems"])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tools/loadtest.py",
        description="Measure throughput, client latency and memory under load, through a real boundary.",
    )
    parser.add_argument("--calls", type=int, default=2000, help="calls to attempt (default 2000)")
    parser.add_argument("--concurrency", type=int, default=8, help="driving threads (default 8)")
    parser.add_argument("--seconds", type=float, default=0.0, help="also stop after this long (soak mode)")
    parser.add_argument("--rate", type=float, default=0.0, help="target calls/second across all threads")
    parser.add_argument("--boundary-port", type=int, default=0, help="fix the boundary's port (0 = any free port)")
    parser.add_argument("--report", type=Path, help="write the combined JSON report here")
    parser.add_argument("--markdown", type=Path, help="write the markdown table here")
    parser.add_argument("--work-dir", type=Path, help="run here instead of a temporary directory")
    parser.add_argument("--json", action="store_true", help="print the report and nothing else")
    parser.add_argument(
        "--cache-hits",
        type=int,
        metavar="N",
        help="run with the response cache on, capped at N answered calls (N > 0). Every call the "
        "driver makes still crosses the boundary; only the misses reach the provider, so the "
        "report's throughput is a statement about the cache (see docs/caching.md)",
    )
    args = parser.parse_args(argv)

    if args.calls < 1 or args.concurrency < 1:
        print("loadtest: --calls and --concurrency must be at least 1", file=sys.stderr)
        return 2
    if args.cache_hits is not None and args.cache_hits < 1:
        print(
            "loadtest: --cache-hits must be at least 1 (0 would configure a cache that never answers: "
            "leave the flag off instead)",
            file=sys.stderr,
        )
        return 2
    work = Path(args.work_dir) if args.work_dir else Path(tempfile.mkdtemp(prefix="docket-loadtest-"))
    work.mkdir(parents=True, exist_ok=True)
    log: list[str] = []
    upstream: subprocess.Popen[str] | None = None
    try:
        upstream, upstream_url = start_upstream(work, log)
        drive = [
            "--calls",
            str(args.calls),
            "--concurrency",
            str(args.concurrency),
        ]
        if args.seconds:
            drive += ["--seconds", str(args.seconds)]
        if args.rate:
            drive += ["--rate", str(args.rate)]
        # The supervisor's own caps must outlast the load, or the run ends mid-measurement and the
        # report would describe a truncated run as if it were complete. `max_events` is not
        # "unlimited": every call writes a request and a response, so twice the call budget plus a
        # margin is the ceiling, and a run that needs more is a run that is doing something else.
        expected_seconds = args.seconds or max(30.0, args.calls / max(args.concurrency, 1) * 2)
        minutes = max(1, int(expected_seconds / 60) + 1)
        run = run_supervised(
            work,
            upstream_url,
            drive,
            max_minutes=minutes,
            max_events=args.calls * 2 + 1000,
            boundary_port=args.boundary_port,
            cache_hits=args.cache_hits,
            log=log,
        )
        evidence = read_evidence(Path(run["docket_dir"]), log)
        problems = judge(run, evidence)
        report = build_report(run, evidence, problems, sys.argv[1:] if argv is None else argv)
    except Failure as exc:
        print(f"loadtest: {exc}", file=sys.stderr)
        for line in log:
            print(f"loadtest: {line}", file=sys.stderr)
        return 2
    finally:
        if upstream is not None:
            upstream.send_signal(signal.SIGTERM)
            try:
                upstream.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - a provider that ignores SIGTERM
                upstream.kill()
        if not args.work_dir and not report_is_kept(args.report):
            shutil.rmtree(work, ignore_errors=True)

    table = markdown(report)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(table, encoding="utf-8")
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(table, end="")
        if work.exists():
            print(f"\nwork dir: {work}  (ledger and receipt are in {work / '.docket'})")
        else:
            print("\nwork dir: removed (pass --report or --work-dir to keep the ledger and receipt)")
    for line in log:
        print(f"loadtest: {line}", file=sys.stderr)
    return 0 if report["usable_as_evidence"] else 1


def report_is_kept(report: Path | None) -> bool:
    """The working tree is kept whenever a report was requested: a report with no run behind it is
    a claim nobody can re-check, and these runs leave a ledger and a receipt in that directory."""
    return report is not None


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())
