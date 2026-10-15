"""Golden files for the rendering: the renderer is a product surface, and it is not code-tested.

Everything else in this repository is checked by asserting on values. The two renderers —
`render_receipt` (the terminal "what this receipt proves" panel) and `render_markdown` (the
report committed next to a receipt) — are checked by *pixels*, in the sense that the thing users
read is the text itself. A test that asserts "the word rollback appears" cannot notice that the
alignment moved, that a column was dropped, or that a caveat sentence vanished; a golden file
can, and an unintended change to it is exactly the kind of regression a reviewer of the diff
would otherwise have to catch by eye.

The inputs are the committed conformance vectors, so the output is deterministic: fixed
timestamps, fixed numbers, no paths, no clock. Regenerate deliberately after a wording change:

    DOCKET_UPDATE_GOLDEN=1 python3 -m pytest tests/test_render_golden.py

then read the diff before committing it — that diff *is* the review.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from docket.report import diff_receipts, render_diff, render_markdown, render_receipt

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "golden"
VECTORS = ROOT / "spec" / "vectors"


def _receipt() -> dict:
    return json.loads((VECTORS / "receipt.json").read_text(encoding="utf-8"))


def _compare(name: str, produced: str) -> None:
    path = GOLDEN / name
    if os.environ.get("DOCKET_UPDATE_GOLDEN"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(produced, encoding="utf-8")
        pytest.skip(f"regenerated {path.relative_to(ROOT)}")
    assert path.exists(), f"{path.relative_to(ROOT)} is missing; regenerate with DOCKET_UPDATE_GOLDEN=1"
    expected = path.read_text(encoding="utf-8")
    if expected != produced:
        # Show the first difference rather than the whole file: the point is to make the change
        # legible in a terminal, not to dump a kilobyte of identical text.
        expected_lines = expected.splitlines()
        produced_lines = produced.splitlines()
        for index, (left, right) in enumerate(zip(expected_lines, produced_lines, strict=False), start=1):
            if left != right:
                pytest.fail(
                    f"{name} changed at line {index}:\n  golden:   {left!r}\n  produced: {right!r}\n"
                    "If the change is intended, regenerate with DOCKET_UPDATE_GOLDEN=1 and read the diff."
                )
        pytest.fail(f"{name} changed length: golden has {len(expected_lines)} lines, produced {len(produced_lines)}")


def test_the_terminal_receipt_rendering_is_unchanged():
    _compare("receipt-vector.txt", render_receipt(_receipt(), color=False))


def test_the_markdown_report_rendering_is_unchanged():
    _compare("report-vector.md", render_markdown(_receipt()))


def test_the_diff_rendering_is_unchanged():
    """`docket diff` compares two runs; its output is the only place two receipts are read
    side by side, so it is the one most likely to drift silently."""
    baseline = _receipt()
    later = json.loads(json.dumps(baseline))
    later["docket_number"] = "DKT-20260103-VECT02"
    later["created_at"] = "2026-01-03T03:04:05Z"
    later["totals"]["cost_usd"] = "0.031500"
    later["totals"]["calls"] = 7
    later["model_calls"]["count"] = 7
    later["files"]["changed"] = 2
    differences = diff_receipts(baseline, later)
    _compare("diff-vector.txt", render_diff(differences, color=False) + "\n")
