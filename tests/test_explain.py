"""Findings are arithmetic over events. These tests pin the arithmetic."""

from __future__ import annotations

from decimal import Decimal

from docket.explain import analyse, oversized_payloads, total_estimated_savings
from docket.ledger import Ledger
from docket.policy import Caps, Policy
from docket.pricing import CostMeter, PriceTable


def _ledger_with(calls: list[dict], *, extra_before: list[tuple[str, dict]] = ()) -> tuple[Ledger, CostMeter]:
    price_table = PriceTable.load()
    ledger = Ledger.in_memory()
    meter = CostMeter(price_table=price_table)
    for event_type, data in extra_before:
        ledger.append(event_type, data)
    for call in calls:
        ledger.append("model.request", {"model": call["model"], "fingerprint": call["fingerprint"]})
        cost = meter.record(
            call["model"],
            call.get("input_tokens", 0),
            call.get("cached_input_tokens", 0),
            call.get("output_tokens", 0),
            call.get("usage_source", "reported"),
        )
        ledger.append(
            "model.response",
            {
                "model": call["model"],
                "usage": {
                    "input_tokens": call.get("input_tokens", 0),
                    "cached_input_tokens": call.get("cached_input_tokens", 0),
                    "output_tokens": call.get("output_tokens", 0),
                },
                "cost_usd": str(cost),
                "usage_source": call.get("usage_source", "reported"),
            },
        )
    return ledger, meter


def test_clean_session_produces_no_findings():
    ledger, _ = _ledger_with(
        [
            {"model": "claude-haiku-4-5", "fingerprint": f"fp{i}", "input_tokens": 2_000, "output_tokens": 400}
            for i in range(4)
        ]
    )
    assert analyse(ledger, price_table=PriceTable.load()) == []


def test_runaway_loop_is_deterministic_and_attributed():
    ledger, _ = _ledger_with(
        [
            {"model": "claude-sonnet-5", "fingerprint": "same", "input_tokens": 40_000, "output_tokens": 300}
            for _ in range(9)
        ]
    )
    findings = analyse(ledger, price_table=PriceTable.load())
    loop = next(f for f in findings if f["rule"] == "runaway-loop")
    assert loop["severity"] == "critical"
    assert loop["confidence"] == "deterministic"
    assert "9 byte-identical requests" in loop["detail"]
    assert loop["usd_impact"].startswith("$")
    assert len(loop["evidence"]) == 9


def test_five_repeats_are_below_the_loop_threshold():
    ledger, _ = _ledger_with(
        [
            {"model": "claude-sonnet-5", "fingerprint": "same", "input_tokens": 1_000, "output_tokens": 100}
            for _ in range(5)
        ]
    )
    findings = analyse(ledger, price_table=PriceTable.load())
    assert not any(f["rule"] == "runaway-loop" for f in findings)


def test_counterfactual_says_what_a_cap_would_have_saved():
    ledger, _ = _ledger_with(
        [
            {"model": "claude-sonnet-5", "fingerprint": f"fp{i}", "input_tokens": 100_000, "output_tokens": 1_000}
            for i in range(5)
        ]
    )
    policy = Policy(caps=Caps(max_usd=Decimal("0.50")))
    findings = analyse(ledger, price_table=PriceTable.load(), policy=policy)
    counterfactual = next(f for f in findings if f["rule"] == "counterfactual")
    assert counterfactual["confidence"] == "deterministic"
    assert counterfactual["usd_impact"].startswith("$")


def test_fat_context_is_flagged_with_the_offending_call():
    ledger, _ = _ledger_with(
        [
            {"model": "claude-sonnet-5", "fingerprint": "a", "input_tokens": 150_000, "output_tokens": 200},
            {"model": "claude-sonnet-5", "fingerprint": "b", "input_tokens": 1_000, "output_tokens": 200},
        ]
    )
    findings = analyse(ledger, price_table=PriceTable.load())
    fat = next(f for f in findings if f["rule"] == "fat-context")
    assert "150,000" in fat["detail"]
    assert fat["evidence"] == [1]


def test_cache_miss_is_labelled_heuristic():
    ledger, _ = _ledger_with(
        [
            {"model": "claude-sonnet-5", "fingerprint": "same", "input_tokens": 50_000, "output_tokens": 100}
            for _ in range(4)
        ]
    )
    findings = analyse(ledger, price_table=PriceTable.load())
    cache = next(f for f in findings if f["rule"] == "cache-miss")
    assert cache["confidence"] == "heuristic", "an estimate must never be labelled deterministic"


def test_premium_model_on_a_trivial_call_is_flagged_but_hedged():
    ledger, _ = _ledger_with(
        [
            {"model": "claude-opus-4-8", "fingerprint": "a", "input_tokens": 900, "output_tokens": 200},
            {"model": "claude-sonnet-5", "fingerprint": "b", "input_tokens": 90_000, "output_tokens": 400},
        ]
    )
    findings = analyse(ledger, price_table=PriceTable.load())
    premium = next(f for f in findings if f["rule"] == "premium-for-small-calls")
    assert "does not make for you" in premium["detail"]


def test_policy_denials_and_write_violations_are_reported():
    ledger = Ledger.in_memory()
    ledger.append("model.denied", {"model": "claude-opus-4-8", "reason": "deny rule"})
    ledger.append("policy.violation", {"rule": "files.deny_write", "path": ".env"})
    findings = analyse(ledger, price_table=PriceTable.load())
    rules = {f["rule"] for f in findings}
    assert {"policy-denials", "write-scope"} <= rules
    write_scope = next(f for f in findings if f["rule"] == "write-scope")
    assert "cannot veto" in write_scope["detail"]


def test_missing_usage_is_marked_estimated_in_the_meter():
    ledger, meter = _ledger_with(
        [
            {
                "model": "claude-sonnet-5",
                "fingerprint": "a",
                "input_tokens": 10,
                "output_tokens": 5,
                "usage_source": "estimated",
            }
        ]
    )
    assert meter.to_dict()["usage_estimated_calls"] == 1
    assert meter.to_dict()["cost_basis"].startswith("estimated")


def test_findings_are_ordered_by_severity():
    ledger, _ = _ledger_with(
        [
            {"model": "claude-opus-4-8", "fingerprint": "same", "input_tokens": 150_000, "output_tokens": 100}
            for _ in range(8)
        ]
    )
    policy = Policy(caps=Caps(max_usd=Decimal("1.00")))
    findings = analyse(ledger, price_table=PriceTable.load(), policy=policy)
    severities = [f["severity"] for f in findings]
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    assert severities == sorted(severities, key=lambda s: order[s])


def test_attributed_impact_sums_dollar_impacts():
    ledger, _ = _ledger_with(
        [
            {"model": "claude-sonnet-5", "fingerprint": "same", "input_tokens": 150_000, "output_tokens": 100},
            {"model": "claude-sonnet-5", "fingerprint": "same", "input_tokens": 150_000, "output_tokens": 100},
            {"model": "claude-sonnet-5", "fingerprint": "same", "input_tokens": 150_000, "output_tokens": 100},
            {"model": "claude-sonnet-5", "fingerprint": "same", "input_tokens": 150_000, "output_tokens": 100},
            {"model": "claude-sonnet-5", "fingerprint": "same", "input_tokens": 150_000, "output_tokens": 100},
            {"model": "claude-sonnet-5", "fingerprint": "same", "input_tokens": 150_000, "output_tokens": 100},
            {"model": "claude-sonnet-5", "fingerprint": "same", "input_tokens": 150_000, "output_tokens": 100},
        ]
    )
    findings = analyse(ledger, price_table=PriceTable.load())
    assert total_estimated_savings(findings).startswith("$")


def test_an_unreadable_attributed_impact_is_named_not_dropped():
    """Docket writes those strings itself, so an unparseable one is a bug — and a sum that
    quietly omitted it would understate the savings in a document people act on."""
    findings = [
        {"title": "a", "usd_impact": "$1.62"},
        {"title": "b", "usd_impact": "$not-a-number"},
        {"title": "c", "usd_impact": None},  # no attributed cost: not part of the sum, fine
    ]
    rendered = total_estimated_savings(findings)
    assert rendered.startswith("$1.62")
    assert "1 unreadable impact value(s) omitted" in rendered

    assert total_estimated_savings(findings[:1] + findings[2:]) == "$1.62"


def test_oversized_payload_helper():
    ledger = Ledger.in_memory()
    ledger.append("model.request", {"body_bytes": 40_000})
    ledger.append("model.request", {"body_bytes": 10})
    assert oversized_payloads(ledger) == [(0, 40_000)]
