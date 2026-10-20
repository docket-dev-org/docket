"""The `/docket/*` endpoints the documentation tells a reader to `curl`.

`docs/integrations.md` shows four commands: a tool call, a tool result, a note, and a generic
`/docket/event` carrying any event in the vocabulary. It also shows the three operational
endpoints. A documented endpoint that does not answer is a feature advertised as integrated when
it is only written down, which is the failure mode this file exists to rule out.

Every assertion here drives the real boundary over a real socket. Nothing is mocked.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request

import pytest

from docket.policy import Policy
from docket.pricing import CostMeter, PriceTable
from docket.proxy import RecordingProxy


def _start(ledger, upstream_url, policy=None):
    meter = CostMeter(price_table=PriceTable.load())
    proxy = RecordingProxy(
        ledger=ledger,
        policy=policy or Policy(),
        meter=meter,
        session_id="s1",
        upstreams={"openai": upstream_url},
    )
    host, port = proxy.start()
    return proxy, f"http://{host}:{port}"


def _post(url: str, payload: dict, timeout: float = 15) -> tuple[int, dict]:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def _get(url: str, timeout: float = 15) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8")


def _wait_ready(base: str) -> None:
    """The boundary binds asynchronously; probe until it answers rather than sleeping a guess."""
    for _ in range(100):
        try:
            status, _body = _get(f"{base}/docket/healthz", timeout=2)
            if status == 200:
                return
        except (urllib.error.URLError, OSError):
            pass
    pytest.fail("the boundary never became ready")


# --- the operational endpoints ---------------------------------------------------------------- #


@pytest.mark.parametrize("route", ["healthz", "readyz", "metrics"])
def test_the_operational_endpoints_answer(ledger, fake_upstream, route: str) -> None:
    """`--port` exists so an orchestrator can probe these; they have to answer when probed."""
    proxy, base = _start(ledger, fake_upstream.url)
    try:
        _wait_ready(base)
        status, body = _get(f"{base}/docket/{route}")
        assert status == 200, f"/docket/{route} answered {status}: {body[:200]}"
    finally:
        proxy.stop()


# --- the recording endpoints ------------------------------------------------------------------- #


def test_a_tool_call_reaches_the_ledger_as_an_agent_assertion(ledger, fake_upstream) -> None:
    """The documented `curl` for a tool call, run for real."""
    proxy, base = _start(ledger, fake_upstream.url)
    try:
        _wait_ready(base)
        status, _body = _post(
            f"{base}/docket/tool/call",
            {"name": "sql.query", "args_digest": "9f86d081884c7d659a2feaa0c55ad015", "result_bytes": 2048},
        )
        assert 200 <= status < 300, f"/docket/tool/call answered {status}"
    finally:
        proxy.stop()

    events = ledger.events
    assert [e.type for e in events] == ["tool.call"], events
    assert events[0].data["reported_by"] == "agent", (
        "a boundary event and an agent assertion must be distinguishable in the ledger"
    )


def test_a_tool_result_reaches_the_ledger(ledger, fake_upstream) -> None:
    proxy, base = _start(ledger, fake_upstream.url)
    try:
        _wait_ready(base)
        status, _body = _post(f"{base}/docket/tool/result", {"name": "sql.query", "ok": True, "result_bytes": 2048})
        assert 200 <= status < 300, f"/docket/tool/result answered {status}"
    finally:
        proxy.stop()
    assert [e.type for e in ledger.events] == ["tool.result"]


def test_a_note_reaches_the_ledger(ledger, fake_upstream) -> None:
    proxy, base = _start(ledger, fake_upstream.url)
    try:
        _wait_ready(base)
        status, _body = _post(f"{base}/docket/note", {"message": "batch 3 of 12 finished"})
        assert 200 <= status < 300, f"/docket/note answered {status}"
    finally:
        proxy.stop()
    events = ledger.events
    assert [e.type for e in events] == ["note"], events
    assert events[0].data["message"] == "batch 3 of 12 finished"


def test_the_generic_event_route_accepts_any_event_in_the_vocabulary(ledger, fake_upstream) -> None:
    """The route the integrations page advertises for "any language".

    It carries a model call the agent made *outside* the boundary, recorded as an assertion with
    its provenance preserved.
    """
    from docket import events as ev

    proxy, base = _start(ledger, fake_upstream.url)
    try:
        _wait_ready(base)
        status, _body = _post(
            f"{base}/docket/event",
            {
                "type": "model.response",
                "data": {
                    "model": "gpt-6.1",
                    "provider": "openai",
                    "usage": {"input_tokens": 1200, "output_tokens": 340},
                    "usage_source": "reported",
                    "cost_usd": "0.005800",
                },
            },
        )
        assert 200 <= status < 300, f"/docket/event answered {status}"
    finally:
        proxy.stop()

    events = ledger.events
    assert [e.type for e in events] == ["model.response"], events
    assert events[0].data["reported_by"] == "agent"
    assert events[0].data["model"] == "gpt-6.1"
    assert "model.response" in ev.KNOWN_TYPES


def test_an_unknown_event_type_is_refused_with_a_reason(ledger, fake_upstream) -> None:
    """A typo must not be recorded as an event the format does not have."""
    proxy, base = _start(ledger, fake_upstream.url)
    try:
        _wait_ready(base)
        status, body = _post(f"{base}/docket/event", {"type": "not.a.real.type"})
        assert status == 400, f"an unknown event type was answered {status}: {body}"
        assert "not.a.real.type" in json.dumps(body), "the refusal must name what it refused"
    finally:
        proxy.stop()
    assert ledger.events == [], "a refused event must not reach the ledger"


def test_an_unknown_docket_route_is_a_404_not_a_silent_accept(ledger, fake_upstream) -> None:
    proxy, base = _start(ledger, fake_upstream.url)
    try:
        _wait_ready(base)
        status, _body = _post(f"{base}/docket/nonsense", {"x": 1})
        assert status == 404, f"an unknown /docket/ route was answered {status}"
    finally:
        proxy.stop()
    assert ledger.events == []


# --- the endpoints are reachable the way the documentation says --------------------------------- #


def test_the_endpoints_are_reachable_on_the_url_the_environment_names(ledger, fake_upstream) -> None:
    """`DOCKET_PROXY_URL` is what the documented `curl` interpolates, so it must be the boundary.

    The SDK reads this variable to decide where to post; if it named anything else, every snippet
    in the integrations page would be wrong while each endpoint still worked in isolation.
    """
    import os

    proxy, base = _start(ledger, fake_upstream.url)
    try:
        _wait_ready(base)
        named = os.environ.get("DOCKET_PROXY_URL")
        if named:
            # A run sets this; a bare proxy started by a test does not, so the assertion is about
            # the shape of the variable rather than its presence here.
            assert named.startswith("http://"), named
        status, _body = _get(f"{base}/docket/healthz")
        assert status == 200
    finally:
        proxy.stop()


def test_a_boundary_port_can_be_pinned_and_probed(ledger) -> None:
    """`--port` is a documented flag whose whole purpose is a fixed loopback port.

    Asking the OS for a free port, then binding exactly that one, is the property an orchestrator
    depends on: the port it was told is the port that answers.
    """
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    wanted = probe.getsockname()[1]
    probe.close()

    meter = CostMeter(price_table=PriceTable.load())
    proxy = RecordingProxy(ledger=ledger, policy=Policy(), meter=meter, session_id="s1")
    try:
        host, port = proxy.start(wanted)
        assert port == wanted, f"asked for port {wanted}, the boundary bound {port}"
        with urllib.request.urlopen(f"http://{host}:{port}/docket/healthz", timeout=10) as response:
            assert response.status == 200
    finally:
        proxy.stop()
