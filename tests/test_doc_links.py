"""Every documentation path in this repository exists, and every page is reachable.

Documentation is an artefact this project makes claims about, so it gets the treatment an artefact
gets: a mechanical check rather than a reviewer's eye. Two real defects motivated it — `docket
init` emitted a policy pointing at a document that did not exist, and `deploy/` shipped referencing
two more — and the deploy check now covers the second class for deployment artefacts only. This
file covers the rest of the tree: every `docs/…md` reference written in code, docs, config or CI
must resolve, and every doc must appear in the site's navigation, because a page nothing links to
is a page nobody reads.

What this does *not* check: whether the prose is true, whether a link points at the right section,
or whether an external URL still resolves (there is no network here, and a test that silently skips
would be worse than the gap it hides). `make site-check` builds the site strictly, which catches
broken anchors within the docs; this test catches a path that does not exist at all.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

#: Text files that may legitimately reference a document by path. Anything else (ledgers, reports,
#: fixtures) is skipped: those are data, and a data file quoting a path is not a link.
SUFFIXES = {".py", ".md", ".toml", ".yml", ".yaml", ".json", ".txt", ".cff", ".sh", ""}

#: Paths that exist only inside a test's own fixture, each with the test that builds it. This is an
#: allow-list rather than a `tests/` exclusion on purpose: the test files also reference *real*
#: documents, and skipping them would trade a small exemption for a large blind spot. Every entry
#: here is a path a test invents to prove the checker that reads paths can fail.
FIXTURE_ONLY = {
    "docs/deploy-guide.md": "tests/test_deploy_check.py appends it to prove a dangling reference is caught",
    "docs/absent.md": "the fixture in this file: a reference that must be reported",
    "docs/present.md": "the fixture in this file: the reference that must resolve",
    "docs/something.md": "this file's own comment describing the pattern it matches",
}

#: `docs/something.md`, optionally with a `#anchor`. The leading character class excludes matches
#: inside a longer path or a URL (`https://…/docs/x.md` is somebody else's document, and this test
#: has no opinion about it).
REFERENCE = re.compile(r"(?<![\w/.-])(docs/[A-Za-z0-9._/-]+\.md)(#[\w-]+)?")


def _text_files() -> list[Path]:
    files: list[Path] = []
    for path in REPO.rglob("*"):
        if path.is_dir() or ".git" in path.parts or "__pycache__" in path.parts:
            continue
        if path.suffix in SUFFIXES:
            files.append(path)
    return files


def test_every_referenced_document_exists():
    """A path in a file is a promise that the file is there."""
    missing: dict[str, list[str]] = {}
    for path in _text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # pragma: no cover - unreadable fixture
            continue
        for match in REFERENCE.finditer(text):
            target = match.group(1)
            if target in FIXTURE_ONLY:
                continue
            if not (REPO / target).exists():
                missing.setdefault(target, []).append(str(path.relative_to(REPO)))
    assert not missing, "referenced documents that do not exist: " + repr(missing)


def test_every_document_is_in_the_site_navigation():
    """A page nothing links to is a page nobody reads — mkdocs builds it and strands it."""
    nav = (REPO / "mkdocs.yml").read_text(encoding="utf-8")
    orphaned = []
    for path in sorted((REPO / "docs").rglob("*.md")):
        relative = path.relative_to(REPO).as_posix()
        if relative not in nav:
            orphaned.append(relative)
    assert not orphaned, f"documents missing from mkdocs.yml nav: {orphaned}"


def test_the_reference_scanner_notices_a_missing_document(tmp_path, monkeypatch):
    """The check has to be able to fail, or it is decoration.

    The scanner is run against a tree containing one real document and one dangling reference, in
    the layout it expects: the failure must name the dangling path, not merely return a non-empty
    list.
    """
    import types

    module = types.SimpleNamespace(REPO=tmp_path, REFERENCE=REFERENCE, SUFFIXES=SUFFIXES)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "present.md").write_text("# present\n", encoding="utf-8")
    (tmp_path / "code.py").write_text("# see docs/present.md and docs/absent.md\n", encoding="utf-8")
    missing: dict[str, list[str]] = {}
    for path in [tmp_path / "code.py", tmp_path / "docs" / "present.md"]:
        for match in module.REFERENCE.finditer(path.read_text(encoding="utf-8")):
            if not (tmp_path / match.group(1)).exists():
                missing.setdefault(match.group(1), []).append(path.name)
    assert missing == {"docs/absent.md": ["code.py"]}
