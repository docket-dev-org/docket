"""Every markdown table in the documentation renders as the table it looks like.

Two defects were found in the published docs by counting rather than by reading, and neither produced
a symptom anywhere:

1. `docs/testing.md` declared a three-column header, `file | tests | what it pins`, while 17 rows
   carried a fourth cell holding why the guard exists. GFM ignores cells beyond the header count, so
   17 rationales -- the "why" behind `test_diagrams.py`, `test_propagation.py`,
   `test_release_workflow.py` and the rest -- were never rendered on the published site. Nothing
   failed, because nothing counted.
2. `docs/production-coverage.md:149` carried `` `| int64` `` with an unescaped pipe inside a code
   span, so the row split mid-code-span and the text after it became a cell the header did not have.

A third candidate looked like the same defect and was not: `docs/deployment.md:65` and
`docs/testing.md:305` also show more pipes than their header has columns, because they use `\\|` for
the same Helm notation, which GFM reads as a literal pipe inside one cell. Three of the five
candidates this started with were correct, which is why the check honours escapes before it counts.

The `test_build_dist.py` row was the same defect with a second layer: it carried five cells and its
text was triplicated, because a `" | " + reason` concatenation had run twice. Cell [4] was a
402-character base, [3] the base plus one extension and [2] the base plus another. A repair that
joins two reasons into one cell is not the same as one that merges them, and the difference is a row
that repeats its own opening sentence three times.
"""

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DOCUMENTS = sorted((REPO / "docs").glob("*.md")) + [REPO / "README.md"]

SEPARATOR = re.compile(r"^\|?[\s:|-]+\|[\s:|-]*$")


def split_cells(row: str) -> list[str]:
    """Split a GFM table row into cells, honouring `\\|` as a literal pipe.

    A naive `split("|")` counts an escaped pipe as a separator and reports correct rows as broken.
    """
    body = row.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    return re.split(r"(?<!\\)\|", body)


def tables(document: Path):
    """Yield (line number, header cells, [(line number, cells)]) for each table in a document."""
    lines = document.read_text(encoding="utf-8").splitlines()
    index = 0
    while index < len(lines) - 1:
        if lines[index].strip().startswith("|") and SEPARATOR.match(lines[index + 1].strip()):
            header = split_cells(lines[index])
            rows: list[tuple[int, list[str]]] = []
            cursor = index + 2
            while cursor < len(lines) and lines[cursor].strip().startswith("|"):
                rows.append((cursor + 1, split_cells(lines[cursor])))
                cursor += 1
            yield index + 1, header, rows
            index = cursor
        else:
            index += 1


def unescaped_pipe_in_code_span(row: str) -> bool:
    """Whether a row carries an unescaped `|` inside a code span, decided by walking the row."""
    inside = False
    for index, character in enumerate(row):
        if character == "`":
            inside = not inside
        elif character == "|" and inside and (index == 0 or row[index - 1] != "\\"):
            return True
    return False


def every_row() -> list[tuple[str, int, list[str], int, list[str]]]:
    """Every table row in the documentation, with its header's width."""
    found = []
    for document in DOCUMENTS:
        for _start, header, rows in tables(document):
            for lineno, cells in rows:
                found.append((document.name, lineno, cells, len(header), header))
    return found


ROWS = every_row()


def test_the_documentation_has_tables_to_check() -> None:
    """A guard over an empty set passes for the wrong reason."""
    assert len(ROWS) > 100, f"only {len(ROWS)} rows found; the scanner has stopped working"
    assert len(DOCUMENTS) > 10, f"only {len(DOCUMENTS)} documents found"


@pytest.mark.parametrize(
    ("document", "lineno", "cells", "width", "header"),
    ROWS,
    ids=[f"{document}:{lineno}" for document, lineno, *_ in ROWS],
)
def test_a_row_has_as_many_cells_as_its_header(
    document: str, lineno: int, cells: list[str], width: int, header: list[str]
) -> None:
    """A row with more cells than its header loses the extra ones when rendered.

    GFM ignores cells beyond the header count, so excess is silent: the text is in the file, is not
    on the page, and nothing reports it. A row with fewer is padded, so only excess is a defect.
    """
    assert len(cells) <= width, (
        f"{document}:{lineno} has {len(cells)} cells against a {width}-column header "
        f"({header}), so {len(cells) - width} cell(s) are dropped when rendered"
    )


@pytest.mark.parametrize(
    ("document", "lineno", "cells", "width", "header"),
    ROWS,
    ids=[f"{document}:{lineno}" for document, lineno, *_ in ROWS],
)
def test_no_row_splits_inside_a_code_span(
    document: str, lineno: int, cells: list[str], width: int, header: list[str]
) -> None:
    """A pipe inside backticks must be escaped, or it becomes a cell boundary mid-code-span."""
    row = "|" + "|".join(cells) + "|"
    assert not unescaped_pipe_in_code_span(row), (
        f"{document}:{lineno} carries an unescaped pipe inside a code span, so the row splits there"
    )


def test_no_row_repeats_itself() -> None:
    """A `" | " + reason` concatenation run twice leaves a row that repeats its own opening.

    The `test_build_dist.py` row was that: five cells, three of them the same 402-character base
    with different extensions. Detected by asking whether any row's cells share a long common
    prefix, which is what a repeated base looks like from the inside.
    """
    repeats = []
    for document, lineno, cells, _width, _header in ROWS:
        prose = [cell.strip() for cell in cells if len(cell.strip()) > 200]
        for first in range(len(prose)):
            for second in range(first + 1, len(prose)):
                shorter, longer = sorted((prose[first], prose[second]), key=len)
                if shorter and longer.startswith(shorter[:200]):
                    repeats.append((document, lineno))
    assert not repeats, f"row(s) whose cells share a long common prefix, which is a repeated base: {repeats}"


def test_the_testing_table_is_uniform() -> None:
    """The per-file table is the one that drifted, so it is held to an exact width."""
    document = REPO / "docs" / "testing.md"
    rows = [
        (lineno, cells)
        for _start, header, table_rows in tables(document)
        if [cell.strip() for cell in header[:2]] == ["file", "tests"]
        for lineno, cells in table_rows
    ]
    assert rows, "the per-file table was not found"
    widths = {len(cells) for _lineno, cells in rows}
    assert widths == {4}, f"the per-file table has rows of {sorted(widths)} cells, expected exactly 4"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "--tb=short"]))
