"""`tools/a11y_check.py` has to be able to *fail*, and it has to not cry wolf.

The site is the only HTML this project publishes; `mkdocs --strict` proves it builds, and nothing
proved a reader using a screen reader could use it. The checker that now does is a tool, so it is
under the same standard as everything else here: two kinds of test below.

* **Every rule fires on a page that breaks it** — one fixture per rule, each asserting the rule name
  and the line number, so a rule that stops matching (a typo in a tag name, a regex that no longer
  matches a minified stylesheet) fails a test instead of going quiet.
* **The naming rules do not fire on pages that are fine** — because a checker that reports a named
  link as unnamed is worse than no checker. The accessible-name cases are enumerated: `aria-label`,
  `aria-labelledby` through the page's ids, element text, a nested `<img alt>`, a nested
  `<svg><title>`, a `<label for=...>` elsewhere on the page, and a `<label>` that wraps the control.

The exemption has its own test, and it is the interesting one: a control whose class the site's CSS
declares `display: none` is not rendered, so it is not announced and no name is due — but that is a
claim about the stylesheet, so the checker reads the stylesheet. Drop the rule from the fixture CSS
and the same page must report the control, or the exemption is a special case that cannot expire.
`::-ms-clear` is in the fixtures for the same reason: a pseudo-element hidden by a rule is not its
owner being hidden, and reading it that way would exempt the search box a reader types into.

Two of the tests read the repository rather than the tool: the rule list has to match the table in the
tool's docstring, and `CODE_OF_CONDUCT.md` has to keep exactly one `h1` (the defect the checker found
on its first run). The check that the tool is wired into the release record is *not* here: that record
(`mutation/run_final_checks.sh`) lives in the authoring workspace and not in this repository, so a test
for it could only pass on the machine that has the workspace and would fail in every clone — which is
what it did, in the clone, the first time this file ran there. The wiring is asserted where both files
exist, in `mutation/rebase_workspace_numbers.py`, and the record itself prints the check's verdict.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "a11y_check.py"

#: A page that breaks none of the rules, with one named control and one labelled link.
GOOD = """<!DOCTYPE html>
<html lang="en">
<head><title>Docket docs</title></head>
<body>
<main>
<h1>Title</h1>
<h2>Section</h2>
<p>Read the <a href="quickstart/">quickstart</a>, or press <label for="q">Find</label>
<input type="text" id="q" class="search-box">.</p>
<img src="logo.svg" alt="the docket logo">
<table><tr><th>check</th></tr><tr><td>pytest</td></tr></table>
</main>
</body>
</html>
"""


def _load():
    """Load the checker as a module, registering it before execution (dataclasses look themselves up)."""
    name = "a11y_check_under_test"
    spec = importlib.util.spec_from_file_location(name, TOOL)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def checker():
    return _load()


def site(tmp_path: Path, pages: dict[str, str], css: str | None = None) -> Path:
    """A miniature built site: HTML pages and, optionally, one stylesheet in assets/."""
    root = tmp_path / "site"
    for name, html in pages.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(html, encoding="utf-8")
    if css is not None:
        assets = root / "assets" / "stylesheets"
        assets.mkdir(parents=True, exist_ok=True)
        (assets / "main.css").write_text(css, encoding="utf-8")
    return root


def violations_of(checker, root: Path) -> list[str]:
    found, pages, _ = checker.check_site(root)
    assert pages == len(list(root.rglob("*.html")))
    return [violation.rule for violation in found]


def test_a_compliant_page_reports_nothing(checker, tmp_path: Path) -> None:
    root = site(tmp_path, {"index.html": GOOD}, css=".search-box { color: red }\n")
    assert violations_of(checker, root) == []


def test_the_search_box_rule_did_not_go_blind(checker, tmp_path: Path) -> None:
    """A page with no `<main>`, no `h1`, no `title` and a stray control is not "clean"."""
    bare = "<html lang='en'><body><h3>Section</h3><img src='x.png'><a href='/x'></a></body></html>"
    root = site(tmp_path, {"index.html": bare})
    rules = set(violations_of(checker, root))
    assert {"main-landmark", "h1", "title", "img-alt", "link-name"} <= rules


@pytest.mark.parametrize(
    ("page", "rule"),
    [
        ("<!DOCTYPE html><html><head><title>t</title></head><body><main><h1>h</h1></main></body></html>", "lang"),
        ("<!DOCTYPE html><html lang='en'><head></head><body><main><h1>h</h1></main></body></html>", "title"),
        ("<!DOCTYPE html><html lang='en'><head><title>t</title></head><body><main></main></body></html>", "h1"),
        (
            "<!DOCTYPE html><html lang='en'><head><title>t</title></head><body><main>"
            "<h1>a</h1><h2>b</h2><h1>c</h1></main></body></html>",
            "h1",
        ),
        (
            "<!DOCTYPE html><html lang='en'><head><title>t</title></head><body><main>"
            "<h1>a</h1><h3>skipped</h3></main></body></html>",
            "heading-order",
        ),
        (
            "<!DOCTYPE html><html lang='en'><head><title>t</title></head><body>"
            "<h1>a</h1><p>no main</p></body></html>",
            "main-landmark",
        ),
        (
            "<!DOCTYPE html><html lang='en'><head><title>t</title></head><body><main><h1>a</h1>"
            "<img src='x.png'></main></body></html>",
            "img-alt",
        ),
        (
            "<!DOCTYPE html><html lang='en'><head><title>t</title></head><body><main><h1>a</h1>"
            "<a href='/elsewhere'><img src='i.png' alt=''></a></main></body></html>",
            "link-name",
        ),
        (
            "<!DOCTYPE html><html lang='en'><head><title>t</title></head><body><main><h1>a</h1>"
            "<input type='text' name='q'></main></body></html>",
            "control-name",
        ),
        (
            "<!DOCTYPE html><html lang='en'><head><title>t</title></head><body><main><h1>a</h1>"
            "<table><tr><td>no header cell</td></tr></table></main></body></html>",
            "table-headers",
        ),
        (
            "<!DOCTYPE html><html lang='en'><head><title>t</title></head><body><main><h1>a</h1>"
            "<h2 id='dup'>b</h2><h2 id='dup'>c</h2></main></body></html>",
            "duplicate-id",
        ),
    ],
)
def test_each_rule_fires_and_names_itself(checker, tmp_path: Path, page: str, rule: str) -> None:
    root = site(tmp_path, {"index.html": page})
    found, _, _ = checker.check_site(root)
    assert [violation.rule for violation in found] == [rule], found
    assert found[0].page == "index.html"
    assert found[0].line > 0


@pytest.mark.parametrize(
    ("anchor", "rule"),
    [
        ("<a href='/x' aria-label='Quickstart'><svg><path d='M0 0'/></svg></a>", None),
        ("<a href='/x' title='Quickstart'><span></span></a>", None),
        ("<a href='/x'><img src='i.png' alt='Quickstart'></a>", None),
        ("<a href='/x'><svg><title>Quickstart</title></svg></a>", None),
        ("<a href='/x'>Quickstart</a>", None),
        ("<a href='/x'><script>var s = '<b>not text</b>';</script></a>", "link-name"),
        ("<a href='/x'><img src='i.png' alt=''></a>", "link-name"),
        ("<a href='/x'><span>   </span></a>", "link-name"),
    ],
)
def test_a_link_is_named_by_everything_that_can_name_it(checker, tmp_path: Path, anchor: str, rule: str | None) -> None:
    body = anchor.replace("'", '"')
    page = (
        f'<!DOCTYPE html><html lang="en"><head><title>t</title></head><body><main><h1>a</h1>{body}</main></body></html>'
    )
    root = site(tmp_path, {"index.html": page})
    rules = violations_of(checker, root)
    assert ("link-name" in rules) is (rule == "link-name"), rules


def test_the_tooltip_attribute_is_the_last_resort_the_specification_says_it_is(checker, tmp_path: Path) -> None:
    """`title` names an element only when nothing else does (AccName 1.2 §4.3.2 step 2.09).

    Both pages below pass — a `title` *is* a name — so the assertion is not the verdict but the report:
    the link with text is named by its text, and only the link with nothing else is named by its
    tooltip. Reading `title` first, as this tool did until a test said otherwise, would have called 536
    links on the real site by their tooltip when a reader hears their text.
    """
    root = site(
        tmp_path,
        {
            "both.html": GOOD.replace(
                '<a href="quickstart/">quickstart</a>', '<a href="/x" title="Tooltip">Quickstart</a>'
            ),
            "tooltip-only.html": GOOD.replace(
                '<a href="quickstart/">quickstart</a>', '<a href="/x" title="Tooltip"></a>'
            ),
        },
    )
    found, _, report = checker.check_site(root)
    assert found == []
    assert report["names_by_source"]["text"] >= 1
    assert report["names_by_source"].get("title") == 1


def test_a_child_that_has_its_own_name_does_not_also_contribute_its_text(checker, tmp_path: Path) -> None:
    """`<a><span aria-label="x">y</span></a>` is named `x`, and the report says the child named it.

    This is the recursion in step 2.06 applied one level down: a child with a name of its own
    contributes that instead of its text. The tally is how a test can see it — the link is named either
    way, and only the report distinguishes a reader hearing `x` from hearing `x y`.
    """
    root = site(
        tmp_path,
        {
            "nested.html": GOOD.replace(
                '<a href="quickstart/">quickstart</a>', '<a href="/x"><span aria-label="x">y</span></a>'
            )
        },
    )
    found, _, report = checker.check_site(root)
    assert found == []
    assert report["names_by_source"].get("descendant:aria-label") == 1


def test_a_link_whose_only_text_is_inside_a_script_has_no_name(checker, tmp_path: Path) -> None:
    root = site(
        tmp_path,
        {
            "script.html": GOOD.replace(
                '<a href="quickstart/">quickstart</a>', "<a href='/x'><script>var s = 1;</script></a>"
            )
        },
    )
    found, _, _ = checker.check_site(root)
    assert [violation.rule for violation in found] == ["link-name"]


def test_aria_labelledby_resolves_through_the_pages_ids(checker, tmp_path: Path) -> None:
    page = (
        '<!DOCTYPE html><html lang="en"><head><title>t</title></head><body><main><h1>a</h1>'
        '<span id="label-text">Read the quickstart</span>'
        '<a href="/x" aria-labelledby="label-text"><span></span></a>'
        '<a href="/y" aria-labelledby="not-on-this-page"><span></span></a>'
        "</main></body></html>"
    )
    root = site(tmp_path, {"index.html": page})
    found, _, _ = checker.check_site(root)
    assert len(found) == 1
    assert "'/y'" in found[0].detail


@pytest.mark.parametrize(
    "control",
    [
        "<label for='q'>Find</label><input type='text' id='q'>",
        "<label>Find <input type='text'></label>",
        "<input type='text' aria-label='Find'>",
        "<input type='text' title='Find'>",
        "<select aria-label='Level'><option>1</option></select>",
        "<textarea aria-label='Notes'></textarea>",
        "<input type='hidden' name='csrf'>",
        "<input type='submit' value='Go'>",
    ],
)
def test_a_control_is_named_by_its_label_or_its_own_attributes(checker, tmp_path: Path, control: str) -> None:
    body = control.replace("'", '"')
    page = (
        f'<!DOCTYPE html><html lang="en"><head><title>t</title></head><body><main><h1>a</h1>{body}</main></body></html>'
    )
    root = site(tmp_path, {"index.html": page})
    assert "control-name" not in violations_of(checker, root)


def test_a_control_with_no_name_anywhere_is_reported(checker, tmp_path: Path) -> None:
    page = (
        '<!DOCTYPE html><html lang="en"><head><title>t</title></head><body><main><h1>a</h1>'
        '<label for="other">Find</label><input type="text" id="q">'
        "</main></body></html>"
    )
    root = site(tmp_path, {"index.html": page})
    assert "control-name" in violations_of(checker, root)


def test_the_unrendered_control_exemption_lasts_only_as_long_as_its_evidence(checker, tmp_path: Path) -> None:
    """The same page, twice: with the theme's rule and without it."""
    page = (
        '<!DOCTYPE html><html lang="en"><head><title>t</title></head><body><main><h1>a</h1>'
        '<input class="md-toggle" type="checkbox" id="__drawer">'
        "</main></body></html>"
    )
    with_rule = site(tmp_path / "with", {"index.html": page}, css=".md-toggle{display:none}")
    without = site(tmp_path / "without", {"index.html": page}, css=".md-toggle{opacity:0}")

    clean, _, report = checker.check_site(with_rule)
    assert clean == [], clean
    assert report["classes_that_exempted_a_control"] == ["md-toggle"]
    assert any(".md-toggle" in line for line in report["css_evidence"])

    found = violations_of(checker, without)
    assert found == ["control-name"], found
    assert checker.check_site(without)[2]["classes_that_exempted_a_control"] == []


def test_a_pseudo_element_rule_does_not_hide_its_owner(checker, tmp_path: Path) -> None:
    """`.search-box::-ms-clear{display:none}` hides a pseudo-element, not the search box."""
    page = (
        '<!DOCTYPE html><html lang="en"><head><title>t</title></head><body><main><h1>a</h1>'
        '<input class="search-box" type="text">'
        "</main></body></html>"
    )
    root = site(tmp_path, {"index.html": page}, css=".search-box::-ms-clear{display:none}")
    found, _, report = checker.check_site(root)
    assert [violation.rule for violation in found] == ["control-name"]
    assert "search-box" not in report["classes_declared_display_none"]


@pytest.mark.parametrize(
    "page",
    [
        # tags and attributes in capitals, unquoted values
        "<HTML LANG=en><HEAD><TITLE>t</TITLE></HEAD><BODY><MAIN><H1>a</H1></MAIN></BODY></HTML>",
        # a comment that contains markup, and a script whose string contains a link
        "<!DOCTYPE html><html lang='en'><head><title>t</title></head><body><main><h1>a</h1>"
        "<!-- <img src='x'> --><script>var a = \"<a href='/x'></a>\";</script></main></body></html>",
        # self-closing tags, an entity in text, and an unclosed paragraph
        "<!DOCTYPE html><html lang='en'><head><title>t</title></head><body><main><h1>a</h1>"
        "<p>Bread &amp; butter<br/><img src='i.png' alt='' /></main></body></html>",
    ],
)
def test_the_parser_does_not_invent_defects(checker, tmp_path: Path, page: str) -> None:
    root = site(tmp_path, {"index.html": page})
    found, _, _ = checker.check_site(root)
    assert [violation.as_line() for violation in found] == [], found


def test_mismatched_end_tags_do_not_corrupt_the_tree(checker, tmp_path: Path) -> None:
    page = (
        '<!DOCTYPE html><html lang="en"><head><title>t</title></head><body><main><h1>a</h1>'
        '<div><p>text</div></p><a href="/x"></a></main></body></html>'
    )
    root = site(tmp_path, {"index.html": page})
    assert violations_of(checker, root) == ["link-name"]


def test_a_page_inside_a_subdirectory_reports_its_relative_path(checker, tmp_path: Path) -> None:
    root = site(
        tmp_path,
        {
            "index.html": GOOD,
            "docs/faq/index.html": (
                "<!DOCTYPE html><html lang='en'><head><title>t</title></head><body>"
                "<main><h1>a</h1><h3>deep</h3></main></body></html>"
            ),
        },
    )
    found, pages, _ = checker.check_site(root)
    assert pages == 2
    assert [violation.as_line() for violation in found] == [
        "docs/faq/index.html:1: heading-order: <h3> follows <h1> without a heading between them"
    ]


def test_exit_codes_are_0_1_and_2(checker, tmp_path: Path) -> None:
    clean = site(tmp_path / "clean", {"index.html": GOOD})
    broken = site(tmp_path / "broken", {"index.html": "<html lang='en'><body><h1>a</h1></body></html>"})

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(TOOL), *args],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )

    assert run("--site", str(clean)).returncode == 0
    failing = run("--site", str(broken))
    assert failing.returncode == 1
    assert "violation(s) across 1 page(s)" in failing.stdout
    assert run("--site", str(tmp_path / "nope")).returncode == 2
    empty = tmp_path / "empty"
    empty.mkdir()
    assert run("--site", str(empty)).returncode == 2


def test_the_json_report_says_what_the_console_says(checker, tmp_path: Path) -> None:
    root = site(tmp_path, {"index.html": "<html lang='en'><body><h1>a</h1></body></html>"})
    report = tmp_path / "report.json"
    subprocess.run(
        [sys.executable, str(TOOL), "--site", str(root), "--json", str(report)],
        capture_output=True,
        cwd=ROOT,
        check=False,
    )
    written = json.loads(report.read_text(encoding="utf-8"))
    assert written["pages"] == 1
    # The page has no <title> and no <main>: both are reported, neither hides the other.
    assert [violation["rule"] for violation in written["violations"]] == ["title", "main-landmark"]
    assert written["exemptions"]["classes_that_exempted_a_control"] == []


def test_the_rule_list_matches_the_table_in_the_docstring(checker) -> None:
    """A rule in the table and not in the code (or the reverse) is a document that lies."""
    docstring = checker.__doc__ or ""
    documented = set(re.findall(r"^\| `([a-z0-9-]+)` \|", docstring, re.MULTILINE))
    assert documented == set(checker.RULES), (documented, checker.RULES)


def test_the_code_of_conduct_keeps_one_h1() -> None:
    """The defect the checker found on its first run, kept fixed by a test inside the repository."""
    text = (ROOT / "CODE_OF_CONDUCT.md").read_text(encoding="utf-8")
    assert sum(1 for line in text.splitlines() if line.startswith("# ")) == 1
    assert text.startswith("# Contributor Covenant Code of Conduct\n")
