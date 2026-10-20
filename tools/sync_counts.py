"""Regenerate every number the documentation quotes about the suite and the version.

Three classes of number appear in prose in this repository, and all three were typed by hand:

* the size of the test suite, quoted in four documents and in a per-file table;
* the version, quoted in the README badge, the README note and `docs/production-coverage.md`;
* nothing else, by design.

The counts are *derived* — `pytest --collect-only` reports them — so a number in a document is
either what pytest collected or it is stale. The guards in `tests/test_doc_links.py` already
refuse a stale number; this tool is the half that fixes one, so a contributor runs
`make sync` instead of editing five files and hoping.

    python3 tools/sync_counts.py            # rewrite the tree, print what changed
    python3 tools/sync_counts.py --check    # exit 1 if anything would change (for CI)
    python3 tools/sync_counts.py --dry-run  # report without writing

The version is read from `pyproject.toml`, which is the single source of authority: the installed
distribution and `src/docket/_version.py` both derive from it, so nothing here writes it back.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: Every place that quotes the size of the whole suite, as (file, regex with one group). The
#: patterns are the same ones `tests/test_doc_links.py` guards with, so a number the guard reads
#: is a number this tool can rewrite. A pattern that stops matching is a failure, not a silence.
SUITE_TOTAL_QUOTES: tuple[tuple[str, str], ...] = (
    ("README.md", r"make check` \((\d+) tests"),
    ("README.md", r"That is (\d+) tests in about three minutes"),
    ("CONTRIBUTING.md", r"# (\d+) tests, fully offline"),
    ("docs/testing.md", r"# (\d+) tests, no network"),
    ("docs/sre-coverage.md", r"the whole suite: (\d+) tests"),
)

#: The per-file table in `docs/testing.md`, whose rows are `| `test_x.py` | N |`.
PER_FILE_ROW = re.compile(r"^\| `(test_[\w.]+)\.py` \| (\d+) \|", re.MULTILINE)

#: Where the version is quoted in prose, as (file, regex with one group).
VERSION_QUOTES: tuple[tuple[str, str], ...] = (
    ("README.md", r"version-(\d+\.\d+\.\d+)-blue"),
    ("README.md", r"Docket (\d+\.\d+\.\d+) is on PyPI"),
    ("docs/production-coverage.md", r"\(`(\d+\.\d+\.\d+)`\)"),
)


class SyncError(RuntimeError):
    """A number could not be derived, or a document no longer matches its pattern."""


def project_version(root: Path | None = None) -> str:
    """The version `pyproject.toml` declares, which is the one authority."""
    text = ((root or REPO) / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"', text, re.MULTILINE)
    if not match:
        raise SyncError("pyproject.toml does not declare a version")
    return match.group(1)


def collected_test_counts(root: Path | None = None) -> dict[str, int]:
    """What `pytest --collect-only` reports per test file, from this interpreter's own run.

    The root is a parameter rather than a module global so a test can point it at a scratch tree
    without having to rebind `REPO` and hope no other fixture has already read it.
    """
    root = root or REPO
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "--collect-only", "-q"],
        cwd=str(root),
        env=dict(os.environ, PYTHONPATH=str(REPO / "src")),
        capture_output=True,
        text=True,
        timeout=600,
    )
    if completed.returncode != 0:
        raise SyncError("pytest --collect-only failed:\n" + completed.stdout[-2000:] + completed.stderr[-2000:])
    counts: dict[str, int] = {}
    for line in completed.stdout.splitlines():
        # `pytest --collect-only -q` prints one `tests/test_x.py: N` line per file.
        match = re.match(r"^(tests/test_[\w.]+\.py): (\d+)$", line.strip())
        if match:
            counts[match.group(1).removeprefix("tests/")] = int(match.group(2))
    if not counts:
        raise SyncError("pytest --collect-only reported no tests; the parser has stopped working")
    return counts


def _label(path: Path) -> str:
    """A path as a reader would name it, or in full when it is outside this repository.

    `relative_to` raises for a scratch tree under `/tmp`, and a test that corrupts a copy of a
    document is exactly the caller that hands one in.
    """
    try:
        return str(path.relative_to(REPO))
    except ValueError:
        return str(path)


def _rewrite(path: Path, pattern: str, replacement: str, label: str) -> str | None:
    """Replace the one captured group in `path` with `replacement`, or say what is wrong.

    Only the group is substituted. The literal text around it — the ``# `` a reader sees, the
    ``make check` (`` before it — is part of the sentence, and a `re.sub` over the whole match
    would delete it. That happened once while this tool was being written, and it turned a
    correct count into a sentence that no longer read as one.
    """
    text = path.read_text(encoding="utf-8")
    compiled = re.compile(pattern)
    matches = list(compiled.finditer(text))
    if len(matches) != 1:
        raise SyncError(f"{_label(path)}: expected exactly one match for {pattern!r}, found {len(matches)}")
    match = matches[0]
    if match.group(1) == replacement:
        return None
    start, end = match.span(1)
    new = text[:start] + replacement + text[end:]
    if new == text:
        raise SyncError(f"{_label(path)}: {label} did not change, which means the pattern is wrong")
    path.write_text(new, encoding="utf-8")
    return f"{_label(path)}: {label} {match.group(1)} -> {replacement}"


def _rewrite_table(path: Path, counts: dict[str, int]) -> list[str]:
    """Bring the per-file table in `docs/testing.md` to the collected counts."""
    text = path.read_text(encoding="utf-8")
    documented = {f"{name}.py": int(count) for name, count in PER_FILE_ROW.findall(text)}
    if len(documented) < 30:
        raise SyncError(f"{_label(path)}: the counted table was not found (parsed {sorted(documented)})")

    changes: list[str] = []
    missing = sorted(set(counts) - set(documented))
    extra = sorted(set(documented) - set(counts))
    if missing:
        changes.append(f"{_label(path)}: table is missing rows for {missing}")
    if extra:
        changes.append(f"{_label(path)}: table names files that do not exist: {extra}")

    def replace_row(match: re.Match[str]) -> str:
        name = f"{match.group(1)}.py"
        if name not in counts:
            return match.group(0)
        return f"| `{match.group(1)}.py` | {counts[name]} |"

    new = PER_FILE_ROW.sub(replace_row, text)
    if new != text:
        path.write_text(new, encoding="utf-8")
    for name in sorted(documented):
        if name in counts and documented[name] != counts[name]:
            changes.append(f"{_label(path)}: `{name}` {documented[name]} -> {counts[name]}")
    return changes


def sync(check_only: bool, dry_run: bool, root: Path | None = None) -> int:
    """Bring every quoted number to the measured value. Returns a process exit code."""
    root = root or REPO
    version = project_version(root)
    counts = collected_test_counts(root)
    total = sum(counts.values())
    changes: list[str] = []

    for filename, pattern in SUITE_TOTAL_QUOTES:
        path = root / filename
        if not path.is_file():
            raise SyncError(f"{filename} is quoted in the suite-size list and is not in the tree")
        if check_only or dry_run:
            text = path.read_text(encoding="utf-8")
            found = re.findall(pattern, text)
            if len(found) != 1:
                raise SyncError(f"{filename}: expected exactly one match for {pattern!r}, found {len(found)}")
            if int(found[0]) != total:
                changes.append(f"{filename}: suite total {found[0]} -> {total}")
        else:
            note = _rewrite(path, pattern, str(total), "suite total")
            if note:
                changes.append(note)

    if check_only or dry_run:
        text = (root / "docs" / "testing.md").read_text(encoding="utf-8")
        documented = {f"{n}.py": int(c) for n, c in PER_FILE_ROW.findall(text)}
        wrong = {
            n: (documented[n], counts[n]) for n in sorted(documented) if n in counts and documented[n] != counts[n]
        }
        if wrong:
            changes.append(f"docs/testing.md: per-file counts stale {wrong}")
        if set(counts) - set(documented):
            changes.append(f"docs/testing.md: missing rows for {sorted(set(counts) - set(documented))}")
    else:
        changes.extend(_rewrite_table(root / "docs" / "testing.md", counts))

    for filename, pattern in VERSION_QUOTES:
        path = root / filename
        if not path.is_file():
            raise SyncError(f"{filename} is quoted in the version list and is not in the tree")
        if check_only or dry_run:
            text = path.read_text(encoding="utf-8")
            found = re.findall(pattern, text)
            if len(found) != 1:
                raise SyncError(f"{filename}: expected exactly one match for {pattern!r}, found {len(found)}")
            if found[0] != version:
                changes.append(f"{filename}: version {found[0]} -> {version}")
        else:
            note = _rewrite(path, pattern, version, "version")
            if note:
                changes.append(note)

    label = "would change" if (check_only or dry_run) else "changed"
    if not changes:
        print(f"every quoted number matches the tree: version {version}, {total} tests in {len(counts)} files")
        return 0
    for change in changes:
        print(f"  {change}")
    print(f"{len(changes)} number(s) {label}")
    return 1 if check_only else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if any number would change; write nothing")
    parser.add_argument("--dry-run", action="store_true", help="report what would change; write nothing")
    parser.add_argument("--root", type=Path, default=None, help="tree to operate on (default: this repository)")
    arguments = parser.parse_args(argv)
    try:
        return sync(check_only=arguments.check, dry_run=arguments.dry_run, root=arguments.root)
    except SyncError as error:
        print(f"sync_counts: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
