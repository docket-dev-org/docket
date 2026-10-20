"""Prove tests/test_install_budget.py by mutation, without leaving damage.

The defect these guards exist for was invisible at run time. The 0.2.7 tag gate set a 300 s
propagation bound, the probe used all of it, and the installer it un-skipped still ran a fixed
5 s / 10 s / 15 s ladder; every attempt failed, the gate went red, and nothing in the log said the
installer had been offered a tenth of the cover. A defect that produces no symptom at the site of
the mistake is exactly the kind that needs mutation rather than inspection.

Each mutation below is applied, the guard is run, and the file is restored in a `finally` -- the
harness pattern every other tool in this directory follows, after `mutate_release_version.py` taught
the cost of applying a mutation and returning.

Two mutations are STALE-detecting rather than catching ones: they check that the anchor is still
where the harness believes it is. A mutation that was never applied has not been caught and has not
escaped; it has not been asked, and reporting it as "not caught" says something is wrong without
saying what.

Run from the repository root with `PYTHONPATH=src python3 tools/mutation/mutate_install_budget.py`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PACKAGING = REPO / "tests" / "test_packaging.py"
WORKFLOW = REPO / ".github" / "workflows" / "release.yml"
GUARD = REPO / "tests" / "test_install_budget.py"

#: Each entry: a description, the file, the exact text to find, and the text to put in its place.
MUTATIONS: tuple[tuple[str, Path, str, str], ...] = (
    (
        "the installer stops consulting the propagation deadline entirely",
        PACKAGING,
        "        and time.monotonic() < deadline\n",
        "",
    ),
    (
        "the installer's deadline is computed from a literal instead of the bound",
        PACKAGING,
        "    deadline = time.monotonic() + PROPAGATION_TIMEOUT_SECONDS\n    last = _run(command, **kwargs)\n",
        "    deadline = time.monotonic() + 30\n    last = _run(command, **kwargs)\n",
    ),
    (
        "the attempt ceiling drops back to the old fixed ladder of 4",
        PACKAGING,
        "INSTALL_RETRIES = 25\n",
        "INSTALL_RETRIES = 4\n",
    ),
    (
        "the installer returns to the old fixed-count loop, ignoring the deadline",
        PACKAGING,
        """    deadline = time.monotonic() + PROPAGATION_TIMEOUT_SECONDS
    last = _run(command, **kwargs)
    attempt = 0
    while (
        last.returncode != 0
        and _looks_like_propagation(last)
        and attempt < INSTALL_RETRIES - 1
        and time.monotonic() < deadline
    ):
        attempt += 1
        time.sleep(min(5 * attempt, 15))
        last = _run(command, **kwargs)
    return last
""",
        """    last = _run(command, **kwargs)
    for attempt in range(1, INSTALL_RETRIES):
        if last.returncode == 0 or not _looks_like_propagation(last):
            return last
        time.sleep(5 * attempt)
        last = _run(command, **kwargs)
    return last
""",
    ),
    (
        "the release gate stops raising the propagation bound",
        WORKFLOW,
        '          DOCKET_PYPI_PROPAGATION_SECONDS: "300"\n',
        '          DOCKET_PYPI_PROPAGATION_SECONDS: "15"\n',
    ),
)


def run_guard() -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - a fixed argv, no shell, no user input
        [sys.executable, "-m", "pytest", str(GUARD), "-q", "--tb=no", "-p", "no:cacheprovider"],
        capture_output=True,
        text=True,
        timeout=900,
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
    print(
        f"{len(MUTATIONS)} mutations: {len(MUTATIONS) - len(survived) - len(stale)} caught, "
        f"{len(survived)} survived, {len(stale)} stale"
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
