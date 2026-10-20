"""The README link check must keep a broken link and an unreachable host apart.

Why this file exists: on the v0.2.3 release gate, `img.shields.io` dropped a connection on one
badge and the check reported `RemoteDisconnected https://img.shields.io/badge/python-3.11%2B-blue`
under "URLs in README.md that do not answer 200". The tag failed on that, with a package and
artifacts that were both correct. `RemoteDisconnected` is an `OSError`, and the handler for
connection-level errors appended to the same list as the handler for status codes, so a host that
never answered was reported as a link that answered wrong.

Both servers here are real loopback sockets, so the code under test takes the path it takes in CI.
"""

from __future__ import annotations

import re
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
LINK_CHECK = REPO / "tests" / "test_doc_links.py"


class _NotFound(BaseHTTPRequestHandler):
    """Answers 404, which is a link that is broken and will answer 404 again."""

    def do_GET(self) -> None:  # noqa: N802 - named by BaseHTTPRequestHandler's protocol
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        return


def _classify(url: str) -> str:
    """Run the link check's own request logic against one URL, in this interpreter's rules.

    The probe re-states the loop rather than importing it, because the loop lives inside a test
    function. If the two drift, the guard below that reads the source is what catches it.
    """
    source = LINK_CHECK.read_text(encoding="utf-8")
    attempts = int(re.search(r"^    attempts = (\d+)$", source, re.M).group(1))
    script = f"""
import sys, urllib.request, urllib.error, time
request = urllib.request.Request(sys.argv[1], method="GET")
for attempt in range({attempts}):
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read(1)
        print("STATUS 200")
        break
    except urllib.error.HTTPError as error:
        print(f"STATUS {{error.code}}")
        break
    except (TimeoutError, urllib.error.URLError, OSError):
        if attempt + 1 < {attempts}:
            time.sleep(0.2)
            continue
        print("CONNECTION unreachable")
"""
    completed = subprocess.run(  # noqa: S603 - a constant probe built from the file under test
        [sys.executable, "-c", script, url],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert lines, f"the probe printed nothing for {url}: {completed.stderr[-400:]}"
    return lines[-1]


def _dropping_server() -> int:
    """Accept a connection and close it without answering, which is what a CDN under load does."""

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)

    def serve() -> None:
        while True:
            try:
                connection, _ = listener.accept()
            except OSError:
                return
            connection.close()

    threading.Thread(target=serve, daemon=True).start()
    return int(listener.getsockname()[1])


def test_a_link_answering_404_is_a_status_failure() -> None:
    """A 404 is an answer, and the answer does not change on a retry, so one request settles it."""
    server = HTTPServer(("127.0.0.1", 0), _NotFound)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        verdict = _classify(f"http://127.0.0.1:{server.server_port}/missing")
    finally:
        server.shutdown()
        server.server_close()
    assert verdict == "STATUS 404", verdict


def test_a_host_that_drops_the_connection_is_not_a_status_failure() -> None:
    """The host never answered, so the check must not claim it answered wrongly."""
    port = _dropping_server()
    verdict = _classify(f"http://127.0.0.1:{port}/badge")
    assert verdict.startswith("CONNECTION "), verdict
    assert "STATUS" not in verdict, verdict


def test_the_check_reports_the_two_outcomes_in_different_lists() -> None:
    """Structural guard: if both handlers append to one list again, the behaviour tests cannot see it.

    This is the part the loopback servers cannot assert, because they exercise one URL at a time
    and the collision happened across URLs.
    """
    source = LINK_CHECK.read_text(encoding="utf-8")
    assert re.search(r"^    unreachable: list\[str\] = \[\]$", source, re.M), (
        "the check has no separate list for hosts that never answered"
    )
    assert 'unreachable.append(f"{type(failure).__name__}' in source, (
        "the connection-level handler no longer records the reason it could not reach the host"
    )
    assert 'broken.append(f"{status} {url}")' in source, "the status handler no longer records the status"


def test_the_connection_failure_is_retried_rather_than_settled_on_the_first_drop() -> None:
    """One dropped packet is weather; three in a row with backoff is a verdict."""
    source = LINK_CHECK.read_text(encoding="utf-8")
    match = re.search(r"^    attempts = (\d+)$", source, re.M)
    assert match, "the link check has no attempt count"
    assert int(match.group(1)) >= 2, f"attempts={match.group(1)}; a single drop is conclusive"
    assert "time.sleep(2**attempt)" in source, "the retries are not backed off"


def test_the_unreachable_list_is_reported_rather_than_discarded() -> None:
    """Not failing the run and hiding the result are different things.

    The link that could not be verified has to reach the person reading the log, or the check
    quietly covers less of the README than it appears to.
    """
    source = LINK_CHECK.read_text(encoding="utf-8")
    assert "if unreachable:" in source, "an unreachable result is discarded rather than reported"
    assert "print(" in source.split("if unreachable:")[1].split("\n\n")[0], (
        "the unreachable branch does not print anything"
    )


if __name__ == "__main__":  # pragma: no cover - `python3 tests/test_doc_links.py`
    raise SystemExit(pytest.main([__file__, "-q", "--tb=short"]))
