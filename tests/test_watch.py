"""`docket watch`: follow a live run with a spend meter.

The roadmap promised it and there was no such command. These tests drive it against a real
supervised run in another thread, because a watcher that has only ever been pointed at a finished
file has not been tested for the thing it exists to do — print events as they arrive.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from docket import events as ev
from docket.pricing import CostMeter, PriceTable
from docket.watch import (
    SHOWN,
    Watcher,
    WatchError,
    newest_ledger,
    render_once,
    resolve_ledger,
    watch,
)

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def a_live_run(tmp_path: Path):
    """A supervised run in another thread, plus the workspace it writes to."""
    from docket.fakeupstream import FakeUpstream, Step
    from docket.policy import Policy
    from docket.supervisor import Supervisor

    agent = tmp_path / "agent.py"
    agent.write_text(
        "import json, os, time, urllib.request\n"
        "base = os.environ['OPENAI_BASE_URL']\n"
        "for _ in range(3):\n"
        "    body = json.dumps({'model': 'gpt-6-luna', 'max_tokens': 16,"
        " 'messages': [{'role': 'user', 'content': 'hi'}]}).encode()\n"
        "    r = urllib.request.Request(base + '/chat/completions', data=body,"
        " headers={'content-type': 'application/json'}, method='POST')\n"
        "    urllib.request.urlopen(r, timeout=30).read()\n"
        "    time.sleep(0.2)\n",
        encoding="utf-8",
    )
    upstream = FakeUpstream(steps=[Step(model="gpt-6-luna", input_tokens=700, output_tokens=140, text="ok")] * 6)
    upstream.start()
    holder: dict[str, Path] = {}

    def run() -> None:
        supervisor = Supervisor(
            [sys.executable, str(agent)],
            policy=Policy(name="watch-test"),
            docket_dir=tmp_path / ".docket",
            cwd=tmp_path,
            upstreams={"openai": upstream.url},
        )
        result = supervisor.run()
        holder["ledger"] = result.ledger_path
        holder["receipt"] = result.receipt_path

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield tmp_path, thread, holder
    finally:
        thread.join(timeout=60)
        upstream.stop()


def _wait_for_ledger(workspace: Path) -> Path:
    runs = workspace / ".docket" / "runs"
    for _ in range(200):
        if runs.is_dir():
            found = sorted(runs.glob("*.jsonl"))
            if found:
                return found[0]
        time.sleep(0.05)
    pytest.fail("no ledger appeared in the supervised run")


# --- following a live run ---------------------------------------------------------------------- #


def test_watch_follows_a_live_run_and_reports_its_end(a_live_run) -> None:
    workspace, _thread, _holder = a_live_run
    _wait_for_ledger(workspace)
    buffer = io.StringIO()
    code = watch(None, out=buffer, cwd=workspace)
    text = buffer.getvalue()
    assert code == 0, text
    assert "following" in text
    assert "model.response" in text, "no metered responses were printed"
    assert "session.end" in text, "the run's end was not reported"
    assert "3 call(s)" in text, f"the meter counted no calls:\n{text}"
    assert "spend $0.000420" in text, f"the running total was wrong:\n{text}"


def test_the_running_total_grows_with_each_call(a_live_run) -> None:
    """A spend meter that does not move is a decoration."""
    workspace, _thread, _holder = a_live_run
    _wait_for_ledger(workspace)
    buffer = io.StringIO()
    watch(None, out=buffer, cwd=workspace)
    lines = [line for line in buffer.getvalue().splitlines() if "model.response" in line]
    assert len(lines) == 3, lines
    totals = [line.split()[0] for line in lines]
    assert totals == ["$0.000140", "$0.000280", "$0.000420"], totals


def test_a_run_that_goes_quiet_is_reported_rather_than_hung(tmp_path: Path) -> None:
    """Silence is the end-of-run signal for a supervisor that exited, and it is said out loud.

    A ledger with no `session.end` is what a killed supervisor leaves behind. The watcher must stop
    and say why, rather than polling a file nobody is going to write to again.
    """
    ledger = tmp_path / "quiet.jsonl"
    ledger.write_text(
        json.dumps(
            {
                "seq": 1,
                "ts": "t",
                "type": ev.SESSION_START,
                "data": {"docket_number": "DKT-Q"},
                "prev": "0" * 64,
                "hash": "h",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    watcher = Watcher(
        meter=CostMeter(price_table=PriceTable.load()),
        price_table=PriceTable.load(),
        quiet_seconds=0.2,
    )
    buffer = io.StringIO()
    started = time.monotonic()
    code = watcher.follow(ledger, buffer)
    assert code == 0
    assert "no new events" in buffer.getvalue(), "the watcher did not say why it stopped"
    assert time.monotonic() - started < 5, "the watcher waited far longer than its quiet window"


def test_a_completed_run_ends_on_session_end_without_waiting(tmp_path: Path) -> None:
    """The quiet timeout is the fallback, not the normal path: `session.end` ends the watch."""
    ledger = tmp_path / "done.jsonl"
    ledger.write_text(
        json.dumps(
            {
                "seq": 1,
                "ts": "t",
                "type": ev.SESSION_START,
                "data": {"docket_number": "DKT-D"},
                "prev": "0" * 64,
                "hash": "h",
            }
        )
        + "\n"
        + json.dumps(
            {
                "seq": 2,
                "ts": "t",
                "type": ev.SESSION_END,
                "data": {"exit_code": 0, "duration_seconds": "1.0"},
                "prev": "h",
                "hash": "i",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    watcher = Watcher(
        meter=CostMeter(price_table=PriceTable.load()),
        price_table=PriceTable.load(),
        quiet_seconds=30.0,
    )
    buffer = io.StringIO()
    started = time.monotonic()
    code = watcher.follow(ledger, buffer)
    assert code == 0
    assert "DKT-D finished" in buffer.getvalue()
    assert time.monotonic() - started < 5, "the watcher waited for the quiet timeout after the run ended"


def test_a_named_ledger_that_does_not_exist_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(WatchError, match="no ledger at"):
        resolve_ledger(str(tmp_path / "absent.jsonl"), tmp_path)


def test_no_ledger_anywhere_is_an_error_that_says_what_to_do(tmp_path: Path) -> None:
    with pytest.raises(WatchError, match="pass --ledger"):
        resolve_ledger(None, tmp_path)


def test_a_torn_tail_does_not_stop_the_rest_of_the_file(tmp_path: Path) -> None:
    """A supervisor killed mid-write leaves a partial line; the events after it still print."""
    ledger = tmp_path / "torn.jsonl"
    good = json.dumps(
        {"seq": 1, "ts": "t", "type": ev.TOOL_CALL, "data": {"name": "sql.query"}, "prev": "0" * 64, "hash": "h"}
    )
    ledger.write_text(good + "\n" + '{"seq": 2, "ts": "t", "type": "no', encoding="utf-8")
    watcher = Watcher(meter=CostMeter(price_table=PriceTable.load()), price_table=PriceTable.load(), quiet_seconds=0.2)
    buffer = io.StringIO()
    watcher.follow(ledger, buffer)
    assert "sql.query" in buffer.getvalue(), "the event before the torn line was not printed"


# --- what is shown ----------------------------------------------------------------------------- #


def test_ledger_plumbing_is_not_shown() -> None:
    """A tail that prints the policy digest is a tail nobody leaves running."""
    assert ev.POLICY_APPLIED not in SHOWN
    assert ev.FS_SNAPSHOT not in SHOWN
    assert ev.MODEL_RESPONSE in SHOWN
    assert ev.CAP_EXCEEDED in SHOWN


def test_a_finished_ledger_renders_the_same_way_a_live_one_does(tmp_path: Path) -> None:
    """`--once` is the same code path, not a second implementation."""
    events = [
        {
            "seq": 1,
            "ts": "t",
            "type": ev.SESSION_START,
            "data": {"docket_number": "DKT-1", "cwd": "/x"},
            "prev": "0" * 64,
            "hash": "h",
        },
        {
            "seq": 2,
            "ts": "t",
            "type": ev.MODEL_RESPONSE,
            "data": {
                "model": "gpt-6-luna",
                "usage": {"input_tokens": 700, "output_tokens": 140},
                "cost_usd": "0.000140",
            },
            "prev": "h",
            "hash": "i",
        },
        {
            "seq": 3,
            "ts": "t",
            "type": ev.SESSION_END,
            "data": {"exit_code": 0, "duration_seconds": "1.000"},
            "prev": "i",
            "hash": "j",
        },
    ]
    text = render_once(events, PriceTable.load())
    assert "model.response" in text
    assert "1 call(s)" in text
    assert "DKT-1 finished" in text, "the docket number from session.start was not carried to the end line"


def test_a_breached_cap_is_shown_in_the_end_line() -> None:
    events = [
        {
            "seq": 1,
            "ts": "t",
            "type": ev.SESSION_START,
            "data": {"docket_number": "DKT-2"},
            "prev": "0" * 64,
            "hash": "h",
        },
        {"seq": 2, "ts": "t", "type": ev.CAP_EXCEEDED, "data": {"cap": "max_usd"}, "prev": "h", "hash": "i"},
        {
            "seq": 3,
            "ts": "t",
            "type": ev.SESSION_END,
            "data": {"exit_code": 42, "duration_seconds": "1.0", "breached": "max_usd"},
            "prev": "i",
            "hash": "j",
        },
    ]
    text = render_once(events, PriceTable.load())
    assert "cap.exceeded" in text
    assert "breached=max_usd" in text, "the end line did not name the cap that fired"


def test_a_response_that_cannot_be_costed_is_reported_not_omitted() -> None:
    """A total that quietly omits a call is worse than a total that says it could not count one."""
    events = [
        {
            "seq": 1,
            "ts": "t",
            "type": ev.MODEL_RESPONSE,
            "data": {"model": "x", "usage": {"input_tokens": "not-a-number"}},
            "prev": "0" * 64,
            "hash": "h",
        },
    ]
    text = render_once(events, PriceTable.load())
    assert "could not be costed" in text, text


# --- the CLI ----------------------------------------------------------------------------------- #


def test_the_cli_watch_once_reads_a_finished_ledger(a_live_run) -> None:
    workspace, thread, holder = a_live_run
    ledger = _wait_for_ledger(workspace)
    thread.join(timeout=60)
    completed = subprocess.run(
        [sys.executable, "-m", "docket", "watch", "--ledger", str(ledger), "--once"],
        cwd=str(workspace),
        env={"PATH": "/tmp/vf/bin:/usr/bin:/bin", "PYTHONPATH": str(REPO / "src")},
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "model.response" in completed.stdout
    assert "call(s)" in completed.stdout


def test_the_cli_says_so_when_there_is_no_ledger(tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "docket", "watch"],
        cwd=str(tmp_path),
        env={"PATH": "/tmp/vf/bin:/usr/bin:/bin", "PYTHONPATH": str(REPO / "src")},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "--ledger" in completed.stderr, "the message did not say what to do"


def test_newest_ledger_picks_the_most_recent(tmp_path: Path) -> None:
    runs = tmp_path / ".docket" / "runs"
    runs.mkdir(parents=True)
    older = runs / "aaa.jsonl"
    newer = runs / "bbb.jsonl"
    older.write_text("{}\n", encoding="utf-8")
    newer.write_text("{}\n", encoding="utf-8")
    import os

    os.utime(older, (1, 1))
    os.utime(newer, (2, 2))
    assert newest_ledger(tmp_path / ".docket") == newer
