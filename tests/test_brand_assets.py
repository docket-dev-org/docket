"""The brand kit is wired to the surfaces that render it, and nothing retired is left behind.

The logo was replaced on 2026-10-19 by an externally supplied kit (`assets/` is its adoption
overlay). That is a change with three properties that make it worth guarding rather than
reviewing by eye:

1. **It is invisible in the source.** A missing favicon, a stylesheet that loses the cascade, a
   theme override that replaces a page template instead of extending it, or an asset the site
   stager drops are all green in the repository and wrong on the deployed site. The third of
   those is not hypothetical: `overrides/main.html` was first written without `{% extends
   "base.html" %}`, and because `main.html` *is* Material's page template the home page and the
   404 page rendered as that one fragment — 256 bytes where a 50 KB document should be. Nothing
   in the repository said so.
2. **The retired identity is still reachable.** The old mark was indigo `#4f46e5` and teal
   `#2dd4bf`, and `mkdocs.yml`'s `primary: indigo` / `accent: teal` palette carried those exact
   colours. A palette name is not a colour, so "is the old branding gone" is a question about
   hex values in files, not about a diff that looks plausible.
3. **The kit's own file had to be reformatted.** `assets/make-assets.py` arrives from the kit with
   imports in a different order and no blank line between the standard-library and third-party
   groups, which this repository's `ruff` gate refuses. The render calls are unchanged and the
   output is byte-identical, and `test_the_brand_raster_generator_reproduces_the_checked_in_pngs`
   is what keeps "unchanged" a measurement instead of a claim.

Everything here is offline and dependency-free: PNG and ICO headers are read with `struct`, and
SVGs are parsed with `xml.etree`. The one check that needs CairoSVG is marked and skips with a
reason, because CairoSVG is an optional build-time pin and not a Docket dependency.
"""

from __future__ import annotations

import re
import struct
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

import pytest

REPO = Path(__file__).resolve().parents[1]
ASSETS = REPO / "assets"

#: The approved palette, from `assets/BRAND.md` and the kit's `GUIDELINES.md`. Written out here
#: rather than imported, because the point of the test is that the repository carries *these*
#: values, and a constant read from the file under test could not disagree with it.
COBALT = "#2457f5"
COBALT_BRIGHT = "#7fa6ff"
CHARCOAL = "#17212b"
IVORY = "#f7f5ef"
SLATE = "#5f7082"
SAND = "#e7e0d3"
WHITE = "#ffffff"
APPROVED = {COBALT, COBALT_BRIGHT, CHARCOAL, IVORY, SLATE, SAND, WHITE}

#: The retired mark: indigo body, teal tick, pale-indigo record lines, near-black text. These are
#: the colours of the logo this one replaced, and the palette names `mkdocs.yml` used to select.
RETIRED = {"#4f46e5", "#2dd4bf", "#eef2ff", "#0f172a", "#4F46E5", "#2DD4BF"}

#: Files that are allowed to mention a retired colour, and why. `CHANGELOG.md` is a record of a
#: release, not a promise about this tree, and this file quotes the values to name them.
RETIRED_ALLOWED = {"CHANGELOG.md", "tests/test_brand_assets.py"}


def _png_header(path: Path) -> tuple[int, int, int]:
    """`(width, height, colour_type)` from a PNG's IHDR, read with `struct`.

    Colour type 2 is truecolour with no alpha channel, which is what "deliberately opaque" means
    in the file itself: the kit ships the app icon, the social card and the touch icon with no
    alpha channel at all, and the lockups and the favicon with one.
    """
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    width, height, depth, colour_type = struct.unpack(">IIBB", data[16:26])
    return width, height, colour_type


def _ico_frames(path: Path) -> list[tuple[int, int, int]]:
    """`[(width, height, bits_per_pixel)]` for every frame in an `.ico`, from its directory."""
    data = path.read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert reserved == 0 and kind == 1, f"{path.name} is not an icon"
    frames = []
    for index in range(count):
        width, height, _colours, _reserved, _planes, bits, _size, _offset = struct.unpack(
            "<BBBBHHII", data[6 + 16 * index : 22 + 16 * index]
        )
        frames.append((width or 256, height or 256, bits))
    return frames


def _svg_root(path: Path) -> ET.Element:
    return ET.fromstring(path.read_text(encoding="utf-8"))


def test_every_asset_the_site_links_is_checked_in() -> None:
    """`theme.logo`, `theme.favicon`, `extra_css` and the head override name files that exist.

    A MkDocs setting that points at a missing file is not a build error: the theme emits the
    `<link>` and the browser 404s, so the site looks fine in the repository and ships a broken
    reference. This is the cheapest place to notice.
    """
    config = (REPO / "mkdocs.yml").read_text(encoding="utf-8")
    named = set(re.findall(r"^\s*(?:logo|favicon|custom_dir):\s*(\S+)", config, re.MULTILINE))
    named |= set(re.findall(r"^\s*-\s*(assets/\S+\.css)\s*$", config, re.MULTILINE))
    assert named, "mkdocs.yml names no logo, favicon, custom_dir or stylesheet"
    missing = sorted(name for name in named if not (REPO / name).exists())
    assert not missing, f"mkdocs.yml references assets that are not checked in: {missing}"

    for target in re.findall(r'(?:href|src)="(assets/[^"]+)"', (REPO / "overrides" / "main.html").read_text()):
        assert (REPO / target).exists(), f"overrides/main.html links a missing asset: {target}"


def test_the_theme_override_extends_the_base_template() -> None:
    """A file in `overrides/` that does not extend a base template *replaces* a page template.

    `main.html` is Material's own page template, and the home page and the 404 page (which extends
    `main.html`) both rendered as the bare fragment while the file was missing its `extends`. The
    build stayed green and reported 53 pages, because a page rendered from a fragment is still a
    page. So the property is asserted directly rather than inferred from a successful build.
    """
    override = (REPO / "overrides" / "main.html").read_text(encoding="utf-8")
    assert re.search(r"\{%-?\s*extends\s+[\"']base\.html[\"']", override), (
        "overrides/main.html must extend base.html; without it, it replaces the page template of the "
        "same name and the home page renders as this fragment alone"
    )
    assert "{% block extrahead %}" in override


def test_the_brand_stylesheet_is_the_last_one_material_emits() -> None:
    """`assets/brand.css` must load after the theme's palette stylesheet, or it changes nothing.

    Material emits `extra_css` after its own `palette.css`, which is the only reason this file
    works at all: it writes the brand values over the same custom properties at equal specificity,
    so document order decides. MkDocs does not promise that ordering, so it is asserted against
    the built `<head>` rather than assumed.
    """
    head = _built_head()
    if head is None:  # pragma: no cover - exercised only without mkdocs installed
        pytest.skip("mkdocs-material is not installed, so the built head cannot be read")
    styles = re.findall(r'<link rel="stylesheet" href="([^"]+)"', head)
    assert "assets/brand.css" in styles, f"assets/brand.css is not linked from the built head: {styles}"
    theme = [href for href in styles if "assets/stylesheets/" in href]
    assert styles.index("assets/brand.css") > max(styles.index(href) for href in theme), (
        f"assets/brand.css must come after the theme stylesheets, or it loses the cascade: {styles}"
    )


def _load_stager():
    """`tools/build_docs_site.py`, loaded from its path, so these guards ask it what it stages.

    Registered in `sys.modules` first: it defines no dataclass, but the loader that skips the
    registration is the same one that breaks the next file, so it is registered here too.
    """
    import importlib.util
    import sys as _sys

    name = "build_docs_site_under_brand_test"
    spec = importlib.util.spec_from_file_location(name, REPO / "tools" / "build_docs_site.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    _sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _staged_build(tmp: Path) -> Path:
    """A scratch copy of the repository, staged and built, returned as its site directory.

    Staging in a copy rather than in the repository is the difference between a guard and a
    side effect: `mkdocs.yml` points `docs_dir` at `.mkdocs-site`, so building in the working tree
    would both require a previous build to have happened and leave one behind. Doing it in a copy
    means the guard runs on a clean checkout and cleans up after itself.

    A build that *fails* is not the same as a build that could not be attempted. `mkdocs` missing is
    a toolchain the contributor does not have, and the caller skips; `mkdocs` present and aborting is
    a defect, and a guard that skipped there would be a check that could not run looking like a
    check that passed. That distinction was measured rather than assumed: renaming `extra_css` in
    `mkdocs.yml` makes `mkdocs build` abort with a configuration error, and the first version of
    this helper turned that into a skip, which is how the mutation for
    `test_the_brand_stylesheet_is_the_last_one_material_emits` went undetected.
    """
    import shutil
    import subprocess

    scratch = tmp / "docket"
    shutil.copytree(
        REPO,
        scratch,
        ignore=shutil.ignore_patterns(
            ".git",
            "site",
            ".mkdocs-site",
            "dist",
            "*.egg-info",
            "__pycache__",
            ".venv",
            ".docket",
            ".pytest_cache",
            ".ruff_cache",
            "node_modules",
        ),
    )
    stager = _load_stager()
    stager.STAGE = scratch / ".mkdocs-site"
    stager.stage(stager.STAGE)

    site = tmp / "site"
    result = subprocess.run(
        ["mkdocs", "build", "--strict", "--site-dir", str(site)],
        cwd=scratch,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        "the documentation site does not build, so its brand wiring cannot be verified: "
        + (result.stdout + result.stderr)[-600:]
    )
    return site


def _built_head() -> str | None:
    """The `<head>` of the built site, or `None` when the toolchain is not installed."""
    import shutil
    import tempfile

    if shutil.which("mkdocs") is None:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        site = _staged_build(Path(tmp))
        index = site / "index.html"
        assert index.exists(), "mkdocs reported success but wrote no index.html"
        return index.read_text(encoding="utf-8").split("</head>", 1)[0]


def test_no_retired_brand_colour_survives_anywhere_in_the_tree() -> None:
    """The old mark's hexes appear in no tracked file except the two that are allowed to name them.

    `mkdocs.yml`'s `primary: indigo` / `accent: teal` was the retired scheme by name, and the
    palette names resolve to colours, so "the palette was changed" is not the same claim as "the
    retired colours are gone". Both are asserted: the palette is checked for the names, and every
    tracked file is checked for the values.
    """
    config = (REPO / "mkdocs.yml").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(?:primary|accent):\s*(?:indigo|teal)\s*$", config, re.MULTILINE), (
        "mkdocs.yml still selects the retired indigo/teal palette by name"
    )

    offenders: dict[str, list[int]] = {}
    for path in sorted(REPO.rglob("*")):
        if not path.is_file() or ".git/" in path.as_posix():
            continue
        relative = path.relative_to(REPO).as_posix()
        if relative in RETIRED_ALLOWED or any(
            part in {".mkdocs-site", "site", "dist", "build", "node_modules", "__pycache__", ".venv", ".docket"}
            for part in path.relative_to(REPO).parts
        ):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        hits = [number for number, line in enumerate(text.splitlines(), start=1) if any(c in line for c in RETIRED)]
        if hits:
            offenders[relative] = hits
    assert not offenders, f"retired brand colours survive in: {offenders}"


def test_the_brand_stylesheet_declares_only_approved_colours() -> None:
    """Every hex in `assets/brand.css` is from the approved palette.

    A theme stylesheet is where an invented colour would hide, because the palette it overrides
    is a set of custom properties and nothing checks what they were set to. The kit's `GUIDELINES.md`
    is explicit that slate and sand support the surrounding material and must not become a fourth
    colour inside the mark, and that bright cobalt is a dark-surface derivative rather than a hue
    of its own; a stray hex is the visible symptom of either mistake.
    """
    text = (ASSETS / "brand.css").read_text(encoding="utf-8")
    declared = {match.lower() for match in re.findall(r"#[0-9a-fA-F]{3,8}\b", text)}
    assert declared, "assets/brand.css declares no colour at all"
    unexpected = sorted(declared - APPROVED)
    assert not unexpected, f"assets/brand.css carries colours outside the approved palette: {unexpected}"
    for required in (COBALT, COBALT_BRIGHT, CHARCOAL, IVORY):
        assert required in declared, f"assets/brand.css does not use the approved {required}"


def test_no_brand_svg_sets_its_wordmark_as_live_text() -> None:
    """The wordmark is outlined paths, never a `<text>` element.

    The kit's guidelines say so twice: "Do not replace the logo with live text", and "Keep the
    outlined paths intact". A `<text>` element would also make the mark depend on a font being
    installed, which the kit explicitly says is not needed to display any SVG.
    """
    live = []
    for path in sorted(ASSETS.glob("*.svg")):
        if _svg_root(path).find(".//{http://www.w3.org/2000/svg}text") is not None:
            live.append(path.name)
    assert not live, f"brand SVGs set their lettering as live text: {live}"


def test_every_brand_svg_is_well_formed_and_titles_itself() -> None:
    """Well-formed XML, a `<title>` for the accessibility tree, and an `aria-label` or `role`.

    The site's own accessibility checker refuses an `<img>` with no `alt`, and an SVG used as a
    theme logo is an `<img>`; a `<title>` inside the file is what a screen reader falls back to.
    """
    for path in sorted(ASSETS.glob("*.svg")):
        root = _svg_root(path)
        title = root.find("{http://www.w3.org/2000/svg}title")
        assert title is not None and (title.text or "").strip(), f"{path.name} has no <title>"
        assert root.get("aria-label") or root.get("role"), f"{path.name} has neither aria-label nor role"


def test_the_rasters_are_the_shapes_their_use_requires() -> None:
    """Dimensions and alpha, read from the file headers, match what each host demands.

    * `icon.png` is the GitHub avatar: 512x512, opaque. GitHub squares off a transparent avatar
      against its own background, which would put a white box behind a full-bleed cobalt tile.
    * `social-preview.png` is the GitHub card: 1280x640, opaque, and 1280x640 is the size GitHub
      crops from.
    * `apple-touch-icon.png` is the iOS home-screen icon: 180x180, opaque, because iOS does not
      composite transparency and paints the backdrop itself.
    * `favicon.ico` carries 16, 32 and 48 at 32 bits, with an alpha channel, so the legacy
      fallback is sharp at all three sizes the browser asks for.
    """
    width, height, colour_type = _png_header(ASSETS / "icon.png")
    assert (width, height, colour_type) == (512, 512, 2), "assets/icon.png must be an opaque 512x512 PNG"
    width, height, colour_type = _png_header(ASSETS / "social-preview.png")
    assert (width, height, colour_type) == (1280, 640, 2), "assets/social-preview.png must be an opaque 1280x640 PNG"
    width, height, colour_type = _png_header(ASSETS / "apple-touch-icon.png")
    assert (width, height, colour_type) == (180, 180, 2), "assets/apple-touch-icon.png must be an opaque 180x180 PNG"
    frames = _ico_frames(ASSETS / "favicon.ico")
    assert sorted(size for size, _height, _bits in frames) == [16, 32, 48], (
        f"assets/favicon.ico must carry 16, 32 and 48: {frames}"
    )
    assert all(bits == 32 for _size, _height, bits in frames), (
        f"assets/favicon.ico frames must have an alpha channel: {frames}"
    )


def test_the_site_stager_carries_the_asset_kinds_the_site_links() -> None:
    """The stager's keep-list covers every asset kind a built page references.

    `assets/favicon.ico` and `assets/brand.css` were both absent from that list before the kit was
    adopted, because no `.ico` or `.css` file existed in the tree. A stager that drops them ships a
    site with two broken asset references and a green build, which is the failure this pins.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp)
        _load_stager().stage(staged)
        for required in ("favicon.ico", "brand.css", "favicon.svg", "logo.svg", "logo-horizontal.svg"):
            assert (staged / "assets" / required).exists(), f"the stager drops assets/{required}"
        assert not (staged / "assets" / "BRAND.md").exists(), (
            "assets/BRAND.md is the in-repository alias map for the artwork, not a site page"
        )


def test_every_asset_link_in_the_built_site_resolves_on_every_page() -> None:
    """A `<link>` or `<img>` in a built page points at a file that is there, on that page.

    mkdocs emits `theme.favicon` and `extra_css` through its `| url` filter, which rewrites a path
    to be relative to the page being rendered. Anything written into a theme override as a literal
    string keeps that string on every page, so it is right at the root and a 404 one directory down.
    That is exactly what happened here: `overrides/main.html` first linked
    `assets/favicon.ico` and `assets/apple-touch-icon.png` literally, and `docs/quickstart/` linked
    both while resolving them against `/docket/docs/quickstart/`. Chromium is what found it — the
    page still built, the site still reported 53 clean pages, and only a request that resolved to
    nothing showed the difference. This test is the offline version of that measurement: it resolves
    every asset reference in every built page against that page's own directory.

    Skipped when mkdocs is absent, for the same reason as the head-order check.
    """
    import shutil
    import tempfile
    import urllib.parse

    if shutil.which("mkdocs") is None:
        pytest.skip("mkdocs-material is not installed, so the built site cannot be read")

    with tempfile.TemporaryDirectory() as tmp:
        site = _staged_build(Path(tmp))
        # mkdocs writes site-root-absolute URLs (`/docket/assets/…`) wherever `site_url` gives it a
        # base path, which on this site is the 404 page and the stylesheets it links. Those resolve
        # against the site root, not against the page, so the prefix has to come off first.
        site_url = re.search(r"^site_url:\s*(\S+)", (REPO / "mkdocs.yml").read_text(encoding="utf-8"), re.MULTILINE)
        # `rstrip("/")` matters: `site_url` ends in a slash, and comparing `/docket/assets/x`
        # against `/docket//` matches nothing, which silently turns every site-root-absolute URL on
        # the 404 page into a reported break.
        prefix = urllib.parse.urlsplit(site_url.group(1)).path.rstrip("/") if site_url else ""

        #: Asset references only: the `<link>` relations that fetch a file, `<img>` sources and
        #: `<script>` sources. Navigation links are deliberately not scanned — mkdocs' `--strict`
        #: already refuses a page link it cannot resolve, and a scan that also read them would fail
        #: on every `use_directory_urls` page in the tree, which is noise rather than signal.
        asset_relations = {"icon", "apple-touch-icon", "stylesheet", "manifest", "preload", "mask-icon"}
        broken: dict[str, list[str]] = {}
        checked = 0
        for page in sorted(site.rglob("*.html")):
            relative = page.relative_to(site).as_posix()
            references: list[str] = []
            for tag in re.findall(r"<(?:link|img|script)\b[^>]*>", page.read_text(encoding="utf-8")):
                attributes = dict(re.findall(r'([\w-]+)="([^"]*)"', tag))
                if tag.startswith("<link") and attributes.get("rel") not in asset_relations:
                    continue
                references.append(attributes.get("href") or attributes.get("src") or "")
            for href in references:
                parsed = urllib.parse.urlsplit(href)
                if parsed.scheme or parsed.netloc or href.startswith(("data:", "#", "mailto:", "javascript:")):
                    continue
                target = urllib.parse.unquote(parsed.path)
                # `..`, `.` and `../..` are breadcrumb navigation, not asset references.
                if not target or PurePosixPath(target).name in {"", ".", ".."}:
                    continue
                if target.startswith("/"):
                    if prefix and target.startswith(prefix + "/"):
                        target = target[len(prefix) :]
                    resolved = (site / target.lstrip("/")).resolve()
                else:
                    resolved = (page.parent / target).resolve()
                if not resolved.is_file():
                    broken.setdefault(relative, []).append(href)
                else:
                    checked += 1
        # Every page links at least the micro favicon, the two theme stylesheets, the brand
        # stylesheet, the `.ico` fallback, the Apple touch icon and the theme bundle, so a scan
        # that finds fewer than four references per page has stopped reading them.
        pages = len(list(site.rglob("*.html")))
        assert checked >= 4 * pages, f"only {checked} asset references across {pages} pages; the scan is not working"
        assert not broken, f"built pages link assets that do not exist, relative to the page: {broken}"


def test_the_brand_raster_generator_reproduces_the_checked_in_pngs() -> None:
    """`assets/make-assets.py` writes the rasters that are checked in, byte for byte.

    The kit ships this generator so the PNGs and the SVG sources cannot drift, and its own handoff
    note records that both were regenerated in a temporary copy and matched exactly. Reproducing
    that here is what makes "the checked-in PNG is the rendered SVG" a measurement rather than a
    claim — and it is the guard on the one manual change this adoption made to a kit file, which
    was reformatting its imports to satisfy this repository's `ruff` gate.

    Skipped, with the reason printed, when CairoSVG is absent: it is an optional build-time pin in
    `assets/requirements-brand.txt`, not a Docket dependency, and a contributor without it should
    still be able to run the suite.
    """
    pytest.importorskip("cairosvg", reason="CairoSVG is the optional brand build dependency")
    import shutil
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        for source in ASSETS.iterdir():
            if source.is_file():
                shutil.copy2(source, scratch / source.name)
        import subprocess

        result = subprocess.run(
            ["python3", str(scratch / "make-assets.py")],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, f"assets/make-assets.py failed: {result.stdout}{result.stderr}"
        for name in ("icon.png", "social-preview.png"):
            regenerated = (scratch / name).read_bytes()
            checked_in = (ASSETS / name).read_bytes()
            assert regenerated == checked_in, (
                f"assets/{name} is not what assets/make-assets.py renders from its SVG source"
            )
