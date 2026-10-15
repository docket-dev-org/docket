"""The load measurement: the driver's counters, and the orchestrator's judgement of them.

Two things are being tested here, and they are different in kind. The first is that the driver
*counts what happened* — that a refusal is not folded into an error, that a call is not lost, and
that the ledger the boundary wrote agrees with the client's own count. The second is that the
orchestrator **refuses to call a run usable** when those two sides disagree, because a load tool
whose output cannot be wrong is a marketing instrument rather than a measurement.

The orchestrator test runs the tool as a subprocess on a handful of calls: it is slow by this
suite's standards (a couple of seconds) and it is the only test that exercises the whole path —
provider service, supervisor, boundary, driver, ledger, receipt, `docket verify`, report.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest

from docket.fakeupstream import Step
from docket.loaddriver import main as driver_main
from docket.loaddriver import run_load
from docket.policy import Caps, Policy
from docket.pricing import CostMeter, PriceTable
from docket.proxy import RateLimiter, RecordingProxy

REPO = Path(__file__).resolve().parents[1]


def _load_tool() -> ModuleType:
    """Import ``tools/loadtest.py`` by path.

    Registering the module in ``sys.modules`` *before* executing it is load-bearing: a module
    executed by path that defines a dataclass dies inside ``dataclasses`` with a message that
    names nothing useful if Python cannot find it in the module table.
    """
    spec = importlib.util.spec_from_file_location("loadtest_tool", REPO / "tools" / "loadtest.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["loadtest_tool"] = module
    spec.loader.exec_module(module)
    return module


loadtest_tool = _load_tool()


def _boundary(ledger, upstream_url, policy=None, **kwargs):
    meter = CostMeter(price_table=PriceTable.load())
    proxy = RecordingProxy(
        ledger=ledger,
        policy=policy or Policy(),
        meter=meter,
        session_id="loadtest",
        upstreams={"openai": upstream_url},
        **kwargs,
    )
    host, port = proxy.start()
    return proxy, f"http://{host}:{port}"


# -- the driver ------------------------------------------------------------- #


def test_every_call_is_counted_and_the_ledger_agrees(ledger, fake_upstream):
    """The client's successes and the recorder's rows are two independent counts of one event."""
    proxy, base = _boundary(ledger, fake_upstream.url)
    try:
        report = run_load(base, calls=24, concurrency=4)
    finally:
        proxy.stop()
    assert report["calls_ok"] == 24
    assert report["calls_error"] == 0
    assert report["calls_refused"] == 0
    responses = [event for event in ledger if event.type == "model.response"]
    assert len(responses) == 24
    assert all(event.data.get("latency_ms") is not None for event in responses)
    assert report["client_latency"]["measured_calls"] == 24


def test_a_refusal_is_counted_as_a_refusal_and_named(ledger, fake_upstream):
    """A cap answering is the boundary working. Folding it into "errors" would hide which happened."""
    policy = Policy(caps=Caps(max_model_calls=3, on_breach="deny"))
    proxy, base = _boundary(ledger, fake_upstream.url, policy)
    try:
        report = run_load(base, calls=10, concurrency=1)
    finally:
        proxy.stop()
    assert report["calls_refused"] >= 1
    assert report["calls_error"] == 0, report["first_error"]
    assert report["refused_codes"], "a refusal must name the code the boundary sent"
    assert "max_model_calls" in report["refused_codes"]
    assert report["first_refusal"]["code"] in report["refused_codes"]
    assert report["calls_ok"] + report["calls_refused"] == report["attempts_issued"]


def test_the_rate_limiter_is_a_refusal_too(ledger, fake_upstream):
    """The limiter and a cap are different mechanisms answering the same question; both are data."""
    # The bucket starts full (one second of steady traffic), so the first call is allowed and the
    # rest arrive long before the next token does. A `Decimal` rate is deliberate: it is what the
    # policy loader produces, and it used to raise inside the request instead of here.
    proxy, base = _boundary(ledger, fake_upstream.url, limiter=RateLimiter(per_second=Decimal("0.5"), burst=1))
    try:
        report = run_load(base, calls=6, concurrency=1)
    finally:
        proxy.stop()
    assert report["calls_ok"] == 1
    assert report["calls_refused"] == 5
    assert report["calls_error"] == 0, report["first_error"]
    assert report["refused_codes"] == {"rate_limited": 5}
    assert report["first_refusal"]["code"] == "rate_limited"


def test_a_limiter_with_room_does_not_refuse_anything(ledger, fake_upstream):
    """The other half of the same contract: headroom means no refusals, so the test above is
    measuring the limit and not a permanently broken limiter."""
    proxy, base = _boundary(ledger, fake_upstream.url, limiter=RateLimiter(per_second=Decimal("1000"), burst=1000))
    try:
        report = run_load(base, calls=10, concurrency=1)
    finally:
        proxy.stop()
    assert report["calls_ok"] == 10
    assert report["calls_refused"] == 0


def test_the_latency_summary_obeys_the_receipt_s_rule(ledger, fake_upstream):
    """Same method, same rule, same ordering: a report and a receipt must not disagree on `p99`."""
    proxy, base = _boundary(ledger, fake_upstream.url)
    try:
        report = run_load(base, calls=12, concurrency=3)
    finally:
        proxy.stop()
    latency = report["client_latency"]
    assert latency["method"] == "nearest-rank"
    assert latency["measured_calls"] == 12
    assert 0 <= latency["p50_ms"] <= latency["p95_ms"] <= latency["p99_ms"] <= latency["max_ms"]
    assert latency["total_ms"] >= latency["max_ms"] > 0


def test_seconds_bounds_a_soak_mode_run(ledger, fake_upstream):
    """An endurance run is bounded by time, not by a call count it would exhaust in two seconds."""
    proxy, base = _boundary(ledger, fake_upstream.url)
    started = time.monotonic()
    try:
        report = run_load(base, calls=100_000, concurrency=1, seconds=0.5, rate=20.0)
    finally:
        proxy.stop()
    elapsed = time.monotonic() - started
    assert elapsed < 10, f"a 0.5 s window took {elapsed:.1f} s"
    assert 0 < report["attempts_issued"] < 100
    assert report["target_rate_per_second"] == "20.00"


def test_a_report_from_a_run_with_no_boundary_is_refused(monkeypatch, capsys):
    """Outside a supervised run there is no boundary, and traffic that is not recorded is not data."""
    monkeypatch.delenv("DOCKET_PROXY_URL", raising=False)
    assert driver_main(["--calls", "1"]) == 2
    assert "DOCKET_PROXY_URL" in capsys.readouterr().err


def test_a_url_the_driver_cannot_address_stops_the_run():
    """Guessing what an unparsable URL meant is how a load test ends up measuring something else."""
    from docket.loaddriver import _parts

    with pytest.raises(ValueError, match="not an http boundary URL"):
        _parts("https://proxy.example/")
    with pytest.raises(ValueError, match="not an http boundary URL"):
        _parts("http://127.0.0.1")  # no port: the boundary always has one


def test_a_transport_failure_is_reported_not_swallowed(ledger, fake_upstream, tmp_path):
    """A socket that dies mid-run must produce an error count and a quoted cause."""
    proxy, base = _boundary(ledger, fake_upstream.url)
    try:
        first = run_load(base, calls=2, concurrency=1)
        assert first["calls_error"] == 0
    finally:
        proxy.stop()
    # Now nothing is listening where the boundary was: the next driver run must fail loudly.
    dead = run_load(base, calls=2, concurrency=1, timeout=2.0)
    assert dead["calls_error"] == 2
    assert dead["first_error"], "an error count without a cause is not a finding"
    assert dead["calls_ok"] == 0


def test_a_provider_error_is_an_error_not_a_refusal(ledger):
    """The distinction the shared reader exists for, and a defect it fixed.

    The boundary relays upstream error bodies faithfully — that is the honest thing to do — and
    providers put their own codes in ``error.code`` (OpenAI's ``invalid_api_key``, for instance).
    The reference agent used to read *any* ``error.code`` as a Docket refusal, so a provider that
    rejected the key would have been reported as the boundary enforcing a limit: the agent would
    have backed off politely instead of surfacing a broken credential. A refusal is identified by
    the envelope's ``type``, and nothing else.
    """
    from docket.fakeupstream import FakeUpstream

    provider_error = {"error": {"code": "invalid_api_key", "message": "bad key", "type": "invalid_request_error"}}
    server = FakeUpstream(steps=[Step(status=401, body_override=provider_error)])
    server.start()
    proxy, base = _boundary(ledger, server.url)
    try:
        report = run_load(base, calls=3, concurrency=1)
    finally:
        proxy.stop()
        server.stop()
    assert report["calls_refused"] == 0, "a provider's own error code is not a Docket refusal"
    assert report["calls_error"] == 3
    assert report["first_error_status"] == 401
    assert "invalid_api_key" in (report["first_error"] or ""), report["first_error"]


def test_a_boundary_refusal_is_a_refusal_and_carries_the_boundary_s_own_type(ledger, fake_upstream):
    """The other half: the envelope the boundary sends is recognised, and its code is reported."""
    from docket.policy import DENIAL_TYPE, denial_code

    policy = Policy(caps=Caps(max_model_calls=1, on_breach="deny"))
    proxy, base = _boundary(ledger, fake_upstream.url, policy)
    try:
        report = run_load(base, calls=4, concurrency=1)
    finally:
        proxy.stop()
    assert report["calls_ok"] == 1 and report["calls_refused"] == 3
    assert report["refused_codes"] == {"max_model_calls": 3}
    assert denial_code(json.dumps(report["first_refusal"]["body"])) == "max_model_calls"
    assert report["first_refusal"]["body"]["error"]["type"] == DENIAL_TYPE


# -- the counters and the pacing, where a wrong number would be believed ---- #


def test_the_rss_reader_says_it_cannot_measure_rather_than_reporting_zero(monkeypatch):
    """`-1` is the whole contract of this function: 0 reads as "measured, and the process used no
    memory", and a memory claim is exactly the number that must not be invented."""
    import builtins

    from docket import loaddriver

    def boom(*args, **kwargs):
        raise OSError("no /proc here")

    monkeypatch.setattr(builtins, "open", boom)
    assert loaddriver._rss_kib() == -1

    class NoVmRSS:
        def __enter__(self):
            return iter(["Name:\tpython\n", "State:\tR\n"])

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(builtins, "open", lambda *a, **k: NoVmRSS())
    assert loaddriver._rss_kib() == -1, "/proc without VmRSS is not a measurement of zero"


def test_the_rss_series_keeps_its_start_its_peak_and_its_sample_count(monkeypatch):
    """Start is the first reading, not the latest one, and the count is not decoration."""
    from docket import loaddriver

    readings = iter([100, 200, 300, 400])
    monkeypatch.setattr(loaddriver, "_rss_kib", lambda: next(readings))
    report = loaddriver.Report(concurrency=1, target_rate=0.0, seconds=0.0, calls=1)
    assert report.rss_samples == 0, "a fresh report has taken no samples"
    assert report.rss_start_kib == 100, "the constructor reads a value for a run that ends at once"
    for _ in range(3):
        report.note_rss()
    # The start of the *series* is the first sample, which is the reading the reports are written
    # from. The constructor's value is the fallback for a run that finishes before any sample.
    assert report.rss_start_kib == 200
    assert report.rss_end_kib == 400
    assert report.rss_max_kib == 400, "the peak is the largest reading, not the last"
    assert report.rss_samples == 3


def test_the_rss_sentinel_is_ignored_and_a_reading_is_not(monkeypatch):
    """`UNMEASURABLE` means "the platform would not say", and it is not a reading of zero.

    Both halves matter. A sampler that records the sentinel invents a measurement (the reports
    would show a process using -1 KiB); a sampler that discards a *real* reading loses one, and the
    start of the series is the number the "memory tracks events" claim is computed from.
    """
    from docket import loaddriver

    assert loaddriver.UNMEASURABLE < 0, "the sentinel has to be distinguishable from a reading"
    report = loaddriver.Report(concurrency=1, target_rate=0.0, seconds=0.0, calls=1)
    start = report.rss_start_kib

    monkeypatch.setattr(loaddriver, "_rss_kib", lambda: loaddriver.UNMEASURABLE)
    report.note_rss()
    assert report.rss_samples == 0, "the sentinel is not a sample"
    assert report.rss_end_kib == start, "the sentinel must not move the end of the series"
    assert report.rss_max_kib == start, "the sentinel must not move the peak"

    monkeypatch.setattr(loaddriver, "_rss_kib", lambda: 4096)
    report.note_rss()
    assert report.rss_samples == 1
    assert report.rss_start_kib == 4096 and report.rss_end_kib == 4096


def test_the_call_budget_and_the_deadline_both_stop_the_run(monkeypatch):
    """Two ceilings, each of which must be exact: one call too many is a load test that lied."""
    from docket import loaddriver

    monkeypatch.setattr(loaddriver, "time", loaddriver.time)
    report = loaddriver.Report(concurrency=1, target_rate=0.0, seconds=0.0, calls=2)
    assert report.next_attempt() is True
    assert report.next_attempt() is True
    assert report.next_attempt() is False, "the budget is a ceiling, not a hint"

    report = loaddriver.Report(concurrency=1, target_rate=0.0, seconds=10.0, calls=1000)
    clock = {"t": 0.0}
    monkeypatch.setattr(loaddriver.time, "monotonic", lambda: clock["t"])
    report.deadline = 10.0
    assert report.next_attempt() is True
    clock["t"] = 9.999
    assert report.next_attempt() is True
    clock["t"] = 10.0
    assert report.next_attempt() is False, "the deadline is inclusive: at the deadline, stop"


def test_a_target_rate_is_actually_honoured(ledger, fake_upstream):
    """Pacing is the difference between a soak and a burst wearing a soak's name.

    Each paced run is checked against the floor its own schedule implies: `n` calls at `r` per
    second cannot be issued inside `(n - 1) / r` seconds, whatever the machine is doing, because the
    driver sleeps the remainder of each interval by construction. The assertion is therefore
    one-sided, and contention can only make the run longer — a threshold that noise could push a
    *correct* run past would be a flaky test, and a test that fails on a slow box teaches people to
    re-run it.

    Two rates are driven rather than one, because a single floor proves the wait exists and two
    prove it is the *rate* that decides how long it is. The mutant this catches (`max` → `min` in
    the schedule) removes the waits entirely: both runs finish in the time the calls take and both
    floors fail.

    The warm-up call is not padding. The first call through a fresh boundary pays for the ledger,
    the price table and the key file, and an earlier version of this test measured that instead of
    the schedule — it passed with the mutant applied, because startup alone cleared the threshold.
    """
    proxy, base = _boundary(ledger, fake_upstream.url)
    try:
        run_load(base, calls=1, concurrency=1, timeout=10.0)  # warm-up: not part of the measurement
        slow = time.monotonic()
        run_load(base, calls=5, concurrency=1, rate=10.0, timeout=10.0)
        slow = time.monotonic() - slow
        fast = time.monotonic()
        report = run_load(base, calls=5, concurrency=1, rate=20.0, timeout=10.0)
        fast = time.monotonic() - fast
    finally:
        proxy.stop()

    assert report["calls_ok"] == 5
    assert report["target_rate_per_second"] == "20.00"
    assert slow >= 0.4 * 0.9, f"five calls at 10/s took {slow:.3f}s, against a floor of 0.400s"
    assert fast >= 0.2 * 0.9, f"five calls at 20/s took {fast:.3f}s, against a floor of 0.200s"


def test_a_single_call_is_a_perfectly_valid_run(ledger, fake_upstream):
    """`calls == 1` must be allowed: an off-by-one guard that refuses it is a defect."""
    proxy, base = _boundary(ledger, fake_upstream.url)
    try:
        report = run_load(base, calls=1, concurrency=1)
    finally:
        proxy.stop()
    assert report["attempts_issued"] == 1 and report["calls_ok"] == 1


def test_a_fast_run_still_reports_a_rate(ledger, fake_upstream):
    """A run that takes under a second still has a rate; reporting 0.00 calls/s would be a lie."""
    proxy, base = _boundary(ledger, fake_upstream.url)
    try:
        report = run_load(base, calls=3, concurrency=1)
    finally:
        proxy.stop()
    assert float(report["seconds"]) < 1.0, "this test only means something if the run is quick"
    assert float(report["achieved_calls_per_second"]) > 0


def test_a_run_with_no_measured_call_has_an_empty_summary_not_an_exception(ledger, fake_upstream):
    """Zero samples is a result. Summarising it must not call a percentile over an empty list."""
    proxy, base = _boundary(ledger, fake_upstream.url)
    proxy.stop()
    report = run_load(base, calls=2, concurrency=1, timeout=2.0)
    assert report["calls_ok"] == 0
    assert report["bytes_read"] == 0, "no wire bytes were read, and a counter is not a placeholder"
    assert report["client_latency"]["measured_calls"] == 0
    assert report["client_latency"]["p50_ms"] == 0 and report["client_latency"]["p99_ms"] == 0
    # Every percentile follows the same rule on an empty list, and the maximum is the list's last
    # element rather than a percentile at all: each of the three is a separate way to raise.
    assert report["client_latency"]["p95_ms"] == 0
    assert report["client_latency"]["max_ms"] == 0
    assert report["client_latency"]["total_ms"] == 0


def test_the_driver_exit_code_separates_a_clean_run_from_a_dropped_call(monkeypatch, tmp_path, ledger, fake_upstream):
    """Exit 0 means every call was answered; exit 3 means one was not. A supervisor reads that."""
    # Port 1 on loopback: nothing is listening there, so every call is a connection failure.
    monkeypatch.setenv("DOCKET_PROXY_URL", "http://127.0.0.1:1")
    assert driver_main(["--calls", "1", "--report", str(tmp_path / "dead.json")]) == 3
    written = json.loads((tmp_path / "dead.json").read_text(encoding="utf-8"))
    assert written["calls_error"] == 1 and written["calls_ok"] == 0

    proxy, base = _boundary(ledger, fake_upstream.url)
    monkeypatch.setenv("DOCKET_PROXY_URL", base)
    try:
        assert driver_main(["--calls", "2", "--report", str(tmp_path / "clean.json")]) == 0
    finally:
        proxy.stop()
    clean = json.loads((tmp_path / "clean.json").read_text(encoding="utf-8"))
    assert clean["calls_ok"] == 2 and clean["calls_error"] == 0


# -- the orchestrator's judgement ------------------------------------------- #


def _run(calls: int, work: Path, *extra: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    report = work / "report.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(REPO / "tools" / "loadtest.py"),
            "--calls",
            str(calls),
            "--concurrency",
            "3",
            "--work-dir",
            str(work),
            "--report",
            str(report),
            "--markdown",
            str(work / "report.md"),
            "--json",
            *extra,
        ],
        cwd=work,  # deliberately not the repository: the tool must not depend on the caller's cwd
        capture_output=True,
        text=True,
        timeout=300,
    )
    return completed, report


def test_the_whole_path_produces_a_report_that_says_it_is_usable(tmp_path):
    """Provider service → supervisor → boundary → driver → ledger → receipt → verifier."""
    completed, report_path = _run(18, tmp_path)
    assert completed.returncode == 0, completed.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["usable_as_evidence"] is True, report["problems"]
    assert report["problems"] == []
    assert report["load"]["calls_ok"] == 18
    assert report["recorder"]["ledger"]["model_responses"] == 18
    assert report["recorder"]["ledger"]["responses_with_latency"] == 18
    assert report["verification"]["ok"] is True, report["verification"]
    assert report["host"]["cpu_count"] >= 1
    assert report["load"]["client_latency"]["method"] == "nearest-rank"
    # The report keeps the artefacts it describes, or the numbers in it cannot be re-checked.
    assert Path(report["recorder"]["receipt_path"]).exists()
    assert Path(report["recorder"]["ledger_path"]).exists()
    table = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "calls answered" in table and "receipt verifies afterwards | yes" in table


def test_the_ledger_is_found_by_the_receipt_s_digest(tmp_path):
    """A second .jsonl in the directory must not be mistaken for this run's ledger."""
    completed, report_path = _run(6, tmp_path)
    assert completed.returncode == 0, completed.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    ledger_path = Path(report["recorder"]["ledger_path"])
    (ledger_path.parent / "000000000000-decoy.jsonl").write_text('{"type": "note"}\n', encoding="utf-8")
    docket_dir = ledger_path.parents[1]
    evidence = loadtest_tool.read_evidence(docket_dir, [])
    assert Path(evidence["ledger_path"]) == ledger_path


def test_a_missing_ledger_is_a_failure_not_an_empty_result(tmp_path):
    """Counting zero rows and calling it a clean run is the failure mode this guards."""
    completed, report_path = _run(4, tmp_path)
    assert completed.returncode == 0, completed.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    docket_dir = Path(report["recorder"]["ledger_path"]).parents[1]
    for ledger_file in (docket_dir / "runs").glob("*.jsonl"):
        ledger_file.write_text('{"type": "note"}\n', encoding="utf-8")  # no longer hashes to the receipt
    with pytest.raises(loadtest_tool.Failure, match="hashes to the receipt"):
        loadtest_tool.read_evidence(docket_dir, [])


def test_a_run_with_no_receipt_is_refused(tmp_path):
    with pytest.raises(loadtest_tool.Failure, match="no receipt"):
        loadtest_tool.read_evidence(tmp_path, [])


def test_disagreeing_counts_make_a_run_unusable():
    """The single most important judgement here: two sources, one event, they must match."""

    def run(ok: int) -> dict:
        return {
            "driver": {
                "calls_ok": ok,
                "calls_refused": 0,
                "calls_error": 0,
                "first_error": None,
                "attempts_issued": ok,
            },
            "exit_code": 0,
        }

    evidence = {
        "ledger": {"model_responses": 7, "responses_with_latency": 7},
        "receipt": {"latency": {"measured_calls": 7}},
        "verify": {"ok": True},
    }
    assert loadtest_tool.judge(run(7), evidence) == []
    problems = loadtest_tool.judge(run(8), evidence)
    assert problems and "disagree" in problems[0]


def test_a_row_without_a_latency_makes_a_run_unusable():
    """Partial coverage of the latency summary is a finding, not a footnote."""
    run = {
        "driver": {"calls_ok": 5, "calls_refused": 0, "calls_error": 0, "first_error": None, "attempts_issued": 5},
        "exit_code": 0,
    }
    evidence = {
        "ledger": {"model_responses": 5, "responses_with_latency": 4},
        "receipt": {"latency": {"measured_calls": 4}},
        "verify": {"ok": True},
    }
    problems = loadtest_tool.judge(run, evidence)
    assert any("carry no latency_ms" in problem for problem in problems)
    assert any("latency summary covers 4" in problem for problem in problems)


def test_a_receipt_that_does_not_verify_makes_a_run_unusable():
    run = {
        "driver": {"calls_ok": 3, "calls_refused": 0, "calls_error": 0, "first_error": None, "attempts_issued": 3},
        "exit_code": 0,
    }
    evidence = {
        "ledger": {"model_responses": 3, "responses_with_latency": 3},
        "receipt": {"latency": {"measured_calls": 3}},
        "verify": {"ok": False},
    }
    assert loadtest_tool.judge(run, evidence) == ["the receipt from this run does not verify"]


def test_a_short_run_refuses_to_state_a_memory_trend():
    """One sample is not a trend, and a fake `+0 KiB` would be indistinguishable from a real one."""
    assert "not measured" in loadtest_tool._rss_cell({"samples": 1, "start": 1, "end": 2, "peak_kib": 2})
    cell = loadtest_tool._rss_cell({"samples": 5, "start": 10_000, "end": 11_024, "peak_kib": 12_000})
    assert "+1024 KiB" in cell and "peak 12000 KiB" in cell


def test_the_provider_is_started_as_a_service(tmp_path):
    """The load must come through the same provider entry point a container runs."""
    log: list[str] = []
    process, url = loadtest_tool.start_upstream(tmp_path, log)
    try:
        assert url.startswith("http://127.0.0.1:")
        assert any("listening" in line for line in log)
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_a_provider_that_cannot_start_is_a_failure_not_a_zero(monkeypatch, tmp_path):
    """No provider means no measurement; the tool must not report an empty run as a clean one."""

    class Dead:
        def __init__(self, *args, **kwargs):
            self.stdout = None
            self.returncode = 1

        def kill(self):
            pass

        def send_signal(self, signal):
            pass

        def wait(self, timeout=None):
            return 1

    monkeypatch.setattr(loadtest_tool.subprocess, "Popen", lambda *a, **k: Dead())
    with pytest.raises(loadtest_tool.Failure, match="no stream to read"):
        loadtest_tool.start_upstream(tmp_path, [])


def test_the_driver_route_and_body_are_the_ones_the_boundary_meters():
    """The driver must address a route the boundary recognises: /openai/v1/chat/completions."""
    from docket.loaddriver import BODY, DEFAULT_ROUTE

    assert DEFAULT_ROUTE == "/openai/v1/chat/completions"
    assert BODY["model"] and BODY["messages"]


def test_the_driver_stops_on_a_call_budget(ledger, fake_upstream):
    """`--calls` is a ceiling, not a hope: concurrency must not overshoot it."""
    proxy, base = _boundary(ledger, fake_upstream.url)
    try:
        report = run_load(base, calls=5, concurrency=8)
    finally:
        proxy.stop()
    assert report["attempts_issued"] == 5
    assert report["calls_ok"] == 5


def test_a_step_that_delays_is_waited_for_rather_than_dropped(ledger):
    """A slow provider is the case a timeout must handle, and the client's latency must show it."""
    upstream = Step(delay_seconds=0.05)
    from docket.fakeupstream import FakeUpstream

    server = FakeUpstream(steps=[upstream])
    server.start()
    proxy, base = _boundary(ledger, server.url)
    try:
        report = run_load(base, calls=3, concurrency=1)
    finally:
        proxy.stop()
        server.stop()
    assert report["calls_ok"] == 3
    assert report["client_latency"]["p50_ms"] >= 50


def test_the_arguments_are_validated_at_the_door(ledger, fake_upstream):
    """A run that cannot be executed must fail before it starts, not by dividing by nothing.

    `run_load` is also a library call — `tools/loadtest.py` and the game day both use it — so the
    guard has to be here and not only in the argument parser.
    """
    from docket import loaddriver

    with pytest.raises(ValueError, match="at least 1"):
        run_load("http://127.0.0.1:1", calls=0)
    with pytest.raises(ValueError, match="at least 1"):
        run_load("http://127.0.0.1:1", concurrency=0)
    with pytest.raises(ValueError, match="at least 1"):
        run_load("http://127.0.0.1:1", concurrency=-3)
    # A URL with no port is not a boundary this driver can reach, and saying so is better than a
    # connection error five seconds later that names the socket instead of the argument.
    with pytest.raises(ValueError, match="not an http boundary URL"):
        loaddriver._parts("http://127.0.0.1")
    with pytest.raises(ValueError, match="not an http boundary URL"):
        loaddriver._parts("file:///etc/passwd")


def test_a_zero_length_run_summarises_to_zero_rather_than_raising():
    """The rate is `ok / elapsed`, so the guard against a zero clock is the guard against a crash.

    Unreachable through `run_load` — a run that returns at all has taken measurable time — and kept
    for exactly that reason: the summary is also built from rows a test or a tool can hand it, and
    a division by zero there would take down a report that had already been paid for.
    """
    from docket import loaddriver

    report = loaddriver.Report(concurrency=1, target_rate=0.0, seconds=0.0, calls=0)
    summary = loaddriver._summarise(report, "http://127.0.0.1:1", "/openai/v1/chat/completions", 0.0)
    assert summary["achieved_calls_per_second"] == "0.00"
    assert summary["seconds"] == "0.000"


def test_the_report_is_written_with_sorted_keys(tmp_path, monkeypatch, ledger, fake_upstream):
    """The report is evidence, so its bytes are canonical: the file is the sorted form of itself.

    `sort_keys=True` is the whole of that property. Without a test it is one keyword away from
    `False`, and a diff of two reports would then show key order rather than what changed.
    """
    monkeypatch.setenv("DOCKET_PROXY_URL", "http://127.0.0.1:1")  # replaced below; keeps the env set
    proxy, base = _boundary(ledger, fake_upstream.url)
    monkeypatch.setenv("DOCKET_PROXY_URL", base)
    written = tmp_path / "one.json"
    other = tmp_path / "two.json"
    try:
        assert driver_main(["--calls", "2", "--report", str(written)]) == 0
        assert driver_main(["--calls", "2", "--report", str(other)]) == 0
    finally:
        proxy.stop()

    text = written.read_text(encoding="utf-8")
    assert text.strip() == json.dumps(
        json.loads(text), sort_keys=True
    ), "the report on disk is not the sorted form of itself"
    pairs = json.loads(text, object_pairs_hook=lambda items: items)  # keeps the byte order
    keys = [key for key, _ in pairs]
    assert keys == sorted(keys), f"top-level keys are in insertion order, not sorted: {keys}"
    assert [key for key, _ in json.loads(other.read_text(encoding="utf-8"), object_pairs_hook=lambda i: i)] == keys


def test_the_help_text_is_the_module_docstring(tmp_path, capsys):
    """`--help` is the only place the driver explains itself to someone who typed `-h`.

    The description is the docstring's *first* line — an index into the split, which is easy to
    change by accident and invisible to every other test.
    """
    from docket import loaddriver

    with pytest.raises(SystemExit) as exited:
        driver_main(["--help"])
    assert exited.value.code == 0
    # argparse re-wraps the description to the terminal width, so the comparison is made on
    # whitespace-normalised text: the sentence has to be there, its line breaks are not ours.
    printed = " ".join(capsys.readouterr().out.split())
    first_line = loaddriver.__doc__.split("\n")[0].strip()
    assert first_line in printed, f"the help does not open with the module's own summary: {first_line!r}"
    assert "usage: python -m docket.loaddriver" in printed
