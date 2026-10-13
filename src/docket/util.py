"""Small shared helpers: atomic writes, globs, formatting, ids, clocks."""

from __future__ import annotations

import fnmatch
import hashlib
import os
import secrets
import tempfile
import time
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path

from .canon import usd


def atomic_write(path: str | os.PathLike[str], data: str | bytes) -> None:
    """Write a file atomically (temp file + rename in the same directory)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = data.encode("utf-8") if isinstance(data, str) else data
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):  # pragma: no cover - only on failure
            os.unlink(tmp)


def matches_any(path: str, patterns: Iterable[str]) -> bool:
    """Glob match with ``**`` support, tolerant of leading ``./``."""
    normalised = path[2:] if path.startswith("./") else path
    for pattern in patterns:
        pat = pattern[2:] if pattern.startswith("./") else pattern
        if pat.endswith("/**") and (normalised == pat[:-3] or normalised.startswith(pat[:-2])):
            return True
        if fnmatch.fnmatch(normalised, pat):
            return True
        if fnmatch.fnmatch(Path(normalised).name, pat):
            return True
    return False


def human_usd(amount: Decimal | str) -> str:
    """Format a monetary amount for humans (``$1,234.56``)."""
    dec = Decimal(str(amount))
    return f"${dec:,.2f}" if abs(dec) >= Decimal("0.005") else f"${dec:,.4f}"


def human_int(value: int) -> str:
    return f"{value:,}"


def human_duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rem = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{rem:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def docket_number(when: float | None = None, rng: secrets.SystemRandom | None = None) -> str:
    """A human-quotable identifier: ``DKT-YYYYMMDD-XXXXXX``.

    The random part is drawn from ``secrets`` so that numbers are not guessable in
    sequence, and derived deterministically in tests via an injected RNG.
    """
    stamp = time.strftime("%Y%m%d", time.gmtime(when if when is not None else time.time()))
    source = rng or secrets.SystemRandom()
    suffix = "".join(source.choice("0123456789ABCDEF") for _ in range(6))
    return f"DKT-{stamp}-{suffix}"


def short_hash(value: str, length: int = 12) -> str:
    return value[:length]


def mb(nbytes: int) -> str:
    if nbytes < 1024:
        return f"{nbytes} B"
    if nbytes < 1024 * 1024:
        return f"{nbytes / 1024:.1f} KB"
    return f"{nbytes / (1024 * 1024):.1f} MB"


def within(host: str, allow: Iterable[str]) -> bool:
    hostname = host.split(":")[0].lower()
    return any(hostname == a.lower() or fnmatch.fnmatch(hostname, a.lower()) for a in allow)


def stable_seed(*parts: str) -> int:
    """Deterministic integer seed derived from string parts (for demo/test clocks)."""
    return int(hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12], 16)


def money_or_zero(value: Decimal | None) -> str:
    return usd(value) if value is not None else "0.00"
