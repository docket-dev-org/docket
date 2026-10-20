"""Prove tests/test_release_workflow.py's version guards by mutation, without leaving damage.

Applies the mutation, runs the guard, restores the file, and reports. The first version of this
applied the mutation and returned: running it left `.github/workflows/release.yml` mutated, which is
a shipped tool that damages the repository it ships with. Measured — running all three harnesses in
sequence left the workflow in exactly that state, and `git status` showed it.

Every other harness in this directory restores. This one did too little to be safe.

Run from the repository root with `PYTHONPATH=src python3 tools/mutation/mutate_release_version.py`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "release.yml"
GUARD = REPO / "tests" / "test_release_workflow.py"

OLD = (
    '            echo "tag ${GITHUB_REF_NAME} does not name the version pyproject.toml '
    'declares (${VERSION}); refusing to publish" >&2\n'
    "            exit 1\n"
)

NEW = '            echo "tag ${GITHUB_REF_NAME} disagrees with pyproject.toml"\n'

DESCRIPTION = "a tag that disagrees with the declared version is reported but not fatal"


def main() -> int:
    original = WORKFLOW.read_text(encoding="utf-8")
    found = original.count(OLD)
    if found != 1:
        raise SystemExit(f"anchor appears {found} times, expected 1; refusing to mutate")

    WORKFLOW.write_text(original.replace(OLD, NEW, 1), encoding="utf-8")
    try:
        completed = subprocess.run(  # noqa: S603 - a fixed argv, no shell
            [sys.executable, "-m", "pytest", str(GUARD), "-q", "--tb=no", "-p", "no:cacheprovider"],
            capture_output=True,
            text=True,
            timeout=900,
            cwd=str(REPO),
            env={**__import__("os").environ, "PYTHONPATH": "src"},
            check=False,
        )
    finally:
        # Restored whether the run passed, failed, crashed or was interrupted, because the file has
        # to be intact before anything else looks at it.
        WORKFLOW.write_text(original, encoding="utf-8")

    tail = (completed.stdout + completed.stderr).strip().splitlines()
    summary = tail[-1] if tail else ""
    caught = completed.returncode != 0
    print(f"{'caught  ' if caught else 'SURVIVED'} {DESCRIPTION}")
    print(f"         pytest: {summary}")
    if WORKFLOW.read_text(encoding="utf-8") != original:
        raise SystemExit("the workflow was not restored; this is a bug in the harness")
    print("         workflow restored")
    return 0 if caught else 1


if __name__ == "__main__":
    raise SystemExit(main())
