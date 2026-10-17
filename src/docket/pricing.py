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
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Context, Decimal, DecimalException, InvalidOperation, localcontext
from pathlib import Path
from typing import Any

from .canon import canonical_bytes, digest_of, sha256_hex, usd
from .util import loads_json

BUNDLED = Path(__file__).with_name("prices.json")
MICROS = Decimal("0.000001")

#: The decimal context for pricing. The default context carries 28 significant digits, which is
#: ample for real money and *not* ample for a number that arrived from a provider or a doctored
#: receipt: ``quantize`` raises ``InvalidOperation`` past the context precision, and a raised
#: decimal error is not a payment figure. 64 digits covers every token count and price the input
#: parsers accept; the bounded exponent range refuses a hand-edited price of ``1e999999``
#: by name instead of computing with it.
ARITHMETIC = Context(prec=64, Emax=999_999, Emin=-999_999)

# Limits on numbers that arrive from *outside* Docket — a provider's reported usage, a
# receipt or a ledger supplied by whoever asked for verification, a price table embedded in
# that receipt. None of them is a measurement Docket made, and all of them reach arithmetic.
#
# These are plausibility ceilings, not safety margins: one API response cannot contain a
# quadrillion tokens (`usage` counts tokens, and 10^15 tokens is some twenty million copies of
# everything ever written), and no model has ever been priced at a million dollars per
# million tokens. A claim past a ceiling is refused *by name* — the verifier reports
# "cannot recompute the cost: event 12 input_tokens is implausible: ...", which is a verdict
# a human can act on — rather than being poured into arithmetic until the decimal module
# raises. Refusing a claim is a finding; raising InvalidOperation is a crashed checker.
MAX_CLAIMED_TOKENS_PER_CALL = 10**15
PRICE_CEILING_PER_MTOK = Decimal("1000000")  # $1,000,000 per million tokens
COST_CEILING_PER_CALL = Decimal("1000000000000")  # $1e12 in a single call


@dataclass(frozen=True)
class Price:
    pattern: str
    input: Decimal
    cached_input: Decimal
    output: Decimal
    source: str

    def cost(self, input_tokens: int, cached_input_tokens: int, output_tokens: int) -> Decimal:
        """Price one call. Raises ``ValueError`` — never a decimal exception — on nonsense.

        The token counts here may come from a provider's response body (the runtime path) or
        from a receipt under verification. Both are input from outside Docket, so this
        function's contract is: it returns a ``Decimal``, or it raises ``ValueError`` naming
        the numbers it could not price. A runaway-loop bug that makes the proxy raise
        ``InvalidOperation`` inside a request handler would take the recorder down with the
        run it is supposed to be recording, which is the one outcome a recorder may not have.
        """
        try:
            with localcontext(ARITHMETIC):
                billable_input = max(input_tokens - cached_input_tokens, 0)
                total = (
                    Decimal(billable_input) * self.input
                    + Decimal(cached_input_tokens) * self.cached_input
                    + Decimal(output_tokens) * self.output
                ) / Decimal(1_000_000)
                return total.quantize(MICROS)
        except DecimalException as exc:
            raise ValueError(
                f"the cost of {input_tokens} input / {cached_input_tokens} cached / "
                f"{output_tokens} output tokens cannot be represented: {exc}"
            ) from exc
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "cannot price a call with token counts "
                f"{input_tokens!r}/{cached_input_tokens!r}/{output_tokens!r}: {exc}"
            ) from exc


@dataclass
class PriceTable:
    """A loaded price table with deterministic matching."""

    document: dict[str, Any]
    as_of: str
    prices: list[Price]
    fallback_pattern: str

    @classmethod
    def load(cls, path: str | Path | None = None) -> PriceTable:
        raw = loads_json(Path(path or BUNDLED).read_text(encoding="utf-8"), what=str(path or BUNDLED))
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

    def quote(self, model: str, input_tokens: int, cached_input_tokens: int, output_tokens: int) -> Decimal:
        """Price a call *without* recording it.

        Used for the one thing that must be priced but never counted: a call the
        response cache answered, whose cost is money that was not spent. The
        arithmetic is the meter's own — the same ``PriceTable.cost`` call
        ``record`` makes — so a quoted saving and a recorded cost can never drift
        apart, and verification recomputes both with one rule.
        """
        cost, _assumed = self.price_table.cost(model, input_tokens, cached_input_tokens, output_tokens)
        return cost

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


def _amount(
    value: Any,
    where: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    """Parse an amount from untrusted input, naming the field when it is not one.

    The verifier runs this over a receipt and a ledger supplied by whoever asked for the
    verification. A malformed amount must produce a message a human can act on, not an
    exception out of the decimal module: the party checking the record is not the party
    who wrote it, and "the checker crashed" is not a verification result.

    ``minimum``/``maximum`` are plausibility bounds, applied where a value is a *claim* about
    something physical (a price, a cost) and a stated value outside them is evidence that the
    document is not what it says it is — not merely an unusual number.
    """
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{where} is not an amount: {value!r}")
    if isinstance(value, float):
        raise ValueError(f"{where} must not be a float ({value!r}); amounts are decimal strings")
    try:
        text = str(value).strip()
        if not text:
            raise ValueError(f"{where} is empty")
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"{where} is not a decimal amount: {value!r}") from exc
    if not amount.is_finite():
        raise ValueError(f"{where} is not a finite amount: {value!r}")
    if minimum is not None and amount < minimum:
        raise ValueError(f"{where} must not be below {minimum}: {value!r}")
    if maximum is not None and amount > maximum:
        raise ValueError(f"{where} is implausible: {value!r} exceeds {maximum}")
    return amount


def _count(
    value: Any,
    where: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Parse a whole number from untrusted input (token counts, call counts)."""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{where} is not a count: {value!r}")
    try:
        count = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{where} is not a whole number: {value!r}") from exc
    if minimum is not None and count < minimum:
        raise ValueError(f"{where} must not be below {minimum}: {value!r}")
    if maximum is not None and count > maximum:
        raise ValueError(f"{where} is implausible: {value!r} exceeds {maximum}")
    return count


def recompute_totals(events: Iterable[dict[str, Any]], price_document: dict[str, Any]) -> dict[str, Any]:
    """Recompute cost from raw usage in a ledger, for third-party verification.

    This is the function that makes a receipt checkable by someone who does not
    trust Docket's arithmetic: given the *provider-reported* token counts and the
    price table embedded in the receipt, the totals are fully determined.
    """
    table = PriceTable(document=price_document, as_of="", prices=[], fallback_pattern="*")
    prices: list[Price] = []
    families = price_document.get("families", [])
    if not isinstance(families, list):
        # The price table travels inside the receipt, so its *shape* is as untrusted as its
        # numbers: a table whose "families" is a string would iterate into characters and then
        # raise AttributeError out of the verifier, which is the one outcome these checks may
        # not have. Refusing an object-shaped field by name costs nothing.
        raise ValueError(f"the price table's families must be a list, got {type(families).__name__}")
    for index, entry in enumerate(families):
        if not isinstance(entry, dict):
            raise ValueError(f"price row {index} is not an object: {entry!r}")
        for pattern in [p.strip() for p in str(entry.get("pattern", "*")).split("/") if p.strip()]:
            prices.append(
                Price(
                    pattern=pattern.lower(),
                    input=_amount(
                        entry.get("input"),
                        f"price row {index} ({entry.get('pattern')!r}) input",
                        minimum=Decimal("0"),
                        maximum=PRICE_CEILING_PER_MTOK,
                    ),
                    cached_input=_amount(
                        entry.get("cached_input", entry.get("input")),
                        f"price row {index} ({entry.get('pattern')!r}) cached_input",
                        minimum=Decimal("0"),
                        maximum=PRICE_CEILING_PER_MTOK,
                    ),
                    output=_amount(
                        entry.get("output"),
                        f"price row {index} ({entry.get('pattern')!r}) output",
                        minimum=Decimal("0"),
                        maximum=PRICE_CEILING_PER_MTOK,
                    ),
                    source=str(entry.get("source", "unspecified")),
                )
            )
    table.prices = prices

    cost = Decimal("0")
    assumed_models: set[str] = set()
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
        it = _count(
            usage.get("input_tokens", 0),
            f"event {ev.get('seq')} input_tokens",
            minimum=0,
            maximum=MAX_CLAIMED_TOKENS_PER_CALL,
        )
        ct = _count(
            usage.get("cached_input_tokens", 0),
            f"event {ev.get('seq')} cached_input_tokens",
            minimum=0,
            maximum=MAX_CLAIMED_TOKENS_PER_CALL,
        )
        ot = _count(
            usage.get("output_tokens", 0),
            f"event {ev.get('seq')} output_tokens",
            minimum=0,
            maximum=MAX_CLAIMED_TOKENS_PER_CALL,
        )
        try:
            computed, _assumed = table.cost(model, it, ct, ot)
        except ValueError as exc:
            # Price.cost does not leak decimal exceptions; if it refuses, the refusal names
            # the event it could not price rather than the bare numbers.
            raise ValueError(f"event {ev.get('seq')} cannot be priced: {exc}") from exc
        except LookupError as exc:
            # A price table with no fallback entry cannot price *anything* unknown — and the
            # table travels inside the receipt, so this is a claim about the document, not a
            # bug in Docket. It becomes a named refusal like every other malformed claim.
            raise ValueError(
                f"event {ev.get('seq')} cannot be priced: the receipt's price table has no entry "
                f"matching {model!r} and no fallback ({exc})"
            ) from exc
        recorded = data.get("cost_usd")
        recorded = "" if recorded is None else str(recorded)
        recorded_amount = (
            _amount(
                recorded,
                f"event {ev.get('seq')} cost_usd",
                minimum=Decimal("0"),
                maximum=COST_CEILING_PER_CALL,
            )
            if recorded
            else None
        )
        if recorded_amount is not None and recorded_amount != computed:
            mismatches.append({"seq": ev.get("seq"), "model": model, "recorded": recorded, "recomputed": usd(computed)})
        if _assumed:
            # The assumption used to be dropped here. It is the one thing in a recomputation that a
            # reader cannot redo from the receipt's numbers: a fallback price is a guess about which
            # tariff applied, and spec §9.3 requires it to be visible wherever the cost is.
            assumed_models.add(model)
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
        "assumed_models": sorted(assumed_models),
    }


def price_document_digest(document: dict[str, Any]) -> str:
    return sha256_hex(canonical_bytes(document))
