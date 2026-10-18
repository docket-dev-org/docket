"""Docket — every agent run gets a docket number.

Docket is a local-first supervisor and evidence layer for AI agents:

* **Bounded** — it enforces hard caps (USD, tokens, wall-clock, call counts) at the
  boundary and stops the process when a cap is crossed.
* **Legible** — it records model traffic, tool calls, filesystem changes, egress
  decisions and process lifecycle into an append-only, hash-chained ledger.
* **Provable** — it emits a *receipt* whose integrity can be checked offline by a
  third party who trusts neither the operator nor this tool, and whose cost totals
  can be recomputed from raw usage numbers.

The public API is intentionally small. Most users want the CLI (``docket run``);
library users want :func:`record`, :class:`Session` and :func:`verify_receipt`.
"""

from __future__ import annotations

import tomllib
from importlib.metadata import PackageNotFoundError, packages_distributions, version
from pathlib import Path

from .ledger import Event, Ledger
from .policy import Caps, Policy
from .receipt import build_receipt, load_receipt, write_receipt
from .sdk import Session, record_model_call, record_note, record_tool
from .verify import VerifyResult, verify_receipt

__all__ = [
    "Ledger",
    "Event",
    "Policy",
    "Caps",
    "build_receipt",
    "write_receipt",
    "load_receipt",
    "verify_receipt",
    "VerifyResult",
    "Session",
    "record_model_call",
    "record_note",
    "record_tool",
    "__version__",
]

# The version this code was built from. Read from the installed distribution so that a
# receipt's ``tool.version`` and ``docket --version`` describe what actually ran.
#
# A literal here would have to be edited by hand on every release, and it was: this used to say
# "0.1.0" while the distribution installed 0.1.1, so every receipt written by 0.1.1 carried a
# tool version of 0.1.0 and ``docket --version`` disagreed with ``pip show``. Nothing guarded
# the two files against each other, so the drift was invisible until someone compared them.
#
# Three sources, in order of authority:
#   1. the installed distribution, by asking which distribution provides the ``docket`` package.
#      The name is not written here, so a rename of the distribution cannot desynchronise it.
#   2. ``pyproject.toml`` in a source tree, which covers ``PYTHONPATH=src python -m docket``
#      and ``pip install .`` from a clone where the wheel is built first.
#   3. "unknown", which is honest: the code cannot tell. It never guesses a version.
_FALLBACK = "unknown"


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


__version__ = _distribution_version() or _source_version() or _FALLBACK
FORMAT_VERSION = "docket/receipt/v1"
