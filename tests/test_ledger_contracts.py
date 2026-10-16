"""The ledger's contracts with its callers, independent of the hash chain itself.

`test_ledger.py` is about the chain, `test_ledger_faults.py` about damaged files, and
`test_ledger_concurrency.py` about parallel appends. This file is about the rest of the surface a
caller actually touches: event dictionaries, byte accounting, ceilings, digests over a file that
grew behind the writer's back, the lock file's contents, and the repair path's sidecar naming.

Every test here exists because `tools/mutate.py` changed a single operator in `ledger.py`, nothing
in the suite objected, and the changed operator was *not* harmless. Where a mutant is genuinely
equivalent — a `None` where the caller only asks for truthiness, a Windows-only branch on a Linux
run — the equivalence is written down in `docs/testing.md` instead, because a test that pretends to
pin an unobservable difference is worse than no test.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from docket.canon import canonical_bytes
from docket.ledger import (
    GENESIS,
    Event,
    Ledger,
    LedgerDamagedError,
    LedgerFullError,
    LedgerLockedError,
    _pid_alive,
    quarantine_torn_tail,
)


def _chain(path: Path, count: int = 2, clock=None) -> Ledger:
    ledger = Ledger.open(path, clock=clock, max_bytes=None)
    for index in range(count):
        ledger.append("note", {"message": f"event {index}"})
    ledger.close()
    return ledger


# --------------------------------------------------------------------------- #
# Event dictionaries
# --------------------------------------------------------------------------- #


def test_an_unhashed_event_fills_in_its_hash_when_serialised(clock):
    """`Event.to_dict()` is what a receipt, the JSONL file and the JS verifier all read.

    An event that was never sealed (`hash=""`, e.g. one reconstructed by a tool or a test) must
    still serialise with a hash — the empty string would make the dict claim a digest it does not
    have. A mutated `or` in that fill-in left the empty hash in place and nothing objected.
    """
    event = Event(seq=0, ts="2026-01-01T00:00:00Z", type="note", data={"message": "x"}, prev=GENESIS)
    assert event.hash == ""
    payload = event.to_dict()
    assert payload["hash"] == event.compute_hash()
    assert payload["hash"] and len(payload["hash"]) == 64
    assert payload["prev"] == GENESIS and payload["data"] == {"message": "x"}

    # A sealed event keeps the hash it was sealed with, rather than being re-derived.
    sealed = Ledger.in_memory(clock=clock)
    appended = sealed.append("note", {"message": "x"})
    assert appended.to_dict()["hash"] == appended.hash


def test_an_event_can_be_rebuilt_from_its_dictionary(clock):
    """`from_dict(to_dict())` round-trips: the file format is the interchange format."""
    event = Event(seq=7, ts="2026-01-01T00:00:00Z", type="x-vendor.custom", data={"a": [1, 2]}, prev="a" * 64)
    event.hash = event.compute_hash()
    rebuilt = Event.from_dict(json.loads(json.dumps(event.to_dict())))
    assert rebuilt == event
    assert rebuilt.to_dict() == event.to_dict()
    assert canonical_bytes(rebuilt.core()) == canonical_bytes(event.core())


# --------------------------------------------------------------------------- #
# Byte accounting and ceilings
# --------------------------------------------------------------------------- #


def test_a_fresh_ledger_has_written_nothing(tmp_path, clock):
    """`bytes_written` is the size the byte ceiling is measured against, so it must start at zero
    for both a brand-new file and an existing empty one — a nonzero start would silently shrink
    every byte budget by that much."""
    fresh = Ledger.open(tmp_path / "fresh.jsonl", clock=clock)
    assert fresh.bytes_written == 0
    assert fresh._room_for("note") is True
    fresh.close()

    empty = tmp_path / "empty.jsonl"
    empty.write_bytes(b"")
    reopened = Ledger.open(empty, clock=clock)
    assert reopened.bytes_written == 0
    reopened.close()

    assert Ledger.in_memory(clock=clock).bytes_written == 0, "and so does a ledger with no file at all"


def test_an_existing_directory_is_reused_rather_than_recreated(tmp_path, clock):
    """Opening a ledger twice in the same directory is the common case — a second run, a
    re-attach, a repaired file — so the directory creation must tolerate a directory that is
    already there. A mutated `exist_ok` turned every such open into `FileExistsError`."""
    deep = tmp_path / "runs" / "2026" / "10" / "deeper"
    nested = Ledger.open(deep / "made.jsonl", clock=clock)  # the whole chain is created
    nested.append("note", {"message": "nested"})
    nested.close()
    assert deep.is_dir() and (deep / "made.jsonl").exists()

    directory = tmp_path / "runs"
    directory.mkdir(exist_ok=True)
    first = Ledger.open(directory / "one.jsonl", clock=clock)
    first.append("note", {"message": "one"})
    first.close()

    second = Ledger.open(directory / "two.jsonl", clock=clock)
    second.append("note", {"message": "two"})
    second.close()
    assert len(Ledger.read(directory / "one.jsonl")) == 1
    assert len(Ledger.read(directory / "two.jsonl")) == 1

    # And resuming the very same file, after a clean close, is an open — not a refusal.
    resumed = Ledger.open(directory / "one.jsonl", clock=clock)
    assert len(resumed) == 1, "the existing chain is loaded, not replaced"
    resumed.append("note", {"message": "three"})
    resumed.close()
    events, diagnostics = Ledger.read_with_diagnostics(directory / "one.jsonl")
    assert diagnostics["healthy"] is True and len(events) == 2


def test_a_one_byte_ledger_is_refused_like_any_other_damaged_file(tmp_path, clock):
    """The size test that decides "resume this file" versus "start a new one" must not round a
    one-byte file down to "empty". A one-byte file is a torn write, and appending to it would put
    a second chain after a fragment of the first."""
    path = tmp_path / "one-byte.jsonl"
    path.write_bytes(b"{")
    with pytest.raises(LedgerDamagedError, match="refusing to append"):
        Ledger.open(path, clock=clock)
    assert path.read_bytes() == b"{", "the evidence is untouched"


def test_opening_a_file_with_several_bad_lines_names_the_first_one(tmp_path, clock):
    """Resuming a damaged ledger is refused, and the refusal points at the *first* line that is not
    an event: that is where an operator opens the file. A mutated index named the second line,
    which sends the reader past the actual start of the damage."""
    path = tmp_path / "two-bad.jsonl"
    _chain(path, 4)
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[1] = "not json at all"
    lines[2] = "also not json"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(LedgerDamagedError) as caught:
        Ledger.open(path, clock=clock)
    message = str(caught.value)
    assert "line 2 is not a valid event" in message
    assert "line 3" not in message, "the first problem is the one to report"
    assert "this is not a torn write" in message.lower(), "and the reader is told what it is"


def test_the_event_ceiling_reports_what_it_stopped_at(tmp_path, clock):
    """`LedgerFullError` carries the ceiling and the usage it was measured against, because
    `docket run` prints them: "limit reached" without the numbers is not a usable error.

    A mutated `or` collapsed the byte ceiling in that expression to `0`, and a mutated `int`
    shifted the reported usage, with nothing in the suite reading either field.
    """
    path = tmp_path / "events.jsonl"
    ledger = Ledger.open(path, clock=clock, max_events=2, max_bytes=None)
    ledger.append("note", {"message": "one"})
    ledger.append("note", {"message": "two"})
    with pytest.raises(LedgerFullError) as caught:
        ledger.append("note", {"message": "three"})
    assert caught.value.limit == 2 and caught.value.observed == 2
    assert caught.value.reason == "ledger event limit reached"
    assert str(caught.value) == "ledger event limit reached: 2 >= 2"
    ledger.close()

    small = tmp_path / "bytes.jsonl"
    tight = Ledger.open(small, clock=clock, max_bytes=400)
    with pytest.raises(LedgerFullError) as caught_bytes:
        for _ in range(20):
            tight.append("note", {"message": "x" * 100})
    assert caught_bytes.value.limit == 400, "the byte ceiling is reported in bytes, not collapsed to 0"
    assert caught_bytes.value.observed == tight.bytes_written, "and the usage is the real byte count"
    assert caught_bytes.value.reason == "ledger size limit reached"
    tight.close()


def test_room_for_is_a_boolean_not_a_truthy_stand_in(tmp_path, clock):
    """`_room_for` answers "may this event be recorded?", and its callers ask `is False`-style
    questions through `not`. `None` happens to be falsy today; that is not a contract, and the two
    states it collapses — "no room" and "no answer" — are not the same thing to a caller that
    wants to warn before the ceiling rather than after."""
    path = tmp_path / "room.jsonl"
    ledger = Ledger.open(path, clock=clock, max_events=1, max_bytes=None)
    assert ledger._room_for("note") is True, "there is room for the first event"
    ledger.append("note", {"message": "one"})
    assert ledger._room_for("note") is False, "the ceiling is reached and must be reported as such"
    ledger.append("session.end", {"exit_code": 0}, control=True)  # control events are exempt
    assert ledger._room_for("note") is False, "and the exemption does not open the ceiling"
    ledger.close()

    unbounded = tmp_path / "room-bytes.jsonl"
    bytes_ledger = Ledger.open(unbounded, clock=clock, max_bytes=300)
    assert bytes_ledger._room_for("note") is True
    with pytest.raises(LedgerFullError):
        for _ in range(20):
            bytes_ledger.append("note", {"message": "y" * 100})
    assert bytes_ledger._room_for("note") is False, "no room, reported as a boolean"
    bytes_ledger.close()


def test_degraded_reports_a_broken_record_as_a_boolean():
    """`degraded()` is public and documented as True/False; `docket run` and the receipt both ask
    it whether an event failed to reach disk. A mutated `return bool(...)` that returned `None`
    would be falsy — i.e. it would report a damaged record as healthy — and no test asked."""
    ledger = Ledger.in_memory()
    assert ledger.degraded is False, "a boolean, not a truthy stand-in"
    ledger.unwritten.append({"seq": 0, "type": "note", "error": "OSError: disk full"})
    assert ledger.degraded is True


def test_an_append_that_cannot_open_the_file_still_returns_its_event(tmp_path, clock):
    """The descriptor can be closed under us and the path can stop being a file — an operator, a
    cleanup job, a `rm` and a `mkdir`. The record still has to work: the event is in memory, with
    its payload, in sequence, and the failure is recorded rather than raised.

    `append` returns that event, and a caller uses it (`seq` for the next `prev`, `hash` for a
    receipt). A mutated `return ev` that returned `None` broke exactly the receipt path for a
    degraded run, and a mutated `bool(self.unwritten)` made that run claim to be healthy.
    """
    path = tmp_path / "vanishing.jsonl"
    ledger = Ledger.open(path, clock=clock)
    first = ledger.append("note", {"message": "one"})
    second_written = ledger.append("note", {"message": "one and a half"})
    assert (first.seq, second_written.seq) == (0, 1)

    ledger._handle.close()  # the descriptor goes away, as it does when it is reclaimed
    path.unlink()
    path.mkdir()  # ... and the path is now something a ledger cannot be written to

    second = ledger.append("note", {"message": "two", "payload": "kept"})
    assert second is not None, "the caller must still receive the event it created"
    assert (second.seq, second.prev) == (2, second_written.hash), "the link is to the *last* event, not the first"
    assert second.data == {"message": "two", "payload": "kept"}
    assert second.hash == second.compute_hash()
    assert ledger.events[-1] is second
    assert ledger.degraded is True, "an unwritable ledger reports itself degraded"
    assert ledger.unwritten and "IsADirectoryError" in ledger.unwritten[-1]["error"]
    assert ledger.unwritten[-1]["seq"] == 2 and ledger.unwritten[-1]["type"] == "note"
    assert ledger.chain_error() is None, "the in-memory chain stays intact so a receipt can be built"
    assert ledger.head == second.hash
    ledger.close()


# --------------------------------------------------------------------------- #
# Digests over a file that changed behind the writer's back
# --------------------------------------------------------------------------- #


def test_the_file_digest_is_over_the_bytes_on_disk_even_when_they_differ_from_the_events(tmp_path, clock):
    """`file_sha256` answers "which bytes did this run produce?" — for a receipt, that is the file,
    not the parsed prefix of it. When something else has appended junk, or a writer was killed
    mid-line, the file digest and the digest of the in-memory events are *supposed* to differ, and
    returning the in-memory one would quietly attest to bytes that are not there.

    A mutated `or` sent every existing file down the in-memory branch; the digests agree for a
    clean file, which is why nothing noticed.
    """
    path = tmp_path / "drifted.jsonl"
    _chain(path, 2)
    reopened = Ledger.open(path, clock=clock, max_bytes=None)
    assert len(reopened) == 2

    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"half": ')  # a killed writer leaves bytes the events do not describe
    on_disk = hashlib.sha256(path.read_bytes()).hexdigest()
    assert reopened.file_sha256() == on_disk
    in_memory = hashlib.sha256(
        b"".join(canonical_bytes(event.to_dict()) + b"\n" for event in reopened.events)
    ).hexdigest()
    assert reopened.file_sha256() != in_memory, "the two digests describe different byte strings"
    reopened.close()


def test_a_ledger_with_no_file_digests_its_events(tmp_path, clock):
    """The other half of the same branch: with no file to read, the digest is taken over the bytes
    the events would occupy, so a receipt built from an in-memory ledger is still bound to a
    reproducible string."""
    frozen = lambda: "2026-01-01T00:00:00Z"  # noqa: E731 - a clock that does not move
    in_memory = Ledger.in_memory(clock=frozen)
    in_memory.append("note", {"message": "one"})
    path = tmp_path / "mirror.jsonl"
    on_disk = Ledger.open(path, clock=frozen)
    on_disk.append("note", {"message": "one"})
    assert in_memory.file_sha256() == hashlib.sha256(path.read_bytes()).hexdigest()
    on_disk.close()

    gone = tmp_path / "removed.jsonl"
    vanished = Ledger.open(gone, clock=frozen)
    vanished.append("note", {"message": "one"})
    digest_with_file = vanished.file_sha256()
    gone.unlink()  # the file is deleted while the ledger is still open
    assert vanished.file_sha256() == digest_with_file == in_memory.file_sha256()
    vanished.close()


# --------------------------------------------------------------------------- #
# The chain verdict: what it may and may not claim
# --------------------------------------------------------------------------- #


def test_a_tampered_prefix_behind_a_torn_tail_gets_no_chain_verdict(tmp_path, clock):
    """A file with a torn tail is reported as *torn* — full stop. Running the chain check over its
    prefix as well would let the diagnosis say two things at once ("the last line is incomplete"
    and "the chain is broken at seq 0") about a file whose only proven fact is that it was cut
    short, and `docket doctor` would then be pointing an operator at tampering that is really an
    interrupted write.
    """
    path = tmp_path / "torn-and-tampered.jsonl"
    _chain(path, 3)
    lines = path.read_text(encoding="utf-8").splitlines()
    payload = json.loads(lines[0])
    payload["data"]["message"] = "edited by hand"  # valid JSON, so not a bad_line — a real edit
    lines[0] = json.dumps(payload, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"half": ')

    _events, diagnostics = Ledger.read_with_diagnostics(path)
    assert diagnostics["torn_tail"] is not None
    assert diagnostics["bad_lines"] == []
    assert diagnostics["chain_error"] is None, "no chain verdict is claimed for a torn file"
    assert diagnostics["events"] is None and diagnostics["healthy"] is False

    # The same file without the torn tail *is* a broken chain: the guard suppresses the verdict
    # only while the file is torn, it does not lose the ability to reach one.
    healed = tmp_path / "tampered-only.jsonl"
    torn_bytes = path.read_bytes()[: -len(b'{"half": ')]
    healed.write_bytes(torn_bytes)
    _events, healed_diagnostics = Ledger.read_with_diagnostics(healed)
    assert healed_diagnostics["chain_error"]["seq"] == 0
    assert healed_diagnostics["chain_error"]["reason"].startswith("content hash mismatch")


def test_a_final_line_that_is_not_json_is_a_torn_tail_when_nothing_else_is_wrong(tmp_path, clock):
    """A killed writer usually loses the final newline, but not always: a flush can land a partial
    line *with* its newline. Both shapes are the same event — an interrupted write — so both get
    the same classification, and the recovery path is offered for both.
    """
    path = tmp_path / "torn-with-newline.jsonl"
    _chain(path, 2)
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"type": "note", "seq": 2\n')  # complete-looking end, unparsable line

    events, diagnostics = Ledger.read_with_diagnostics(path)
    assert len(events) == 2
    assert diagnostics["trailing_newline"] is True
    assert diagnostics["bad_lines"] == [], "the final line is an interrupted write, not junk"
    assert diagnostics["torn_tail"] is not None and diagnostics["torn_tail"]["line"] == 3

    # ... and, being a torn tail, it is recoverable by moving those bytes aside.
    result = quarantine_torn_tail(path)
    assert result.performed is True
    assert result.moved_bytes == len('{"type": "note", "seq": 2\n')
    assert Ledger.read_with_diagnostics(path)[1]["healthy"] is True


def test_an_empty_file_is_healthy_and_reports_itself_as_such(tmp_path):
    """A zero-byte file is not a torn write: there is nothing to be cut short. `trailing_newline`
    says so, and the write of the *first* event is what creates the record."""
    path = tmp_path / "zero.jsonl"
    path.write_bytes(b"")
    events, diagnostics = Ledger.read_with_diagnostics(path)
    assert events == []
    assert diagnostics["trailing_newline"] is True
    assert diagnostics["healthy"] is True and diagnostics["events"] == 0
    assert diagnostics["chain_error"] is None


def test_read_names_the_first_bad_line_and_the_first_torn_tail(tmp_path, clock):
    """When a file has several problems, the error names the *first* one: that is the line an
    operator opens the file at. A mutated index reported the second, which sends the reader past
    the actual start of the damage."""
    path = tmp_path / "two-bad.jsonl"
    _chain(path, 4)
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[1] = "not json at all"
    lines[3] = "also not json"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"line 2 is not valid JSON"):
        Ledger.read(path)


def test_a_chain_error_locates_an_event_whose_payload_cannot_be_canonicalised(tmp_path, clock):
    """Canonicalisation is a promise about *integers and decimal strings*, so a damaged line can
    carry a value the canonical form refuses. The chain check reports that as damage at a known
    sequence number instead of raising — the diagnostics reader's whole job is to classify broken
    files. A mutated return replaced the verdict with `None`, i.e. with "the chain is fine"."""
    path = tmp_path / "unhashable.jsonl"
    _chain(path, 2)
    lines = path.read_text(encoding="utf-8").splitlines()
    payload = json.loads(lines[1])
    payload["data"]["cost_usd"] = 1.5  # a float is not a canonical number
    lines[1] = json.dumps(payload, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    _events, diagnostics = Ledger.read_with_diagnostics(path)
    error = diagnostics["chain_error"]
    assert error is not None, "a payload the canonical form refuses is damage, not a clean chain"
    assert error["seq"] == 1
    assert "cannot be canonicalised" in error["reason"]
    assert diagnostics["bad_lines"] == [], "the line is JSON, so it is not junk"
    assert diagnostics["healthy"] is False


def test_chain_error_reports_a_broken_link_between_two_intact_events(clock):
    """Kills `return (ev.seq, "broken link: ...") -> return None` in `chain_error`.

    `None` is this function's answer for "the chain is intact", so a mutant that returns `None` for a
    broken link reports a ledger whose events do not follow one another as whole. The other three
    branches are covered — a sequence gap, an uncanonicalisable payload, a content hash — and this one
    is the fourth: sequence and hash both agree, and only the *link* is wrong, which is what a file
    edited by hand (or stitched together from two runs) looks like.
    """
    ledger = Ledger.in_memory(clock=clock)
    ledger.append("note", {"message": "first"})
    second = ledger.append("note", {"message": "second"})
    assert ledger.chain_error() is None

    second.prev = "f" * 64  # the link is wrong; the seq and the payload are untouched
    assert second.seq == 1 and second.data == {"message": "second"}

    problem = ledger.chain_error()
    assert problem is not None, "a broken link is damage, not a clean chain"
    assert problem[0] == second.seq == 1
    assert problem[1].startswith("broken link: prev=ffffffffffff"), problem[1]


def test_chain_error_reports_an_event_that_cannot_be_canonicalised(clock):
    """`Ledger.chain_error()` is asked about a *live* ledger — by `docket run` before it writes a
    receipt, and by the boundary's health endpoint. An event whose payload the canonical form
    refuses (a float, a set, an object) is damage like any other: it must come back as a located
    problem, not as `None` ("the chain is fine") and not as a raised exception.

    The file-based reader has its own copy of this check (`read_with_diagnostics`), which is why a
    mutant in `chain_error` survived a test that went through a file.
    """
    ledger = Ledger.in_memory(clock=clock)
    ledger.append("note", {"message": "fine"})
    broken = ledger.append("note", {"message": "will be damaged"})
    assert ledger.chain_error() is None
    broken.data["cost_usd"] = 1.5  # a float is not a canonical number

    problem = ledger.chain_error()
    assert problem is not None, "an event whose payload cannot be hashed is a chain problem"
    assert problem[0] == broken.seq == 1
    assert problem[1].startswith("the event's data cannot be canonicalised")
    assert "1.5 cannot appear in a hashed structure" in problem[1], "the reason quotes the rule"

    # And the check is a check of the *sealed* bytes: restoring a canonical value without
    # re-sealing leaves the event modified, which is the other, louder finding.
    broken.data["cost_usd"] = "1.50"
    assert ledger.chain_error() == (1, "content hash mismatch (event was modified after it was written)")


def test_the_torn_tail_message_counts_the_bytes_and_names_the_line(tmp_path, clock):
    """The refusal message is what an operator reads to decide whether to quarantine, so it has
    to be right: how many bytes are stranded, and on which line. A mutated constant in that
    sentence has no test of its own — receipts quote it, humans act on it."""
    path = tmp_path / "msg.jsonl"
    _chain(path, 3)
    torn = '{"partial": tru'
    with path.open("a", encoding="utf-8") as handle:
        handle.write(torn)
    with pytest.raises(LedgerDamagedError) as caught:
        Ledger.open(path, clock=clock)
    message = str(caught.value)
    assert f"({len(torn)} bytes after line 3)" in message
    assert "docket doctor --quarantine-torn-tail" in message
    assert str(path) in message


# --------------------------------------------------------------------------- #
# Handles, locks, and what the process holds
# --------------------------------------------------------------------------- #


def test_appending_does_not_reopen_a_healthy_descriptor(tmp_path, clock):
    """Each append checks that its descriptor is still usable. If that check is wrong in the
    permissive direction it reopens (or drops) a perfectly good handle on every event: the
    descriptor the ledger is holding changes identity under it, and the "closed under us" repair
    path starts firing on healthy files."""
    path = tmp_path / "steady.jsonl"
    ledger = Ledger.open(path, clock=clock)
    handle = ledger._handle
    assert handle is not None
    for index in range(3):
        ledger.append("note", {"index": index})
        assert ledger._handle is handle, "the descriptor is the same object after every append"
    ledger.close()
    assert len(Ledger.read(path)) == 3


def test_the_descriptor_is_not_inheritable_by_a_forked_child(tmp_path, clock):
    """`_open_ledger_handle` clears the inheritable flag so a forked child cannot hold the ledger
    — and its lock — open after the parent exits. That flag is invisible from the ledger's own
    behaviour, so if it were lost the run would keep a lock nobody could explain.

    A mutated `False` set it to inheritable; nothing in the suite looked at the flag.
    """
    path = tmp_path / "fd.jsonl"
    ledger = Ledger.open(path, clock=clock)
    try:
        assert os.get_inheritable(ledger._handle.fileno()) is False
    finally:
        ledger.close()


def test_a_locked_ledger_names_the_process_holding_it(tmp_path, clock):
    """The second writer's error is the *only* thing an operator gets, and it must name the
    holder's pid: "already held by another Docket process (pid unknown)" turns a one-line fix into
    an investigation. A mutated fallback in that read replaced the pid with `unknown` on every
    POSIX refusal."""
    path = tmp_path / "held.jsonl"
    first = Ledger.open(path, clock=clock)
    first.append("note", {"message": "holding"})
    try:
        with pytest.raises(LedgerLockedError) as caught:
            Ledger.open(path, clock=clock)
        assert f"(pid {os.getpid()})" in str(caught.value), "the holder is named, not guessed at"
        assert "--docket-dir" in str(caught.value), "and the message says what to do about it"
    finally:
        first.close()

    # Once the holder closes, the lock is gone and the next writer proceeds normally.
    second = Ledger.open(path, clock=clock)
    second.append("note", {"message": "after"})
    second.close()
    assert len(Ledger.read(path)) == 2


def test_the_lock_is_held_on_the_file_descriptor_and_released_on_close(tmp_path, clock):
    """Two writers are refused by an OS lock on a descriptor this process holds, so the descriptor
    *is* the lock. A mutated `_acquire_lock` that returned `None` would leave the marker file
    behind after `close()` — a stale lock with a live pid, i.e. a directory that can never be
    written to again by any process including the one that made it."""
    path = tmp_path / "locked.jsonl"
    ledger = Ledger.open(path, clock=clock)
    lock_path = Path(str(path) + ".lock")
    assert ledger._lock_handle is not None, "the lock is a descriptor this process holds"
    assert lock_path.read_text(encoding="utf-8").strip() == str(os.getpid())
    ledger.close()
    assert not lock_path.exists(), "closing releases the marker as well as the lock"

    # And the directory is reusable, which is the user-visible consequence.
    again = Ledger.open(path, clock=clock)
    again.close()


def test_a_dead_process_is_not_alive_and_this_one_is(tmp_path):
    """Stale-lock recovery is only as good as its liveness probe, and a probe that errs towards
    "dead" is how two writers end up in one chain. `os.kill(pid, 0)` is the POSIX idiom; this test
    pins both answers on a pid that is certainly alive and one that is certainly not."""
    assert _pid_alive(os.getpid()) is True
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert _pid_alive(child.pid) is False, "a reaped child is not a live holder"
    assert _pid_alive(2**31 - 1) is False, "a pid no kernel hands out is not a live holder"


def test_the_liveness_probe_asks_about_a_process_without_signalling_it(tmp_path):
    """Kills `os.kill(pid, 0) -> os.kill(pid, 1)` in `_pid_alive` — a probe that becomes a signal.

    Signal 0 is the POSIX idiom for "does this pid exist"; signal 1 is `SIGHUP`, whose default action
    is to terminate. The mutant answers the question correctly and leaves the process it asked about
    dead, which is the same defect class as the Windows probe recounted in `docs/testing.md` (where
    `os.kill` *is* `TerminateProcess`): a liveness check with a blast radius.

    The existing test could not see it, and the reason is worth keeping: the whole mutation sweep
    runs under `nohup`, which ignores `SIGHUP` and passes that disposition to every child, so in the
    harness's environment the signal is a no-op. A test that relied on the default disposition would
    be a test of the harness's process flags. So the child installs its own handler and creates a
    marker file *inside* it: the file exists if and only if a `SIGHUP` was delivered, in an
    environment that would otherwise swallow it.
    """
    marker = tmp_path / "signalled.txt"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal, sys, time\n"
            f"path = {str(marker)!r}\n"
            "signal.signal(signal.SIGHUP, lambda *_: (open(path, 'w').write('sighup'), sys.exit(3)))\n"
            "print('ready', flush=True)\n"
            "time.sleep(30)\n",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "ready"
        assert _pid_alive(child.pid) is True, "a running child is a live holder"
        time.sleep(0.3)
        assert not marker.exists(), "the probe delivered a signal to the process it asked about"
        assert child.poll() is None, "the probe terminated the process it asked about"
    finally:
        child.kill()
        child.wait(timeout=5)


def test_a_stale_lock_file_does_not_block_a_new_writer(tmp_path, clock):
    """The marker file survives a crash (`kill -9`), and the next run has to be able to take it
    over: the flock dies with the process, so a leftover marker is not a lock at all."""
    path = tmp_path / "stale.jsonl"
    Path(str(path) + ".lock").write_text("999999", encoding="utf-8")  # a pid with no process
    ledger = Ledger.open(path, clock=clock)
    assert ledger._lock_handle is not None
    ledger.append("note", {"message": "recovered"})
    ledger.close()
    assert len(Ledger.read(path)) == 1


def test_a_symlinked_lock_file_is_refused(tmp_path, clock):
    """The lock file sits in a directory an attacker may be able to write. A lock that is a
    symlink would have Docket truncate and write through it — anywhere the attacker chose."""
    target = tmp_path / "real-lock"
    target.write_text("", encoding="utf-8")
    path = tmp_path / "link.jsonl"
    Path(str(path) + ".lock").symlink_to(target)
    with pytest.raises(LedgerDamagedError, match="lock file is a symbolic link"):
        Ledger.open(path, clock=clock)
    assert target.read_text(encoding="utf-8") == "", "the link's target was not written through"


# --------------------------------------------------------------------------- #
# The repair path
# --------------------------------------------------------------------------- #


def test_the_sidecar_is_named_for_the_moment_and_a_collision_gets_a_suffix(tmp_path, clock):
    """One sidecar per recovery, never two recoveries in one file, never an overwrite.

    The timestamp comes from the caller when it is given one and from the clock when it is not,
    and a name that is already taken gets `-2`, `-3`, ... A mutated counter started the numbering
    at the wrong place and a mutated `return now` ignored the caller's timestamp — both invisible
    while the collision branch had no test.
    """
    path = tmp_path / "collide.jsonl"
    _chain(path, 2)
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"half": ')

    taken = Path(str(path) + ".torn-20260101T000000Z")
    taken.write_text("an earlier recovery\n", encoding="utf-8")
    result = quarantine_torn_tail(path, now="20260101T000000Z")
    assert result.performed is True
    assert result.sidecar == str(path) + ".torn-20260101T000000Z-2", "the caller's stamp, and the next free name"
    assert taken.read_text(encoding="utf-8") == "an earlier recovery\n", "the first sidecar is untouched"
    assert Path(result.sidecar).read_bytes().endswith(b'{"half": ')

    # Without a stamp the name carries the time of the recovery, in the documented format.
    second = tmp_path / "clocked.jsonl"
    _chain(second, 1)
    with second.open("a", encoding="utf-8") as handle:
        handle.write("{")
    fresh = quarantine_torn_tail(second)
    assert re.fullmatch(rf"{re.escape(str(second))}\.torn-\d{{8}}T\d{{6}}Z", fresh.sidecar or ""), fresh.sidecar


def test_quarantine_keeps_every_byte_and_reports_both_sides(tmp_path, clock):
    """The invariant that makes the repair admissible at all: nothing is lost. `kept_bytes` and
    `moved_bytes` are the numbers an operator checks that claim against, and they have to add up
    to the size of the damaged file."""
    path = tmp_path / "counted.jsonl"
    _chain(path, 3)
    torn = '{"seq": 3, "type": "note"'
    with path.open("a", encoding="utf-8") as handle:
        handle.write(torn)
    before = path.read_bytes()

    result = quarantine_torn_tail(path)
    assert result.kept_bytes + result.moved_bytes == len(before)
    assert result.kept_bytes == len(before) - len(torn)
    sidecar = Path(result.sidecar or "")
    assert sidecar.read_bytes().endswith(torn.encode("utf-8"))
    assert path.read_bytes() == before[: result.kept_bytes]


def test_quarantine_refuses_a_file_that_does_not_exist(tmp_path):
    """A missing ledger is not a torn tail, and the repair path must say why rather than creating
    a sidecar for a file that was never written."""
    missing = tmp_path / "absent.jsonl"
    result = quarantine_torn_tail(missing)
    assert result.performed is False
    assert "does not exist" in result.reason, "a file that was never written is not a torn tail"
    assert missing.exists() is False, "and the repair path does not create a ledger to repair"
    assert not list(tmp_path.glob("*.torn-*"))


def test_appending_to_a_repaired_ledger_continues_the_chain(tmp_path, clock):
    """Recovery has to leave a file the writer will accept — otherwise the only repair the tool
    offers ends in a refusal. This is the end-to-end shape of `docket doctor --quarantine-torn-tail`
    followed by the next run."""
    path = tmp_path / "resume.jsonl"
    written = _chain(path, 3)
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"partial": tru')

    assert quarantine_torn_tail(path).performed is True
    resumed = Ledger.open(path, clock=clock)
    event = resumed.append("note", {"message": "after the repair"})
    assert (event.seq, event.prev) == (3, written.head), "the chain continues from the intact head"
    resumed.close()
    events, diagnostics = Ledger.read_with_diagnostics(path)
    assert diagnostics["healthy"] is True and len(events) == 4


# --------------------------------------------------------------------------- #
# The record under concurrent appends
# --------------------------------------------------------------------------- #


def test_parallel_appends_keep_the_file_in_sequence_order(tmp_path, clock):
    """`test_ledger_concurrency.py` checks this from the outside; this is the assertion the receipt
    path depends on, stated once for the record: a file written by N threads parses, hashes and
    links as one chain, and its `prev` links are the hashes of the events next to them."""
    path = tmp_path / "threaded.jsonl"
    ledger = Ledger.open(path, clock=clock, max_bytes=None)
    per_thread = 25
    workers = 4

    def writer(worker: int) -> None:
        for index in range(per_thread):
            ledger.append("note", {"worker": worker, "index": index})

    threads = [threading.Thread(target=writer, args=(worker,)) for worker in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    ledger.close()

    events, diagnostics = Ledger.read_with_diagnostics(path)
    assert len(events) == per_thread * workers
    assert diagnostics["healthy"] is True
    assert [event.seq for event in events] == list(range(len(events)))
    assert {event.data["worker"] for event in events} == set(range(workers))
    assert [event.data["index"] for event in events if event.data["worker"] == 0] == list(range(per_thread))


def test_a_ledger_and_its_receipt_agree_about_the_head(tmp_path, clock):
    """`Ledger.head` is what a receipt binds to. It is the last event's hash, from genesis when
    there are no events — the two values a verifier re-derives. A mutant returning `None` for the
    empty case, or for the general case, would put a receipt and its ledger out of step."""
    empty = Ledger.in_memory(clock=clock)
    assert empty.head == GENESIS
    assert empty.merkle_root() == "" or isinstance(empty.merkle_root(), str)

    path = tmp_path / "head.jsonl"
    ledger = Ledger.open(path, clock=clock)
    first = ledger.append("note", {"message": "one"})
    assert ledger.head == first.hash
    second = ledger.append("note", {"message": "two"})
    assert ledger.head == second.hash == ledger.events[-1].hash
    ledger.close()
    assert Ledger.read(path)[-1].hash == second.hash


def test_the_merkle_root_follows_the_events(tmp_path, clock):
    """The receipt's Merkle root is computed from the event hashes in order: same events, same
    root; one more event, a different root. This is the value the receipt's usage proofs are built
    against, so it must not be a constant or a stale copy."""
    frozen = lambda: "2026-01-01T00:00:00Z"  # noqa: E731 - a clock that does not move
    ledger = Ledger.in_memory(clock=frozen)
    roots = [ledger.merkle_root()]
    for index in range(4):
        ledger.append("note", {"index": index})
        roots.append(ledger.merkle_root())
    assert len(set(roots)) == len(roots), "each append changes the root"

    twin = Ledger.in_memory(clock=frozen)
    for index in range(4):
        twin.append("note", {"index": index})
    assert twin.merkle_root() == roots[-1], "the same events produce the same root"

    other = Ledger.in_memory(clock=frozen)
    for index in range(4):
        other.append("note", {"index": index + 100})
    assert other.merkle_root() != roots[-1], "different events produce a different root"
