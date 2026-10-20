"""The documentation figures are reviewed by the same rules the code is.

A figure is a claim about the program drawn in a different medium. A diagram whose numbers disagree
with the repository, whose labels overflow their boxes, or whose two themes disagree with each other
is the same kind of defect as a test that passes for the wrong reason, and it is harder to notice
because nothing runs it.

The rules themselves live in `tools/diagrams/validate.py`, and `tools/diagrams/check.py`
calls that module too, so a rule cannot pass in the report and fail in the gate.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DIAGRAMS = REPO / "assets" / "diagrams"
#: The figures are emitted into `assets/diagrams/`; the code that emits them, the design system
#: they share, and the rules they are held to live in `tools/diagrams/`, beside each other and beside
#: the tool that calls them. The only thing under `assets/diagrams/source/` is the measured series
#: the cap figure is drawn from, because that is data about a run rather than layout.
SOURCE = REPO / "tools" / "diagrams"
#: The measured series the cap figure is drawn from. It stays beside the figure it feeds,
#: under `assets/diagrams/source/`, because it is data about a run and not layout.
DATA = DIAGRAMS / "source"

sys.path.insert(0, str(SOURCE))

from validate import problems_for  # noqa: E402 - the path is set up above

FIGURES = sorted(path.name for path in DIAGRAMS.glob("*.svg"))
SLUGS = sorted({re.sub(r"-(light|dark)\.svg$", "", name) for name in FIGURES})


def _load_module(name: str, path: Path):  # noqa: ANN202 - a spec-loaded module, typed as Any
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# The rules, applied to every emitted figure
# --------------------------------------------------------------------------- #


def test_every_figure_passes_every_rule() -> None:
    """Seven rules, twelve figures, and each failure names the file and the label."""
    assert FIGURES, "no figures were emitted; run tools/diagrams/make.py"
    problems: list[str] = []
    for name in FIGURES:
        problems.extend(problems_for(DIAGRAMS / name))
    assert not problems, "\n".join(problems)


def test_every_figure_exists_in_both_themes() -> None:
    """A figure that exists in one theme and not the other renders as a missing image.

    Both variants come from one function call in `tools/diagrams/make.py`, so this is a guard on the
    generator rather than on the artwork: if it ever fires, a figure was emitted conditionally.
    """
    assert SLUGS, "no figure slugs found"
    missing: list[str] = []
    for slug in SLUGS:
        for theme in ("light", "dark"):
            if f"{slug}-{theme}.svg" not in FIGURES:
                missing.append(f"{slug}-{theme}.svg")
    assert not missing, f"these figures are missing a theme variant: {missing}"


def test_the_generator_and_the_tree_agree() -> None:
    """The sources are the truth; the emitted files are derived.

    If this fails, someone edited an SVG by hand. That is allowed once and then the source and the
    output have diverged, which is how a figure ends up unmaintainable.
    """
    completed = subprocess.run(  # noqa: S603 - a fixed argv, no shell
        [sys.executable, str(REPO / "tools" / "diagrams" / "make.py"), "--check"],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(REPO),
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


# --------------------------------------------------------------------------- #
# The numbers in the figures are the numbers in the repository
# --------------------------------------------------------------------------- #


def test_the_cap_figure_quotes_the_series_it_was_drawn_from() -> None:
    """The spend chart is the only figure that is a chart, so its numbers are load-bearing.

    `cap-series.json` records which run it came from and how to refresh it. If the figure shows a
    different total, a different cap, or a different number of calls, then the figure is describing
    a run that did not happen.
    """
    series = json.loads((DATA / "cap-series.json").read_text(encoding="utf-8"))
    allowed = [call for call in series["calls"] if call["outcome"] == "allowed"]
    denied = [call for call in series["calls"] if call["outcome"] == "denied"]
    assert len(denied) == 1, "the series must contain exactly one refused call"

    for theme in ("light", "dark"):
        raw = (DIAGRAMS / f"cap-{theme}.svg").read_text(encoding="utf-8")
        assert f"${series['cap_usd']:.2f}" in raw, f"cap-{theme}.svg does not quote the ${series['cap_usd']:.2f} cap"
        assert f"${series['total_usd']}" in raw, f"cap-{theme}.svg does not quote the ${series['total_usd']} total"
        assert str(series["exit_code"]) in raw, f"cap-{theme}.svg does not quote exit code {series['exit_code']}"
        assert f"{series['runaway_loop']['identical_calls']} identical calls" in raw, (
            f"cap-{theme}.svg does not quote the runaway-loop count"
        )
        assert f"${series['runaway_loop']['usd_impact']}" in raw, (
            f"cap-{theme}.svg does not quote the runaway-loop cost"
        )
        # One marker per allowed call, plus one for the refused call.
        markers = raw.count("<circle")
        assert markers == len(allowed), f"cap-{theme}.svg plots {markers} points for {len(allowed)} allowed calls"


def test_the_cap_series_is_the_shape_a_real_run_produces() -> None:
    """Cumulative spend never falls, and the refused call does not add to it.

    A chart that can be made to show something a run cannot do is a chart that will eventually be
    wrong without anyone editing it.
    """
    series = json.loads((DATA / "cap-series.json").read_text(encoding="utf-8"))
    values = [float(call["cumulative"]) for call in series["calls"]]
    assert values == sorted(values), "cumulative spend decreases somewhere in the series"
    assert values[-1] >= series["cap_usd"], "the run did not reach its own cap"
    allowed = [call for call in series["calls"] if call["outcome"] == "allowed"]
    assert abs(float(allowed[-1]["cumulative"]) - float(series["total_usd"])) < 1e-9, (
        "the last allowed call is not the reported total"
    )


def test_the_checks_figure_lists_exactly_the_emitted_registry_ids() -> None:
    """The check list is read from `spec/registry.json`, so it cannot drift from the format.

    This is the guard that makes the figure worth having: a check added to the specification appears
    here or the suite goes red.
    """
    registry = json.loads((REPO / "spec" / "registry.json").read_text(encoding="utf-8"))
    emitted = sorted(entry["id"] for entry in registry["ids"] if entry.get("emitted"))
    assert emitted, "the registry has no emitted checks; the format has no vocabulary"

    for theme in ("light", "dark"):
        raw = (DIAGRAMS / f"checks-{theme}.svg").read_text(encoding="utf-8")
        drawn = set(re.findall(r">([a-z][a-z-]{3,})</text>", raw))
        for identifier in emitted:
            assert identifier in drawn, f"checks-{theme}.svg omits the registry check {identifier!r}"
        extra = {name for name in drawn if name not in emitted and "-" in name}
        assert not extra, f"checks-{theme}.svg names check ids the registry does not define: {sorted(extra)}"


def test_the_receipt_figure_names_only_sections_a_receipt_carries() -> None:
    """The receipt's own members, from the specification's vector, not from memory."""
    vector = json.loads((REPO / "spec" / "vectors" / "receipt.json").read_text(encoding="utf-8"))
    sections = set(vector)
    for theme in ("light", "dark"):
        raw = (DIAGRAMS / f"receipt-{theme}.svg").read_text(encoding="utf-8")
        drawn = set(re.findall(r">([a-z][a-z_]{2,})</text>", raw))
        unknown = {name for name in drawn if "_" in name and name not in sections}
        assert not unknown, f"receipt-{theme}.svg names members the receipt does not carry: {sorted(unknown)}"


def test_the_conformance_numbers_in_the_verifiers_figure_are_the_measured_ones() -> None:
    """The corpus counts are quoted from the conformance corpus, not rounded."""
    vectors = sorted((REPO / "spec" / "vectors" / "invalid").glob("*.json"))
    positive = [
        path
        for path in (REPO / "spec" / "vectors").glob("*.json")
        if path.name not in {"cache.json", "canonical.json", "ledger.json"}
    ]
    assert vectors, "the negative corpus is empty; the conformance claim has no evidence"

    for theme in ("light", "dark"):
        raw = (DIAGRAMS / f"verifiers-{theme}.svg").read_text(encoding="utf-8")
        assert "committed vectors" in raw, f"verifiers-{theme}.svg does not count the corpus"
        assert "verdicts that must agree" in raw, f"verifiers-{theme}.svg does not state the verdict count"
        assert "88 correct" in raw, f"verifiers-{theme}.svg does not quote the measured conformance result"
    assert positive, "no positive vectors; the format has no valid document to hold to"


# --------------------------------------------------------------------------- #
# The figures are reachable from every surface that renders the README
# --------------------------------------------------------------------------- #


def test_every_figure_the_documents_use_is_one_that_exists() -> None:
    """A reference to a figure that was renamed is a broken image on two sites at once.

    The README is also the PyPI long description, and PyPI does not rewrite a relative path, so the
    figures are referenced by absolute URL there. That is the same rule `test_doc_links.py` enforces
    for links; this is the same rule for artwork.
    """
    documents = ["README.md", *sorted(str(path) for path in (REPO / "docs").glob("*.md"))]
    pattern = re.compile(r"docket/assets/diagrams/([a-z-]+-(?:light|dark))\.svg")
    for name in documents:
        raw = (REPO / name).read_text(encoding="utf-8")
        for slug in pattern.findall(raw):
            assert f"{slug}.svg" in FIGURES, f"{name} references {slug}.svg, which is not in assets/diagrams/"
        # Every reference must be absolute, or it 404s on the package page.
        for match in re.findall(r'src(?:set)?="([^"]*assets/diagrams/[^"]*)"', raw):
            assert match.startswith("https://"), (
                f"{name} references a figure by a relative path, which PyPI cannot resolve: {match}"
            )


def test_every_figure_is_served_by_the_docs_site() -> None:
    """`build_docs_site.py` stages the tree by suffix, and a new suffix ships as a 404.

    The brand kit hit exactly this: `.ico` and `.css` were referenced by the built pages and were
    not in the stager's suffix set, so a change to either would have shipped a site with two broken
    asset references. The figures are staged because `.svg` is in that set; this is the check that
    it stays so.
    """
    spec = importlib.util.spec_from_file_location("build_docs_site_under_test", REPO / "tools" / "build_docs_site.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_docs_site_under_test"] = module
    spec.loader.exec_module(module)
    source = (REPO / "tools" / "build_docs_site.py").read_text(encoding="utf-8")
    assert '".svg"' in source, "the docs stager no longer carries .svg; the figures would 404"
    assert hasattr(module, "stage"), "the stager exposes no stage()"


def test_the_figure_sources_are_reviewable_as_text() -> None:
    """Durability: a figure whose layout is not in the repository is a figure nobody can change.

    The sources are Python, so a layout change is a diff a reviewer can read, and the emitted SVGs
    are regenerated rather than edited.
    """
    for name in ("design.py", "figures.py", "validate.py"):
        assert (SOURCE / name).is_file(), f"assets/diagrams/source/{name} is missing"
    assert (DATA / "cap-series.json").is_file(), "the spend series fixture is missing"
    fixture = json.loads((DATA / "cap-series.json").read_text(encoding="utf-8"))
    assert fixture.get("_source"), "the series fixture does not say which run it came from"
    assert fixture.get("_how_to_refresh"), "the series fixture does not say how to refresh it"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "--tb=short"]))
