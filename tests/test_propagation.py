"""The propagation handling in tests/test_packaging.py, exercised rather than reasoned about.

Three questions, each of which the 0.2.1 and 0.2.4 release gates answered the wrong way:

  1. Does the guard read the cache the installers read, rather than a different one?
  2. Does a propagation-shaped failure get retried, and a real breakage not?
  3. Does a version that never appears get reported as not published, rather than waited on forever?

Run from the repository root.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests"))

import test_packaging as packaging  # noqa: E402 - the path is set up above


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["x"], returncode=returncode, stdout=stdout, stderr=stderr)


# --- 1. which cache the guard asks ------------------------------------------------------------


def test_the_guard_reads_the_simple_index_and_not_the_json_api() -> None:
    """The simple index is what `pip`, `pipx` and `uv` resolve against.

    The 0.2.1 gate failed because the guard asked the JSON API, which is a different cache with a
    different lifetime, and answered 200 for a version `pipx` still resolved as
    "No matching distribution found".
    """
    source = (REPO / "tests" / "test_packaging.py").read_text(encoding="utf-8")
    assert "pypi.org/simple/docket-ai/" in source, "the guard no longer reads the simple index"
    assert f"pypi.org/pypi/docket-ai/{packaging.VERSION}/json" not in source, (
        "the guard is reading the JSON API, which is a different cache from the one installers use"
    )


def test_the_guard_waits_rather_than_deciding_once() -> None:
    """One probe cannot promise what the installer will see.

    Measured on 0.2.4: the guard found the version on the index, un-skipped the clean-venv install,
    and CI's `pip` then resolved only up to the previous release.
    """
    source = (REPO / "tests" / "test_packaging.py").read_text(encoding="utf-8")
    assert "PROPAGATION_TIMEOUT_SECONDS" in source, "the guard has no propagation bound"
    assert "time.sleep(" in source.split("def _published_on_pypi")[1].split("def ")[0], (
        "the guard decides once rather than polling"
    )


# --- 2. which failures are retried ------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "ERROR: Could not find a version that satisfies the requirement docket-ai==9.9.9",
        "ERROR: No matching distribution found for docket-ai==9.9.9",
        "× No solution found when resolving dependencies\n  cause: Because there is no version of "
        "docket-ai==9.9.9 and you require docket-ai==9.9.9",
    ],
)
def test_a_propagation_shaped_failure_is_recognised(message: str) -> None:
    """Each of pip, pipx and uv has its own wording for the same fact, and all three are retried."""
    for stream in ("stdout", "stderr"):
        completed = _completed(1, **{stream: message})
        assert packaging._looks_like_propagation(completed), f"{stream} was not recognised: {message!r}"


@pytest.mark.parametrize(
    "message",
    [
        "ERROR: Invalid requirement: 'docket-ai=='",
        "error: subprocess-exited-with-error",
        "ERROR: Could not install packages due to an OSError: [Errno 28] No space left on device",
        "Requirement already satisfied: docket-ai",
        "HTTPError: 403 Forbidden",
    ],
)
def test_a_real_breakage_is_not_mistaken_for_propagation(message: str) -> None:
    """Retrying a genuine failure only spends time before reporting the same thing."""
    for stream in ("stdout", "stderr"):
        completed = _completed(1, **{stream: message})
        assert not packaging._looks_like_propagation(completed), f"{stream} was treated as propagation: {message!r}"


def test_a_successful_install_is_not_retried() -> None:
    assert not packaging._looks_like_propagation(_completed(0, stdout="Installed 1 executable: docket"))


def test_the_install_helper_returns_the_first_attempt_when_it_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        packaging,
        "_run",
        lambda command, **kwargs: calls.append(command) or _completed(0, stdout="ok"),
    )
    completed = packaging._install(["uv", "tool", "install", "x"])
    assert completed.returncode == 0
    assert len(calls) == 1, f"a successful install was attempted {len(calls)} times"


def test_the_install_helper_retries_a_propagation_failure_and_then_reports_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A version that never arrives still fails the test, after every attempt, with real output."""
    attempts: list[int] = []

    def always_fails(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        attempts.append(1)
        return _completed(1, stderr="ERROR: No matching distribution found for docket-ai==9.9.9")

    monkeypatch.setattr(packaging, "_run", always_fails)
    monkeypatch.setattr(packaging, "INSTALL_RETRIES", 3)
    monkeypatch.setattr(packaging.time, "sleep", lambda _seconds: None)

    completed = packaging._install(["pip", "install", "docket-ai==9.9.9"])
    assert completed.returncode == 1
    assert len(attempts) == packaging.INSTALL_RETRIES, (
        f"attempted {len(attempts)} times, expected {packaging.INSTALL_RETRIES}"
    )
    assert "No matching distribution" in completed.stderr, "the reported output is not the real one"


def test_the_install_helper_does_not_retry_a_real_breakage(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def fails(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(1)
        return _completed(1, stderr="error: subprocess-exited-with-error")

    monkeypatch.setattr(packaging, "_run", fails)
    completed = packaging._install(["pip", "install", "docket-ai"])
    assert completed.returncode == 1
    assert len(calls) == 1, f"a real breakage was retried {len(calls)} times"


def test_every_install_invocation_goes_through_the_retrying_helper() -> None:
    """A call site added later bypasses the retry, which is how this bug came back.

    The invariant is deliberately narrow: an invocation *of an installer* goes through `_install`.
    Running the binary an install just produced, to read its version, is not an install and has
    nothing to retry. The first version of this guard asserted that nothing calls `_run` at all,
    which was false, and the packaging tests that exercise those paths still passed because the
    assertion was about a list of lines rather than about the calls' arguments.
    """
    source = (REPO / "tests" / "test_packaging.py").read_text(encoding="utf-8")
    # One `_run(` or `_install(` call per line, in the four places the tests make them.
    calls = re.findall(r"^\s*(?:\w+) = (_run|_install)\(", source, re.M)
    assert calls, "no subprocess calls found; the pattern has stopped matching"

    install_verbs = ('"install"', '"tool", "install"', '"tool", "run"')
    direct_installs = []
    for line in source.split("\n"):
        match = re.match(r"^\s*\w+ = (_run|_install)\(", line)
        if not match:
            continue
        # A call may span lines, so the argv is read from this line and the ones that follow until
        # the closing bracket balances.
        window = [line]
        depth = line.count("[") - line.count("]")
        index = source.split("\n").index(line)
        lines = source.split("\n")
        while depth > 0 and index + 1 < len(lines):
            index += 1
            window.append(lines[index])
            depth += lines[index].count("[") - lines[index].count("]")
        argv = " ".join(window)
        if any(verb in argv for verb in install_verbs) and match.group(1) == "_run":
            direct_installs.append(line.strip())

    assert not direct_installs, (
        f"these installs bypass the retrying helper and will fail on a propagation delay: {direct_installs}"
    )


# --- 3. a version that never appears ----------------------------------------------------------


def test_a_version_that_is_not_published_is_reported_not_waited_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wait is bounded. A version that never appears becomes a skip with its reason."""
    monkeypatch.setattr(packaging, "_simple_index_serves", lambda version: False)
    monkeypatch.setattr(packaging, "PROPAGATION_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(packaging.time, "sleep", lambda _seconds: None)
    assert packaging._published_on_pypi() is False


def test_a_published_version_is_found_on_the_first_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(packaging, "_simple_index_serves", lambda version: seen.append(version) or True)
    assert packaging._published_on_pypi() is True
    assert seen == [packaging.VERSION], f"the guard probed {seen}, not the declared version"


def test_a_version_that_arrives_mid_wait_is_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wait has to be able to succeed, not merely to give up."""
    answers = iter([False, False, True])

    def arrives(version: str) -> bool:
        return next(answers)

    monkeypatch.setattr(packaging, "_simple_index_serves", arrives)
    monkeypatch.setattr(packaging.time, "sleep", lambda _seconds: None)
    assert packaging._published_on_pypi() is True


def test_the_real_index_is_reached_and_answers() -> None:
    """Not mocked: the guard's actual endpoint, asked for a version that certainly exists.

    A guard that cannot reach the index it reads is a guard that skips everything, so the endpoint
    is probed here against a release published long enough ago that propagation is not in question.
    """
    assert packaging._simple_index_serves("0.2.0") is True, (
        "the simple index did not serve 0.2.0; either the endpoint moved or there is no network"
    )


def test_the_real_index_does_not_answer_for_a_version_that_was_never_released() -> None:
    """The negative case, on the same endpoint: the guard must be able to say no."""
    assert packaging._simple_index_serves("99.99.99") is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "--tb=short"]))
