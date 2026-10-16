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


#: Directories that hold a *copy* of the tree rather than the tree. `.mkdocs-site` is staged by
#: `tools/build_docs_site.py` and `site` is what mkdocs writes out of it: every number, every path and
#: every quoted sentence in them is a copy of a file this suite already reads, so scanning them adds
#: nothing and reports a defect in the copy as a defect in the documentation. It is not hypothetical —
#: running `make site` and then the suite made this file fail on its own `CHANGELOG.md` example of a
#: counted reference (`tests/test_x.py`), because the staged copy carries the example too.
BUILD_OUTPUT = {".mkdocs-site", "site"}


def _is_build_output(path: Path) -> bool:
    return any(part in BUILD_OUTPUT for part in path.relative_to(REPO).parts)


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
    """Four sentences promise the reader a number for the whole suite, and nothing read any of them.

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
            assert (
                base in DOCUMENTED_STATUSES
            ), f"{section} / {name}: status {status!r} is not one of the legend's: {DOCUMENTED_STATUSES}"

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
                f"{section} / {name}: status {status!r} is not one of the legend's: " f"{DOCUMENTED_STATUSES}"
            )
        total += len(rows)

    assert total == 22, f"the practice map has {total} rows; twenty-two practices were catalogued"
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert (
        f"{total} resilience practices" in readme
    ), "the README no longer points at the practice map by its counted size"


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
            assert (
                root / path
            ).exists(), f"{mode.strip()}: {path} is named as what catches this failure and is not in the tree"

    assert risks == sorted(
        risks, reverse=True
    ), f"the FMEA's rows are no longer ordered by risk, highest first: {risks}"


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
            f"{mechanism}: strength {strength!r} is outside the vocabulary " f"{STANDARDS_STRENGTHS} the page defines"
        )

    companion = (root / "docs" / "sre-coverage.md").read_text(encoding="utf-8")
    assert (
        f"{len(rows)} mechanisms" in companion
    ), f"docs/sre-coverage.md no longer quotes the standards map's size ({len(rows)} mechanisms)"
