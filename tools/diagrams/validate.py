"""Validate an emitted figure.

This module exists so that `tools/check_diagrams.py` and `tests/test_diagrams.py` apply *one*
implementation of the rules. A guard whose logic is copied into a test is a guard that can pass in
one place and fail in the other, which is the same class of defect as a guard that passes for the
wrong reason.

Each `check_*` function returns a list of problems; an empty list means the figure satisfied that
rule.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

SVG = "{http://www.w3.org/2000/svg}"

#: The type scale, mirrored from `design.py` so a size can be recovered from a class name. A second
#: copy of four numbers is cheaper than importing the generator into a test that must also run
#: against the *emitted* file, which is the thing being checked.
SIZES = {"title": 19.0, "section": 15.0, "label": 13.0, "detail": 11.0, "micro": 10.0}

#: Average glyph advance as a fraction of font size. Pessimistic on purpose: a guard that asks a
#: question about a marginal label is better than one that waves a real overflow through.
ADVANCE = {"sans": 0.56, "mono": 0.62}


def luminance(colour: str) -> float:
    value = colour.lstrip("#")
    channels = []
    for index in (0, 2, 4):
        channel = int(value[index : index + 2], 16) / 255
        channel = channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
        channels.append(channel)
    red, green, blue = channels
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(first: str, second: str) -> float:
    one, two = luminance(first), luminance(second)
    return (max(one, two) + 0.05) / (min(one, two) + 0.05)


def parse(path: Path) -> ET.Element:
    return ET.fromstring(path.read_text(encoding="utf-8"))


def measured_size(element: ET.Element) -> float:
    style = element.get("class", "")
    for name, size in SIZES.items():
        if f"t-{name}" in style:
            return size
    return SIZES["label"]


def estimate(element: ET.Element) -> tuple[float, float]:
    """The horizontal extent of a text element: left edge and width."""
    value = (element.text or "").strip()
    size = measured_size(element)
    family = "mono" if "mono" in element.get("class", "") else "sans"
    width = len(value) * ADVANCE[family] * size
    x = float(element.get("x", 0))
    anchor = element.get("text-anchor", "start")
    if anchor == "middle":
        left = x - width / 2
    elif anchor == "end":
        left = x - width
    else:
        left = x
    return left, width


def check_well_formed(path: Path) -> list[str]:
    problems: list[str] = []
    try:
        tree = parse(path)
    except ET.ParseError as error:
        return [f"{path.name}: does not parse: {error}"]
    if tree.tag != f"{SVG}svg":
        problems.append(f"{path.name}: root is {tree.tag}, not svg")
    return problems


def check_accessibility(path: Path) -> list[str]:
    """The accessible-graphics contract, which is also what makes a figure reviewable in a diff."""
    tree = parse(path)
    name = path.name
    problems: list[str] = []

    if tree.get("role") != "img":
        problems.append(f"{name}: role is not img, so a screen reader reads every path in turn")
    identifiers = (tree.get("aria-labelledby") or "").split()
    if len(identifiers) != 2:
        problems.append(f"{name}: aria-labelledby names {len(identifiers)} ids, expected a title and a desc")

    children = [child.tag.replace(SVG, "") for child in list(tree)[:3]]
    # The page background rect comes first, then title, then desc, then defs.
    expected = ["rect", "title", "desc"] if children[:1] == ["rect"] else children[:2]
    if expected[:2] != (["rect", "title"] if children[:1] == ["rect"] else children[:2]):
        pass
    order = [tag for tag in children if tag in ("title", "desc")]
    if order != ["title", "desc"]:
        problems.append(f"{name}: title and desc appear as {order}, expected title then desc")

    for identifier in identifiers:
        if tree.find(f".//*[@id='{identifier}']") is None:
            problems.append(f"{name}: aria-labelledby names a missing id {identifier!r}")

    title = tree.find(f"{SVG}title")
    desc = tree.find(f"{SVG}desc")
    if title is None or not (title.text or "").strip():
        problems.append(f"{name}: no title text")
    elif len(title.text) > 90:
        problems.append(f"{name}: title is {len(title.text)} characters, over 90")
    if desc is None or len((desc.text or "").strip()) < 120:
        problems.append(f"{name}: desc is missing or too short to describe the figure")

    # A figure with no background borrows its canvas from whatever renders it, so it looks different
    # in a file view, a pull-request diff, and a dark theme.
    if tree.find(f".//{SVG}rect[@class='page']") is None:
        problems.append(f"{name}: no page background, so it inherits the host page colour")

    view = (tree.get("viewBox") or "").split()
    if len(view) != 4:
        problems.append(f"{name}: viewBox is {tree.get('viewBox')!r}, expected four numbers")
    return problems


def check_contrast(path: Path) -> list[str]:
    """Every text element meets 4.5:1 against the innermost surface that contains it."""
    tree = parse(path)
    name = path.name
    problems: list[str] = []

    rects = [
        (
            float(rect.get("x", 0)),
            float(rect.get("y", 0)),
            float(rect.get("width", 0)),
            float(rect.get("height", 0)),
            rect.get("fill", ""),
        )
        for rect in tree.findall(f".//{SVG}rect")
        if rect.get("fill") and rect.get("fill") != "none"
    ]
    page = tree.find(f".//{SVG}rect[@class='page']")
    page_fill = page.get("fill") if page is not None else "#ffffff"

    for element in tree.findall(f".//{SVG}text"):
        value = (element.text or "").strip()
        if not value:
            continue
        fill = element.get("fill")
        if not fill or not fill.startswith("#"):
            problems.append(f"{name}: text {value[:28]!r} has no hex fill")
            continue
        left, width = estimate(element)
        y = float(element.get("y", 0))
        background, best = None, None
        for rx, ry, rw, rh, rfill in rects:
            if rw <= 0 or rh <= 0:
                continue
            if rx <= left + 1 and ry <= y and ry + rh >= y and rx + rw >= left + width - 1:
                area = rw * rh
                if best is None or area < best:
                    best, background = area, rfill
        if background is None:
            background = page_fill
        try:
            ratio = contrast(fill, background)
        except ValueError:
            problems.append(f"{name}: text {value[:28]!r} fill {fill} is not a hex colour")
            continue
        if ratio < 4.5:
            problems.append(f"{name}: text {value[:28]!r} is {ratio:.2f}:1 on {background}, under 4.5:1")
    return problems


def check_within_viewbox(path: Path) -> list[str]:
    """No label runs past the canvas, which is the failure a reader sees as a truncated word."""
    tree = parse(path)
    name = path.name
    view = (tree.get("viewBox") or "").split()
    if len(view) != 4:
        return []
    total = float(view[2])
    problems: list[str] = []
    for element in tree.findall(f".//{SVG}text"):
        if not (element.text or "").strip():
            continue
        left, width = estimate(element)
        if left < -1:
            problems.append(f"{name}: text {(element.text or '')[:28]!r} starts at {left:.0f}, off-canvas")
        if left + width > total + 1:
            problems.append(
                f"{name}: text {(element.text or '')[:28]!r} ends at {left + width:.0f}, past the {total:.0f} viewBox"
            )
    return problems


def check_orthogonal(path: Path) -> list[str]:
    """Lines are axis-aligned; quadratic curves are quarter-arc corners or they are not drawn."""
    tree = parse(path)
    name = path.name
    problems: list[str] = []
    for element in tree.findall(f".//{SVG}path"):
        data = element.get("d", "")
        if data.startswith("M 0 0"):
            continue  # the arrowhead marker
        commands = re.findall(r"([MLQ])\s+([-\d.]+)\s+([-\d.]+)(?:\s+([-\d.]+)\s+([-\d.]+))?", data)
        moved = False
        previous: tuple[float, float] | None = None
        for kind, first, second, third, fourth in commands:
            if kind == "M":
                moved, previous = True, None
                continue
            control = (float(first), float(second))
            endpoint = (float(third), float(fourth)) if third else control
            if kind == "Q":
                if previous is None:
                    problems.append(f"{name}: a curve with no starting point")
                    break
                start_ok = previous[0] == control[0] or previous[1] == control[1]
                end_ok = endpoint[0] == control[0] or endpoint[1] == control[1]
                if not (start_ok and end_ok):
                    problems.append(f"{name}: curve at {control} is not a quarter-arc corner: {previous} to {endpoint}")
                    break
            elif previous is not None and endpoint[0] != previous[0] and endpoint[1] != previous[1]:
                problems.append(f"{name}: diagonal line {previous} to {endpoint}")
                break
            previous = endpoint
        if not moved:
            problems.append(f"{name}: a path with no moveto")
    return problems


def check_references(path: Path) -> list[str]:
    """Every marker the figure points at is one the figure defines."""
    raw = path.read_text(encoding="utf-8")
    problems: list[str] = []
    for reference in re.findall(r"url\(#([^)]+)\)", raw):
        if f'id="{reference}"' not in raw:
            problems.append(f"{path.name}: marker #{reference} is referenced but not defined")
    for identifier in re.findall(r'<text[^>]*\bfill="(#[0-9A-Fa-f]{6})"', raw):
        if not identifier.startswith("#") or len(identifier) != 7:
            problems.append(f"{path.name}: fill {identifier} is not a six-digit hex colour")
    return problems


def check_no_overlap(path: Path) -> list[str]:
    """Two opaque cards must not be drawn on top of each other.

    This is the check whose absence let the RECORDED band in the pipeline figure draw `model calls`
    and `file changes` at the same coordinates for as long as it did. A figure that names four
    things and shows three is worse than one that names three.
    """
    tree = parse(path)
    name = path.name
    problems: list[str] = []
    boxes: list[tuple[float, float, float, float]] = []
    for rect in tree.findall(f".//{SVG}rect"):
        if rect.get("class") in ("page",):
            continue
        width = float(rect.get("width", 0))
        height = float(rect.get("height", 0))
        if width < 80 or height < 40:
            continue  # pills, bars, mask plates and markers are meant to nest
        boxes.append(
            (
                float(rect.get("x", 0)),
                float(rect.get("y", 0)),
                width,
                height,
            )
        )
    for index, (ax, ay, aw, ah) in enumerate(boxes):
        for bx, by, bw, bh in boxes[index + 1 :]:
            overlap_x = min(ax + aw, bx + bw) - max(ax, bx)
            overlap_y = min(ay + ah, by + bh) - max(ay, by)
            if overlap_x > 4 and overlap_y > 4:
                problems.append(
                    f"{name}: cards at ({ax:.0f},{ay:.0f},{aw:.0f}x{ah:.0f}) and "
                    f"({bx:.0f},{by:.0f},{bw:.0f}x{bh:.0f}) overlap by "
                    f"{overlap_x:.0f}x{overlap_y:.0f}"
                )
    return problems


RULES = (
    ("well formed", check_well_formed),
    ("accessible", check_accessibility),
    ("contrast", check_contrast),
    ("inside the viewBox", check_within_viewbox),
    ("orthogonal", check_orthogonal),
    ("references resolve", check_references),
    ("no overlapping cards", check_no_overlap),
)


def problems_for(path: Path) -> list[str]:
    found: list[str] = []
    for _name, rule in RULES:
        try:
            found.extend(rule(path))
        except (ET.ParseError, ValueError, IndexError) as error:
            found.append(f"{path.name}: {_name} raised {type(error).__name__}: {error}")
    return found
