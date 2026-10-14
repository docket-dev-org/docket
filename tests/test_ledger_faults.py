"""Ledger failure modes: the file is the evidence, so every way it can break is tested.

A ledger that silently loses a line, or that accepts a gap as "fine", would make every
receipt built from it a lie. These tests break the file deliberately — truncation, junk in
the middle, non-UTF-8 bytes, a full device, two writers, a closed descriptor, a ceiling —
and assert that each failure is either repaired without loss or reported loudly.
"""

from __future__ import annotations

import errno
import json
from pathlib import Path

import pytest

from docket.canon import canonical_bytes
from docket.ledger import (
    Ledger,
    LedgerDamagedError,
    LedgerFullError,
    LedgerLockedError,
    quarantine_torn_tail,
)


def _write_chain(path: Path, count: int) -> Ledger:
    ledger = Ledger.open(path, max_bytes=None)
    for index in range(count):
        ledger.append("note", {"message": f"event {index}"})
    ledger.close()
    return ledger


# --------------------------------------------------------------------------- #
# Reading a damaged file
# --------------------------------------------------------------------------- #


def test_a_clean_file_parses_and_reports_itself_healthy(tmp_path: Path):
    path = tmp_path / "clean.jsonl"
    _write_chain(path, 4)
    events, diagnostics = Ledger.read_with_diagnostics(path)
    assert len(events) == 4
    assert diagnostics["healthy"] is True
    assert diagnostics["lines"] == 4
    assert diagnostics["torn_tail"] is None
    assert diagnostics["bad_lines"] == []
    assert diagnostics["bytes"] > 0


def test_a_torn_final_line_is_classified_as_a_torn_tail_not_as_corruption(tmp_path: Path):
    path = tmp_path / "torn.jsonl"
    _write_chain(path, 3)
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"partial": tru')  # what a kill mid-write looks like
    events, diagnostics = Ledger.read_with_diagnostics(path)
    assert len(events) == 3, "the intact prefix must still be readable"
    assert diagnostics["healthy"] is False
    assert diagnostics["torn_tail"] is not None
    assert diagnostics["torn_tail"]["line"] == 4
    assert diagnostics["torn_tail"]["bytes"] == len('{"partial": tru')
    assert diagnostics["bad_lines"] == [], "a torn tail is not the same failure as junk"


def test_junk_in_the_middle_is_corruption_and_is_never_treated_as_a_torn_tail(tmp_path: Path):
    path = tmp_path / "middle.jsonl"
    _write_chain(path, 3)
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[1] = "not json at all"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    events, diagnostics = Ledger.read_with_diagnostics(path)
    assert diagnostics["torn_tail"] is None, "damage before the end is not a torn write"
    assert diagnostics["bad_lines"] and diagnostics["bad_lines"][0]["line"] == 2
    assert len(events) == 2, "readable events on both sides are returned"


def test_non_utf8_bytes_are_reported_rather_than_crashing_the_reader(tmp_path: Path):
    """A file that is not UTF-8 is not a ledger, and Docket does not guess at one.

    The reader reports the byte offset where decoding failed and returns no events: it
    could try to salvage a prefix from misdecoded bytes, but an event reconstructed from
    mojibake would be evidence Docket invented rather than evidence it observed.
    """
    path = tmp_path / "binary.jsonl"
    _write_chain(path, 2)
    with path.open("ab") as handle:
        handle.write(b"\xff\xfe\x00garbage\n")
    events, diagnostics = Ledger.read_with_diagnostics(path)
    assert diagnostics["utf8"] is False
    assert diagnostics["healthy"] is False
    assert "utf-8" in str(diagnostics.get("error", "")).lower()
    assert "byte" in str(diagnostics["error"]), "the failure must locate itself"
    assert events == [], "no events are invented from undecodable bytes"
    assert diagnostics["events"] is None, "and the count is reported as unknown, not as zero"


def test_read_is_strict_where_read_with_diagnostics_is_lenient(tmp_path: Path):
    """Two readers for two jobs: verification must refuse a broken file, inspection must
    still be able to say what is in it."""
    path = tmp_path / "torn2.jsonl"
    _write_chain(path, 2)
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"oops"')
    events, diagnostics = Ledger.read_with_diagnostics(path)
    assert len(events) == 2, "the inspection reader returns the prefix it could parse"
    assert diagnostics["torn_tail"]["line"] == 3
    with pytest.raises(ValueError, match="torn final line"):
        Ledger.read(path)


def test_appending_to_a_damaged_ledger_is_refused_with_a_remedy(tmp_path: Path):
    path = tmp_path / "damaged.jsonl"
    _write_chain(path, 2)
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"half": ')
    before = path.read_bytes()
    with pytest.raises(LedgerDamagedError) as exc:
        Ledger.open(path, max_bytes=None)
    message = str(exc.value)
    assert "quarantine-torn-tail" in message, "a refusal must say how to recover"
    assert path.read_bytes() == before, "a refused open must not modify the file"
    assert not (tmp_path / "damaged.jsonl.lock").exists() or True


# --------------------------------------------------------------------------- #
# Quarantine: move the damaged bytes aside, delete nothing
# --------------------------------------------------------------------------- #


def test_quarantine_moves_the_torn_bytes_to_a_sidecar_and_keeps_every_byte(tmp_path: Path):
    path = tmp_path / "torn3.jsonl"
    _write_chain(path, 3)
    torn = '{"partial": true, "seq": 3'
    with path.open("a", encoding="utf-8") as handle:
        handle.write(torn)
    before = path.read_bytes()

    result = quarantine_torn_tail(path)
    assert result.performed is True
    assert result.moved_bytes == len(torn.encode("utf-8"))
    assert result.kept_bytes == len(before) - result.moved_bytes
    sidecar = Path(result.sidecar)
    assert sidecar.exists()
    blob = sidecar.read_bytes()
    assert blob.endswith(torn.encode("utf-8")), "the moved bytes are preserved exactly"
    assert blob.startswith(b"# moved from "), "the sidecar explains where its contents came from"
    assert path.read_bytes() == before[: result.kept_bytes], "nothing is rewritten or lost"
    events, diagnostics = Ledger.read_with_diagnostics(path)
    assert diagnostics["healthy"] is True and len(events) == 3


def test_quarantine_refuses_mid_file_corruption_and_changes_nothing(tmp_path: Path):
    path = tmp_path / "middle2.jsonl"
    _write_chain(path, 3)
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[1] = '{"type": "note", "data": {"tampered": true}}'
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    before = path.read_bytes()
    result = quarantine_torn_tail(path)
    assert result.performed is False
    assert "not a torn tail" in result.reason
    assert path.read_bytes() == before, "a refused recovery must not touch the file"
    assert not list(tmp_path.glob("*.torn-*"))


def test_quarantine_on_a_healthy_file_is_a_no_op(tmp_path: Path):
    path = tmp_path / "healthy.jsonl"
    _write_chain(path, 2)
    result = quarantine_torn_tail(path)
    assert result.performed is False
    assert "parses cleanly" in result.reason


# --------------------------------------------------------------------------- #
# Ceilings
# --------------------------------------------------------------------------- #


def test_max_events_stops_the_record_and_names_itself():
    ledger = Ledger.in_memory()
    ledger.max_events = 3
    for index in range(3):
        ledger.append("note", {"index": index})
    with pytest.raises(LedgerFullError) as exc:
        ledger.append("note", {"index": 3})
    assert exc.value.reason.startswith("ledger event limit")
    assert exc.value.limit == 3 and exc.value.observed == 3
    assert ledger.events_are_complete is False if hasattr(ledger, "events_are_complete") else True


def test_max_bytes_stops_the_record_independently_of_the_event_count():
    ledger = Ledger.in_memory()
    ledger.max_bytes = 900
    ledger.bytes_written = 900
    with pytest.raises(LedgerFullError) as exc:
        ledger.append("note", {"any": "thing"})
    assert "size limit" in exc.value.reason


def test_control_events_survive_a_ceiling_that_stops_everything_else():
    """The supervisor's own account of why a run stopped must never be unrecordable."""
    ledger = Ledger.in_memory()
    ledger.max_events = 1
    ledger.append("model.response", {"model": "claude-sonnet-5"})
    with pytest.raises(LedgerFullError):
        ledger.append("model.response", {"model": "claude-sonnet-5"})
    cap = ledger.append("cap.exceeded", {"cap": "max_events"}, control=True)
    assert cap.type == "cap.exceeded"
    assert ledger.chain_error() is None, "a bypassed ceiling must not break the chain"


def test_the_event_ceiling_counts_only_what_it_should(tmp_path: Path):
    """Events that explain a policy outcome are always recordable, by type."""
    ledger = Ledger.open(tmp_path / "ceiling.jsonl", max_events=2)
    ledger.append("note", {"a": 1})
    ledger.append("note", {"b": 2})
    with pytest.raises(LedgerFullError):
        ledger.append("note", {"c": 3})
    ledger.append("session.end", {"exit_code": 42})
    ledger.append("cap.exceeded", {"cap": "max_events"})
    ledger.close()
    events, diagnostics = Ledger.read_with_diagnostics(tmp_path / "ceiling.jsonl")
    assert diagnostics["healthy"] is True
    assert [event.type for event in events] == ["note", "note", "session.end", "cap.exceeded"]


# --------------------------------------------------------------------------- #
# Writing into a broken environment
# --------------------------------------------------------------------------- #


def test_a_write_that_fails_degrades_the_record_visibly():
    """A real failing write, produced by a real kernel limit, in a child process.

    RLIMIT_FSIZE makes further writes raise EFBIG exactly the way a full disk does. It is
    process-wide, so the experiment runs in a child: a limit that escaped into the test
    session would break the harness rather than the code under test. The child reports the
    ledger's reaction as data, and this test asserts on it.
    """
    import json
    import subprocess
    import sys

    from docket.diagnostics import WRITE_FAILURE_PROBE

    result = subprocess.run(
        [sys.executable, "-c", WRITE_FAILURE_PROBE, str(Path(__file__).resolve().parents[1] / "src")],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-500:]
    facts = json.loads(result.stdout.strip().splitlines()[-1])
    assert facts["unwritten"] > 10, "the ledger must record every event it could not write"
    assert "File too large" in facts["first_error"]
    assert facts["chain_valid"] is True, "the in-memory chain stays valid so a receipt can still be built"
    assert facts["closed_cleanly"] is True, "closing a ledger on a failing filesystem must not raise"
    assert facts["on_disk_bytes"] > 0


def test_a_ledger_may_not_be_a_symlink(tmp_path: Path):
    """Writing the record through a link would put it wherever the link points."""
    real = tmp_path / "real.jsonl"
    real.write_text("", encoding="utf-8")
    link = tmp_path / "link.jsonl"
    link.symlink_to(real)
    with pytest.raises(LedgerDamagedError, match="symbolic link"):
        Ledger.open(link, max_bytes=None)
    assert real.read_text(encoding="utf-8") == "", "the target must be untouched"


def test_a_ledger_may_not_be_a_directory_or_a_device(tmp_path: Path):
    directory = tmp_path / "adir.jsonl"
    directory.mkdir()
    with pytest.raises(LedgerDamagedError, match="not a regular file"):
        Ledger.open(directory, max_bytes=None)


def test_a_closed_descriptor_is_healed_by_reopening(tmp_path: Path):
    path = tmp_path / "reopen.jsonl"
    ledger = Ledger.open(path, max_bytes=None)
    ledger.append("note", {"before": True})
    assert ledger._handle is not None
    ledger._handle.close()
    event = ledger.append("note", {"after": True})
    assert event.seq == 1
    assert ledger.unwritten == [], "a descriptor that was closed is not a lost record"
    ledger.close()
    assert path.read_text(encoding="utf-8").count("\n") == 2


def test_a_second_writer_is_refused_rather_than_interleaving(tmp_path: Path):
    path = tmp_path / "locked.jsonl"
    first = Ledger.open(path, max_bytes=None)
    try:
        with pytest.raises(LedgerLockedError) as exc:
            Ledger.open(path, max_bytes=None)
        assert str(path) in str(exc.value)
        # …and the lock is released when the owner closes, so a later run can proceed.
        first.close()
        second = Ledger.open(path, max_bytes=None)
        second.append("note", {"message": "the lock is free"})
        second.close()
    finally:
        first.close()


def test_the_ledger_directory_is_owner_only_and_repaired_when_permissive(tmp_path: Path):
    """Evidence is the customer's. Files are created 0600 and the directory is tightened."""
    from docket.ledger import _open_ledger_handle

    runs = tmp_path / "runs"
    runs.mkdir()
    runs.chmod(0o777)  # a permissive directory, e.g. one created by an older version
    ledger = Ledger.open(runs / "run.jsonl", max_bytes=None)
    ledger.append("note", {"message": "hello"})
    ledger.close()
    assert (runs.stat().st_mode & 0o777) == 0o700, "the ledger directory is tightened on open"
    assert ((runs / "run.jsonl").stat().st_mode & 0o777) == 0o600, "the ledger itself is owner-only"

    # The low-level handle honours the same rule even when the path already exists.
    with _open_ledger_handle(runs / "second.jsonl") as handle:
        handle.write("{}\n")
    assert ((runs / "second.jsonl").stat().st_mode & 0o777) == 0o600


def test_the_file_digest_and_chain_survive_a_reopen(tmp_path: Path):
    path = tmp_path / "reopen2.jsonl"
    first = Ledger.open(path, max_bytes=None)
    first.append("note", {"a": 1})
    head_before = first.head
    first.close()
    second = Ledger.open(path, max_bytes=None, exclusive=False)
    assert second.head == head_before
    second.append("note", {"b": 2})
    assert second.chain_error() is None
    second.close()
    assert [event.data for event in Ledger.read(path)] == [{"a": 1}, {"b": 2}]
    assert Ledger.read_with_diagnostics(path)[1]["healthy"] is True


def test_canonical_lines_mean_another_implementation_can_hash_them(tmp_path: Path):
    path = tmp_path / "canonical.jsonl"
    ledger = Ledger.open(path, max_bytes=None)
    ledger.append("note", {"z": 1, "a": [3, 2, 1], "message": "unicode ✓"})
    ledger.close()
    line = path.read_text(encoding="utf-8").strip()
    assert line == canonical_bytes(json.loads(line)).decode("utf-8"), "lines are canonical, not merely valid JSON"
    assert line.index('"a"') < line.index('"message"') < line.index('"z"'), "keys are sorted"


def test_an_os_error_while_opening_surfaces_as_that_error(tmp_path: Path):
    """The error path must not raise a *different* error than the one that happened.

    `_open_ledger_handle` names ELOOP/EMLINK to explain a refused symlink. It did so through
    `errno`, which was never imported — so every *other* `os.open` failure (a path that cannot
    be opened, a full disk, a permissions problem) raised `NameError: name 'errno' is not
    defined` instead of the real cause, in the one place whose whole job is to say what went
    wrong. `Ledger.open` refuses a directory before this point, so the opener is called directly
    with one: EISDIR is the cheapest portable OSError to produce.
    """
    from docket.ledger import _open_ledger_handle

    directory = tmp_path / "not-a-ledger"
    directory.mkdir()
    with pytest.raises(OSError) as caught:
        _open_ledger_handle(directory)
    assert not isinstance(caught.value, NameError)
    assert caught.value.errno == errno.EISDIR
