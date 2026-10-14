"""Regressions for defects found by ``tools/fuzz.py`` — one test per escaped bug.

A fuzzer's value is the bugs it finds, and a bug that is fixed without a test is a bug that
comes back. Each test below reproduces a *specific* input that used to defeat a documented
promise, and names the property in ``tools/fuzz.py`` that caught it:

``policy``   a policy that loads must round-trip to the same digest (a receipt records that
             digest; if the document beside it cannot reproduce it, the record contradicts
             itself).
``receipt``  the verifier must give a verdict on any receipt, including a hostile one, and a
             changed total must never still verify.
``canon``    nothing non-canonicalisable may reach a hashed structure.

These are *input* regression tests: the fuzzer explores, these pin.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from docket.fakeupstream import FakeUpstream, Step
from docket.ledger import Ledger
from docket.policy import Policy
from docket.pricing import (
    MAX_CLAIMED_TOKENS_PER_CALL,
    PRICE_CEILING_PER_MTOK,
    CostMeter,
    Price,
    PriceTable,
    recompute_totals,
)
from docket.proxy import RecordingProxy
from docket.signing import ed25519_available
from docket.verify import verify_receipt

# --------------------------------------------------------------------------- #
# Policy: the name is identity, and identity must survive serialisation
# --------------------------------------------------------------------------- #


def test_a_policy_round_trips_through_its_serialised_form():
    """``Policy.from_dict(policy.to_dict())`` must be the same policy, digest and all.

    The supervisor, the SDK and the receipt all write ``{"digest": policy.digest(),
    "document": policy.to_dict()}``. Before this was fixed, ``from_dict`` read the name only
    from the ``[policy]`` table, so every document written into a ledger reconstructed a
    policy called ``"default"`` with a *different* digest — the reconstruction did not match
    the digest recorded beside it.
    """
    policy = Policy(name="prod")
    policy.caps.max_usd = Decimal("12.50")
    again = Policy.from_dict(policy.to_dict())
    assert again.name == "prod"
    assert again.to_dict() == policy.to_dict()
    assert again.digest() == policy.digest()


def test_the_document_beside_a_digest_in_the_ledger_reproduces_that_digest(tmp_path):
    """The invariant the ledger itself depends on, tested on a real ``policy.applied`` event."""
    policy = Policy(name="from-ledger")
    ledger = Ledger.open(tmp_path / "l.jsonl")
    ledger.append("policy.applied", {"name": policy.name, "digest": policy.digest(), "document": policy.to_dict()})
    event = ledger.events[-1]
    ledger.close()
    reconstructed = Policy.from_dict(event.data["document"])
    assert reconstructed.digest() == event.data["digest"]
    assert reconstructed.name == event.data["name"]


@pytest.mark.parametrize("literal", ["true", "1", "1.5", "[]", '""'])
def test_a_policy_name_that_is_not_a_string_is_refused_by_name(tmp_path, literal):
    path = tmp_path / "p.toml"
    path.write_text(f"[policy]\nname = {literal}\n", encoding="utf-8")
    with pytest.raises(ValueError) as caught:
        Policy.load(path)
    assert "policy.name" in str(caught.value)
    assert "string" in str(caught.value) or "empty" in str(caught.value)


def test_a_policy_name_given_twice_and_differently_is_refused():
    """Two spellings of one identity field is an ambiguity, not a precedence puzzle."""
    with pytest.raises(ValueError) as caught:
        Policy.from_dict({"name": "a", "policy": {"name": "b"}})
    assert "twice" in str(caught.value)
    # The same name in both spellings is not ambiguous.
    assert Policy.from_dict({"name": "a", "policy": {"name": "a"}}).name == "a"


# --------------------------------------------------------------------------- #
# Pricing: untrusted numbers produce verdicts, never decimal exceptions
# --------------------------------------------------------------------------- #


def _usage_row(seq: int, *, input_tokens=1000, cached_input_tokens=0, output_tokens=200, cost="0.006000"):
    return {
        "seq": seq,
        "type": "model.response",
        "data": {
            "model": "claude-sonnet-5",
            "usage": {
                "input_tokens": input_tokens,
                "cached_input_tokens": cached_input_tokens,
                "output_tokens": output_tokens,
            },
            "cost_usd": cost,
        },
    }


def test_an_implausible_token_count_is_refused_by_name_not_by_exception():
    """Fuzzer target ``receipt``: a 40-digit token count used to raise ``InvalidOperation``.

    ``quantize`` raises past the decimal context's precision, so the old code leaked a decimal
    exception out of ``verify_receipt`` — a verifier an attacker can switch off with one number.
    """
    rows = [_usage_row(4, cached_input_tokens="9" * 40)]
    with pytest.raises(ValueError) as caught:
        recompute_totals(rows, PriceTable.load().document)
    message = str(caught.value)
    assert "event 4 cached_input_tokens" in message
    assert "implausible" in message


@pytest.mark.parametrize("bad", ["-5", "-1"])
def test_a_negative_token_count_is_refused_by_name(bad):
    with pytest.raises(ValueError) as caught:
        recompute_totals([_usage_row(1, input_tokens=bad)], PriceTable.load().document)
    assert "event 1 input_tokens" in str(caught.value)


@pytest.mark.parametrize("bad", ["abc", "", "1.5", True, None])
def test_a_token_count_that_is_not_a_whole_number_is_refused_by_name(bad):
    with pytest.raises(ValueError) as caught:
        recompute_totals([_usage_row(2, output_tokens=bad)], PriceTable.load().document)
    assert "event 2 output_tokens" in str(caught.value)


def test_the_token_ceiling_is_a_boundary_not_a_vibe():
    """Boundary-value analysis: the largest accepted count works, one more is refused."""
    document = json.loads(json.dumps(PriceTable.load().document))
    accepted = recompute_totals([_usage_row(1, input_tokens=MAX_CLAIMED_TOKENS_PER_CALL)], document)
    assert accepted["calls"] == 1
    assert Decimal(accepted["cost_usd"]) > 0
    with pytest.raises(ValueError):
        recompute_totals([_usage_row(1, input_tokens=MAX_CLAIMED_TOKENS_PER_CALL + 1)], document)


def test_a_price_row_beyond_any_real_price_is_refused_by_name():
    """The receipt carries its own price table, so the prices are attacker-controlled too."""
    document = json.loads(json.dumps(PriceTable.load().document))
    document["families"][0]["input"] = "1e999999"
    with pytest.raises(ValueError) as caught:
        recompute_totals([_usage_row(1)], document)
    assert "price row" in str(caught.value)
    assert "1e999999" in str(caught.value) or "implausible" in str(caught.value)


def test_a_price_that_is_not_a_number_is_refused_by_name():
    document = json.loads(json.dumps(PriceTable.load().document))
    document["families"][0]["input"] = "not a price"
    with pytest.raises(ValueError) as caught:
        recompute_totals([_usage_row(1)], document)
    assert "price row" in str(caught.value)
    assert "input" in str(caught.value)


def test_pricing_never_raises_a_decimal_exception():
    """``Price.cost`` promises a ``Decimal`` or a ``ValueError`` — nothing in between.

    The runtime path prices what a *provider* reported, inside a request handler: an
    ``InvalidOperation`` escaping there would take the recorder down with the run it exists to
    record.
    """
    price = Price(
        pattern="*", input=Decimal("15.00"), cached_input=Decimal("1.50"), output=Decimal("75.00"), source="t"
    )
    # Absurd but arithmetically representable: computed, not crashed.
    assert price.cost(MAX_CLAIMED_TOKENS_PER_CALL, 0, 0) > 0
    # A price whose exponent cannot be represented is a named refusal.
    exploded = Price(
        pattern="*",
        input=Decimal("1e999999"),
        cached_input=Decimal("0"),
        output=Decimal("0"),
        source="t",
    )
    with pytest.raises(ValueError) as caught:
        exploded.cost(1000, 0, 200)
    assert "cannot be represented" in str(caught.value) or "cannot price" in str(caught.value)
    # Non-numeric token counts (a string where a count belongs) are refused, not multiplied.
    with pytest.raises(ValueError):
        price.cost("lots", 0, 200)


def test_the_price_ceiling_is_a_boundary_not_a_vibe():
    document = json.loads(json.dumps(PriceTable.load().document))
    document["families"][0]["input"] = str(PRICE_CEILING_PER_MTOK)
    assert recompute_totals([_usage_row(1)], document)["calls"] == 1
    document["families"][0]["input"] = str(PRICE_CEILING_PER_MTOK + Decimal("1"))
    with pytest.raises(ValueError):
        recompute_totals([_usage_row(1)], document)


def test_a_failed_recomputation_is_a_verification_verdict_not_a_traceback(tmp_path, make_receipt):
    """End to end: a receipt whose usage is nonsense verifies to a *stated* failure."""
    receipt, path, ledger_path = make_receipt()
    receipt["appendix"]["usage"][0]["cached_input_tokens"] = "9" * 40
    path.write_text(json.dumps(receipt), encoding="utf-8")
    result = verify_receipt(receipt, ledger_path=ledger_path)
    assert result.ok is False
    failing = {check.name: check for check in result.checks if check.status == "fail"}
    assert "cost-recomputation" in failing
    assert "cannot recompute the cost" in failing["cost-recomputation"].detail
    assert "implausible" in failing["cost-recomputation"].detail


# --------------------------------------------------------------------------- #
# The boundary: a hostile provider must not be able to crash the recorder
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "usage",
    [
        {"input_tokens": "abc", "output_tokens": 200},
        {"input_tokens": True, "output_tokens": 200},
        {"input_tokens": 1.5, "output_tokens": 200},
        {"input_tokens": -5, "output_tokens": 200},
        {"input_tokens": int("9" * 40), "output_tokens": 200},
        {"input_tokens": 1000, "output_tokens": "9" * 40},
        {"input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": "many"},
        {"prompt_tokens": {"nested": "object"}, "completion_tokens": 200},
    ],
)
def test_a_provider_cannot_crash_the_recorder_with_absurd_usage(tmp_path, usage):
    """The recorder's one unacceptable outcome is dying with the thing it records.

    Every field above used to raise out of the request handler (``ValueError`` from ``int()``)
    or out of the meter (``InvalidOperation`` from quantising a 40-digit cost). The response is
    still passed through — Docket is a recorder, not a gate — but the tokens are marked
    ``estimated`` and the event says which numbers were refused, so an invented measurement is
    never filed as ``reported``.
    """
    upstream = FakeUpstream(
        steps=[Step(body_override={"id": "x", "model": "claude-sonnet-5", "usage": usage, "choices": []})]
    )
    host, port = upstream.start()
    ledger = Ledger.open(tmp_path / "l.jsonl")
    meter = CostMeter(price_table=PriceTable.load())
    proxy = RecordingProxy(
        ledger=ledger, policy=Policy(), meter=meter, session_id="s", upstreams={"openai": f"http://{host}:{port}"}
    )
    proxy_host, proxy_port = proxy.start()
    try:
        import urllib.request

        request = urllib.request.Request(
            f"http://{proxy_host}:{proxy_port}/openai/v1/chat/completions",
            data=json.dumps({"model": "claude-sonnet-5", "messages": []}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            assert response.status == 200  # recorded, not gated
            response.read()
    finally:
        proxy.stop()
        upstream.stop()
        ledger.close()

    events = [event.to_dict() for event in Ledger.read(tmp_path / "l.jsonl")]
    response_event = next(event for event in events if event["type"] == "model.response")
    assert response_event["data"]["usage_source"] == "estimated"
    assert "usage_note" in response_event["data"], "an unusable usage block must be disclosed"
    assert Decimal(response_event["data"]["cost_usd"]) >= 0
    for event in events:
        json.dumps(event, allow_nan=False)


def test_a_provider_reporting_a_huge_but_plausible_count_is_taken_at_its_word(tmp_path):
    """A measurement is recorded as a measurement; only the *impossible* is refused.

    Boundary behaviour on the other side of the ceiling: a large count is priced (and so can
    breach a cap), rather than being quietly downgraded to an estimate.
    """
    big = MAX_CLAIMED_TOKENS_PER_CALL
    upstream = FakeUpstream(steps=[Step(input_tokens=big, output_tokens=0)])
    host, port = upstream.start()
    ledger = Ledger.open(tmp_path / "l.jsonl")
    meter = CostMeter(price_table=PriceTable.load())
    proxy = RecordingProxy(
        ledger=ledger, policy=Policy(), meter=meter, session_id="s", upstreams={"openai": f"http://{host}:{port}"}
    )
    proxy_host, proxy_port = proxy.start()
    try:
        import urllib.request

        request = urllib.request.Request(
            f"http://{proxy_host}:{proxy_port}/openai/v1/chat/completions",
            data=json.dumps({"model": "claude-sonnet-5", "messages": []}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            response.read()
    finally:
        proxy.stop()
        upstream.stop()
        ledger.close()
    event = Ledger.read(tmp_path / "l.jsonl")[-1].to_dict()
    assert event["data"]["usage_source"] == "reported"
    assert event["data"]["usage"]["input_tokens"] == big
    assert "usage_note" not in event["data"]


# --------------------------------------------------------------------------- #
# Inclusion proofs: the claim the project is built on
# --------------------------------------------------------------------------- #


def _flip_hex(value: str) -> str:
    digit = value[0].lower()
    return ("0" if digit != "0" else "1") + value[1:]


def test_a_doctored_inclusion_proof_never_verifies(make_receipt):
    """One flipped hex digit in one sibling hash used to leave the receipt *verified*.

    The verifier counted proven rows and never compared that count with the number of rows, so
    a receipt with a doctored proof reported ``usage-inclusion-proofs: pass`` — with a detail
    line asserting every row was proven. The fuzz target ``proof`` now holds this line.
    """
    receipt, _, ledger_path = make_receipt()
    row = receipt["appendix"]["usage"][0]
    row["merkle_path"][0]["hash"] = _flip_hex(row["merkle_path"][0]["hash"])
    for ledger in (None, ledger_path):
        result = verify_receipt(receipt, ledger_path=ledger)
        assert result.ok is False, f"a doctored proof verified (ledger={ledger is not None})"
        assert any(check.name == "usage-inclusion-proofs" and check.status == "fail" for check in result.checks)


@pytest.mark.parametrize(
    "doctor",
    [
        lambda row: row["merkle_path"][0].__setitem__("side", "sideways"),
        lambda row: row["merkle_path"].__setitem__(0, 42),
        lambda row: row["merkle_path"].__setitem__(0, {"side": "left", "hash": "zz"}),
        lambda row: row["merkle_path"].__setitem__(0, {"side": "left", "hash": "aa"}),
        lambda row: row.__setitem__("merkle_path", {"side": "left", "hash": "aa"}),
        lambda row: row.__setitem__("merkle_path", 7),
        lambda row: row.__setitem__("event_hash", 99),
        lambda row: row.__setitem__("event_hash", "not-hex"),
    ],
)
def test_a_malformed_inclusion_proof_is_a_verdict_not_a_traceback(make_receipt, doctor):
    receipt, _, ledger_path = make_receipt()
    doctor(receipt["appendix"]["usage"][0])
    result = verify_receipt(receipt, ledger_path=ledger_path)
    assert result.ok is False
    assert any(check.name == "usage-inclusion-proofs" and check.status == "fail" for check in result.checks)


def test_a_doctored_row_binding_is_counted(make_receipt):
    """A row whose event hash no longer matches the ledger must be counted, not shrugged at."""
    receipt, _, ledger_path = make_receipt()
    receipt["appendix"]["usage"][1]["merkle_path"][0]["hash"] = _flip_hex(
        receipt["appendix"]["usage"][1]["merkle_path"][0]["hash"]
    )
    result = verify_receipt(receipt, ledger_path=ledger_path)
    details = [
        check.detail for check in result.checks if check.name == "usage-inclusion-proofs" and check.status == "fail"
    ]
    assert any("of 3 usage rows" in detail for detail in details), details
    assert any("does not prove against" in detail for detail in details), details


def test_a_summary_without_proofs_is_a_stated_skip(make_receipt):
    """The large-session case (spec §6): no proofs is a disclosed gap, not a claimed pass."""
    receipt, _, ledger_path = make_receipt()
    for row in receipt["appendix"]["usage"]:
        row.pop("merkle_path")
    result = verify_receipt(receipt, ledger_path=ledger_path)
    statuses = [check.status for check in result.checks if check.name == "usage-inclusion-proofs"]
    assert statuses == ["skip"], statuses


def test_an_unreadable_appendix_cannot_pass_cost_recomputation(make_receipt):
    receipt, _, ledger_path = make_receipt()
    receipt["appendix"] = ["not", "an", "object"]
    result = verify_receipt(receipt, ledger_path=ledger_path)
    assert result.ok is False
    failing = {check.name for check in result.checks if check.status == "fail"}
    assert "cost-recomputation" in failing
    assert "usage-inclusion-proofs" in failing


# --------------------------------------------------------------------------- #
# Shape: a member of the wrong type is a finding, never a traceback
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", [None, "", 0, False, [], "string", 3.5])
def test_a_document_that_is_not_a_receipt_is_a_verdict(value):
    """``docket verify notes.json`` where notes.json is a list must say so, not crash.

    Whoever asks for a verification chooses the file to hand over, so the entry point is the one
    place that must accept *anything*.
    """
    result = verify_receipt(value)
    assert result.ok is False
    assert result.checks[0].name == "receipt-format"
    assert result.checks[0].status == "fail"
    assert "not a receipt" in result.checks[0].detail


@pytest.mark.parametrize("member", ["ledger", "totals", "evidence", "ops", "price_table", "appendix"])
def test_a_member_of_the_wrong_type_is_reported_by_name(make_receipt, member):
    """``totals`` as a list used to raise ``AttributeError`` out of the verifier."""
    receipt, _, ledger_path = make_receipt()
    receipt[member] = ["not", "an", "object"]
    result = verify_receipt(receipt, ledger_path=ledger_path)
    assert result.ok is False
    assert any(check.name == "receipt-shape" and check.status == "fail" for check in result.checks)


@pytest.mark.parametrize("value", ["1.5", "abc", "", [], {}, True, None])
def test_a_non_numeric_event_count_is_reported_not_multiplied(make_receipt, value):
    receipt, _, ledger_path = make_receipt()
    receipt["ledger"]["events"] = value
    result = verify_receipt(receipt, ledger_path=ledger_path)
    assert result.ok is False
    assert any(
        check.name == "receipt-shape" and "ledger.events" in check.detail and check.status == "fail"
        for check in result.checks
    )


def test_a_price_table_with_no_fallback_is_a_named_refusal(make_receipt):
    """A table that cannot price a model at all used to raise ``LookupError`` out of the verifier."""
    receipt, _, ledger_path = make_receipt()
    for row in receipt["price_table"]["document"]["families"]:
        row["pattern"] = "some-other-vendor-*"
    result = verify_receipt(receipt, ledger_path=ledger_path)
    assert result.ok is False
    failing = [check.detail for check in result.checks if check.name == "cost-recomputation" and check.status == "fail"]
    assert failing, [check.name for check in result.checks]
    assert any("cannot recompute the cost" in detail and "fallback" in detail for detail in failing), failing


@pytest.mark.parametrize(
    "document",
    [
        {"families": "not a list"},
        {"families": [42]},
        {"families": [{"pattern": "*"}]},
        {"families": [{"pattern": "*", "input": "abc", "output": "1"}]},
        {"families": [{"pattern": "*", "input": "-1", "output": "1"}]},
        {},
        [],
    ],
)
def test_a_hostile_price_table_is_refused_by_name(make_receipt, document):
    receipt, _, ledger_path = make_receipt()
    receipt["price_table"]["document"] = document
    result = verify_receipt(receipt, ledger_path=ledger_path)
    detail = " | ".join(check.detail for check in result.checks if check.status == "fail")
    assert result.ok is False, detail
    assert any(
        check.name in {"cost-recomputation", "receipt-shape"} for check in result.checks if check.status == "fail"
    )


@pytest.mark.parametrize(
    "signature",
    [
        42,
        [],
        "0" * 128,
        {},
        {"alg": "ed25519"},
        {"alg": "ed25519", "public_key": 5, "value": 7},
        {"alg": "ed25519", "public_key": "zz", "value": "aa"},
        {"alg": "something-else", "public_key": "aa" * 32, "value": "bb" * 64},
    ],
)
def test_a_malformed_signature_is_a_verdict_not_a_traceback(make_receipt, signature):
    receipt, _, ledger_path = make_receipt()
    receipt["signature"] = signature
    result = verify_receipt(receipt, ledger_path=ledger_path)
    status = next(check.status for check in result.checks if check.name == "signature")
    well_formed = (
        isinstance(signature, dict)
        and signature.get("alg") == "ed25519"
        and isinstance(signature.get("public_key"), str)
        and isinstance(signature.get("value"), str)
    )
    if well_formed and not ed25519_available():
        # Without the optional 'cryptography' package, a well-formed Ed25519 block is a stated
        # skip — the environment's limitation, not the receipt's fault. Everything malformed is
        # still a failure, because shape is checkable without the library.
        assert status == "skip", status
    else:
        assert result.ok is False, f"a malformed signature verified: {signature!r}"
        assert status == "fail", status


@pytest.mark.parametrize("seal", [42, [], "0" * 64, {}, {"alg": "hmac-sha256"}])
def test_a_malformed_seal_is_a_verdict_not_a_traceback(make_receipt, seal):
    receipt, _, ledger_path = make_receipt()
    receipt["seal"] = seal
    result = verify_receipt(receipt, ledger_path=ledger_path, seal_key="00" * 32)
    assert result.ok is False
    assert any(check.name == "seal" and check.status == "fail" for check in result.checks)


def test_a_seal_key_that_is_not_hex_is_reported_not_a_traceback(make_receipt):
    receipt, _, ledger_path = make_receipt()
    receipt["seal"] = {"alg": "hmac-sha256", "key_id": "k", "value": "v"}
    result = verify_receipt(receipt, ledger_path=ledger_path, seal_key="not-hex-at-all")
    assert result.ok is False
    detail = " ".join(check.detail for check in result.checks if check.name == "seal")
    assert "seal key is not usable" in detail


@pytest.mark.parametrize("empty", [0, {}, [], "", False])
def test_an_empty_signature_or_seal_is_a_failure_not_a_silent_skip(make_receipt, empty):
    """``"signature": 0`` and ``"seal": {}`` used to read as "not signed" — a lenient lie.

    A false-y value in a member that must be an object is a malformed member. Reporting "not
    signed" for it would quietly discount a field an attacker chose to send.
    """
    receipt, _, ledger_path = make_receipt()
    receipt["signature"] = empty
    receipt["seal"] = empty
    result = verify_receipt(receipt, ledger_path=ledger_path, seal_key="00" * 32)
    statuses = {check.name: check.status for check in result.checks}
    assert statuses["signature"] == "fail", statuses
    assert statuses["seal"] == "fail", statuses


# --------------------------------------------------------------------------- #
# Policy: the TOML surface refuses nonsense by name
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text",
    [
        "[resilience]\nbreaker_threshold = 1e999\n",
        "[resilience]\nbreaker_threshold = true\n",
        "[resilience]\nmax_events = 2.5\n",
        "[resilience]\nfail_fast_evidence = 1\n",
        "[resilience]\nmax_calls_per_second = 1e999\n",
        "caps = 5\n",
        "[caps]\nmax_usd = 1e999\n",
        "resilience = 5\n",
    ],
)
def test_policy_values_that_cannot_mean_anything_are_refused_by_name(tmp_path, text):
    path = tmp_path / "p.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError) as caught:
        Policy.load(path)
    assert any(section in str(caught.value) for section in ("resilience", "caps")), str(caught.value)
    assert "." in str(caught.value) or "table" in str(caught.value)
