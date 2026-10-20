#!/usr/bin/env python3
"""Report on the documentation figures: parse, measure, and look.

    python3 tools/diagrams/check.py

The rules live in `assets/diagrams/source/validate.py`, and `tests/test_diagrams.py` calls the same
module, so a rule cannot pass here and fail there. This script is the human-facing view; the test is
the gate.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DIAGRAMS = REPO / "assets" / "diagrams"

from validate import RULES, parse, problems_for  # noqa: E402 - a sibling module


def main() -> int:
    files = sorted(DIAGRAMS.glob("*.svg"))
    if not files:
        print(f"no figures in {DIAGRAMS.relative_to(REPO)}; run tools/make_diagrams.py")
        return 1

    problems = 0
    for path in files:
        found = problems_for(path)
        tree = parse(path)
        texts = len(tree.findall(".//{http://www.w3.org/2000/svg}text"))
        rects = len(tree.findall(".//{http://www.w3.org/2000/svg}rect"))
        problems += len(found)
        print(
            f"  {'ok  ' if not found else 'FAIL'} {path.name:<24} {texts:>3} text  "
            f"{rects:>3} rect  {len(path.read_text(encoding='utf-8')):>6} B  "
            f"{tree.get('viewBox') if tree is not None else ''}"
        )
        for problem in found:
            print(f"         {problem}")

    print()
    print(f"{len(files)} figures, {len(RULES)} rules each")
    if problems:
        print(f"{problems} problem(s)")
        return 1
    print("every figure parses, is accessible, is high contrast, fits its canvas, and has no overlap")
    return 0
