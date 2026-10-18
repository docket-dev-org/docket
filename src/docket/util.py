"""Small shared helpers: atomic writes, globs, formatting, ids, clocks."""

from __future__ import annotations

import atexit
import fnmatch
import hashlib
import json
import os
import secrets
import shutil
import tempfile
import time
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path
from typing import Any

from .canon import usd


class NonStandardJSONError(ValueError):
    """Base for values a JSON *reader* accepts and a JSON *format* does not define.

    Everything in this family has the same shape of danger: two correct-looking
    implementations disagree, so the document stops being evidence and becomes an argument
    about parsers. Docket refuses all of them and names which one it found.
    """

    #: A short, stable identifier for the refusal, used in records and HTTP error bodies.
    code = "non_standard_json"


class NonFiniteJSONError(NonStandardJSONError):
    """Raised when a document contains ``NaN``, ``Infinity`` or ``-Infinity``.

    Those three tokens are *not* JSON. They are a Python extension that ``json.loads``
    accepts by default and RFC 8259 forbids; ``JSON.parse`` in JavaScript rejects them, so a
    document carrying one is accepted by exactly one of Docket's two reference implementations.
    A distinct type (rather than a bare ``ValueError``) lets a caller say "this file is not JSON"
    instead of "this file is damaged" — the two want different sentences in a report.
    """

    code = "non_finite_number"

    def __init__(self, token: str) -> None:
        super().__init__(
            f"{token} is not a JSON number (RFC 8259 allows neither NaN nor Infinity); "
            "if a real number was meant, write it as a finite decimal"
        )
        self.token = token


class DuplicateJSONKeyError(NonStandardJSONError):
    """Raised when one object names the same key twice.

    RFC 8259 says the behaviour of a parser meeting duplicate keys is *undefined*. In practice
    almost every parser keeps the last one — including both of Docket's — but "almost every" is
    the wrong foundation for a digest: a document whose meaning depends on which parser read it
    is not a document two parties can agree about, and the disagreement would surface as a
    verification failure rather than as the malformed input it is.
    """

    code = "duplicate_object_key"

    def __init__(self, key: str) -> None:
        super().__init__(
            f"the object names the key {key!r} twice; RFC 8259 leaves the result undefined "
            "and Docket will not guess which of the two values was meant"
        )
        self.key = key


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    for key, _value in pairs:
        if key in seen:
            raise DuplicateJSONKeyError(key)
        seen.add(key)
    return dict(pairs)


def reject_json_constant(token: str) -> Any:
    """A ``json.loads`` hook (``parse_constant=``) that refuses the JSON non-extensions.

    Pass it at every site that parses a document Docket did not write, or that writes a
    document itself: a non-finite number that gets far enough to be recorded cannot be
    canonicalised, so it becomes an internal error inside the run's own evidence instead of a
    legible refusal. ``parse_constant`` is called only for ``NaN``/``Infinity``/``-Infinity``,
    never for ordinary numbers or for the words inside strings.
    """
    raise NonFiniteJSONError(token)


def loads_json(data: str | bytes, *, what: str = "document") -> Any:
    """Parse JSON strictly: the JSON extensions and duplicate keys are refused.

    Both refusals are about the same thing — a document must mean the same thing to every
    implementation that reads it. `NaN`/`Infinity` are not spellable in RFC 8259 JSON, and the
    meaning of a repeated key is left undefined; either one turns a record into something two
    verifiers can disagree about. Called at every site that reads a document Docket did not
    itself write, or that is about to write one.
    """
    return json.loads(data, parse_constant=reject_json_constant, object_pairs_hook=_reject_duplicate_keys)


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


#: Host names that count as this machine. ``localhost`` because that is what a user types;
#: everything else goes through ``ipaddress.is_loopback`` so the whole 127.0.0.0/8 range and
#: ``::1`` are recognised without hand-rolling an IP parser — and so that ``0.0.0.0`` is
#: *not* accepted (it is a bind address, not a destination, and on some systems it routes to
#: the default interface), nor is ``localhost.example.com``, nor ``localhost@evil.example``.
_LOOPBACK_NAMES = {"localhost"}


def _is_local_host(host: str) -> bool:
    if host in _LOOPBACK_NAMES:
        return True
    import ipaddress

    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def http_url(url: str) -> str:
    """Return ``url`` if it is an ``http``/``https`` URL, else raise ``ValueError``.

    The weaker of the two guards, and the right one for code that *stands in for an agent*:
    a supervised agent talks to whatever the boundary points it at, so restricting its host
    would be wrong, but handing it ``file:///etc/passwd`` is never right. Every
    ``urlopen`` in this package goes through this function or :func:`local_http_url`, which is
    also the answer to the static-analysis finding that flags unchecked ``urlopen`` as a
    possible SSRF or local-file read (see ``[tool.bandit]`` in pyproject.toml).
    """
    from urllib.parse import urlsplit

    parts = urlsplit(str(url))
    if parts.scheme not in {"http", "https"}:
        raise ValueError(f"refusing to fetch {url!r}: only http/https URLs are used by docket")
    if not parts.hostname:
        raise ValueError(f"refusing to fetch {url!r}: no host")
    return str(url)


def sanitize_url(url: str) -> str:
    """``url`` with everything that can carry a secret removed: query, fragment, userinfo.

    A base URL is recorded as *evidence* — the run's ledger says where model traffic was sent,
    because "a call to a mock/gateway/self-hosted endpoint" is not the same claim as "a call to the
    provider's own API". The path is kept (an OpenAI-compatible gateway lives at a path), the port
    is kept, and the three components that routinely carry credentials — `?api_key=…`,
    `#…`, `user:pass@host` — are dropped, so an operator who pasted a token into a URL does not
    find it in a hash-chained file they may share.

    An unparsable value is *reported as such* rather than silently normalised: the marker reads
    ``<unparsable url>`` in the ledger, so a reader sees that a URL was configured and that Docket
    could not summarise it. Dropping it entirely would be a quiet claim that no override existed.
    """
    from urllib.parse import urlsplit, urlunsplit

    text = str(url)
    try:
        parts = urlsplit(text)
        if not parts.scheme or not parts.hostname:
            return "<unparsable url>"
        host = parts.hostname or ""
        if ":" in host:
            # An IPv6 literal keeps its brackets: dropping them turns `http://[::1]:9000/v1` into
            # `http://::1:9000/v1`, which is not a URL at all. (Found by a test, not by reading.)
            host = f"[{host}]"
        netloc = f"{host}:{parts.port}" if parts.port else host
        return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    except ValueError:
        # A port that is not a number, an IPv6 literal with a typo — `urlsplit` defers some of
        # these to attribute access, and the answer is the same as any other unparsable value.
        return "<unparsable url>"


def local_http_url(url: str) -> str:
    """Return ``url`` if it is an HTTP URL pointing at this machine, else raise ``ValueError``.

    This is the guard for Docket's *own* clients: the SDK posting to the run's boundary, the
    self-test probing it, the demo talking to its scripted provider. All of those are loopback
    by contract, and each one reads its address from somewhere an attacker may reach — an
    environment variable, a config file, a redirect. Asserting the host here means such a value
    cannot steer Docket at ``file://``, at a cloud metadata endpoint, or at somebody else's
    host with the agent's event stream — which for the SDK would be exfiltration of whatever the
    agent recorded as notes.
    """
    parts_url = http_url(url)
    from urllib.parse import urlsplit

    host = (urlsplit(parts_url).hostname or "").lower()
    if not _is_local_host(host):
        raise ValueError(f"refusing to fetch {url!r}: docket only fetches from local addresses, not {host!r}")
    return parts_url


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


def ledger_for_receipt(receipt_path: str | os.PathLike[str]) -> Path | None:
    """The ledger a receipt was written from, found from the receipt's own location.

    ``docket verify`` used to check only the file it was handed, so a reader who ran the
    tamper demo and then re-ran the command the demo *should* have printed got ``VERIFIED``
    back — the tamper is in the ledger, not the receipt, and nothing looked for the ledger.
    The fix is to look, and to look *here*: the receipt sits in ``<dir>/.docket/receipts/``,
    so its ledger is a sibling of it rather than something in the current directory, which
    is where the previous cwd-relative search in ``explain`` looked and often found nothing.

    Selection is by digest, not by recency. A docket directory accumulates one ledger per
    run, and the receipt already carries ``ledger.file_sha256``; matching on it means the
    file chosen is provably the one the receipt was built from, so a reader who verifies a
    receipt copied out of an archive still gets the chain check when the ledger came with it.
    """
    receipt = Path(receipt_path)
    for parent in receipt.parents:
        runs = parent / "runs"
        if not runs.is_dir():
            continue
        candidates = sorted(runs.glob("*.jsonl"))
        if not candidates:
            continue
        if len(candidates) == 1:
            return candidates[0]
        try:
            document = loads_json(receipt.read_bytes())
        except (OSError, ValueError):
            return candidates[-1]
        wanted = document.get("ledger", {}).get("file_sha256") if isinstance(document.get("ledger"), dict) else None
        if isinstance(wanted, str):
            for candidate in candidates:
                digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
                if digest == wanted:
                    return candidate
        return candidates[-1]
    return None
