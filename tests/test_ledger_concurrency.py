"""Concurrency: one ledger, several writer threads.

Docket's boundary is a `ThreadingHTTPServer`, so two model calls in flight are two threads
appending to the same ledger. The ledger is the record everything else is derived from, and a
record that loses or renumbers an event under load is worse than one that refuses to write: the
receipt would describe a run that did not happen.

These tests are aimed at that, not at throughput.
"""

from __future__ import annotations

import threading
from pathlib import Path

from docket.canon import sha256_hex
from docket.ledger import GENESIS, Ledger


def _hammer(ledger: Ledger, threads: int, per_thread: int) -> None:
    start = threading.Barrier(threads)

    def worker(index: int) -> None:
        start.wait()  # release every thread at once: worst case for an unguarded read-then-append
        for step in range(per_thread):
            ledger.append("model.request", {"thread": index, "step": step})

    workers = [threading.Thread(target=worker, args=(index,)) for index in range(threads)]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join(timeout=60)


def test_concurrent_appends_produce_one_contiguous_chain(tmp_path: Path):
    path = tmp_path / "runs" / "concurrent.jsonl"
    ledger = Ledger.open(path, max_bytes=None)
    try:
        _hammer(ledger, threads=8, per_thread=25)
    finally:
        ledger.close()

    events = Ledger.read(path)
    assert len(events) == 200, f"{len(events)} events reached the disk of 200 appended"
    assert [event.seq for event in events] == list(range(200)), "sequence numbers must be the file order"
    assert events[0].prev == GENESIS
    previous = None
    for event in events:
        assert event.prev == (previous.hash if previous else GENESIS), f"broken link at seq {event.seq}"
        assert event.hash == event.compute_hash(), f"event {event.seq} does not hash to its own digest"
        previous = event
    assert len({event.hash for event in events}) == 200, "two events hashed to the same value"


def test_concurrent_appends_do_not_lose_events_from_the_in_memory_view(tmp_path: Path):
    """The in-memory list and the file must agree: the receipt is built from the list, the
    verifier reads the file, and a disagreement between them is a false receipt."""
    path = tmp_path / "runs" / "counted.jsonl"
    ledger = Ledger.open(path, max_bytes=None)
    try:
        _hammer(ledger, threads=6, per_thread=20)
        assert len(ledger.events) == 120
        written = path.read_text(encoding="utf-8").splitlines()
    finally:
        ledger.close()
    assert len(written) == 120, "every event in the list reached the file"
    assert ledger.bytes_written == sum(len(line.encode("utf-8")) + 1 for line in written)


def test_two_ledgers_in_one_process_still_serialise_on_the_file(tmp_path: Path):
    """A second opener in the same process gets the lock or a clear refusal — never a silent
    second writer appending to a chain it did not build."""
    path = tmp_path / "runs" / "locked.jsonl"
    first = Ledger.open(path, max_bytes=None)
    try:
        try:
            second = Ledger.open(path, max_bytes=None)
        except Exception as exc:  # the documented behaviour: refuse, name the holder
            assert "lock" in str(exc).lower()
        else:
            # If the implementation allows it, the chain must still be sound.
            second.append("note", {"who": "second"})
            second.close()
        first.append("note", {"who": "first"})
    finally:
        first.close()
    events = Ledger.read(path)
    assert [event.seq for event in events] == list(range(len(events)))
    for event in events:
        assert event.hash == event.compute_hash()


def test_the_ledger_file_digest_is_reproducible_from_the_bytes(tmp_path: Path):
    """`ledger.file_sha256` is what the receipt publishes, so it has to be a function of the
    bytes on disk and nothing else — including after concurrent writes."""
    path = tmp_path / "runs" / "digest.jsonl"
    ledger = Ledger.open(path, max_bytes=None)
    try:
        _hammer(ledger, threads=4, per_thread=10)
        digest = ledger.file_sha256()
    finally:
        ledger.close()
    assert digest == sha256_hex(path.read_bytes())
