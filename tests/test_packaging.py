"""The installation paths the README promises, executed rather than asserted in prose.

`pip install docket-ai`, `pipx install docket-ai` and `uv tool install docket-ai` are three different
mechanisms with three different failure modes: an unpinned resolve, a missing console script, and a
binary that lands somewhere `PATH` does not look. A reader who follows the README meets whichever one
is broken, so each one is run here in a throwaway environment and the command it installs is executed.

The tests that need a tool which is not installed are skipped, and they say so, rather than
pretending to have checked something they could not reach.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _declared_version() -> str:
    """The version `pyproject.toml` declares, read from the tree rather than written here.

    This constant used to be a literal, which meant every release made these tests red and every
    release fixed a test instead of the tool. It is pinned in the sense that matters — it is the
    version this tree declares, so a pending upload cannot make these tests measure a release that
    is not published yet — and it follows the tree on its own.
    """
    document = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    project = document.get("project")
    assert isinstance(project, dict), "pyproject.toml has no [project] table"
    version = project.get("version")
    assert isinstance(version, str) and version, "pyproject.toml declares no version"
    return version


#: The version the README's install commands resolve to.
VERSION = _declared_version()


#: The environment variable a release gate sets when it is running against a version it just
#: uploaded. Everywhere else the default applies, because the wait is paid at import and a tree
#: declaring an unpublished version has nothing to wait for.
PROPAGATION_ENV = "DOCKET_PYPI_PROPAGATION_SECONDS"

#: How long to wait for an upload to reach the index before reporting it as not published.
#: Measured against this project's own uploads: 0.2.4 was resolvable by `uv` on the second poll,
#: about 15 s after the upload response, and the 0.2.3 release gate had already failed at a window
#: that the release bound covers many times over. The default is short on purpose — it is paid at
#: import, and a tree declaring a version nobody has uploaded should not spend three minutes
#: discovering that. Measured: 3m0.6s of import time before this was split.
PROPAGATION_TIMEOUT_SECONDS = float(os.environ.get(PROPAGATION_ENV, "15"))

#: The ceiling on install attempts, whatever the clock says. The bound that actually decides is
#: `PROPAGATION_TIMEOUT_SECONDS`, the same one the probe polls against; this exists so a clock that
#: does not advance cannot turn a bounded wait into an unbounded one. It is deliberately larger than
#: the short default needs, because the release gate raises the deadline to 300 s and the installer
#: has to be able to use it.
INSTALL_RETRIES = 25

#: The phrases `pip`, `pipx` and `uv` use when the index has no such version. Matched as a set so a
#: wording change in a tool is a visible failure rather than a silent loss of the retry.
_NO_SUCH_VERSION = (
    "No matching distribution found",
    "Could not find a version that satisfies",
    "there is no version of",
)


def _simple_index_serves(version: str) -> bool:
    """Whether PyPI's simple index serves this version.

    The simple index is the cache `pip`, `pipx` and `uv` resolve against. The JSON API is a
    different cache with a different lifetime, and asking it the installer's question is how the
    0.2.1 gate failed: the JSON API answered 200 for a freshly uploaded version while `pipx`
    still resolved "No matching distribution found", so the guard un-skipped a test that then
    failed on a delay that had nothing to do with the package.
    """
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        f"https://pypi.org/simple/docket-ai/?cb={version}",
        headers={
            "User-Agent": "docket-packaging-test",
            "Accept": "application/vnd.pypi.simple.v1+json, text/html;q=0.1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError):
        # No network is not a verdict either. The install is not exercised, and the reason says so.
        return False
    return version in body


def _published_on_pypi() -> bool:
    """Whether the version this tree declares has reached the index the installers read.

    These tests install the *published* distribution, so a tree whose version has been bumped and
    not yet uploaded has nothing to measure. That is reported as a skip with the reason, never as a
    pass: an install check that quietly did not run is the failure mode this project exists to
    prevent, and it is the difference between "could not check" and "works".

    This waits rather than deciding once, because no single probe can promise what the installer
    will see. Measured on 0.2.4: this guard read the index, found the version, un-skipped the
    clean-venv install, and CI's `pip` then resolved only up to the previous release — the probe
    and the installer had hit different edge caches minutes apart. Polling bounds the window; only
    a version that never appears at all is reported as not published.

    How long it waits depends on who is asking. The release workflow sets
    `DOCKET_PYPI_PROPAGATION_SECONDS`, because a release gate runs against a version that was
    uploaded moments earlier and the whole point is to give it time to arrive. Every other run uses
    the short default, because it pays this wait at import and a tree declaring an unpublished
    version is not waiting for anything.
    """
    deadline = time.monotonic() + PROPAGATION_TIMEOUT_SECONDS
    attempt = 0
    while True:
        if _simple_index_serves(VERSION):
            return True
        attempt += 1
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(5 * attempt, 15))


def _install(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    """Run an install, retrying while the index has not been told about the version yet.

    The distinction this preserves: a propagation-shaped failure is retried, because it is a fact
    about a cache and not about the package, and every other failure is returned on the first
    attempt, because retrying a real breakage only spends time before reporting the same thing.
    A package that genuinely cannot be installed still fails, after the last attempt, with the
    output of that attempt.

    The wait is bounded by `PROPAGATION_TIMEOUT_SECONDS`, the same bound `_published_on_pypi`
    polls against, with `INSTALL_RETRIES` as a ceiling above it. The two have to agree. The release
    gate sets the bound to 300 s so the probe will wait for the index; while the installer was given
    a fixed 5 s / 10 s / 15 s ladder instead, it was offered a tenth of the patience the gate had
    just paid for. Measured on the 0.2.7 tag gate: the simple index served 0.2.7, the probe returned
    True, the install was un-skipped, and `pip` inside `pipx` still resolved only up to 0.2.6
    through a different edge cache, so all four attempts failed and the gate went red. Retrying for
    30 s does not close a window that has already been given 300 s of cover.
    """
    deadline = time.monotonic() + PROPAGATION_TIMEOUT_SECONDS
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


def _looks_like_propagation(completed: subprocess.CompletedProcess[str]) -> bool:
    """Whether a failed install failed because the index has no such version.

    A package that cannot be installed and an index that has not been told about it yet produce the
    same exit code and different meanings. Only the second one is worth another attempt.
    """
    haystack = f"{completed.stdout}\n{completed.stderr}"
    return any(phrase in haystack for phrase in _NO_SUCH_VERSION)


_PUBLISHED = _published_on_pypi()
_SKIP_UNPUBLISHED = pytest.mark.skipif(
    not _PUBLISHED,
    reason=f"docket-ai=={VERSION} is not on PyPI yet; the install paths were not exercised",
)


def _run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=900, **kwargs)  # type: ignore[call-overload]


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed; `uv tool install` was not exercised")
@_SKIP_UNPUBLISHED
def test_uv_tool_install_produces_a_working_command(request: pytest.FixtureRequest) -> None:
    """`uv tool install` builds an isolated environment and installs one executable named `docket`."""
    # A throwaway HOME, so the isolated environment cannot collide with one already on this
    # machine, with the real PATH kept so `uv` itself is still findable. `TemporaryDirectory`
    # rather than `mkdtemp`, because `mkdtemp` with no removal leaks the environment for the life of
    # the machine: measured at 149 directories and 636 MB, with a 1 GB tmpfs at 100% turning an
    # unrelated fixture into `OSError: [Errno 28] No space left on device`.
    home = Path(tempfile.mkdtemp(prefix="docket-uv-"))
    request.addfinalizer(lambda: shutil.rmtree(home, ignore_errors=True))
    # `--refresh` because `uv` keeps its own index cache with a different lifetime from PyPI's, and
    # the guard above reads PyPI's. Without it the two disagree in the minutes after an upload: the
    # guard says the version is published and `uv` says there is no such version, so the test fails
    # on a cache rather than on the package. Measured on 0.2.2: the simple index served the file
    # immediately while `uv tool install` resolved "no version of docket-ai==0.2.2" until refreshed.
    completed = _install(
        ["uv", "tool", "install", "--python", sys.executable, "--refresh", f"docket-ai=={VERSION}"],
        env={**os.environ, "HOME": str(home)},
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    # `uv` writes its progress to stderr, so both streams are read rather than assuming which.
    assert f"docket-ai=={VERSION}" in completed.stdout + completed.stderr, "the resolved version was not the pinned one"

    # `uv tool run` does not depend on where the executable landed, which is the half of the install
    # the README warns about.
    ran = _install(["uv", "tool", "run", "--refresh", "--from", f"docket-ai=={VERSION}", "docket", "--version"])
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert f"docket {VERSION}" in ran.stdout, ran.stdout


@pytest.mark.skipif(shutil.which("pipx") is None, reason="pipx is not installed; `pipx install` was not exercised")
@_SKIP_UNPUBLISHED
def test_pipx_install_produces_a_working_command(request: pytest.FixtureRequest) -> None:
    """`pipx install` puts one executable named `docket` on a bin directory, and it works.

    The install is directed into a throwaway `PIPX_HOME` and `PIPX_BIN_DIR`, and the binary is
    taken from *that* directory rather than from `PATH`. The previous version called
    `shutil.which("docket")` after a successful install, which resolves the first `docket` on `PATH`
    — and on a machine that already has one, from `pip install .` or a system package, that is a
    different program from the one pipx just made. It failed on the release candidate for exactly
    that reason, having passed for it on every machine where no other `docket` existed. A test that
    can pass because of something other than what it is testing is worse than no test.
    """
    home = Path(tempfile.mkdtemp(prefix="docket-pipx-"))
    request.addfinalizer(lambda: shutil.rmtree(home, ignore_errors=True))
    pipx_home = home / "pipx"
    pipx_bin = home / "bin"
    pipx_bin.mkdir()
    env = {**os.environ, "PIPX_HOME": str(pipx_home), "PIPX_BIN_DIR": str(pipx_bin)}
    completed = _install(["pipx", "install", "--force", f"docket-ai=={VERSION}"], env=env)
    assert completed.returncode == 0, completed.stdout + completed.stderr

    binary = pipx_bin / "docket"
    assert binary.is_file(), (
        f"pipx reported success but wrote no {binary}; it wrote "
        f"{sorted(p.name for p in pipx_bin.iterdir()) or 'nothing'}"
    )
    ran = _run([str(binary), "--version"])
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert f"docket {VERSION}" in ran.stdout, f"the binary pipx installed reports {ran.stdout.strip()!r}, not {VERSION}"


@_SKIP_UNPUBLISHED
def test_a_clean_venv_install_runs_the_documented_journey(
    request: pytest.FixtureRequest,
) -> None:
    """`pip install` into a fresh venv, then the three commands the quick start shows.

    This is the path most readers take, and it is the one that catches an undeclared runtime
    dependency: a wheel that imports cleanly in the repository and fails in a venv is a wheel whose
    `install_requires` is lying.
    """
    venv_root = Path(tempfile.mkdtemp(prefix="docket-venv-"))
    request.addfinalizer(lambda: shutil.rmtree(venv_root, ignore_errors=True))
    venv = venv_root / "env"
    created = _run([sys.executable, "-m", "venv", str(venv)])
    assert created.returncode == 0, created.stdout + created.stderr

    python = venv / "bin" / "python"
    installed = _install([str(python), "-m", "pip", "install", "--quiet", "--no-cache-dir", f"docket-ai=={VERSION}"])
    assert installed.returncode == 0, installed.stdout + installed.stderr

    binary = venv / "bin" / "docket"
    assert binary.is_file(), "the wheel installed no console script"
    version = _run([str(binary), "--version"])
    assert version.returncode == 0, version.stdout + version.stderr
    assert f"docket {VERSION}" in version.stdout, version.stdout

    workspace = Path(tempfile.mkdtemp(prefix="docket-journey-"))
    request.addfinalizer(lambda: shutil.rmtree(workspace, ignore_errors=True))
    for command in (
        ["init", "--name", "journey", "--max-usd", "1"],
        ["run", "--policy", "docket.toml", "--", sys.executable, "-c", "print('hello')"],
        ["verify", ".docket/latest.receipt.json"],
    ):
        completed = _run([str(binary), *command], cwd=str(workspace))
        assert completed.returncode == 0, f"{' '.join(command)} failed:\n{completed.stdout}\n{completed.stderr}"
        if command[0] == "verify":
            assert "VERIFIED" in completed.stdout, completed.stdout


def test_the_installed_package_declares_no_runtime_dependencies() -> None:
    """A dependency-free package that needs a dependency is the failure this wheel is built to avoid."""
    document = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert document["project"].get("dependencies", []) == [], (
        "the package declares a runtime dependency; the README promises it needs none"
    )
    assert document["project"]["requires-python"] == ">=3.11"


def test_every_install_command_the_readme_shows_is_one_these_tests_cover() -> None:
    """A README line that installs the package must be a line something here executes.

    Otherwise the README grows an install path nobody has run, which is the failure mode this file
    exists to prevent.
    """
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    installs = [
        line.strip()
        for line in readme.splitlines()
        if line.strip().startswith(("pip install", "pipx install", "uv tool install"))
    ]
    assert installs, "the README shows no install command at all"
    for line in installs:
        assert "docket-ai" in line, f"the README installs something other than docket-ai: {line!r}"


def test_every_throwaway_environment_is_removed_when_the_test_ends() -> None:
    """A suite that leaks its scratch directories fills the disk it runs on, elsewhere.

    Measured: 149 directories named `docket-uv-*`, `docket-pipx-*`, `docket-venv-*` and
    `docket-journey-*` under `/tmp`, 636 MB, with a 1 GB tmpfs at 100% — which turned a full gate
    run into `OSError: [Errno 28] No space left on device` at *setup*, in `test_mutate.py`'s
    fixture, a long way from the tests that caused it.

    This is the guard nothing had. The leak is invisible in review, and its symptom lands in a
    different file, so the only thing that holds it closed is a check that counts.

    Read with `ast` rather than by counting strings, because the first version of this counted
    `mkdtemp` against `addfinalizer` across the whole file and reported 7 against 6 — the seventh
    being this test's own inline `rmtree`, which is a removal. A guard that fires on correct code
    gets switched off.
    """
    import ast

    tree = ast.parse((REPO / "tests" / "test_packaging.py").read_text(encoding="utf-8"))

    def calls(node: ast.AST, name: str) -> bool:
        return any(
            isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) and child.func.attr == name
            for child in ast.walk(node)
        )

    creators = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and calls(node, "mkdtemp"):
            removes = calls(node, "addfinalizer") or calls(node, "rmtree")
            creators.append((node.name, removes))

    assert creators, "the packaging tests no longer create throwaway environments"
    unremoved = [name for name, removes in creators if not removes]
    assert not unremoved, (
        f"these tests create a throwaway environment and never remove it, so it accumulates "
        f"until the disk fills and an unrelated test fails at setup: {unremoved}"
    )
    assert len(creators) >= 4, (
        f"only {len(creators)} tests create throwaway environments; the four install paths each "
        f"need one, and a missing creator means a path is no longer isolated"
    )


def test_the_throwaway_environments_really_are_removed() -> None:
    """Not a pattern match: create one the way the tests do, and check it is gone.

    The guard above reads the source. This one runs the pattern, so a finalizer that is registered
    but does nothing is caught here rather than by the disk filling.
    """
    import shutil as remove_tree
    import tempfile as temporary

    scratch = temporary.mkdtemp(prefix="docket-guard-")
    path = Path(scratch)
    assert path.is_dir()
    (path / "env" / "bin").mkdir(parents=True)
    (path / "env" / "bin" / "docket").write_text("#!/bin/sh\n", encoding="utf-8")

    remove_tree.rmtree(path, ignore_errors=True)
    assert not path.exists(), "the pattern the packaging tests use does not remove the directory"

    leftovers = sorted(entry.name for entry in Path(tempfile.gettempdir()).glob("docket-guard-*") if entry.is_dir())
    assert not leftovers, f"throwaway guard directories remain in the temp directory: {leftovers}"
