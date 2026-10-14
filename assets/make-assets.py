#!/usr/bin/env python3
"""Regenerate the raster brand assets from the same geometry as `logo.svg`.

The checked-in PNGs (`icon.png` 512x512, `social-preview.png` 1280x640) are produced
here rather than by exporting the SVG, because SVG rasterisers disagree about stroke
colours and clipping (ImageMagick's built-in renderer silently drops the check mark,
which is the one element that carries the meaning). Drawing the shapes directly means
the output is the same everywhere, and the geometry stays reviewable as code.

    python3 assets/make-assets.py [font-directory]

Requires Pillow. Default font directory is DejaVu (present on most Linux systems).
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
FONT_DIR = Path(sys.argv[1] if len(sys.argv) > 1 else "/usr/share/fonts/truetype/dejavu")

INDIGO = (79, 70, 229, 255)
TEAL = (45, 212, 191, 255)
CARD = (238, 242, 255, 255)
SLATE = (15, 23, 42, 255)
MUTED = (148, 163, 184, 255)
WHITE = (255, 255, 255, 255)


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_DIR / name), size)


def rounded_points(x: float, y: float, size: float, radius: float, *, steps: int = 12) -> list[tuple[float, float]]:
    """The receipt outline in the 64-unit coordinate system of `logo.svg`.

    Clockwise from the top-left corner, with real arcs on the three rounded corners
    and a sawtooth bottom edge (the torn receipt).
    """
    unit = size / 64.0

    def p(ux: float, uy: float) -> tuple[float, float]:
        return (x + ux * unit, y + uy * unit)

    points: list[tuple[float, float]] = []

    # top edge, left to right
    points += [p(12, 6), p(52, 6)]
    # top-right corner
    for step in range(1, steps + 1):
        angle = -math.pi / 2 + (math.pi / 2) * (step / steps)
        points.append(p(52 + 6 * math.cos(angle), 12 + 6 * math.sin(angle)))
    # right edge down to where the tear starts
    points.append(p(58, 45))
    # sawtooth tear, right to left: seven teeth of ~7 units
    for tooth in range(7):
        right = 58 - tooth * 7
        points.append(p(right - 3.5, 41.5))
        points.append(p(right - 7, 45))
    # bottom-left corner: centre (12, 39), sweeping from (12, 45) to (6, 39).
    # Getting this arc's centre wrong is what produces the notch that a reviewer
    # notices at 1280px and the author does not in the code.
    for step in range(1, steps + 1):
        angle = math.pi / 2 + (math.pi / 2) * (step / steps)
        points.append(p(12 + 6 * math.cos(angle), 39 + 6 * math.sin(angle)))
    # left edge up to the top-left corner: centre (12, 12), from (6, 12) to (12, 6)
    for step in range(1, steps + 1):
        angle = math.pi + (math.pi / 2) * (step / steps)
        points.append(p(12 + 6 * math.cos(angle), 12 + 6 * math.sin(angle)))
    return points


def draw_mark(draw: ImageDraw.ImageDraw, x: int, y: int, size: int) -> None:
    unit = size / 64.0

    draw.polygon(rounded_points(x, y, size, 0), fill=INDIGO)

    for index, (left, right) in enumerate(((15, 35), (15, 49), (15, 41))):
        top = y + (17 + index * 8) * unit
        draw.rounded_rectangle(
            [x + left * unit, top, x + right * unit, top + 3.4 * unit],
            radius=1.7 * unit,
            fill=CARD,
        )

    draw.line(
        [
            (x + 40 * unit, y + 46 * unit),
            (x + 45 * unit, y + 51 * unit),
            (x + 56 * unit, y + 39 * unit),
        ],
        fill=TEAL,
        width=max(2, round(6 * unit)),
        joint="curve",
    )


def fit(text: str, path: Path, start: int, limit: int) -> ImageFont.FreeTypeFont:
    """Largest font size at or below `start` whose rendered width fits `limit`."""
    size = start
    while size > 8:
        candidate = font(path.name, size)
        if candidate.getlength(text) <= limit:
            return candidate
        size -= 1
    return font(path.name, 8)


def icon() -> None:
    image = Image.new("RGBA", (512, 512), (255, 255, 255, 0))
    draw_mark(ImageDraw.Draw(image), 64, 64, 384)
    image.save(HERE / "icon.png")
    print("wrote icon.png (512x512)")


def social_preview() -> None:
    width, height = 1280, 640
    image = Image.new("RGBA", (width, height), SLATE)
    draw = ImageDraw.Draw(image)

    draw_mark(draw, 88, 168, 256)

    left = 416
    limit = width - left - 72
    title_text = "Docket"
    tagline_text = "Every AI agent run gets a docket number."
    detail_text = "A hard cap before it overspends. A receipt anybody can verify offline."
    detail_2 = "No account, no server, no telemetry."

    title = fit(title_text, FONT_DIR / "DejaVuSans-Bold.ttf", 128, limit)
    tagline = fit(tagline_text, FONT_DIR / "DejaVuSans.ttf", 46, limit)
    detail = fit(detail_text, FONT_DIR / "DejaVuSansMono.ttf", 30, limit)
    detail_small = fit(detail_2, FONT_DIR / "DejaVuSansMono.ttf", 30, limit)

    draw.text((left, 150), title_text, font=title, fill=WHITE)
    draw.text((left + 6, 300), tagline_text, font=tagline, fill=MUTED)
    draw.text((left + 6, 386), detail_text, font=detail, fill=MUTED)
    draw.text((left + 6, 424), detail_2, font=detail_small, fill=MUTED)
    draw.text((left + 6, 510), "$ docket demo", font=detail, fill=TEAL)

    image.convert("RGB").save(HERE / "social-preview.png", quality=95)
    print("wrote social-preview.png (1280x640)")


if __name__ == "__main__":
    icon()
    social_preview()
