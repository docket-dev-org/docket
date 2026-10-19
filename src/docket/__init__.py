"""
Docket — every agent run gets a docket number.

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

from ._version import FORMAT_VERSION as FORMAT_VERSION
from ._version import __version__ as __version__
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
    "FORMAT_VERSION",
]
