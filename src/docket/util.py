"""Small shared helpers: atomic writes, globs, formatting, ids, clocks."""

from __future__ import annotations

import atexit
import fnmatch
import hashlib
import os
import secrets
import shutil
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


#: Set this to keep every scratch workspace a run creates (``DOCKET_KEEP_SCRATCH=1``).
KEEP_SCRATCH_ENV = "DOCKET_KEEP_SCRATCH"


def keep_scratch_requested() -> bool:
    return os.environ.get(KEEP_SCRATCH_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def scratch_workspace(
    prefix: str,
    *,
    keep: bool = False,
    path: str | os.PathLike[str] | None = None,
) -> tuple[Path, bool]:
    """Return ``(directory, kept)`` for a scratch workspace, and promise what happens to it.

    A resilience tool that leaves a directory behind on every invocation is running its own slow
    leak: a CI job that calls ``docket demo`` or ``docket chaos`` a hundred times a day fills the
    container's tmpfs, and a full tmpfs fails somewhere unrelated with an unhelpful error. So the
    rule is stated once, here:

    * a directory the *caller* supplied is theirs — never removed, ``kept=True``;
    * a directory this function creates is removed at interpreter exit unless ``keep=True`` or
      ``DOCKET_KEEP_SCRATCH`` is set, and ``kept`` says which happened.

    Callers that print a path for a human to open are expected to print it only when ``kept`` says
    the path will still exist.
    """
    if path is not None:
        return Path(path), True
    directory = Path(tempfile.mkdtemp(prefix=prefix))
    kept = bool(keep) or keep_scratch_requested()
    if not kept:
        atexit.register(shutil.rmtree, directory, True)
    return directory, kept


_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "0.0.0.0"}


def local_http_url(url: str) -> str:
    """Return ``url`` if it is an HTTP URL pointing at this machine, else raise ``ValueError``.

    Every ``urllib.request.urlopen`` in this package talks to a scripted provider or the
    boundary on loopback; nothing else is ever intended. Asserting that here means a URL that
    arrives from outside — a redirect, a crafted ``DOCKET_*`` environment variable, a receipt —
    cannot steer the client at ``file://``, at a cloud metadata endpoint, or at somebody else's
    host. (Static analysis flags every ``urlopen`` as a possible SSRF for exactly this reason;
    this function is the answer to it, and the analysis suppressals point here.)
    """
    from urllib.parse import urlsplit

    parts = urlsplit(str(url))
    if parts.scheme not in {"http", "https"}:
        raise ValueError(f"refusing to fetch {url!r}: only http/https URLs are used by docket")
    host = (parts.hostname or "").lower()
    if host not in _LOOPBACK_HOSTS:
        raise ValueError(f"refusing to fetch {url!r}: docket only fetches from local addresses, not {host!r}")
    return str(url)


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


def human_bytes(count: int | None) -> str:
    """Format a byte count for humans (``1.5 MiB``). Binary units, because these are file sizes."""
    size = float(max(0, int(count or 0)))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


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
