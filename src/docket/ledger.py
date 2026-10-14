"""The append-only, hash-chained ledger.

Each event is bound to its predecessor:

    hash(n) = SHA256(canonical_json({seq, ts, type, data, prev}))
    prev(n) = hash(n-1), and prev(0) = the genesis value

Any mutation of any field of any event — including reordering, truncation of the
tail, or insertion — changes at least one hash and breaks the chain from that
point forward. The chain alone does not prove *who* wrote the file; that is what
sealing (HMAC) and signing (Ed25519) are for. See ``docs/threat-model.md`` for
what each guarantee does and does not cover.

``GENESIS`` is the canonical start-of-chain value: the SHA-256 of the UTF-8 string
``"docket/genesis/v1"``.

Three properties beyond "append a JSON line" earn their complexity here:

* **Writes are crash-, full-disk- and read-only-aware.** A write failure is recorded in
  memory (never swallowed), counted, and can either be tolerated (the receipt declares
  the gap) or escalated (``fail_fast``), depending on who is watching. See
  ``resilience.EvidenceGuard``.
* **The file is read defensively.** ``read_with_diagnostics`` distinguishes a torn final
  line from mid-file junk from a non-UTF-8 byte sequence, because those need different
  responses: a torn tail is a crash, mid-file junk is tampering.
* **Opening a ledger takes an exclusive lock.** Two processes appending to one chain
  would each hold half of a valid history and a fork in the middle; the second writer
  refuses to start instead. Evidence is never repaired by rewriting it — see
  :func:`quarantine_torn_tail`, which *moves* bytes and says where they went.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .canon import canonical_bytes, merkle_root, sha256_hex
from .resilience import EvidenceGuard

GENESIS = sha256_hex("docket/genesis/v1")
FORMAT = "docket/ledger/v1"

#: Events that are always written even when ``max_events`` has been reached. A capped
#: ledger must still be able to say why the run stopped; a record that ends silently
#: mid-run is worse than a record that ends with a reason.
ALWAYS_RECORDABLE = frozenset(
    {
        "cap.exceeded",
        "cap.warning",
        "policy.violation",
        "session.end",
        "proc.exit",
        "x-docket.evidence-gap",
    }
)

#: Default byte ceiling for a single ledger file (256 MiB). A runaway agent must not be
#: able to fill a disk with its own press coverage.
DEFAULT_MAX_BYTES = 256 * 1024 * 1024


class LedgerLockedError(RuntimeError):
    """Raised when another process already holds this ledger."""


class LedgerDamagedError(RuntimeError):
    """Raised when an existing ledger file cannot be safely appended to.

    Appending to a damaged file would produce a record that *looks* like evidence and is
    not: the new events would chain from genesis on top of a chain that is already broken.
    The refusal names the damage and the remedy, and never rewrites the file.
    """


class LedgerFullError(RuntimeError):
    """Raised when ``max_events`` or ``max_bytes`` has been reached.

    Deliberately not an ``OSError``: callers distinguish "the disk refused the write"
    (an environment problem) from "the record is complete" (a policy outcome).
    """

    def __init__(self, reason: str, limit: int, observed: int) -> None:
        super().__init__(f"{reason}: {observed} >= {limit}")
        self.reason = reason
        self.limit = limit
        self.observed = observed


def _now_rfc3339() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class Event:
    """A single ledger entry."""

    seq: int
    ts: str
    type: str
    data: dict[str, Any]
    prev: str
    hash: str = ""

    def core(self) -> dict[str, Any]:
        return {"seq": self.seq, "ts": self.ts, "type": self.type, "data": self.data, "prev": self.prev}

    def compute_hash(self) -> str:
        return sha256_hex(canonical_bytes(self.core()))

    def to_dict(self) -> dict[str, Any]:
        out = self.core()
        out["hash"] = self.hash or self.compute_hash()
        return out

    @classmethod
    def from_dict(cls, obj: dict[str, Any]) -> Event:
        return cls(
            seq=int(obj["seq"]),
            ts=str(obj["ts"]),
            type=str(obj["type"]),
            data=obj.get("data") or {},
            prev=str(obj["prev"]),
            hash=str(obj.get("hash", "")),
        )


@dataclass
class Ledger:
    """Append-only ledger writer/reader backed by a JSONL file."""

    path: Path | None = None
    clock: Callable[[], str] = _now_rfc3339
    events: list[Event] = field(default_factory=list)
    guard: EvidenceGuard | None = None
    max_events: int | None = None
    max_bytes: int | None = DEFAULT_MAX_BYTES
    bytes_written: int = 0
    unwritten: list[dict[str, Any]] = field(default_factory=list)
    _handle: Any = None
    _lock_handle: Any = None

    # -- construction ------------------------------------------------------ #
    @classmethod
    def open(
        cls,
        path: str | os.PathLike[str],
        clock: Callable[[], str] | None = None,
        *,
        guard: EvidenceGuard | None = None,
        exclusive: bool = True,
        max_events: int | None = None,
        max_bytes: int | None = DEFAULT_MAX_BYTES,
    ) -> Ledger:
        """Open (and create) a ledger file, taking an exclusive lock by default."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):  # not every platform has chmod on directories
            os.chmod(p.parent, 0o700)  # the record is the customer's, not the machine's
        if p.is_symlink():
            # Following a symlink would append the run's history to whatever the link
            # points at — a file whose owner never agreed to host it. Refuse and say why.
            raise LedgerDamagedError(
                f"refusing to write {p}: it is a symbolic link. A ledger must be a regular file "
                "owned by the run; point --docket-dir at a directory you control."
            )
        if p.exists() and not p.is_file():
            raise LedgerDamagedError(f"refusing to write {p}: it exists and is not a regular file")
        lock_handle = None
        if exclusive:
            lock_handle = _acquire_lock(p)
        handle = _open_ledger_handle(p)
        ledger = cls(
            path=p,
            clock=clock or _now_rfc3339,
            _handle=handle,
            guard=guard,
            max_events=max_events,
            max_bytes=max_bytes,
            _lock_handle=lock_handle,
        )
        size = p.stat().st_size if p.exists() else 0
        ledger.bytes_written = size
        if size > 0:
            # Resuming an existing ledger means continuing *its* chain. Starting a second
            # chain from genesis in the same file is the one failure that would corrupt a
            # record while looking like success, so it is impossible here: the file is
            # read, validated, and appended to only if it is intact.
            events, diagnostics = cls.read_with_diagnostics(p)
            damage = None
            if diagnostics.get("utf8") is False:
                damage = f"the file is not valid UTF-8 ({diagnostics.get('error')})"
            elif diagnostics.get("bad_lines"):
                first = diagnostics["bad_lines"][0]
                damage = (
                    f"line {first['line']} is not a valid event ({first['error']}). This is not a torn "
                    "write — the file was edited or corrupted. It is preserved as-is: copy it aside "
                    "and start a new session if you need to keep working."
                )
            elif diagnostics.get("torn_tail"):
                leaf = diagnostics["torn_tail"]
                damage = (
                    f"the final line is incomplete ({leaf['bytes']} bytes after line {leaf['line'] - 1}). "
                    f"Recover it with `docket doctor --quarantine-torn-tail {p}`, which moves those bytes "
                    "to a sidecar without deleting anything."
                )
            elif diagnostics.get("chain_error"):
                broken = diagnostics["chain_error"]
                damage = f"the hash chain is broken at seq {broken['seq']}: {broken['reason']}"
            if damage is not None:
                with contextlib.suppress(Exception):
                    handle.close()
                if lock_handle is not None:
                    with contextlib.suppress(Exception):
                        _release_lock(lock_handle, p)
                raise LedgerDamagedError(f"refusing to append to {p}: {damage}")
            ledger.events.extend(events)
        return ledger

    @classmethod
    def in_memory(cls, clock: Callable[[], str] | None = None) -> Ledger:
        return cls(path=None, clock=clock or _now_rfc3339)

    # -- writing ----------------------------------------------------------- #
    def append(
        self, event_type: str, data: dict[str, Any] | None = None, *, control: bool = False
    ) -> Event:
        """Record an event.

        ``control=True`` marks an event written by the supervising tool itself — the
        decision to stop a run, the exit of a process, the end of a session. Those events
        are exempt from the ledger's own ceilings: a ceiling that could prevent the record
        from explaining *why the run stopped* would turn a policy outcome into a crash.
        Events attributed to the agent never get this exemption.
        """
        # A handle can be closed under us (descriptors get reclaimed, an operator closes
        # one by hand, an embedder reuses the object). Reopen first: a writable path that
        # was only *closed* is not a lost record.
        if self._handle is not None and getattr(self._handle, "closed", False):
            self._handle = None
        if self._handle is None and self.path is not None:
            try:
                self._handle = self.path.open("a", encoding="utf-8")
            except OSError as exc:
                self.unwritten.append({"seq": len(self.events), "type": event_type, "error": f"{type(exc).__name__}: {exc}"})
                if self.guard is not None:
                    self.guard.record_failure(seq=len(self.events), error=exc)
                ev = Event(seq=len(self.events), ts=self.clock(), type=event_type, data=data or {}, prev=self.events[-1].hash if self.events else GENESIS)
                ev.hash = ev.compute_hash()
                self.events.append(ev)
                return ev
        if not (control or self._room_for(event_type)):
            raise LedgerFullError(
                "ledger event limit reached" if self.max_events is not None else "ledger size limit reached",
                int(self.max_events if self.max_events is not None else self.max_bytes or 0),
                len(self.events) if self.max_events is not None else int(self.bytes_written),
            )
        seq = len(self.events)
        prev = self.events[-1].hash if self.events else GENESIS
        ev = Event(seq=seq, ts=self.clock(), type=event_type, data=data or {}, prev=prev)
        ev.hash = ev.compute_hash()
        self.events.append(ev)
        if self._handle is not None:
            # Lines are written in canonical form (sorted keys, no insignificant
            # whitespace) so the file digest is reproducible by other
            # implementations, not just by Python's json module.
            line = canonical_bytes(ev.to_dict()).decode("utf-8") + "\n"
            try:
                self._handle.write(line)
                self._handle.flush()
                os.fsync(self._handle.fileno())
            except Exception as exc:  # noqa: BLE001
                # Deliberately broad: a closed handle raises ValueError, a revoked mount
                # raises OSError, an encoder can raise anything. Every one of them means
                # the same thing — the record did not reach the disk — and every one must
                # be visible. The event stays in memory, the failure is recorded, and
                # `evidence.degraded` reaches the receipt; `fail_fast` stops the run
                # (see resilience.EvidenceGuard).
                self.unwritten.append({"seq": ev.seq, "type": ev.type, "error": f"{type(exc).__name__}: {exc}"})
                if self.guard is not None:
                    self.guard.record_failure(seq=ev.seq, error=exc)
            else:
                self.bytes_written += len(line.encode("utf-8"))
        return ev

    def _room_for(self, event_type: str) -> bool:
        if event_type in ALWAYS_RECORDABLE:
            return True
        if self.max_events is not None and len(self.events) >= self.max_events:
            return False
        if self.max_bytes is not None and self.bytes_written >= self.max_bytes:
            return False
        return True

    @property
    def degraded(self) -> bool:
        """True when at least one event could not be written to disk."""
        return bool(self.unwritten)

    def close(self) -> None:
        """Release the file and the lock. Cleanup never raises.

        A ledger on a failing device (ENOSPC, EIO, a revoked mount) raises from
        ``close()`` as well as from ``write()``; letting that escape turns an orderly
        shutdown into a traceback after the failure was already recorded by ``append``.
        """
        if self._handle is not None:
            with contextlib.suppress(Exception):
                self._handle.close()
            self._handle = None
        if self._lock_handle is not None:
            with contextlib.suppress(Exception):
                _release_lock(self._lock_handle, self.path)
            self._lock_handle = None

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *exc_info: Any) -> bool:
        self.close()
        return False

    # -- reading ----------------------------------------------------------- #
    @staticmethod
    def read(path: str | os.PathLike[str]) -> list[Event]:
        """Read a ledger strictly: any unparsable line raises.

        Callers that need to survive a damaged file (verifiers, ``docket doctor``) use
        :meth:`read_with_diagnostics`, which reports the damage instead of raising.
        """
        events, diagnostics = Ledger.read_with_diagnostics(path)
        if diagnostics["torn_tail"]:
            tail = diagnostics["torn_tail"]
            raise ValueError(f"ledger has a torn final line at line {tail['line']}: {tail['error']}")
        if diagnostics["bad_lines"]:
            first = diagnostics["bad_lines"][0]
            raise ValueError(f"ledger line {first['line']} is not valid JSON: {first['error']}")
        return events

    @staticmethod
    def read_with_diagnostics(path: str | os.PathLike[str]) -> tuple[list[Event], dict[str, Any]]:
        """Parse as much as possible and describe exactly what was wrong.

        Damage is classified rather than lumped together, because the classes imply
        different responses: a *torn tail* is a crash during the last write (recoverable
        by quarantining those bytes), *bad lines* in the middle are tampering or bit rot,
        and *non-UTF-8 bytes* usually mean the file is not a ledger at all.
        """
        target = Path(path)
        diagnostics: dict[str, Any] = {
            "path": str(target),
            "bytes": 0,
            "lines": 0,
            "events": 0,
            "torn_tail": None,
            "bad_lines": [],
            "trailing_newline": True,
            "utf8": True,
        }
        events: list[Event] = []
        if not target.exists():
            diagnostics["error"] = "file does not exist"
            return events, diagnostics
        raw = target.read_bytes()
        diagnostics["bytes"] = len(raw)
        diagnostics["trailing_newline"] = raw.endswith(b"\n") or raw == b""
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            # The diagnostics shape is the same on every path: a caller that reads three
            # keys from a healthy file must not get a KeyError from a damaged one. The
            # whole point of this method is that damage is *reported*, never surprising.
            diagnostics["utf8"] = False
            diagnostics["error"] = f"not valid UTF-8 at byte {exc.start}: {exc.reason}"
            diagnostics["healthy"] = False
            diagnostics["events"] = None
            diagnostics["chain_error"] = None
            diagnostics["torn_tail"] = None
            diagnostics["bad_lines"] = []
            diagnostics["recoverable_prefix"] = False
            return events, diagnostics

        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        diagnostics["lines"] = len(lines)
        for index, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
                events.append(Event.from_dict(payload))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                entry = {
                    "line": index + 1,
                    "error": f"{type(exc).__name__}: {exc}",
                    "bytes": len(line.encode("utf-8")),
                }
                is_last = index == len(lines) - 1
                if is_last and (not diagnostics["trailing_newline"] or not diagnostics["bad_lines"]):
                    # The final line without a newline, or a final line that will not
                    # parse, is the signature of a process killed mid-write.
                    diagnostics["torn_tail"] = entry
                else:
                    diagnostics["bad_lines"].append(entry)
        # Structural health is necessary but not sufficient: a file can parse perfectly
        # and still not be a ledger (two chains appended to one file, an edited event, a
        # deleted line). The chain check is the one a receipt depends on, so it is part of
        # the diagnostics rather than something a caller has to remember to run.
        diagnostics["chain_error"] = None
        if events and not diagnostics["torn_tail"] and not diagnostics["bad_lines"]:
            prev = GENESIS
            for expected_seq, event in enumerate(events):
                if event.seq != expected_seq:
                    diagnostics["chain_error"] = {
                        "seq": expected_seq,
                        "reason": f"sequence gap: expected seq {expected_seq}, found {event.seq}",
                    }
                    break
                if event.prev != prev:
                    diagnostics["chain_error"] = {
                        "seq": event.seq,
                        "reason": f"broken link at seq {event.seq}: prev does not match the previous hash",
                    }
                    break
                if event.compute_hash() != event.hash:
                    diagnostics["chain_error"] = {
                        "seq": event.seq,
                        "reason": "content hash mismatch (the event was modified after it was written)",
                    }
                    break
                prev = event.hash
        diagnostics["events"] = len(events) if not (diagnostics["torn_tail"] or diagnostics["bad_lines"]) else None
        diagnostics["healthy"] = not (
            diagnostics["torn_tail"] or diagnostics["bad_lines"] or diagnostics["chain_error"]
        )
        return events, diagnostics

    def __iter__(self) -> Iterator[Event]:
        return iter(self.events)

    def __len__(self) -> int:
        return len(self.events)

    # -- derived values ---------------------------------------------------- #
    @property
    def head(self) -> str:
        """Hash of the last event, or the genesis value for an empty ledger."""
        return self.events[-1].hash if self.events else GENESIS

    def merkle_root(self) -> str:
        """RFC 6962 Merkle root over all event hashes."""
        return merkle_root([e.hash for e in self.events])

    def file_sha256(self) -> str:
        """SHA-256 of the ledger file exactly as written on disk."""
        if self.path is None or not self.path.exists():
            payload = "".join(canonical_bytes(e.to_dict()).decode("utf-8") + "\n" for e in self.events).encode("utf-8")
            return hashlib.sha256(payload).hexdigest()
        return hashlib.sha256(self.path.read_bytes()).hexdigest()

    def types(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for e in self.events:
            counts[e.type] = counts.get(e.type, 0) + 1
        return counts

    # -- verification helper ---------------------------------------------- #
    def chain_error(self) -> tuple[int, str] | None:
        """Return ``(seq, reason)`` for the first broken link, or ``None``."""
        prev = GENESIS
        for expected_seq, ev in enumerate(self.events):
            if ev.seq != expected_seq:
                return expected_seq, f"sequence gap: expected seq {expected_seq}, found {ev.seq}"
            if ev.prev != prev:
                return ev.seq, f"broken link: prev={ev.prev[:12]}… expected {prev[:12]}…"
            if ev.compute_hash() != ev.hash:
                return ev.seq, "content hash mismatch (event was modified after it was written)"
            prev = ev.hash
        return None


def count_events(events: Iterable[Event], event_type: str) -> int:
    return sum(1 for e in events if e.type == event_type)


# --------------------------------------------------------------------------- #
# Locking
# --------------------------------------------------------------------------- #

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]


def _open_ledger_handle(path: Path) -> Any:
    """Open (creating) the ledger for append, refusing to follow a symlink.

    ``O_NOFOLLOW`` closes the race that a ``is_symlink()`` check alone leaves open: an
    attacker who can write in the ledger's directory could swap the file for a link
    between the check and the open. Where the flag does not exist, the check above plus
    ``O_EXCL`` on creation still make the common case safe.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        if exc.errno in {getattr(errno, "ELOOP", 40), getattr(errno, "EMLINK", 31)}:
            raise LedgerDamagedError(
                f"refusing to write {path}: it is a symbolic link (no-follow open refused)"
            ) from exc
        raise
    os.set_inheritable(fd, False)  # a forked child must not hold the ledger open
    handle = os.fdopen(fd, "a", encoding="utf-8")
    with contextlib.suppress(OSError):  # pragma: no cover - platform dependent
        os.fchmod(handle.fileno(), 0o600)
    return handle


def _acquire_lock(path: Path) -> Any:
    """Take an exclusive, non-blocking lock beside the ledger.

    ``flock`` where the platform provides it; otherwise an ``O_EXCL`` marker file whose
    stale entries are detected by re-reading the PID. The two implementations differ in
    mechanism, not intent: never let two processes build one chain.
    """
    lock_path = path.with_suffix(path.suffix + ".lock")
    if fcntl is not None:
        if lock_path.is_symlink():
            raise LedgerDamagedError(f"refusing to use {lock_path}: the lock file is a symbolic link")
        handle = lock_path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            holder = lock_path.read_text(encoding="utf-8").strip() or "unknown"
            raise LedgerLockedError(
                f"{path} is already held by another Docket process (pid {holder}). "
                "Two writers would produce two chains in one file; use a different --docket-dir "
                "or stop the other run."
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        return handle
    try:  # pragma: no cover - Windows
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        holder = lock_path.read_text(encoding="utf-8").strip() if lock_path.exists() else "unknown"
        if holder.isdigit() and not _pid_alive(int(holder)):
            lock_path.unlink(missing_ok=True)
            return _acquire_lock(path)
        raise LedgerLockedError(f"{path} is already held by another Docket process (pid {holder})") from exc
    os.write(fd, str(os.getpid()).encode("utf-8"))
    return fd


def _release_lock(handle: Any, path: Path | None) -> None:
    if fcntl is not None:
        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
    else:  # pragma: no cover - Windows
        with contextlib.suppress(OSError):
            os.close(handle)
    if path is not None:
        with contextlib.suppress(OSError):
            path.with_suffix(path.suffix + ".lock").unlink(missing_ok=True)


def _pid_alive(pid: int) -> bool:  # pragma: no cover - Windows path
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------- #
# Recovery: quarantining a torn tail without destroying evidence
# --------------------------------------------------------------------------- #


@dataclass
class QuarantineResult:
    """What a quarantine moved, and where it went."""

    performed: bool
    reason: str
    kept_bytes: int = 0
    moved_bytes: int = 0
    sidecar: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "performed": self.performed,
            "reason": self.reason,
            "kept_bytes": self.kept_bytes,
            "moved_bytes": self.moved_bytes,
            "sidecar": self.sidecar,
        }


def quarantine_torn_tail(path: str | os.PathLike[str], *, now: str | None = None) -> QuarantineResult:
    """Move an incomplete final line out of the way, keeping every byte.

    Refuses to act on anything except a torn tail: a ledger with a broken chain in the
    middle is *evidence of tampering*, and rewriting it would destroy the thing worth
    keeping. The removed bytes are appended to ``<ledger>.torn-<timestamp>`` (fsync'd
    before the truncate) and the operation is described in the returned result.
    """
    target = Path(path)
    events, diagnostics = Ledger.read_with_diagnostics(target)
    if events is None:  # pragma: no cover - defensive
        return QuarantineResult(False, "unreadable ledger")
    if diagnostics.get("bad_lines"):
        return QuarantineResult(
            False,
            "refusing to modify a ledger whose damage is not a torn tail: "
            f"line {diagnostics['bad_lines'][0]['line']} is not valid JSON, which indicates tampering "
            "or corruption rather than an interrupted write. Preserve it as evidence.",
        )
    if not diagnostics.get("utf8", True):
        return QuarantineResult(False, f"refusing to modify a file that is not UTF-8: {diagnostics.get('error')}")
    if not diagnostics.get("torn_tail"):
        return QuarantineResult(False, "nothing to do: the ledger parses cleanly")
    if diagnostics["torn_tail"] and diagnostics["bad_lines"] == []:
        pass
    raw = target.read_bytes()
    # Split on bytes so the moved blob is byte-identical to what was on disk.
    if raw.endswith(b"\n"):
        # A trailing newline with an unparsable last line: move that whole line.
        head, _, tail = raw.rpartition(b"\n")
        head += b"\n"
    else:
        head, _, tail = raw.rpartition(b"\n")
        head += b"\n" if head else b""
    # One sidecar per recovery, created exclusively: appending to an existing sidecar
    # would mix two recoveries into one file, and overwriting one would destroy evidence
    # that an operator is entitled to keep.
    stamp = _stamp(now)
    sidecar = target.with_suffix(target.suffix + f".torn-{stamp}")
    attempt = 1
    while True:
        try:
            fd = os.open(sidecar, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            attempt += 1
            sidecar = target.with_suffix(target.suffix + f".torn-{stamp}-{attempt}")
            continue
        break
    with os.fdopen(fd, "wb") as handle:
        handle.write(b"# moved from " + str(target).encode("utf-8") + b" by quarantine_torn_tail\n")
        handle.write(tail)
        handle.flush()
        os.fsync(handle.fileno())
    with target.open("r+b") as handle:
        handle.truncate(len(head))
        handle.flush()
        os.fsync(handle.fileno())
    return QuarantineResult(
        performed=True,
        reason="moved an incomplete final line; every byte is preserved in the sidecar",
        kept_bytes=len(head),
        moved_bytes=len(tail),
        sidecar=str(sidecar),
    )


def _stamp(now: str | None) -> str:
    if now:
        return now
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
