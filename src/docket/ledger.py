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
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from .canon import canonical_bytes, merkle_root, sha256_hex

GENESIS = sha256_hex("docket/genesis/v1")
FORMAT = "docket/ledger/v1"


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
    def from_dict(cls, obj: dict[str, Any]) -> "Event":
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
    _handle: Any = None

    # -- construction ------------------------------------------------------ #
    @classmethod
    def open(cls, path: str | os.PathLike[str], clock: Callable[[], str] | None = None) -> "Ledger":
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        handle = p.open("a", encoding="utf-8")
        return cls(path=p, clock=clock or _now_rfc3339, _handle=handle)

    @classmethod
    def in_memory(cls, clock: Callable[[], str] | None = None) -> "Ledger":
        return cls(path=None, clock=clock or _now_rfc3339)

    # -- writing ----------------------------------------------------------- #
    def append(self, event_type: str, data: dict[str, Any] | None = None) -> Event:
        if self._handle is None and self.path is not None:  # tolerate reopened objects
            self._handle = self.path.open("a", encoding="utf-8")
        seq = len(self.events)
        prev = self.events[-1].hash if self.events else GENESIS
        ev = Event(seq=seq, ts=self.clock(), type=event_type, data=data or {}, prev=prev)
        ev.hash = ev.compute_hash()
        self.events.append(ev)
        if self._handle is not None:
            # Lines are written in canonical form (sorted keys, no insignificant
            # whitespace) so the file digest is reproducible by other
            # implementations, not just by Python's json module.
            self._handle.write(canonical_bytes(ev.to_dict()).decode("utf-8") + "\n")
            self._handle.flush()
            os.fsync(self._handle.fileno())
        return ev

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            finally:
                self._handle = None

    # -- reading ----------------------------------------------------------- #
    @staticmethod
    def read(path: str | os.PathLike[str]) -> list[Event]:
        events: list[Event] = []
        with Path(path).open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                events.append(Event.from_dict(json.loads(line)))
        return events

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
            payload = "".join(
                canonical_bytes(e.to_dict()).decode("utf-8") + "\n" for e in self.events
            ).encode("utf-8")
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
