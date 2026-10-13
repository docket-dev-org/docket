"""Cost estimation from token counts, with provenance.

Docket never bills anyone. It estimates what a run cost from the token counts the
provider reported (or, when a provider reports nothing, from a clearly-marked
estimate), using a price table that ships with the tool, is embedded in every
receipt, and can be overridden by the operator.

Three rules keep this honest:

* Every total carries a ``cost_basis`` marking whether it rests on reported or
  estimated usage. Estimated and reported numbers are never silently mixed.
* Unknown models fall back to a mid-tier assumption which is flagged as
  ``assumed`` in the ledger event, the receipt and the report.
* The exact price table used is embedded in the receipt, so a third party can
  recompute the totals from raw token counts without trusting Docket's own maths.
"""

from __future__ import annotations

import fnmatch
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from .canon import canonical_bytes, digest_of, sha256_hex, usd

BUNDLED = Path(__file__).with_name("prices.json")
MICROS = Decimal("0.000001")


@dataclass(frozen=True)
class Price:
    pattern: str
    input: Decimal
    cached_input: Decimal
    output: Decimal
    source: str

    def cost(self, input_tokens: int, cached_input_tokens: int, output_tokens: int) -> Decimal:
        billable_input = max(input_tokens - cached_input_tokens, 0)
        total = (
            Decimal(billable_input) * self.input
            + Decimal(cached_input_tokens) * self.cached_input
            + Decimal(output_tokens) * self.output
        ) / Decimal(1_000_000)
        return total.quantize(MICROS)


@dataclass
class PriceTable:
    """A loaded price table with deterministic matching."""

    document: dict[str, Any]
    as_of: str
    prices: list[Price]
    fallback_pattern: str

    @classmethod
    def load(cls, path: str | Path | None = None) -> PriceTable:
        raw = json.loads(Path(path or BUNDLED).read_text(encoding="utf-8"))
        families = raw.get("families", [])
        prices: list[Price] = []
        for entry in families:
            pattern_field = str(entry.get("pattern", "*"))
            # A single entry may declare several equivalent patterns: "o3* / o4*".
            for pattern in [p.strip() for p in pattern_field.split("/") if p.strip()]:
                prices.append(
                    Price(
                        pattern=pattern.lower(),
                        input=Decimal(str(entry["input"])),
                        cached_input=Decimal(str(entry.get("cached_input", entry["input"]))),
                        output=Decimal(str(entry["output"])),
                        source=str(entry.get("source", "unspecified")),
                    )
                )
        if not prices:
            raise ValueError("price table contains no entries")
        return cls(
            document=raw,
            as_of=str(raw.get("_meta", {}).get("as_of", "unknown")),
            prices=prices,
            fallback_pattern="*",
        )

    def match(self, model: str) -> tuple[Price, bool]:
        """Return ``(price, assumed)`` for ``model``.

        ``assumed`` is True when no specific family matched and the fallback was
        used — callers must surface that to the user rather than hide it.
        """
        name = (model or "").lower()
        for price in self.prices:
            if price.pattern != self.fallback_pattern and fnmatch.fnmatch(name, price.pattern):
                return price, False
        for price in self.prices:
            if price.pattern == self.fallback_pattern:
                return price, True
        raise LookupError("price table has no fallback entry")

    def digest(self) -> str:
        return digest_of(self.document)

    def cost(
        self,
        model: str,
        input_tokens: int,
        cached_input_tokens: int,
        output_tokens: int,
    ) -> tuple[Decimal, bool]:
        price, assumed = self.match(model)
        return price.cost(input_tokens, cached_input_tokens, output_tokens), assumed


@dataclass
class ModelTotals:
    model: str
    calls: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    cost: Decimal = Decimal("0")

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": usd(self.cost),
        }


@dataclass
class CostMeter:
    """Accumulates token usage and estimated spend across a session.

    The meter is the single source of truth for cap enforcement: the boundary proxy
    asks it, before forwarding a request, whether forwarding is still allowed.
    """

    price_table: PriceTable
    mode: str = "usd"  # "usd" or "quota" (subscription plans where spend is not metered)
    totals: dict[str, ModelTotals] = field(default_factory=dict)
    cost: Decimal = Decimal("0")
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    reported_calls: int = 0
    estimated_calls: int = 0
    assumed_models: set[str] = field(default_factory=set)

    def record(
        self,
        model: str,
        input_tokens: int,
        cached_input_tokens: int,
        output_tokens: int,
        usage_source: str,
    ) -> Decimal:
        cost, assumed = self.price_table.cost(model, input_tokens, cached_input_tokens, output_tokens)
        if assumed:
            self.assumed_models.add(model or "unknown")
        entry = self.totals.setdefault(model or "unknown", ModelTotals(model=model or "unknown"))
        entry.calls += 1
        entry.input_tokens += input_tokens
        entry.cached_input_tokens += cached_input_tokens
        entry.output_tokens += output_tokens
        entry.cost += cost if self.mode == "usd" else Decimal("0")
        self.calls += 1
        self.input_tokens += input_tokens
        self.cached_input_tokens += cached_input_tokens
        self.output_tokens += output_tokens
        if self.mode == "usd":
            self.cost += cost
        if usage_source == "reported":
            self.reported_calls += 1
        else:
            self.estimated_calls += 1
        return cost

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, Any]:
        if self.mode == "usd":
            if self.estimated_calls and self.reported_calls:
                basis = "mixed: reported and estimated usage"
            elif self.estimated_calls:
                basis = "estimated: provider reported no usage"
            else:
                basis = "reported: provider usage fields"
        else:
            basis = "quota mode: subscription usage is counted in tokens, not dollars"
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": usd(self.cost),
            "cost_basis": basis,
            "usage_reported_calls": self.reported_calls,
            "usage_estimated_calls": self.estimated_calls,
            "assumed_price_models": sorted(self.assumed_models),
            "by_model": [t.to_dict() for t in sorted(self.totals.values(), key=lambda t: -t.cost)],
        }


def estimate_tokens_from_text(text: str) -> int:
    """Rough token estimate for providers that report no usage.

    Four characters per token is the conventional rule of thumb for English text
    with byte-pair tokenisers. It is wrong in both directions; Docket marks every
    number derived this way as ``estimated`` so nobody mistakes it for metered
    usage.
    """
    return max(0, len(text) // 4)


def recompute_totals(events: Iterable[dict[str, Any]], price_document: dict[str, Any]) -> dict[str, Any]:
    """Recompute cost from raw usage in a ledger, for third-party verification.

    This is the function that makes a receipt checkable by someone who does not
    trust Docket's arithmetic: given the *provider-reported* token counts and the
    price table embedded in the receipt, the totals are fully determined.
    """
    table = PriceTable(document=price_document, as_of="", prices=[], fallback_pattern="*")
    prices: list[Price] = []
    for entry in price_document.get("families", []):
        for pattern in [p.strip() for p in str(entry.get("pattern", "*")).split("/") if p.strip()]:
            prices.append(
                Price(
                    pattern=pattern.lower(),
                    input=Decimal(str(entry["input"])),
                    cached_input=Decimal(str(entry.get("cached_input", entry["input"]))),
                    output=Decimal(str(entry["output"])),
                    source=str(entry.get("source", "unspecified")),
                )
            )
    table.prices = prices

    cost = Decimal("0")
    input_tokens = 0
    cached = 0
    output_tokens = 0
    calls = 0
    mismatches: list[dict[str, Any]] = []
    for ev in events:
        if ev.get("type") != "model.response":
            continue
        data = ev.get("data", {})
        usage = data.get("usage") or {}
        model = str(data.get("model") or "unknown")
        it = int(usage.get("input_tokens") or 0)
        ct = int(usage.get("cached_input_tokens") or 0)
        ot = int(usage.get("output_tokens") or 0)
        computed, _assumed = table.cost(model, it, ct, ot)
        recorded = str(data.get("cost_usd", ""))
        if recorded and Decimal(recorded) != computed:
            mismatches.append({"seq": ev.get("seq"), "model": model, "recorded": recorded, "recomputed": usd(computed)})
        cost += computed
        input_tokens += it
        cached += ct
        output_tokens += ot
        calls += 1
    return {
        "calls": calls,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "cost_usd": usd(cost),
        "per_call_mismatches": mismatches,
    }


def price_document_digest(document: dict[str, Any]) -> str:
    return sha256_hex(canonical_bytes(document))
