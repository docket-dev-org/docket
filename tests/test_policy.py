"""Policy parsing, decisions and the disclosure block."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from docket.policy import DEFAULT_TOML, Caps, Policy


def test_defaults_are_permissive_but_disclosed():
    policy = Policy()
    assert policy.caps.max_usd is None
    ok, reason = policy.models.decide("claude-sonnet-5")
    assert ok and "no model allow-list" in reason
    assert policy.budget_mode == "usd"


def test_policy_from_toml(tmp_path):
    path = tmp_path / "docket.toml"
    path.write_text(
        """
[policy]
name = "prod"

[budget]
mode = "quota"

[caps]
max_usd = 12.50
max_tokens = 1000000
max_model_calls = 50
on_breach = "deny"

[models]
allow = ["claude-*"]

[egress]
mode = "tunnel"

[files]
deny_write = [".env"]
""",
        encoding="utf-8",
    )
    policy = Policy.load(path)
    assert policy.name == "prod"
    assert policy.caps.max_usd == Decimal("12.50")
    assert policy.caps.on_breach == "deny"
    assert policy.budget_mode == "quota"
    assert policy.egress.mode == "tunnel"
    assert policy.source_path == str(path)


@pytest.mark.parametrize(
    "document,fragment",
    [
        ('[caps]\non_breach = "explode"\n', "on_breach"),
        ('[egress]\nmode = "whatever"\n', "egress.mode"),
        ('[budget]\nmode = "bitcoin"\n', "budget.mode"),
        ("[caps]\nmax_usd = -1\n", "negative"),
    ],
)
def test_invalid_policy_documents_are_rejected(tmp_path, document, fragment):
    path = tmp_path / "bad.toml"
    path.write_text(document, encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        Policy.load(path)
    assert fragment in str(exc.value)


def test_model_decisions_respect_deny_first():
    policy = Policy.from_dict({"models": {"allow": ["claude-*", "*opus*"], "deny": ["*opus*"]}})
    ok, reason = policy.models.decide("claude-opus-4-8")
    assert not ok and "deny rule" in reason
    ok, _ = policy.models.decide("claude-sonnet-5")
    assert ok
    ok, reason = policy.models.decide("gpt-6-astra")
    assert not ok and "allow rule" in reason


def test_egress_decisions():
    policy = Policy.from_dict({"egress": {"mode": "deny", "allow": ["api.openai.com", "*.internal"]}})
    assert policy.egress.decide("api.openai.com:443")[0]
    assert policy.egress.decide("svc.internal")[0]
    allowed, reason = policy.egress.decide("evil.example.com:443")
    assert not allowed and "allow-list" in reason
    tunnelling = Policy.from_dict({"egress": {"mode": "tunnel", "allow": []}})
    allowed, reason = tunnelling.egress.decide("anything.example")
    assert allowed and "tunnel" in reason


def test_policy_digest_is_stable_and_content_addressed():
    a = Policy(name="x", caps=Caps(max_usd=Decimal("1.00")))
    b = Policy(name="x", caps=Caps(max_usd=Decimal("1.00")))
    c = Policy(name="x", caps=Caps(max_usd=Decimal("2.00")))
    assert a.digest() == b.digest()
    assert a.digest() != c.digest()


def test_limits_disclosure_states_both_sides():
    disclosure = Policy().limits_disclosure()
    for key in ("enforced_before_send", "recorded_not_enforced", "not_observed", "trust_assumptions"):
        assert disclosure[key], key
    assert any("deny_write" in item for item in disclosure["recorded_not_enforced"])
    assert any("does not pass through" in item for item in disclosure["not_observed"])


def test_default_toml_round_trips(tmp_path):
    path = tmp_path / "docket.toml"
    path.write_text(DEFAULT_TOML(name="rt", max_usd="7.50"), encoding="utf-8")
    policy = Policy.load(path)
    assert policy.name == "rt"
    assert policy.caps.max_usd == Decimal("7.50")
    assert policy.digest() == Policy(name="rt", caps=Caps(max_usd=Decimal("7.50"))).digest()


# --------------------------------------------------------------------------- #
# The refusal reader
# --------------------------------------------------------------------------- #


def test_denial_code_reads_only_the_boundary_s_own_envelope():
    """The one place two clients agree about what "Docket refused this" means.

    A relayed provider error is not a refusal, however much it looks like one: the boundary
    forwards upstream bodies verbatim, and providers use ``error.code`` for their own codes. The
    envelope's ``type`` is what identifies the author.
    """
    from docket.policy import DENIAL_TYPE, denial_code

    assert denial_code({"error": {"type": DENIAL_TYPE, "code": "max_usd"}}) == "max_usd"
    assert denial_code(json.dumps({"error": {"type": DENIAL_TYPE, "code": "rate_limited"}})) == "rate_limited"
    # A provider's own 401 body, relayed unchanged: an error, not a refusal.
    assert denial_code({"error": {"code": "invalid_api_key", "type": "invalid_request_error"}}) == ""
    # A Docket-shaped envelope with no code, a code that is not a string, and junk of every kind.
    assert denial_code({"error": {"type": DENIAL_TYPE}}) == ""
    assert denial_code({"error": {"type": DENIAL_TYPE, "code": 42}}) == ""
    assert denial_code(b'{"error": {"type": "' + DENIAL_TYPE.encode() + b'", "code": "max_tokens"}}') == "max_tokens"
    assert denial_code(b"not json at all") == ""
    assert denial_code(None) == ""
    assert denial_code([]) == ""
    assert denial_code(b"") == ""


def test_a_policy_file_that_omits_the_byte_ceiling_gets_the_documented_default():
    """The ceiling that silence used to remove.

    `docs/resilience.md` and every deployment manifest describe `max_ledger_bytes` as
    having a 256 MiB default. A policy file that said nothing about it used to get *no*
    ceiling at all — a weaker policy than the one the documentation promised, applied
    silently to anyone who hand-wrote a policy (both shipped examples did). Absent now
    means the default, like every other key in the section, and `0` is how an operator
    asks for no ceiling on purpose.
    """
    from docket.policy import Resilience

    default = Policy.from_dict({})
    assert default.resilience.max_ledger_bytes == Resilience().max_ledger_bytes == 268_435_456
    assert default.digest() == Policy().digest(), "silence must mean the default, not a different policy"

    unlimited = Policy.from_dict({"resilience": {"max_ledger_bytes": 0}})
    assert unlimited.resilience.max_ledger_bytes is None
    assert unlimited.to_dict()["resilience"]["max_ledger_bytes"] is None
    assert unlimited.digest() != default.digest()

    tuned = Policy.from_dict({"resilience": {"max_ledger_bytes": 8192}})
    assert tuned.resilience.max_ledger_bytes == 8192
    with pytest.raises(ValueError, match="no ceiling"):
        Policy.from_dict({"resilience": {"max_ledger_bytes": 1024}})


def test_every_default_written_down_in_a_parser_is_the_dataclass_default():
    """A default spelled twice is a default that can drift, so the two are compared here.

    Several parsers spell a default inline rather than reading the dataclass's own field —
    `caps_raw.get("warn_at", 0.8)`, `egress_raw.get("mode", "deny")`, `300.0` for the cache's TTL.
    That is readable and fine right up until one of the two is edited: `resilience.max_ledger_bytes`
    was written down as "absent means no ceiling" in the parser and as 256 MiB in the dataclass, and
    a hand-written policy file got the weaker of the two for as long as it took someone to notice.
    This test walks every dataclass behind a section of the policy document and asserts that a
    document which says nothing produces exactly the object the dataclass describes, value for
    value, so the next drift fails here instead of in someone's audit trail.
    """
    from dataclasses import fields

    silent = Policy.from_dict({})
    default = Policy()
    assert silent.to_dict() == default.to_dict()
    assert silent.digest() == default.digest()

    # Naming a section without any keys must also land on the defaults: this is the path where a
    # parser supplies the value itself instead of falling through to the dataclass constructor.
    empty_sections = Policy.from_dict(
        {name: {} for name in ("caps", "models", "egress", "files", "resilience", "cache", "policy", "budget")}
    )
    assert empty_sections.to_dict() == default.to_dict(), empty_sections.to_dict()["resilience"]

    for name in ("caps", "models", "egress", "files", "resilience", "cache"):
        parsed = getattr(silent, name)
        built = type(parsed)()
        for field in fields(parsed):
            assert getattr(parsed, field.name) == getattr(built, field.name), (
                f"{name}.{field.name}: the parser's default is {getattr(parsed, field.name)!r}, "
                f"the dataclass's is {getattr(built, field.name)!r}"
            )
