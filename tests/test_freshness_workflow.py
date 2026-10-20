"""`.github/workflows/freshness.yml`, tested by running what it runs.

The workflow is the one piece of CI that has to work without a commit to trigger it, and a scheduled
workflow that has never been exercised is the easiest thing in a repository to get wrong. So the shell
it runs is executed here: the drift check, the report it writes, and the `if:` conditions that decide
whether it opens an issue at all.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML is needed to read the workflow")

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "freshness.yml"


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _steps(workflow: dict) -> list[dict]:
    return workflow["jobs"]["check"]["steps"]


def _substitute(script: str, outputs: dict[str, str]) -> str:
    for name, value in outputs.items():
        script = script.replace(f"${{{{ steps.drift.outputs.{name} }}}}", value)
    script = script.replace("${{ github.token }}", "not-a-real-token")
    assert "${{" not in script, script
    return script


# --- the document ------------------------------------------------------------------------------ #


def test_the_workflow_runs_on_a_schedule_and_on_demand(workflow: dict) -> None:
    """A freshness check that only runs on push is a check that only sees the commit that caused it."""
    triggers = workflow.get("on") or workflow.get(True)
    assert "schedule" in triggers, "the workflow has no schedule"
    assert "workflow_dispatch" in triggers, "the workflow cannot be run by hand"


def test_the_workflow_can_open_an_issue(workflow: dict) -> None:
    assert workflow["permissions"].get("issues") == "write", "the workflow cannot write an issue"


def test_the_drift_check_is_the_same_one_ci_runs(workflow: dict) -> None:
    """A freshness check that measures something different from the push check is noise."""
    step = next(s for s in _steps(workflow) if s.get("id") == "drift")
    assert "tools/sync_counts.py --check" in step["run"], step["run"]


def test_the_report_names_the_command_that_fixes_it(workflow: dict) -> None:
    """An issue that does not say what to run is a ticket, not a fix."""
    step = next(s for s in _steps(workflow) if "Write the report" in s.get("name", ""))
    assert "make sync" in step["run"], step["run"]
    assert "sync_counts.py --dry-run" in step["run"], "the report does not say what would change"


def test_the_workflow_does_not_open_an_issue_when_nothing_drifted(workflow: dict) -> None:
    for step in _steps(workflow):
        name = step.get("name", "")
        if "issue" not in name.lower():
            continue
        assert "steps.drift.outputs.exit-code != '0'" in (step.get("if") or ""), (
            f"{name!r} is not guarded on the drift check, so it would fire on a clean tree"
        )


# --- the shell, executed ----------------------------------------------------------------------- #


def test_the_drift_step_passes_on_a_clean_tree(workflow: dict) -> None:
    step = next(s for s in _steps(workflow) if s.get("id") == "drift")
    outputs = Path("/tmp") / "freshness-clean-output"
    outputs.write_text("", encoding="utf-8")
    completed = subprocess.run(
        ["bash", "-c", _substitute(step["run"], {"exit-code": "0"})],
        cwd=str(REPO),
        env={**os.environ, "GITHUB_OUTPUT": str(outputs)},
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    written = dict(line.split("=", 1) for line in outputs.read_text().splitlines() if "=" in line)
    assert written.get("exit-code") == "0", written


def test_the_drift_step_reports_a_drifted_tree(workflow: dict, tmp_path: Path) -> None:
    """The other half: a drifted tree must produce a non-zero exit code the workflow can branch on."""
    tree = tmp_path / "repo"
    tree.mkdir()
    (tree / "docs").mkdir()
    for name in (
        "README.md",
        "CONTRIBUTING.md",
        "docs/testing.md",
        "docs/sre-coverage.md",
        "docs/production-coverage.md",
        "pyproject.toml",
    ):
        source = REPO / name
        target = tree / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    for helper in ("tools/sync_counts.py",):
        target = tree / helper
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((REPO / helper).read_text(encoding="utf-8"), encoding="utf-8")
    # Drift one of the quoted numbers.
    contributing = tree / "CONTRIBUTING.md"
    text = contributing.read_text(encoding="utf-8")
    contributing.write_text(text.replace("# 1238 tests, fully offline", "# 999 tests, fully offline"), encoding="utf-8")

    step = next(s for s in _steps(workflow) if s.get("id") == "drift")
    outputs = tmp_path / "drifted-output"
    outputs.write_text("", encoding="utf-8")
    completed = subprocess.run(
        ["bash", "-c", _substitute(step["run"], {"exit-code": "0"})],
        cwd=str(tree),
        env={
            **os.environ,
            "GITHUB_OUTPUT": str(outputs),
            "PYTHONPATH": str(tree / "src") if (tree / "src").exists() else "",
        },
        capture_output=True,
        text=True,
        timeout=900,
    )
    # The step itself succeeds: it records the code rather than aborting, so the branch below works.
    assert completed.returncode == 0, completed.stdout + completed.stderr
    written = dict(line.split("=", 1) for line in outputs.read_text().splitlines() if "=" in line)
    assert written.get("exit-code") not in (None, "0"), f"a drifted tree reported {written.get('exit-code')!r}"


def test_the_report_step_writes_a_file_gh_can_read(workflow: dict, tmp_path: Path) -> None:
    """`gh issue create --body-file` needs a file, and a step that writes nothing is a silent skip."""
    step = next(s for s in _steps(workflow) if "Write the report" in s.get("name", ""))
    completed = subprocess.run(
        ["bash", "-c", _substitute(step["run"], {})],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = REPO / "drift.md"
    try:
        assert report.is_file(), "the report step wrote no drift.md"
        text = report.read_text(encoding="utf-8")
        assert "make sync" in text, "the report does not name the fix"
        assert "```" in text, "the report has no fenced block"
    finally:
        if report.exists():
            report.unlink()
