"""Prove tests/test_doc_tables.py by mutation, without leaving damage.

Each mutation reintroduces one of the defects the guard was written after, or removes the thing that
makes the guard able to see at all. The harness applies, runs the guard, and restores in a `finally`,
and refuses to report success unless the file it read back is byte-identical to what it read.

The two "the scanner stops working" mutations matter more than the rest. A guard over an empty set
passes for the wrong reason, and this one is parametrised over every table row in the documentation,
so a scanner that silently matched nothing would still report 1405 passing tests.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TESTING = REPO / "docs" / "testing.md"
COVERAGE = REPO / "docs" / "production-coverage.md"
GUARD = REPO / "tests" / "test_doc_tables.py"

MUTATIONS: tuple[tuple[str, Path, str, str], ...] = (
    (
        "the per-file table's header loses its fourth column, so 17 rationales stop rendering",
        TESTING,
        "| file | tests | what it pins | why it exists |\n| --- | --- | --- | --- |\n",
        "| file | tests | what it pins |\n| --- | --- | --- |\n",
    ),
    (
        "a row gains a fifth cell the header does not have",
        TESTING,
        "| `test_install_budget.py` | 5 |",
        "| `test_install_budget.py` | 5 | and a cell that will not render |",
    ),
    (
        "an unescaped pipe returns inside a code span",
        COVERAGE,
        "chart writes its integers through `\\| int64` now",
        "chart writes its integers through `| int64` now",
    ),
    (
        "a row repeats its own base in two cells, the shape the botched repair left",
        TESTING,
        "|  `test_policy.py` | 14 | accept and reject paths for every policy key  |  |\n",
        "|  `test_policy.py` | 14 | " + "x" * 220 + "  |  " + "x" * 220 + " plus an ending  |\n",
    ),
    (
        "the scanner stops honouring escaped pipes, so correct rows are reported as broken",
        GUARD,
        '    return re.split(r"(?<!\\\\)\\|", body)',
        '    return body.split("|")',
    ),
    (
        "a row repeats its own base in two cells without changing the cell count, which only the "
        "repeat detector can see: the width check passes, so nothing else is looking",
        TESTING,
        "|  `test_explain.py` | 13 | analysis rules and their attributed impact  |  |\n",
        "|  `test_explain.py` | 13 | " + "y" * 220 + "  |  " + "y" * 220 + " plus an ending  |\n",
    ),
)


def run_guard() -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - a fixed argv, no shell, no user input
        [sys.executable, "-m", "pytest", str(GUARD), "-q", "--tb=no", "-p", "no:cacheprovider"],
        capture_output=True,
        text=True,
        timeout=1800,
        cwd=str(REPO),
        env={**os.environ, "PYTHONPATH": "src"},
        check=False,
    )


def main() -> int:
    survived: list[str] = []
    stale: list[str] = []
    for index, (description, path, old, new) in enumerate(MUTATIONS, start=1):
        original = path.read_text(encoding="utf-8")
        found = original.count(old)
        if found != 1:
            print(f"STALE    [{index}/{len(MUTATIONS)}] {description}")
            print(f"         anchor appears {found} times in {path.name}, expected 1")
            stale.append(description)
            continue

        path.write_text(original.replace(old, new, 1), encoding="utf-8")
        try:
            completed = run_guard()
        finally:
            path.write_text(original, encoding="utf-8")

        if path.read_text(encoding="utf-8") != original:
            raise SystemExit(f"{path.name} was not restored; this is a bug in the harness")

        tail = (completed.stdout + completed.stderr).strip().splitlines()
        caught = completed.returncode != 0
        print(f"{'caught  ' if caught else 'SURVIVED'} [{index}/{len(MUTATIONS)}] {description}")
        if tail:
            print(f"         pytest: {tail[-1][:110]}")
        if not caught:
            survived.append(description)

    print()
    total = len(MUTATIONS)
    print(
        f"{total} mutations: {total - len(survived) - len(stale)} caught, {len(survived)} survived, {len(stale)} stale"
    )
    if stale:
        print("A stale anchor means the mutation was never applied, so it was never asked:")
        for description in stale:
            print(f"  - {description}")
    for description in survived:
        print(f"  SURVIVED: {description}")
    return 1 if survived or stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
