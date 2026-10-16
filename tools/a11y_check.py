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
named link as unnamed is worse than no checker: it teaches its reader to ignore it. The order below is
the precedence of *Accessible Name and Description Computation 1.2* §4.3.2 step 2, and each step falls
through when it yields nothing:

1. `aria-labelledby`, resolved through the page's `id`s (2.02);
2. `aria-label` (2.04);
3. the host language's own naming (2.05): for a form control, the text of a `<label for=...>` or of a
   `<label>` that wraps it;
4. name from content (2.06–2.08): the element's text and its children's, where a child that has a name
   of its own contributes that instead of its text, and a child's `alt` or `<svg><title>` counts;
5. the `title` attribute, which the specification marks as used *only if nothing else, including
   subtree content, has provided results* (2.09).

`<script>`, `<style>` and `<template>` contribute nothing. Two consequences of the order are worth
stating because the site exercises both: a link with visible text *and* a tooltip is named by its text,
and a link whose only content is a script has no name at all. On the 47-page build of 2026-10-16, 3,493
elements were named by their text, 235 by an `aria-label` the theme wrote (141 of them the drawer's and
search's controls), and 5 by the logo's `alt` — that 240 is the number a text-only rule would have
reported as nameless, and the reason the report prints the split beside the verdict instead of letting
a reader assume it.

The whole algorithm is not implemented — no CSS generated content, no shadow roots, no `aria-hidden`
exclusion inside a name, no full flat-string normalisation, and no role-based prohibition of naming,
which is safe for the two rules that ask: a link and a form control both allow naming. `axe-core`
implements the rest; this tool's job is to decide ten questions, explain each answer, and refuse to run
rather than guess.
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
        if tag not in VOID:
            self._open.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._attach(Node(tag, {key: (value or "") for key, value in attrs}, self.getpos()[0]))

    def close(self) -> None:
        """Finish parsing, then read the document title from the tree that was built.

        A `_capturing_title` flag was the first implementation, and the mutation sweep of 2026-10-16
        showed it could not be killed: both of its branches are unreachable, because `handle_data`
        keeps only the *first* captured text and a page's `<title>` always precedes any other text. An
        unkillable branch is a branch to delete, not to explain — so the title is a property of the
        finished tree now, and a page whose text precedes its `<title>` (impossible in HTML, cheap to
        survive) needs no flag to get it right.
        """
        super().close()
        for node in self.root.descendants():
            if node.tag == "title" and node.text_of().strip():
                self.title = node.text_of().strip()
                return

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID:
            return
        for index in range(len(self._open) - 1, 0, -1):
            if self._open[index].tag == tag:
                del self._open[index:]
                return
        # An end tag with no open element: ignore it rather than corrupting the tree above.

    def handle_data(self, data: str) -> None:
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


def _own_attributes(element: Node, prefix: str = "") -> list[tuple[str, str]]:
    """The element's own `aria-label` and `title` (steps 2.04 and 2.09), in precedence order."""
    found: list[tuple[str, str]] = []
    for attribute in ("aria-label", "title"):
        value = element.attrs.get(attribute, "").strip()
        if value:
            found.append((prefix + attribute, value))
    return found


def _labelled_by(element: Node, ids: dict[str, Node], prefix: str = "") -> list[tuple[str, str]]:
    """The text of every element this one references by `aria-labelledby` (step 2.02), concatenated."""
    values = []
    for reference in element.attrs.get("aria-labelledby", "").split():
        target = ids.get(reference)
        if target is not None and target.text_of().strip():
            values.append(target.text_of().strip())
    return [(prefix + "aria-labelledby", " ".join(values))] if values else []


def _native_text_alternative(element: Node) -> list[tuple[str, str]]:
    """What the host language itself names this element: `alt` on an image, `<title>` in an SVG."""
    if element.tag == "img" and element.attrs.get("alt", "").strip():
        return [("alt", element.attrs["alt"].strip())]
    if element.tag == "svg":
        for inner in element.descendants():
            if inner.tag == "title" and inner.text.strip():
                return [("svg-title", inner.text.strip())]
    return []


def _name_from_content(element: Node, ids: dict[str, Node], prefix: str = "") -> list[tuple[str, str]]:
    """One element's contribution to a name from content (steps 2.06–2.08).

    Each child contributes *its own* name, in document order, and a child that has a name of its own
    contributes that instead of its text — the difference between a `<span aria-label="x">y</span>`
    inside a link naming it `x` and naming it `x y`. A `<script>`, `<style>` or `<template>` subtree
    contributes nothing: it is not rendered, so a link whose only content is a script tag has no name,
    and reporting that is the point of walking the tree rather than reading `text_of()`.
    """
    if element.tag in OPAQUE:
        return []
    labelled = _labelled_by(element, ids, prefix)
    if labelled:
        return labelled
    attributes = _own_attributes(element, prefix)
    if attributes:
        return attributes
    native = _native_text_alternative(element)
    if native:
        return [(prefix + source, value) for source, value in native]
    fragments: list[tuple[str, str]] = []
    if element.text.strip():
        fragments.append((prefix + "text", element.text.strip()))
    for child in element.children:
        fragments.extend(_name_from_content(child, ids, prefix))
    return fragments


def name_parts(
    node: Node,
    ids: dict[str, Node],
    labels: dict[str, Node],
    parents: dict[int, Node] | None = None,
) -> list[tuple[str, str]]:
    """Every fragment of this element's name, as `(which step named it, the text)`.

    The order is the precedence order of the accessible-name computation (W3C, *Accessible Name and
    Description Computation 1.2*, §4.3.2 step 2): `aria-labelledby` (2.02) over `aria-label` (2.04)
    over the host language's own naming — a `<label for=…>` or a wrapping label (2.05) over the
    element's content, in which a descendant's `aria-label`, its `alt` or its `<svg><title>` count
    (2.06) — over the tooltip attribute (2.09), which the specification marks as used *only if nothing
    else, including subtree content, has provided results*. Only the winning step's fragments are
    returned: a link with both visible text and a `title` is named by its text, and reporting the
    `title` as its name would misdescribe the page. A step that yields nothing falls through to the
    next, which is what the specification asks for (*Return the accumulated text if it is not the
    empty string*).

    The whole algorithm is not implemented here, and the departures are deliberate: no CSS generated
    content, no shadow roots or slots, no `aria-hidden` or `display:none` exclusion inside a name, no
    full flat-string normalisation, and no role-based prohibition of naming — that last one is safe
    because this tool only asks the question of links and of form controls, whose roles all allow
    naming. `axe-core` implements the rest. What this function has to be is decidable and explainable
    for the ten rules beside it, and to *say where each name came from*, so the number of names a
    text-only rule would have missed is a line in the report rather than a claim in a comment.
    """
    labelled = _labelled_by(node, ids)
    if labelled:
        return labelled

    aria_label = [fragment for fragment in _own_attributes(node) if fragment[0] == "aria-label"]
    if aria_label:
        return aria_label

    if node.tag in CONTROLS:
        label = labels.get(node.attrs.get("id", ""))
        if label is not None and label.text_of().strip():
            return [("label-for", label.text_of().strip())]
        if parents:
            wrapping = wrapper_label(node, parents)
            if wrapping is not None:
                # The wrapper's text already contains the control's own text, so only the wrapper's
                # text outside this control is used; joining the whole wrapper would double it.
                # Only the text outside the control counts, and only if there is any: a `<label>`
                # that wraps a control and says nothing else names it as much as an empty `<label>`
                # does, which is to say not at all. (An earlier version fell back to the whole
                # wrapper's text, which for a `<textarea>` is its *content* — that is the value a
                # reader types over, not a name.)
                outside = "".join(
                    [wrapping.text, *(child.text_of() for child in wrapping.children if child is not node)]
                ).strip()
                if outside:
                    return [("wrapper-label", outside)]

    # Step 2.06 applies when the element's role allows naming from its content, and a form control's
    # does not: WAI-ARIA gives `textbox`, `combobox` and `checkbox` the *author* as their name source,
    # which is why `<label><textarea>draft</textarea></label>` says what the control will contain, not
    # what it is called. Reading it the other way named the textarea `draft` — a value used as a name,
    # and the one direction of this rule where a wrong answer is invisible to a reader.
    if node.tag not in CONTROLS:
        content: list[tuple[str, str]] = []
        if node.text.strip():
            content.append(("text", node.text.strip()))
        for child in node.children:
            content.extend(_name_from_content(child, ids, "descendant:"))
        if content:
            return content

    return [fragment for fragment in _own_attributes(node) if fragment[0] == "title"]


def check_page(
    page: Path,
    site: Path,
    hidden: set[str],
    names: dict[str, int] | None = None,
) -> tuple[list[Violation], list[str]]:
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
        if node.tag == "a" and node.attrs.get("href", "").strip():
            parts = name_parts(node, ids, labels, parents)
            if not parts:
                flag("link-name", f"link to {node.attrs['href'][:70]!r} has no accessible name", node.line)
            elif names is not None:
                names[parts[0][0]] = names.get(parts[0][0], 0) + 1
        if node.tag in CONTROLS and node.attrs.get("type", "text").lower() not in SILENT_INPUT_TYPES:
            # `md-toggle` is a class the site's stylesheet declares `display:none`: not rendered, so not
            # announced, so no name is due — while the rule exists. See the module docstring.
            if set(node.attrs.get("class", "").split()) & hidden:
                continue
            parts = name_parts(node, ids, labels, parents)
            if not parts:
                flag("control-name", f"<{node.tag} {_describe(node)}> has no accessible name", node.line)
            elif names is not None:
                names["control from " + parts[0][0]] = names.get("control from " + parts[0][0], 0) + 1
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
    names: dict[str, int] = {}
    for page in pages:
        page_violations, page_classes = check_page(page, site, hidden, names)
        violations.extend(page_violations)
        exempted_classes |= set(page_classes)
    return (
        violations,
        len(pages),
        {
            "classes_declared_display_none": sorted(hidden),
            "classes_that_exempted_a_control": sorted(exempted_classes),
            "css_evidence": evidence,
            "names_by_source": dict(sorted(names.items(), key=lambda item: (-item[1], item[0]))),
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
    print(f"{pages} page(s) checked: no violation of the {len(RULES)} rules; {note}{named_by(exemptions)}")
    return 0


def named_by(exemptions: dict[str, object]) -> str:
    """A clause for the summary line: how many names the site's elements got from text, and how many
    from somewhere else. Not decoration — it is the count of elements a text-only rule would have
    reported as nameless, and it is the number that justifies resolving attributes at all."""
    tally = exemptions.get("names_by_source")
    if not isinstance(tally, dict) or not tally:
        return ""
    text = sum(count for source, count in tally.items() if source.endswith("text"))
    elsewhere = {source: count for source, count in tally.items() if not source.endswith("text")}
    if not elsewhere:
        return f"; all {text} name(s) came from the element's own text"
    detail = ", ".join(f"{source} {count}" for source, count in list(elsewhere.items())[:3])
    return f"; {text} name(s) from text and {sum(elsewhere.values())} from elsewhere ({detail})"


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
