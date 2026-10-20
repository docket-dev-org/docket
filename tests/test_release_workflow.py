"""The release gate, held to the same standard as the push gate.

The Release workflow is the last thing that runs before a tag becomes a release, and it was red for
every tag from the moment the action tests landed. It installed test dependencies and ran the whole
suite, but it never installed the package under test, so `tests/test_action.py` and
`tests/test_packaging.py` failed with `docket: command not found` — exit 127, which is not a verdict
about the tag and reads like one.

That is the same omission the CI `tests` job had, fixed there in one commit and missed here. The
general shape of the bug is two jobs that run the same suite with different environments, and nothing
holding them together: the freshness workflow opened a spurious issue for exactly that reason, which
is why `tests/test_freshness_workflow.py` now pins the two dependency lists against each other. This
file does the same for the release gate, and adds the parts of a release that are not just "run the
tests": that the gate is the whole suite, that it prints its counts, that a tag with no notes is
refused, and that a PyPI token never becomes a repository secret.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML is not installed; the workflow documents cannot be parsed")

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github" / "workflows"

RELEASE = WORKFLOWS / "release.yml"
CI = WORKFLOWS / "ci.yml"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _steps(document: dict, job: str) -> list[dict]:
    return [step for step in document["jobs"][job]["steps"] if isinstance(step, dict)]


def _runs(document: dict, job: str) -> list[str]:
    return [str(step.get("run") or "") for step in _steps(document, job)]


def _suite_runs(document: dict, job: str) -> list[str]:
    """Only the steps that *invoke* pytest, not the ones that merely name it.

    `pip install build pytest cryptography pyyaml` contains the string "pytest". Matching on that
    alone finds the install step and calls it the test step, which is how the first version of this
    file asserted an ordering that was already correct and called it broken.
    """
    return [run for run in _runs(document, job) if _is_suite_run(run)]


def _is_suite_run(run: str) -> bool:
    """Whether a `run:` block *invokes* pytest rather than merely naming it.

    `pip install build pytest cryptography pyyaml` contains the word. Anchoring on the command
    position — after any `VAR=value` prefixes, at the start of a line — is what separates the step
    that runs the suite from the step that installs it. The first version of this predicate matched
    on the word and called the install step the test step.
    """
    for line in run.splitlines():
        command = re.sub(r"^\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*", "", line).strip()
        if re.match(r"^(python3?\s+-m\s+)?pytest\b", command):
            return True
    return False


# --- the gate must be able to run the suite at all ---------------------------------------------- #


def test_the_release_job_installs_the_package_under_test() -> None:
    """`tests/test_action.py` and `tests/test_packaging.py` call the `docket` console script.

    Without the install they fail with exit 127, which is "command not found" and not a verdict
    about the tag. This is the assertion whose absence let the gate go red unnoticed.
    """
    document = _load(RELEASE)
    installs = [run for run in _runs(document, "release") if "pip install ." in run]
    assert installs, "the release job never installs the package under test"


def test_the_package_is_installed_before_the_suite_runs() -> None:
    """Order, not just presence: an install after the tests changes nothing."""
    steps = _steps(_load(RELEASE), "release")
    runs = [(index, str(step.get("run") or "")) for index, step in enumerate(steps)]
    install_at = next((i for i, run in runs if "pip install ." in run), None)
    test_at = next((i for i, run in runs if _is_suite_run(run)), None)
    assert install_at is not None, "the release job never installs the package under test"
    assert test_at is not None, "the release job never runs the suite"
    assert install_at < test_at, "the package is installed after the suite runs"


def test_the_release_job_installs_what_the_ci_tests_job_installs() -> None:
    """Two jobs, one suite: a smaller environment collects a smaller suite and reports drift.

    The same class as the freshness defect, and the same fix. `uv` is excluded for the reason
    recorded in `tests/test_freshness_workflow.py`: `tests/test_packaging.py` skips itself when it
    is absent, so a missing `uv` changes no collected count.
    """
    release = _load(RELEASE)
    ci = _load(CI)

    def packages(document: dict, job: str) -> set[str]:
        for step in _steps(document, job):
            run = str(step.get("run") or "")
            if "pip install" in run and "pytest" in run:
                return set(re.findall(r"[a-z0-9_.-]+", run.split("pip install", 1)[1]))
        pytest.fail(f"no pip install step found in the {job!r} job")

    expected = packages(ci, "test") - {"uv"}
    assert expected, "the CI tests job installs nothing this can compare against"
    assert packages(release, "release") >= expected, (
        f"the release job is missing {sorted(expected - packages(release, 'release'))}; a "
        "smaller environment collects a smaller suite, so the gate would measure a different tree"
    )


# --- the gate must be the whole suite, and must say so ------------------------------------------ #


def test_the_release_gate_runs_the_whole_suite_directory() -> None:
    """A gate that runs a subset is a gate about a subset.

    The target is matched as a whole word, because `pytest tests/test_receipt_verify.py` contains
    the substring "tests" and the first version of this assertion accepted it. That is a guard that
    passes for the wrong reason, which is worse than no guard: it reads as coverage while letting
    the defect through, and it is the same shape as the `shutil.which` bug in the packaging tests.
    """
    runs = _suite_runs(_load(RELEASE), "release")
    assert runs, "the release job never runs the suite"
    targets = [
        token
        for run in runs
        for token in run.split()
        if not token.startswith("-") and token not in {"pytest", "python", "python3", "-m", "src"}
    ]
    assert "tests" in targets, (
        f"the release gate runs {targets or 'nothing'}, not the tests directory; a gate over a "
        "subset is a gate about a subset"
    )


def test_the_release_gate_does_not_ask_for_a_quieter_run_than_the_line_the_docs_quote() -> None:
    """`addopts` already carries `-q`; a second one is `-qq`, which prints no counts line.

    The release log is the evidence that the tag was tested, so a run that prints no counts is a
    log with nothing in it.
    """
    for run in _suite_runs(_load(RELEASE), "release"):
        assert "-qq" not in run, run
        assert "-ra" in run or "--tb" in run or "-v" in run, f"neither counts nor detail: {run}"


# --- the parts of a release that are not just running the tests --------------------------------- #


def test_a_tag_with_no_notes_is_refused_rather_than_published() -> None:
    """A release with no notes is worse than a failed run: the failure is visible and it is not."""
    document = _load(RELEASE)
    extract = next((run for run in _runs(document, "release") if "CHANGELOG.md" in run and ">" in run), "")
    assert extract, "no step extracts the release notes from CHANGELOG.md"
    assert "exit 1" in extract, "an empty notes file must fail the job"
    assert "RELEASE_NOTES.md" in extract


def test_the_pypi_upload_stays_out_of_the_workflow() -> None:
    """A PyPI token can publish under the project's name forever and cannot be scoped to a branch.

    Storing one as a repository secret hands it to everyone who ever gains admin on the repository.
    The workflow publishes the GitHub release, because `github.token` is scoped to one run and
    expires with it; the PyPI step is deliberate and manual, and the workflow says so in a comment
    that this test holds in place.
    """
    document = _load(RELEASE)
    for step in _steps(document, "release"):
        run = str(step.get("run") or "")
        assert "twine" not in run, f"the release workflow must not upload to PyPI: {run[:120]}"
        env = step.get("env") or {}
        for key in env:
            assert "PYPI" not in key.upper(), f"a PyPI credential appears as {key!r} in env"
    # The prose that explains *why* it stays manual is what makes the policy reviewable, and it
    # names `twine` in a comment. It is read here, from the comment lines only, so the assertion
    # above and this one cannot contradict each other.
    comments = "\n".join(
        line for line in RELEASE.read_text(encoding="utf-8").splitlines() if line.lstrip().startswith("#")
    )
    assert "manual" in comments.lower(), "the workflow must state in prose that the PyPI step is manual"


def test_the_github_release_uses_the_scoped_token_and_not_a_secret() -> None:
    """`github.token` is scoped to the run; anything else is a credential with a longer life."""
    document = _load(RELEASE)
    for step in _steps(document, "release"):
        env = step.get("env") or {}
        if "GH_TOKEN" in env:
            assert env["GH_TOKEN"] == "${{ github.token }}", env["GH_TOKEN"]


def test_the_release_workflow_is_triggered_by_a_tag() -> None:
    """A release workflow that fires on every push is a release workflow firing on every push."""
    document = _load(RELEASE)
    triggers = document.get(True, document.get("on", {}))
    push = triggers.get("push") or {}
    assert push.get("tags"), f"no tag trigger: {trgers_key(triggers)}"


def trgers_key(triggers: dict) -> list[str]:
    return sorted(str(key) for key in triggers)


def test_the_release_version_comes_from_pyproject_and_not_from_the_ref_name() -> None:
    """`pyproject.toml` is authoritative; the ref name is a label that can drift from it.

    Measured on v0.2.3: the workflow derived the version from `${GITHUB_REF_NAME#v}`, so a
    `workflow_dispatch` run on a branch resolved `main`, looked for `## [main]` in the changelog,
    and failed with "CHANGELOG.md has no section for main" whatever the maintainer did. The
    version is now read out of `pyproject.toml`, so a manual run publishes the version the
    package declares.
    """

    document = _load(RELEASE)
    resolve = [step for step in _steps(document, "release") if "Resolve this version" in str(step.get("name", ""))]
    assert len(resolve) == 1, f"expected one resolve step, found {len(resolve)}"
    body = resolve[0].get("run", "")
    assert "pyproject.toml" in body, body
    assert "GITHUB_ENV" in body, body
    for step in _steps(document, "release"):
        run = step.get("run")
        if isinstance(run, str):
            assert "GITHUB_REF_NAME#v" not in run, (
                f"{step.get('name')} still derives a version from the ref name, which is "
                "unusable under workflow_dispatch: {run!r}"
            )


def test_a_tag_that_disagrees_with_the_declared_version_is_refused() -> None:
    """A tag naming a different version than the package must fail before anything ships.

    This is the defect that produced the 0.2.0 provenance mismatch: the tag named a later commit
    than the artifacts were built from, and nothing in the pipeline checked the two against each
    other. The pipeline cannot see the commit, but it can see the version, and the version is
    the half the release body is built from.
    """

    document = _load(RELEASE)
    resolve = [step for step in _steps(document, "release") if "Resolve this version" in str(step.get("name", ""))]
    assert len(resolve) == 1, "no resolve step"
    body = resolve[0].get("run", "")
    assert "${GITHUB_REF_NAME}" in body and "refusing to publish" in body, body
    assert "exit 1" in body, f"the disagreement is reported but not fatal: {body!r}"


def test_the_release_publication_recovers_from_a_release_that_already_exists() -> None:
    """A re-run must not require deleting the release to get past publication.

    Measured on v0.2.3: the release was cut by hand, then the tag's workflow run reached
    "Publish the GitHub release" and failed, because `gh release create` exits non-zero when the
    release exists. The gate had passed and the artifacts were good; only the publication step
    was stuck, and the documented way out was to delete the release.
    """

    document = _load(RELEASE)
    publish = [
        step for step in _steps(document, "release") if "Publish the GitHub release" in str(step.get("name", ""))
    ]
    assert len(publish) == 1, f"expected one publish step, found {len(publish)}"
    body = publish[0].get("run", "")
    assert "gh release view" in body, f"no existence check, so a re-run cannot recover: {body!r}"
    assert "gh release upload" in body, f"no upload path for an existing release: {body!r}"
    assert "--clobber" in body, f"a re-run must replace the artifacts, not add to them: {body!r}"
    assert "gh release create" in body, f"the first-run path is gone: {body!r}"


if __name__ == "__main__":  # pragma: no cover - `python3 tests/test_release_workflow.py`
    import sys

    sys.exit(pytest.main([__file__, "-q"]))
