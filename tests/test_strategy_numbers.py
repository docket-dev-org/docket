"""The adoption page's numbers, against the sweep that ships beside them.

`docs/strategy.md` argues about star counts: how many repositories hold more than 100,000 stars, and
how fast ten comparable projects got there. On 2026-10-17 an audit of the repository's own claims
found that two of those figures could not be traced to anything — one had been read against a slug
that does not exist (`dify/dify` answers 404; the project is `langgenius/dify`) and one had come from
a search listing nobody saved. A number in a published page that no artefact produces is the defect
this repository's `README.md` calls a bug in as many words, so the fix was not only to re-base the
numbers but to ship the sweep: `docs/measurements/2026-10-16-github-sweep/` holds the raw JSON of the
query, one row per project, each with the slug it asked for and the URL that answered.

These tests are what keeps the page and that file from drifting apart in a clone, with no network and
no workspace around it. They cannot re-ask GitHub — that is the sweep's job, and it is re-run when
someone re-bases the page — but they can refuse a page whose numbers stopped matching the evidence
shipped next to it, which is the failure that actually happened.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "docs" / "strategy.md"
SWEEP_DIR = ROOT / "docs" / "measurements" / "2026-10-16-github-sweep"

#: The page's table of comparable projects, one row per project:
#: `| gemini-cli | 107,412 | 18.0 months | ~6,000 |`
TABLE_ROW = re.compile(r"^\| ([a-zA-Z][\w.-]*) \| ([\d,]+) \| ([\d.]+) months \| ~([\d,]+) \|$", re.MULTILINE)

#: The page's three ecosystem rows: `| repositories with more than **100,000** stars | **132** | …`
TOTAL_ROW = re.compile(
    r"^\| repositories with more than (?:\*\*)?(\d[\d,]*)(?:\*\*)? stars \| (?:\*\*)?(\d+)(?:\*\*)? \|",
    re.MULTILINE,
)


def page() -> str:
    return PAGE.read_text(encoding="utf-8")


def sweep_files() -> list[Path]:
    files = sorted(SWEEP_DIR.glob("gh_sweep-*.json"), key=lambda path: path.name)
    assert files, f"no sweep ships in {SWEEP_DIR.relative_to(ROOT)} — the page's numbers cite one"
    return files


def sweep() -> dict:
    """The newest shipped sweep. Its file name carries the UTC stamp of the query."""
    return json.loads(sweep_files()[-1].read_text(encoding="utf-8"))


def sweep_rows() -> dict[str, dict]:
    return {row["label_in_plan"]: row for row in sweep()["repositories"]}


def test_the_page_names_the_sweep_that_ships_beside_it() -> None:
    """A page that quotes numbers has to say where they came from, by file name."""
    newest = sweep_files()[-1].name
    assert newest in page(), f"docs/strategy.md does not name the shipped sweep {newest}"
    named = set(re.findall(r"gh_sweep-[\w.-]+\.json", page()))
    assert named == {newest}, f"the page names sweep(s) other than the newest: {sorted(named)}"


def test_every_project_the_page_compares_against_has_a_sweep_row() -> None:
    """Both directions: a table row with no evidence, or evidence this page never uses, is a drift."""
    table = {match.group(1) for match in TABLE_ROW.finditer(page())}
    inline = {row["label_in_plan"] for row in sweep()["repositories"]}
    missing = sorted(table - inline)
    assert not missing, f"docs/strategy.md compares against {missing}, which the sweep does not carry"
    unquoted = sorted(inline - table - {"phoenix", "openllmetry"})
    assert not unquoted, f"the sweep carries {unquoted}, which the page's table does not quote"


def test_the_table_star_counts_are_the_sweeps_with_commas() -> None:
    """The page writes `107,412`; the API answers `107412`. Anything else is a stale or typed number."""
    rows = sweep_rows()
    checked = 0
    for name, written, _age, _rate in TABLE_ROW.findall(page()):
        row = rows[name]
        message = f"docs/strategy.md says {name} has {written} stars; the sweep of {row['slug']} says {row['stars']}"
        assert written.replace(",", "") == str(row["stars"]), message
        checked += 1
    assert checked >= 10, f"only {checked} comparable rows parsed — the table changed shape"


def test_the_table_ages_are_the_months_the_sweep_measured() -> None:
    """Age is months since creation as the API reports it, to one decimal — not a remembered figure."""
    rows = sweep_rows()
    for name, _stars, age, _rate in TABLE_ROW.findall(page()):
        row = rows[name]
        assert abs(float(age) - row["age_months"]) <= 0.1, (
            f"docs/strategy.md says {name} is {age} months old; the sweep, from created "
            f"{row['created']}, says {row['age_months']}"
        )


def test_the_table_rates_are_the_stars_over_the_age() -> None:
    """The rate column is arithmetic, rounded to the hundreds the page writes: `~6,000`."""
    rows = sweep_rows()
    for name, _stars, _age, rate in TABLE_ROW.findall(page()):
        row = rows[name]
        assert abs(int(rate.replace(",", "")) - row["stars_per_month"]) <= 100, (
            f"docs/strategy.md writes ~{rate}/month for {name}; {row['stars']} stars over "
            f"{row['age_months']} months is {row['stars_per_month']}/month"
        )


def test_the_inline_audience_figures_agree_with_the_sweep() -> None:
    """The same numbers appear again in prose, and prose is where a stale count hides."""
    rows = sweep_rows()
    text = page()
    for label, pattern in (
        ("Claude-Code-Usage-Monitor", r"`Claude-Code-Usage-Monitor` has \*\*([\d,]+) stars\*\*"),
        ("litellm", r"LiteLLM \((\d[\d,]*)\)"),
        ("langfuse", r"Langfuse \((\d[\d,]*)\)"),
    ):
        match = re.search(pattern, text)
        assert match, f"the inline figure for {label} is gone from docs/strategy.md"
        written, row = match.group(1).replace(",", ""), rows[label]
        message = f"docs/strategy.md writes {label} as {match.group(1)}; the sweep says {row['stars']}"
        assert written == str(row["stars"]), message


def test_the_ecosystem_totals_are_the_search_counts() -> None:
    """The three threshold rows are `total_count` from three queries in the same sweep."""
    totals = {row["query"]: row["total_count"] for row in sweep()["searches"]}
    checked = 0
    for threshold, written in TOTAL_ROW.findall(page()):
        query = f"stars:>{threshold.replace(',', '')}"
        assert query in totals, f"docs/strategy.md has a row for {query}, which the sweep never asked"
        assert int(written) == totals[query], (
            f"docs/strategy.md says {written} repositories over {threshold} stars; the sweep says " f"{totals[query]}"
        )
        checked += 1
    assert checked == 3, f"parsed {checked} ecosystem rows, expected three"


def test_the_page_dates_the_sweep_the_sweep_dates_itself() -> None:
    """The timestamp in the page is the sweep's own `queried_utc`, to the minute."""
    stamp = sweep()["queried_utc"][:16]
    assert stamp in page(), f"docs/strategy.md does not carry the sweep's query time {stamp}"


def test_every_sweep_row_records_the_request_that_produced_it() -> None:
    """A row that does not name its own request cannot be re-run by a reader."""
    for row in sweep()["repositories"]:
        assert row["slug"].count("/") == 1, f"{row['slug']} is not an owner/repo slug"
        assert row["request"] == f"/repos/{row['slug']}", f"{row['slug']}: request path disagrees with row"
        assert row["response_url"] == f"https://api.github.com/repos/{row['slug']}"
    for row in sweep()["searches"]:
        # The query is percent-encoded in the URL GitHub was actually called with
        # (`stars%3A%3E100000`), so the row's readable query is what gets compared.
        encoded = urllib.parse.quote(row["query"])
        assert row["request"] == f"/search/repositories?q={encoded}&per_page=1", row["query"]
        assert row["response_url"] == f"https://api.github.com{row['request']}"


def test_no_row_the_page_quotes_is_an_error_row() -> None:
    """`dify/dify` answers 404: the sweep records a 404 as a row, and the page must not quote one."""
    text = page()
    for row in sweep()["repositories"]:
        if row.get("error"):
            assert (
                row["label_in_plan"] not in text
            ), f"docs/strategy.md quotes {row['label_in_plan']}, whose sweep row is an error: {row['error']}"
        else:
            assert isinstance(row["stars"], int) and row["stars"] > 0, f"{row['slug']}: no star count"
            assert row["age_months"] > 0, f"{row['slug']}: no age"


def test_the_sweep_file_is_the_newest_one_shipped() -> None:
    """If a newer sweep is ever added beside it, the page has to be re-based onto that one."""
    files = sweep_files()
    assert files[-1].name >= files[0].name
    assert len({json.loads(path.read_text(encoding="utf-8"))["queried_utc"] for path in files}) == len(
        files
    ), "two shipped sweeps carry the same query time — one of them is a copy"
