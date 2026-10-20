"""Prove tests/test_release_workflow.py by mutation.

Each mutation is a defect reintroduced into the release workflow. A mutation no test catches is a
guard that does not guard. Run from the repository root with
``PYTHONPATH=src python3 tools/mutation/mutate_release.py``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TARGET = REPO / "tests" / "test_release_workflow.py"
WORKFLOW = REPO / ".github" / "workflows" / "release.yml"

MUTATIONS: list[tuple[str, str, str]] = [
    (
        "the package under test is never installed",
        "        run: pip install .\n",
        "",
    ),
    (
        "the test-dependency list loses cryptography",
        "run: pip install build pytest cryptography pyyaml\n",
        "run: pip install build pytest pyyaml\n",
    ),
    (
        "the gate runs a subset instead of the suite directory",
        "pytest tests -ra",
        "pytest tests/test_receipt_verify.py -ra",
    ),
    (
        "the gate asks for a quieter run than the docs quote",
        "pytest tests -ra",
        "pytest tests -qq",
    ),
    (
        "a tag with no notes is published anyway",
        "            exit 1\n",
        "            exit 0\n",
    ),
    (
        "the workflow uploads to PyPI itself",
        "        run: python -m build\n",
        "        run: python -m build\n      - name: Upload to PyPI\n        run: twine upload dist/*.whl dist/*.tar.gz\n",
    ),
    (
        "the GitHub release uses a long-lived secret instead of github.token",
        "GH_TOKEN: ${{ github.token }}",
        "GH_TOKEN: ${{ secrets.RELEASE_TOKEN }}",
    ),
    (
        "the workflow fires on every push rather than on a tag",
        '    tags: ["v*"]\n',
        "",
    ),
]


def run_suite() -> tuple[bool, str]:
    # The environment is inherited rather than rebuilt. The first version hardcoded a path to a
    # virtual environment, which bandit flags as an insecure temporary path and which is wrong
    # anyway: this script is run from whatever interpreter and environment the caller set up, and a
    # mutation score taken in a different environment from the one being measured is not a score.
    environment = {**os.environ}
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = f"src{os.pathsep}{existing}" if existing else "src"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(TARGET), "-q", "--no-header", "--tb=no"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=600,
        env=environment,
    )
    return proc.returncode == 0, (proc.stdout + proc.stderr)[-800:]


def main() -> int:
    ok, output = run_suite()
    if not ok:
        print("the suite does not pass on an unmutated tree; fix that first")
        print(output)
        return 2

    escaped = 0
    for name, old, new in MUTATIONS:
        original = WORKFLOW.read_text(encoding="utf-8")
        if original.count(old) < 1:
            print(f"SKIP  {name}: the anchor is not in the workflow")
            escaped += 1
            continue
        WORKFLOW.write_text(original.replace(old, new, 1), encoding="utf-8")
        try:
            passed, output = run_suite()
        finally:
            WORKFLOW.write_text(original, encoding="utf-8")
        if passed:
            print(f"ESCAPED  {name}")
            escaped += 1
        else:
            print(f"caught   {name}")

    print()
    if escaped:
        print(f"{escaped} of {len(MUTATIONS)} mutations were not caught")
        return 1
    print(f"all {len(MUTATIONS)} mutations caught")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
