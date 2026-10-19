"""The version of this code, derived rather than written down.

Every receipt carries ``tool.version``, ``docket --version`` prints it, ``docket selftest`` and
``docket doctor`` head their output with it, and the supervisor puts it in a child's environment
as ``DOCKET_TOOL_VERSION``. That is five places a version can be *claimed*, and a receipt's claim
about the tool that wrote it is the one thing a receipt exists to make checkable.

This module used to be three literals — ``__init__.py``, ``receipt.py``'s ``TOOL_VERSION`` and
``supervisor.py``'s ``DOCKET_TOOL_VERSION`` — each of which had to be edited by hand on every
release. Two of them were still ``0.1.0`` when the distribution installed 0.1.2, so every receipt
written since 0.1.0 named a tool version that was not running. Nothing compared them, so the
drift was invisible until someone put ``pip show`` beside a receipt.

Three sources, in order of authority:

1. the installed distribution, by asking which distribution provides the ``docket`` package. The
   name is not written here, so renaming the distribution cannot desynchronise it again.
2. the nearest ``pyproject.toml`` above this file, which covers ``PYTHONPATH=src python -m docket``
   and ``pip install .`` from a clone, where the wheel is built from the same tree.
3. ``unknown``, which is honest: the code cannot tell, and it does not guess.

``FORMAT_VERSION`` lives here for the same reason. It is the receipt format tag, and a tag that
appears in two modules is a tag that can disagree with itself.
"""

from __future__ import annotations

import tomllib
from importlib.metadata import PackageNotFoundError, packages_distributions, version
from pathlib import Path

#: The receipt format tag. A receipt whose ``format`` is not this value is not a receipt this
#: module's verifier will accept, so the string is defined once and imported everywhere.
FORMAT_VERSION = "docket/receipt/v1"

#: Returned when the version cannot be determined. Never a guess: a receipt that says "unknown"
#: is a receipt that admits it does not know, which is the only honest option left.
UNKNOWN_VERSION = "unknown"


def _distribution_version() -> str | None:
    """The version of whichever installed distribution provides the ``docket`` package."""
    try:
        names = packages_distributions().get("docket") or []
    except Exception:  # pragma: no cover - a broken site-packages is not this process's problem
        return None
    for name in names:
        try:
            return version(name)
        except PackageNotFoundError:
            continue
    return None


def _source_version() -> str | None:
    """The version in the nearest ``pyproject.toml`` above this file, when running from a tree."""
    for directory in Path(__file__).resolve().parents:
        candidate = directory / "pyproject.toml"
        if not candidate.is_file():
            continue
        try:
            with candidate.open("rb") as handle:
                data = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError):
            return None
        project = data.get("project")
        if isinstance(project, dict):
            found = project.get("version")
            if isinstance(found, str):
                return found
        return None
    return None


def resolve_version() -> str:
    """The version of the code that is running, or ``UNKNOWN_VERSION`` if it cannot be told."""
    return _distribution_version() or _source_version() or UNKNOWN_VERSION


#: The version of the running code. Computed once, at import, because both the distribution
#: lookup and the ``pyproject.toml`` walk touch the filesystem.
__version__ = resolve_version()
