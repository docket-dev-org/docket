"""The mutation harness's own tests.

`tools/mutate.py` produces every mutation score this repository quotes, and until this file existed
it had no tests at all: the numbers in `docs/testing.md` rested on a tool that nothing checked, which
is the same shape of gap as a conformance vector nothing ran. The harness is mostly pure functions —
find the sites, splice one, decide what pytest's exit code meant — so the parts that can be tested
quickly are tested here, and the parts that need a sweep are tested with a stand-in `_run_tests` that
returns the outcomes instead of running them.

What is *not* tested here: that a real sweep's numbers are correct, which is a claim about the whole
suite rather than about this file (the reports in `docs/testing.md` are the evidence for that), and
the two defensive branches the module itself marks unreachable.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location("docket_mutate_under_test", ROOT / "tools" / "mutate.py")
mutate = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules["docket_mutate_under_test"] = mutate
spec.loader.exec_module(mutate)


#: A fixture with one of every site the harness claims to find, in a shape where the splice is easy
#: to check by eye: each statement is on its own line, so a correct mutant differs from the original
#: on exactly one line.
FIXTURE = '''"""A fixture with one of every kind of site."""

import math


class Thing:
    def __init__(self) -> None:
        self.flag = True
        self.count = 0
        self.other = False

    def compare(self, left: int, right: int) -> bool:
        return left < right

    def boolean(self, first: bool, second: bool) -> bool:
        return first and second

    def negate(self, value: bool) -> bool:
        return not value

    def clamp(self, value: int, low: int, high: int) -> int:
        return max(low, min(value, high))

    def describe(self) -> str:
        return "a fixture"

    def early(self) -> bool:
        if self.flag:
            return True
        return False

    def arithmetic(self, value: int) -> int:
        return math.floor(value / 2 + 0)
'''


def _changed_lines(before: str, after: str) -> list[int]:
    return [
        index
        for index, (one, other) in enumerate(zip(before.splitlines(), after.splitlines(), strict=False))
        if one != other
    ]


def test_the_splice_is_a_byte_for_byte_no_op_when_nothing_changes():
    """The harness's falsification of itself, on a fixture rather than on a module.

    `splice_only` applies every site's replacement *verbatim* and must return the input unchanged;
    if it does not, the harness is editing text it did not mean to edit and every score it printed
    is suspect. The real sweep refuses to report a score when this fails, so the property is what
    stands between a bug in `_span` and a number in the documentation.
    """
    assert mutate.splice_only(FIXTURE) == FIXTURE == mutate.splice_only(mutate.splice_only(FIXTURE))


def test_every_mutant_changes_exactly_one_line_and_still_compiles():
    """A mutant is a single-site change, and a file that does not parse is not a behaviour change.

    Asserted as three properties over every mutant of the fixture: it differs from the original
    somewhere, it differs on exactly one line, and it compiles. The one-line property is the one
    that matters most — a mutant that edited two lines would make a surviving verdict say nothing
    about either site — and it is checkable because the fixture is written one statement per line.
    """
    mutants = mutate.mutants_for_source(FIXTURE, "fixture.py")
    assert len(mutants) >= 12, f"the fixture stopped producing mutants: {[m.describe() for m in mutants]}"
    for mutant in mutants:
        assert mutant.source != FIXTURE, mutant.describe()
        changed = _changed_lines(FIXTURE, mutant.source)
        assert len(changed) == 1, f"{mutant.describe()} changed lines {changed}"
        assert changed[0] == mutant.line - 1, f"{mutant.describe()} was spliced on a different line"
        compile(mutant.source, mutant.file, "exec")


def test_the_fixture_covers_every_operator_the_harness_claims_to_mutate():
    """A canary for `_site_kind`: an operator that stops being found is a silent score increase.

    If `_site_kind` loses a branch, the sites it used to produce simply do not exist any more, and
    every score computed afterwards is higher for a reason nobody chose. The set of operators the
    fixture yields is therefore asserted exactly; adding an operator to the harness without a
    fixture site fails here rather than passing quietly.
    """
    found = {mutant.operator for mutant in mutate.mutants_for_source(FIXTURE, "fixture.py")}
    assert found == {
        "bool",
        "boolop",
        "compare",
        "drop-not",
        "int",
        "minmax",
        "return-none",
    }, sorted(found)
    # `string` is opt-in: mutating a message changes what users read, so it is asked for by name.
    with_strings = {
        mutant.operator for mutant in mutate.mutants_for_source(FIXTURE, "fixture.py", include_strings=True)
    }
    assert "string" in with_strings


def test_a_site_on_a_line_with_multi_byte_text_splices_by_character_not_by_byte():
    """The offsets the harness uses are character offsets, and the fixture proves it matters.

    `ast` reports `col_offset` in *bytes* and `_span` adds it to a character offset into the source;
    for a line containing a non-ASCII character before the mutated token the two disagree, and a
    mutant spliced by byte offset would corrupt the line — the replacement landing mid-character.
    The fixture line below keeps an em dash before the `or`, which is where the two readings differ,
    and the assertion is that the em dash survives the splice intact.
    """
    source = 'def message(text: str) -> str:\n    return text or "—nothing to say—"\n'
    drops: list[dict[str, object]] = []
    mutants = mutate.mutants_for_source(source, "fixture.py", drops=drops)
    assert not drops, f"the fixture produced no mutant for a site that exists: {drops}"
    by_operator = {mutant.operator: mutant for mutant in mutants}
    assert set(by_operator) == {"boolop", "return-none"}, sorted(by_operator)
    # The em dashes are *before* the boolean operator in the line, so a byte offset is four
    # characters ahead of the character index: the splice used to land inside the closing em dash,
    # produce text that did not compile, and drop both mutants without a word.
    # The replacement is unparsed, so quote style is normalised and only the text is preserved.
    assert by_operator["boolop"].source.splitlines()[1] == "    return text and '—nothing to say—'"
    assert by_operator["return-none"].source.splitlines()[1] == "    return None"
    for mutant in mutants:
        compile(mutant.source, mutant.file, "exec")


def test_every_operator_label_is_the_symbol_a_reader_expects():
    """The report is read by people; `describe()` is what they read.

    A mutant's `from`/`to` go into the JSON report and the markdown table beside a line number, so a
    wrong label is a wrong instruction for whoever re-checks the survivor by hand.
    """
    by_operator = {mutant.operator: mutant for mutant in mutate.mutants_for_source(FIXTURE, "fixture.py")}
    assert (by_operator["compare"].before, by_operator["compare"].after) == ("<", "<=")
    assert (by_operator["boolop"].before, by_operator["boolop"].after) == ("and", "or")
    assert (by_operator["bool"].before, by_operator["bool"].after) == ("True", "False")
    assert (by_operator["int"].before, by_operator["int"].after) == ("0", "1")
    assert (by_operator["drop-not"].before, by_operator["drop-not"].after) == ("not x", "x")
    assert by_operator["minmax"].before in {"min", "max"}
    assert by_operator["minmax"].after in {"min", "max"}
    assert by_operator["minmax"].before != by_operator["minmax"].after
    assert by_operator["return-none"].after == "return None"
    assert by_operator["compare"].describe() == f"fixture.py:{by_operator['compare'].line} [compare] < -> <="


def test_the_sampling_is_seeded_and_the_same_seed_picks_the_same_mutants():
    """Two runs of one command have to be comparable, which means the sample has to be a function of
    the seed and nothing else.

    Sorting the chosen *indices* rather than the mutants keeps a run's order stable, so a report can
    be read beside the log line by line; the assertion here is that the same seed gives the same
    sequence and a different seed gives a different one.
    """
    import random

    mutants = mutate.mutants_for_source(FIXTURE, "fixture.py")

    def sample(seed: int, count: int) -> list[str]:
        rng = random.Random(seed)
        order = list(range(len(mutants)))
        rng.shuffle(order)
        chosen = sorted(order[:count]) if count < len(mutants) else order
        return [mutants[index].describe() for index in chosen]

    assert sample(0, 5) == sample(0, 5)
    assert sample(0, 5) != sample(1, 5)
    assert len(sample(0, 5)) == 5


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    """A throwaway tree with a `tests/` directory `_run_tests` can be pointed at."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "src").mkdir()
    return tmp_path


def _write_case(sandbox: Path, body: str) -> None:
    (sandbox / "tests" / "test_case.py").write_text(body, encoding="utf-8")


def test_the_verdict_comes_from_pytest_and_the_six_outcomes_are_distinguished(sandbox):
    """`_run_tests` is the whole interface between the suite and the score.

    Six outcomes matter and each is asserted with a real pytest run: a passing suite *survives*, a
    failing one is *killed*, a collection error is *killed-import* (a copy that cannot be collected
    is not evidence about behaviour), and a suite that never returns is a *timeout* — reported beside
    the score, never as a kill. The last one is the reason the function exists at all rather than a
    bare `subprocess.run`: a mutant that hangs must not be counted as caught.
    """
    _write_case(sandbox, "def test_ok():\n    assert True\n")
    outcome, detail = mutate._run_tests(sandbox, ["tests/test_case.py"], 60)
    assert outcome == "survived", (outcome, detail)

    _write_case(sandbox, "def test_no():\n    assert False, 'this mutant is caught'\n")
    outcome, detail = mutate._run_tests(sandbox, ["tests/test_case.py"], 60)
    assert outcome == "killed", (outcome, detail)

    _write_case(sandbox, "import a_module_that_is_not_installed_anywhere\n")
    outcome, detail = mutate._run_tests(sandbox, ["tests/test_case.py"], 60)
    assert outcome == "killed-import", (outcome, detail)

    _write_case(sandbox, "import time\n\n\ndef test_slow():\n    time.sleep(30)\n")
    outcome, detail = mutate._run_tests(sandbox, ["tests/test_case.py"], 1)
    assert outcome == "timeout", (outcome, detail)
    assert "no result within 1s" in detail


def test_the_control_run_means_the_harness_refuses_a_broken_baseline(monkeypatch, tmp_path):
    """A score is only meaningful against a sandbox that passes the suite unmutated.

    If the copy is already red, every mutant would be scored as *killed* against a broken baseline
    and the report would read as a perfect score for tests that never ran. The sweep raises instead
    of reporting, and names what it saw.
    """
    monkeypatch.setattr(mutate, "_run_tests", lambda *args: ("killed", "the copy was already red"))
    with pytest.raises(RuntimeError, match="does not pass the suite before any mutation"):
        mutate.run_mutation_sweep(
            files=["src/docket/latency.py"],
            tests_for={"src/docket/latency.py": ["tests/test_latency.py"]},
            fallback_tests=["tests"],
            max_mutants=2,
            seed=0,
            timeout=5,
            include_strings=False,
            budget_seconds=60,
            log=lambda _message: None,
        )


def test_timeouts_are_reported_beside_the_score_and_never_folded_into_it(monkeypatch):
    """The scoring contract, with the outcome of each mutant decided by the stand-in.

    Four mutants, three verdicts: one killed, one survived, one killed by an import error, one
    timed out. The denominator is what produced a verdict (three), the score is 2/3, and the timeout
    is reported separately — a mutant that hangs is evidence that the suite noticed *something* but
    it is not a caught behaviour change, and folding it in would inflate every score in the tree.
    """
    scripted = ["survived", "killed", "survived", "timeout", "killed-import"]
    calls: list[str] = []

    def fake_run_tests(sandbox, tests, timeout):
        outcome = scripted[len(calls)]
        calls.append(outcome)
        return outcome, f"stand-in: {outcome}"

    monkeypatch.setattr(mutate, "_run_tests", fake_run_tests)
    report = mutate.run_mutation_sweep(
        files=["src/docket/latency.py"],
        tests_for={"src/docket/latency.py": ["tests/test_latency.py"]},
        fallback_tests=["tests"],
        max_mutants=4,
        seed=0,
        timeout=5,
        include_strings=False,
        budget_seconds=600,
        log=lambda _message: None,
    )
    assert calls == scripted
    assert (report["run"], report["killed"], report["killed_by_import"]) == (4, 2, 1)
    assert (report["survived"], report["timed_out"]) == (1, 1)
    assert report["score_denominator"] == 3, "the timeout is not in the denominator"
    assert report["score"] == round(2 / 3, 4)
    assert [item["outcome"] for item in report["survivors"]] == ["survived"]
    assert report["only_lines"] == []
    assert report["sandbox_kept"] is False
    assert report["candidates"] >= report["run"]
    assert len(report["mutants"]) == 4 and all("detail" in item for item in report["mutants"])


def test_the_control_detail_is_pytest_s_own_counts_line(tmp_path):
    """The counts are in the report only if pytest prints them, so the real invocation is asserted.

    `pyproject.toml` carries `-q` in `addopts`; passing `-q` in the harness as well makes the run
    `-qq`, and pytest 9 prints no counts line at all at that level. The harness would then report a
    control with no counts — the exact silence the counts were added to break. This is a real
    pytest run on a one-test tree, so the flags are checked by their effect rather than by reading
    them: one test, one line back, and the line parses to that test.
    """
    tree = _tiny_tree(tmp_path)
    outcome, detail = mutate._run_tests(tree, ["tests/test_mod.py"], 120)
    assert outcome == "survived", detail
    assert mutate.pytest_counts(detail) == {"passed": 1}, detail


def test_the_control_names_the_interpreter_that_ran_the_subset(monkeypatch, tmp_path):
    """A score measured under a different interpreter is a different measurement: the report says which.

    `sys.version.split()[0]` is the version string; the subscript `[0]` is a site the harness mutates
    (to `[1]`), and `[1]` is not a version — `sys.version` reads `3.13.16 (main, ...)`, so splitting it
    and taking the second word records `(main,` beside the score. The assertion is the contract that
    field exists for, checked against the interpreter running the test.
    """
    tree = _tiny_tree(tmp_path)
    monkeypatch.setattr(mutate, "ROOT", tree)
    monkeypatch.setattr(mutate, "_run_tests", lambda *args: ("survived", "306 passed in 62.34s"))
    report = mutate.run_mutation_sweep(
        files=["src/mod.py"],
        tests_for={"src/mod.py": ["tests/test_mod.py"]},
        fallback_tests=["tests"],
        max_mutants=1,
        seed=0,
        timeout=5,
        include_strings=False,
        budget_seconds=60,
        log=None,
    )
    version = report["control"]["python"]
    assert version == sys.version.split()[0], f"the report names a different interpreter: {version!r}"
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), version


def _report_with_control(**control: object) -> dict:
    """A report shaped like the harness's own, so `write_markdown` can be asked about one line of it."""
    return {
        "files": ["src/mod.py"],
        "tests": ["tests/test_mod.py"],
        "seed": 0,
        "candidates": 0,
        "sampled": 0,
        "run": 0,
        "killed": 0,
        "killed_by_import": 0,
        "survived": 0,
        "timed_out": 0,
        "score": 0.0,
        "score_denominator": 0,
        "include_strings": False,
        "only_lines": [],
        "dropped": [],
        "survivors": [],
        "control": dict(control),
    }


def test_a_control_with_no_passes_is_written_as_zero_and_not_as_a_guess(tmp_path):
    """`0 passed` is a fact about a run that only failed or skipped, and the markdown has to say it.

    pytest's counts line omits every outcome whose count is zero, so a control that failed and
    skipped carries no `passed` key at all. The default in the f-string is what turns that absence
    into `0`; the mutant of that default writes `1 passed` beside a score taken by a run where
    nothing passed — the same shape of error as the skip this whole block exists to make visible.
    """
    report = _report_with_control(
        tests=["tests/test_mod.py"],
        counts={"failed": 1, "skipped": 2},
        summary_line="1 failed, 2 skipped in 3.00s",
        python="3.13.16",
        executable="/usr/bin/python3",
        duration_seconds=3.0,
        clean=False,
    )
    markdown = tmp_path / "report.md"
    mutate.write_markdown(report, markdown)
    text = markdown.read_text(encoding="utf-8")
    assert "- control: 0 passed, 1 failed, 2 skipped" in text
    assert "2 test(s) skipped in the control run" in text


def test_the_warning_beside_the_score_appears_only_when_the_control_skipped(monkeypatch, tmp_path, capsys):
    """The warning is the sentence that stops a reader quoting a conditional score, so both polarities.

    A warning that prints unconditionally is a warning nobody reads, and a check whose default can
    only fire downwards is a caveat that does not exist: `main` reads the *absence* of the key as
    zero skips, and the mutant of that default prints the warning for a control that skipped nothing.
    """
    tree = _tiny_tree(tmp_path)
    monkeypatch.setattr(mutate, "ROOT", tree)
    common = [
        "--file",
        "src/mod.py",
        "--tests",
        "tests/test_mod.py",
        "--max-mutants",
        "1",
        "--budget-seconds",
        "60",
        "--min-score",
        "0",
        "--json",
        str(tmp_path / "report.json"),
    ]
    for detail, expected in (("1 passed, 2 skipped in 3.00s", True), ("1 passed in 3.00s", False)):
        monkeypatch.setattr(mutate, "_run_tests", lambda *args, _detail=detail: ("survived", _detail))
        assert mutate.main(common) == 0
        printed = capsys.readouterr().out
        # The *prefix*, not the whole sentence: an assertion on "skipped 2" would pass under a mutant
        # that printed the warning with a different number, which is the shape of the defect the
        # default in that line can produce. The count is checked in the case where it should appear.
        assert ("warning: the control run skipped" in printed) is expected, printed
        if expected:
            assert "skipped 2 test(s)" in printed, printed


def test_pytest_counts_reads_the_summary_line_pytest_actually_writes():
    """The control's counts are parsed from pytest's own last line, so the parser is pinned to it.

    pytest writes `306 passed, 7 skipped in 62.34s` and omits any outcome whose count is zero, so a
    parser that answered a slightly different question would put a number in the report that no run
    produced. The last case is the one that matters most: `no tests ran` must not read as a passing
    control, and a word that is not an outcome (`warnings`) must not be counted as one.
    """
    assert mutate.pytest_counts("306 passed in 62.34s") == {"passed": 306}
    assert mutate.pytest_counts("1 failed, 2 passed in 0.31s") == {"failed": 1, "passed": 2}
    assert mutate.pytest_counts("12 passed, 3 skipped, 2 warnings in 4.10s") == {"passed": 12, "skipped": 3}
    assert mutate.pytest_counts("17 deselected, 4 xfailed in 0.20s") == {"deselected": 17, "xfailed": 4}
    assert mutate.pytest_counts("no tests ran in 0.01s") == {}


def test_the_score_carries_what_the_control_run_passed_and_skipped(monkeypatch, tmp_path):
    """A subset that skips is not a subset that ran, and the report has to say which one it was.

    The control run proves the sandbox passes the subset before anything is mutated, but *passing*
    and *running* are different facts: on 2026-10-16 the verify sweep ran under an interpreter
    without the optional `cryptography` extra, the signature tests skipped, and the five mutants in
    `verify.py`'s signature block survived a sweep that had nothing left to catch them — 171/177
    where the same tree scores 176/177 with the extra installed. The harness cannot tell a
    deliberate `skipif` from a missing dependency. It can put the control's counts in the report,
    mark the control unclean, and say so in the log and in the markdown.
    """
    tree = _tiny_tree(tmp_path)
    monkeypatch.setattr(mutate, "ROOT", tree)
    monkeypatch.setattr(mutate, "_run_tests", lambda *args: ("survived", "306 passed, 7 skipped in 62.34s"))
    messages: list[str] = []
    report = mutate.run_mutation_sweep(
        files=["src/mod.py"],
        tests_for={"src/mod.py": ["tests/test_mod.py"]},
        fallback_tests=["tests"],
        max_mutants=1,
        seed=0,
        timeout=5,
        include_strings=False,
        budget_seconds=60,
        log=messages.append,
    )
    control = report["control"]
    assert control["tests"] == ["tests/test_mod.py"], "the control ran the subset the mutants use"
    assert control["counts"] == {"passed": 306, "skipped": 7}
    assert control["clean"] is False, "a control that skipped is not a clean control"
    assert control["summary_line"] == "306 passed, 7 skipped in 62.34s"
    assert any("control: 306 passed, 0 failed, 7 skipped in" in message for message in messages), messages

    markdown = tmp_path / "report.md"
    mutate.write_markdown(report, markdown)
    text = markdown.read_text(encoding="utf-8")
    assert "- control: 306 passed, 0 failed, 7 skipped" in text
    assert "7 test(s) skipped in the control run" in text


def test_a_control_that_ran_the_whole_subset_is_recorded_as_clean(monkeypatch, tmp_path):
    """The other polarity: nothing skipped means no warning, in the report and in the file.

    Without this the warning could be unconditional and nothing would notice, which is how a
    caveat stops being read.
    """
    tree = _tiny_tree(tmp_path)
    monkeypatch.setattr(mutate, "ROOT", tree)
    monkeypatch.setattr(mutate, "_run_tests", lambda *args: ("survived", "306 passed in 62.34s"))
    messages: list[str] = []
    report = mutate.run_mutation_sweep(
        files=["src/mod.py"],
        tests_for={"src/mod.py": ["tests/test_mod.py"]},
        fallback_tests=["tests"],
        max_mutants=1,
        seed=0,
        timeout=5,
        include_strings=False,
        budget_seconds=60,
        log=messages.append,
    )
    assert report["control"]["counts"] == {"passed": 306}
    assert report["control"]["clean"] is True
    assert any("control: 306 passed, 0 failed, 0 skipped in" in message for message in messages), messages
    markdown = tmp_path / "clean.md"
    mutate.write_markdown(report, markdown)
    text = markdown.read_text(encoding="utf-8")
    assert "- control: 306 passed, 0 failed, 0 skipped" in text
    assert "skipped in the control run" not in text


def test_a_site_whose_replacement_will_not_compile_is_counted_not_lost(monkeypatch):
    """The branch that hid the byte-offset bug: a drop used to be a silent `continue`.

    Every such site was invisible — not a mutant, not a survivor, not a line in any report — which is
    how a harness can report a clean score over a file it only partly mutated. The record now says
    which line produced nothing and why, and this test drives the branch with a replacement that
    cannot compile rather than waiting for one to appear in the corpus.
    """
    monkeypatch.setattr(mutate, "_mutate", lambda kind, node, source: ("def (", "x", "def ("))
    drops: list[dict[str, object]] = []
    assert mutate.mutants_for_source(FIXTURE, "fixture.py", drops=drops) == []
    assert drops, "a site that produced no mutant has to be recorded"
    assert {drop["reason"] for drop in drops} == {"the replacement does not compile"}
    assert {drop["file"] for drop in drops} == {"fixture.py"}
    assert all(isinstance(drop["line"], int) for drop in drops)


def test_a_site_whose_replacement_is_the_original_text_is_counted_too(monkeypatch):
    """The other silent branch: a "mutant" identical to the original measures nothing.

    A no-op mutant would be scored as *survived* — a test that cannot fail, counted against the
    suite — so the harness drops it by construction, and the drop is now visible for the same
    reason as the one above.
    """
    source = "def value() -> int:\n    return 0\n"
    # A replacement that *is* the node's own text: the splice is a no-op, and the harness must refuse
    # to call it a mutant rather than scoring a test that cannot fail as "survived".
    monkeypatch.setattr(mutate, "_mutate", lambda kind, node, src: (ast.get_source_segment(src, node) or "", "x", "x"))
    drops: list[dict[str, object]] = []
    assert mutate.mutants_for_source(source, "fixture.py", drops=drops) == []
    assert {drop["reason"] for drop in drops} == {"the replacement is the original text"}


def test_a_dropped_site_travels_into_the_report_and_is_never_a_survivor(monkeypatch, tmp_path):
    """A report with no candidates has to say why, or it reads as a file with nothing to say.

    `0 candidates, 0 killed, 0 survived, score 0.0%` is indistinguishable from a clean sweep of a
    file that happens to hold no mutable site. The drop records and the count go into the JSON report
    and into the markdown table's header, so the difference is visible where the numbers are read.
    """
    monkeypatch.setattr(mutate, "_mutate", lambda kind, node, source: ("def (", "x", "def ("))
    report = mutate.run_mutation_sweep(
        files=["src/docket/latency.py"],
        tests_for={"src/docket/latency.py": ["tests/test_latency.py"]},
        fallback_tests=["tests"],
        max_mutants=4,
        seed=0,
        timeout=5,
        include_strings=False,
        budget_seconds=600,
        log=lambda _message: None,
    )
    assert report["candidates"] == 0 and report["run"] == 0
    assert report["dropped"] and report["survivors"] == []
    assert all(drop["reason"] == "the replacement does not compile" for drop in report["dropped"])
    markdown = tmp_path / "report.md"
    mutate.write_markdown(report, markdown)
    text = markdown.read_text(encoding="utf-8")
    assert f"sites that produced no mutant: {len(report['dropped'])}" in text
    assert report["dropped"][0]["file"] in text


def test_only_line_runs_every_mutant_on_that_line_and_samples_none(monkeypatch):
    """`--only-line` is how a survivor is re-checked in seconds instead of a ten-minute sweep.

    It exists because the honest next step after reading a survivor list is to reproduce one
    verdict with the harness's own machinery, and doing that by hand is how a reader ends up
    measuring a stale `__pycache__` instead of the mutant. Every mutant on the line runs — the
    `--max-mutants` sample is bypassed — and the report names the selection so a filtered run is
    never read as a whole-file score.
    """
    source = (ROOT / "src" / "docket" / "latency.py").read_text(encoding="utf-8")
    candidates = mutate.mutants_for_source(source, "src/docket/latency.py")
    line = sorted({mutant.line for mutant in candidates})[1]
    on_line = [mutant for mutant in candidates if mutant.line == line]
    assert len(on_line) >= 1

    seen: list[int] = []

    def fake_run_tests(sandbox, tests, timeout):
        seen.append(1)
        return ("survived" if len(seen) == 1 else "killed"), "stand-in"

    monkeypatch.setattr(mutate, "_run_tests", fake_run_tests)
    report = mutate.run_mutation_sweep(
        files=["src/docket/latency.py"],
        tests_for={"src/docket/latency.py": ["tests/test_latency.py"]},
        fallback_tests=["tests"],
        max_mutants=1,
        seed=0,
        timeout=5,
        include_strings=False,
        budget_seconds=600,
        only_lines=[line],
        log=lambda _message: None,
    )
    assert report["only_lines"] == [line]
    assert report["run"] == len(on_line), "the sample was bypassed: every mutant on the line ran"
    assert {item["line"] for item in report["mutants"]} == {line}


def test_only_line_on_a_line_with_no_mutant_refuses_rather_than_reporting_a_perfect_score():
    """The failure mode of a filter is a score of 100% over nothing.

    Asking for a line that holds no mutant must stop the run and say so, because the alternative —
    "0 run, 0 survived, score 0.0%" — reads as a clean file, and refusing to report is the harness's
    established behaviour when its own preconditions fail.
    """
    with pytest.raises(RuntimeError, match="no mutant sits on line"):
        mutate.run_mutation_sweep(
            files=["src/docket/latency.py"],
            tests_for={"src/docket/latency.py": ["tests/test_latency.py"]},
            fallback_tests=["tests"],
            max_mutants=1,
            seed=0,
            timeout=5,
            include_strings=False,
            budget_seconds=600,
            only_lines=[1],
            log=lambda _message: None,
        )


def test_the_list_command_prints_the_mutants_without_running_anything():
    """`--list` is the cheap way to answer "what is on this line", and it must not run the suite.

    It is also used by this test file's neighbours to generate the expectations, so it is checked
    end to end through `main()` rather than through `mutants_for_source`: the exit code, the count
    line, and the selection.
    """
    completed = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "mutate.py"), "--file", "src/docket/latency.py", "--list"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr[-800:]
    listed = [line for line in completed.stdout.splitlines() if line.startswith("  src/docket/latency.py:")]
    assert len(listed) >= 10, completed.stdout[-800:]
    assert all("[" in line and "]" in line for line in listed)
    assert "candidate mutant(s)" in completed.stdout

    # `--only-line` narrows the listing to the mutants on one line — and the line is taken from the
    # listing above rather than hard-coded, because a hand-written line number is a test that stops
    # meaning anything the moment the module is edited (and `0 candidate mutant(s)` would pass a
    # careless assertion while testing nothing).
    lines = [int(match) for match in re.findall(r"latency\.py:(\d+) \[", completed.stdout)]
    assert len(lines) >= 10, completed.stdout[-500:]
    chosen = lines[1]
    on_line = lines.count(chosen)
    selected = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "mutate.py"),
            "--file",
            "src/docket/latency.py",
            "--list",
            "--only-line",
            str(chosen),
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert selected.returncode == 0, selected.stderr[-800:]
    assert f"{on_line} candidate mutant(s)" in selected.stdout, selected.stdout[-500:]
    assert len(selected.stdout.splitlines()) < len(completed.stdout.splitlines())


def test_the_latency_fixture_the_other_tests_use_is_the_real_module():
    """The stand-in tests mutate `src/docket/latency.py`; this asserts it is still a real file.

    A path that quietly stops existing would make `only_lines=[1]` raise for the *wrong* reason
    (a missing file rather than a line with no mutant) and the timeout test would be scoring a
    fixture that is not there.
    """
    source = (ROOT / "src" / "docket" / "latency.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert any(isinstance(node, ast.FunctionDef) for node in ast.walk(tree))
    assert mutate.mutants_for_source(source, "src/docket/latency.py"), "the real module yields no mutant"


def test_a_mutant_record_is_frozen():
    """`@dataclass(frozen=True)` on `Mutant`: a record a sweep edits in place is one the report cannot trust.

    The report is built from these objects and the file is restored between mutants, so a field that
    can be reassigned is a field that can be reassigned between the splice and the verdict — the
    record would then describe a mutant that is not the one that ran.
    """
    mutant = mutate.mutants_for_source("value = 0\n", "fixture.py")[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        mutant.line = 5  # type: ignore[misc]


def test_the_docstring_rule_covers_module_and_body_docstrings_and_nothing_else():
    """Five mutants decide whether a string is a site; each turns a docstring into a behaviour change.

    Module-level and body docstrings are excluded, a bare string expression that is *not* the first
    statement is included, and strings themselves are opt-in: dropping the `not` in either check, or
    flipping either `return False`/`return True`, or slipping `body[0] is parent` by one index, makes
    the harness ask the suite to notice a reworded docstring — and the answer would be "no", which
    every score computed afterwards would have counted as a survivor.
    """
    source = (
        '"""module docstring"""\n'
        "\n"
        "\n"
        "def f() -> str:\n"
        '    """body docstring"""\n'
        '    "a bare expression, not the first statement"\n'
        '    return "a value"\n'
    )
    string_lines = {node.lineno for kind, node in mutate.sites(source, include_strings=True) if kind == "string"}
    assert string_lines == {6, 7}, "the two docstrings are not sites; the other two strings are"
    assert {kind for kind, _ in mutate.sites(source)} == {"return-none"}, "string sites are opt-in"

    non_string = 'def f():\n    """doc"""\n    return 1\n'
    only_docstring = [kind for kind, node in mutate.sites(non_string, include_strings=True) if kind == "string"]
    assert only_docstring == [], "the first statement of a body is a docstring wherever it sits"


def test_the_docstring_rule_answers_with_exactly_true_or_false():
    """Six mutants in a five-line helper, none of which a sweep can reach on its own.

    `_is_docstring` has one caller, in `--include-strings`, and that caller has already established
    that the node is a non-empty string constant — so inside a sweep every guard here is decided
    before it is asked, and the mutants that change those guards are invisible to the tests above.
    Asking the function directly is the only way to pin its answers, and the answers matter in three
    ways: a constant that is *not* a string must not be treated as a docstring (`ast.Expr` wraps
    plain numbers too, and `and` turned into `or` says yes to all of them, filtering out whole files'
    worth of mutants), a "no" must be exactly `False` rather than any falsy value, and a wrapper with
    no recorded parent gets the conservative answer — the string is skipped, because rewriting a
    string nobody executes cannot change behaviour.
    """
    module = ast.parse('"""module docstring"""\n')
    assert mutate._is_docstring(module.body[0].value, mutate._parents_of(module)) is True

    orphan = ast.Constant(value="a string whose wrapper has no parent on record")
    assert mutate._is_docstring(orphan, {orphan: ast.Expr(value=orphan)}) is True

    statement = ast.parse("pass\n")
    assert mutate._is_docstring(statement.body[0], mutate._parents_of(statement)) is False

    number = ast.parse("5\n")
    assert (
        mutate._is_docstring(number.body[0].value, mutate._parents_of(number)) is False
    ), "a bare number is wrapped in the same `ast.Expr` a docstring is"

    assigned = ast.parse('value = "a string, but not a docstring"\n')
    assert mutate._is_docstring(assigned.body[0].value, mutate._parents_of(assigned)) is False


def test_an_integer_site_covers_both_literals_and_swaps_them():
    """`0` and `1` are both sites and each becomes the other.

    Three mutants are pinned here: dropping `1` from the pair the harness recognises (an integer of
    one is then not a site at all), and the two directions of the swap — each of which, alone, makes
    the replacement identical to the original text, so the site produces no mutant and the drop is
    what a reader sees instead of a number.
    """
    assert [
        (mutant.operator, mutant.before, mutant.after)
        for mutant in mutate.mutants_for_source("value = 0\n", "fixture.py")
    ] == [("int", "0", "1")]
    assert [
        (mutant.operator, mutant.before, mutant.after)
        for mutant in mutate.mutants_for_source("value = 1\n", "fixture.py")
    ] == [("int", "1", "0")]


def test_the_site_limit_is_a_ceiling_on_what_is_returned():
    """`>=` versus `>` in the limit check is one extra mutant at the boundary — the kind that is noticed last."""
    source = "def f(value):\n    if value > 0 and value > 1:\n        return 2\n    return 3\n"
    assert len(mutate.sites(source)) >= 5
    assert len(mutate.mutants_for_source(source, "fixture.py", limit=2)) == 2
    assert len(mutate.mutants_for_source(source, "fixture.py", limit=1)) == 1


def test_a_site_at_the_first_byte_of_a_file_is_inside_the_file():
    """The span guard's lower bound is byte zero, not byte one.

    `0 <= start < end <= len(source)` is the harness's own check that a splice lands inside the file.
    A site it refuses is *dropped*, and a drop is reported as a site that produced no mutant — never
    as a survivor — so a bound that is off by one hides a real site while every test still passes.
    Only a file whose first node is mutable can tell the two bounds apart: a module that begins with
    a bare expression statement (legal Python, and what a generated or scratch module looks like) has
    `start == 0`, and with `1 <= start` its first mutant disappears from the run.
    """
    source = "left and right\nvalue = 1\n"
    first = next(node for kind, node in mutate.sites(source) if kind == "boolop")
    assert mutate._span(source, first) == (0, 14), "the fixture has to start at byte zero to test this"

    drops: list[dict[str, object]] = []
    mutants = mutate.mutants_for_source(source, "scratch.py", drops=drops)
    assert {(mutant.line, mutant.operator) for mutant in mutants} == {(1, "boolop"), (2, "int")}
    assert drops == [], "the first byte of the file is inside the file"


def test_the_detail_the_harness_records_is_the_last_line_of_pytests_own_output(sandbox):
    """The recorded detail is pytest's *last* line, as text, or the placeholder if there was none.

    Four mutants live in these four lines: `capture_output` and `text` (the detail arrives as bytes,
    or not at all), `lines[-1]` (the first line of a failure is a progress character, not a count),
    and the two `summary or "..."` fallbacks (a suppressed summary must not be replaced by a
    sentence that reads like an answer). The placeholder is asserted as absent, not the text as
    exact: the count of failures is pytest's wording to change, the *shape* is the harness's.
    """
    _write_case(sandbox, "def test_ok():\n    assert True\n")
    outcome, detail = mutate._run_tests(sandbox, ["tests/test_case.py"], 60)
    assert outcome == "survived"
    assert isinstance(detail, str), "the detail is text, not bytes"
    assert detail != "the suite passed", "the run printed something, and that is what is recorded"

    _write_case(sandbox, "def test_no():\n    assert False, 'caught'\n")
    outcome, detail = mutate._run_tests(sandbox, ["tests/test_case.py"], 60)
    assert outcome == "killed"
    assert isinstance(detail, str) and "failed" in detail, f"the last line of a failure: {detail!r}"

    _write_case(sandbox, "import a_module_that_is_not_installed_anywhere\n")
    outcome, detail = mutate._run_tests(sandbox, ["tests/test_case.py"], 60)
    assert outcome == "killed-import"
    assert isinstance(detail, str) and "error" in detail.lower()
    assert detail != "the copy could not even be collected"


def test_the_import_kill_count_is_a_filter_and_not_its_opposite(monkeypatch):
    """`killed_by_import` counts the mutants the *collector* refused, not the ones an assertion caught.

    The stand-in returns two plain kills and one import kill; the report must say one, and a mutant
    that counts `!= "killed-import"` says two — with the same total, so only a reader who knows the
    difference between "the assertions fired" and "the copy would not import" would notice.
    """
    scripted = ["survived", "killed", "killed", "killed-import"]
    calls: list[str] = []

    def stand_in(*args):
        outcome = "survived" if not calls else scripted[len(calls) % len(scripted)]
        calls.append(outcome)
        return outcome, f"stand-in: {outcome}"

    monkeypatch.setattr(mutate, "_run_tests", stand_in)
    report = mutate.run_mutation_sweep(
        files=["src/docket/latency.py"],
        tests_for={"src/docket/latency.py": ["tests/test_latency.py"]},
        fallback_tests=["tests"],
        max_mutants=3,
        seed=0,
        timeout=5,
        include_strings=False,
        budget_seconds=60,
        log=lambda _message: None,
    )
    assert (report["killed"], report["killed_by_import"]) == (3, 1), report["killed_by_import"]


def test_a_sweep_runs_without_a_log_callable_and_draws_a_sample_at_the_boundary(monkeypatch):
    """Two sites in the sweep's own bookkeeping, reachable without running a single test.

    The first is `log or (lambda ...)`: without it a quiet sweep is a `TypeError`. The second is the
    sampling branch — `sorted(order[:max_mutants]) if max_mutants < len(planned) else order` — which
    has to stay a *draw* when `--max-mutants` is exactly the number of sites. With `<=` instead of `<`
    the run silently falls back to `sorted(...)`, and because `planned` is in `ast.walk` order (which
    is breadth-first, and not the file's line order) the giveaway is not "the lines are ascending":
    it is that two different seeds produce the *same* run order, which is what this asserts.
    """
    monkeypatch.setattr(mutate, "_run_tests", lambda *args: ("survived", "stand-in: survived"))
    source = (ROOT / "src" / "docket" / "latency.py").read_text(encoding="utf-8")
    sites = len(mutate.mutants_for_source(source, "src/docket/latency.py"))
    orders = []
    for seed in (7, 8):
        report = mutate.run_mutation_sweep(
            files=["src/docket/latency.py"],
            tests_for={"src/docket/latency.py": ["tests/test_latency.py"]},
            fallback_tests=["tests"],
            max_mutants=sites,
            seed=seed,
            timeout=5,
            include_strings=False,
            budget_seconds=60,
            log=None,
        )
        orders.append([(item["line"], item["operator"], item["from"], item["to"]) for item in report["mutants"]])
        assert len(orders[-1]) == report["candidates"] == sites >= 20, "the limit covers every site"
    assert orders[0] != orders[1], "at the boundary the run order is a draw, not `sorted(...)`"


def test_the_sandbox_is_removed_when_it_is_not_asked_for(monkeypatch):
    """`if not keep:` is the difference between a clean machine and a growing pile of copies.

    The sweep copies the repository into `/tmp` for every run. Left behind, that is a multi-megabyte
    directory per sweep and a disk that fills quietly — the kind of defect nothing notices until a
    run fails for a reason that has nothing to do with the code under test.
    """
    monkeypatch.setattr(mutate, "_run_tests", lambda *args: ("survived", "stand-in: survived"))
    root = Path(tempfile.gettempdir())
    before = set(root.glob("docket-mutate-*"))
    mutate.run_mutation_sweep(
        files=["src/docket/latency.py"],
        tests_for={"src/docket/latency.py": ["tests/test_latency.py"]},
        fallback_tests=["tests"],
        max_mutants=1,
        seed=0,
        timeout=5,
        include_strings=False,
        budget_seconds=60,
        log=None,
    )
    assert set(root.glob("docket-mutate-*")) == before, "the sandbox copy was left behind"


def _sandboxes() -> set[Path]:
    """Every sweep sandbox currently in the system temporary directory."""
    return set(Path(tempfile.gettempdir()).glob("docket-mutate-*"))


def _give_back_the_write_bit(root: Path) -> None:
    """Restore the write bit on every directory under `root` so the tree can be removed.

    Deepest first: a directory cannot be removed while a directory inside it is still locked.
    """
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_dir():
            path.chmod(0o700)


@pytest.fixture
def undeletable(tmp_path):
    """A fixture tree the sweep cannot delete: a directory with the write bit removed, holding a file.

    Every sweep copies the tree it is run against into a sandbox under the system temporary
    directory, and removing a directory that contains an unwritable one fails with `PermissionError`
    — that is what `ignore_errors=True` is for. Nothing else in this suite removes a directory it
    cannot write to, so these four tests are the only place that argument is decided. The shape is
    not exotic: a test under mutation is free to create a read-only directory, and then the cleanup
    runs against a tree it cannot delete.

    The four tests ask one question each: a cleanup that fails may not replace the report, may not
    replace a broken baseline's refusal, may not replace a refused splice, and may not replace a
    refused line filter. `run_mutation_sweep` is documented to fail loudly when its own preconditions
    are not met, and a `PermissionError` from `finally:` would be the tool breaking that promise in
    the one place a reader is least likely to look.

    Root ignores the write bit, so the tests skip there — the sandbox is removable and there is
    nothing to observe.
    """
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root can delete a directory it cannot write to, so there is no failure to test")
    tree = _tiny_tree(tmp_path)
    locked = tree / "src" / "read-only"
    locked.mkdir()
    (locked / "kept.txt").write_text("a file inside a directory nobody may write to\n", encoding="utf-8")
    locked.chmod(0o500)
    before = _sandboxes()
    yield tree
    _give_back_the_write_bit(tree)
    for leftover in _sandboxes() - before:
        _give_back_the_write_bit(leftover)
        shutil.rmtree(leftover, ignore_errors=True)


SWEEP = {
    "files": ["src/mod.py"],
    "tests_for": {"src/mod.py": ["tests/test_mod.py"]},
    "fallback_tests": ["tests"],
    "max_mutants": 50,
    "seed": 0,
    "timeout": 5,
    "include_strings": False,
    "budget_seconds": 60,
}


def test_a_sandbox_that_cannot_be_deleted_does_not_replace_the_report(monkeypatch, undeletable):
    """The cleanup lives in `finally:`, so a cleanup that raises replaces whatever the run was doing.

    Here the run is fine and the *report* is the result: five mutants, none of them caught, a score
    of zero. Swallowing the `PermissionError` is what keeps that result. Raising it out of the
    `finally:` block would turn a measured zero into a traceback about a temporary directory.
    """
    monkeypatch.setattr(mutate, "ROOT", undeletable)
    monkeypatch.setattr(mutate, "_run_tests", lambda *args: ("survived", "stand-in: survived"))
    report = mutate.run_mutation_sweep(**SWEEP, log=None)
    assert report["run"] > 0 and report["score"] == 0.0 and report["killed"] == 0


def test_a_sandbox_that_cannot_be_deleted_does_not_replace_a_broken_baseline(monkeypatch, undeletable):
    """A red baseline is refused by name, and a cleanup failure must not change *which* error surfaces."""
    monkeypatch.setattr(mutate, "ROOT", undeletable)
    monkeypatch.setattr(mutate, "_run_tests", lambda *args: ("killed", "the copy was already red"))
    with pytest.raises(RuntimeError, match="does not pass the suite before any mutation"):
        mutate.run_mutation_sweep(**SWEEP, log=None)


def test_a_sandbox_that_cannot_be_deleted_does_not_replace_a_refused_splice(monkeypatch, undeletable):
    """The harness falsifies itself first: if a splice with no mutant in it edits the file, it stops.

    Nothing else here exercises that refusal, and it is the check that makes every score in
    `docs/testing.md` mean something — so it has to be the error a reader sees, not a `PermissionError`
    from the sandbox it was about to delete.
    """
    monkeypatch.setattr(mutate, "ROOT", undeletable)
    monkeypatch.setattr(mutate, "splice_only", lambda source: source + "# the harness edited this\n")
    with pytest.raises(AssertionError, match="without mutating it"):
        mutate.run_mutation_sweep(**SWEEP, log=None)


def test_a_sandbox_that_cannot_be_deleted_does_not_replace_a_refused_filter(monkeypatch, undeletable):
    """`--only-line` on a line with no mutant stops the run; the refusal is the answer, not the cleanup."""
    monkeypatch.setattr(mutate, "ROOT", undeletable)
    monkeypatch.setattr(mutate, "_run_tests", lambda *args: ("survived", "stand-in: survived"))
    with pytest.raises(RuntimeError, match="no mutant sits on line"):
        mutate.run_mutation_sweep(**SWEEP, only_lines=[1], log=None)


def test_a_budget_of_exactly_the_elapsed_time_still_runs_the_next_mutant(monkeypatch):
    """`> budget_seconds` rather than `>=`: a run that has used exactly its budget has not exceeded it.

    With real clocks the two are indistinguishable — the elapsed time is never exactly the budget —
    so the clock is pinned: the sweep starts at 1000.0 and the check sees 1000.0 again. The mutant
    stops before the first mutant; the harness runs it.
    """
    monkeypatch.setattr(mutate, "_run_tests", lambda *args: ("survived", "stand-in: survived"))
    # One instant, answered as often as the harness asks: the sweep may read the clock for the
    # control run, for the budget check and for its own duration, and a pinned *count* of ticks
    # would fail on the next honest clock read — a fact about call sites, not about `>` vs `>=`.
    monkeypatch.setattr(mutate.time, "monotonic", lambda: 1000.0)
    report = mutate.run_mutation_sweep(
        files=["src/docket/latency.py"],
        tests_for={"src/docket/latency.py": ["tests/test_latency.py"]},
        fallback_tests=["tests"],
        max_mutants=2,
        seed=0,
        timeout=5,
        include_strings=False,
        budget_seconds=0.0,
        log=None,
    )
    assert report["run"] == 2, "exactly zero elapsed against a zero budget is not over budget"


def test_the_budget_message_counts_the_mutants_that_ran(monkeypatch):
    """`enumerate(..., start=1)` and `{number - 1}`: the one place a sweep explains why it stopped early.

    A budget that is already spent stops the run before the first mutant, so the message must say
    *after 0 mutant(s)* — a count that starts at zero or a subtraction that is skipped reports one
    more mutant than the report will contain, and the report is what a reader trusts.
    """
    monkeypatch.setattr(mutate, "_run_tests", lambda *args: ("survived", "stand-in: survived"))
    messages: list[str] = []
    report = mutate.run_mutation_sweep(
        files=["src/docket/latency.py"],
        tests_for={"src/docket/latency.py": ["tests/test_latency.py"]},
        fallback_tests=["tests"],
        max_mutants=5,
        seed=0,
        timeout=5,
        include_strings=False,
        budget_seconds=0.0,
        log=messages.append,
    )
    assert report["run"] == 0, "a budget of zero seconds stops before the first mutant"
    assert any("reached after 0 mutant(s)" in message for message in messages), messages


def _tiny_tree(tmp_path: Path) -> Path:
    """A complete tree the CLI can sweep without touching the real one: one module, one test.

    Three sites (`>`, `1` and `0`) and a test that catches one of them, which is all `main` needs to
    reach its own edges — the two report files, the exit codes, and the sentences in the markdown.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "mod.py").write_text(
        "def limit(value: int) -> int:\n    if value > 3:\n        return 1\n    return 0\n", encoding="utf-8"
    )
    (tmp_path / "tests" / "test_mod.py").write_text(
        "from mod import limit\n\n\ndef test_limit() -> None:\n    assert limit(4) == 1\n", encoding="utf-8"
    )
    # The repository's own `addopts`, because a harness run inherits it and one flag in it decides
    # whether pytest prints a counts line at all: the copy carries this file, so a fixture without
    # it would test a pytest invocation the harness never makes.
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = "-q --strict-markers"\n', encoding="utf-8"
    )
    return tmp_path


def test_the_cli_writes_both_reports_and_the_min_score_decides_the_exit_code(monkeypatch, tmp_path):
    """End to end through `main` on a three-line tree, with the verdicts supplied by the stand-in.

    Seven sites in `main` are only reachable this way: the seed it records, `only_lines or 'all'` and
    the dropped count in the markdown, `parents=True` and `exist_ok=True` on both report paths, the
    *inclusive* comparison against `--min-score`, and the two return codes. The first call asks for a
    score of exactly 1.0, which the stand-in produces: `>=` passes it and `>` refuses it, so the
    assertion is about the inclusive comparison rather than about the number.
    """
    tree = _tiny_tree(tmp_path)
    monkeypatch.setattr(mutate, "ROOT", tree)
    calls: list[int] = []

    def stand_in(*args):
        calls.append(1)
        return ("survived" if len(calls) == 1 else "killed"), "stand-in"

    monkeypatch.setattr(mutate, "_run_tests", stand_in)

    def sweep(*args: str) -> int:
        """Run `main` with the counter reset: the *first* call of each sweep is its control run."""
        calls.clear()
        return mutate.main(list(args))

    first_json = tmp_path / "one" / "nested" / "report.json"
    first_md = tmp_path / "one" / "nested" / "report.md"
    common = ["--file", "src/mod.py", "--tests", "tests/test_mod.py", "--seed", "0", "--timeout", "60"]
    code = sweep(
        *common,
        "--max-mutants",
        "50",
        "--budget-seconds",
        "60",
        "--min-score",
        "1.0",
        "--json",
        str(first_json),
        "--markdown",
        str(first_md),
    )
    assert code == 0, "1.0 is a score the run matched, and the comparison is inclusive"
    report = json.loads(first_json.read_text(encoding="utf-8"))
    assert (report["seed"], report["score"], report["dropped"]) == (0, 1.0, [])
    assert report["survivors"] == [] and report["killed"] == report["run"] > 0
    text = first_md.read_text(encoding="utf-8")
    assert "- selected lines: all" in text
    assert "- sites that produced no mutant: 0" in text
    assert "None: every sampled mutant was caught by the suite." in text

    second_md = tmp_path / "two" / "deeper" / "report.md"
    refused = sweep(
        *common, "--max-mutants", "50", "--budget-seconds", "60", "--min-score", "2.0", "--markdown", str(second_md)
    )
    assert refused == 1, "a bar no run can clear is a refusal, not a crash and not a pass"
    assert second_md.is_file(), "the report is written before the exit code is decided"

    assert sweep("--file", "no/such/file.py") == 2, "a missing file is a usage error"

    # The seed has a default, and the run records it: `--seed` is not required, and the number in
    # the report is the one the sample was drawn with.
    unseeded = tmp_path / "three" / "deeper" / "report.json"
    assert (
        sweep(
            "--file",
            "src/mod.py",
            "--tests",
            "tests/test_mod.py",
            "--max-mutants",
            "50",
            "--budget-seconds",
            "60",
            "--min-score",
            "0",
            "--json",
            str(unseeded),
        )
        == 0
    )
    assert json.loads(unseeded.read_text(encoding="utf-8"))["seed"] == 0

    # Both report paths are created with their parents, and re-running into the same directory is
    # not an error: the first run made `four/nested`, the second writes into it again. Both paths
    # are re-run, because `exist_ok` is argued twice in `main` and once is not enough.
    again_json = tmp_path / "four" / "nested" / "report.json"
    again_md = tmp_path / "four" / "nested" / "report.md"
    for _ in range(2):
        assert (
            sweep(
                "--file",
                "src/mod.py",
                "--tests",
                "tests/test_mod.py",
                "--max-mutants",
                "50",
                "--budget-seconds",
                "60",
                "--min-score",
                "0",
                "--json",
                str(again_json),
                "--markdown",
                str(again_md),
            )
            == 0
        )
    assert again_json.is_file() and again_md.is_file()

    # `--list` runs nothing and is a success, which is a return code of its own.
    assert sweep("--list", "--file", "src/mod.py") == 0
