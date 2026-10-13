"""Canonical serialisation and digest primitives.

Everything in Docket that is hashed is first canonicalised. The canonical form is
defined normatively in ``spec/docket-receipt-1.md`` so that independent
implementations (see ``verify-js/verify.mjs``) can reproduce every digest byte for
byte:

1. Objects: keys sorted by Unicode code point, no insignificant whitespace.
2. Strings: UTF-8, JSON-escaped, non-ASCII emitted literally (``ensure_ascii=False``).
3. Numbers: integers only. **Monetary amounts are decimal strings, never floats.**
4. Booleans and null are literal.
5. Arrays preserve order.

Rule 3 exists because IEEE-754 formatting differs between language runtimes; a
receipt that cannot be re-verified in another language is not a receipt.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

__all__ = [
    "canonical_bytes",
    "canonical_json",
    "sha256_hex",
    "merkle_root",
    "merkle_proof",
    "verify_merkle_proof",
    "usd",
    "MICROS",
]

MICROS = Decimal("0.000001")
_USD_RE = re.compile(r"^-?\d+(\.\d{1,6})?$")


def _normalise(obj: Any) -> Any:
    """Rewrite numbers so that every runtime encodes them identically.

    Integers stay integers. A float with an integral value becomes an integer
    (``5.0 -> 5``). Any other float is **rejected**: JavaScript would print
    ``0.8``, Python ``0.8``, but their shortest-repr rules diverge on values like
    ``1e-07``, and a receipt that verifies in one language and not another is not a
    receipt. Non-integer quantities must be encoded as decimal strings (see §3.3).
    """
    if isinstance(obj, bool) or obj is None or isinstance(obj, str):
        return obj
    if isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        if obj.is_integer():
            return int(obj)
        raise TypeError(
            f"non-integer number {obj!r} cannot appear in a hashed structure; "
            "encode it as a decimal string (see spec/docket-receipt-1.md §3.3)"
        )
    if isinstance(obj, dict):
        return {str(k): _normalise(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_normalise(v) for v in obj]
    return obj


def canonical_bytes(obj: Any) -> bytes:
    """Return the canonical UTF-8 encoding of ``obj``."""
    try:
        text = json.dumps(_normalise(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"object is not canonically serialisable: {exc}") from exc
    return text.encode("utf-8")


def canonical_json(obj: Any) -> str:
    """Return the canonical JSON text of ``obj``."""
    return canonical_bytes(obj).decode("utf-8")


def num(value: int | float | Decimal | str) -> int | str:
    """Encode a number for a hashed structure: integers stay integers, fractions
    become decimal strings. This is the only sanctioned way to put a number into a
    ledger event (see §3.3 of the spec)."""
    if isinstance(value, str):
        return value
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return format(value.normalize(), "f")
    integer = int(value) if isinstance(value, float) and float(value).is_integer() else None
    if integer is not None:
        return integer
    if isinstance(value, int):
        return value
    return format(value, "f").rstrip("0").rstrip(".")


def sha256_hex(data: bytes | str) -> str:
    """SHA-256 of ``data`` as lowercase hex."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def digest_of(obj: Any) -> str:
    """SHA-256 of the canonical encoding of ``obj``."""
    return sha256_hex(canonical_bytes(obj))


# --------------------------------------------------------------------------- #
# Merkle tree (RFC 6962 style: 0x00 leaf prefix, 0x01 node prefix)
# --------------------------------------------------------------------------- #

_LEAF_PREFIX = b"\x00"
_NODE_PREFIX = b"\x01"


def _leaf_hash(hexdigest: str) -> bytes:
    return hashlib.sha256(_LEAF_PREFIX + bytes.fromhex(hexdigest)).digest()


def _node_hash(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(_NODE_PREFIX + left + right).digest()


def merkle_root(hexdigests: Iterable[str]) -> str:
    """RFC 6962 Merkle tree hash over an ordered list of hex digests.

    An empty list hashes to the SHA-256 of the empty string. An odd node is
    promoted unchanged to the next level (this is RFC 6962's rule, and it is
    different from Bitcoin's duplicate-last-node rule).
    """
    level = [_leaf_hash(h) for h in hexdigests]
    if not level:
        return sha256_hex(b"")
    while len(level) > 1:
        nxt: list[bytes] = []
        for i in range(0, len(level) - 1, 2):
            nxt.append(_node_hash(level[i], level[i + 1]))
        if len(level) % 2:
            nxt.append(level[-1])
        level = nxt
    return level[0].hex()


def merkle_proof(hexdigests: list[str], index: int) -> list[dict[str, str]]:
    """Inclusion proof for the leaf at ``index``.

    Returns the audit path as a list of ``{"side": "left"|"right", "hash": hex}``
    entries, ordered from leaf to root.
    """
    if not 0 <= index < len(hexdigests):
        raise IndexError("leaf index out of range")
    level = [_leaf_hash(h) for h in hexdigests]
    idx = index
    path: list[dict[str, str]] = []
    while len(level) > 1:
        nxt: list[bytes] = []
        for i in range(0, len(level) - 1, 2):
            left, right = level[i], level[i + 1]
            if i == idx - (idx % 2):
                if idx % 2 == 0:
                    path.append({"side": "right", "hash": right.hex()})
                else:
                    path.append({"side": "left", "hash": left.hex()})
            nxt.append(_node_hash(left, right))
        if len(level) % 2:
            nxt.append(level[-1])
        idx //= 2
        level = nxt
    return path


def verify_merkle_proof(hexdigest: str, path: list[dict[str, str]], root: str) -> bool:
    """Verify an inclusion proof produced by :func:`merkle_proof`."""
    node = _leaf_hash(hexdigest)
    for step in path:
        sibling = bytes.fromhex(step["hash"])
        node = _node_hash(node, sibling) if step["side"] == "right" else _node_hash(sibling, node)
    return node.hex() == root.lower()


# --------------------------------------------------------------------------- #
# Money
# --------------------------------------------------------------------------- #


def usd(value: Decimal | str | int | float) -> str:
    """Render a monetary amount as the canonical decimal string.

    Amounts are rounded half-up to 6 decimal places (micro-dollars) and always
    carry at least two decimals, so that ``usd(0)`` is ``"0.00"`` and
    ``usd("1.5")`` is ``"1.50"``.
    """
    if isinstance(value, float):  # tolerate floats at the edges of the system only
        value = Decimal(repr(value))
    dec = Decimal(value).quantize(MICROS)
    # Two canonical shapes only: whole cents are written with 2 decimals, anything
    # finer is written with exactly 6. This keeps verification unambiguous in every
    # language runtime (see spec/docket-receipt-1.md §3.3).
    if (dec * 100) == (dec * 100).to_integral_value():
        return f"{dec.quantize(Decimal('0.01')):.2f}"
    return f"{dec:.6f}"


def parse_usd(text: str) -> Decimal:
    """Parse a monetary decimal string, rejecting malformed input."""
    if not isinstance(text, str) or not _USD_RE.match(text):
        raise ValueError(f"not a canonical USD amount: {text!r}")
    return Decimal(text)
