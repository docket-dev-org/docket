"""Per-call latency: one definition, one implementation, and the method named in the artifact.

Latency is the one number in a run that every operator asks for and no verifier can derive from
first principles — it is a *measurement*, taken around a call, and a receipt that states it is
making a claim about the world rather than about arithmetic. Two consequences shape this module:

* **The method travels with the number.** "p99 = 2,004 ms" is not a fact until it says how it was
  computed: with 15 samples, nearest-rank p99 and linear-interpolated p99 differ, and a reader
  comparing two receipts computed two ways is comparing nothing. Every summary produced here
  carries ``"method": "nearest-rank"``, and the verifier in the other language recomputes with the
  same rule from the same rows.
* **The summary is checkable from the receipt alone.** It is derived from the ``latency_ms``
  values in the appendix's usage rows, so `docket verify` can recompute it — which is what makes
  it admissible in an evidence document rather than a decoration.

The rule, stated precisely enough to reimplement: sort the samples ascending, take
``rank = ceil(p / 100 * n)`` clamped into ``[1, n]``, and read the ``rank``-th smallest
(1-indexed). This is the "nearest-rank" definition; it always returns a value that was actually
measured, which matters when a receipt is being read by someone deciding whether a call took 2.0
seconds or "somewhere around two seconds".
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

#: The percentile rule this module implements. Named in every summary it produces, checked by
#: every verifier that reads one, and normative in ``spec/docket-receipt-1.md`` §6.3.
METHOD = "nearest-rank"

#: Percentiles reported. p50 is the typical call; p95 and p99 are where "the average was fine"
#: stops being true, which for an agent making a hundred calls in a loop is the number that
#: decides whether the run finishes inside its wall-clock cap.
REPORTED = (50, 95, 99)


class LatencyError(ValueError):
    """A latency value that is not a whole, non-negative number of milliseconds.

    Raised rather than coerced: ``12.5`` is a measurement someone forgot to round, ``True`` is a
    bug, and ``-1`` is a clock that moved. Inventing a plausible number for any of those would put
    a fabricated measurement into an evidence document.
    """


def is_latency(value: object) -> bool:
    """True when ``value`` may be recorded as a latency: an ``int`` (not a ``bool``) >= 0."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def as_latency(value: object, *, where: str = "latency_ms") -> int:
    """``value`` as a latency, or raise :class:`LatencyError` naming where it came from."""
    if not is_latency(value):
        raise LatencyError(f"{where} is not a whole number of milliseconds: {value!r}")
    return int(value)  # type: ignore[arg-type]


def percentile_ms(values: Sequence[int], percent: int) -> int:
    """The ``percent``-th percentile of ``values``, nearest-rank, over *unsorted* input."""
    if not values:
        raise LatencyError("percentile of an empty sample")
    if not 1 <= percent <= 100:
        raise LatencyError(f"percentile must be between 1 and 100, not {percent}")
    ordered = sorted(values)
    # ceil, in integer arithmetic: no float rounding, so p99 of 200 rows can never come out as the
    # 197.999…-th value. The two guards above are what keep `rank` inside ``[1, len(ordered)]``
    # (an empty sample and a percentage outside 1..100 both raise before this line), so no clamp is
    # needed here — and a clamp would be unreachable code whose mutants nothing can kill.
    rank = -(-percent * len(ordered) // 100)
    return ordered[rank - 1]


def summary(latencies: Iterable[int], *, total_rows: int | None = None) -> dict[str, int | str] | None:
    """The latency summary for a receipt's appendix, or ``None`` when nothing was measured.

    ``measured_calls`` counts the rows that carried a latency and ``total_rows`` the rows in the
    appendix, so partial coverage is stated in the artifact rather than inferred by whoever reads
    it later: a session where the SDK recorded latency for 12 of 15 calls says exactly that.
    """
    values = [as_latency(value) for value in latencies]
    if not values:
        return None
    out: dict[str, int | str] = {
        "method": METHOD,
        "measured_calls": len(values),
        "total_rows": len(values) if total_rows is None else int(total_rows),
        "total_ms": sum(values),
        "max_ms": max(values),
    }
    for percent in REPORTED:
        out[f"p{percent}_ms"] = percentile_ms(values, percent)
    return out


def check_summary(stated: object, latencies: Sequence[int], total_rows: int) -> list[str]:
    """Every disagreement between a stated summary and the rows it claims to summarise.

    Returns a list of sentences; empty means the summary is exactly what the rows imply. Written
    as a comparison rather than a boolean so the caller can report *which* number is wrong — the
    difference between "this receipt is internally inconsistent" and "the p99 in this receipt is
    2,004 ms but the calls it cites reach 9,140 ms" is the difference between a re-run and a bug
    report.
    """
    if not isinstance(stated, dict):
        return [f"the receipt's latency is not an object: {type(stated).__name__}"]
    problems: list[str] = []
    if stated.get("method") != METHOD:
        problems.append(
            f"latency.method is {stated.get('method')!r}; this verifier recomputes {METHOD!r} and "
            "will not compare a summary computed some other way"
        )
    expected = summary(latencies, total_rows=total_rows)
    if expected is None:
        problems.append("the receipt states a latency summary but no usage row carries latency_ms")
        return problems
    for key in ("measured_calls", "total_rows", "total_ms", "max_ms", *(f"p{p}_ms" for p in REPORTED)):
        if key not in stated:
            problems.append(f"latency.{key} is missing")
        elif stated[key] != expected[key]:
            problems.append(f"latency.{key} is {stated[key]!r} but the rows give {expected[key]!r}")
    for key in stated:
        if key not in expected:
            problems.append(f"latency.{key} is not a member this format defines")
    return problems
