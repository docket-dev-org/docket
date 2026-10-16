#!/usr/bin/env python3
"""Check the built documentation site for the accessibility defects that can be decided mechanically.

The site is the only HTML this project publishes. `mkdocs --strict` proves that it *builds*; it says
nothing about whether a reader using a screen reader can use it, and until 2026-10-16 nothing in the
repository looked. This tool looks, with no dependency beyond the standard library, so it can run in
the release record next to the build it inspects.

    python3 tools/a11y_check.py                      # check ./site
    python3 tools/a11y_check.py --site /tmp/site     # check somewhere else
    python3 tools/a11y_check.py --json report.json   # machine-readable, same exit code
    python3 tools/a11y_check.py --build              # build the site first, then check it

Exit codes: 0 clean, 1 at least one violation, 2 could not run (no site directory, no pages, unreadable
page). The rules, and what each one is for:

| rule | what it refuses | why it matters |
| --- | --- | --- |
| `lang` | a page whose `<html>` has no non-empty `lang` | a screen reader picks its pronunciation from it (WCAG 2.2 SC 3.1.1) |
| `title` | a page with no non-empty `<title>` | the window, the tab, and the bookmark are nameless (SC 2.4.2) |
| `h1` | a page with no `h1`, more than one, or one that is not the first heading | the outline a screen reader announces has a root or two (SC 1.3.1, 2.4.6) |
| `heading-order` | a heading level more than one deeper than the heading before it | the outline claims a nesting that the page does not have (SC 1.3.1) |
| `img-alt` | an `<img>` with no `alt` attribute at all | `alt=""` is a decision (decorative); a missing `alt` is an omission (SC 1.1.1) |
| `link-name` | a link with no accessible name | "link" announced with no destination is unusable (SC 2.4.4) |
| `control-name` | an input, select or textarea with no accessible name | a form control announced as "edit text" (SC 3.3.2) |
| `table-headers` | a `<table>` with no `<th>` and no `<caption>` | a data table read as an undifferentiated grid (SC 1.3.1) |
| `main-landmark` | a page that does not have exactly one `<main>` | landmark navigation is how a screen reader skips the chrome (SC 2.4.1) |
| `duplicate-id` | the same `id` twice in one page | `#anchor` links and `aria-labelledby` resolve to one of them, silently (anchors are how this project cross-references its own evidence) |

**The one exemption, and why it is not a special case.** material renders its drawer and search as
`<input class="md-toggle" type="checkbox">`, and the theme's own stylesheet says `.md-toggle{
display:none}`. A control that is not rendered is not in the accessibility tree, so no accessible name
is due — but that is a claim about the theme's CSS, so the tool reads the CSS rather than trusting the
class name: it exempts a control only while a rule that sets `display:none` names one of its classes,
prints how many it exempted, and stops exempting the moment the rule is gone. An exemption that
cannot expire is how a checker rots into a rubber stamp.

`main-landmark` measures the theme rather than the prose — material emits exactly one `<main>`, so a
violation there means the theme changed under the site, which is worth knowing on the release record
and is not a writer's mistake to fix in a document.

The accessible-name computation is the part that has to be right, because a checker that reports a
named link as unnamed is worse than no checker: it teaches its reader to ignore it. A name is taken
from, in order, `aria-label`, `aria-labelledby` (resolved through the page's `id`s), the text of the
element, the `alt` of a nested `<img>`, a nested `<svg><title>`, the `title` attribute, and — for form
controls — the text of a `<label for=...>` or a wrapping `<label>`. `<script>` and `<style>` text is
never part of a name.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import NoReturn

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SITE = ROOT / "site"

#: Elements that never have children, so a tree builder must not push them.
VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())
#: Elements whose text is code, not prose: never part of an accessible name.
OPAQUE = frozenset(("script", "style", "template"))
#: Form controls, and the input types whose accessible name a person never hears.
CONTROLS = frozenset(("input", "select", "textarea"))
SILENT_INPUT_TYPES = frozenset(("hidden", "submit", "button", "reset", "image"))
HEADINGS = frozenset(("h1", "h2", "h3", "h4", "h5", "h6"))


@dataclass
class Node:
    """One element: its tag, its attributes, its children, its own text, and where it started."""

    tag: str
    attrs: dict[str, str]
    line: int
    text: str = ""
    children: list[Node] = field(default_factory=list)

    def descendants(self) -> list[Node]:
        found: list[Node] = []
        for child in self.children:
            found.append(child)
            found.extend(child.descendants())
        return found

    def text_of(self) -> str:
        """This element's prose: its own text and its descendants', skipping script and style."""
        if self.tag in OPAQUE:
            return ""
        parts = [self.text] if self.text else []
        for child in self.children:
            parts.append(child.text_of())
        return "".join(parts)


class Tree(HTMLParser):
    """A small DOM: enough for names and structure, forgiving of what real themes emit."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("root", {}, 1)
        self._open = [self.root]
        self.title: str | None = None
        self.lang: str | None = None
        self._capturing_title = False
        self.parents: dict[int, Node] = {}

    def _attach(self, node: Node) -> None:
        parent = self._open[-1]
        parent.children.append(node)
        self.parents[id(node)] = parent

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, {key: (value or "") for key, value in attrs}, self.getpos()[0])
        self._attach(node)
        if tag == "html":
            self.lang = node.attrs.get("lang", "")
        if tag == "title":
            self._capturing_title = True
        if tag not in VOID:
            self._open.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._attach(Node(tag, {key: (value or "") for key, value in attrs}, self.getpos()[0]))

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._capturing_title = False
        if tag in VOID:
            return
        for index in range(len(self._open) - 1, 0, -1):
            if self._open[index].tag == tag:
                del self._open[index:]
                return
        # An end tag with no open element: ignore it rather than corrupting the tree above.

    def handle_data(self, data: str) -> None:
        if self._capturing_title and self.title is None:
            self.title = data.strip()
        self._open[-1].text += data


def cannot_run(message: str) -> NoReturn:
    """Exit 2 with a reason. Not `sys.exit("...")`: that exits 1, which is the code for findings."""
    print(message, file=sys.stderr)
    raise SystemExit(2)


def display_none_classes(site: Path) -> tuple[set[str], list[str]]:
    """Class names the subject of a `display: none` rule in the site's CSS, and where it says so.

    Read from the built site rather than from the theme's source: the question is what the artefact
    ships, and the artefact carries one minified stylesheet. Only the *subject* of a rule counts — the
    last compound selector — because `.md-search__input::-ms-clear { display:none }` hides a
    pseudo-element, not the input, and a checker that read that as "the search box is invisible" would
    exempt the one control on the page a reader actually types into.
    """
    classes: set[str] = set()
    evidence: list[str] = []
    for stylesheet in sorted(site.rglob("*.css")):
        text = stylesheet.read_text(encoding="utf-8", errors="replace")
        for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text):
            if not re.search(r"display\s*:\s*none", body):
                continue
            for part in selector.split(","):
                if "::" in part:
                    # A pseudo-element is not the element: `::-ms-clear`, `::marker` and
                    # `::-webkit-scrollbar` are hidden by these rules, not their owners.
                    continue
                subject = re.split(r"[ >+~]", part.strip())[-1]
                for name in re.findall(r"\.([A-Za-z][\w-]*)", subject):
                    classes.add(name)
                    evidence.append(
                        f"{stylesheet.relative_to(site).as_posix()}: {part.strip()[:100]} {{ display:none }}"
                    )
    return classes, evidence


@dataclass
class Violation:
    rule: str
    page: str
    line: int
    detail: str

    def as_line(self) -> str:
        return f"{self.page}:{self.line}: {self.rule}: {self.detail}"

    def as_dict(self) -> dict[str, object]:
        return {"rule": self.rule, "page": self.page, "line": self.line, "detail": self.detail}


def by_id(tree: Tree) -> dict[str, Node]:
    found: dict[str, Node] = {}
    for node in tree.root.descendants():
        identifier = node.attrs.get("id")
        if identifier and identifier not in found:
            found[identifier] = node
    return found


def labels_for(tree: Tree) -> dict[str, Node]:
    """`<label for=x>` by the id it names. The label itself has no id, so it is not in the id map."""
    found: dict[str, Node] = {}
    for element in tree.root.descendants():
        if element.tag == "label" and element.attrs.get("for"):
            found.setdefault(element.attrs["for"], element)
    return found


def wrapper_label(node: Node, parents: dict[int, Node]) -> Node | None:
    """The nearest `<label>` that wraps this control, if any: the other way a label names a control."""
    current = parents.get(id(node))
    while current is not None and current.tag != "root":
        if current.tag == "label":
            return current
        current = parents.get(id(current))
    return None


def accessible_name(
    node: Node,
    ids: dict[str, Node],
    labels: dict[str, Node],
    parents: dict[int, Node] | None = None,
) -> str:
    """The name a screen reader would announce for this element. Empty means it would announce none."""
    parts: list[str] = []

    def from_attributes(element: Node) -> None:
        for attr in ("aria-label", "title"):
            value = element.attrs.get(attr, "").strip()
            if value:
                parts.append(value)
        for reference in element.attrs.get("aria-labelledby", "").split():
            target = ids.get(reference)
            if target is not None:
                parts.append(target.text_of().strip())

    from_attributes(node)
    ancestors = node.descendants()
    for element in ancestors:
        from_attributes(element)
        if element.tag == "img":
            alt = element.attrs.get("alt", "").strip()
            if alt:
                parts.append(alt)
        if element.tag == "svg":
            for inner in element.descendants():
                if inner.tag == "title" and inner.text.strip():
                    parts.append(inner.text.strip())

    parts.append(node.text_of().strip())

    if node.tag in CONTROLS:
        # A control's own name can live in a label elsewhere on the page, which is how every theme
        # hides its toggles: material's `<input class="md-toggle" id="__drawer">` is named by
        # `<label class="md-nav__title" for="__drawer">`. Resolving `for=` is the difference between
        # 141 reports on this site and two.
        label = labels.get(node.attrs.get("id", ""))
        if label is not None:
            parts.append(label.text_of().strip())
        if parents:
            wrapping = wrapper_label(node, parents)
            if wrapping is not None:
                # The wrapper's text already includes the control's own text, which the step above
                # may have added; joining the whole wrapper would double it, so only the wrapper's
                # text outside this control is used.
                outside = "".join(child.text_of() for child in wrapping.children if child is not node).strip()
                parts.append(outside or wrapping.text_of().strip())
    return " ".join(part for part in parts if part).strip()


def check_page(page: Path, site: Path, hidden: set[str]) -> tuple[list[Violation], list[str]]:
    tree = Tree()
    tree.feed(page.read_text(encoding="utf-8"))
    tree.close()
    relative = page.relative_to(site).as_posix()
    ids = by_id(tree)
    labels = labels_for(tree)
    parents = tree.parents
    violations: list[Violation] = []

    def flag(rule: str, detail: str, line: int) -> None:
        violations.append(Violation(rule, relative, line, detail))

    if not tree.lang or not tree.lang.strip():
        flag("lang", "<html> carries no non-empty lang attribute", 1)
    if not tree.title:
        flag("title", "the page has no non-empty <title>", 1)

    elements = tree.root.descendants()
    mains = [node for node in elements if node.tag == "main" or node.attrs.get("role") == "main"]
    if len(mains) != 1:
        flag("main-landmark", f"the page has {len(mains)} main landmarks, not 1", mains[0].line if mains else 1)

    headings = [node for node in elements if node.tag in HEADINGS]
    tops = [node for node in headings if node.tag == "h1"]
    if len(tops) != 1:
        where = f"at line {tops[0].line}" if tops else "nowhere"
        flag("h1", f"the page has {len(tops)} h1 heading(s) ({where})", tops[0].line if tops else 1)
    elif headings[0] is not tops[0]:
        flag("h1", f"the first heading is <{headings[0].tag}> at line {headings[0].line}, not the h1", headings[0].line)
    for before, after in zip(headings, headings[1:], strict=False):
        if int(after.tag[1]) - int(before.tag[1]) > 1:
            flag("heading-order", f"<{after.tag}> follows <{before.tag}> without a heading between them", after.line)

    for node in elements:
        if node.tag == "img" and "alt" not in node.attrs:
            flag("img-alt", f"<img src={node.attrs.get('src', '?')!r}> has no alt attribute", node.line)
        if node.tag == "a" and node.attrs.get("href", "").strip() and not accessible_name(node, ids, labels, parents):
            flag("link-name", f"link to {node.attrs['href'][:70]!r} has no accessible name", node.line)
        if node.tag in CONTROLS and node.attrs.get("type", "text").lower() not in SILENT_INPUT_TYPES:
            # `md-toggle` is a class the site's stylesheet declares `display:none`: not rendered, so not
            # announced, so no name is due — while the rule exists. See the module docstring.
            if set(node.attrs.get("class", "").split()) & hidden:
                continue
            if not accessible_name(node, ids, labels, parents):
                flag("control-name", f"<{node.tag} {_describe(node)}> has no accessible name", node.line)
        if node.tag == "table" and not any(inner.tag in ("th", "caption") for inner in node.descendants()):
            flag("table-headers", "the table has no <th> and no <caption>", node.line)

    seen: dict[str, int] = {}
    for node in elements:
        identifier = node.attrs.get("id")
        if not identifier:
            continue
        if identifier in seen:
            flag("duplicate-id", f"id {identifier!r} is used again (first at line {seen[identifier]})", node.line)
        else:
            seen[identifier] = node.line
    exempting_classes: set[str] = set()
    for node in elements:
        if node.tag in CONTROLS:
            exempting_classes |= set(node.attrs.get("class", "").split()) & hidden
    return violations, sorted(exempting_classes)


def _describe(node: Node) -> str:
    keep = ("type", "name", "id", "class", "placeholder")
    return " ".join(f"{key}={node.attrs[key]!r}" for key in keep if node.attrs.get(key)) or "control"


def check_site(site: Path) -> tuple[list[Violation], int, dict[str, object]]:
    if not site.is_dir():
        cannot_run(f"{site} is not a directory — build the site first (tools/build_docs_site.py), or pass --site")
    pages = sorted(site.rglob("*.html"))
    if not pages:
        cannot_run(f"{site} holds no .html — is that the built site, or the directory it builds from?")
    hidden, evidence = display_none_classes(site)
    violations: list[Violation] = []
    exempted_classes: set[str] = set()
    for page in pages:
        page_violations, page_classes = check_page(page, site, hidden)
        violations.extend(page_violations)
        exempted_classes |= set(page_classes)
    return (
        violations,
        len(pages),
        {
            "classes_declared_display_none": sorted(hidden),
            "classes_that_exempted_a_control": sorted(exempted_classes),
            "css_evidence": evidence,
        },
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the built site for mechanical accessibility defects.")
    parser.add_argument("--site", type=Path, default=DEFAULT_SITE, help="the built site (default: ./site)")
    parser.add_argument("--build", action="store_true", help="build the site first (needs mkdocs on PATH)")
    parser.add_argument("--json", type=Path, default=None, help="write the report here as JSON")
    args = parser.parse_args(argv)

    if args.build:
        built = subprocess.run([sys.executable, str(ROOT / "tools" / "build_docs_site.py")], cwd=ROOT)
        if built.returncode != 0:
            sys.exit(2)
    violations, pages, exemptions = check_site(args.site)

    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    "site": str(args.site),
                    "pages": pages,
                    "violations": [violation.as_dict() for violation in violations],
                    "rules": sorted({violation.rule for violation in violations}),
                    "exemptions": exemptions,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    for violation in violations:
        print(violation.as_line())
    exempted = exemptions["classes_that_exempted_a_control"]
    note = (
        f"{len(exempted)} class(es) exempted controls as unrendered ({', '.join(exempted)})"
        if exempted
        else "nothing was exempted"
    )
    if violations:
        rules = sorted({violation.rule for violation in violations})
        print(f"{len(violations)} violation(s) across {pages} page(s): {', '.join(rules)}; {note}")
        print("every rule this tool applies, and why it is a defect, is in its module docstring")
        return 1
    print(f"{pages} page(s) checked: no violation of the {len(RULES)} rules; {note}")
    return 0


#: The rule names this tool can report, for the summary line. Kept beside the checks that emit them.
RULES = (
    "lang",
    "title",
    "h1",
    "heading-order",
    "main-landmark",
    "img-alt",
    "link-name",
    "control-name",
    "table-headers",
    "duplicate-id",
)


if __name__ == "__main__":
    raise SystemExit(main())
