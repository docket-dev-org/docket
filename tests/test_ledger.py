"""The hash chain: what it detects, and what it refuses to accept."""

from __future__ import annotations

import hashlib
import json

import pytest

from docket.ledger import GENESIS, Ledger, count_events


def test_genesis_is_the_sha256_of_the_genesis_string():
    from docket.canon import sha256_hex

    assert sha256_hex("docket/genesis/v1") == GENESIS


def test_first_event_links_to_genesis(ledger):
    event = ledger.append("note", {"message": "hello"})
    assert event.seq == 0
    assert event.prev == GENESIS
    assert event.hash == event.compute_hash()
    assert ledger.head == event.hash
    assert ledger.chain_error() is None


def test_chain_links_each_event_to_its_predecessor(ledger):
    first = ledger.append("note", {"message": "one"})
    second = ledger.append("note", {"message": "two"})
    assert second.prev == first.hash
    assert second.hash != first.hash
    assert ledger.chain_error() is None


def test_chain_detects_a_modified_payload(ledger):
    ledger.append("model.response", {"model": "claude-sonnet-5", "cost_usd": "0.01"})
    ledger.append("session.end", {"exit_code": 0})
    ledger.events[0].data["model"] = "gpt-6-luna"
    problem = ledger.chain_error()
    assert problem is not None
    assert problem[0] == 0
    assert "content hash mismatch" in problem[1]


def test_chain_detects_reordering(ledger):
    ledger.append("note", {"message": "one"})
    ledger.append("note", {"message": "two"})
    ledger.events.reverse()
    problem = ledger.chain_error()
    assert problem is not None
    assert problem[0] == 0


def test_chain_detects_a_missing_event(ledger):
    ledger.append("note", {"message": "one"})
    ledger.append("note", {"message": "two"})
    ledger.append("note", {"message": "three"})
    del ledger.events[1]
    problem = ledger.chain_error()
    assert problem is not None
    assert problem[0] == 1


def test_chain_detects_a_relinked_deletion(ledger):
    """Deleting the last event and repairing the head still leaves the count wrong."""
    ledger.append("note", {"message": "one"})
    ledger.append("note", {"message": "two"})
    head_before = ledger.head
    ledger.events.pop()
    assert ledger.head != head_before  # the head moves, so a receipt no longer matches


def test_ledger_persists_and_reloads(tmp_path, clock):
    path = tmp_path / "ledger.jsonl"
    led = Ledger.open(path, clock=clock)
    led.append("session.start", {"argv": ["x"]})
    led.append("session.end", {"exit_code": 0})
    head = led.head
    file_digest = led.file_sha256()
    led.close()

    reloaded = Ledger.read(path)
    assert len(reloaded) == 2
    assert reloaded[-1].hash == head
    assert Ledger(events=reloaded).chain_error() is None
    assert Ledger(events=reloaded).file_sha256() == file_digest


def test_file_digest_changes_when_the_file_changes(tmp_path, clock):
    path = tmp_path / "ledger.jsonl"
    led = Ledger.open(path, clock=clock)
    led.append("note", {"message": "one"})
    before = led.file_sha256()
    led.append("note", {"message": "two"})
    assert led.file_sha256() != before
    led.close()


def test_unknown_event_types_are_preserved(ledger):
    ledger.append("x-vendor.custom", {"anything": [1, 2, 3]})
    assert ledger.events[0].data["anything"] == [1, 2, 3]
    assert ledger.chain_error() is None


def test_counts(ledger):
    ledger.append("model.response", {})
    ledger.append("model.response", {})
    ledger.append("tool.call", {})
    assert count_events(ledger, "model.response") == 2
    assert ledger.types() == {"model.response": 2, "tool.call": 1}


def test_written_lines_are_valid_json_and_sorted_keys(tmp_path, clock):
    path = tmp_path / "ledger.jsonl"
    led = Ledger.open(path, clock=clock)
    led.append("note", {"z": 1, "a": 2})
    led.close()
    line = path.read_text(encoding="utf-8").strip()
    payload = json.loads(line)
    assert payload["type"] == "note"
    assert list(payload.keys()) == sorted(payload.keys())
    assert list(payload["data"].keys()) == sorted(payload["data"].keys())


# --------------------------------------------------------------------------- #
# The public surface: contexts, sizes, and what a log line may contain
# --------------------------------------------------------------------------- #


def test_a_ledger_is_a_context_manager_that_closes_and_releases(tmp_path, clock):
    """`with Ledger.open(...) as led:` is the documented way to use it, and the *binding* is the
    contract: entering has to produce the ledger, not `None`. Exiting closes the file, releases
    the lock, and lets an exception through — a context manager that swallowed one would hide the
    failure the run is being recorded for."""
    path = tmp_path / "ctx.jsonl"
    with Ledger.open(path, clock=clock) as led:
        assert isinstance(led, Ledger)
        led.append("note", {"message": "inside"})
        assert led._handle is not None
    assert led._handle is None, "the context manager must close the handle"
    assert not path.with_suffix(".jsonl.lock").exists(), "and release the lock"
    assert Ledger.read(path)[0].data == {"message": "inside"}

    with pytest.raises(RuntimeError, match="boom"), Ledger.open(tmp_path / "ctx2.jsonl", clock=clock) as led2:
        led2.append("note", {})
        raise RuntimeError("boom")

    assert led2._handle is None, "an exception must still close the ledger"


def test_len_iteration_and_truthiness_report_the_record(tmp_path, clock):
    """`len(ledger)` is used by the boundary's health endpoint and by callers deciding whether a
    run recorded anything; a `None` from `__len__` is a `TypeError` there, and an untested
    `__len__` is one refactor away from it."""
    path = tmp_path / "count.jsonl"
    led = Ledger.open(path, clock=clock)
    assert len(led) == 0 and not led
    for index in range(3):
        led.append("note", {"index": index})
    assert len(led) == 3
    assert bool(led) is True
    assert [event.seq for event in led] == [0, 1, 2]
    assert [event.data["index"] for event in led] == [0, 1, 2]
    led.close()


def test_the_repr_of_a_ledger_is_safe_to_put_in_a_log(tmp_path, clock):
    """A ledger's `repr` shows up in test output and CI logs. The append lock is an implementation
    detail with a memory address in it; the dataclass field is declared `repr=False` for that
    reason, and this pins the reason rather than the field."""
    path = tmp_path / "repr.jsonl"
    led = Ledger.open(path, clock=clock)
    led.append("note", {"message": "x"})
    text = repr(led)
    assert " at 0x" not in text, f"a memory address or handle leaked into the repr: {text}"
    assert "Lock" not in text, f"the append lock is plumbing, not state: {text}"
    assert "_handle" not in text and "clock=" not in text, f"a descriptor leaked into the repr: {text}"
    assert "repr.jsonl" in text and "bytes_written=" in text and "events=[Event(seq=0" in text
    assert (
        repr(Ledger.in_memory(clock=clock)) == "Ledger(path=None, events=[], guard=None, max_events=None, "
        "max_bytes=268435456, bytes_written=0, unwritten=[])"
    )
    led.close()


def test_the_lock_file_holds_exactly_the_holder_pid(tmp_path, clock):
    """Stale-lock detection reads this file and asks whether the pid is alive.

    If the file contains anything but the pid — a stray byte from an off-by-one seek, a
    suffix, a newline-separated list — the check silently stops working and two writers can be
    admitted to one chain. `tools/mutate.py` moved the write position by one byte and no test
    noticed, because nothing looked at the file's contents.
    """
    import os as _os

    path = tmp_path / "lock.jsonl"
    led = Ledger.open(path, clock=clock)
    lock_path = path.with_suffix(".jsonl.lock")
    assert lock_path.exists()
    assert lock_path.read_text(encoding="utf-8").strip() == str(_os.getpid())
    led.close()
    assert not lock_path.exists()


def test_an_empty_existing_file_is_a_new_ledger(tmp_path, clock):
    """A zero-byte file is not "an existing ledger to resume": there is no chain to continue.

    The distinction is `size > 0`, and getting it wrong in the other direction (treating an empty
    file as a chain to resume) is harmless *today* only because an empty file has no events; the
    test states which branch is intended so a later change to the resume logic has something to
    break.
    """
    path = tmp_path / "empty.jsonl"
    path.write_bytes(b"")
    led = Ledger.open(path, clock=clock)
    assert led.bytes_written == 0, "nothing has been written, and the file is empty"
    assert led.events == []
    event = led.append("note", {"message": "first"})
    assert (event.seq, event.prev) == (0, GENESIS), "the first event of an empty file starts a chain"
    assert led.bytes_written == len(path.read_bytes())
    led.close()
    assert len(Ledger.read(path)) == 1


def test_an_in_memory_ledger_has_a_digest_over_its_events(tmp_path, clock):
    """`file_sha256` answers "which bytes am I attesting to?" — including when there is no file.

    An in-memory ledger (the SDK's and the tests' favourite) must produce the digest of exactly
    the bytes its events would occupy on disk, so a receipt can bind to either. A mutated `or` in
    that guard sent an in-memory ledger down the path that reads a file that does not exist.
    """
    frozen = lambda: "2026-01-01T00:00:00Z"  # noqa: E731 - a clock that does not move, named for the log
    led = Ledger.in_memory(clock=frozen)
    assert led.path is None
    empty_digest = led.file_sha256()
    led.append("note", {"message": "one"})
    assert led.file_sha256() != empty_digest

    path = tmp_path / "materialised.jsonl"
    on_disk = Ledger.open(path, clock=frozen)
    on_disk.append("note", {"message": "one"})
    assert on_disk.file_sha256() == led.file_sha256(), "the in-memory digest is the file digest"
    assert led.file_sha256() == hashlib.sha256(path.read_bytes()).hexdigest()
    on_disk.close()
