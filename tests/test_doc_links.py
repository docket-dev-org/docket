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


# --------------------------------------------------------------------------- #
# The site build, which stages the tree before mkdocs sees it
# --------------------------------------------------------------------------- #

INLINE_LINK = re.compile(r"\]\(([^)\s]+)\)")
EXTERNAL = ("http://", "https://", "mailto:", "//", "#")


def _staged_site(tmp_path: Path) -> Path:
    """Stage the repository the way `tools/build_docs_site.py` does, and return the staged root.

    Imported by path for the same reason the other tools are: it is a script, not a package.
    """
    import importlib.util
    import sys

    name = "build_docs_site_under_test"
    spec = importlib.util.spec_from_file_location(name, REPO / "tools" / "build_docs_site.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    site = tmp_path / "site"
    module.stage(site)
    return site


def test_every_relative_link_in_the_docs_resolves_inside_the_staged_site(tmp_path):
    """`mkdocs build --strict` fails on a relative link whose target the staging step did not copy.

    That is not hypothetical: the first strict build of this repository aborted with two warnings,
    both links from the documentation to `tools/*.py` — real files, referenced the way every other
    repository path is referenced, that `stage()` did not carry. The property is checked here rather
    than only in the docs CI job so that it needs no `mkdocs-material` install to run, and so a
    link added to a page cannot pass review by being reasonable.

    Anchor-only links, external URLs and mail links are not this test's business; a target that is a
    directory counts as resolving, because that is how mkdocs treats it.
    """
    site = _staged_site(tmp_path)
    missing: list[str] = []
    seen = 0
    for page in sorted((site / "docs").rglob("*.md")):
        for target in INLINE_LINK.findall(page.read_text(encoding="utf-8")):
            if target.startswith(EXTERNAL):
                continue
            seen += 1
            path = (page.parent / target.split("#", 1)[0]).resolve()
            if not path.exists():
                missing.append(f"{page.relative_to(site).as_posix()} -> {target}")

    # 49 at the time of writing, across the guides and the measurements. The floor is a canary for
    # a matcher that has stopped matching, not a count to keep in step with the documentation.
    assert seen >= 40, f"only {seen} relative links were scanned; the matcher has stopped working"
    assert not missing, "links that the staged site cannot resolve: " + repr(missing)
