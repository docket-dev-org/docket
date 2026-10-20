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


def _published_on_pypi() -> bool:
    """Whether the version this tree declares is downloadable from PyPI.

    These tests install the *published* distribution, so a tree whose version has been bumped and
    not yet uploaded has nothing to measure. That is reported as a skip with the reason, never as a
    pass: an install check that quietly did not run is the failure mode this project exists to
    prevent, and it is the difference between "could not check" and "works".
    """
    import json
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        f"https://pypi.org/pypi/docket-ai/{VERSION}/json",
        headers={"User-Agent": "docket-packaging-test"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            document = json.load(response)
    except (urllib.error.URLError, TimeoutError, OSError):
        # No network is not a verdict either. The install is not exercised, and the reason says so.
        return False
    return isinstance(document, dict) and bool(document.get("urls"))


_PUBLISHED = _published_on_pypi()
_SKIP_UNPUBLISHED = pytest.mark.skipif(
    not _PUBLISHED,
    reason=f"docket-ai=={VERSION} is not on PyPI yet; the install paths were not exercised",
)


def _run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=900, **kwargs)  # type: ignore[arg-type]


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed; `uv tool install` was not exercised")
@_SKIP_UNPUBLISHED
def test_uv_tool_install_produces_a_working_command() -> None:
    """`uv tool install` builds an isolated environment and installs one executable named `docket`."""
    home = Path(tempfile.mkdtemp(prefix="docket-uv-"))
    # A throwaway HOME, so the isolated environment cannot collide with one already on this machine,
    # with the real PATH kept so `uv` itself is still findable.
    # `--refresh` because `uv` keeps its own index cache with a different lifetime from PyPI's, and
    # the guard above reads PyPI's. Without it the two disagree in the minutes after an upload: the
    # guard says the version is published and `uv` says there is no such version, so the test fails
    # on a cache rather than on the package. Measured on 0.2.2: the simple index served the file
    # immediately while `uv tool install` resolved "no version of docket-ai==0.2.2" until refreshed.
    completed = _run(
        ["uv", "tool", "install", "--python", sys.executable, "--refresh", f"docket-ai=={VERSION}"],
        env={**os.environ, "HOME": str(home)},
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    # `uv` writes its progress to stderr, so both streams are read rather than assuming which.
    assert f"docket-ai=={VERSION}" in completed.stdout + completed.stderr, "the resolved version was not the pinned one"

    # `uv tool run` does not depend on where the executable landed, which is the half of the install
    # the README warns about.
    ran = _run(["uv", "tool", "run", "--refresh", "--from", f"docket-ai=={VERSION}", "docket", "--version"])
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert f"docket {VERSION}" in ran.stdout, ran.stdout


@pytest.mark.skipif(shutil.which("pipx") is None, reason="pipx is not installed; `pipx install` was not exercised")
@_SKIP_UNPUBLISHED
def test_pipx_install_produces_a_working_command() -> None:
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
    pipx_home = home / "pipx"
    pipx_bin = home / "bin"
    pipx_bin.mkdir()
    env = {**os.environ, "PIPX_HOME": str(pipx_home), "PIPX_BIN_DIR": str(pipx_bin)}
    completed = _run(["pipx", "install", "--force", f"docket-ai=={VERSION}"], env=env)
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
def test_a_clean_venv_install_runs_the_documented_journey() -> None:
    """`pip install` into a fresh venv, then the three commands the quick start shows.

    This is the path most readers take, and it is the one that catches an undeclared runtime
    dependency: a wheel that imports cleanly in the repository and fails in a venv is a wheel whose
    `install_requires` is lying.
    """
    venv = Path(tempfile.mkdtemp(prefix="docket-venv-")) / "env"
    created = _run([sys.executable, "-m", "venv", str(venv)])
    assert created.returncode == 0, created.stdout + created.stderr

    python = venv / "bin" / "python"
    installed = _run([str(python), "-m", "pip", "install", "--quiet", "--no-cache-dir", f"docket-ai=={VERSION}"])
    assert installed.returncode == 0, installed.stdout + installed.stderr

    binary = venv / "bin" / "docket"
    assert binary.is_file(), "the wheel installed no console script"
    version = _run([str(binary), "--version"])
    assert version.returncode == 0, version.stdout + version.stderr
    assert f"docket {VERSION}" in version.stdout, version.stdout

    workspace = Path(tempfile.mkdtemp(prefix="docket-journey-"))
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
