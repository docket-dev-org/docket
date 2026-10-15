"""Cost estimation: precedence, honesty markers and third-party recomputation."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from docket.pricing import CostMeter, PriceTable, estimate_tokens_from_text, recompute_totals


def test_price_table_ships_with_provenance():
    table = PriceTable.load()
    meta = table.document["_meta"]
    assert meta["as_of"] >= "2026-01-01"
    assert meta["currency"] == "USD"
    assert meta["sources"], "prices must cite where they came from"
    assert len(table.prices) >= 10


@pytest.mark.parametrize(
    "model,pattern_fragment",
    [
        ("claude-sonnet-5", "sonnet"),
        ("claude-opus-4-8", "opus"),
        ("claude-haiku-4-5", "haiku"),
        ("gpt-6.1-sol", "gpt-6.1"),
        ("gemini-3.1-pro", "gemini-*-pro"),
        ("deepseek-v4-flash", "deepseek-*"),
    ],
)
def test_matching_prefers_specific_families(price_table, model, pattern_fragment):
    price, assumed = price_table.match(model)
    assert not assumed
    assert pattern_fragment in price.pattern


def test_unknown_models_fall_back_and_are_flagged_assumed(price_table):
    price, assumed = price_table.match("totally-new-model-9000")
    assert assumed is True
    assert price.pattern == "*"


def test_specificity_beats_generic_glob(price_table):
    """gpt-6.1 must not be priced as gpt-6 (a 5x difference in list price)."""
    specific, _ = price_table.match("gpt-6.1-sol")
    generic, _ = price_table.match("gpt-6-astra")
    assert specific.input < generic.input


def test_cost_math_and_cached_discount(price_table):
    full, _ = price_table.cost("claude-sonnet-5", 1_000_000, 0, 0)
    assert full == Decimal("3.00")
    cached, _ = price_table.cost("claude-sonnet-5", 1_000_000, 1_000_000, 0)
    assert cached == Decimal("0.30")
    mixed, _ = price_table.cost("claude-sonnet-5", 1_000_000, 500_000, 0)
    assert mixed == Decimal("1.65")


def test_cached_tokens_cannot_exceed_input(price_table):
    cost, _ = price_table.cost("claude-sonnet-5", 100, 5_000, 0)
    assert cost >= Decimal("0")


def test_meter_splits_reported_and_estimated(price_table):
    meter = CostMeter(price_table=price_table)
    meter.record("claude-sonnet-5", 1000, 0, 200, "reported")
    meter.record("mystery-model", 1000, 0, 200, "estimated")
    payload = meter.to_dict()
    assert payload["usage_reported_calls"] == 1
    assert payload["usage_estimated_calls"] == 1
    assert payload["cost_basis"].startswith("mixed")
    assert payload["assumed_price_models"] == ["mystery-model"]
    assert payload["calls"] == 2


def test_quota_mode_does_not_pretend_to_know_dollars(price_table):
    meter = CostMeter(price_table=price_table, mode="quota")
    meter.record("claude-sonnet-5", 1_000_000, 0, 0, "reported")
    payload = meter.to_dict()
    assert payload["cost_usd"] == "0.00"
    assert "quota mode" in payload["cost_basis"]
    assert payload["total_tokens"] == 1_000_000


def test_recompute_totals_matches_the_meter(price_table):
    meter = CostMeter(price_table=price_table)
    rows = []
    for index in range(5):
        cost = meter.record("claude-sonnet-5", 10_000 + index, 1_000, 500, "reported")
        rows.append(
            {
                "seq": index,
                "type": "model.response",
                "data": {
                    "model": "claude-sonnet-5",
                    "usage": {"input_tokens": 10_000 + index, "cached_input_tokens": 1_000, "output_tokens": 500},
                    "cost_usd": str(cost),
                },
            }
        )
    recomputed = recompute_totals(rows, price_table.document)
    assert recomputed["cost_usd"] == meter.to_dict()["cost_usd"]
    assert recomputed["total_tokens"] == meter.total_tokens
    assert recomputed["per_call_mismatches"] == []


def test_recompute_totals_reports_a_forged_cost():
    table = PriceTable.load()
    rows = [
        {
            "seq": 0,
            "type": "model.response",
            "data": {
                "model": "claude-sonnet-5",
                "usage": {"input_tokens": 1_000_000, "cached_input_tokens": 0, "output_tokens": 0},
                "cost_usd": "0.01",
            },
        }
    ]
    recomputed = recompute_totals(rows, table.document)
    assert recomputed["per_call_mismatches"], "a lying cost must not be silently accepted"
    assert recomputed["cost_usd"] == "3.00"


def test_estimate_tokens_is_clearly_a_heuristic():
    assert estimate_tokens_from_text("") == 0
    assert estimate_tokens_from_text("a" * 400) == 100


def test_price_table_digest_changes_with_content(tmp_path):
    table = PriceTable.load()
    document = json.loads(json.dumps(table.document))
    document["families"][0]["input"] = "999.00"
    mutated = tmp_path / "prices.json"
    mutated.write_text(json.dumps(document), encoding="utf-8")
    assert PriceTable.load(mutated).digest() != table.digest()


def test_bundled_price_file_is_valid_json():
    path = Path(__file__).resolve().parents[1] / "src" / "docket" / "prices.json"
    json.loads(path.read_text(encoding="utf-8"))


def test_a_price_table_with_a_non_json_number_is_refused(tmp_path):
    """The price table is an input like any other — bundled by default, but an operator can
    point `--prices` (or `[pricing] table`) at their own file. A `NaN` rate would flow into
    every cost in the run, so the reader refuses the document rather than reading the value."""
    from docket.util import NonFiniteJSONError

    hostile = tmp_path / "prices.json"
    hostile.write_bytes(b'{"as_of": "2026-10-13", "families": [{"pattern": "*", "input": NaN}]}')
    with pytest.raises(NonFiniteJSONError) as caught:
        PriceTable.load(hostile)
    assert "RFC 8259" in str(caught.value)
