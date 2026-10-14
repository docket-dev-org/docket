"""The boundary under parallel load: several model calls in flight at once.

The proxy is a `ThreadingHTTPServer`, so this is the normal case for an agent that fans out —
not an exotic one. Four properties have to hold when it happens, and each of them is a claim
Docket makes elsewhere in its own documentation:

* the ledger file stays one chain in sequence order (the record must not accuse an honest run);
* the cost total equals the sum of the calls (the receipt's number is the reason to install it);
* the rate limiter admits exactly its budget, not "about" its budget under contention;
* the breaker counts each failure once, so a run cannot trip it by racing.

Times and totals are asserted as *conservation laws* rather than as timings: a slow machine may
reorder anything it likes, but it cannot make two calls cost one call.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path

from docket.fakeupstream import FakeUpstream, Step
from docket.ledger import GENESIS, Ledger
from docket.policy import Caps, Files, Policy
from docket.pricing import CostMeter, PriceTable
from docket.proxy import RecordingProxy
from docket.resilience import CircuitBreaker, RateLimiter
from docket.verify import verify_receipt

CALLS = 24
THREADS = 8


def _upstream() -> FakeUpstream:
    # One step per call would be fragile under concurrency; the fake repeats its last step,
    # which is what makes the per-call cost a fixed number.
    return FakeUpstream(steps=[Step(model="claude-sonnet-5", input_tokens=1_000, output_tokens=200)])


def _post(base: str, payload: dict, timeout: float = 20) -> str:
    """The outcome of one call, as a string: a status code, or the transport failure.

    Every outcome is returned rather than raised, because a request that dies on the way *is* an
    outcome this file has to account for: a thread that dies takes its remaining calls with it
    and the test then passes on half the traffic it meant to send.
    """
    request = urllib.request.Request(
        f"{base}/openai/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
            return str(response.status)
    except urllib.error.HTTPError as exc:
        exc.read()
        return str(exc.code)
    except Exception as exc:  # noqa: BLE001 - the point is that no outcome is lost
        return f"{type(exc).__name__}"


def _drive(base: str, calls: int, threads: int) -> list[str]:
    """Fire `calls` requests from `threads` threads at once, and collect every status."""
    statuses: list[str] = []
    lock = threading.Lock()
    index = iter(range(calls))
    start = threading.Barrier(threads)

    def worker() -> None:
        start.wait()
        while True:
            with lock:
                try:
                    step = next(index)
                except StopIteration:
                    return
            status = _post(base, {"model": "claude-sonnet-5", "messages": [{"content": f"step {step}"}]})
            with lock:
                statuses.append(status)

    workers = [threading.Thread(target=worker) for _ in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=120)
    return statuses


def test_parallel_calls_leave_one_sound_ledger_and_one_honest_total(tmp_path: Path):
    upstream = _upstream()
    host, port = upstream.start()
    ledger = Ledger.open(tmp_path / "runs" / "parallel.jsonl", max_bytes=None)
    meter = CostMeter(price_table=PriceTable.load())
    proxy = RecordingProxy(
        ledger=ledger,
        policy=Policy(name="parallel", caps=Caps(max_usd=Decimal("1000")), files=Files(watch=[])),
        meter=meter,
        session_id="parallel",
        upstreams={"openai": f"http://{host}:{port}"},
    )
    proxy_host, proxy_port = proxy.start()
    base = f"http://{proxy_host}:{proxy_port}"
    try:
        statuses = _drive(base, CALLS, THREADS)
        ledger.close()
    finally:
        proxy.stop()
        upstream.stop()

    assert statuses.count("200") == CALLS, f"statuses {sorted(set(statuses))}"

    events = Ledger.read(tmp_path / "runs" / "parallel.jsonl")
    assert [event.seq for event in events] == list(range(len(events))), "sequence order in the file"
    previous = None
    for event in events:
        assert event.prev == (previous.hash if previous else GENESIS)
        previous = event

    responses = [event for event in events if event.type == "model.response"]
    assert len(responses) == CALLS, f"{len(responses)} responses recorded of {CALLS} calls"
    recorded = sum(Decimal(str(event.data["cost_usd"])) for event in responses)
    # Same tokens every time, so the meter must be exactly N times one call — no lost updates.
    # This is not book-keeping: `docket verify` recomputes the total from the usage rows and
    # compares it with the receipt's, so a lost update here fails verification on an honest run.
    assert meter.calls == CALLS
    assert meter.cost == recorded == per_call_cost(responses[0]) * CALLS


def per_call_cost(event) -> Decimal:
    return Decimal(str(event.data["cost_usd"]))


def test_the_rate_limiter_admits_its_budget_and_no_more_under_contention(tmp_path: Path):
    """`per_minute=5` means five. A token bucket that is read-modify-written by two threads at
    once admits six, and "the limiter is approximate" is not a thing you can say to the person
    whose week of calls went out in a burst."""
    upstream = _upstream()
    host, port = upstream.start()
    ledger = Ledger.open(tmp_path / "runs" / "limited.jsonl", max_bytes=None)
    meter = CostMeter(price_table=PriceTable.load())
    proxy = RecordingProxy(
        ledger=ledger,
        policy=Policy(name="limited", files=Files(watch=[])),
        meter=meter,
        session_id="limited",
        upstreams={"openai": f"http://{host}:{port}"},
        limiter=RateLimiter(per_minute=5),
    )
    proxy_host, proxy_port = proxy.start()
    base = f"http://{proxy_host}:{proxy_port}"
    try:
        statuses = _drive(base, 20, 8)
        ledger.close()
    finally:
        proxy.stop()
        upstream.stop()
    assert statuses.count("200") == 5, f"admitted {statuses.count('200')} of a budget of 5: {sorted(statuses)}"
    assert statuses.count("429") == 15
    assert len(statuses) == 20, f"a request went missing: {sorted(statuses)}"
    limited = [
        event for event in Ledger.read(tmp_path / "runs" / "limited.jsonl") if event.type == "x-docket.rate-limited"
    ]
    assert len(limited) == 15, f"{len(limited)} refusals recorded of 15"


def test_the_breaker_counts_parallel_failures_once_each(tmp_path: Path):
    """A breaker with a threshold of 5 must not open after three failures counted twice."""

    class AlwaysFails:
        url = "http://127.0.0.1:1"  # nothing is listening: every call fails the same way

    ledger = Ledger.open(tmp_path / "runs" / "breaker.jsonl", max_bytes=None)
    breaker = CircuitBreaker(threshold=5, cooldown_seconds=30, max_cooldown_seconds=300)
    proxy = RecordingProxy(
        ledger=ledger,
        policy=Policy(name="breaker", files=Files(watch=[])),
        meter=CostMeter(price_table=PriceTable.load()),
        session_id="breaker",
        upstreams={"openai": AlwaysFails.url},
        breaker=breaker,
    )
    proxy_host, proxy_port = proxy.start()
    base = f"http://{proxy_host}:{proxy_port}"
    try:
        statuses = _drive(base, 8, 4)
        ledger.close()
    finally:
        proxy.stop()
    assert len(statuses) == 8, f"a request went missing: {statuses}"
    assert all(status in {"500", "502", "503"} or status.startswith("URLError") for status in statuses), sorted(
        set(statuses)
    )

    state = breaker.snapshot()["openai"]
    assert state["openings"] == 1, f"the breaker opened {state['openings']} times: {state}"
    assert state["consecutive_failures"] >= 5, state
    assert state["state"] != "closed", state


def test_a_parallel_burst_cannot_walk_past_the_ceiling(tmp_path: Path):
    """The cap is enforced at *admission*, so the honest statement is a bound, not a number: a
    burst can overshoot by at most one call per request that was already in flight when the
    breach was recorded — six threads, so at most six calls. What must never happen is a burst
    that ignores the ceiling altogether, or a run that keeps going after it."""
    upstream = _upstream()
    host, port = upstream.start()
    path = tmp_path / "runs" / "capped.jsonl"
    ledger = Ledger.open(path, max_bytes=None)
    meter = CostMeter(price_table=PriceTable.load())
    # One call costs $0.006 (1000 in / 200 out on claude-sonnet-5), so a $0.006 cap admits one
    # call and is crossed by the second.
    policy = Policy(name="capped", caps=Caps(max_usd=Decimal("0.006"), on_breach="deny"), files=Files(watch=[]))
    proxy = RecordingProxy(
        ledger=ledger,
        policy=policy,
        meter=meter,
        session_id="capped",
        upstreams={"openai": f"http://{host}:{port}"},
    )
    proxy_host, proxy_port = proxy.start()
    base = f"http://{proxy_host}:{proxy_port}"
    burst, in_flight = 12, 6
    try:
        statuses = _drive(base, burst, in_flight)
        time.sleep(0.05)
        ledger.close()
    finally:
        proxy.stop()
        upstream.stop()
    events = Ledger.read(path)
    paid = [event for event in events if event.type == "model.response"]
    assert len(paid) <= in_flight + 1, f"{len(paid)} paid calls recorded for a one-call cap"
    assert statuses.count("200") == len(paid), f"{statuses.count('200')} 200s of {len(paid)} billed calls"
    assert len(paid) >= 1, "the call that crossed the cap is a real call and is billed"
    caps = [event for event in events if event.type == "cap.exceeded"]
    assert caps, "the ceiling must be recorded as well as enforced"
    # Every request has exactly one outcome: the ones that passed the check and were paid for,
    # and the ones the boundary refused afterwards. Nothing vanishes under load.
    assert len(statuses) == burst, sorted(statuses)
    assert statuses.count("200") + statuses.count("403") == burst, sorted(set(statuses))


def test_a_real_run_receipt_still_verifies_after_parallel_calls(tmp_path: Path):
    """The end of the chain: the receipt built from a concurrently-written ledger verifies —
    including the usage rows, whose Merkle paths depend on the event order the file has."""
    from docket.receipt import build_receipt

    upstream = _upstream()
    host, port = upstream.start()
    path = tmp_path / "runs" / "receipt.jsonl"
    ledger = Ledger.open(path, max_bytes=None)
    meter = CostMeter(price_table=PriceTable.load())
    proxy = RecordingProxy(
        ledger=ledger,
        policy=Policy(name="receipt", caps=Caps(max_usd=Decimal("1000")), files=Files(watch=[])),
        meter=meter,
        session_id="receipt",
        upstreams={"openai": f"http://{host}:{port}"},
    )
    proxy_host, proxy_port = proxy.start()
    base = f"http://{proxy_host}:{proxy_port}"
    try:
        _drive(base, 12, 4)
        receipt = build_receipt(
            events=list(ledger.events),
            ledger=ledger,
            meter=meter,
            policy=proxy.policy,
            session_id="receipt",
            docket_number="DKT-20261014-PARALL",
            exit_code=0,
            duration_seconds=1.0,
            price_table=PriceTable.load(),
        )
        ledger.close()
    finally:
        proxy.stop()
        upstream.stop()

    assert receipt["totals"]["calls"] == 12
    result = verify_receipt(receipt, ledger_path=path)
    assert result.ok, "\n".join(f"{check.name}: {check.detail}" for check in result.checks if check.status == "fail")


def test_the_cost_meter_does_not_lose_updates_under_forced_thread_switches():
    """A negative result, recorded because the invariant matters more than the result.

    The meter is shared by every request thread, and `verify` recomputes the receipt's total
    from the usage rows: a lost `+=` would make an honest receipt fail its own check. CPython
    switches threads between bytecodes, not within an attribute increment, so 3,200 concurrent
    `record()` calls under a 1-microsecond switch interval lose nothing. The attempt is kept so
    that an implementation which stops being safe here — one that parses, formats or reads
    through a file — is caught by a test that was looking for exactly that.
    """
    import sys

    from docket.pricing import CostMeter, PriceTable

    meter = CostMeter(price_table=PriceTable.load())
    calls, threads = 400, 8
    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:

        def worker() -> None:
            for _ in range(calls):
                meter.record("claude-sonnet-5", 1_000, 0, 200, "reported")

        workers = [threading.Thread(target=worker) for _ in range(threads)]
        for worker_thread in workers:
            worker_thread.start()
        for worker_thread in workers:
            worker_thread.join(timeout=60)
    finally:
        sys.setswitchinterval(previous)

    assert meter.calls == calls * threads
    assert meter.input_tokens == 1_000 * calls * threads
    assert meter.cost == Decimal("0.006") * calls * threads
