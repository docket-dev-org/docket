"""The boundary proxy: pass-through, streaming, enforcement and instrumentation."""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from decimal import Decimal

import pytest

from docket.policy import Policy
from docket.pricing import CostMeter, PriceTable
from docket.proxy import RecordingProxy


def _start(ledger, policy, upstream_url, **kwargs):
    meter = CostMeter(price_table=PriceTable.load())
    proxy = RecordingProxy(
        ledger=ledger, policy=policy, meter=meter, session_id="s1", upstreams={"openai": upstream_url}, **kwargs
    )
    host, port = proxy.start()
    return proxy, meter, f"http://{host}:{port}"


def _post(url, payload, timeout=15):
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read().decode("utf-8"), dict(response.headers)


def test_passthrough_records_a_model_call(ledger, fake_upstream):
    proxy, meter, base = _start(ledger, Policy(), fake_upstream.url)
    try:
        status, body, _ = _post(f"{base}/openai/v1/chat/completions", {"model": "claude-sonnet-5", "messages": []})
    finally:
        proxy.stop()
    assert status == 200
    assert json.loads(body)["choices"][0]["message"]["content"]
    types = [e.type for e in ledger]
    assert types == ["model.request", "model.response"]
    response = ledger.events[1]
    assert response.data["usage"] == {"input_tokens": 1000, "cached_input_tokens": 0, "output_tokens": 200}
    assert response.data["usage_source"] == "reported"
    assert meter.calls == 1
    assert Decimal(response.data["cost_usd"]) > 0


def test_auth_headers_are_forwarded_but_never_recorded(ledger, fake_upstream):
    proxy, _, base = _start(ledger, Policy(), fake_upstream.url)
    request = urllib.request.Request(
        f"{base}/openai/v1/chat/completions",
        data=json.dumps({"model": "claude-sonnet-5"}).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer sk-secret-value"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            response.read()
    finally:
        proxy.stop()
    assert fake_upstream.requests[-1]["headers"].get("Authorization") == "Bearer sk-secret-value"
    for event in ledger:
        assert "sk-secret-value" not in json.dumps(event.to_dict())


def test_bodies_are_not_recorded_by_default(ledger, fake_upstream):
    proxy, _, base = _start(ledger, Policy(), fake_upstream.url)
    try:
        _post(
            f"{base}/openai/v1/chat/completions",
            {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "TOPSECRET"}]},
        )
    finally:
        proxy.stop()
    assert "TOPSECRET" not in json.dumps([e.to_dict() for e in ledger])
    assert ledger.events[0].data["request_sha256"]


def test_record_bodies_opt_in(ledger, fake_upstream):
    proxy, _, base = _start(ledger, Policy(), fake_upstream.url, record_bodies=True)
    try:
        _post(
            f"{base}/openai/v1/chat/completions",
            {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "TOPSECRET"}]},
        )
    finally:
        proxy.stop()
    assert "TOPSECRET" in json.dumps([e.to_dict() for e in ledger])


def test_streaming_is_passed_through_and_usage_is_parsed(ledger, fake_upstream):
    proxy, meter, base = _start(ledger, Policy(), fake_upstream.url)
    try:
        status, body, headers = _post(
            f"{base}/openai/v1/chat/completions", {"model": "claude-sonnet-5", "stream": True}
        )
    finally:
        proxy.stop()
    assert status == 200
    assert "text/event-stream" in headers.get("Content-Type", "")
    assert "[DONE]" in body
    assert ledger.events[0].data["streaming"] is True
    assert ledger.events[1].data["usage_source"] == "reported"
    assert meter.calls == 1


def test_model_deny_rule_blocks_before_forwarding(ledger, fake_upstream):
    policy = Policy.from_dict({"models": {"deny": ["*sonnet*"]}})
    proxy, _, base = _start(ledger, policy, fake_upstream.url)
    try:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _post(f"{base}/openai/v1/chat/completions", {"model": "claude-sonnet-5"})
        assert excinfo.value.code == 403
        payload = json.loads(excinfo.value.read().decode())
        assert payload["error"]["type"] == "docket_policy_denied"
    finally:
        proxy.stop()
    assert [e.type for e in ledger] == ["model.denied"]
    assert fake_upstream.requests == [], "a denied call must never reach the provider"


def test_cap_blocks_the_next_call_after_being_crossed(ledger, fake_upstream):
    policy = Policy.from_dict({"caps": {"max_usd": 0.0001, "on_breach": "deny"}})
    proxy, meter, base = _start(ledger, policy, fake_upstream.url)
    try:
        _post(f"{base}/openai/v1/chat/completions", {"model": "claude-sonnet-5"})
        assert meter.cost > Decimal("0")
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _post(f"{base}/openai/v1/chat/completions", {"model": "claude-sonnet-5"})
        assert excinfo.value.code == 403
        assert json.loads(excinfo.value.read().decode())["error"]["code"] == "max_usd"
    finally:
        proxy.stop()
    types = [e.type for e in ledger]
    assert "cap.exceeded" in types
    assert "model.denied" in types
    assert len(fake_upstream.requests) == 1


def test_breach_callback_fires_once(ledger, fake_upstream):
    seen: list[str] = []
    policy = Policy.from_dict({"caps": {"max_usd": 0.0001}})
    proxy, _, base = _start(ledger, policy, fake_upstream.url, on_breach=seen.append)
    try:
        _post(f"{base}/openai/v1/chat/completions", {"model": "claude-sonnet-5"})
        _post(f"{base}/openai/v1/chat/completions", {"model": "claude-sonnet-5"})
    except urllib.error.HTTPError:
        pass
    finally:
        proxy.stop()
    assert seen == ["max_usd"]


def test_instrumentation_endpoint_records_tool_calls(ledger):
    proxy, _, base = _start(ledger, Policy(), "http://127.0.0.1:1")
    try:
        status, body, _ = _post(f"{base}/docket/tool/call", {"name": "sql", "args_digest": "abc"})
        assert status == 200 and json.loads(body)["ok"] is True
        status, _, _ = _post(f"{base}/docket/note", {"message": "checkpoint"})
        assert status == 200
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _post(f"{base}/docket/nonsense", {})
        assert excinfo.value.code == 404
    finally:
        proxy.stop()
    kinds = [e.type for e in ledger]
    assert kinds == ["tool.call", "note"]
    assert ledger.events[0].data["reported_by"] == "agent"


def test_unknown_route_returns_404(ledger):
    proxy, _, base = _start(ledger, Policy(), "http://127.0.0.1:1")
    try:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _post(f"{base}/not-a-provider/v1/chat/completions", {})
        assert excinfo.value.code == 404
    finally:
        proxy.stop()


def test_egress_denied_for_unlisted_hosts_in_proxy_mode(ledger):
    policy = Policy.from_dict({"egress": {"mode": "deny", "allow": ["api.openai.com"]}})
    proxy, _, base = _start(ledger, policy, "http://127.0.0.1:1")
    host, port = base.split("//")[1].split(":")
    try:
        with socket.create_connection((host, int(port)), timeout=10) as sock:
            sock.sendall(b"CONNECT evil.example.com:443 HTTP/1.1\r\nHost: evil.example.com:443\r\n\r\n")
            data = sock.recv(4096).decode("utf-8", "replace")
    finally:
        proxy.stop()
    assert "403" in data
    assert [e.type for e in ledger] == ["net.denied"]
    assert ledger.events[0].data["host"] == "evil.example.com:443"


def test_model_hosts_are_allowed_to_tunnel(ledger):
    policy = Policy.from_dict({"egress": {"mode": "deny", "allow": []}})
    proxy, _, _ = _start(ledger, policy, "https://api.openai.com")
    try:
        allowed, reason = proxy.decide_egress("api.openai.com:443")
        assert allowed and "model host" in reason
        allowed, _ = proxy.decide_egress("evil.example.com:443")
        assert not allowed
    finally:
        proxy.stop()


def test_resolve_maps_provider_prefixes_and_absolute_urls(ledger):
    proxy, _, _ = _start(ledger, Policy(), "https://api.openai.com")
    try:
        assert proxy.resolve("/openai/v1/chat/completions") == ("openai", "/v1/chat/completions")
        assert proxy.resolve("/anthropic/v1/messages") == ("anthropic", "/v1/messages")
        provider, path = proxy.resolve("http://api.openai.com/v1/models?x=1")
        assert provider == "openai" and path == "/v1/models?x=1"
        assert proxy.resolve("/nothing-here") == (None, "/nothing-here")
    finally:
        proxy.stop()


def test_upstream_failure_is_recorded_as_a_model_error(ledger):
    proxy, _, base = _start(ledger, Policy(), "http://127.0.0.1:9")  # nothing listens here
    try:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _post(f"{base}/openai/v1/chat/completions", {"model": "claude-sonnet-5"})
        assert excinfo.value.code == 502
    finally:
        proxy.stop()
    assert "model.error" in [e.type for e in ledger]


def test_usage_estimate_is_marked_when_the_provider_reports_nothing(ledger):
    """A provider response with no usage block must not be presented as metered."""

    class SilentUpstream:
        def __init__(self):
            import http.server
            import threading

            class Handler(http.server.BaseHTTPRequestHandler):
                protocol_version = "HTTP/1.1"

                def log_message(self, *args):
                    return

                def do_POST(self):  # noqa: N802
                    length = int(self.headers.get("Content-Length") or 0)
                    self.rfile.read(length)
                    body = json.dumps({"choices": [{"message": {"content": "hi there"}}]}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            self.server.daemon_threads = True
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

        def stop(self):
            self.server.shutdown()

    upstream = SilentUpstream()
    proxy, _, base = _start(ledger, Policy(), upstream.url)
    try:
        _post(
            f"{base}/openai/v1/chat/completions",
            {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "x" * 400}]},
        )
    finally:
        proxy.stop()
        upstream.stop()
    assert ledger.events[1].data["usage_source"] == "estimated"
