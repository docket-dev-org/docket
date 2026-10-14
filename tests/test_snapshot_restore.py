"""Snapshot and rollback: the `--snapshot` / `--rollback-on-breach` / `docket restore` path.

The point of these tests is the awkward half of rollback: not that files can be copied, but
that the copy is *derived from the record*, refuses what it cannot cover, and never overwrites
work somebody did after the run. So most of what is asserted here is about honesty — the
not-covered list, the refusals, the counts in the receipt — rather than about bytes moved.
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from docket.fakeupstream import FakeUpstream, Step
from docket.ledger import Ledger
from docket.policy import Caps, Files, Policy
from docket.snapshot import (
    DEFAULT_MAX_BYTES,
    SnapshotTooLargeError,
    apply_restore,
    plan_restore,
    read_manifest,
    summarise,
    take_snapshot,
)
from docket.supervisor import Supervisor
from docket.verify import verify_receipt

AGENT = '''
import json, os, sys, urllib.request

cwd = os.getcwd()
with open(os.path.join(cwd, "notes.txt"), "a", encoding="utf-8") as handle:
    handle.write("agent was here\\n")
with open(os.path.join(cwd, "created.txt"), "w", encoding="utf-8") as handle:
    handle.write("new file\\n")
os.unlink(os.path.join(cwd, "gone.txt"))
base = os.environ["OPENAI_BASE_URL"]
for index in range(3):
    body = json.dumps({"model": "claude-sonnet-5", "messages": [{"role": "user", "content": f"step {index}"}]})
    request = urllib.request.Request(
        f"{base}/chat/completions", data=body.encode(), headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            response.read()
    except Exception as exc:  # the boundary may refuse once the cap is hit; that is the point
        print(f"call {index}: {type(exc).__name__}", file=sys.stderr)
        break
'''


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    (root / "src").mkdir(parents=True)
    (root / "notes.txt").write_text("original\n", encoding="utf-8")
    (root / "gone.txt").write_text("delete me\n", encoding="utf-8")
    (root / "src" / "app.py").write_text("print('app')\n", encoding="utf-8")
    (root / "agent.py").write_text(AGENT, encoding="utf-8")
    return root


# --------------------------------------------------------------------------- #
# The snapshot itself
# --------------------------------------------------------------------------- #


def test_a_snapshot_captures_contents_modes_and_is_content_addressed(workspace: Path, tmp_path: Path):
    duplicate = workspace / "src" / "copy.py"
    duplicate.write_text("print('app')\n", encoding="utf-8")  # identical bytes to src/app.py
    store = tmp_path / "store"
    taken = take_snapshot(roots=["."], ignore=[], root=workspace, store=store, taken_at="2026-01-01T00:00:00Z")
    paths = {entry["path"] for entry in taken.entries}
    assert {"notes.txt", "gone.txt", "src/app.py", "src/copy.py", "agent.py"} <= paths
    assert taken.bytes_stored < taken.bytes_watched + 1 or taken.bytes_stored == taken.bytes_watched
    blobs = list((store / "blobs").rglob("*"))
    files = [path for path in blobs if path.is_file()]
    # Two identical files, one blob: the store is content-addressed, so repeated content costs
    # one copy and the manifest can never disagree with itself about a file's bytes.
    assert len(files) == len({entry["sha256"] for entry in taken.entries})
    manifest = read_manifest(store)
    assert manifest["format"] == "docket/snapshot/v1"
    assert len(manifest["entries"]) == len(taken.entries)
    notes = next(entry for entry in taken.entries if entry["path"] == "notes.txt")
    assert (store / notes["blob"]).read_bytes() == b"original\n"
    assert notes["mode"] & 0o777


def test_a_tree_over_the_budget_is_refused_before_anything_is_written(workspace: Path, tmp_path: Path):
    store = tmp_path / "store"
    with pytest.raises(SnapshotTooLargeError) as caught:
        take_snapshot(roots=["."], ignore=[], root=workspace, store=store, max_bytes=8)
    assert caught.value.watched > 8
    assert "budget" in str(caught.value)
    assert not (store / "blobs").exists(), "a refused snapshot must leave no half-copied tree"


def test_a_file_over_the_per_file_ceiling_is_named_not_silently_skipped(workspace: Path, tmp_path: Path):
    (workspace / "big.bin").write_bytes(b"x" * 64)
    taken = take_snapshot(
        roots=["."], ignore=[], root=workspace, store=tmp_path / "store", max_file_bytes=16
    )
    reasons = {item["path"]: item["reason"] for item in taken.not_covered}
    assert "big.bin" in reasons and "ceiling" in reasons["big.bin"]
    assert all(entry["path"] != "big.bin" for entry in taken.entries)


def test_ignored_globs_are_not_captured(workspace: Path, tmp_path: Path):
    (workspace / ".docket").mkdir()
    (workspace / ".docket" / "ledger.jsonl").write_text("{}\n", encoding="utf-8")
    taken = take_snapshot(roots=["."], ignore=[".docket/**"], root=workspace, store=tmp_path / "store")
    assert all(not entry["path"].startswith(".docket") for entry in taken.entries)


# --------------------------------------------------------------------------- #
# The plan
# --------------------------------------------------------------------------- #


def _change(path: str, kind: str, before: str | None = None, after: str | None = None) -> dict:
    item: dict = {"path": path, "change": kind}
    if before:
        item["before"] = {"sha256": before, "size": 1}
    if after:
        item["after"] = {"sha256": after, "size": 1}
    return item


def test_the_plan_maps_each_kind_of_change_to_one_action(workspace: Path, tmp_path: Path):
    store = tmp_path / "store"
    take_snapshot(roots=["."], ignore=[], root=workspace, store=store)
    manifest = read_manifest(store)
    plan = plan_restore(
        manifest,
        [
            _change("notes.txt", "modified", "aaa", "bbb"),
            _change("gone.txt", "removed", "ccc", None),
            _change("created.txt", "added", None, "ddd"),
            _change("never-snapshotted.log", "modified", "eee", "fff"),
        ],
    )
    actions = {item["path"]: item["action"] for item in plan}
    assert actions == {
        "notes.txt": "restore",
        "gone.txt": "recreate",
        "created.txt": "remove",
        "never-snapshotted.log": "not_covered",
    }


def test_a_change_the_snapshot_does_not_cover_is_stated_with_its_reason(workspace: Path, tmp_path: Path):
    store = tmp_path / "store"
    take_snapshot(roots=["."], ignore=[], root=workspace, store=store)
    plan = plan_restore(read_manifest(store), [_change("stranger.txt", "modified", "aaa", "bbb")])
    assert plan[0]["action"] == "not_covered"
    assert "no copy" in plan[0]["reason"]


# --------------------------------------------------------------------------- #
# Applying it
# --------------------------------------------------------------------------- #


def test_restore_puts_back_bytes_modes_and_removes_what_the_agent_created(workspace: Path, tmp_path: Path):
    store = tmp_path / "store"
    taken = take_snapshot(roots=["."], ignore=[], root=workspace, store=store)
    plan = plan_restore(
        read_manifest(store),
        [
            _change("notes.txt", "modified", "aaa", "bbb"),
            _change("gone.txt", "removed", "ccc", None),
            _change("created.txt", "added", None, "ddd"),
        ],
    )
    # The tree as the agent left it.
    (workspace / "notes.txt").write_text("agent was here\n", encoding="utf-8")
    (workspace / "gone.txt").unlink()
    (workspace / "created.txt").write_text("new file\n", encoding="utf-8")
    applied = apply_restore(plan, root=workspace, store=store, force=True)
    counts = summarise(applied)
    assert counts.get("restored") == 1 and counts.get("recreated") == 1 and counts.get("removed") == 1
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == "original\n"
    assert (workspace / "gone.txt").read_text(encoding="utf-8") == "delete me\n"
    assert not (workspace / "created.txt").exists()
    assert len(taken.entries) >= 4


def test_restore_refuses_to_overwrite_an_edit_made_after_the_run(workspace: Path, tmp_path: Path):
    store = tmp_path / "store"
    take_snapshot(roots=["."], ignore=[], root=workspace, store=store)
    manifest = read_manifest(store)
    plan = plan_restore(manifest, [_change("notes.txt", "modified", "aaa", "bbb")])
    plan[0]["post_sha256"] = "not-the-current-digest"
    applied = apply_restore(plan, root=workspace, store=store)  # force=False
    assert applied[0]["status"] == "refused"
    assert "changed after the run" in applied[0]["reason"]
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == "original\n"
    forced = apply_restore(plan, root=workspace, store=store, force=True)
    assert forced[0]["status"] == "restored"


def test_a_corrupted_snapshot_blob_is_reported_not_applied(workspace: Path, tmp_path: Path):
    store = tmp_path / "store"
    taken = take_snapshot(roots=["."], ignore=[], root=workspace, store=store)
    manifest = read_manifest(store)
    notes = next(entry for entry in taken.entries if entry["path"] == "notes.txt")
    (store / notes["blob"]).write_bytes(b"tampered\n")
    (workspace / "notes.txt").write_text("agent was here\n", encoding="utf-8")
    plan = plan_restore(manifest, [_change("notes.txt", "modified", "aaa", "bbb")])
    applied = apply_restore(plan, root=workspace, store=store, force=True)
    assert applied[0]["status"] == "failed"
    assert "digest" in applied[0]["reason"]
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == "agent was here\n"


def test_restore_refuses_to_follow_a_symlink(workspace: Path, tmp_path: Path):
    store = tmp_path / "store"
    take_snapshot(roots=["."], ignore=[], root=workspace, store=store)
    plan = plan_restore(read_manifest(store), [_change("notes.txt", "modified", "aaa", "bbb")])
    target = tmp_path / "elsewhere.txt"
    target.write_text("somebody else's file\n", encoding="utf-8")
    (workspace / "notes.txt").unlink()
    (workspace / "notes.txt").symlink_to(target)
    applied = apply_restore(plan, root=workspace, store=store, force=True)
    assert applied[0]["status"] == "refused"
    assert target.read_text(encoding="utf-8") == "somebody else's file\n"


# --------------------------------------------------------------------------- #
# The whole thing, through a real run
# --------------------------------------------------------------------------- #


def _run_with_rollback(workspace: Path, tmp_path: Path, *, inside: bool = False, **kwargs) -> tuple[dict, Path]:
    upstream = FakeUpstream(steps=[Step(model="claude-sonnet-5", input_tokens=120_000, output_tokens=60_000)])
    host, port = upstream.start()
    policy = Policy(
        name="rollback",
        caps=Caps(max_usd=Decimal("0.000001"), on_breach="kill", grace_seconds=1.0),
        # No `.docket` in `ignore` on purpose: whether the evidence directory is watched is
        # Docket's business, not something a policy author has to remember.
        files=Files(watch=["."], ignore=[]),
    )
    kwargs.setdefault("snapshot_max_bytes", DEFAULT_MAX_BYTES)
    docket_dir = (workspace / ".docket") if inside else (tmp_path / "docket")
    supervisor = Supervisor(
        [sys.executable, "agent.py"],
        policy=policy,
        docket_dir=docket_dir,
        cwd=workspace,
        upstreams={"openai": f"http://{host}:{port}"},
        snapshot_files=True,
        **kwargs,
    )
    try:
        result = supervisor.run()
    finally:
        upstream.stop()
    return result, docket_dir


def test_a_breached_run_really_puts_the_tree_back(workspace: Path, tmp_path: Path):
    """The claim, end to end: agent edits, cap breach, tree restored, and the record says so."""
    result, docket_dir = _run_with_rollback(workspace, tmp_path, rollback_on_breach=True)
    assert result.exit_code == 42, result.receipt["session"]
    assert result.breached == "max_usd"

    assert (workspace / "notes.txt").read_text(encoding="utf-8") == "original\n"
    assert (workspace / "gone.txt").read_text(encoding="utf-8") == "delete me\n"
    assert not (workspace / "created.txt").exists()

    receipt = result.receipt
    assert receipt["ops"]["snapshot"]["taken"] is True
    assert receipt["ops"]["snapshot"]["files"] >= 4
    rollback = receipt["ops"]["rollback"]
    assert rollback["applied"] is True
    assert rollback["trigger"] == "cap breached"
    assert rollback["counts"]["restored"] >= 1
    assert rollback["counts"]["removed"] >= 1
    assert rollback["counts"]["recreated"] >= 1
    assert "notes.txt" in rollback["paths"]

    events = [event.type for event in Ledger.read(result.ledger_path)]
    assert "x-docket.rollback-applied" in events
    assert "fs.snapshot" in events

    store = docket_dir / "snapshots" / str(receipt["session"]["id"])
    assert (store / "manifest.json").exists()
    assert verify_receipt(receipt, ledger_path=result.ledger_path).ok, "a rolled-back run must still verify"


def test_a_breached_run_without_rollback_leaves_the_tree_alone(workspace: Path, tmp_path: Path):
    """Snapshot yes, rollback no: the evidence is captured but nothing is undone behind the user."""
    result, _ = _run_with_rollback(workspace, tmp_path, rollback_on_breach=False)
    assert result.exit_code == 42
    assert (workspace / "notes.txt").read_text(encoding="utf-8") != "original\n"
    assert (workspace / "created.txt").exists()
    assert result.receipt["ops"]["rollback"] is None
    assert result.receipt["ops"]["snapshot"]["taken"] is True


def test_what_the_snapshot_could_not_cover_reaches_the_receipt(workspace: Path, tmp_path: Path):
    """The tree it could not capture has to be visible in the record, not just in a log line.

    "We can roll this back" is a claim a reader has to be able to check, and the part that is
    usually missing is the *except*: the files a snapshot declined. So the refusal travels
    with the run's evidence.
    """
    secret = tmp_path / "outside-secret.txt"
    secret.write_text("private key material\n", encoding="utf-8")
    (workspace / "link.txt").symlink_to(secret)

    result, _ = _run_with_rollback(workspace, tmp_path, rollback_on_breach=True)
    not_covered = {item["path"]: item["reason"] for item in result.receipt["ops"]["snapshot"]["not_covered"]}
    assert "link.txt" in not_covered and "symbolic link" in not_covered["link.txt"]
    assert secret.read_text(encoding="utf-8") == "private key material\n"


def test_a_snapshot_that_cannot_fit_stops_the_run_before_the_agent_starts(workspace: Path, tmp_path: Path):
    """Refusing up front is the honest option: half a snapshot invites restoring over nothing."""
    with pytest.raises(SnapshotTooLargeError):
        _run_with_rollback(workspace, tmp_path, rollback_on_breach=True, snapshot_max_bytes=8)
    # The agent never ran: no created file, no edits.
    assert not (workspace / "created.txt").exists()
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == "original\n"


def test_a_deleted_file_is_recovered_from_the_snapshot_alone(workspace: Path, tmp_path: Path):
    """Recovery without the agent's cooperation: the bytes come from the store, not the run."""
    result, docket_dir = _run_with_rollback(workspace, tmp_path, rollback_on_breach=False)
    session_id = str(result.receipt["session"]["id"])
    store = docket_dir / "snapshots" / session_id
    assert (store / "manifest.json").exists()
    # Now do it by hand, exactly as `docket restore` does: read the record, plan, apply.
    changes = [
        dict(event.data) for event in Ledger.read(result.ledger_path) if event.type == "fs.change"
    ]
    plan = plan_restore(read_manifest(store), changes)
    for item in plan:
        change = next((c for c in changes if c.get("path") == item["path"]), {})
        after = change.get("after")
        item["post_sha256"] = after.get("sha256") if isinstance(after, dict) else None
    applied = apply_restore(plan, root=workspace, store=store, force=True)
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == "original\n"
    assert (workspace / "gone.txt").read_text(encoding="utf-8") == "delete me\n"
    assert summarise(applied).get("restored", 0) >= 1


def test_a_run_never_records_its_own_evidence_as_the_agents_work(workspace: Path, tmp_path: Path):
    """The ledger is written during the run. If it is inside the watched tree, it must not be
    reported as a change the agent made — and a rollback must never plan to delete it."""
    result, docket_dir = _run_with_rollback(workspace, tmp_path, inside=True, rollback_on_breach=True)
    events = Ledger.read(result.ledger_path)
    changed = [event.data.get("path") for event in events if event.type == "fs.change"]
    assert changed, "the agent's edits are still recorded"
    assert not [path for path in changed if path and path.startswith(".docket")], changed
    notes = [event.data for event in events if event.type == "note" and event.data.get("excluded")]
    assert notes, "the exclusion has to be in the record, not just in the code"
    assert notes[0]["excluded"] == ".docket/**"
    assert ".docket/**" in result.receipt["policy"]["document"]["files"]["ignore"], (
        "the receipt embeds the policy that was in force, exclusion included"
    )
    assert set(result.receipt["ops"]["rollback"]["paths"]) == set(changed)
    assert (docket_dir / "runs").exists(), "the ledger survived the rollback"
    assert (docket_dir / "receipts").exists()


def test_a_run_whose_evidence_directory_is_the_tree_says_so(workspace: Path, tmp_path: Path, capsys):
    """`--docket-dir .` is an operator choice; it is disclosed, not silently watched."""
    upstream = FakeUpstream(steps=[Step(model="claude-sonnet-5", input_tokens=1_000, output_tokens=100)])
    host, port = upstream.start()
    policy = Policy(name="same dir", files=Files(watch=["."], ignore=[]))
    supervisor = Supervisor(
        [sys.executable, "-c", "print('nothing to do')"],
        policy=policy,
        docket_dir=workspace,
        cwd=workspace,
        upstreams={"openai": f"http://{host}:{port}"},
    )
    try:
        result = supervisor.run()
    finally:
        upstream.stop()
    captured = capsys.readouterr()
    assert "the evidence directory is the working directory" in captured.err
    assert result.receipt["policy"]["document"]["files"]["ignore"] == []


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #


def test_the_restore_command_dry_runs_then_applies(workspace: Path, tmp_path: Path, monkeypatch, capsys):
    from docket.cli import main

    result, _ = _run_with_rollback(workspace, tmp_path, rollback_on_breach=False)
    receipt_path = result.receipt_path
    monkeypatch.chdir(tmp_path)

    # Something the operator did after the run: restoring must not clobber it by default.
    (workspace / "notes.txt").write_text("operator edited this\n", encoding="utf-8")

    exit_code = main(["restore", str(receipt_path)])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "dry run" in out and "would restore" in out
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == "operator edited this\n"

    exit_code = main(["restore", str(receipt_path), "--apply"])
    out = capsys.readouterr().out
    assert exit_code == 1, "a refused path must not report success"
    assert "REFUSED" in out and "changed after the run" in out
    assert "dry run" not in out, "a restore that ran must not claim nothing was written"
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == "operator edited this\n"

    exit_code = main(["restore", str(receipt_path), "--apply", "--force"])
    assert exit_code == 0
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == "original\n"
    assert (workspace / "gone.txt").read_text(encoding="utf-8") == "delete me\n"


def test_the_restore_command_finds_the_store_from_latest_receipt(
    workspace: Path, tmp_path: Path, monkeypatch, capsys
):
    """`latest.receipt.json` sits at the top of the docket directory, not in `receipts/`.

    The store and the ledger are found from what the receipt records — the manifest path and the
    session id — rather than from the receipt's own directory, so the short path works as well
    as the long one. (It did not, until a run on a real tree showed `no snapshot` next to a
    snapshot that had just been taken.)
    """
    from docket.cli import main

    result, docket_dir = _run_with_rollback(workspace, tmp_path, rollback_on_breach=False)
    latest = docket_dir / "latest.receipt.json"
    latest.write_bytes(Path(result.receipt_path).read_bytes())
    (workspace / "notes.txt").write_text("agent was here\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    exit_code = main(["restore", str(latest)])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "would restore" in out and "notes.txt" in out, out


def test_the_restore_command_is_honest_when_there_is_no_snapshot(tmp_path: Path, monkeypatch, capsys):
    from docket.cli import main

    receipt = tmp_path / "receipts" / "DKT-20260101-AAAAAA.receipt.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({"format": "docket/receipt/v1", "session": {"id": "s"}}), encoding="utf-8")
    exit_code = main(["restore", str(receipt)])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "no snapshot" in captured.err and "--snapshot" in captured.err


def test_the_restore_command_reports_not_covered_paths(workspace: Path, tmp_path: Path, monkeypatch, capsys):
    from docket.cli import main

    result, docket_dir = _run_with_rollback(workspace, tmp_path, rollback_on_breach=False)
    session_id = str(result.receipt["session"]["id"])
    store = docket_dir / "snapshots" / session_id
    manifest = json.loads((store / "manifest.json").read_text(encoding="utf-8"))
    manifest["not_covered"] = [{"path": "huge.bin", "reason": "over the per-file ceiling"}]
    (store / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    exit_code = main(["restore", str(result.receipt_path), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["not_covered"][0]["path"] == "huge.bin"
    assert payload["counts"] is None
