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
   the run. Widen ``TARGET_TESTS`` and change nothing else: if the score moves, it was the subset.
2. **The branch cannot be reached.** A mutant of defensive code that nothing can execute survives
   by construction. The answer is to delete the dead branch, not to write a test that pretends to
   cover it — see ``docs/testing.md``.
3. **The mutant is equivalent.** It changes the text and not the behaviour. Then the *reason* is
   written down beside the other survivors, because an unexplained survivor and a known-equivalent
   survivor look identical in a report.

Only what remains is a gap: a real behaviour change the suite did not notice. The ledger's first
sweep scored 50.0% over a 70-mutant sample and most of its 35 survivors turned out to be gaps of
exactly that kind; the second, after the tests those survivors asked for, scored 83.6% with every
one of its 28 remaining survivors classified.

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
    "src/docket/canon.py": ["tests/test_canon.py", "tests/test_vectors.py"],
    # `test_signing_and_attach.py` is in this list because the sealing and signing checks live in
    # the same module: without it, every mutant in the Ed25519 branch survives and the score
    # understates the suite (a survivor can be an artefact of the subset, not a gap).
    "src/docket/verify.py": [
        "tests/test_receipt_verify.py",
        "tests/test_vectors.py",
        "tests/test_signing_and_attach.py",
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
    ],
    "src/docket/util.py": ["tests/test_url_guards.py", "tests/test_cli_input.py"],
    "src/docket/policy.py": ["tests/test_policy.py", "tests/test_cli_input.py"],
    "src/docket/pricing.py": ["tests/test_pricing.py"],
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


def _span(source: str, node: ast.AST) -> tuple[int, int]:
    """The node's character span in ``source`` (absolute offsets, not line/column)."""
    starts = [0]
    for line in source.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    start = starts[node.lineno - 1] + node.col_offset
    end = starts[node.end_lineno - 1] + node.end_col_offset
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
) -> list[Mutant]:
    """Every valid single-site mutant of ``source``, in site order, up to ``limit``.

    A site whose replacement does not produce text that compiles is dropped rather than reported:
    the property being measured is "does the suite notice a behaviour change", and a file that
    cannot be parsed is not one.
    """
    out: list[Mutant] = []
    for kind, node in sites(source, include_strings=include_strings):
        if limit is not None and len(out) >= limit:
            break
        start, end = _span(source, node)
        if not (0 <= start < end <= len(source)):  # pragma: no cover - defensive
            continue
        try:
            replacement, before, after = _mutate(kind, node, source)
        except ValueError:  # pragma: no cover - defensive
            continue
        mutated = source[:start] + replacement + source[end:]
        try:
            compile(mutated, file, "exec")
        except (SyntaxError, ValueError):
            continue
        if mutated == source:  # pragma: no cover - a no-op is not a mutant
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
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", *tests, "-x", "-q", "-p", "no:randomly", "-p", "no:cacheprovider"],
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
    keep: bool = False,
    log=None,
) -> dict:
    """Mutate, run, classify. Returns the report document."""
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
    outcome, detail = _run_tests(repo, control_tests, timeout * 3)
    if outcome != "survived":
        shutil.rmtree(sandbox_root, ignore_errors=True)
        raise RuntimeError(
            f"the sandbox does not pass the suite before any mutation ({outcome}: {detail}). "
            "Every mutant would be scored as killed against a broken baseline, so no score is reported."
        )

    planned: list[Mutant] = []
    for name in files:
        found = mutants_for_source(originals[name], name, include_strings=include_strings)
        say(f"  {name}: {len(found)} candidate mutant(s)")
        planned.extend(found)

    rng = random.Random(seed)
    order = list(range(len(planned)))
    rng.shuffle(order)
    chosen = sorted(order[:max_mutants]) if max_mutants < len(planned) else order
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
        "candidates": len(planned),
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
        keep=args.keep,
        log=print,
    )
    print(
        f"\n{report['killed']} killed, {report['survived']} survived, {report['timed_out']} timed out "
        f"of {report['run']} run — score {report['score']:.1%} (seed {report['seed']})"
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
