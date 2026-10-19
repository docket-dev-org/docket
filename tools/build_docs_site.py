#!/usr/bin/env python3
"""Build the documentation site, or check that it *can* be built.

The site is generated from a staged copy of the repository's Markdown rather than
from `docs/` alone, and that is deliberate. Documents link to their neighbours with
paths that are correct on GitHub (`../ROADMAP.md`, `../spec/docket-receipt-1.md`),
and a site built from `docs/` only would 404 on every one of those. Staging the tree
means there is exactly one set of links, valid in both places, with no rewrite step
that can silently drift.

    python3 tools/build_docs_site.py            # build to ./site  (strict: warnings fail)
    python3 tools/build_docs_site.py --serve    # preview at http://127.0.0.1:8000
    python3 tools/build_docs_site.py --check    # build to a temp dir and discard

Requires `mkdocs-material` (`pip install mkdocs-material`).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / ".mkdocs-site"
SKIP_DIRS = {".git", ".mkdocs-site", "site", "dist", "build", "node_modules", "__pycache__", ".venv"}
#: Paths that are part of the repository but not part of the site. `tests/golden` holds the
#: rendered-output fixtures the golden tests compare against; no document links to them, and mkdocs
#: reported each one as "a page not in the nav" — which is the right complaint about a fixture that
#: was staged as documentation. `assets/BRAND.md` is the same shape for a different reason: it is
#: the in-repository alias map for the brand kit, written for whoever regenerates the artwork, and
#: it names no document a reader arrives from. Leaving it in the staged tree adds a page to the site
#: that the navigation does not list, which is the orphan mkdocs is right to complain about.
SKIP_PATHS = {"tests/golden", "assets/BRAND.md"}


def stage(destination: Path) -> None:
    """Copy the Markdown tree, preserving relative paths, plus the assets it references."""
    for path in sorted(ROOT.rglob("*")):
        relative_to_root = path.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in relative_to_root.parts):
            continue
        if any(
            relative_to_root == Path(skipped) or Path(skipped) in relative_to_root.parents for skipped in SKIP_PATHS
        ):
            continue
        if path.is_dir():
            continue
        relative = path.relative_to(ROOT)
        # Documents link to real repository files — `pyproject.toml`, example policies, and the
        # tools they are describing (`tools/deploy_check.py`, `tools/loadtest.py`) — so the staging
        # step carries the file kinds they name. It is not cosmetic: `mkdocs build --strict` treats
        # a link to a file it cannot find as a warning and *aborts*, so a document that points at a
        # `tools/*.py` is a red build until that file is staged. The kinds below are the ones the
        # documentation actually references; `tests/test_doc_links.py` stages this tree in a test and
        # resolves every relative link against it, so a new kind of link fails there rather than in
        # the docs job.
        keep = path.suffix in {
            ".md",
            ".png",
            ".svg",
            ".jpg",
            ".webp",
            # `assets/favicon.ico` is the legacy multi-size favicon the site links
            # from `overrides/main.html`, and `assets/brand.css` is the palette it
            # loads through `extra_css`. Both are referenced by the built pages, so
            # a stager that dropped them would ship a site with two broken asset
            # references — and neither suffix was in this set before the brand kit
            # was adopted, which is exactly how that would have gone unnoticed.
            ".ico",
            ".css",
            ".toml",
            ".json",
            ".yml",
            ".yaml",
            ".py",
            ".sh",
            ".txt",
        } or (relative.name in {"mkdocs.yml", "LICENSE", "Makefile", "Dockerfile", ".dockerignore", ".gitignore"})
        if not keep:
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true", help="run a local preview server")
    parser.add_argument("--check", action="store_true", help="build to a temporary directory")
    args = parser.parse_args()

    if shutil.which("mkdocs") is None:
        print("mkdocs is not installed: pip install mkdocs-material", file=sys.stderr)
        return 2

    if STAGE.exists():
        shutil.rmtree(STAGE)
    stage(STAGE)

    try:
        if args.check:
            with tempfile.TemporaryDirectory() as tmp:
                return subprocess.run(
                    ["mkdocs", "build", "--strict", "--site-dir", str(Path(tmp) / "site")],
                    cwd=ROOT,
                    check=False,
                ).returncode
        if args.serve:
            return subprocess.run(["mkdocs", "serve", "--strict"], cwd=ROOT, check=False).returncode
        return subprocess.run(["mkdocs", "build", "--strict"], cwd=ROOT, check=False).returncode
    finally:
        # The staged tree is an artifact of the build, not something to leave behind for
        # someone to edit by mistake.
        if args.check and STAGE.exists():
            shutil.rmtree(STAGE, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
