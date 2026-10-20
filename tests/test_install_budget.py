"""Three guards for the installer's retry bound, added after the 0.2.7 tag gate went red.

The defect, measured on run 39495258366 step "Test the tag before shipping it":

  - the release workflow sets `DOCKET_PYPI_PROPAGATION_SECONDS=300`
  - `_published_on_pypi()` polls for up to that long, found 0.2.7, returned True
  - that un-skipped `test_pipx_install_produces_a_working_command`
  - `_install` then offered `pip` a fixed 5 s / 10 s / 15 s ladder -- 30 s, a tenth of the cover
  - `pip` inside `pipx` resolved against a different edge cache and reported versions only up to
    0.2.6, so every attempt failed and the gate went red

The probe and the installer can disagree; that was already measured and recorded on 0.2.4. What was
missing is that the bound has to cover the installer as well as the probe.

Each guard below is proven by mutation before it is trusted. See `docs/testing.md`.
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SOURCE = (REPO / "tests" / "test_packaging.py").read_text(encoding="utf-8")


def test_the_installer_is_bounded_by_the_propagation_deadline() -> None:
    """The installer reads the same bound the probe polls against.

    Read from the source rather than simulated, because the thing that went wrong was invisible at
    run time: the ladder ran, the attempts failed, and nothing in the output said the installer had
    been given a tenth of the time the gate had just spent. A structural check is what notices it.
    """
    body = SOURCE.split("def _install(")[1].split("def _looks_like_propagation(")[0]
    assert "PROPAGATION_TIMEOUT_SECONDS" in body, (
        "the installer's retry does not consult the propagation deadline, so the release gate's "
        "300 s bound covers the probe and leaves the installer on its own fixed ladder"
    )
    assert "time.monotonic() < deadline" in body, (
        "the installer's retry loop is not bounded by the deadline it computes"
    )


def test_the_installer_stops_when_the_deadline_passes_rather_than_at_a_fixed_count(
    monkeypatch,
) -> None:
    """With a fake clock, the retry stops because time ran out, not because a counter did.

    The old ladder stopped after a fixed number of attempts whatever the bound said. This drives
    `_install` with a clock that advances by the sleep it is asked for, so the deadline is the thing
    that ends the loop, and asserts the count is larger than the old fixed 4 -- otherwise the new
    bound has silently become the old ceiling with extra words.
    """
    import sys

    sys.path.insert(0, str(REPO / "tests"))
    import test_packaging as packaging

    attempts: list[int] = []
    clock = {"now": 1000.0}

    def fails(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        attempts.append(1)
        return subprocess.CompletedProcess(
            args=["x"], returncode=1, stderr="ERROR: No matching distribution found for docket-ai"
        )

    def advance(seconds: float) -> None:
        clock["now"] += seconds

    monkeypatch.setattr(packaging, "_run", fails)
    monkeypatch.setattr(packaging.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(packaging.time, "sleep", advance)
    monkeypatch.setattr(packaging, "PROPAGATION_TIMEOUT_SECONDS", 60.0)

    completed = packaging._install(["pip", "install", "docket-ai"])

    assert completed.returncode == 1, "a propagation failure must still fail the caller"
    assert len(attempts) > 4, (
        f"attempted {len(attempts)} times; the old fixed ladder gave 4, so the deadline is not what is ending this loop"
    )
    # 60 s of cover, sleeps of 5, 10, then 15 each: it must stop having spent at most that.
    spent = clock["now"] - 1000.0
    assert spent <= 60.0 + 15.0, (
        f"the loop slept {spent:.0f} s against a 60 s deadline, so the deadline is not bounding it"
    )


def test_the_release_gate_bound_reaches_the_installer() -> None:
    """The release gate's bound has to be raised, and the installer has to be able to use all of it.

    Read from the workflow file rather than restated here, because a bound raised in one place and
    not honoured in the other is exactly how the 0.2.7 gate failed.

    The first version of this asserted only that the *name* appears in the workflow, and mutation
    showed why that is not a guard: setting the value to `"15"`, which is the default and therefore
    grants a release gate no extra cover at all, passed. A guard that passes for a reason other than
    the one it exists for is worse than no guard, and the defect under test was a bound set in the
    right place that never reached the code needing it -- which is what a low value is, one level up.
    """
    workflow = (REPO / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    match = re.search(r'DOCKET_PYPI_PROPAGATION_SECONDS:\s*"(\d+(?:\.\d+)?)"', workflow)
    assert match, "the release gate no longer raises the propagation bound at all"
    bound = float(match.group(1))

    default = float(
        re.search(r'PROPAGATION_TIMEOUT_SECONDS = float\(os\.environ\.get\(PROPAGATION_ENV, "(\d+)"\)\)', SOURCE).group(
            1
        )
    )
    assert bound > default, (
        f"the release gate sets the bound to {bound} s, which is the {default} s default, so a "
        "release gate gets no more cover than any other run"
    )

    ceiling = int(re.search(r"^INSTALL_RETRIES = (\d+)$", SOURCE, re.M).group(1))
    # 5 + 10 + 15*(n-2) seconds of sleep must be able to exceed the bound the gate sets, otherwise
    # the attempt ceiling binds before the deadline and the installer is capped again by the back
    # door -- the same defect wearing a different hat.
    cover = 5 + 10 + 15 * max(ceiling - 2, 0)
    assert cover >= bound, (
        f"INSTALL_RETRIES={ceiling} gives at most {cover} s of retrying against a {bound} s bound, "
        "so the deadline never becomes the binding constraint"
    )


def test_a_real_breakage_is_still_returned_on_the_first_attempt(monkeypatch) -> None:
    """The wider bound must not widen what gets retried.

    A longer budget on a failure that is not propagation-shaped would mean a genuinely broken
    install spends the whole gate failing slowly, which is worse than failing fast.
    """
    import sys

    sys.path.insert(0, str(REPO / "tests"))
    import test_packaging as packaging

    calls: list[int] = []

    def breaks(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(1)
        return subprocess.CompletedProcess(args=["x"], returncode=1, stderr="error: subprocess-exited-with-error")

    monkeypatch.setattr(packaging, "_run", breaks)
    monkeypatch.setattr(packaging, "PROPAGATION_TIMEOUT_SECONDS", 300.0)

    completed = packaging._install(["pip", "install", "docket-ai"])

    assert completed.returncode == 1
    assert len(calls) == 1, (
        f"a real breakage was attempted {len(calls)} times against a 300 s bound; the wider "
        "budget must apply to propagation-shaped failures only"
    )


def test_the_deadline_is_read_at_call_time_not_at_import(monkeypatch) -> None:
    """`PROPAGATION_TIMEOUT_SECONDS` is read when the install runs, not captured earlier.

    Read with `ast` rather than by splitting on punctuation: the signature ends `]:`, not `):`, and
    the docstring contains parentheses of its own, so a text split reaches into the prose and
    reports the thing it is looking for. Reading the tree asks the question directly.

    Reading it once at import would freeze the short default into every process, including the
    release gate, which is the same class of mistake as the ladder: the value is set in the right
    place and never reaches the code that needs it.
    """
    import ast
    import sys

    sys.path.insert(0, str(REPO / "tests"))
    import test_packaging as packaging

    tree = ast.parse(SOURCE)
    function = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_install")

    # No default argument or annotation may mention the bound, or it binds at import.
    captured = [
        ast.unparse(node)
        for node in [*function.args.defaults, *function.args.kw_defaults]
        if node is not None and "PROPAGATION_TIMEOUT_SECONDS" in ast.unparse(node)
    ]
    assert not captured, f"the bound is a default argument ({captured}), which binds it at import time"

    # And the body must read the module constant, not a copy taken at import.
    reads = [
        ast.unparse(node)
        for node in ast.walk(function)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "deadline" for target in node.targets)
    ]
    assert len(reads) == 1, f"expected one `deadline` assignment, found {reads}"
    assert "PROPAGATION_TIMEOUT_SECONDS" in reads[0], (
        f"the deadline is computed as {reads[0]!r}, not from the module constant"
    )

    # Behaviourally: raising the constant after import must change what the helper uses.
    monkeypatch.setattr(packaging, "_run", lambda command, **kwargs: _ok())
    monkeypatch.setattr(packaging, "PROPAGATION_TIMEOUT_SECONDS", 300.0)
    assert packaging.PROPAGATION_TIMEOUT_SECONDS == 300.0, "the constant is not settable at runtime"


def _ok() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["x"], returncode=0, stdout="ok")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "--tb=short"]))
