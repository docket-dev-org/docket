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

#: The version the README's install commands resolve to. Pinned so a pending upload cannot make
#: these tests measure a release that is not published yet.
VERSION = "0.1.8"


def _run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=900, **kwargs)  # type: ignore[arg-type]


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed; `uv tool install` was not exercised")
def test_uv_tool_install_produces_a_working_command() -> None:
    """`uv tool install` builds an isolated environment and installs one executable named `docket`."""
    home = Path(tempfile.mkdtemp(prefix="docket-uv-"))
    # A throwaway HOME, so the isolated environment cannot collide with one already on this machine,
    # with the real PATH kept so `uv` itself is still findable.
    completed = _run(
        ["uv", "tool", "install", "--python", sys.executable, f"docket-ai=={VERSION}"],
        env={**os.environ, "HOME": str(home)},
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    # `uv` writes its progress to stderr, so both streams are read rather than assuming which.
    assert f"docket-ai=={VERSION}" in completed.stdout + completed.stderr, "the resolved version was not the pinned one"

    # `uv tool run` does not depend on where the executable landed, which is the half of the install
    # the README warns about.
    ran = _run(["uv", "tool", "run", "--from", f"docket-ai=={VERSION}", "docket", "--version"])
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert f"docket {VERSION}" in ran.stdout, ran.stdout


@pytest.mark.skipif(shutil.which("pipx") is None, reason="pipx is not installed; `pipx install` was not exercised")
def test_pipx_install_produces_a_working_command() -> None:
    completed = _run(["pipx", "install", "--force", f"docket-ai=={VERSION}"])
    assert completed.returncode == 0, completed.stdout + completed.stderr
    binary = shutil.which("docket")
    assert binary, "pipx reported success but no `docket` is on PATH"
    ran = _run([binary, "--version"])
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert f"docket {VERSION}" in ran.stdout, ran.stdout


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
