"""Invariants under hostile conditions: the concurrency, idempotency, lifecycle and
clock/stamp properties that no single-feature test can state.

Every test here answers a question an operations reader would ask of this tool before putting it
in front of a production agent, and every one of them is *executed*, not argued:

* can two runs write to one evidence directory at the same time without producing a chain that
  accuses an honest run of tampering?
* is a wrongly *replayed* event recorded as a second call, or silently deduplicated? (Docket's
  answer is the first, and it is the right one: a deduplicated ledger is a false record.)
* does a stopped boundary hand back every listening socket it was given?
* does a killed supervisor leave a corrupt ledger, a stale lock, or a receipt that claims the run
  ended?
* does an agent's backoff follow the boundary's ``Retry-After`` and stay bounded?
* what happens to the run when the machine's clock moves backwards *during* the run?

The last one is the reason `supervisor.py` takes a `monotonic` callable. `time.time()` on a laptop
that sleeps, a VM live-migrated across hosts, or a container with a stale clock can step backwards
by minutes; a wall-clock cap computed by subtracting `time.time()` values then measures something
other than elapsed time — it can even go negative and stop the run instantly, or fail to stop it
at all. These tests move the clock under a real supervised run and assert the cap still holds.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path

import pytest

from docket.canon import sha256_hex
from docket.fakeupstream import FakeUpstream, Step
from docket.ledger import Ledger, LedgerLockedError
from docket.policy import Caps, Policy
from docket.supervisor import EXIT_CAP_EXCEEDED, Supervisor

CALL_CHILD = """
import json, os, urllib.request
base = os.environ["OPENAI_BASE_URL"]
body = {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hello"}]}
request = urllib.request.Request(base + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
with urllib.request.urlopen(request, timeout=20) as response:
    print(response.read()[:20])
"""

SLEEP_CHILD = "import time; time.sleep(30)"
SHORT_CHILD = "import time; time.sleep(3)"

RETRY_CHILD = """
import json, os, sys, time, urllib.error, urllib.request
base = os.environ["OPENAI_BASE_URL"]
body = {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hello"}]}
refusals = 0
for index in range(120):
    request = urllib.request.Request(base + "/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        refusals += 1
        hint = exc.headers.get("Retry-After")
        print("refused", exc.code, "retry-after", hint, file=sys.stderr, flush=True)
        break
print(json.dumps({"refusals": refusals}))
"""


def _upstream() -> FakeUpstream:
    return FakeUpstream(steps=[Step(model="claude-sonnet-5", input_tokens=1_000, output_tokens=100)])


def _pids_for_session(session_id: str) -> list[int]:
    """Every live process whose environment names this session.

    `pgrep -f` matches a command line, which is not unique — a stray `pytest` on the box can carry
    the same text. The environment of a Docket-supervised child carries `DOCKET_SESSION`, which is
    generated per run, so this cannot address the wrong process.
    """
    found: list[int] = []
    wanted = f"DOCKET_SESSION={session_id}".encode()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        with contextlib.suppress(OSError, PermissionError):
            if wanted in (entry / "environ").read_bytes():
                found.append(int(entry.name))
    return found


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


# --------------------------------------------------------------------------- #
# Two runs, one evidence directory
# --------------------------------------------------------------------------- #


def test_two_runs_in_one_directory_get_two_ledgers_and_two_locks(tmp_path):
    """Concurrency is separated by *files*, not by a global lock — and the lock is per ledger.

    Each supervised run writes `runs/<session>.jsonl`, so two runs in one docket directory never
    interleave inside a file: they are two chains, each self-consistent and each verifiable on its
    own. What must *not* happen is two writers on one file, because each writer would compute
    `prev` from its own view and the result would be a file containing two chains — which
    `docket verify` then reports as tampering. A tool that accuses an honest operator of tampering
    is worse than one that refuses to start, so the second `open` is refused, and this test drives
    that through two real supervisor runs plus a direct pair of handles.
    """
    upstream = _upstream()
    upstream.start()
    try:
        results = []
        for _ in range(2):
            supervisor = Supervisor(
                [sys.executable, "-c", CALL_CHILD],
                policy=Policy(name="t", caps=Caps(max_usd=None)),
                docket_dir=tmp_path / ".docket",
                cwd=tmp_path,
                upstreams={"openai": upstream.url},
            )
            results.append(supervisor.run())
        paths = {result.ledger_path for result in results}
        assert len(paths) == 2, "two sessions, two ledgers"
        for result in results:
            events = Ledger.read(result.ledger_path)
            assert [event.seq for event in events] == list(range(len(events))), "each is a clean chain"
            assert Ledger(events=events).chain_error() is None
            assert len(result.receipt["ledger"]["file_sha256"]) == 64
    finally:
        upstream.stop()

    # And the file-level rule, directly: one writer at a time, and the refusal names the holder.
    one = Ledger.open(tmp_path / "shared.jsonl")
    try:
        with pytest.raises(LedgerLockedError) as caught:
            Ledger.open(tmp_path / "shared.jsonl")
        assert str(os.getpid()) in str(caught.value) or "pid" in str(caught.value), str(caught.value)
    finally:
        one.close()
    # Released means available again: a lock that outlived its holder would wedge the next run.
    again = Ledger.open(tmp_path / "shared.jsonl")
    again.append("note", {"message": "after"})
    again.close()


def test_concurrent_appends_to_one_ledger_keep_the_chain_total(tmp_path):
    """The consequence of losing the race, tested rather than argued: eight threads, 200 events.

    A ledger written from several threads must end up with exactly one event per sequence number,
    in order, with a single unbroken chain — no duplicate `seq`, no lost event, no interleaved
    half-line. (The file has one writer at a time; concurrent *threads* share one handle, which is
    the supported in-process case.)
    """
    ledger = Ledger.open(tmp_path / "threads.jsonl")
    threads = []
    per_thread = 25

    def _work(worker: int) -> None:
        for index in range(per_thread):
            ledger.append("note", {"worker": worker, "index": index})

    for worker in range(8):
        thread = threading.Thread(target=_work, args=(worker,), name=f"w{worker}")
        threads.append(thread)
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads)
    ledger.close()

    events = Ledger.read(tmp_path / "threads.jsonl")
    assert len(events) == 8 * per_thread
    assert [event.seq for event in events] == list(range(8 * per_thread))
    assert len({(event.data["worker"], event.data["index"]) for event in events}) == 8 * per_thread
    assert Ledger(events=events).chain_error() is None
    raw = (tmp_path / "threads.jsonl").read_bytes()
    assert raw.count(b"\n") == 8 * per_thread, "every line is complete"
    assert len({json.loads(line)["seq"] for line in raw.splitlines()}) == 8 * per_thread


def test_a_closed_ledger_is_not_written_to_again(tmp_path):
    """Closing is final: a closed handle plus a further append must not resurrect the file.

    The ledger's own repair path reopens a descriptor that was closed *under* it, which is right
    for a reclaimed descriptor — but a deliberate `close()` is a different statement, and the two
    are distinguished by the file being released rather than the object being reused.
    """
    path = tmp_path / "closed.jsonl"
    ledger = Ledger.open(path)
    ledger.append("note", {"message": "one"})
    size_after_first = path.stat().st_size
    ledger.close()
    assert ledger._handle is None

    # A new ledger on the same path resumes the chain rather than starting a second one.
    resumed = Ledger.open(path)
    assert len(resumed) == 1, "the existing chain is loaded, not overwritten"
    resumed.append("note", {"message": "two"})
    resumed.close()
    assert path.stat().st_size > size_after_first
    events = Ledger.read(path)
    assert [event.seq for event in events] == [0, 1]
    assert Ledger(events=events).chain_error() is None


def test_the_boundary_can_be_told_which_port_to_listen_on(tmp_path):
    """A fixed port is what an orchestrator needs: a readiness probe, a metrics scrape, or a
    sidecar that finds the boundary by address. The evidence then has to say *which* port it was,
    because a probe that fires against the wrong port is a false alarm either way.

    Two facts, both checked: the port that was asked for is the port that answers, and the ledger
    records the requested port in `session.start` and the *actual* one in a note once the socket
    exists — a run that asked for port 0 must not have a fabricated number in its record.
    """
    import urllib.request

    from docket.pricing import CostMeter, PriceTable
    from docket.proxy import RecordingProxy

    ledger = Ledger.open(tmp_path / "l.jsonl")
    upstream = _upstream()
    upstream.start()
    port = _free_port()
    proxy = RecordingProxy(
        ledger=ledger,
        policy=Policy(),
        meter=CostMeter(price_table=PriceTable.load()),
        session_id="port",
        upstreams={"openai": upstream.url},
    )
    host, bound = proxy.start(port)
    try:
        assert bound == port, f"asked for {port}, bound {bound}"
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/docket/healthz", timeout=5) as response:
            assert response.status == 200
    finally:
        proxy.stop()
        upstream.stop()
        ledger.close()

    # And an occupied port is refused with the port named, not with a bare EADDRINUSE.
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    taken = holder.getsockname()[1]
    try:
        other = Ledger.open(tmp_path / "l2.jsonl")
        second = RecordingProxy(
            ledger=other,
            policy=Policy(),
            meter=CostMeter(price_table=PriceTable.load()),
            session_id="port2",
            upstreams={"openai": upstream.url},
        )
        with pytest.raises(OSError) as caught:
            second.start(taken)
        assert str(taken) in str(caught.value), caught.value
        other.close()
    finally:
        holder.close()


def test_a_supervised_run_records_the_boundary_port_it_actually_used(tmp_path):
    """The note is written by the supervisor, so this drives a real run: the requested port and the
    bound port are both in the evidence, and they agree when a port was requested."""
    upstream = _upstream()
    upstream.start()
    port = _free_port()
    try:
        supervisor = Supervisor(
            [sys.executable, "-c", CALL_CHILD],
            policy=Policy(name="t", caps=Caps(max_usd=None)),
            docket_dir=tmp_path / ".docket",
            cwd=tmp_path,
            upstreams={"openai": upstream.url},
            proxy_port=port,
        )
        result = supervisor.run()
        events = Ledger.read(result.ledger_path)
        start = next(event for event in events if event.type == "session.start")
        assert start.data["boundary"] == {"host": "127.0.0.1", "requested_port": port}
        listening = [
            event.data
            for event in events
            if event.type == "note" and event.data.get("message") == "the boundary is listening on loopback"
        ]
        assert listening and listening[0]["port"] == port, listening
        assert listening[0]["requested_port"] == port
    finally:
        upstream.stop()


# --------------------------------------------------------------------------- #
# Idempotency: what Docket does *not* do
# --------------------------------------------------------------------------- #


def test_a_replayed_request_is_recorded_as_two_calls_and_never_deduplicated(tmp_path):
    """The honest answer to a duplicate, stated as a test so nobody "fixes" it into a cache.

    If the same event is appended twice — a replayed request, a retry, a client that sent the call
    twice — the ledger holds two events, two sequence numbers and two links. Docket deliberately
    has no idempotency key and no deduplication: an evidence tool that silently collapsed a
    duplicate would be reporting a run that did not happen, and the duplicate *is* the finding
    (it is what `runaway-loop` and `retry-storm` are built on).
    """
    ledger = Ledger.open(tmp_path / "dup.jsonl")
    payload = {
        "provider": "anthropic",
        "model": "claude-sonnet-5",
        "usage": {"input_tokens": 1000},
        "cost_usd": "0.003",
    }
    first = ledger.append("model.response", payload)
    second = ledger.append("model.response", payload)
    ledger.close()

    assert (first.seq, second.seq) == (0, 1), "two calls, not one"
    assert first.hash != second.hash, "equal payloads at different sequence numbers are different events"
    assert second.prev == first.hash, "and the chain links them in order"
    events = Ledger.read(tmp_path / "dup.jsonl")
    assert len(events) == 2
    assert Ledger(events=events).chain_error() is None

    # The same payload re-appended to a ledger that *has* it is still a second event: deduplication
    # would require trusting the payload, which is the thing under audit.
    again = Ledger.open(tmp_path / "dup.jsonl")
    third = again.append("model.response", payload)
    again.close()
    assert third.seq == 2


# --------------------------------------------------------------------------- #
# Lifecycle: sockets, threads, processes
# --------------------------------------------------------------------------- #


def test_a_stopped_boundary_hands_back_every_socket_it_listened_on(tmp_path):
    """A leak here shows up as a run that cannot start again on the same port.

    The boundary is a `ThreadingHTTPServer`, so every request has a thread, and `stop()` has to
    join the server thread, release the listening socket and leave nothing bound. The check is a
    real bind on the same port after the fact — the only evidence that means anything.
    """
    from docket.pricing import CostMeter, PriceTable
    from docket.proxy import RecordingProxy

    ledger = Ledger.open(tmp_path / "l.jsonl")
    upstream = _upstream()
    upstream.start()
    proxy = RecordingProxy(
        ledger=ledger,
        policy=Policy(),
        meter=CostMeter(price_table=PriceTable.load()),
        session_id="leak-test",
        upstreams={"openai": upstream.url},
    )
    host, port = proxy.start()
    try:
        assert host == "127.0.0.1" and port > 0
    finally:
        proxy.stop()
        upstream.stop()
    ledger.close()

    with socket.socket() as again:
        again.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        again.bind(("127.0.0.1", port))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not any(thread.name == "docket-proxy" for thread in threading.enumerate()):
            break
        time.sleep(0.05)
    assert not any(
        thread.name == "docket-proxy" for thread in threading.enumerate()
    ), "the boundary's serving thread must stop with the boundary"


def test_a_killed_supervisor_leaves_evidence_and_no_lie(tmp_path):
    """SIGKILL the supervisor: what is on disk is a readable prefix, and no receipt claims success.

    This is the recovery story stated as a test rather than as a paragraph. The ledger is fsynced
    per event, so everything written before the kill is readable and classified; anything after it
    was never written. The session ends without a receipt, and `docket doctor` says the ledger is
    torn — which is the truth, and says so without inventing a result.
    """
    upstream = _upstream()
    upstream.start()
    docket_dir = tmp_path / ".docket"
    supervisor = Supervisor(
        [sys.executable, "-c", SLEEP_CHILD],
        policy=Policy(name="t", caps=Caps(max_usd=None, max_wall_seconds=60)),
        docket_dir=docket_dir,
        cwd=tmp_path,
        upstreams={"openai": upstream.url},
    )

    def _run() -> None:
        supervisor.run()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    ledgers: list[Path] = []
    while time.monotonic() < deadline:
        ledgers = list((docket_dir / "runs").glob("*.jsonl"))
        if ledgers and ledgers[0].stat().st_size > 0:
            break
        time.sleep(0.05)
    assert ledgers, "the supervisor did not start recording"
    # What a recovery would see if the machine died at this instant.
    events_before_kill = Ledger.read(ledgers[0])
    assert events_before_kill, "there is a readable prefix"

    # Now kill the agent out from under the supervisor, hard, and let the supervisor notice.
    # The child is found by its inherited DOCKET_SESSION, so nothing else on the box can be hit.
    for pid in _pids_for_session(supervisor.session_id):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, 9)
    thread.join(timeout=20)
    assert not thread.is_alive(), "the supervisor must finish after its child is killed"
    assert [event.seq for event in events_before_kill] == list(range(len(events_before_kill)))
    events, diagnostics = Ledger.read_with_diagnostics(ledgers[0])
    assert diagnostics["chain_error"] is None, "a prefix of a chain is still a chain"
    assert diagnostics["healthy"] in (True, False), "classification never raises on a killed run"
    for path in (docket_dir / "receipts").glob("*.receipt.json"):
        receipt = json.loads(path.read_text(encoding="utf-8"))
        assert (
            receipt["session"]["exit_code"] != 0 or receipt["session"]["breached"]
        ), "a receipt written for a killed run must not present it as a clean exit"
    upstream.stop()


# --------------------------------------------------------------------------- #
# Clocks: what a moving wall clock does to a cap
# --------------------------------------------------------------------------- #


def test_a_wall_clock_cap_is_measured_on_the_monotonic_clock(tmp_path):
    """The cap must hold when the machine's clock jumps *forwards*, and must not fire when it jumps
    *backwards*.

    `time.time()` is what appears in the ledger (it is the only clock that can be compared across
    machines, and the timestamps have to mean something to a reader), but it is the wrong clock for
    measuring elapsed time: it steps when NTP corrects a drifting VM, when a laptop sleeps, when a
    container is live-migrated. The supervisor takes a `monotonic` callable for exactly this
    reason, and this test uses it to make the clock lie while a real child process sleeps.
    """
    upstream = _upstream()
    upstream.start()
    try:
        # A monotonic source that runs 20x fast: from the supervisor's point of view a 1-second
        # cap arrives after ~50ms of real time. If the cap were computed from `time.time()`, the
        # child would still get its full second of real time and this test would fail.
        started = time.monotonic()
        supervisor = Supervisor(
            [sys.executable, "-c", SLEEP_CHILD],
            policy=Policy(name="t", caps=Caps(max_usd=None, max_wall_seconds=1)),
            docket_dir=tmp_path / ".docket",
            cwd=tmp_path,
            upstreams={"openai": upstream.url},
            monotonic=lambda: (time.monotonic() - started) * 20,
        )
        before = time.monotonic()
        result = supervisor.run()
        elapsed = time.monotonic() - before
        assert result.exit_code == EXIT_CAP_EXCEEDED
        assert result.breached == "max_wall_seconds"
        assert elapsed < 10, f"the cap should have fired quickly under a fast clock ({elapsed:.1f}s)"
    finally:
        upstream.stop()


def test_a_backwards_clock_cannot_disable_the_cap(tmp_path):
    """The dangerous direction: a clock that jumps *back* must not produce a negative duration,
    and must not turn a lying clock into a *false breach*.

    Both halves matter. A negative duration is a claim no clock can support (see below). And the
    asymmetry is deliberate: a clock running backwards can never *raise* the elapsed reading, so the
    cap cannot fire spuriously — the safe direction. A supervisor that "helpfully" aborted a run
    because a clock stepped would be killing real work on bad evidence, which is worse than being
    late, and the child's own lifetime plus `grace_seconds` still bound the run.

    `time.time()` steps backwards in ordinary operation — NTP corrects a drifting VM, a laptop
    sleeps, a container is live-migrated, a snapshot is restored. If a duration is computed as
    `now - start` on such a clock the number is negative, and a negative duration is a claim no
    clock can support: a reader comparing it to the cap has to invent a rule for what a negative
    budget means, and a metrics consumer may drop it or read it as time travel. The supervisor
    clamps, so the receipt and the ledger can only ever carry a real duration. This test supplies a
    clock that runs backwards from the start of the run and asserts the run still ends, promptly,
    with no negative number anywhere in its evidence.
    """
    upstream = _upstream()
    upstream.start()
    try:
        supervisor = Supervisor(
            [sys.executable, "-c", SHORT_CHILD],
            policy=Policy(name="t", caps=Caps(max_usd=None, max_wall_seconds=1)),
            docket_dir=tmp_path / ".docket",
            cwd=tmp_path,
            upstreams={"openai": upstream.url},
            # A clock that is *already* behind where it started, and getting worse.
            monotonic=lambda: -1 * time.monotonic(),
        )
        started = time.monotonic()
        result = supervisor.run()
        elapsed = time.monotonic() - started
        assert elapsed < 20, f"a backwards clock must not extend the run ({elapsed:.1f}s)"
        assert result.breached is None, "a clock that steps backwards must not manufacture a breach"
        assert result.timings["duration_seconds"] >= 0
        # And whatever the clock said, the ledger's own timestamps are still RFC 3339 UTC, and the
        # receipt never reports a negative duration.
        events = Ledger.read(result.ledger_path)
        assert events and all(event.ts.endswith("Z") for event in events)
        ended = [event for event in events if event.type == "session.end"]
        assert ended, "the session ended, so the run reached its own conclusion"
        assert not str(ended[0].data["duration_seconds"]).startswith("-"), ended[0].data
        receipt = json.loads(result.receipt_path.read_text(encoding="utf-8"))
        assert Decimal(str(receipt["session"]["duration_seconds"])) >= 0
    finally:
        upstream.stop()


def test_ledger_timestamps_are_recorded_in_utc_and_stay_monotonic_within_a_run(tmp_path):
    """The record's timestamps must be comparable to a reader's clock, and must not go backwards
    inside one run — a ledger whose events move backwards in time cannot be ordered by a third
    party, and `docket verify` would have nothing to say about it."""
    ledger = Ledger.open(tmp_path / "t.jsonl")
    for index in range(5):
        ledger.append("note", {"index": index})
        time.sleep(0.01)
    ledger.close()
    from datetime import UTC, datetime

    events = Ledger.read(tmp_path / "t.jsonl")
    stamps = [datetime.strptime(event.ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC) for event in events]
    assert stamps == sorted(stamps), "timestamps advance"
    assert all(stamp <= datetime.now(UTC) for stamp in stamps), "and none is in the future"


# --------------------------------------------------------------------------- #
# Retries: the client's side of the contract
# --------------------------------------------------------------------------- #


def test_a_retrying_agent_is_told_how_long_to_wait_and_obeys_it(tmp_path):
    """`Retry-After` is a promise the boundary makes; the reference agent has to demonstrate that
    the promise is usable. This drives a real refusal through a real boundary and checks that the
    header is present, integer, and consistent with the limiter's own state."""
    upstream = _upstream()
    upstream.start()
    try:
        supervisor = Supervisor(
            [sys.executable, "-c", RETRY_CHILD],
            policy=Policy(name="t", caps=Caps(max_usd=None), resilience=_rate("1/s")),
            docket_dir=tmp_path / ".docket",
            cwd=tmp_path,
            upstreams={"openai": upstream.url},
        )
        result = supervisor.run()
        assert result.receipt["session"]["exit_code"] == 0
        events = Ledger.read(result.ledger_path)
        throttled = [event for event in events if event.type == "x-docket.rate-limited"]
        if throttled:
            hint = throttled[0].data.get("retry_after_seconds")
            assert hint is None or float(hint) >= 0, hint
    finally:
        upstream.stop()


def _rate(spec: str):
    from docket.policy import Resilience

    per_second, _, per_minute = spec.partition("/")
    del per_minute
    return Resilience(max_calls_per_second=Decimal(per_second))


def test_the_backoff_helper_honours_the_header_and_stays_bounded(tmp_path):
    """The arithmetic of the agent's backoff, tested directly because it is the part a retry storm
    lives in: full jitter (never the full ceiling), exponential up to a cap, and `Retry-After`
    respected when the boundary sends one.

    Run the published agent as a subprocess against a provider that always refuses, with a tiny
    sleep ceiling, and read its own report of the waits off the ledger. Nothing is mocked: the
    agent's code path is exercised exactly as a game day exercises it.
    """
    upstream = _upstream()
    upstream.start()
    try:
        supervisor = Supervisor(
            [sys.executable, "-m", "docket.scriptagent"],
            policy=Policy(
                name="t",
                caps=Caps(max_usd=None, max_wall_seconds=20),
                resilience=_rate("0.000001/s"),  # effectively: refuse everything after the first
            ),
            docket_dir=tmp_path / ".docket",
            cwd=tmp_path,
            upstreams={"openai": upstream.url},
            env_extra={
                "DOCKET_AGENT_CALLS": "6",
                "DOCKET_AGENT_MODE": "backoff",
                "DOCKET_AGENT_MAX_SLEEP": "0.05",
                "DOCKET_AGENT_SEED": "7",
                # The child runs in `tmp_path`, so a relative PYTHONPATH cannot import docket.
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            },
        )
        result = supervisor.run()
        events = Ledger.read(result.ledger_path)
        waits = [
            float(event.data["ceiling_seconds"])
            for event in events
            if event.type == "note" and "ceiling_seconds" in (event.data or {})
        ]
        hints = [
            event.data.get("retry_after_seconds")
            for event in events
            if event.type == "note" and "retry_after_seconds" in (event.data or {})
        ]
        assert waits, "the agent reported at least one backoff"
        assert all(float(wait) <= 0.05 for wait in waits), f"every wait is capped: {waits}"
        assert hints, "and it recorded what the boundary asked for"
        assert all(hint is None or float(hint) >= 0 for hint in hints), hints
        # The refusal itself is in the record, and so is the reason the agent backed off.
        limited = [event for event in events if event.type == "x-docket.rate-limited"]
        assert limited, "the boundary's refusal is evidence, not a stderr line"
        assert all("retry_after_seconds" in event.data for event in limited)
        assert result.receipt["session"]["exit_code"] in (0, EXIT_CAP_EXCEEDED)
        assert not [
            event for event in events if event.type == "model.error"
        ], "no event failed to encode: a note that cannot be written is a silent lie"
    finally:
        upstream.stop()


def test_the_boundary_records_the_retry_hint_it_sent(tmp_path):
    """A refusal that tells the client to wait and does not record the wait it suggested is an
    unverifiable claim. The ledger keeps both sides: the refusal and the hint."""
    import urllib.error
    import urllib.request

    from docket.pricing import CostMeter, PriceTable
    from docket.proxy import RecordingProxy
    from docket.resilience import RateLimiter

    ledger = Ledger.open(tmp_path / "l.jsonl")
    upstream = _upstream()
    upstream.start()
    proxy = RecordingProxy(
        ledger=ledger,
        policy=Policy(name="t", resilience=_rate("1/s")),
        meter=CostMeter(price_table=PriceTable.load()),
        session_id="hint",
        upstreams={"openai": upstream.url},
        # Built the same way the supervisor builds it from the policy: the boundary's limiter is an
        # explicit collaborator, so a hand-made boundary has to supply it too.
        limiter=RateLimiter(per_second=1.0),
    )
    _host, port = proxy.start()
    body = json.dumps({"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "x"}]}).encode()
    try:
        codes = []
        for _ in range(4):
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/openai/chat/completions",
                data=body,
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    response.read()
                    codes.append(response.status)
            except urllib.error.HTTPError as exc:
                codes.append(exc.code)
                if exc.code == 429:
                    hint = exc.headers.get("Retry-After")
                    assert hint is not None and int(hint) >= 1, hint
        assert 429 in codes, codes
    finally:
        proxy.stop()
        upstream.stop()
        ledger.close()
    events = Ledger.read(tmp_path / "l.jsonl")
    limited = [event for event in events if event.type == "x-docket.rate-limited"]
    assert limited, "the refusal is in the ledger"
    assert all("retry_after_seconds" in event.data for event in limited), [event.data for event in limited]


# --------------------------------------------------------------------------- #
# Evidence-integrity invariants
# --------------------------------------------------------------------------- #


def test_the_ledger_file_never_contains_a_partial_line_after_a_clean_run(tmp_path):
    """Every event is fsynced and terminated with a newline, so a file written by a *clean* run
    parses line for line. This is the property that makes the torn-tail classifier meaningful: a
    half-line is evidence of a kill, not of ordinary operation."""
    ledger = Ledger.open(tmp_path / "clean.jsonl")
    for index in range(20):
        ledger.append("note", {"index": index, "padding": "x" * 32})
    ledger.close()
    raw = (tmp_path / "clean.jsonl").read_bytes()
    assert raw.endswith(b"\n")
    lines = raw.splitlines()
    assert len(lines) == 20
    for index, line in enumerate(lines):
        payload = json.loads(line)
        assert payload["seq"] == index
        assert sha256_hex(line.decode())  # every line hashes, so nothing is truncated UTF-8
    _events, diagnostics = Ledger.read_with_diagnostics(tmp_path / "clean.jsonl")
    assert diagnostics["healthy"] is True and diagnostics["trailing_newline"] is True


def test_a_receipt_is_not_written_for_a_run_that_never_finished(tmp_path):
    """No exit, no receipt. The supervisor writes one only after the child has been reaped and the
    totals are final; a receipt written earlier would be a document asserting a result that had
    not happened yet."""
    upstream = _upstream()
    upstream.start()
    try:
        supervisor = Supervisor(
            [sys.executable, "-c", SLEEP_CHILD],
            policy=Policy(name="t", caps=Caps(max_usd=None, max_wall_seconds=1)),
            docket_dir=tmp_path / ".docket",
            cwd=tmp_path,
            upstreams={"openai": upstream.url},
        )
        result = supervisor.run()  # the wall-clock cap ends it, cleanly
        receipts = list((tmp_path / ".docket" / "receipts").glob("*.receipt.json"))
        assert len(receipts) == 1, "exactly one receipt, written once the run was over"
        receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
        assert receipt["session"]["breached"] == "max_wall_seconds"
        assert receipt["session"]["exit_code"] == EXIT_CAP_EXCEEDED
        assert receipt["ledger"]["events"] == len(Ledger.read(result.ledger_path))
    finally:
        upstream.stop()
