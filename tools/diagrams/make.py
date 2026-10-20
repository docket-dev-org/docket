#!/usr/bin/env python3
"""Emit the documentation figures as light and dark SVG pairs.

    python3 tools/diagrams/make.py           # write every figure
    python3 tools/diagrams/make.py --check   # fail if the tree is not up to date

The sources live in `assets/diagrams/source/` and the output in `assets/diagrams/`, so the figures
are reviewable as text and regenerable in one command. That is the durability requirement: a figure
whose numbers come from a committed fixture and whose layout comes from a shared design system is a
figure that can change without being redrawn by hand.

A figure is emitted once per theme, from the same function, so a figure cannot exist in one theme
and silently not in the other.
"""

from __future__ import annotations

import argparse
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUTPUT = ROOT / "assets" / "diagrams"

from design import THEMES  # noqa: E402 - a sibling module, imported after the path constants
from figures import (  # noqa: E402
    figure_cap,
    figure_chain,
    figure_checks,
    figure_pipeline,
    figure_receipt,
    figure_verifiers,
)

FIGURES = (
    ("pipeline", figure_pipeline),
    ("cap", figure_cap),
    ("chain", figure_chain),
    ("receipt", figure_receipt),
    ("verifiers", figure_verifiers),
    ("checks", figure_checks),
)


def render() -> dict[str, str]:
    """Every file the generator owns, as a path relative to `assets/diagrams/`."""
    written: dict[str, str] = {}
    for slug, builder in FIGURES:
        for theme in THEMES:
            written[f"{slug}-{theme.name}.svg"] = builder(theme)
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the tree is not up to date")
    options = parser.parse_args()

    expected = render()
    stale: list[str] = []
    for relative, content in expected.items():
        path = OUTPUT / relative
        # The trailing newline is part of what is written, so it is part of what is compared.
        # Comparing against the bare document reported every figure stale the moment it was
        # regenerated, which is a guard that fires on a correct tree.
        on_disk = content + "\n"
        if options.check:
            if not path.exists() or path.read_text(encoding="utf-8") != on_disk:
                stale.append(relative)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(on_disk, encoding="utf-8")

    if options.check:
        if stale:
            print(f"{len(stale)} figure(s) are not up to date:")
            for name in sorted(stale):
                print(f"  {name}")
            print("run `python3 tools/diagrams/make.py`")
            return 1
        print(f"all {len(expected)} figures are up to date")
        return 0

    total = sum(len(content) for content in expected.values())
    print(f"wrote {len(expected)} figures, {total:,} bytes, to {OUTPUT.relative_to(ROOT)}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
