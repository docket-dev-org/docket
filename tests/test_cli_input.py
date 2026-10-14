"""Numeric input at the command line: the caps a user can spell wrong.

The cap is the guard this tool exists for, so "the user typed it wrong" must never mean
"the guard is off". Two defects motivated this file, both found by typing nonsense at `docket
run`:

* `--max-usd nan` was accepted and **silently disabled the cap** — NaN compares false against
  every threshold, so the run completed, filed a normal-looking receipt and reported `$NaN cap`.
* `--max-usd abc` printed `[<class 'decimal.ConversionSyntax'>]` and exited `1`.

A bad value is a usage error: exit `2`, the flag named, the value quoted, and no run.
"""

from __future__ import annotations

import argparse
from decimal import Decimal

import pytest

from docket.cli import MAX_CAP_USD, _at_least_one, _cap_amount, _positive_minutes, _whole_number, main
from docket.policy import Caps


@pytest.mark.parametrize("text", ["nan", "NaN", "inf", "-inf", "Infinity"])
def test_a_cap_that_is_not_finite_is_refused_at_the_flag(text: str):
    """`inf` reads like "no limit" and `nan` behaves like one; neither is a budget. An unset cap
    is spelled by omitting the flag, and the receipt discloses it as unset."""
    with pytest.raises(argparse.ArgumentTypeError) as caught:
        _cap_amount(text)
    assert "finite" in str(caught.value)


@pytest.mark.parametrize("text", ["abc", "", "1,000", "5.0.0", "0x10"])
def test_a_cap_that_is_not_a_number_is_refused(text: str):
    with pytest.raises(argparse.ArgumentTypeError) as caught:
        _cap_amount(text)
    assert "number" in str(caught.value)


def test_a_negative_cap_is_refused():
    with pytest.raises(argparse.ArgumentTypeError) as caught:
        _cap_amount("-5")
    assert "negative" in str(caught.value)


def test_an_absurd_cap_is_refused_where_it_was_typed():
    with pytest.raises(argparse.ArgumentTypeError) as caught:
        _cap_amount("1e300")
    assert "exceed" in str(caught.value)
    assert _cap_amount(str(MAX_CAP_USD)) == str(MAX_CAP_USD)


def test_a_real_amount_survives_as_written():
    assert _cap_amount(" 0.05 ") == "0.05"
    assert Decimal(_cap_amount("1234.567")) == Decimal("1234.567")


@pytest.mark.parametrize("text", ["0", "-0", "-1"])
def test_minutes_must_be_positive(text: str):
    """A wall-clock cap of zero stops the run before the agent starts — a cap spelled as a flag."""
    with pytest.raises(argparse.ArgumentTypeError) as caught:
        _positive_minutes(text)
    assert "more than zero" in str(caught.value)


def test_minutes_convert_to_seconds():
    assert _positive_minutes("0.5") == 30
    assert _positive_minutes("2") == 120


@pytest.mark.parametrize("text", ["2.5", "abc", ""])
def test_a_whole_number_refuses_fractions_and_words(text: str):
    with pytest.raises(argparse.ArgumentTypeError):
        _whole_number(text)


def test_a_rate_must_be_at_least_one():
    with pytest.raises(argparse.ArgumentTypeError) as caught:
        _at_least_one("0")
    assert "at least 1" in str(caught.value)
    assert _at_least_one("60") == 60


@pytest.mark.parametrize(
    "argv",
    [
        ["run", "--max-usd", "nan", "--", "/bin/echo", "hi"],
        ["run", "--max-usd", "inf", "--", "/bin/echo", "hi"],
        ["run", "--max-usd", "-1", "--", "/bin/echo", "hi"],
        ["run", "--max-usd", "abc", "--", "/bin/echo", "hi"],
        ["run", "--max-tokens", "abc", "--", "/bin/echo", "hi"],
        ["run", "--max-calls", "-3", "--", "/bin/echo", "hi"],
        ["run", "--max-minutes", "0", "--", "/bin/echo", "hi"],
        ["run", "--max-events", "2.5", "--", "/bin/echo", "hi"],
        ["run", "--max-calls-per-minute", "0", "--", "/bin/echo", "hi"],
        ["init", "--max-usd", "nan", "--path", "p.toml"],
        ["demo", "--max-usd", "nan"],
        ["chaos", "--max-usd", "inf"],
        ["game-day", "--max-usd", "nan"],
    ],
)
def test_the_commands_refuse_a_bad_cap_without_running_anything(argv, tmp_path, monkeypatch, capsys):
    """Exit `2`, the flag named, and nothing created: the refusal happens before any work."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as caught:
        main(argv)
    assert caught.value.code == 2, argv
    err = capsys.readouterr().err
    assert "error: argument" in err, err
    flag = next(item for item in argv if item.startswith("--max"))
    assert flag in err, err
    assert not list(tmp_path.iterdir()), "a rejected flag must not leave anything behind"


def test_the_policy_object_refuses_the_same_values_when_set_directly():
    """The CLI is not the only caller: the SDK, tests and examples construct policies, and
    `max_usd` used to be assignable to anything after construction."""
    caps = Caps(max_usd=Decimal("1"))
    for bad in ("nan", "inf", "-1"):
        with pytest.raises(ValueError):
            caps.set_max_usd(bad)
    assert caps.max_usd == Decimal("1"), "a refused assignment must not have changed the cap"
    caps.set_max_usd(None)
    assert caps.max_usd is None
    caps.set_max_usd("0.25")
    assert caps.max_usd == Decimal("0.25")


def test_a_cap_still_works_end_to_end(tmp_path, monkeypatch, capsys):
    """The point of all of the above: a *valid* cap still runs, and still says what it is."""
    monkeypatch.chdir(tmp_path)
    assert main(["run", "--max-usd", "0.50", "--", "/bin/echo", "hello"]) == 0
    out = capsys.readouterr()
    combined = out.out + out.err
    assert "$0.50 cap" in combined
