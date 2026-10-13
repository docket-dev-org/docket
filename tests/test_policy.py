"""Policy parsing, decisions and the disclosure block."""

from __future__ import annotations

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
