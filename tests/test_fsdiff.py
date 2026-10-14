"""The filesystem diff: the paths in the record, and what the ignore list actually ignores.

Two properties matter more than the hashing here, because both fail *silently*:

* paths must be relative to the watched tree, whatever form the caller passed the cwd in. A
  relative cwd used to make every path absolute (the resolved walk can never be relativised
  against an unresolved base), which meant no ignore pattern matched — so Docket's own ledger
  and snapshot store were recorded as the agent's changes, and the restore then refused its own
  change list as out-of-tree. The eight-phase game day caught it on the rollback phase.
* the ignore list must therefore apply identically with a relative and an absolute cwd. That
  equivalence is the property the fix restores, so it is what the test asserts.
"""

from __future__ import annotations

import os
from pathlib import Path

from docket.fsdiff import diff, snapshot, tree_digest


def _tree(root: Path) -> None:
    (root / "notes.txt").write_text("kept\n", encoding="utf-8")
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (root / "evidence").mkdir(exist_ok=True)
    (root / "evidence" / "run.jsonl").write_text("{}\n", encoding="utf-8")


def test_paths_are_relative_to_the_tree_even_when_the_cwd_is_relative(tmp_path: Path, monkeypatch):
    _tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    state = snapshot(["."], cwd=".")
    assert sorted(state) == ["evidence/run.jsonl", "notes.txt", "src/app.py"], sorted(state)
    assert all(not Path(path).is_absolute() for path in state), sorted(state)


def test_ignore_patterns_apply_the_same_with_a_relative_and_an_absolute_cwd(tmp_path: Path, monkeypatch):
    """The equivalence: the two calls must agree, and neither may see the evidence directory."""
    _tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    absolute = snapshot(["."], ignore=["evidence/**"], cwd=str(tmp_path))
    monkeypatch.chdir(tmp_path)
    relative = snapshot(["."], ignore=["evidence/**"], cwd=".")
    assert sorted(relative) == sorted(absolute) == ["notes.txt", "src/app.py"]


def test_a_watched_root_outside_the_tree_is_not_pretended_to_be_inside_it(tmp_path: Path, monkeypatch):
    """An out-of-tree watch keeps its absolute path, which makes it visible and unavailable to
    a confined restore — refusing it loudly beats silently pretending it is part of the tree."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "log.txt").write_text("x\n", encoding="utf-8")
    inside = tmp_path / "inside"
    inside.mkdir()
    (inside / "here.txt").write_text("y\n", encoding="utf-8")
    state = snapshot([str(outside), "."], cwd=str(inside))
    assert sorted(state) == [str(outside / "log.txt"), "here.txt"]


def test_the_diff_reports_the_same_paths_as_the_snapshots(tmp_path: Path, monkeypatch):
    """`diff` is fed the two snapshots, so a path shape that only holds in one of them would
    surface here as a change against a path nobody can restore."""
    _tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    before = snapshot(["."], ignore=["evidence/**"], cwd=".")
    (tmp_path / "notes.txt").write_text("touched\n", encoding="utf-8")
    (tmp_path / "evidence" / "run.jsonl").write_text("{}\n{}\n", encoding="utf-8")
    after = snapshot(["."], ignore=["evidence/**"], cwd=".")
    changes = diff(before, after)
    assert [change["path"] for change in changes] == ["notes.txt"]
    assert changes[0]["change"] == "modified"
    assert tree_digest(before) != tree_digest(after)


def test_an_unreadable_path_is_skipped_rather_than_recorded_as_empty(tmp_path: Path):
    """A file Docket cannot read is not a file it can honestly report a digest for."""
    if os.geteuid() == 0:  # pragma: no cover - root ignores the mode bits
        return
    root = tmp_path / "tree"
    root.mkdir()
    (root / "readable.txt").write_text("y\n", encoding="utf-8")
    unreadable = root / "secret.txt"
    unreadable.write_text("z\n", encoding="utf-8")
    unreadable.chmod(0o000)
    try:
        state = snapshot(["."], cwd=root)
    finally:
        unreadable.chmod(0o600)
    assert "readable.txt" in state
    assert "secret.txt" not in state
