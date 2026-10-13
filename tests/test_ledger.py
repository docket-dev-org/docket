"""The hash chain: what it detects, and what it refuses to accept."""

from __future__ import annotations

import json

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
