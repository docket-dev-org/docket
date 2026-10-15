"""Per-call latency: the measurement, its summary, and the fact that it is checkable.

Latency is the only number in a receipt that is a *measurement* rather than arithmetic. That makes
two things worth testing hard: the rule itself (a percentile is not a fact until it says how it
was computed, and "p99" over fifteen samples is a different number under a different rule), and
the property that makes the number admissible in an evidence document at all — that
`docket verify` recomputes it from the rows it claims to summarise and refuses a summary that
disagrees.

Everything here is deterministic. The wall-clock timing that *produces* latency is not (it is
whatever the machine did), so it is never asserted on directly: the tests build the values and
check what the code does with them.
"""

from __future__ import annotations

import pytest

from docket.latency import (
    METHOD,
    LatencyError,
    as_latency,
    check_summary,
    is_latency,
    percentile_ms,
    summary,
)
from docket.ledger import Ledger
from docket.receipt import build_receipt
from docket.report import render_markdown, render_receipt
from docket.verify import verify_receipt

# --------------------------------------------------------------------------- #
# The rule
# --------------------------------------------------------------------------- #


def test_a_percentile_always_returns_a_measured_value():
    """Nearest-rank, not interpolation: with 4 samples, p50 is the 2nd smallest — a value that
    actually happened — and never the average of the 2nd and 3rd.

    This is the property that makes the number safe to put in a receipt. A reader deciding whether
    a call took two seconds or "somewhere around two seconds" is asking about a real call, and an
    interpolated percentile answers with a value no call had.
    """
    samples = [40, 10, 30, 20]
    assert percentile_ms(samples, 50) == 20, "rank ceil(0.5*4)=2 -> the 2nd smallest"
    assert percentile_ms(samples, 75) == 30
    assert percentile_ms(samples, 99) == 40
    assert percentile_ms(samples, 100) == 40
    assert percentile_ms(samples, 1) == 10, "rank clamps up to 1 rather than returning nothing"
    assert percentile_ms([7], 99) == 7, "one sample: every percentile is that sample"


def test_a_percentile_does_not_care_how_the_samples_arrived():
    samples = [5, 900, 12, 12, 300]
    assert percentile_ms(samples, 95) == percentile_ms(list(reversed(samples)), 95)
    assert percentile_ms(samples, 95) == sorted(samples)[-1]


def test_the_guards_are_what_keep_the_rank_inside_the_sample():
    """`percentile_ms` has no clamp on the computed rank, and that is a proof obligation rather than
    a shortcut: `1 <= percent <= 100` and a non-empty sample put `ceil(percent*n/100)` inside
    ``[1, n]`` for every input, so a clamp would be unreachable code. These are the extremes of that
    interval — the only two inputs where a clamp could have bound — and both return real measured
    values rather than raising or wrapping around to the other end of the sample."""
    assert percentile_ms([7], 1) == 7
    assert percentile_ms([7], 100) == 7
    assert percentile_ms([1, 2, 3, 4], 1) == 1
    assert percentile_ms([1, 2, 3, 4], 100) == 4
    # And every percentage of every sample size stays inside the sample.
    for size in range(1, 12):
        sample = list(range(1, size + 1))
        for percent in range(1, 101):
            assert percentile_ms(sample, percent) in sample


def test_a_percentile_refuses_an_empty_sample_or_an_impossible_rank():
    with pytest.raises(LatencyError, match="empty sample"):
        percentile_ms([], 50)
    with pytest.raises(LatencyError, match="between 1 and 100"):
        percentile_ms([1], 0)
    with pytest.raises(LatencyError, match="between 1 and 100"):
        percentile_ms([1], 101)


def test_a_latency_must_be_a_whole_non_negative_millisecond_count():
    """`True` is an `int` in Python, `12.5` is a measurement someone forgot to round, and `-1` is
    a clock that moved. All three are refused *by name* rather than coerced into a plausible
    number, because the alternative is a fabricated measurement inside a tamper-evident file."""
    assert is_latency(0) is True
    assert is_latency(12_345) is True
    for hostile in (True, False, -1, 12.5, "12", None, [12], {"ms": 12}):
        assert is_latency(hostile) is False, hostile
        with pytest.raises(LatencyError, match="not a whole number of milliseconds"):
            as_latency(hostile, where="row 3")
    with pytest.raises(LatencyError, match="row 3"):
        as_latency("fast", where="row 3")


def test_the_summary_reports_the_percentile_method_and_both_counts():
    """The method travels with the number, and so does coverage.

    A session where the SDK timed 12 of 15 calls must say so in the artifact: `measured_calls`
    12 and `total_rows` 15. Reading the p99 as if it covered the whole session is exactly the
    mistake this member exists to prevent.
    """
    payload = summary([10, 20, 30, 40], total_rows=6)
    assert payload == {
        "method": METHOD,
        "measured_calls": 4,
        "total_rows": 6,
        "total_ms": 100,
        "max_ms": 40,
        "p50_ms": 20,
        "p95_ms": 40,
        "p99_ms": 40,
    }
    assert summary([10], total_rows=1)["total_rows"] == 1
    assert summary([]) is None, "nothing measured is reported as nothing, not as zeroes"


def test_the_rule_this_module_states_is_the_rule_it_implements():
    """The docstring says: sort ascending, rank = ceil(p/100 * n) clamped into [1, n]. Written out
    as a loop over every percentile of a known sample, so that a change to the implementation has
    to argue with the sentence rather than quietly redefining p99."""
    samples = [3, 1, 4, 1, 5, 9, 2, 6]
    ordered = sorted(samples)
    for percent in range(1, 101):
        rank = -(-percent * len(ordered) // 100)
        assert percentile_ms(samples, percent) == ordered[min(max(rank, 1), len(ordered)) - 1], percent


# --------------------------------------------------------------------------- #
# The summary in a receipt
# --------------------------------------------------------------------------- #


def _receipt_with_latencies(ledger: Ledger, make_like, latencies: list[int | None]):
    """Build a receipt whose usage rows carry exactly these latencies (``None`` = no measurement)."""

    from docket.policy import Caps, Policy
    from docket.pricing import CostMeter, PriceTable

    table = PriceTable.load()
    meter = CostMeter(price_table=table)
    for index, latency in enumerate(latencies):
        body = {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hi"}]}
        ledger.append(
            "model.request",
            {"provider": "anthropic", "model": "claude-sonnet-5", "fingerprint": f"fp{index}", "body": body},
        )
        cost = meter.record("claude-sonnet-5", 1000, 0, 200, "reported")
        data = {
            "provider": "anthropic",
            "model": "claude-sonnet-5",
            "status": 200,
            "usage": {"input_tokens": 1000, "cached_input_tokens": 0, "output_tokens": 200},
            "usage_source": "reported",
            "cost_usd": str(cost),
        }
        if latency is not None:
            data["latency_ms"] = latency
        ledger.append("model.response", data)
    policy = Policy(name="t", caps=Caps(max_usd=None))
    return build_receipt(
        events=list(ledger),
        ledger=ledger,
        meter=meter,
        policy=policy,
        session_id="s",
        docket_number="DKT-20260101-ABC123",
        exit_code=0,
        duration_seconds=1.0,
        price_table=table,
    )


def test_rows_carry_latency_only_when_the_call_was_measured(tmp_path):
    """A row asserts a latency only if the event has one. The SDK and the boundary time their own
    round trips; an agent that writes its own ``model.response`` may not have a number at all, and
    the row then claims none — ``measured_calls`` 2 of 3 rows says exactly that."""
    ledger = Ledger.open(tmp_path / "l.jsonl")
    receipt = _receipt_with_latencies(ledger, None, [10, 20, None])
    ledger.close()

    rows = receipt["appendix"]["usage"]
    assert [row.get("latency_ms") for row in rows] == [10, 20, None]
    assert receipt["latency"] == {
        "method": METHOD,
        "measured_calls": 2,
        "total_rows": 3,
        "total_ms": 30,
        "max_ms": 20,
        "p50_ms": 10,
        "p95_ms": 20,
        "p99_ms": 20,
    }


def test_a_receipt_without_latency_says_nothing_rather_than_zero(tmp_path):
    ledger = Ledger.open(tmp_path / "l.jsonl")
    receipt = _receipt_with_latencies(ledger, None, [None, None])
    ledger.close()
    assert "latency" not in receipt, "no measurement is not the same as a measurement of zero"


def test_a_float_latency_cannot_even_be_recorded_and_other_hostile_values_never_reach_a_row(tmp_path):
    """The canonical form is the first defence, and the row builder is the second.

    A float is refused *by the ledger* — `12.5` cannot be hashed (spec §3.2), so `append` raises
    and the value never reaches a file. The values the canonical form does allow — `True` is a
    JSON boolean, `-3` an integer, `"20"` a string — are recorded faithfully in the event and then
    *not* presented as a measurement by the row builder: a row asserts a latency only when it has
    an integer non-negative count, and the rest of the row survives untouched.
    """
    ledger = Ledger.open(tmp_path / "l.jsonl")
    ledger.append("model.request", {"provider": "anthropic", "model": "claude-sonnet-5", "body": {}})

    with pytest.raises(TypeError, match="non-integer number 12.5"):
        ledger.append(
            "model.response",
            {
                "provider": "anthropic",
                "model": "claude-sonnet-5",
                "status": 200,
                "usage": {"input_tokens": 1000, "cached_input_tokens": 0, "output_tokens": 200},
                "cost_usd": "0.018",
                "latency_ms": 12.5,
            },
        )

    for hostile in (True, -3, "20"):
        ledger.append(
            "model.response",
            {
                "provider": "anthropic",
                "model": "claude-sonnet-5",
                "status": 200,
                "usage": {"input_tokens": 1000, "cached_input_tokens": 0, "output_tokens": 200},
                "usage_source": "reported",
                "cost_usd": "0.018",
                "latency_ms": hostile,
            },
        )
    ledger.append(
        "model.response",
        {
            "provider": "anthropic",
            "model": "claude-sonnet-5",
            "status": 200,
            "usage": {"input_tokens": 1000, "cached_input_tokens": 0, "output_tokens": 200},
            "usage_source": "reported",
            "cost_usd": "0.018",
            "latency_ms": 42,
        },
    )
    receipt = _receipt_with_latencies(ledger, None, [])
    ledger.close()

    rows = receipt["appendix"]["usage"]
    assert [row.get("latency_ms") for row in rows] == [
        None,
        None,
        None,
        42,
    ], "only the integer, non-negative value is presented as a measurement"
    assert [row["cost_usd"] for row in rows] == ["0.018"] * 4, "and every row still carries its cost"
    assert receipt["latency"]["measured_calls"] == 1
    assert receipt["latency"]["total_rows"] == 4


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #


def _verified(receipt: dict, ledger_path=None):
    return verify_receipt(receipt, ledger_path=ledger_path)


def _check(result, name):
    matches = [check for check in result.checks if check.name == name]
    assert len(matches) == 1, f"expected exactly one {name} check, got {len(matches)}"
    return matches[0]


def test_the_summary_is_recomputed_by_the_verifier_and_a_faked_one_is_caught(tmp_path):
    """The point of the whole exercise: a receipt that *states* a p99 its own rows contradict does
    not verify. Without this check the numbers would be decoration — a receipt is a
    tamper-evident document, and a fabricated measurement inside one is worse than none."""
    ledger = Ledger.open(tmp_path / "l.jsonl")
    receipt = _receipt_with_latencies(ledger, None, [10, 20, 30, 40, 5000])
    ledger.close()

    honest = _verified(receipt)
    check = _check(honest, "latency-summary")
    assert check.status == "pass"
    assert "p50 30 ms" in check.detail and "p99 5000 ms" in check.detail and "5 measured call(s)" in check.detail
    assert honest.ok is True

    for field_name, faked in (
        ("p99_ms", 30),
        ("max_ms", 40),
        ("total_ms", 100),
        ("measured_calls", 4),
        ("total_rows", 4),
    ):
        tampered = {**receipt, "latency": {**receipt["latency"], field_name: faked}}
        result = _verified(tampered)
        check = _check(result, "latency-summary")
        assert check.status == "fail", field_name
        assert field_name in check.detail, check.detail
        assert result.ok is False, f"a faked {field_name} must not verify"

    # The rule is part of the claim: a summary computed some other way is not comparable, so it is
    # refused rather than recomputed under a guess.
    other_rule = {**receipt, "latency": {**receipt["latency"], "method": "linear-interpolation"}}
    assert _check(_verified(other_rule), "latency-summary").status == "fail"

    # And a member the format does not define is refused too: an invented field in an evidence
    # document is a claim nobody can check.
    invented = {**receipt, "latency": {**receipt["latency"], "stddev_ms": 12}}
    assert _check(_verified(invented), "latency-summary").status == "fail"


def test_the_check_returns_a_list_of_sentences_and_says_nothing_when_it_agrees():
    """The verifier's contract, stated directly: `check_summary` hands back the *reasons*, not a
    boolean and not a truthy nothing.

    The distinction is not cosmetic. `verify.py` prints `"; ".join(problems[:3])`, and a caller
    that received `None` instead of an empty list would be entitled to treat "no problems" and "I
    could not tell" as the same answer — in a tool whose whole job is to distinguish those two
    things. So an honest summary must produce exactly `[]`, and every problem must be a non-empty
    string.
    """
    latencies = [10, 20, 30, 40, 5000]
    honest = summary(latencies, total_rows=len(latencies))
    assert check_summary(honest, latencies, len(latencies)) == []

    wrong = {**honest, "p99_ms": 30}
    problems = check_summary(wrong, latencies, len(latencies))
    assert isinstance(problems, list) and problems, problems
    assert all(isinstance(problem, str) and problem for problem in problems)
    assert any("p99_ms" in problem for problem in problems)


def test_a_latency_member_that_is_not_an_object_is_a_failure_not_a_skip():
    """A receipt that carries `latency` and puts something other than an object there is making a
    claim that cannot be checked — which is a failure, not an absence.

    The distinction is exact: an *absent* member means "this tool did not time calls" and skips,
    while a *present* member holding a string, a list, a number or null means someone wrote
    something into a tamper-evident document that the verifier cannot recompute. Reading those two
    as the same thing would let a receipt claim a p99 it invented, spelled as `"latency": "p99
    23ms"`, and pass.
    """
    ledger = Ledger.in_memory()
    receipt = _receipt_with_latencies(ledger, None, [10, 20, 30])
    assert _check(_verified(receipt), "latency-summary").status == "pass"

    for junk in (None, "nearest-rank", [], 23, {"method": "nearest-rank"}):
        tampered = {**receipt, "latency": junk}
        check = _check(_verified(tampered), "latency-summary")
        assert check.status == "fail", junk
        assert "latency" in check.detail, check.detail
    # A present-but-null member is the one that looks most like an absence, so it gets its own
    # assertion on the words the operator will actually read.
    nulled = _check(_verified({**receipt, "latency": None}), "latency-summary")
    assert "NoneType" in nulled.detail, nulled.detail
    # And an object that is missing the members the format defines is missing them, not fine.
    partial = _check(_verified({**receipt, "latency": {"method": "nearest-rank"}}), "latency-summary")
    assert "is missing" in partial.detail, partial.detail


def test_a_receipt_without_latency_skips_the_check_rather_than_guessing():
    """A receipt with no latency at all — a ledger recorded by a tool that does not time calls —
    gets a *skip with a reason*, not a pass. Nothing was measured, so nothing is claimed."""
    ledger = Ledger.in_memory()
    receipt = _receipt_with_latencies(ledger, None, [])
    check = _check(_verified(receipt), "latency-summary")
    assert check.status == "skip"
    assert "no per-call latency" in check.detail


def test_a_stated_summary_over_rows_that_were_never_timed_cannot_pass(tmp_path):
    """The one shape that would let a fabricated measurement through: a receipt that *states* a
    latency summary while none of its usage rows carries a latency to check it against.

    Mutation testing found this hole rather than a reviewer: every other branch of `check_summary`
    had a test, and replacing this branch's `return problems` with `return None` changed nothing —
    because the verifier reads a falsy return as "no disagreement", so a receipt could claim any
    p99 at all as long as its rows were silent. The rules are asymmetric on purpose: this is an
    *uncheckable claim*, so it fails, and it says which claim could not be checked.
    """
    ledger = Ledger.open(tmp_path / "l.jsonl")
    receipt = _receipt_with_latencies(ledger, None, [10, 20, 30])
    ledger.close()
    assert _check(_verified(receipt), "latency-summary").status == "pass"

    # Keep the summary, take the measurements away. Nothing about the claim can now be recomputed.
    stripped = {
        **receipt,
        "appendix": {
            "usage": [{k: v for k, v in row.items() if k != "latency_ms"} for row in receipt["appendix"]["usage"]]
        },
    }
    result = _verified(stripped)
    check = _check(result, "latency-summary")
    assert check.status == "fail", check.detail
    assert "no usage row carries latency_ms" in check.detail, check.detail
    assert result.ok is False


def test_a_malformed_latency_in_a_row_is_a_failure_not_a_silent_drop(tmp_path):
    """A row carrying ``latency_ms: 12.5`` is a row whose claim cannot be checked. The verifier
    says so: dropping it would let a receipt state a summary that quietly ignores the rows that
    disagree with it."""
    ledger = Ledger.open(tmp_path / "l.jsonl")
    receipt = _receipt_with_latencies(ledger, None, [10, 20])
    ledger.close()

    receipt["appendix"]["usage"][1]["latency_ms"] = 12.5
    result = _verified(receipt)
    check = _check(result, "latency-summary")
    assert check.status == "fail"
    assert "12.5" in check.detail
    assert result.ok is False


def test_the_summary_counts_every_row_even_the_ones_that_were_not_timed(tmp_path):
    """Coverage is checked, not inferred: a receipt that claims to summarise 2 calls while its
    appendix holds 3 rows is making a claim about a session it did not describe."""
    ledger = Ledger.open(tmp_path / "l.jsonl")
    receipt = _receipt_with_latencies(ledger, None, [10, 20, None])
    ledger.close()
    assert _check(_verified(receipt), "latency-summary").status == "pass"

    understated = {**receipt, "latency": {**receipt["latency"], "total_rows": 2}}
    check = _check(_verified(understated), "latency-summary")
    assert check.status == "fail" and "total_rows" in check.detail


def test_an_unreadable_appendix_cannot_produce_a_latency_pass(tmp_path):
    ledger = Ledger.open(tmp_path / "l.jsonl")
    receipt = _receipt_with_latencies(ledger, None, [10, 20])
    ledger.close()

    receipt["appendix"]["usage"] = "not a list"
    check = _check(_verified(receipt), "latency-summary")
    assert check.status == "fail"
    assert "not readable" in check.detail


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def test_the_report_shows_the_tail_not_just_the_average(tmp_path):
    """p50 alone is the number that hides the problem: an agent loop whose median call is 12 ms and
    whose p99 is 5 s is a loop that will not finish inside its wall-clock cap. The panel and the
    markdown both carry the tail, and both say how the percentile was computed."""
    ledger = Ledger.open(tmp_path / "l.jsonl")
    receipt = _receipt_with_latencies(ledger, None, [10, 20, 30, 40, 5000])
    ledger.close()

    panel = render_receipt(receipt, color=False, show_findings=False)
    latency_line = next(line for line in panel.splitlines() if "latency" in line)
    assert "p50 30ms" in latency_line
    assert "p99 5.0s" in latency_line
    assert "max 5.0s" in latency_line
    assert "nearest-rank" in latency_line
    assert "\033[" not in panel

    markdown = render_markdown(receipt)
    row = next(line for line in markdown.splitlines() if line.startswith("| latency |"))
    assert "p99 5.0s" in row and "5 measured call(s)" in row


def test_the_report_omits_the_line_when_nothing_was_measured():
    from docket.report import diff_receipts, render_diff  # noqa: F401  (import cost kept honest)

    ledger = Ledger.in_memory()
    receipt = _receipt_with_latencies(ledger, None, [None])
    panel = render_receipt(receipt, color=False, show_findings=False)
    assert not [line for line in panel.splitlines() if "latency" in line]
    assert not [line for line in render_markdown(receipt).splitlines() if line.startswith("| latency |")]


def test_partial_coverage_is_stated_in_the_report_too(tmp_path):
    ledger = Ledger.open(tmp_path / "l.jsonl")
    receipt = _receipt_with_latencies(ledger, None, [10, 20, None])
    ledger.close()
    line = next(line for line in render_receipt(receipt, color=False).splitlines() if "latency" in line)
    assert "2 call(s) of 3 call(s)" in line, line
