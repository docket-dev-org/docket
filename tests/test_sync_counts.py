"""Guards for `tools/sync_counts.py`, the tool that keeps quoted numbers honest.

The documentation quotes the size of the suite in four places, the version in three, and a
per-file count for every test module. All of them were typed by hand, and each has drifted at
least once. `tests/test_doc_links.py` refuses a stale number; this file proves the tool that
*fixes* one does what it claims, because a repair tool that silently corrupts a sentence is worse
than no tool at all.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _load():
    """Load the tool as a module, registered before execution.

    The pattern matches `tests/test_deploy_check.py`: loading a file without registering it in
    `sys.modules` is the documented way to break a `@dataclass`, and the resulting error names
    `NoneType` rather than the real cause.
    """
    name = "sync_counts_under_test"
    spec = importlib.util.spec_from_file_location(name, REPO / "tools" / "sync_counts.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sc = _load()


@pytest.fixture
def scratch_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private copy of the four documents, so a test can corrupt them safely."""
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    for name in (
        "README.md",
        "CONTRIBUTING.md",
        "docs/testing.md",
        "docs/sre-coverage.md",
        "docs/production-coverage.md",
        "pyproject.toml",
    ):
        source = REPO / name
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return root


def _counts(n: int = 1238) -> dict[str, int]:
    return {"test_example.py": n}


# --- the parser -------------------------------------------------------------------------------


def test_the_collect_parser_reads_the_shape_pytest_actually_prints(monkeypatch: pytest.MonkeyPatch) -> None:
    """`pytest --collect-only -q` prints `tests/test_x.py: N`, one line per file.

    The first version of this parser looked for `tests/test_x.py::` and found nothing, which is
    the failure mode a parser has: it reports an empty result and the caller believes there are
    no tests.
    """
    import subprocess

    def stand_in(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[],  # type: ignore[arg-type]
            returncode=0,
            stdout="tests/test_a.py: 12\ntests/test_b.py: 3\n\n2 files collected\n",
            stderr="",
        )

    monkeypatch.setattr(sc.subprocess, "run", stand_in)
    assert sc.collected_test_counts() == {"test_a.py": 12, "test_b.py": 3}


def test_a_collect_run_that_reports_nothing_is_an_error_not_an_empty_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty result would make the tool rewrite every number to zero."""
    import subprocess

    def stand_in(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="nothing here\n", stderr="")  # type: ignore[arg-type]

    monkeypatch.setattr(sc.subprocess, "run", stand_in)
    with pytest.raises(sc.SyncError, match="no tests"):
        sc.collected_test_counts()


def test_a_failing_collect_run_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    def stand_in(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="collection error")  # type: ignore[arg-type]

    monkeypatch.setattr(sc.subprocess, "run", stand_in)
    with pytest.raises(sc.SyncError, match="collect-only failed"):
        sc.collected_test_counts()


# --- the version ------------------------------------------------------------------------------


def test_the_version_comes_from_pyproject(scratch_tree: Path) -> None:
    assert sc.project_version(scratch_tree) == re.search(
        r'^version = "([^"]+)"', (scratch_tree / "pyproject.toml").read_text(), re.M
    ).group(1)


def test_a_pyproject_with_no_version_is_an_error(scratch_tree: Path) -> None:
    (scratch_tree / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    with pytest.raises(sc.SyncError, match="does not declare a version"):
        sc.project_version(scratch_tree)


# --- check mode -------------------------------------------------------------------------------


def test_check_reports_drift_without_writing(scratch_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sc, "collected_test_counts", lambda root=None: _counts(999))
    before = (scratch_tree / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert sc.sync(check_only=True, dry_run=False, root=scratch_tree) == 1
    assert (scratch_tree / "CONTRIBUTING.md").read_text(encoding="utf-8") == before, "check mode wrote to the tree"


@pytest.fixture
def real_counts() -> dict[str, int]:
    """The real per-file counts, collected before any test rebinds `REPO`.

    The fixture is declared *first* in the signatures that use it, because pytest resolves
    fixtures in signature order and `scratch_tree` rebinds `REPO` to a tree with no `tests/`
    directory.
    """
    return sc.collected_test_counts()


def test_check_passes_on_a_tree_that_matches(
    real_counts: dict[str, int], scratch_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real tree is in step, so a check against the real counts must pass."""
    monkeypatch.setattr(sc, "collected_test_counts", lambda root=None: real_counts)
    assert sc.sync(check_only=True, dry_run=False, root=scratch_tree) == 0


def test_a_pattern_that_matches_nothing_is_an_error_not_a_pass(
    scratch_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scanner that matches nothing would report a clean tree.

    The guard in `tests/test_doc_links.py` has the same shape, and the same reason.
    """
    monkeypatch.setattr(sc, "collected_test_counts", lambda root=None: _counts())
    target = scratch_tree / "CONTRIBUTING.md"
    target.write_text("make test          # fully offline, no count\n", encoding="utf-8")
    with pytest.raises(sc.SyncError, match="expected exactly one match"):
        sc.sync(check_only=True, dry_run=False, root=scratch_tree)


# --- write mode -------------------------------------------------------------------------------


def test_a_rewrite_preserves_the_sentence_around_the_number(
    scratch_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The literal text is part of the sentence.

    The first version substituted the whole match, so `# 1237 tests, fully offline` became `1238`
    and the sentence stopped reading as one. The surrounding words are asserted here, not just the
    digit.
    """
    monkeypatch.setattr(sc, "collected_test_counts", lambda root=None: _counts())
    target = scratch_tree / "CONTRIBUTING.md"
    target.write_text("make test          # 1237 tests, fully offline, ~2-4 min on 2 vCPU\n", encoding="utf-8")
    assert sc.sync(check_only=False, dry_run=False, root=scratch_tree) == 0
    assert (scratch_tree / "CONTRIBUTING.md").read_text(
        encoding="utf-8"
    ) == "make test          # 1238 tests, fully offline, ~2-4 min on 2 vCPU\n"


def test_a_rewrite_of_the_version_leaves_the_badge_intact(scratch_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The badge is a *quote* of the version, and the tool is the only thing that keeps it true.

    Nothing here names a version. This test used to hardcode the one that was current when it was
    written, which meant every release bumped it to red and every release fixed a test rather than
    the tool. The version is read from the tree, made wrong in two independent places (the project
    file and the badge), and the tool is required to bring both back to the same value — so it
    keeps working on the release after this one, and the one after that.
    """
    monkeypatch.setattr(sc, "collected_test_counts", lambda root=None: _counts())
    document = (scratch_tree / "pyproject.toml").read_text(encoding="utf-8")
    declared = re.search(r'^version = "([^"]+)"$', document, re.M)
    assert declared, "the scratch tree has no version to work from"
    current = declared.group(1)
    other = "9.9.9"

    (scratch_tree / "pyproject.toml").write_text(
        document.replace(f'version = "{current}"', f'version = "{other}"', 1), encoding="utf-8"
    )
    readme = scratch_tree / "README.md"
    stale = readme.read_text(encoding="utf-8")
    assert f"version-{current}-blue" in stale, (
        f"the README no longer carries a version-{current}-blue badge to correct; if the badge "
        "changed shape, this test must follow it rather than be deleted"
    )
    readme.write_text(stale.replace(f"version-{current}-blue", "version-0.0.1-blue", 1), encoding="utf-8")

    assert sc.sync(check_only=False, dry_run=False, root=scratch_tree) == 0
    after = readme.read_text(encoding="utf-8")
    assert f"version-{other}-blue" in after, "the badge was not brought back to the declared version"
    assert "version-0.0.1-blue" not in after


def test_the_per_file_table_is_repaired(
    real_counts: dict[str, int], scratch_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One row is corrupted, and the tool puts back the number pytest actually collected.

    The corruption uses the real count rather than a made-up one, so the test survives the suite
    growing: it asserts the row returns to the collected value whatever that value is.
    """
    monkeypatch.setattr(sc, "collected_test_counts", lambda root=None: real_counts)
    table = scratch_tree / "docs" / "testing.md"
    name = next(iter(real_counts))
    good = f"| `{name}` | {real_counts[name]} |"
    assert good in table.read_text(encoding="utf-8"), "the fixture no longer carries the row this test corrupts"
    table.write_text(table.read_text(encoding="utf-8").replace(good, f"| `{name}` | 3 |"), encoding="utf-8")
    assert sc.sync(check_only=False, dry_run=False, root=scratch_tree) == 0
    assert good in table.read_text(encoding="utf-8")


def test_a_missing_table_is_an_error(scratch_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A table that parses to nothing would make the tool rewrite zero rows and report success."""
    monkeypatch.setattr(sc, "collected_test_counts", lambda root=None: _counts())
    table = scratch_tree / "docs" / "testing.md"
    table.write_text("no table here\n", encoding="utf-8")
    with pytest.raises(sc.SyncError, match="counted table was not found"):
        sc._rewrite_table(table, _counts())


# --- the two halves must stay in step ---------------------------------------------------------


def test_every_pattern_the_tool_rewrites_is_a_pattern_the_guard_reads() -> None:
    """A number the guard cannot see is a number the tool cannot fix.

    `SUITE_TOTAL_QUOTES` here and in `tests/test_doc_links.py` are the same list by construction:
    one is imported from the other's source of truth. If they ever diverge, a count can be stale in
    a place neither half notices.
    """
    import test_doc_links

    assert set(sc.SUITE_TOTAL_QUOTES) == set(test_doc_links.SUITE_TOTAL_QUOTES), (
        "tools/sync_counts.py and tests/test_doc_links.py disagree about which sentences quote the suite size"
    )


def test_the_tools_own_patterns_all_match_the_real_documents() -> None:
    """Every pattern the tool claims to rewrite matches exactly once, in the real tree."""
    for filename, pattern in (*sc.SUITE_TOTAL_QUOTES, *sc.VERSION_QUOTES):
        text = (REPO / filename).read_text(encoding="utf-8")
        found = re.findall(pattern, text)
        assert len(found) == 1, f"{filename}: {pattern!r} matched {len(found)} times, expected 1"
