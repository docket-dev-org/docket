"""Every documentation path in this repository exists, and every page is reachable.

Documentation is an artefact this project makes claims about, so it gets the treatment an artefact
gets: a mechanical check rather than a reviewer's eye. Two real defects motivated it — `docket
init` emitted a policy pointing at a document that did not exist, and `deploy/` shipped referencing
two more — and the deploy check now covers the second class for deployment artefacts only. This
file covers the rest of the tree: every `docs/…md` reference written in code, docs, config or CI
must resolve, and every doc must appear in the site's navigation, because a page nothing links to
is a page nobody reads.

What this does *not* check: whether the prose is true, whether a link points at the right section,
or whether an external URL still resolves. The last one is deliberate rather than an accident of the
environment: this suite is specified to run with **no network required**, and a test that reached out
would make a bare clone fail or, worse, skip silently — a skip that hides the gap it claims to cover.
(Whether the *build* machine has network is a different question, and this docstring previously
asserted that it did not. It does. Anchors within the docs are checked separately, by
`make site-check`, which builds the site strictly.)
"""

from __future__ import annotations

import importlib.util
import re
import sys
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

import pytest

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


#: Directories that hold a *copy* of the tree rather than the tree. `.mkdocs-site` is staged by
#: `tools/build_docs_site.py` and `site` is what mkdocs writes out of it: every number, every path and
#: every quoted sentence in them is a copy of a file this suite already reads, so scanning them adds
#: nothing and reports a defect in the copy as a defect in the documentation. It is not hypothetical —
#: running `make site` and then the suite made this file fail on its own `CHANGELOG.md` example of a
#: counted reference (`tests/test_x.py`), because the staged copy carries the example too.
BUILD_OUTPUT = {".mkdocs-site", "site"}


def _is_build_output(path: Path) -> bool:
    return any(part in BUILD_OUTPUT for part in path.relative_to(REPO).parts)


def _distribution_name(root: Path) -> str:
    """The distribution name from `pyproject.toml`, which is the single place it is written.

    Read rather than repeated so that a rename cannot leave the guard checking one name while
    the guide tells a reader another. PyPI prohibits the bare name `docket`, so the
    distribution is `docket-ai` while the import, the console script and the command are all
    still `docket`.
    """
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^name\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match, "pyproject.toml has no project name"
    return match.group(1)


def _text_files() -> list[Path]:
    files: list[Path] = []
    for path in REPO.rglob("*"):
        if path.is_dir() or ".git" in path.parts or "__pycache__" in path.parts:
            continue
        if _is_build_output(path):
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


def test_a_staged_site_copy_is_not_read_as_documentation():
    """The exclusion that keeps `make site` from turning the suite red is a claim, so it is pinned.

    `tools/build_docs_site.py` stages a copy of the whole tree into `.mkdocs-site/` and mkdocs writes
    `site/` out of it; both contain every counted reference in this file's own examples, and reading
    them reported a documentation defect that was a duplicate of a file the scanner had already read.
    A copy is not a second opinion, and this test is what says so without building a site.
    """
    assert _is_build_output(REPO / ".mkdocs-site" / "CHANGELOG.md")
    assert _is_build_output(REPO / "site" / "docs" / "testing" / "index.html")
    assert _is_build_output(REPO / ".mkdocs-site" / "docs" / "testing.md")
    assert not _is_build_output(REPO / "docs" / "testing.md")
    assert not _is_build_output(REPO / "CHANGELOG.md")
    assert not _is_build_output(REPO / "docs" / "site-notes.md"), "a name that merely contains 'site' is not a copy"


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

#: The README carries its logo as raw HTML — a `<picture>` with a dark-mode `<source>` and an
#: `<img>` — because that is the only way to switch the artwork with the colour scheme. A raw
#: `src` or `srcset` is invisible to `INLINE_LINK`, which matches `](target)` only, so a logo
#: pointed at a relative path would 404 on the PyPI page exactly like the 29 markdown links this
#: guard was written for and would not be reported. This matcher closes that hole: it reads the
#: `src` and `srcset` attributes of any `img`, `source` or `link` tag in the document.
HTML_ASSET = re.compile(r"<(?:img|source|link)\b[^>]*?(?:src|srcset|href)\s*=\s*\"([^\"]+)\"")


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


def test_the_readme_carries_no_relative_link_the_pypi_page_cannot_resolve() -> None:
    """`README.md` is the PyPI long description, and PyPI does not rewrite relative links.

    PyPI renders the description with no repository around it and resolves a relative target
    against `https://pypi.org/project/docket-ai/`, so every one of them lands on a 404 there
    while resolving perfectly on GitHub. That is invisible from the repository, which is exactly
    why it survived three releases: `README.md` carried 29 relative links, and the first reader
    to notice was a user arriving from `pip install`.

    The property is "absolute, or an in-page anchor", not "absolute GitHub URL": a target that
    names a page the documentation site publishes is pointed at the site, so a reader who
    arrives from PyPI lands in the documentation rather than in raw markdown. `LICENSE` is the
    exception the rule permits, because no site publishes a licence file.

    The offline half — no relative target — is checked without the network. The online half,
    that every absolute target answers 200, is `test_every_url_the_readme_links_to_answers_200`.
    """
    readme = REPO / "README.md"
    text = readme.read_text(encoding="utf-8")
    markdown_targets = INLINE_LINK.findall(text)
    html_targets = [
        target.split()[0] for match in HTML_ASSET.findall(text) for target in match.split(",") if target.strip()
    ]
    relative = sorted(
        {
            target
            for target in markdown_targets + html_targets
            if not target.startswith(EXTERNAL) and not target.startswith("#")
        }
    )
    assert not relative, (
        "README.md is the PyPI long description; these relative targets 404 on the package page: " + repr(relative)
    )


def test_every_url_the_readme_links_to_answers_200() -> None:
    """Every absolute URL in `README.md` answers 200, from this machine, right now.

    This is the half that needs the network, so it is a separate test: a contributor with no
    network can still run the suite, and a link that rots is reported as a link that rots
    rather than as a suite that cannot start. `requests` is not a dependency of the package,
    so this uses `urllib` from the standard library.

    Skipped, not failed, when there is no network: "could not check" is not "broken", and a
    red suite on a plane teaches people to ignore red suites.
    """
    import urllib.error
    import urllib.request

    readme = REPO / "README.md"
    text = readme.read_text(encoding="utf-8")
    targets = sorted(
        {
            target.split("#", 1)[0]
            for target in INLINE_LINK.findall(text) + HTML_ASSET.findall(text)
            if target.startswith(("http://", "https://"))
        }
    )
    assert targets, "no absolute links found in README.md; the matcher has stopped working"

    # shields.io answers 403 to `Python-urllib/3.x` and 200 to a browser. The page these links
    # sit on is read by browsers, so the check asks the question a browser asks. Without the
    # header the badge images fail and the test reports three links that are not broken.
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}

    probe = urllib.request.Request("https://pypi.org/pypi/docket-ai/json", method="HEAD", headers=headers)
    try:
        with urllib.request.urlopen(probe, timeout=20) as response:
            response.read(1)
    except (TimeoutError, urllib.error.URLError, OSError):
        pytest.skip("no network: the link check needs to reach pypi.org")

    broken: list[str] = []
    for url in targets:
        request = urllib.request.Request(url, method="GET", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                response.read(1)
        except urllib.error.HTTPError as error:
            broken.append(f"{error.code} {url}")
        except (TimeoutError, urllib.error.URLError, OSError) as error:
            broken.append(f"{type(error).__name__} {url}")
    assert not broken, "URLs in README.md that do not answer 200: " + repr(broken)


def _collected_test_counts() -> dict[str, int]:
    """What `pytest --collect-only` reports per test file, from this interpreter's own run.

    Shared by the two guards below because they ask the same question of two different documents,
    and a second copy of this subprocess call would be a second thing to keep in step.
    """
    import os
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "--collect-only", "-q"],
        cwd=str(root),
        env=dict(os.environ, PYTHONPATH=str(root / "src")),
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert completed.returncode == 0, completed.stdout[-2000:] + completed.stderr[-2000:]
    collected = {
        path.removeprefix("tests/"): int(count)
        for path, count in re.findall(r"^(tests/[\w./]+\.py): (\d+)$", completed.stdout, re.MULTILINE)
    }
    assert len(collected) >= 30, f"collection output was not parsed as expected: {completed.stdout[-500:]!r}"
    return collected


def test_the_counted_test_table_in_the_docs_matches_the_suite():
    """The table of test counts is part of the document's claim, so it is checked like one.

    `docs/testing.md` says its counts are collected rather than remembered, and for a while the
    sentence went on to say they were "checked by the CI run" when nothing checked them: a number
    in a table is exactly the kind of thing that goes stale silently while every test stays green
    (this repository has already shipped a stale conformance vector for that reason). So the claim
    is now enforced: the table's rows must equal what `pytest --collect-only` reports per file, and
    every collected file must appear in the table. A canary asserts the parse found a table at all,
    because a regex that matches nothing would make this test pass by accident.
    """
    root = Path(__file__).resolve().parents[1]
    collected = _collected_test_counts()

    document = (root / "docs" / "testing.md").read_text(encoding="utf-8")
    documented = {
        # The capture stops before the extension (the trailing `\.py` is matched outside the
        # group), so it is put back here rather than left to a comparison that would silently
        # see two disjoint sets of names.
        f"{name}.py": int(count)
        for name, count in re.findall(r"^\| `(test_[\w.]+)\.py` \| (\d+) \|", document, re.MULTILINE)
    }
    assert len(documented) >= 30, f"the counted table in docs/testing.md was not found (parsed {sorted(documented)})"

    missing = sorted(set(collected) - set(documented))
    extra = sorted(set(documented) - set(collected))
    assert not missing, f"test files missing from the table in docs/testing.md: {missing}"
    assert not extra, f"the table in docs/testing.md names files that do not exist: {extra}"
    wrong = {
        name: (documented[name], collected[name]) for name in sorted(documented) if documented[name] != collected[name]
    }
    assert not wrong, f"docs/testing.md test counts are stale (documented, actual): {wrong}"


#: The living documents, by contrast with the two that are records by construction: `CHANGELOG.md`
#: describes the tree as it was on the day of a release, and `docs/measurements/**` is dated evidence
#: of one run on one host. A count in either is a statement about *then*, so re-basing it in a later
#: commit would be falsifying the record, not fixing it. Neither is a promise about today's suite, and
#: neither is scanned here.
RECORD_NOT_PROMISE = ("CHANGELOG.md", "docs/measurements/")

#: The three shapes a living document uses to say "this test file has N tests": backticked filename
#: then `(N)`, then `(N tests)`, and the inverted "N tests in `tests/x.py`". Deliberately narrow — a
#: bare number in prose is not a claim about the suite, but a number written directly against a test
#: filename can only be the size of that file.
COUNTED_REFERENCES = (
    re.compile(r"`tests/(test_[\w.]+\.py)` \((\d+)(?: tests?)?[),;]"),
    re.compile(r"(\d+) tests? in `tests/(test_[\w.]+\.py)`"),
)

#: Every place that quotes the size of the *whole suite*, as `(file, regex with one group)`. Named
#: rather than pattern-matched across the tree because the number appears in prose that also uses
#: the word "tests" for other things ("28 tests, not skipped — when it was absent"), and a scanner
#: loose enough to catch all four of these would also rewrite that sentence.
SUITE_TOTAL_QUOTES = (
    ("README.md", r"make check` \((\d+) tests"),
    ("README.md", r"That is (\d+) tests in about three minutes"),
    ("CONTRIBUTING.md", r"# (\d+) tests, fully offline"),
    ("docs/testing.md", r"# (\d+) tests, no network"),
    ("docs/sre-coverage.md", r"the whole suite: (\d+) tests"),
)


#: A source line cited in prose — `verify.py:554`, `src/docket/proxy.py:590` — is a claim about a file
#: that moves, and it is the failure mode the mutation paydown notes in `tests/test_cache.py` were
#: written about: a name in a docstring is a line-number index, and the index expires when the file
#: does. So the citations are guarded the way the counts are. The lookbehind keeps the scanner out of
#: longer paths and URLs, exactly as the document-reference pattern above does.
CITED_LINE = re.compile(r"(?<![\w/.-])((?:[A-Za-z_][A-Za-z0-9_]*/)*[a-z_][A-Za-z0-9_]*\.py):(\d+)")

#: Where a bare `verify.py:186` is looked up. A name that matches in more than one of these is an
#: ambiguity, not a citation, and is reported as one.
SOURCE_DIRS = ("src/docket", "tools", "tests")


def _harness():
    """`tools/mutate.py`, loaded from its path, so this guard can ask it what a line carries.

    Registered in `sys.modules` before it executes: it defines a dataclass, and `dataclasses` resolves
    string annotations through `sys.modules[cls.__module__]`.
    """
    name = "docket_mutate_for_the_doc_guard"
    spec = importlib.util.spec_from_file_location(name, REPO / "tools" / "mutate.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_every_source_line_cited_in_the_docs_is_a_line_that_exists():
    """Three questions per citation, and the third is the one that makes the other two worth asking.

    A cited line has to exist (the file is there, and it has that many lines), it has to be a line
    with something on it, and — when the file is one the harness mutates at all — it has to be a line
    that still carries a mutable site. Without the third check a citation drifts quietly to a line
    that compiles and does nothing, which is how a survivor table becomes decoration: the survivor
    tables and the paydown notes are the two places in this repository that name line numbers as
    evidence, and both of them are read by people deciding whether a claim is worth checking.
    """
    harness = _harness()
    corpus = sorted(harness.DEFAULT_TESTS)
    sites = {
        source: {
            mutant.line for mutant in harness.mutants_for_source((REPO / source).read_text(encoding="utf-8"), source)
        }
        for source in corpus
    }
    assert sites, "the harness's DEFAULT_TESTS is empty: the third question would be vacuous"

    problems: list[str] = []
    checked = 0
    for page in sorted(REPO.rglob("*.md")):
        if ".git" in page.parts or _is_build_output(page):
            continue
        relative = page.relative_to(REPO).as_posix()
        if relative in RECORD_NOT_PROMISE or relative.startswith(RECORD_NOT_PROMISE[1]):
            continue
        for match in CITED_LINE.finditer(page.read_text(encoding="utf-8")):
            raw, number = match.group(1), int(match.group(2))
            if "/" in raw:
                path = REPO / raw
            else:
                found = [REPO / directory / raw for directory in SOURCE_DIRS if (REPO / directory / raw).is_file()]
                if not found and (REPO / raw).is_file():
                    found = [REPO / raw]
                if len(found) != 1:
                    problems.append(
                        f"{relative}: {raw}:{number} — "
                        + ("no such file" if not found else f"ambiguous: {[str(p.relative_to(REPO)) for p in found]}")
                    )
                    continue
                path = found[0]
            if not path.is_file():
                problems.append(f"{relative}: {raw}:{number} — no such file")
                continue
            lines = path.read_text(encoding="utf-8").splitlines()
            if not 1 <= number <= len(lines):
                problems.append(f"{relative}: {raw}:{number} — the file has {len(lines)} lines")
                continue
            if not lines[number - 1].strip():
                problems.append(f"{relative}: {raw}:{number} — that line is blank")
                continue
            source = path.relative_to(REPO).as_posix()
            if source in sites and number not in sites[source]:
                problems.append(f"{relative}: {raw}:{number} — no site the harness would mutate")
                continue
            checked += 1
    assert checked >= 20, f"only {checked} citations were found: the pattern has stopped matching"
    assert not problems, "a source line cited in the docs is stale: " + "; ".join(problems)


def test_every_count_written_beside_a_test_filename_matches_the_suite():
    """The same number, in a dozen other sentences, and four of them were wrong.

    The table guard above covers the one place that says "these counts are collected". Nothing
    covered the sentence form — and that is where the rot had already happened: `docs/resilience.md`
    and `docs/sre-coverage.md` both said `test_resilience.py` had 49 tests (it had 54),
    `docs/sre-coverage.md` said `test_helm_fidelity.py` had 5 (it had 17, after the tool's own
    contract was written), `docs/caching.md` said `test_cache.py` had 41 (55), and
    `docs/production-coverage.md` said `test_chaos.py` had 32 (33). Every one of those sentences was
    written when its number was true, which is exactly why a number needs a reader: the fix is not
    to write more carefully.

    A reference to a file that does not exist fails too — a renamed test file would otherwise leave
    a count that no run can contradict. The canary is the number of references found: if the
    patterns stop matching, this test says so instead of passing over an empty set.
    """
    root = Path(__file__).resolve().parents[1]
    collected = _collected_test_counts()

    # Every occurrence, not one per filename. The first version kept a dict keyed by the test file
    # and `setdefault`, so only the alphabetically first mention of each file was ever compared —
    # `docs/sre-coverage.md`'s stale count of `test_chaos.py` sat behind two correct mentions for
    # weeks, which is the same failure this whole test exists to fix, one level up.
    found: list[tuple[str, str, int]] = []
    for page in sorted(root.rglob("*.md")):
        relative = page.relative_to(root).as_posix()
        if (
            ".git" in page.parts
            or _is_build_output(page)
            or relative in RECORD_NOT_PROMISE
            or relative.startswith(RECORD_NOT_PROMISE[1])
        ):
            continue
        text = page.read_text(encoding="utf-8")
        for pattern in COUNTED_REFERENCES:
            for first, second in pattern.findall(text):
                name, count = (first, second) if first.startswith("test_") else (second, first)
                found.append((relative, name, int(count)))

    assert len(found) >= 15, (
        f"only {len(found)} counted test references were found in the tree; the patterns have stopped "
        f"matching ({[f'{where}:{name}' for where, name, _ in found]})"
    )
    unknown = sorted({name for _, name, _ in found if name not in collected})
    assert not unknown, f"the docs count tests in files that do not exist: {unknown}"
    stale = [
        f"{where} says {name} has {documented}, the file has {collected[name]}"
        for where, name, documented in found
        if documented != collected[name]
    ]
    assert not stale, "a count written beside a test filename is stale: " + "; ".join(stale)


def test_every_place_that_quotes_the_size_of_the_suite_quotes_it_correctly():
    """Five sentences promise the reader a number for the whole suite, and nothing read any of them.

    The suite's size is the first quantitative thing a reader of the README meets, and it is the number
    most likely to be quoted onward — in a blog post, a CV, a comparison table. It had no guard for
    the same reason the per-file counts had none: the number was true when it was written, and the
    table guard that does exist measures files, not totals. Each place is named with the sentence it
    sits in, so a deleted phrase fails loudly instead of matching nothing.
    """
    root = Path(__file__).resolve().parents[1]
    collected = _collected_test_counts()
    total = sum(collected.values())

    for filename, pattern in SUITE_TOTAL_QUOTES:
        page = root / filename
        assert page.is_file(), f"{filename} is quoted in the suite-size list and is not in the tree"
        matches = re.findall(pattern, page.read_text(encoding="utf-8"))
        assert len(matches) == 1, (
            f"{filename}: expected exactly one match for {pattern!r}, found {len(matches)}. If the "
            f"sentence moved, move it in SUITE_TOTAL_QUOTES with it"
        )
        assert int(matches[0]) == total, (
            f"{filename} tells the reader the suite has {matches[0]} tests; pytest collects {total} "
            f"in {len(collected)} files"
        )


def test_every_flag_the_cli_reference_documents_is_an_option_of_that_command():
    """`docs/cli.md` is 397 lines of flags, and none of them was checked against the parser.

    A documented flag that no command accepts is a command that does not run, and the failure is
    invisible until a reader copies a line — the same class as a stale count, with a worse blast
    radius, because the reader has to discover it at a terminal. The check is narrow on purpose: only
    level-2 sections whose heading *is* a `docket <command>` line are scanned, so the flags belonging
    to `tools/` scripts documented further down the same page are not read as the CLI's. Every
    subcommand named in a heading must exist too, and the scan asserts it checked a plausible number
    of flags — a heading rename would otherwise turn this green by finding nothing.
    """
    import argparse

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from docket import cli  # the same package the docs describe, imported from the tree under test

    def options(parser: argparse.ArgumentParser) -> set[str]:
        found: set[str] = set()
        for action in parser._actions:
            found.update(action.option_strings)
            if isinstance(action, argparse._SubParsersAction):
                for child in action.choices.values():
                    found |= options(child)
        return found

    parser = cli.build_parser()
    subcommands = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
    document = (Path(__file__).resolve().parents[1] / "docs" / "cli.md").read_text(encoding="utf-8")

    checked = 0
    problems: list[str] = []
    for section in re.split(r"^## ", document, flags=re.MULTILINE)[1:]:
        heading = section.split("\n", 1)[0]
        match = re.match(r"`docket ([a-z-]+)", heading)
        if not match:
            continue
        command = match.group(1)
        if command not in subcommands.choices:
            problems.append(f"docs/cli.md documents `docket {command}`, which the CLI does not define")
            continue
        known = options(parser) | options(subcommands.choices[command])
        body = section.split("\n## ", 1)[0]
        for flag in sorted(set(re.findall(r"(?<![\w-])(--[a-z][a-z0-9-]+)", body))):
            checked += 1
            if flag not in known:
                problems.append(f"docs/cli.md / `docket {command}`: {flag} is not an option of that command")

    assert checked >= 60, f"the scan read {checked} flags from docs/cli.md — the headings have changed shape"
    assert not problems, "\n".join(problems)


#: Where an absolute authoring path is legitimate: the raw measurement reports carry the `command`,
#: `ledger_path` and `receipt_path` their harness ran with, unedited, and that directory's README
#: says so in as many words. Everywhere else, a path from the machine that wrote the docs is a dead
#: end for the reader — and this tree has already shipped one defect of that shape (a `.gitignore`
#: rule that kept two measurement reports out of every clone while the documents cited them).
AUTHORING_PATHS_ALLOWED_IN = ("docs/measurements/",)

#: Directories the tree generates and the repository ignores. A staged copy of the docs is not a
#: document a reader can be sent to, and neither is a built site: scanning them made this test fail on
#: its first full-suite run, on `.mkdocs-site/`, a directory `tools/build_docs_site.py` had left behind
#: from an earlier build. The list is the same set that tool skips when it stages the tree.
GENERATED_DIRECTORIES = (".mkdocs-site", "site", "dist", "build", ".venv", "node_modules", "__pycache__")


def test_no_prose_document_leaks_a_path_from_the_machine_that_wrote_it():
    """`/home/user/…` in a document that is not raw evidence means the reader cannot follow it."""
    root = Path(__file__).resolve().parents[1]
    paths = ("/home/user/", "/Users/", "/tmp/venv-")
    problems: list[str] = []
    checked_files = 0
    for path in sorted(root.rglob("*.md")):
        relative = path.relative_to(root).as_posix()
        if any(relative.startswith(prefix) for prefix in AUTHORING_PATHS_ALLOWED_IN):
            continue
        if any(part in GENERATED_DIRECTORIES for part in path.relative_to(root).parts):
            continue
        checked_files += 1
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for needle in paths:
                if needle in line:
                    problems.append(f"{relative}:{number} names {needle}… — {line.strip()[:100]}")
    assert checked_files >= 15, f"the scan read {checked_files} markdown files — is it looking in the tree?"
    assert not problems, "\n".join(problems)


#: The two tables whose subject *is* the confidence of a claim. Every row in them has to carry a
#: grade, because a row without one is a claim that has quietly become the same weight as a sourced
#: one — and this repository's whole evidence standard is that the difference is visible. The tables
#: are found by their own header lines rather than by position, so a new table above them does not
#: make this pass for the wrong reason.
GRADED_TABLES = (
    ("docs/evidence.md", "| Claim as used | Source | Grade |", r"^\*\*[A-D]"),
    ("docs/incident-corpus.md", "| date | what happened | grade | source |", r"^\*{0,2}[A-D]\b"),
)


def test_every_row_of_a_graded_table_carries_a_grade():
    """Fifteen of `docs/evidence.md`'s rows are graded A–D; a row that is not graded is smuggled in.

    Written after the correction of 2026-10-16, when one row's claim turned out to be unsupported
    while the same table's neighbouring row *declined* to cite the number at all: the table's grades
    are the reason that contradiction was findable, and a row without one would have removed the
    handle.
    """
    root = Path(__file__).resolve().parents[1]
    for relative, header, grade in GRADED_TABLES:
        page = root / relative
        assert page.is_file(), f"{relative} is named here and is not in the tree"
        lines = page.read_text(encoding="utf-8").splitlines()
        assert header in lines, f"{relative}: the graded table's header row changed: {header!r}"
        start = lines.index(header) + 2  # the header, then the divider
        ungraded: list[str] = []
        rows = 0
        for line in lines[start:]:
            if not line.startswith("|"):
                break
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            rows += 1
            if not re.match(grade, cells[2] if len(cells) > 2 else ""):
                ungraded.append(f"{relative}: {line[:90]}…")
        assert rows >= 7, f"{relative}: the graded table has {rows} rows — has it been emptied?"
        assert not ungraded, "rows without a grade:\n" + "\n".join(ungraded)


def test_every_make_target_the_repository_names_exists():
    """`make deploy-check` in a workflow, or in the Makefile's own help, has to be a real target.

    A CI job that calls a target nobody defined fails for whoever enables it, and a help line that
    promises a target is a documented command that does not run — the same class of defect as a
    count in a table. Only the workflow commands and the Makefile's `help` text are scanned: prose
    saying "make sure" or "make decisions" is not a command, and a scanner that cannot tell the
    difference would be a scanner nobody could keep green.
    """
    root = Path(__file__).resolve().parents[1]
    makefile = (root / "Makefile").read_text(encoding="utf-8")
    targets = set(re.findall(r"^([a-z][a-z-]*):", makefile, re.MULTILINE))
    assert "test" in targets and "check" in targets, f"the Makefile's targets do not parse: {sorted(targets)}"

    checked = 0
    problems: list[str] = []
    for path in sorted((root / ".github" / "workflows").glob("*.yml")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            match = re.search(r"(?<![\w-])make ([a-z][a-z-]{2,})", line)
            if not match:
                continue
            checked += 1
            if match.group(1) not in targets:
                problems.append(
                    f"{path.name}:{number} calls `make {match.group(1)}`, which the Makefile does not define"
                )
    for number, line in enumerate(makefile.splitlines(), start=1):
        match = re.search(r'@echo "make ([a-z][a-z-]{2,})', line)
        if not match:
            continue
        checked += 1
        if match.group(1) not in targets:
            problems.append(f"Makefile:{number} help text promises `make {match.group(1)}`")
    assert checked >= 5, f"the scan found {checked} make commands — the pattern has stopped matching"
    assert not problems, "\n".join(problems)


#: The document that carries the evidence ledger, and the sources whose *internal ranking* this
#: repository is not entitled to state. On 2026-10-16 three shipped files said OWASP's agentic top ten
#: puts "denial of wallet" at risk #2 — a specific, checkable, and wrong claim (ASI02 is *Tool Misuse
#: and Exploitation*, and cost is not one of that list's ten classes at all). One of the same three
#: files already hedged that the rank "could not be pinned to the primary text", so the repository was
#: contradicting itself in adjacent paragraphs. The rule this test holds is narrow and is the one that
#: was broken: a claim about a position *inside* a source list needs the source, and where the primary
#: page does not enumerate its entries, no position may be asserted at all.
EVIDENCE_LEDGER = "docs/evidence.md"
RANKED_LIST_SOURCES = {
    "agentic": (
        "Top 10 for Agentic Applications",
        "https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/",
    ),
}


def test_no_document_asserts_a_rank_inside_a_source_that_does_not_publish_one():
    """A rank is the most precise kind of citation and the easiest to invent.

    Two halves, because either alone can be satisfied vacuously: every document that discusses the
    agentic list must be free of a position claim against it, *and* the repository must still discuss
    it — deleting the paragraph would otherwise be a way to pass this test.
    """
    root = Path(__file__).resolve().parents[1]
    discussions: list[str] = []
    problems: list[str] = []

    #: A position inside a list: `risk #2`, `risk 2`, `ranked #2`, `#2 risk`, `item 2 of`.
    rank = re.compile(r"(?:risk|rank|ranked|item|entry|position)\s*#?\s*\d+|#\s*\d+\s*(?:risk|item)", re.I)

    for path in sorted(root.rglob("*.md")):
        if any(part in {".git", "node_modules"} for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8")
        for label, (title, url) in RANKED_LIST_SOURCES.items():
            if title not in text and url not in text:
                continue
            discussions.append(f"{path.relative_to(root)} ({label})")
            for number, line in enumerate(text.splitlines(), start=1):
                if title not in line and url not in line:
                    continue
                # The line may *quote* the mistake in order to correct it; that is how the
                # correction is recorded, and it is written as a quotation in backticks.
                stripped = line.replace("`", "")
                match = rank.search(stripped)
                if match and "earlier version" not in stripped and "earlier draft" not in stripped:
                    problems.append(
                        f"{path.relative_to(root)}:{number} states a position inside {label}: "
                        f"{match.group(0)!r} — {line.strip()[:110]}"
                    )

    assert discussions, (
        "no document discusses the OWASP agentic list any more. If that is deliberate, remove the "
        "source from RANKED_LIST_SOURCES with the reason; if not, the discussion was deleted"
    )
    assert not problems, "\n".join(problems)


#: The commands the repository itself ships that run the suite: the Makefile target a contributor is
#: told to run, and the four CI jobs that run it. `pyproject.toml` gives all of them `-q` through
#: `addopts`, so a `-q` written on one of these lines is the *second* one — `-qq`, at which pytest
#: prints no counts line. That was measured, not assumed: with `addopts = "-q --strict-markers"`,
#: `pytest tests -q` prints the progress line and nothing else, exits 0, and a reader — or a script —
#: has no idea how many tests ran. `-ra` is required here for the same reason: a cell that skips
#: without saying why is indistinguishable from one that never had the test, which is the sentence
#: `docs/testing.md` uses to explain why its matrix was measured with it.
SHIPPED_PYTEST_COMMANDS = (
    "Makefile",
    ".github/workflows/ci.yml",
    ".github/workflows/release.yml",
    ".github/workflows/security.yml",
)


def test_no_shipped_pytest_command_asks_for_less_than_the_line_the_docs_quote():
    """Every command in this repository that runs the suite prints its counts, and says what skipped.

    Written after finding the same defect in three separate places on 2026-10-16 — `tools/mutate.py`'s
    control run, `mutation/run_final_checks.sh`, and then the Makefile and four CI jobs, which had been
    doing it since they were written. In the harness it was load-bearing (a control whose counts are
    missing cannot be read); in CI it was quieter: the job goes green with no line saying how many
    tests passed, which is the evidence a reader of a green badge is entitled to. The documentation in
    `docs/testing.md` states the rule; nothing enforced it, so this does, in the file whose subject is
    the documents matching what the repository actually does.
    """
    root = Path(__file__).resolve().parents[1]
    addopts = [
        token
        for line in (root / "pyproject.toml").read_text(encoding="utf-8").splitlines()
        if line.startswith("addopts")
        for token in line.split("=", 1)[1].strip().strip('"').split()
    ]
    assert addopts.count("-q") == 1, (
        f"`pyproject.toml`'s addopts are {addopts!r}: this test's arithmetic assumes exactly one `-q` "
        f"there, which every shipped command line is then measured against"
    )

    problems: list[str] = []
    for relative in SHIPPED_PYTEST_COMMANDS:
        page = root / relative
        assert page.is_file(), f"{relative} is named here and is not in the tree"
        for number, line in enumerate(page.read_text(encoding="utf-8").splitlines(), start=1):
            if "-m pytest" not in line and not line.strip().startswith(("run: pytest", "pytest ")):
                continue
            tokens = line.split()
            quiet = [token for token in tokens if token.startswith("-q")]
            skipped = [token for token in tokens if token == "-ra" or token == "-r" or token.startswith("-r")]
            extra = sum(token.count("q") for token in quiet)
            if extra:
                problems.append(
                    f"{relative}:{number} passes {quiet} on top of addopts' `-q` — that is `-qq`, "
                    f"where pytest prints no counts line: {line.strip()}"
                )
            if not skipped:
                problems.append(
                    f"{relative}:{number} runs the suite without `-ra`, so a skip is silent: {line.strip()}"
                )
    assert not problems, "\n".join(problems)


#: The concepts `docs/production-coverage.md` undertakes to cover, by section. This list *is* the
#: requirement: it is the set of terms the map was written to answer, kept here so that a row which
#: disappears — or a section that quietly shrinks — fails a test instead of only making the document
#: shorter. Spellings follow the document's own house style (`optimisation`, `p99 / tail latency`).
CONCEPT_MAP = {
    "1. Infrastructure and deployment": (
        "Rate limiting",
        "Caching",
        "Load balancing",
        "Reverse proxies",
        "API gateways",
        "CI/CD",
        "Docker",
        "Kubernetes",
        "Service discovery",
    ),
    "2. Backend and distributed systems": (
        "Circuit breakers",
        "Timeouts",
        "Retries",
        "Exponential backoff",
        "Idempotency",
        "Message queues",
        "Pub/sub",
        "Event-driven architecture",
        "Distributed transactions",
        "Saga / compensating actions",
        "Dead-letter queues",
        "Cron jobs",
        "WebSockets",
        "Long polling",
        "SSE (server-sent events)",
    ),
    "3. Databases and concurrency": (
        "Indexing",
        "Query optimisation",
        "Replication",
        "Leader election",
        "CAP theorem",
        "Eventual consistency",
        "Optimistic locking",
        "Pessimistic locking",
        "Distributed locks",
        "Race conditions",
        "Deadlocks",
        "Memory leaks",
        "Garbage collection",
        "Thread safety",
        "Backpressure",
    ),
    "4. Performance and scalability": (
        "Autoscaling",
        "Horizontal scaling",
        "Vertical scaling",
        "CDN",
        "Edge caching",
        "Cache invalidation",
        "Feature flags",
        "Database migrations",
        "Schema versioning",
        "Disaster recovery",
        "Backups",
        "Failover",
        "Multi-region",
        "Chaos engineering",
        "Cost optimisation",
        "Cold starts",
        "Serverless limits",
        "Latency",
        "Throughput",
        "p99 / tail latency",
    ),
    "5. Networking and operations": (
        "Network partitions",
        "Clock skew",
        "DNS",
        "TCP vs UDP",
        "HTTP/2",
        "HTTP/3",
        "gRPC",
        "Webhooks",
        "API versioning",
        "Semantic versioning",
        "IaC",
        "Terraform",
        "Helm charts",
        "Build caching",
        "Dependency hell",
        "Production incidents",
        "On-call",
        "Post-mortems",
    ),
}

#: The five statuses the map's legend defines. A cell may add a qualification in parentheses
#: (`**out of scope (by design)**`), but the status itself has to be one of these — a sixth status
#: would mean a reader cannot tell what the legend promised from what the row says.
DOCUMENTED_STATUSES = ("built", "measured", "modelled", "reasoned", "out of scope")


def test_the_production_coverage_map_covers_every_concept_it_claims_to():
    """The map's rows, the number quoted for it, and its own legend are checked here.

    The README once called this document a "~110-concept" map. It has 77 rows, and it has had 77 since it
    was written: the number was never measured, which is the same failure as a stale test count or a
    drifted conformance vector, and the fix is the same one — count it, in a test, and make the
    places that quote it quote the counted number.
    """
    root = Path(__file__).resolve().parents[1]
    document = (root / "docs" / "production-coverage.md").read_text(encoding="utf-8")

    found: dict[str, list[str]] = {}
    for section, expected in CONCEPT_MAP.items():
        assert f"## {section}" in document, f"section '## {section}' is gone from the coverage map"
        body = document.split(f"## {section}", 1)[1].split("\n## ", 1)[0]
        rows = re.findall(r"^\| ([^|]+?) \| \*\*([^|]+?)\*\*", body, re.MULTILINE)
        found[section] = [name for name, _ in rows]
        assert found[section] == list(expected), (
            f"{section}: the map's rows no longer match the concepts it undertakes to cover\n"
            f"  missing: {sorted(set(expected) - set(found[section]))}\n"
            f"  unexpected: {sorted(set(found[section]) - set(expected))}\n"
            f"  order/naming differs: {[a for a, b in zip(found[section], expected, strict=False) if a != b]}"
        )
        for name, status in rows:
            base = re.split(r",| \(", status, maxsplit=1)[0].strip()
            assert base in DOCUMENTED_STATUSES, (
                f"{section} / {name}: status {status!r} is not one of the legend's: {DOCUMENTED_STATUSES}"
            )

    total = sum(len(rows) for rows in found.values())
    for filename in ("README.md", "CHANGELOG.md"):
        text = (root / filename).read_text(encoding="utf-8")
        quoted = re.findall(r"(\d+) production-engineering concepts", text)
        assert quoted, f"{filename} no longer quotes a concept count for the coverage map"
        for value in quoted:
            assert int(value) == total, f"{filename} says {value} concepts; the map has {total}"


#: The resilience practices `docs/sre-coverage.md` undertakes to cover, by section. Like
#: :data:`CONCEPT_MAP`, this list *is* the requirement: the practices the project was asked to
#: practise. A row that disappears — or a practice quietly renamed — fails here instead of making the
#: document shorter and the claim broader.
SRE_PRACTICES = {
    "1. Failing on purpose": (
        "Stress testing",
        "Fuzzing",
        "Chaos engineering",
        "Chaos monkey",
        "Game days",
        "Red teaming",
    ),
    "2. Watching": (
        "Dynamic analysis",
        "Vulnerability scanning",
        "Synthetic monitoring",
        "Proactive monitoring",
        "Observability",
        "FMEA",
    ),
    "3. Recovering, limiting, hardening": (
        "Circuit breaking",
        "Rate limiting",
        "Automated rollback",
        "Disaster recovery",
        "Software rejuvenation",
        "Degradation testing",
        "Blast-radius mitigation",
        "IaC testing",
        "Post-mortems",
        "Infrastructure hardening",
    ),
}


def test_the_resilience_practice_map_covers_every_practice_it_claims_to():
    """Twenty-two practices, one row each, in a documented status, and a count that is counted.

    The map was written because two of the practices it covers — the FMEA and the post-mortem
    habit — were being *asserted* in prose rather than done, and a coverage document is the cheapest
    place for that to happen again: a row is easy to delete, a paragraph is easier. So the rows are
    enumerated here, the statuses have to be the legend's, and the number the README quotes has to be
    the number of rows that exist.
    """
    root = Path(__file__).resolve().parents[1]
    document = (root / "docs" / "sre-coverage.md").read_text(encoding="utf-8")

    total = 0
    for section, expected in SRE_PRACTICES.items():
        assert f"## {section}" in document, f"section '## {section}' is gone from the practice map"
        body = document.split(f"## {section}", 1)[1].split("\n## ", 1)[0]
        rows = re.findall(r"^\| \*\*([^*]+)\*\* \| \*\*([^|]+?)\*\*", body, re.MULTILINE)
        names = [name.strip() for name, _ in rows]
        assert names == list(expected), (
            f"{section}: the map's rows no longer match the practices it undertakes to cover\n"
            f"  missing: {sorted(set(expected) - set(names))}\n"
            f"  unexpected: {sorted(set(names) - set(expected))}\n"
            f"  order/naming differs: {[a for a, b in zip(names, expected, strict=False) if a != b]}"
        )
        for name, status in rows:
            base = re.split(r",| \(| / ", status, maxsplit=1)[0].strip()
            assert base in DOCUMENTED_STATUSES, (
                f"{section} / {name}: status {status!r} is not one of the legend's: {DOCUMENTED_STATUSES}"
            )
        total += len(rows)

    assert total == 22, f"the practice map has {total} rows; twenty-two practices were catalogued"
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert f"{total} resilience practices" in readme, (
        "the README no longer points at the practice map by its counted size"
    )


def test_every_failure_mode_in_the_fmea_names_a_detector_that_exists():
    """The FMEA's ratings are opinions; its detector column is evidence, and evidence is checkable.

    `docs/resilience.md` carries a failure-mode-and-effects table: sixteen ways a run fails, each
    rated severity/occurrence/detection, ordered by risk, each naming the artefact that catches it.
    The ratings are a judgement and the document says so. What a test can do is hold the parts that
    are not a judgement to the standard the rest of this repository is held to — that a named path
    exists, that the risk score is the product it claims to be, and that the ordering is the ordering
    claimed (highest risk first, so the row a reader should care about is at the top rather than
    wherever it was written).
    """
    root = Path(__file__).resolve().parents[1]
    document = (root / "docs" / "resilience.md").read_text(encoding="utf-8")
    heading = "## Failure modes, ranked by what they would cost (an FMEA)"
    assert heading in document, "the FMEA section is gone from docs/resilience.md"
    body = document.split(heading, 1)[1].split("\n## ", 1)[0]

    rows = re.findall(
        r"^\| (?!---)([^|]+?) \| ([^|]+?) \| (\d) \| (\d) \| (\d) \| (\d+) \| (.+?) \|$",
        body,
        re.MULTILINE,
    )
    assert len(rows) >= 16, f"the FMEA analyses {len(rows)} failure modes; it analysed sixteen"

    risks: list[int] = []
    for mode, _cost, severity, occurrence, detection, risk, catcher in rows:
        for value, name in ((severity, "severity"), (occurrence, "occurrence"), (detection, "detection")):
            assert 1 <= int(value) <= 5, f"{mode.strip()}: {name} {value} is outside the 1-5 scale"
        product = int(severity) * int(occurrence) * int(detection)
        assert product == int(risk), f"{mode.strip()}: risk {risk} is not {severity}×{occurrence}×{detection}"
        risks.append(int(risk))

        paths = re.findall(r"`([A-Za-z0-9_./-]+\.(?:py|md|json|mjs|yaml|yml|toml))`", catcher)
        assert paths, f"{mode.strip()}: the catcher names no artefact to check"
        for path in paths:
            assert (root / path).exists(), (
                f"{mode.strip()}: {path} is named as what catches this failure and is not in the tree"
            )

    assert risks == sorted(risks, reverse=True), (
        f"the FMEA's rows are no longer ordered by risk, highest first: {risks}"
    )


#: Documentation that a reader is expected to open, and therefore paths in it that a clone must
#: carry. `docs/measurements/**` is in here on purpose: it is evidence, and the defect these two
#: tests were written for was a report the FMEA cited and no clone had.
DOCUMENTATION = ("README.md", "CONTRIBUTING.md", "SUPPORT.md")

#: A file named in prose. Narrow by suffix, for the same reason the citation scanner is: a backticked
#: word is not a path, and a backticked path with an extension in this set can only be one.
NAMED_FILE = re.compile(r"`([A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:py|md|json|mjs|yaml|yml|toml|txt|cfg|sh|html))`")


@dataclass(frozen=True)
class _Ignore:
    """One `.gitignore` rule, read into the four things matching needs."""

    number: int
    line: str
    negated: bool
    directory: bool
    anchored: bool
    pattern: str


def _ignore_rules(text: str) -> list[_Ignore]:
    """Read `.gitignore`, and refuse what this reader cannot model rather than guess at it.

    A hand-written reader of `.gitignore` is a liability if it quietly models half the grammar, so
    this one implements the forms this repository uses — `name`, `name/`, `/name`, `dir/name`,
    `*.ext`, `[abc]` character classes and `!` re-includes — and raises on `**`, which changes the
    meaning of every other wildcard. That refusal is a feature: if someone reaches for `**`, this
    canary stops and says so instead of answering a question it cannot answer.
    """
    rules: list[_Ignore] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        negated = line.startswith("!")
        body = line[1:] if negated else line
        directory = body.endswith("/")
        anchored = body.startswith("/") or "/" in body.rstrip("/")
        body = body.strip("/")
        assert "**" not in body, f".gitignore:{number}: {line!r}: this canary does not model `**`"
        assert body, f".gitignore:{number}: {line!r}: names nothing"
        rules.append(_Ignore(number, line.strip(), negated, directory, anchored, body))
    return rules


def _matches(rule: _Ignore, relpath: str) -> bool:
    """Git's two matching modes: a path anchored at the root, or a name at any level."""
    parts = PurePosixPath(relpath).parts
    if rule.anchored:
        if fnmatch(relpath, rule.pattern):
            return True
        if rule.directory:
            return any(fnmatch("/".join(parts[: i + 1]), rule.pattern) for i in range(len(parts) - 1))
        return False
    if rule.directory:
        return any(fnmatch(part, rule.pattern) for part in parts[:-1])
    return any(fnmatch(part, rule.pattern) for part in parts)


def _ignoring_rule(rules: list[_Ignore], relpath: str) -> _Ignore | None:
    """The rule that decides the file's fate: the last one that matches, `!` included."""
    verdict: _Ignore | None = None
    for rule in rules:
        if _matches(rule, relpath):
            verdict = None if rule.negated else rule
    return verdict


def _documented_paths() -> dict[str, set[str]]:
    """Every existing file named in the documentation, and the documents that name it."""
    paths: dict[str, set[str]] = {}
    documents = [REPO / name for name in DOCUMENTATION] + sorted((REPO / "docs").rglob("*.md"))
    for document in documents:
        where = document.relative_to(REPO).as_posix()
        for match in NAMED_FILE.finditer(document.read_text(encoding="utf-8")):
            named = match.group(1)
            for candidate in (REPO / named, document.parent / named):
                if candidate.is_file() and not _is_build_output(candidate):
                    paths.setdefault(candidate.relative_to(REPO).as_posix(), set()).add(where)
                    break
    return paths


def test_no_document_names_a_file_a_clone_would_not_carry():
    """A path in a document is a promise the reader can open it — and `.gitignore` can break it.

    Not a hypothetical, and not a small one. The FMEA in `docs/resilience.md` named
    `docs/measurements/2026-10-15-2core/load-report.json` as what catches "the provider goes slow
    (tail latency)", and `.gitignore` carried a bare `load-report.json` line — which ignores that
    name *anywhere* in the tree. The report was in the authoring checkout and in no clone: the suite
    was green where it was written and red the first time the release check ran against a clone of
    the bundle, which is the whole reason that check runs against a clone.

    So: a file the documentation names, in a directory a reader can browse, must be a file the
    repository ships. Build outputs are not promises (`dist/` and `site/` are outputs by
    definition, and `_is_build_output` says which), and nothing else is exempt.
    """
    rules = _ignore_rules((REPO / ".gitignore").read_text(encoding="utf-8"))
    report: list[str] = []
    for relpath, documents in sorted(_documented_paths().items()):
        rule = _ignoring_rule(rules, relpath)
        if rule is not None:
            report.append(
                f"`{relpath}` is named in {', '.join(sorted(documents))} and a clone would not carry "
                f"it: .gitignore:{rule.number} ({rule.line!r}) ignores it. Anchor the rule to the "
                f"repository root (`/{rule.pattern}`) if only the copy written there is an output."
            )
    assert not report, "documented files that a clone would not have:\n  " + "\n  ".join(report)


def test_the_documented_file_canary_notices_the_rule_that_hid_a_measurement():
    """The canary above is only worth its runtime if it fails on the defect that produced it.

    The check is fed the rule that shipped — a bare `load-report.json` — and the rule that replaced
    it, anchored with a leading `/`, and has to say `ignored` to the first and `shipped` to the
    second. A checker that cannot fail is decoration.
    """
    target = "docs/measurements/2026-10-15-2core/load-report.json"
    historical = _ignore_rules("#!/\nload-report.json\n/soak-report.json\n")
    assert _ignoring_rule(historical, target) is not None, (
        "the canary no longer notices a basename rule that hides a curated measurement report; "
        "the regression test for .gitignore is worthless until it does"
    )
    current = _ignore_rules((REPO / ".gitignore").read_text(encoding="utf-8"))
    assert _ignoring_rule(current, target) is None, "the shipped .gitignore now hides a report the documentation names"
    assert _ignoring_rule(current, "load-report.json") is not None, (
        "the anchored rule must still ignore the copy `make load` writes to the repository root"
    )


#: The vocabulary the standards map's strength column is allowed to use. It is deliberately small:
#: the point of the column is that a reader can tell a normative text that was read from a canonical
#: practice that was merely named, and a sixth way of saying "pretty reliable" would defeat it.
STANDARDS_STRENGTHS = ("primary,", "practitioner canon", "A", "A/B", "B")


def test_the_standards_map_states_a_source_and_a_strength_for_every_mechanism():
    """A mechanism named without a source is a claim about someone else's idea.

    `docs/standards.md` maps each mechanism Docket implements to the norm or paper it comes from and
    says how that source was read. Two properties are worth a test: the map does not quietly shrink
    (the companion maps quote its size), and every row commits to a strength from the small
    vocabulary the page defines — because "cited" and "read" are different claims, and the whole page
    exists to keep them apart.
    """
    root = Path(__file__).resolve().parents[1]
    document = (root / "docs" / "standards.md").read_text(encoding="utf-8")
    heading = "| mechanism | source | strength | what it establishes, and where Docket stands |"
    assert heading in document, "the standards table's header changed shape"
    body = document.split(heading, 1)[1].split("\n\n", 1)[0]

    rows = [line for line in body.splitlines() if line.startswith("| ") and not line.startswith("| ---")]
    assert len(rows) >= 20, f"the standards map lists {len(rows)} mechanisms; it catalogued twenty"

    for line in rows:
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        assert len(cells) == 4, f"a standards row has {len(cells)} cells: {line[:80]}"
        mechanism, source, strength, _ = cells
        lookable = (
            re.search(r"\b(?:19|20)\d\d\b", source)  # a dated publication, edition or talk
            or re.search(r"\b[\w-]+\.(?:org|com|io|dev|google)\b", source)  # a domain to visit
            or re.search(r"\b(?:RFC \d+|DOI|CACM|SIGMOD|OSDI|arXiv|#\d+)\b", source)  # an identifier
        )
        assert lookable, (
            f"{mechanism}: the source cell is not something a reader can look up — no year, no domain, "
            f"no document identifier: {source[:120]}"
        )
        assert strength.startswith(STANDARDS_STRENGTHS), (
            f"{mechanism}: strength {strength!r} is outside the vocabulary {STANDARDS_STRENGTHS} the page defines"
        )

    companion = (root / "docs" / "sre-coverage.md").read_text(encoding="utf-8")
    assert f"{len(rows)} mechanisms" in companion, (
        f"docs/sre-coverage.md no longer quotes the standards map's size ({len(rows)} mechanisms)"
    )


def test_the_scan_skips_exactly_the_directories_the_site_stager_skips():
    """`GENERATED_DIRECTORIES` above claims to be the stager's list; a claim with no guard is a comment.

    Loaded from its path and registered in `sys.modules` first: importing a file that defines no
    dataclass would work without it, but the loader that skips the registration is the same one that
    breaks the next file, so it is registered here too.
    """
    import importlib.util
    import sys as _sys

    root = Path(__file__).resolve().parents[1]
    name = "build_docs_site_under_test"
    spec = importlib.util.spec_from_file_location(name, root / "tools" / "build_docs_site.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    _sys.modules[name] = module
    spec.loader.exec_module(module)
    assert set(GENERATED_DIRECTORIES) == set(module.SKIP_DIRS) - {".git"}, (
        f"this scan skips {sorted(GENERATED_DIRECTORIES)}; the stager skips {sorted(module.SKIP_DIRS)}"
    )


def test_no_document_tells_a_reader_to_install_an_unpublished_package() -> None:
    """No document may apologise for an install path, and the README's first one must resolve.

    This guard used to assert the opposite: that no document instructs a reader to run
    `pip install docket`, because `https://pypi.org/pypi/docket/json` answered 404 and the
    first command in a guide is the one a reader runs before they have any reason to trust
    the project. It was the right guard while the package was unpublished, and it caught four
    documents each carrying their own apology for the same gap.

    The package is published now, so the guard inverts rather than disappears. Two things are
    asserted, and the second is the one that matters:

    1. No document still carries the apology. A stale "not on PyPI" line is worse than the gap
       it describes, because a reader who can install the package is being told they cannot.
    2. The README's first install instruction names the distribution that is actually on PyPI.
       That is `docket-ai`, not `docket`: PyPI administratively prohibits the bare name, so
       `pip install docket` answers "no matching distribution" no matter what the README says.
       The distribution name is checked against `pyproject.toml` rather than written twice, so
       a rename cannot leave the guard and the guide disagreeing.

    Only an *instruction* is checked, not a mention: prose that says funding pays for the
    machinery which makes installation trustworthy is a statement of intent, and a reader
    cannot run a table cell. The marker may sit on the line above, which is where a YAML
    comment lives, so both are considered.
    """
    root = Path(__file__).resolve().parents[1]
    distribution = _distribution_name(root)
    instructions = ("- run:", f"pip install {distribution}", f"pipx run {distribution}", f"pipx install {distribution}")
    apologies: list[str] = []
    for page in sorted(root.rglob("*.md")):
        relative = page.relative_to(root).as_posix()
        if ".git" in page.parts or _is_build_output(page):
            continue
        lines = page.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            stripped = line.strip()
            if not stripped.startswith(instructions):
                continue
            if "docket" not in stripped:
                continue
            lowered = stripped.lower()
            if "not on pypi" in lowered or "does not resolve" in lowered or "is not on pypi" in lowered:
                apologies.append(f"{relative}:{index + 1}: {stripped}")
    assert not apologies, "a document still apologises for an install path that works: " + "; ".join(apologies)

    readme = (root / "README.md").read_text(encoding="utf-8").splitlines()
    first_install = next(
        (
            line
            for line in readme
            if line.strip().startswith("python3 -m pip install") or line.strip() == "pipx install docket"
        ),
        None,
    )
    assert first_install is not None, "the README no longer contains a pip install instruction"
    assert f"pip install {distribution}" in first_install, (
        f"the README's first install instruction is not the published distribution: {first_install!r}"
    )
