"""Confinement: a restore only ever touches files inside the tree it was pointed at.

A restore is the one Docket operation that writes and deletes. Its list of paths comes from
documents — a ledger, a snapshot manifest — that describe what a run did but carry no proof of
who wrote them, so these tests take the hostile-author position: hand Docket a doctored
manifest, ledger and receipt and check that the worst it can do is refuse loudly.

The threat model is explicit, because "confined" is a claim that needs its limits stated: these
tests cover a *hostile document*. They do not cover a local attacker racing Docket between the
check and the write on a tree the operator has already handed to an agent.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from docket.ledger import Ledger
from docket.snapshot import (
    apply_restore,
    plan_restore,
    read_manifest,
    take_snapshot,
)


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    (root / "sub").mkdir(parents=True)
    (root / "notes.txt").write_text("original\n", encoding="utf-8")
    (root / "sub" / "app.py").write_text("print('app')\n", encoding="utf-8")
    return root


@pytest.fixture()
def store(tree: Path, tmp_path: Path) -> Path:
    location = tmp_path / "store"
    take_snapshot(roots=["."], ignore=[], root=tree, store=location)
    return location


def _change(path: str, kind: str = "modified", after: str | None = "d" * 64) -> dict:
    return {
        "path": path,
        "change": kind,
        "before": {"sha256": "a" * 64, "size": 1},
        "after": ({"sha256": after, "size": 1} if after else None),
    }


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "hostile",
    [
        "../outside.txt",
        "sub/../../outside.txt",
        "/etc/passwd",
        "sub//app.py",
        "./notes.txt",
        "sub/./app.py",
        "notes.txt\x00",
    ],
)
def test_a_hostile_path_in_the_record_is_refused_not_walked(store: Path, hostile: str):
    plan = plan_restore(read_manifest(store), [_change(hostile)])
    assert plan[0]["action"] == "refused"
    assert "will not touch" in plan[0]["reason"]


def test_a_refused_path_is_never_written_even_by_a_hand_built_plan(tree: Path, store: Path, tmp_path: Path):
    """`apply_restore` is public, so it re-checks rather than trusting the plan it is given."""
    outside = tmp_path / "outside.txt"
    outside.write_text("not docket's\n", encoding="utf-8")
    manifest = read_manifest(store)
    entry = manifest["entries"][0]
    plan = [
        {
            "path": "../outside.txt",
            "action": "restore",
            "blob": entry["blob"],
            "sha256": entry["sha256"],
            "mode": 0o644,
        }
    ]
    applied = apply_restore(plan, root=tree, store=store, force=True)
    assert applied[0]["status"] == "refused"
    assert outside.read_text(encoding="utf-8") == "not docket's\n"


def test_a_path_that_goes_through_a_symlink_is_refused(tree: Path, store: Path, tmp_path: Path):
    """Restoring through a link writes wherever the link points — here, a decoy tree.

    The plan is built without knowing the tree (it is auditable on a machine that has never
    seen it), so the *plan* only checks the shape of the path; the check that needs the tree —
    is a component a symlink? — happens in ``apply_restore``, which has one.
    """
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / "app.py").write_text("untouched\n", encoding="utf-8")
    (tree / "sub").rename(tree / "real-sub")
    (tree / "sub").symlink_to(decoy, target_is_directory=True)
    plan = plan_restore(read_manifest(store), [_change("sub/app.py")])
    assert plan[0]["action"] == "restore", "the plan cannot see the tree, so it plans; apply refuses"
    applied = apply_restore(plan, root=tree, store=store, force=True)
    assert applied[0]["status"] == "refused"
    assert "symbolic link" in applied[0]["reason"]
    assert (decoy / "app.py").read_text(encoding="utf-8") == "untouched\n"


def test_a_malformed_change_record_is_reported_rather_than_dropped(store: Path):
    plan = plan_restore(read_manifest(store), [{"path": None, "change": "modified"}])
    assert plan[0]["action"] == "refused"
    assert "malformed" in plan[0]["reason"]


# --------------------------------------------------------------------------- #
# Blobs
# --------------------------------------------------------------------------- #


def test_a_blob_field_pointing_outside_the_store_is_refused(tree: Path, store: Path, tmp_path: Path):
    """The digest names the blob; a document does not get to name a file to copy from."""
    secret = tmp_path / "secret.txt"
    secret.write_text("private key material\n", encoding="utf-8")
    entry = read_manifest(store)["entries"][0]
    plan = [
        {
            "path": "notes.txt",
            "action": "restore",
            "blob": str(secret),
            "sha256": entry["sha256"],
            "mode": 0o644,
        }
    ]
    applied = apply_restore(plan, root=tree, store=store, force=True)
    assert applied[0]["status"] == "refused"
    assert "only ever its own digest" in applied[0]["reason"]
    assert (tree / "notes.txt").read_text(encoding="utf-8") == "original\n"


@pytest.mark.parametrize("digest", ["", "not-hex", "A" * 64, "a" * 63, "../../../etc/passwd"])
def test_a_digest_that_is_not_a_content_address_is_refused(tree: Path, store: Path, digest: str):
    plan = [{"path": "notes.txt", "action": "restore", "blob": "blobs/aa/bb", "sha256": digest, "mode": 0o644}]
    applied = apply_restore(plan, root=tree, store=store, force=True)
    assert applied[0]["status"] == "refused"
    assert (tree / "notes.txt").read_text(encoding="utf-8") == "original\n"


def test_a_plan_without_a_digest_cannot_be_applied(tree: Path, store: Path):
    plan = [{"path": "notes.txt", "action": "restore", "blob": "blobs/aa/bb", "mode": 0o644}]
    applied = apply_restore(plan, root=tree, store=store, force=True)
    assert applied[0]["status"] == "refused"
    assert "no digest" in applied[0]["reason"]


def test_a_manifest_that_names_one_path_twice_cannot_swap_the_bytes(tree: Path, store: Path, tmp_path: Path):
    """First entry wins: with last-one-wins, appending a line to a manifest replaces a file."""
    manifest = read_manifest(store)
    entry = next(item for item in manifest["entries"] if item["path"] == "notes.txt")
    other = tmp_path / "other"
    other.mkdir()
    (other / "evil.txt").write_text("evil\n", encoding="utf-8")
    forged = take_snapshot(roots=["."], ignore=[], root=other, store=tmp_path / "forged")
    hostile = dict(forged.entries[0])
    hostile["path"] = "notes.txt"
    hostile["blob"] = str((Path(tmp_path / "forged") / forged.entries[0]["blob"]).relative_to(tmp_path / "forged"))
    manifest["entries"] = manifest["entries"] + [hostile]
    (store / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    plan = plan_restore(read_manifest(store), [_change("notes.txt")])
    assert plan[0]["sha256"] == entry["sha256"], "the first manifest entry must win"
    applied = apply_restore(plan, root=tree, store=store, force=True)
    assert applied[0]["status"] == "restored"
    assert (tree / "notes.txt").read_text(encoding="utf-8") == "original\n"


# --------------------------------------------------------------------------- #
# Modes and symlinks in the tree
# --------------------------------------------------------------------------- #


def test_a_setuid_mode_in_the_manifest_is_not_applied(tree: Path, store: Path):
    manifest = read_manifest(store)
    for entry in manifest["entries"]:
        if entry["path"] == "sub/app.py":
            entry["mode"] = 0o4755
    (store / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    plan = plan_restore(read_manifest(store), [_change("sub/app.py")])
    applied = apply_restore(plan, root=tree, store=store, force=True)
    assert applied[0]["status"] == "restored"
    mode = os.stat(tree / "sub" / "app.py").st_mode & 0o7777
    assert mode == 0o755, f"expected 0755, got {mode:#o}"
    assert "setuid" in applied[0]["mode_note"] or "only permission bits" in applied[0]["mode_note"]


def test_a_symlink_in_the_tree_is_disclosed_rather_than_followed(tree: Path, tmp_path: Path):
    """Copying a link's target would pull whatever it points at — often a key — into the store."""
    secret = tmp_path / "secret.txt"
    secret.write_text("private key material\n", encoding="utf-8")
    (tree / "link.txt").symlink_to(secret)
    taken = take_snapshot(roots=["."], ignore=[], root=tree, store=tmp_path / "store2")
    assert all(entry["path"] != "link.txt" for entry in taken.entries)
    reasons = {item["path"]: item["reason"] for item in taken.not_covered}
    assert "symbolic link" in reasons["link.txt"]
    # The evidence directory holds no copy of the secret's bytes.
    copied = [path.read_bytes() for path in (tmp_path / "store2").rglob("*") if path.is_file()]
    assert b"private key material\n" not in copied


def test_a_watched_root_outside_the_tree_is_disclosed(tree: Path, tmp_path: Path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "file.txt").write_text("outside\n", encoding="utf-8")
    taken = take_snapshot(roots=[".", str(outside)], ignore=[], root=tree, store=tmp_path / "store3")
    assert all(not entry["path"].startswith("/") for entry in taken.entries)
    assert any("outside the run's working directory" in item["reason"] for item in taken.not_covered)


# --------------------------------------------------------------------------- #
# The command end to end, against a doctored record
# --------------------------------------------------------------------------- #


def _docket_dir_with(tree: Path, tmp_path: Path) -> tuple[Path, str, Path]:
    """A docket directory whose ledger contains a hostile change, plus a real snapshot."""
    docket_dir = tmp_path / "docket"
    session_id = "20260101-AAAAAA"
    # Snapshotted *before* the run's edits, exactly as the supervisor does: a snapshot taken
    # after the damage would faithfully restore the damage.
    store = docket_dir / "snapshots" / session_id
    taken = take_snapshot(roots=["."], ignore=[], root=tree, store=store)
    assert any(entry["path"] == "notes.txt" for entry in taken.entries)
    ledger_path = docket_dir / "runs" / f"{session_id}.jsonl"
    ledger = Ledger.open(ledger_path)
    ledger.append("session.start", {"argv": ["python", "agent.py"], "cwd": str(tree)})
    ledger.append(
        "fs.change",
        {
            "path": "../outside.txt",
            "change": "modified",
            "before": {"sha256": "a" * 64, "size": 1},
            "after": {"sha256": "b" * 64, "size": 1},
        },
    )
    # The run's own record of what it left behind, digest and all: the restore checks the file
    # against this before overwriting it, so a fixture that lies here would (correctly) be
    # refused as an edit made after the run.
    (tree / "notes.txt").write_text("agent was here\n", encoding="utf-8")
    agent_left = hashlib.sha256((tree / "notes.txt").read_bytes()).hexdigest()
    ledger.append(
        "fs.change",
        {
            "path": "notes.txt",
            "change": "modified",
            "before": {"sha256": "a" * 64, "size": 9},
            "after": {"sha256": agent_left, "size": 15},
        },
    )
    ledger.close()
    receipt_path = docket_dir / "receipts" / f"{session_id}.receipt.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(
        json.dumps(
            {
                "format": "docket/receipt/v1",
                "docket_number": "DKT-20260101-AAAAAA",
                "session": {"id": session_id, "cwd": str(tree), "exit_code": 42},
                "ops": {"snapshot": {"taken": True}},
            }
        ),
        encoding="utf-8",
    )
    return receipt_path, session_id, store


def test_the_command_restores_the_covered_file_and_refuses_the_escaping_one(
    tree: Path, tmp_path: Path, monkeypatch, capsys
):
    from docket.cli import main

    receipt_path, _, _ = _docket_dir_with(tree, tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("not docket's\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    exit_code = main(["restore", str(receipt_path), "--apply"])
    out = capsys.readouterr().out
    assert exit_code == 1, "a refused path means the restore is not a success"
    assert (tree / "notes.txt").read_text(encoding="utf-8") == "original\n"
    assert outside.read_text(encoding="utf-8") == "not docket's\n"
    assert "REFUSE" in out and "../outside.txt" in out


def test_the_command_stops_when_the_records_disagree_about_the_tree(tree: Path, tmp_path: Path, monkeypatch, capsys):
    """A receipt is evidence, not proof of authorship: it does not get to redirect a restore."""
    from docket.cli import main

    receipt_path, _, _ = _docket_dir_with(tree, tmp_path)
    document = json.loads(receipt_path.read_text(encoding="utf-8"))
    document["session"]["cwd"] = str(tmp_path / "somebody-elses-checkout")
    receipt_path.write_text(json.dumps(document), encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    exit_code = main(["restore", str(receipt_path), "--apply"])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "disagree" in captured.err and "--root" in captured.err
    assert (tree / "notes.txt").read_text(encoding="utf-8") == "agent was here\n", "nothing was written"

    # With --root, the operator decides out loud, and the disagreement is printed alongside.
    exit_code = main(["restore", str(receipt_path), "--apply", "--root", str(tree)])
    out = capsys.readouterr().out
    assert exit_code == 1, "the escaping path is still refused"
    assert "not the tree" in out
    assert (tree / "notes.txt").read_text(encoding="utf-8") == "original\n"


def test_the_command_says_where_to_look_when_the_recorded_tree_is_gone(tree: Path, tmp_path: Path, monkeypatch, capsys):
    from docket.cli import main

    receipt_path, _, _ = _docket_dir_with(tree, tmp_path)
    moved = tmp_path / "moved-away"
    tree.rename(moved)
    monkeypatch.chdir(tmp_path)
    exit_code = main(["restore", str(receipt_path)])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "not a directory" in captured.err and "--root" in captured.err
