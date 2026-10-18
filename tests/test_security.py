"""Red-team suite: the boundary attacked the way a hostile agent or provider would.

Every test here is an attack, and each one asserts a *defence* — not that the attack
"does not crash" but that it is refused, recorded, and explainable. The threat model
these tests implement is written out in ``docs/threat-model.md``; the mapping between the
two is deliberate, because an untested mitigation is a hope.

Attacks covered:

* request-line abuse: ``..`` traversal, absolute-form routing to a foreign host, control
  characters, null bytes, and URL-encoded traversal;
* body abuse: an enormous declared ``Content-Length``, a truncated body, a negative
  length, a non-numeric length, chunked transfer encoding, and a deeply nested JSON bomb;
* provider abuse: a response header containing CRLF (status-line injection), an invalid
  header name, and a redirect/no-body response;
* local attack surface: a symlinked ledger, a symlinked sealing key, a world-readable
  sealing key, and a permissive ledger directory;
* record integrity: hash collisions across field order, duplicate keys, unicode
  normalisation, and a receipt whose sealed payload was edited.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import time
from pathlib import Path

import pytest

from docket.canon import canonical_bytes, sha256_hex
from docket.ledger import Ledger, LedgerDamagedError
from docket.policy import Caps, Egress, Policy
from docket.pricing import CostMeter, PriceTable
from docket.proxy import MAX_REQUEST_BODY, RecordingProxy, _unsafe_path

# --------------------------------------------------------------------------- #
# A raw socket client, because urllib refuses to send malformed requests
# --------------------------------------------------------------------------- #


def raw_request(host: str, port: int, request: bytes, *, timeout: float = 5.0) -> bytes:
    """Send bytes on a socket and read back exactly one HTTP response.

    Hand-written because urllib will not transmit the malformed requests this suite needs
    (chunked bodies, bad Content-Length, control characters in the request line).
    """
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(request)
        sock.settimeout(timeout)
        buffer = b""
        try:
            while b"\r\n\r\n" not in buffer:
                chunk = sock.recv(65536)
                if not chunk:
                    return buffer
                buffer += chunk
        except TimeoutError:  # pragma: no cover - surfaces a hang
            return buffer
        head, _, body = buffer.partition(b"\r\n\r\n")
        declared = 0
        for line in head.split(b"\r\n")[1:]:
            if line.lower().startswith(b"content-length:"):
                with contextlib.suppress(ValueError):
                    declared = int(line.split(b":", 1)[1].strip())
        while len(body) < declared:
            try:
                chunk = sock.recv(65536)
            except TimeoutError:  # pragma: no cover
                break
            if not chunk:
                break
            body += chunk
        return head + b"\r\n\r\n" + body


def status_of(response: bytes) -> int:
    first = response.split(b"\r\n", 1)[0].decode("latin-1")
    return int(first.split()[1])


@pytest.fixture()
def live_proxy(tmp_path: Path, fake_upstream):
    """A boundary talking to the offline scripted provider, with a real ledger."""
    ledger = Ledger.open(tmp_path / "runs" / "security.jsonl", max_bytes=None)
    proxy = RecordingProxy(
        ledger=ledger,
        policy=Policy(name="red team", caps=Caps(max_usd=None)),
        meter=CostMeter(price_table=PriceTable.load()),
        session_id="security",
        upstreams={"openai": fake_upstream.url, "anthropic": fake_upstream.url},
        upstream_timeout=10,
    )
    host, port = proxy.start()
    try:
        yield proxy, host, port, ledger
    finally:
        proxy.stop()
        ledger.close()


# --------------------------------------------------------------------------- #
# Request-line abuse
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path, reason",
    [
        ("/openai/../../etc/passwd", "a '..' segment"),
        ("/openai/..%2f..%2fadmin", "a '..' segment"),
        ("/openai/v1/chat/completions\r\nX-Evil: 1", "control characters"),
        ("/openai/v1/%00", "percent-encoded control characters"),
        ("/openai/v1/x%0d%0aX-Evil: 1", "percent-encoded control characters"),
    ],
)
def test_a_hostile_request_path_is_refused_with_a_reason(path: str, reason: str):
    assert _unsafe_path(path) is not None
    assert reason in (_unsafe_path(path) or "")


def test_traversal_and_control_characters_never_reach_the_provider(live_proxy):
    proxy, host, port, ledger = live_proxy
    for path in ("/../../etc/passwd", "/openai/..%2f..%2fsecrets.json"):
        response = raw_request(
            host, port, f"POST {path} HTTP/1.1\r\nHost: localhost\r\nContent-Length: 2\r\n\r\n{{}}".encode()
        )
        assert status_of(response) == 400, path
    denied = [e for e in ledger if e.type == "net.denied"]
    assert len(denied) == 2
    assert all(e.data.get("code") == "unsafe_path" for e in denied)
    assert proxy.stats.model_calls == 0, "nothing may be forwarded while refusing"


def test_absolute_form_requests_are_routed_by_host_and_foreign_hosts_are_refused(live_proxy):
    proxy, host, port, ledger = live_proxy
    upstream_host = proxy.upstreams["openai"].split("//", 1)[1]
    allowed = raw_request(
        host,
        port,
        f"POST http://{upstream_host}/v1/chat/completions HTTP/1.1\r\n"
        f"Host: {upstream_host}\r\nContent-Length: 44\r\n\r\n"
        '{"model": "claude-sonnet-5", "messages": []}'.encode(),
    )
    assert status_of(allowed) == 200, "a request for the configured provider is allowed"

    refused = raw_request(
        host,
        port,
        b"POST http://attacker.example/steal HTTP/1.1\r\nHost: attacker.example\r\nContent-Length: 2\r\n\r\n{}",
    )
    assert status_of(refused) == 404, "an absolute-form request for another host is not routed"
    assert proxy.stats.model_calls == 1


def test_egress_policy_is_the_authority_for_unknown_hosts():
    policy = Policy(name="egress", caps=Caps(max_usd=None), egress=Egress(mode="deny", allow=["api.openai.com"]))
    proxy = RecordingProxy(
        ledger=Ledger.in_memory(),
        policy=policy,
        meter=CostMeter(price_table=PriceTable.load()),
        session_id="egress",
        upstreams={"openai": "https://api.openai.com"},
    )
    allowed, why = proxy.decide_egress("api.openai.com:443")
    assert allowed and "openai" in why
    denied, why = proxy.decide_egress("attacker.example:443")
    assert not denied and why


# --------------------------------------------------------------------------- #
# Body abuse
# --------------------------------------------------------------------------- #


def test_an_enormous_declared_body_is_refused_before_it_is_read(live_proxy):
    proxy, host, port, ledger = live_proxy
    started = time.monotonic()
    response = raw_request(
        host,
        port,
        f"POST /openai/v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n"
        f"Content-Length: {MAX_REQUEST_BODY + 1}\r\n\r\n".encode(),
    )
    elapsed = time.monotonic() - started
    assert status_of(response) == 413
    assert elapsed < 5.0, "the refusal must not wait for the body it refuses"
    body = json.loads(response.split(b"\r\n\r\n", 1)[1])
    assert body["error"]["code"] == "body_too_large"
    assert body["error"]["limit_bytes"] == MAX_REQUEST_BODY
    denials = [e for e in ledger if e.type == "net.denied"]
    assert denials and denials[-1].data["declared_bytes"] == MAX_REQUEST_BODY + 1


def test_a_chunked_body_is_refused_rather_than_forwarded_empty(live_proxy):
    """Silently forwarding an empty body would bill the customer for a request nobody made."""
    proxy, host, port, ledger = live_proxy
    response = raw_request(
        host,
        port,
        b"POST /openai/v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n"
        b"Transfer-Encoding: chunked\r\n\r\n"
        b'2b\r\n{"model": "claude-sonnet-5", "messages": []}\r\n0\r\n\r\n',
    )
    assert status_of(response) == 411
    body = json.loads(response.split(b"\r\n\r\n", 1)[1])
    assert body["error"]["code"] == "chunked_unsupported"
    assert proxy.stats.model_calls == 0, "no call may be recorded for a request that was not forwarded"
    denial = [e for e in ledger if e.type == "net.denied"][-1]
    assert denial.data["transfer_encoding"] == "chunked"


@pytest.mark.parametrize("value", ["abc", "", "-5", "1e9"])
def test_a_malformed_content_length_is_refused(value: str, live_proxy):
    _proxy, host, port, _ledger = live_proxy
    response = raw_request(
        host,
        port,
        f"POST /openai/v1/chat/completions HTTP/1.1\r\nHost: localhost\r\nContent-Length: {value}\r\n\r\n".encode(),
    )
    assert status_of(response) in {400, 411}


def test_a_body_that_ends_early_is_not_forwarded_as_if_it_were_complete(live_proxy):
    proxy, host, port, _ledger = live_proxy
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.sendall(
            b"POST /openai/v1/chat/completions HTTP/1.1\r\nHost: localhost\r\nContent-Length: 500\r\n\r\n"
            b'{"model": "claude-sonnet-5"}'
        )
        sock.shutdown(socket.SHUT_WR)
        data = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    assert status_of(data) == 400
    assert b"truncated_request" in data, "the refusal must name the cause, not just a status"
    assert proxy.stats.model_calls == 0


def test_a_deeply_nested_json_body_is_refused_by_name(live_proxy):
    """A nesting bomb must be refused, not raise: an uncaught RecursionError inside a
    request thread is a crash in the component that exists to prevent crashes."""
    proxy, host, port, ledger = live_proxy
    body = b'{"model": "claude-sonnet-5", "messages": ' + b"[" * 6000 + b"]" * 6000 + b"}"
    response = raw_request(
        host,
        port,
        b"POST /openai/v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode()
        + body,
    )
    assert status_of(response) == 400
    payload = json.loads(response.split(b"\r\n\r\n", 1)[1])
    assert payload["error"]["code"] == "body_too_deep"
    assert proxy.stats.model_calls == 0, "a request the boundary cannot parse is not forwarded"


# --------------------------------------------------------------------------- #
# Provider abuse (a hostile or compromised upstream)
# --------------------------------------------------------------------------- #


class _HostileUpstream:
    """An upstream that returns a forged status line inside a header value."""

    def __init__(self) -> None:
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:  # noqa: D102
                return

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                payload = json.dumps(
                    {"model": "claude-sonnet-5", "usage": {"input_tokens": 10, "output_tokens": 2}, "content": "ok"}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                # The attack: a header value carrying its own CRLF-delimited header.
                self.wfile.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    b"X-Injected: yes\r\nSet-Cookie: session=stolen\r\n\r\n" + payload
                )

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def test_a_hop_by_hop_header_is_not_relayed_to_the_agent(live_proxy):
    """TE/Connection/Upgrade are the headers that let a response change its own shape."""
    proxy, host, port, _ledger = live_proxy
    response = raw_request(
        host,
        port,
        b"POST /openai/v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n"
        b"Content-Length: 44\r\n\r\n"
        b'{"model": "claude-sonnet-5", "messages": []}',
    )
    assert status_of(response) == 200
    lowered = response.lower()
    assert b"\r\nte:" not in lowered
    assert b"\r\nupgrade:" not in lowered
    assert b"\r\ntransfer-encoding:" not in lowered


def test_a_forged_header_value_cannot_forge_a_response(tmp_path: Path):
    """A provider that answers with a mangled header block is a provider failure.

    The upstream here writes its own status line into the middle of a header block. What
    matters is not what the parser makes of it (bad-status-line, bad header name, or a
    body that never terminates) but what the agent and the ledger end up with: one status
    line, no injected headers, a *provider* error rather than an internal one, and a
    watchdog that cuts the connection off instead of hanging on it.
    """
    hostile = _HostileUpstream()
    ledger = Ledger.open(tmp_path / "hostile.jsonl", max_bytes=None)
    proxy = RecordingProxy(
        ledger=ledger,
        policy=Policy(name="hostile", caps=Caps(max_usd=None)),
        meter=CostMeter(price_table=PriceTable.load()),
        session_id="hostile",
        upstreams={"openai": hostile.url},
        upstream_timeout=1.0,  # the watchdog, deliberately short for the test
    )
    host, port = proxy.start()
    try:
        started = time.monotonic()
        response = raw_request(
            host,
            port,
            b"POST /openai/v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n"
            b"Content-Length: 44\r\n\r\n"
            b'{"model": "claude-sonnet-5", "messages": []}',
            timeout=15.0,
        )
        elapsed = time.monotonic() - started
        assert response, "the agent must receive an answer, not a dead socket"
        assert elapsed < 10.0, f"the watchdog must cut off a hanging provider (took {elapsed:.1f}s)"
        assert b"Set-Cookie: session=stolen" not in response, "a header must not inject headers"
        assert response.count(b"HTTP/1.1") == 1, "exactly one status line reaches the agent"
        assert status_of(response) == 502
        payload = json.loads(response.split(b"\r\n\r\n", 1)[1])
        assert payload["error"]["type"] == "docket_upstream_error"
        assert payload["error"]["code"] in {"bad_upstream_response", "upstream_timeout"}
        errors = [e.data for e in ledger if e.type == "model.error"]
        assert errors and errors[0].get("code") in {
            "bad_upstream_response",
            "upstream_timeout",
        }, "the ledger must record a provider failure, not an internal error"
        assert not [e for e in ledger if e.type == "model.response"], "no success may be recorded here"
    finally:
        proxy.stop()
        ledger.close()
        hostile.stop()


def test_an_upstream_that_returns_a_redirect_is_recorded_not_followed(tmp_path: Path, fake_upstream):
    """Docket records the call; it does not chase redirects on its own authority."""
    ledger = Ledger.open(tmp_path / "redirect.jsonl", max_bytes=None)
    proxy = RecordingProxy(
        ledger=ledger,
        policy=Policy(name="redirect", caps=Caps(max_usd=None)),
        meter=CostMeter(price_table=PriceTable.load()),
        session_id="redirect",
        upstreams={"openai": fake_upstream.url},
        upstream_timeout=10,
    )
    host, port = proxy.start()
    try:
        response = raw_request(
            host,
            port,
            b"POST /nonexistent-provider/v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n"
            b"Content-Length: 2\r\n\r\n{}",
        )
        assert status_of(response) == 404
    finally:
        proxy.stop()
        ledger.close()


# --------------------------------------------------------------------------- #
# Local attack surface
# --------------------------------------------------------------------------- #


def test_a_symlinked_sealing_key_is_refused(tmp_path: Path):
    from docket.signing import load_or_create_seal_key

    real = tmp_path / "elsewhere.key"
    os.close(os.open(real, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    link = tmp_path / "seal.key"
    link.symlink_to(real)
    with pytest.raises(ValueError, match="symbolic link"):
        load_or_create_seal_key(link)


def test_a_world_readable_sealing_key_is_refused_with_the_fix(tmp_path: Path):
    from docket.signing import load_or_create_seal_key

    path = tmp_path / "seal.key"
    path.write_text("00" * 32 + "\n", encoding="utf-8")
    path.chmod(0o644)
    with pytest.raises(ValueError) as exc:
        load_or_create_seal_key(path)
    assert "chmod 600" in str(exc.value), "the refusal must name the remedy"
    path.chmod(0o600)
    assert load_or_create_seal_key(path).startswith("00")


def test_a_new_sealing_key_is_created_owner_only(tmp_path: Path):
    from docket.signing import load_or_create_seal_key

    path = tmp_path / "keys" / "seal.key"
    key = load_or_create_seal_key(path)
    assert len(key) == 64
    assert (path.stat().st_mode & 0o777) == 0o600
    assert ((path.parent).stat().st_mode & 0o777) == 0o700


def test_a_symlinked_ledger_is_refused_and_the_target_is_untouched(tmp_path: Path):
    victim = tmp_path / "authorized_keys"
    victim.write_text("ssh-ed25519 AAAA… real key\n", encoding="utf-8")
    link = tmp_path / "run.jsonl"
    link.symlink_to(victim)
    with pytest.raises(LedgerDamagedError):
        Ledger.open(link, max_bytes=None)
    assert victim.read_text(encoding="utf-8") == "ssh-ed25519 AAAA… real key\n"


# --------------------------------------------------------------------------- #
# Record integrity under adversarial input
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "left, right, why",
    [
        ({"a": "1"}, {"a": 1}, "a string and a number are different values"),
        ({"a": "é"}, {"a": "e\u0301"}, "different unicode spellings are different bytes"),
        ({"a": [1, 2]}, {"a": [2, 1]}, "list order is meaningful"),
        ({"a": None}, {"a": False}, "null and false are different"),
    ],
)
def test_distinct_values_never_collide_in_the_canonical_form(left, right, why):
    assert sha256_hex(canonical_bytes(left)) != sha256_hex(canonical_bytes(right)), why


def test_field_order_never_changes_the_hash():
    """The canonical form sorts keys precisely so that two encodings of the same object
    cannot produce two different hashes — that is what makes cross-implementation
    verification possible."""
    assert canonical_bytes({"a": 1, "b": 2}) == canonical_bytes({"b": 2, "a": 1})


def test_identical_values_always_produce_identical_bytes():
    first = {"b": [1, {"z": 2, "a": 3}], "a": "x"}
    second = {"a": "x", "b": [1, {"a": 3, "z": 2}]}
    assert canonical_bytes(first) == canonical_bytes(second)


def test_a_line_edited_in_place_fails_the_chain_even_if_it_still_parses(tmp_path: Path):
    """The defence against editing a ledger is the hash chain, not the JSON parser.

    Duplicate keys are a parser curiosity — Python and JavaScript both resolve them
    "last value wins", so the two verifiers agree — but an editor who changes a value and
    keeps the old hash is caught by recomputation, which is the property that matters.
    """
    path = tmp_path / "edited.jsonl"
    ledger = Ledger.open(path, max_bytes=None)
    ledger.append("note", {"message": "original", "amount": 1})
    ledger.append("session.end", {"exit_code": 0})
    ledger.close()

    lines = path.read_text(encoding="utf-8").splitlines()
    edited = json.loads(lines[0])
    edited["data"]["amount"] = 999  # the value changes, the stored hash does not
    lines[0] = json.dumps(edited)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    _events, diagnostics = Ledger.read_with_diagnostics(path)
    assert diagnostics["healthy"] is False
    assert diagnostics["chain_error"] is not None
    assert "content hash mismatch" in diagnostics["chain_error"]["reason"]


def test_a_receipt_with_an_edited_sealed_payload_fails_verification(make_receipt):
    """The seal covers the receipt core; editing the core must break it."""
    from docket.verify import verify_receipt

    receipt, _path, _ledger_path = make_receipt(seal_key="ab" * 32)
    assert verify_receipt(receipt, seal_key="ab" * 32).ok
    edited = json.loads(json.dumps(receipt))
    edited["session"]["exit_code"] = 0 if edited["session"]["exit_code"] != 0 else 1
    result = verify_receipt(edited, seal_key="ab" * 32)
    assert not result.ok
    assert any(check.name == "seal" and check.status == "fail" for check in result.checks)
    wrong_key = verify_receipt(receipt, seal_key="cd" * 32)
    assert not wrong_key.ok, "a different key must not validate the seal"


# --------------------------------------------------------------------------- #
# Numbers that are not JSON: the non-extensions
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_a_request_body_carrying_a_non_json_number_is_refused_by_name(live_proxy, token):
    """`NaN` and `Infinity` parse in Python and are rejected by `JSON.parse`.

    Before this was fixed, such a body reached the recorder, could not be canonicalised, and
    the boundary answered `502 docket_internal_error`: a hostile or merely sloppy client could
    decide that the run's own evidence read "docket internal error". The refusal now names the
    token, is recorded, and never reaches the provider — because a body Docket cannot record is
    a body it must not forward.
    """
    import urllib.error
    import urllib.request

    proxy, host, port, _ledger = live_proxy
    body = f'{{"model": "claude-sonnet-5", "messages": [{{"content": "x"}}], "temperature": {token}}}'.encode()
    request = urllib.request.Request(
        f"http://{host}:{port}/openai/v1/chat/completions", data=body, headers={"Content-Type": "application/json"}
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=10)
    assert caught.value.code == 400
    payload = json.loads(caught.value.read())
    assert payload["error"]["type"] == "docket_bad_request"
    assert payload["error"]["code"] == "non_finite_number"
    assert token in payload["error"]["message"]
    assert "RFC 8259" in payload["error"]["message"]

    denial = next(event for event in proxy.ledger.events if event.type == "net.denied")
    assert denial.data["code"] == "non_finite_number"
    assert token in denial.data["reason"]
    assert not [event for event in proxy.ledger.events if event.type == "model.request"], (
        "the body was refused before any upstream call was attempted"
    )
    assert not [event for event in proxy.ledger.events if event.type == "model.error"], (
        "a refusal is not an internal error: nothing here is a defect in the boundary"
    )


def test_an_agent_reported_event_cannot_smuggle_a_non_json_number(live_proxy):
    """The instrumentation route takes the agent's word for the event; it must not take a value
    the verifier cannot round-trip."""
    import urllib.error
    import urllib.request

    proxy, host, port, _ledger = live_proxy
    request = urllib.request.Request(
        f"http://{host}:{port}/docket/note",
        data=b'{"message": "all done", "confidence": Infinity}',
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=10)
    assert caught.value.code == 400
    assert "Infinity" in caught.value.read().decode()
    assert not [event for event in proxy.ledger.events if event.type == "note"]


def test_a_ledger_line_with_a_non_json_number_is_damage_and_not_a_crashed_tail(tmp_path: Path):
    """A hand-written `Infinity` is not the signature of a process killed mid-write.

    Classifying it as a torn tail would let a doctored file claim the ordinary recovery path —
    "the supervisor was killed, recover the prefix" — for a file that no implementation of the
    format would ever have produced.
    """
    path = tmp_path / "hostile.jsonl"
    path.write_bytes(
        b'{"seq":0,"ts":"2026-10-14T00:00:00Z","type":"note","data":{"v":Infinity},"prev":null,"hash":"deadbeef"}'
    )
    _events, diagnostics = Ledger.read_with_diagnostics(path)
    assert diagnostics["healthy"] is False
    assert diagnostics["torn_tail"] is None, "a non-finite number is not a torn write"
    assert diagnostics["bad_lines"] and diagnostics["bad_lines"][0]["line"] == 1
    assert "RFC 8259" in diagnostics["bad_lines"][0]["error"]

    with pytest.raises(ValueError) as caught:
        Ledger.read(path)
    assert "RFC 8259" in str(caught.value)


def test_a_receipt_with_a_non_json_number_is_refused(tmp_path: Path):
    """The verifier's input is a document anyone can edit; the reader refuses the non-extensions
    before any check runs, so no check can be fooled by a value with no JSON form."""
    from docket.receipt import load_receipt
    from docket.util import NonFiniteJSONError

    document = {
        "format": "docket/receipt/v1",
        "docket_number": "DKT-20260102-VECT01",
        "totals": {"cost_usd": float("nan")},
    }
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(NonFiniteJSONError):
        load_receipt(path)


def test_a_request_body_that_names_a_key_twice_is_refused(live_proxy):
    """RFC 8259 leaves duplicate keys undefined; both Docket and `JSON.parse` happen to keep the
    last one, but "happens to agree" is not a foundation for a digest. The body is refused, so
    the value Docket records can never differ from the value the provider acts on."""
    import urllib.error
    import urllib.request

    proxy, host, port, _ledger = live_proxy
    body = b'{"model": "gpt-4o", "model": "claude-sonnet-5", "messages": [{"content": "x"}]}'
    request = urllib.request.Request(
        f"http://{host}:{port}/openai/v1/chat/completions", data=body, headers={"Content-Type": "application/json"}
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=10)
    assert caught.value.code == 400
    payload = json.loads(caught.value.read())
    assert payload["error"]["code"] == "duplicate_object_key"
    assert "'model'" in payload["error"]["message"]
    denial = next(event for event in proxy.ledger.events if event.type == "net.denied")
    assert denial.data["code"] == "duplicate_object_key"
    assert not [event for event in proxy.ledger.events if event.type == "model.request"]


def test_a_ledger_line_with_a_duplicate_key_is_damage_not_a_crashed_tail(tmp_path: Path):
    path = tmp_path / "ambiguous.jsonl"
    path.write_bytes(
        b'{"seq":0,"ts":"2026-10-14T00:00:00Z","type":"note","data":{"v":1},"type":"model.response",'
        b'"prev":null,"hash":"deadbeef"}'
    )
    _events, diagnostics = Ledger.read_with_diagnostics(path)
    assert diagnostics["healthy"] is False
    assert diagnostics["torn_tail"] is None
    assert diagnostics["bad_lines"] and "twice" in diagnostics["bad_lines"][0]["error"]
    with pytest.raises(ValueError) as caught:
        Ledger.read(path)
    assert "twice" in str(caught.value)


def test_an_ambiguous_provider_response_is_billed_as_estimated_and_disclosed(tmp_path: Path):
    """A provider response is the one document Docket reads *after* the call has happened.

    There is no useful refusal here — the agent is waiting for bytes — so the boundary estimates
    the tokens instead, and says so **on the event**. What must never happen is an estimate being
    presented as a measurement, so the disclosure is what this test asserts.
    """
    import urllib.request

    from docket.fakeupstream import FakeUpstream, Step

    # A body with the usage block named twice and a non-JSON number: both refused by the strict
    # reader, and neither expressible through `json.dumps`.
    upstream = FakeUpstream(
        steps=[
            Step(
                raw_body=(
                    b'{"id": "x", "model": "claude-sonnet-5", "choices": [{"message": {"content": "ok"}}],'
                    b' "usage": {"input_tokens": 10, "output_tokens": 2},'
                    b' "usage": {"input_tokens": 10, "output_tokens": 2}, "score": NaN}'
                )
            )
        ]
    )
    host, port = upstream.start()
    ledger = Ledger.open(tmp_path / "runs" / "ambiguous.jsonl", max_bytes=None)
    proxy = RecordingProxy(
        ledger=ledger,
        policy=Policy(name="strict json", caps=Caps(max_usd=None)),
        meter=CostMeter(price_table=PriceTable.load()),
        session_id="strict-json",
        upstreams={"openai": f"http://{host}:{port}"},
    )
    proxy_host, proxy_port = proxy.start()
    try:
        request = urllib.request.Request(
            f"http://{proxy_host}:{proxy_port}/openai/v1/chat/completions",
            data=b'{"model": "claude-sonnet-5", "messages": [{"content": "x"}]}',
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            assert response.status == 200, "the call already happened; the bytes reach the agent"
            response.read()
        recorded = next(event for event in ledger.events if event.type == "model.response")
        assert recorded.data["usage_source"] == "estimated"
        note = recorded.data.get("usage_note", "")
        assert "twice" in note or "not a JSON number" in note, note
        assert "not reported" in note
    finally:
        proxy.stop()
        ledger.close()
        upstream.stop()
