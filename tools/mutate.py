#!/usr/bin/env python3
"""Mutation testing: does the suite *notice* when the code is wrong?

Coverage says which lines ran. It does not say whether anything was *asserted* about them, and
this project has a specific reason to care: a verifier that accepts everything passes every
positive test, and a chain check with an inverted comparison passes every "happy path" test. A
mutation score is the closest thing to a measurement of what those tests were written for.

Design decisions worth knowing before reading a number from this tool:

* **Single-site, spliced mutants.** One operator, boolean, small integer, `not`, `max`/`min`,
  string or `return` is changed, and the change is spliced back into the original text at exactly
  that node's span — so the file is byte-identical everywhere else. Comments, formatting and
  everything unmutated survive, which matters because a mutant that also reformats the file can
  be killed for the wrong reason.
* **The original tree is never written to.** Every mutant runs in a copy made outside the working
  directory, and the copy is removed afterwards unless ``--keep`` is passed.
* **A control run first.** Before any mutant is scored, the suite is run in the sandbox
  *unmutated*. If that fails, the score would be meaningless (every mutant would look killed), so
  the tool aborts and says so.
* **A real suite, not a model of one.** Each mutant is killed by running actual tests — the
  per-module map below, or ``--tests`` — with ``pytest -x``, offline, in the copy.
* **The denominator is stated.** Mutants that do not compile or were skipped by the budget are
  reported and never counted as killed; mutants that hang the suite are counted separately from
  mutants the assertions caught; and a mutant killed by an import error is marked as such,
  because "the copy no longer imports" is a weaker signal than "a test disagreed".
* **No claim of totality.** Eight mutators, no statement deletion, no block reordering, and
  string constants only behind ``--include-strings`` (message text is often intentionally
  unasserted). A mutation score is a floor on test strength, not a certificate.

Reading a score, in the order the mistakes actually happen. A survivor is a *question*, not a
finding, and there are three wrong answers before the right one:

1. **The subset was too small.** A survivor in a branch the subset never runs is an artefact of
   the run. Widen ``DEFAULT_TESTS`` for that file and change nothing else: if the score moves,
   it was the subset.
2. **The branch cannot be reached.** A mutant of defensive code that nothing can execute survives
   by construction. The answer is to delete the dead branch, not to write a test that pretends to
   cover it — see ``docs/testing.md``.
3. **The mutant is equivalent.** It changes the text and not the behaviour. Then the *reason* is
   written down beside the other survivors, because an unexplained survivor and a known-equivalent
   survivor look identical in a report.

Only what remains is a gap: a real behaviour change the suite did not notice. The ledger's first
sweep scored 50.0% over a 70-mutant sample and most of its 35 survivors turned out to be gaps of
exactly that kind; the second, after the tests those survivors asked for, scored 83.7% (144 of 172
candidates) with every one of its 28 remaining survivors classified.

Usage::

    python tools/mutate.py                          # canon.py and verify.py, time-boxed
    python tools/mutate.py --file src/docket/ledger.py --tests tests/test_ledger.py,tests/test_vectors.py
    python tools/mutate.py --max-mutants 40 --seed 7 --json /tmp/mutation.json --markdown /tmp/mutation.md
    python tools/mutate.py --list                   # the candidate mutants, without running any

Exit status is 0 when the kill rate meets ``--min-score`` (default 0.85), else 1.
"""

from __future__ import annotations

import argparse
import ast
import json
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Which tests to run for a mutated module, by default. Deliberately a *subset*: the point is to
#: finish, and these are the files that make claims about the module in question. A module with
#: no entry here uses ``--tests``.
DEFAULT_TESTS: dict[str, list[str]] = {
    # The cache is checked from three sides: the module's own rules, the boundary that uses
    # it, and the verifier that recomputes what it claims to have saved. A mutant that only
    # the receipt-level checks would catch must not survive because the subset was too narrow.
    "src/docket/cache.py": [
        "tests/test_cache.py",
        "tests/test_proxy.py",
        "tests/test_receipt_verify.py",
        "tests/test_vectors.py",
    ],
    "src/docket/canon.py": ["tests/test_canon.py", "tests/test_vectors.py"],
    # `test_signing_and_attach.py` is in this list because the sealing and signing checks live in
    # the same module: without it, every mutant in the Ed25519 branch survives and the score
    # understates the suite (a survivor can be an artefact of the subset, not a gap).
    # `test_cache.py` is in it for exactly the same reason, and it was missing for a while: the
    # checks that recompute a *stated* cache saving — `cache-savings`, `cache-hit-origins`,
    # `cache-hit-proofs` — live in this module and are exercised from `tests/test_cache.py`, so
    # without that file 51 of the 177 mutants below survived a whole round and the report read as
    # 51 gaps in the verifier. It is first in the list because those tests are the ones that fail
    # first under the mutants, and `-x` stops at the first failure.
    "src/docket/verify.py": [
        "tests/test_cache.py",
        "tests/test_receipt_verify.py",
        "tests/test_vectors.py",
        "tests/test_signing_and_attach.py",
        # `test_conformance.py` runs the negative corpus through both verifiers, and the corpus is
        # where the *spec's* edge cases live: an integral float that must be read (`34`), a BOM
        # that must not be skipped. Without it, three mutants in `whole()` survived a sweep — the
        # float branch, its negation, and `return int(value)` — and the report read as three gaps
        # in the verifier rather than a file missing from its suite. Same mistake as
        # `test_cache.py` below, caught the same way.
        "tests/test_conformance.py",
    ],
    # The ledger's failure-handling code (torn tails, quarantine, locks) is exercised by
    # `docket doctor`, so `test_diagnostics.py` belongs to this module's suite too — without it
    # every mutant in the quarantine path survives, which says nothing about the tests.
    "src/docket/ledger.py": [
        "tests/test_ledger.py",
        "tests/test_ledger_faults.py",
        "tests/test_ledger_concurrency.py",
        "tests/test_ledger_contracts.py",
        "tests/test_diagnostics.py",
        "tests/test_vectors.py",
        "tests/test_invariants.py",
    ],
    # The latency rule is checked from two sides: the rule itself, and the verifier that recomputes a
    # receipt's summary from the rows it cites. Without the second file, every mutant in
    # `check_summary` survives and the score reads 0.04 — a survivor that is an artefact of the
    # subset, not a gap in the tests.
    "src/docket/latency.py": ["tests/test_latency.py", "tests/test_receipt_verify.py"],
    # The invariants file drives real supervised runs (locking, killing, clock stepping), so it
    # belongs to any module whose promises are about lifecycle or concurrency.
    "src/docket/supervisor.py": ["tests/test_supervisor.py", "tests/test_invariants.py"],
    "src/docket/scriptagent.py": ["tests/test_invariants.py", "tests/test_chaos.py"],
    "src/docket/util.py": ["tests/test_url_guards.py", "tests/test_cli_input.py"],
    # The load driver's whole job is counting correctly and classifying refusals, so its tests are
    # the only thing that can kill a mutant in those counters.
    "src/docket/loaddriver.py": ["tests/test_loadtest.py"],
    # `RateLimiter`'s validation mutants are reachable only from the parametrised refusals in
    # `test_resilience.py`; the breaker and fault-injection code in the same file has its own file.
    "src/docket/resilience.py": ["tests/test_resilience.py", "tests/test_proxy.py", "tests/test_chaos.py"],
    "src/docket/policy.py": ["tests/test_policy.py", "tests/test_cli_input.py"],
    "src/docket/pricing.py": ["tests/test_pricing.py"],
    # The harnesses are mutated too, and with a subset that is not the whole suite: `test_mutate.py`
    # loads `tools/mutate.py` from the sandbox, so a mutant of the mutation harness is run by the
    # tests that describe it. `make check` runs both tools, and a tool whose own numbers are quoted
    # in `docs/testing.md` is as much a part of the evidence as the modules it mutates.
    "tools/mutate.py": ["tests/test_mutate.py"],
    # The site checker is the release record's sixteenth check and the only thing in the repository
    # that reads the published HTML, so its own mutants are worth a subset. Its tests are the only
    # thing that can kill a mutant in it: they are written as pages that break one rule each.
    "tools/a11y_check.py": ["tests/test_a11y_check.py"],
    "tools/helm_fidelity.py": ["tests/test_helm_fidelity.py"],
    "tools/deploy_check.py": ["tests/test_deploy_check.py"],
    "tools/loadtest.py": ["tests/test_loadtest.py"],
    "tools/fuzz.py": ["tests/test_fuzz_regressions.py"],
}

#: The modules a plain run mutates: the ones whose promises are cryptographic.
DEFAULT_FILES = ["src/docket/canon.py", "src/docket/verify.py"]

#: Directories that must not be copied into the sandbox (a mutation run has no business
#: carrying a previous build, a virtualenv or a ledger into the copy).
COPY_IGNORE = shutil.ignore_patterns(
    ".git", "dist", "build", "__pycache__", "*.egg-info", ".venv", ".docket", ".mypy_cache", ".ruff_cache"
)

#: Readable spellings for the operators a mutant can change.
SYMBOLS = {
    "Eq": "==",
    "NotEq": "!=",
    "Lt": "<",
    "LtE": "<=",
    "Gt": ">",
    "GtE": ">=",
    "And": "and",
    "Or": "or",
}


@dataclass(frozen=True)
class Mutant:
    """One single-site change to one file: what changed, where, and the whole mutated file."""

    file: str
    line: int
    operator: str
    before: str
    after: str
    source: str

    def describe(self) -> str:
        return f"{self.file}:{self.line} [{self.operator}] {self.before} -> {self.after}"

    def as_dict(self) -> dict[str, object]:
        return {
            "file": self.file,
            "line": self.line,
            "operator": self.operator,
            "from": self.before,
            "to": self.after,
        }


def _is_docstring(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """True for a string constant that is a docstring (mutating one changes no behaviour)."""
    if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
        return False
    parent = parents.get(node)
    if not isinstance(parent, ast.Expr):
        return False
    container = parents.get(parent)
    if container is None:
        return True  # module-level docstring
    body = getattr(container, "body", None)
    return isinstance(body, list) and bool(body) and body[0] is parent


def _parents_of(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _site_kind(node: ast.AST, parents: dict[ast.AST, ast.AST], *, include_strings: bool) -> str | None:
    """Classify a node as a mutable site, or return None.

    This is the single source of truth for "what can be mutated": both the counting pass and the
    applying pass use it, so the two can never disagree about how many sites exist.
    """
    if (
        isinstance(node, ast.Compare)
        and len(node.ops) == 1
        and type(node.ops[0])
        in {
            ast.Eq,
            ast.NotEq,
            ast.Lt,
            ast.LtE,
            ast.Gt,
            ast.GtE,
        }
    ):
        return "compare"
    if isinstance(node, ast.BoolOp):
        return "boolop"
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return "drop-not"
    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        return "bool"
    if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
        return "int" if node.value in (0, 1) else None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        if include_strings and node.value and not _is_docstring(node, parents):
            return "string"
        return None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"max", "min"}:
        return "minmax"
    if isinstance(node, ast.Return) and node.value is not None:
        if isinstance(node.value, ast.Constant) and node.value.value is None:
            return None
        return "return-none"
    return None


def sites(source: str, *, include_strings: bool = False) -> list[tuple[str, ast.AST]]:
    """Every mutable site, in a deterministic order (``ast.walk`` is breadth-first and stable)."""
    tree = ast.parse(source)
    parents = _parents_of(tree)
    found: list[tuple[str, ast.AST]] = []
    for node in ast.walk(tree):
        kind = _site_kind(node, parents, include_strings=include_strings)
        if kind is not None:
            found.append((kind, node))
    return found


def _char_offset(line: str, byte_offset: int) -> int:
    """Turn one of ``ast``'s byte offsets into a character index into ``line``.

    ``col_offset`` and ``end_col_offset`` are UTF-8 **byte** offsets — the `ast` documentation says
    so, and it is easy to read past: for a line whose text before the token contains an em dash the
    byte offset is larger than the character index, so a splice built from it lands *inside* a
    character. The mutant text then looks plausible, does not compile, and was dropped silently —
    and `splice_only` could not catch it, because splicing the original text back is a no-op at any
    offset. Every mutant on every such line was lost, invisibly, while the scores in
    `docs/testing.md` were being quoted from this harness. Found by this tool's own tests: a
    one-line fixture with an em dash in it produced no mutant at all.

    An offset that falls inside a character cannot come from a real node; it is ignored rather than
    raised on, so a caller gets a short span instead of a crash.
    """
    return len(line.encode("utf-8")[:byte_offset].decode("utf-8", errors="ignore"))


def _span(source: str, node: ast.AST) -> tuple[int, int]:
    """The node's character span in ``source`` (absolute offsets, not line/column)."""
    lines = source.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))
    start = starts[node.lineno - 1] + _char_offset(lines[node.lineno - 1], node.col_offset)
    end = starts[node.end_lineno - 1] + _char_offset(lines[node.end_lineno - 1], node.end_col_offset)
    return start, end


def _mutate(kind: str, node: ast.AST, source: str) -> tuple[str, str, str]:
    """Return (replacement text, before, after) for a site. Raises ValueError if it cannot."""
    if kind == "compare":
        op = node.ops[0]
        swapped = {
            ast.Eq: ast.NotEq,
            ast.NotEq: ast.Eq,
            ast.Lt: ast.LtE,
            ast.LtE: ast.Lt,
            ast.Gt: ast.GtE,
            ast.GtE: ast.Gt,
        }
        replacement = swapped[type(op)]
        return (
            ast.unparse(ast.Compare(left=node.left, ops=[replacement()], comparators=node.comparators)),
            SYMBOLS[type(op).__name__],
            SYMBOLS[replacement.__name__],
        )
    if kind == "boolop":
        replacement = ast.Or if isinstance(node.op, ast.And) else ast.And
        return (
            ast.unparse(ast.BoolOp(op=replacement(), values=node.values)),
            SYMBOLS[type(node.op).__name__],
            SYMBOLS[replacement.__name__],
        )
    if kind == "drop-not":
        return (ast.unparse(node.operand), "not x", "x")
    if kind == "bool":
        return (ast.unparse(ast.Constant(value=not node.value)), str(node.value), str(not node.value))
    if kind == "int":
        replacement = 1 if node.value == 0 else 0
        return (ast.unparse(ast.Constant(value=replacement)), str(node.value), str(replacement))
    if kind == "string":
        return (
            ast.unparse(ast.Constant(value=node.value + "-mutated")),
            repr(node.value)[:40],
            repr(node.value + "-mutated")[:40],
        )
    if kind == "minmax":
        replacement = "min" if node.func.id == "max" else "max"
        return (
            ast.unparse(ast.Call(func=ast.Name(id=replacement), args=node.args, keywords=node.keywords)),
            node.func.id,
            replacement,
        )
    if kind == "return-none":
        return ("return None", ast.unparse(node)[:60], "return None")
    raise ValueError(f"unknown site kind {kind!r}")  # pragma: no cover - guarded by _site_kind


def mutants_for_source(
    source: str,
    file: str,
    *,
    limit: int | None = None,
    include_strings: bool = False,
    drops: list[dict[str, object]] | None = None,
) -> list[Mutant]:
    """Every valid single-site mutant of ``source``, in site order, up to ``limit``.

    A site whose replacement does not produce text that compiles is dropped rather than reported:
    the property being measured is "does the suite notice a behaviour change", and a file that
    cannot be parsed is not one. ``drops``, when given, receives one record per dropped site — a
    silent drop is how a harness bug hid a whole class of mutants (see :func:`_char_offset`), so the
    count travels into the report instead of living only in this function's control flow.
    """
    out: list[Mutant] = []
    for kind, node in sites(source, include_strings=include_strings):
        if limit is not None and len(out) >= limit:
            break
        start, end = _span(source, node)
        if not (0 <= start < end <= len(source)):  # pragma: no cover - defensive
            _note_drop(drops, file, node, kind, "the span does not lie inside the file")
            continue
        try:
            replacement, before, after = _mutate(kind, node, source)
        except ValueError:  # pragma: no cover - defensive
            _note_drop(drops, file, node, kind, "the operator could not be applied")
            continue
        mutated = source[:start] + replacement + source[end:]
        try:
            compile(mutated, file, "exec")
        except (SyntaxError, ValueError):
            _note_drop(drops, file, node, kind, "the replacement does not compile")
            continue
        if mutated == source:  # pragma: no cover - a no-op is not a mutant
            _note_drop(drops, file, node, kind, "the replacement is the original text")
            continue
        out.append(
            Mutant(
                file=file,
                line=node.lineno,
                operator=kind,
                before=before,
                after=after,
                source=mutated,
            )
        )
    return out


def _note_drop(drops: list[dict[str, object]] | None, file: str, node: ast.AST, kind: str, reason: str) -> None:
    """Record a site that produced no mutant. Nothing calls this on the happy path."""
    if drops is not None:
        drops.append({"file": file, "line": node.lineno, "operator": kind, "reason": reason})


def splice_only(source: str) -> str:
    """The source with the same splice applied back verbatim — the harness's own falsification.

    Splicing must be a no-op when nothing changes; if this ever differs from ``source``, the
    harness is editing files it did not mean to edit and every score it reports is suspect.
    """
    out = source
    for _kind, node in sites(source):
        start, end = _span(out, node)
        out = out[:start] + out[start:end] + out[end:]
    return out


def _run_tests(sandbox: Path, tests: list[str], timeout: float) -> tuple[str, str]:
    """Run the given tests in the sandbox copy. Returns (outcome, detail)."""
    env = {
        "PYTHONPATH": str(sandbox / "src"),
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(sandbox),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    try:
        # No `-q` here: `pyproject.toml` carries one in `addopts`, and a second one makes this a
        # `-qq` run — at which level pytest 9 prints no counts line at all. The control run's counts
        # (and every survivor's "the suite said" line) are read off that line, so passing `-q` here
        # is what would make them unreadable. `-x` is deliberate: the first failure is the kill.
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", *tests, "-x", "-p", "no:randomly", "-p", "no:cacheprovider"],
            cwd=str(sandbox),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        # A mutant that hangs the suite *is* detected, but it is not a clean kill: say so rather
        # than folding it into the assertions' tally.
        return "timeout", f"no result within {timeout:.0f}s"
    lines = [line for line in (completed.stdout or "").strip().splitlines() if line.strip()]
    summary = lines[-1] if lines else ""
    if completed.returncode == 0:
        return "survived", summary or "the suite passed"
    if completed.returncode in (2, 3, 4):
        # pytest's own errors: collection failure, internal error, usage error.
        return "killed-import", summary or "the copy could not even be collected"
    return "killed", summary


def pytest_counts(summary: str) -> dict[str, int]:
    """Parse pytest's own summary line into ``{outcome: count}``, e.g. ``{"passed": 306}``.

    Used on the control run: what the subset *did* before any mutation is part of the measurement,
    and "it passed" is not the same fact as "it ran". A test that skips cannot fail, so a subset
    that skips inside the region being mutated cannot kill mutants there.
    """
    counts: dict[str, int] = {}
    for number, word in re.findall(r"(\d+)\s+([a-z]+)", summary):
        if word in {"passed", "failed", "skipped", "error", "errors", "xfailed", "xpassed", "deselected"}:
            counts["error" if word == "errors" else word] = int(number)
    return counts


def run_mutation_sweep(
    *,
    files: list[str],
    tests_for: dict[str, list[str]],
    fallback_tests: list[str],
    max_mutants: int,
    seed: int,
    timeout: float,
    include_strings: bool,
    budget_seconds: float,
    only_lines: list[int] | None = None,
    keep: bool = False,
    log=None,
) -> dict:
    """Mutate, run, classify. Returns the report document.

    ``only_lines`` restricts the run to the mutants on those source lines and runs *all* of them,
    sampling nothing: it exists so that a reader can re-check one survivor with the harness's own
    machinery — its copy, its interpreter, its environment — in seconds rather than re-running a
    ten-minute sweep. The report says which lines were selected, so a filtered run is never read as
    a whole-file score.
    """
    say = log or (lambda _message: None)
    started = time.monotonic()
    sandbox_root = Path(tempfile.mkdtemp(prefix="docket-mutate-"))
    repo = sandbox_root / "repo"
    shutil.copytree(ROOT, repo, ignore=COPY_IGNORE)

    originals = {name: (ROOT / name).read_text(encoding="utf-8") for name in files}

    # 0. Falsify the harness on itself, then prove the sandbox can pass the suite at all.
    for name, source in originals.items():
        if splice_only(source) != source:
            shutil.rmtree(sandbox_root, ignore_errors=True)
            raise AssertionError(f"the mutation harness altered {name} without mutating it; refusing to report a score")
    control_tests = sorted({test for name in files for test in tests_for.get(name, fallback_tests)})
    say(f"  control: running {len(control_tests)} test path(s) in the sandbox, unmutated")
    control_started = time.monotonic()
    outcome, detail = _run_tests(repo, control_tests, timeout * 3)
    if outcome != "survived":
        shutil.rmtree(sandbox_root, ignore_errors=True)
        raise RuntimeError(
            f"the sandbox does not pass the suite before any mutation ({outcome}: {detail}). "
            "Every mutant would be scored as killed against a broken baseline, so no score is reported."
        )
    # The control's counts travel in the report. On 2026-10-16 an interpreter without the optional
    # `cryptography` extra skipped the signature tests, so five mutants in `verify.py`'s own
    # signature block had nothing left to catch them and were scored as survivors (171/177 instead
    # of 176/177). The harness cannot tell a deliberate `skipif` from a missing dependency; it can
    # stop a reader from having to guess which environment a score was taken in.
    control = {
        "tests": control_tests,
        "counts": pytest_counts(detail),
        "summary_line": detail,
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "duration_seconds": round(time.monotonic() - control_started, 2),
    }
    control["clean"] = (
        bool(control["counts"]) and not control["counts"].get("skipped") and not control["counts"].get("failed")
    )
    # A counts line with no "skipped" in it means pytest saw no skips, so say `0 skipped`, not `?`:
    # `?` reads as "unknown", and this is the line a reader checks to find out which it was.
    skipped_in_control = control["counts"].get("skipped", 0) if control["counts"] else "?"
    say(
        f"  control: {control['counts'].get('passed', '?')} passed, {control['counts'].get('failed', 0)} failed, "
        f"{skipped_in_control} skipped in {control['duration_seconds']:.1f}s — {detail}"
    )

    planned: list[Mutant] = []
    drops: list[dict[str, object]] = []
    for name in files:
        found = mutants_for_source(originals[name], name, include_strings=include_strings, drops=drops)
        say(f"  {name}: {len(found)} candidate mutant(s)")
        planned.extend(found)
    if drops:
        # A dropped site is a harness limitation, not a survivor and not a kill: say so, and carry
        # the records into the report so a reader can see which lines produced no mutant and why.
        say(f"  {len(drops)} site(s) produced no mutant: {[(d['line'], d['reason']) for d in drops]}")

    if only_lines:
        wanted = sorted(set(only_lines))
        planned = [mutant for mutant in planned if mutant.line in set(wanted)]
        if not planned:
            shutil.rmtree(sandbox_root, ignore_errors=True)
            raise RuntimeError(f"no mutant sits on line(s) {wanted} of {', '.join(files)}")
        say(f"  only-line {wanted}: running all {len(planned)} mutant(s) on that line, sampled none")

    rng = random.Random(seed)
    order = list(range(len(planned)))
    rng.shuffle(order)
    chosen = order if only_lines else (sorted(order[:max_mutants]) if max_mutants < len(planned) else order)
    chosen_mutants = [planned[index] for index in chosen]

    results: list[dict] = []
    try:
        for number, mutant in enumerate(chosen_mutants, start=1):
            if time.monotonic() - started > budget_seconds:
                say(f"  budget of {budget_seconds:.0f}s reached after {number - 1} mutant(s)")
                break
            target = repo / mutant.file
            backup = target.read_text(encoding="utf-8")
            target.write_text(mutant.source, encoding="utf-8")
            outcome, detail = _run_tests(repo, tests_for.get(mutant.file, fallback_tests), timeout)
            target.write_text(backup, encoding="utf-8")
            results.append({**mutant.as_dict(), "outcome": outcome, "detail": detail[:200]})
            say(f"  {number}/{len(chosen_mutants)} {outcome:13s} {mutant.describe()}")
    finally:
        if not keep:
            shutil.rmtree(sandbox_root, ignore_errors=True)

    killed = [r for r in results if r["outcome"] in {"killed", "killed-import"}]
    survived = [r for r in results if r["outcome"] == "survived"]
    timeouts = [r for r in results if r["outcome"] == "timeout"]
    scored = len(killed) + len(survived)  # timeouts are reported, never scored
    return {
        "seed": seed,
        "files": files,
        "tests": control_tests,
        "control": control,
        "candidates": len(planned),
        "dropped": drops,
        "sampled": len(chosen_mutants),
        "run": len(results),
        "killed": len(killed),
        "killed_by_import": len([r for r in killed if r["outcome"] == "killed-import"]),
        "survived": len(survived),
        "timed_out": len(timeouts),
        "score": round(len(killed) / scored, 4) if scored else 0.0,
        "score_denominator": scored,
        "duration_seconds": round(time.monotonic() - started, 2),
        "include_strings": include_strings,
        "only_lines": sorted(set(only_lines)) if only_lines else [],
        "survivors": survived,
        "mutants": results,
        "sandbox_kept": keep,
    }


def write_markdown(report: dict, path: Path) -> None:
    lines = [
        "# Mutation report",
        "",
        f"- files: {', '.join(report['files'])}",
        f"- tests: {', '.join(report['tests'])}",
        f"- seed: {report['seed']} · candidates: {report['candidates']} · sampled: {report['sampled']} · run: {report['run']}",
        f"- killed: {report['killed']} (of which {report['killed_by_import']} by an import/collection error)",
        f"- survived: {report['survived']} · timed out (reported, not scored): {report['timed_out']}",
        f"- **score: {report['score']:.1%}** over {report['score_denominator']} scored mutant(s)",
        f"- string constants mutated: {report['include_strings']}",
        f"- selected lines: {report['only_lines'] or 'all'}",
        f"- sites that produced no mutant: {len(report.get('dropped', []))}",
    ]
    control = report.get("control")
    if control:
        counts = control.get("counts", {})
        lines.insert(
            4,
            f"- control: {counts.get('passed', 0)} passed, {counts.get('failed', 0)} failed, "
            f"{counts.get('skipped', 0)} skipped in {control.get('duration_seconds', 0.0):.1f}s before any "
            f"mutation (Python {control.get('python', '?')})",
        )
        if counts.get("skipped"):
            lines.insert(
                5,
                f"  - **{counts['skipped']} test(s) skipped in the control run: a mutant only a skipped test "
                "would catch survives, and the score below reflects that environment.**",
            )
    for drop in report.get("dropped", []):
        lines.append(f"  - `{drop['file']}:{drop['line']}` [{drop['operator']}]: {drop['reason']}")
    lines += [
        "",
        "## Survivors",
        "",
    ]
    if not report["survivors"]:
        lines.append("None: every sampled mutant was caught by the suite.")
    else:
        lines.append("| file:line | operator | change | the suite said |")
        lines.append("| --- | --- | --- | --- |")
        for item in report["survivors"]:
            lines.append(
                f"| `{item['file']}:{item['line']}` | {item['operator']} | `{item['from']}` → `{item['to']}` | {item['detail']} |"
            )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mutation-test Docket's cryptographic core")
    parser.add_argument("--file", action="append", dest="files", help="a file to mutate (repeatable)")
    parser.add_argument("--tests", help="comma-separated tests to run for every mutant (default: per-file map)")
    parser.add_argument("--max-mutants", type=int, default=30, help="how many mutants to run (default 30)")
    parser.add_argument(
        "--only-line",
        action="append",
        dest="only_lines",
        type=int,
        metavar="LINE",
        help="run every mutant on this source line, sampling nothing (repeatable; ignores --max-mutants). "
        "The cheap way to re-check a survivor with the harness's own machinery",
    )
    parser.add_argument("--seed", type=int, default=0, help="sampling seed (default 0)")
    parser.add_argument("--timeout", type=float, default=120.0, help="seconds allowed per mutant")
    parser.add_argument("--budget-seconds", type=float, default=600.0, help="stop after this long")
    parser.add_argument("--min-score", type=float, default=0.85, help="fail below this kill rate")
    parser.add_argument("--include-strings", action="store_true", help="also mutate string constants")
    parser.add_argument("--json", type=Path, help="write the full report here")
    parser.add_argument("--markdown", type=Path, help="write a readable report here")
    parser.add_argument("--list", action="store_true", help="list the mutants instead of running them")
    parser.add_argument("--keep", action="store_true", help="keep the sandbox copy (debugging)")
    args = parser.parse_args(argv)

    files = args.files or DEFAULT_FILES
    for name in files:
        if not (ROOT / name).exists():
            print(f"mutate: no such file: {name}", file=sys.stderr)
            return 2
    fallback = [part.strip() for part in args.tests.split(",")] if args.tests else ["tests"]

    if args.list:
        total = 0
        for name in files:
            source = (ROOT / name).read_text(encoding="utf-8")
            found = mutants_for_source(source, name, include_strings=args.include_strings)
            if args.only_lines:
                found = [mutant for mutant in found if mutant.line in set(args.only_lines)]
            total += len(found)
            for mutant in found:
                print(f"  {mutant.describe()}")
        print(f"{total} candidate mutant(s) across {len(files)} file(s)")
        return 0

    print(
        f"docket mutation sweep — {len(files)} file(s), up to {args.max_mutants} mutant(s), seed {args.seed}, "
        f"{args.budget_seconds:.0f}s budget"
    )
    report = run_mutation_sweep(
        files=files,
        tests_for=DEFAULT_TESTS,
        fallback_tests=fallback,
        max_mutants=args.max_mutants,
        seed=args.seed,
        timeout=args.timeout,
        include_strings=args.include_strings,
        budget_seconds=args.budget_seconds,
        only_lines=args.only_lines,
        keep=args.keep,
        log=print,
    )
    print(
        f"\n{report['killed']} killed, {report['survived']} survived, {report['timed_out']} timed out "
        f"of {report['run']} run — score {report['score']:.1%} (seed {report['seed']})"
    )
    skipped_in_control = report.get("control", {}).get("counts", {}).get("skipped", 0)
    if skipped_in_control:
        print(
            f"\nwarning: the control run skipped {skipped_in_control} test(s) before any mutation, so a "
            "mutant only a skipped test would catch is scored as a survivor. The score is conditional "
            "on a subset that did not run; install the optional extras and re-run."
        )
    if report["survivors"]:
        print("\nSurvivors (a change no test noticed):")
        for item in report["survivors"]:
            print(f"  {item['file']}:{item['line']} [{item['operator']}] {item['from']} -> {item['to']}")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"\nreport: {args.json}")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        write_markdown(report, args.markdown)
        print(f"report: {args.markdown}")
    return 0 if report["score"] >= args.min_score else 1


if __name__ == "__main__":
    raise SystemExit(main())
