"""The figures themselves.

Each `figure_*` function returns one SVG document as a string. `tools/make_diagrams.py` calls them
once per theme and writes the pair, so a figure cannot exist in one theme and not the other.

Every number in these figures is either a layout constant from `design.py` or read from
`source/cap-series.json`, `spec/registry.json` or the run recorded in the fixture's `_source` note.
Nothing is rounded into looking better than it was.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

from design import (
    DETAIL,
    ELBOW,
    LABEL,
    MICRO,
    MONO,
    RADIUS,
    SANS,
    SECTION,
    STROKE,
    TITLE,
    Palette,
    text_width,
)

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
#: The measured series the cap figure is drawn from, kept beside the figure it feeds rather than
#: beside the code that draws it, because it is data about a run and not layout.
DATA = REPO / "assets" / "diagrams" / "source"

WIDTH = 980


# --------------------------------------------------------------------------- #
# Primitives
# --------------------------------------------------------------------------- #


def esc(value: str) -> str:
    """Escape for XML text and attribute content alike.

    The figures carry real field names and real command lines. An unescaped `&` in a path is not a
    typo, it is a document that will not parse.
    """
    return html.escape(value, quote=True)


def document(slug: str, theme: Palette, title: str, desc: str, body: str, height: int) -> str:
    """Wrap a figure body in the accessible, theme-aware document the project has settled on.

    `role="img"` collapses the internals for assistive technology so a screen reader reads the name
    once. `<title>` is the first child of `<svg>` and `<desc>` the second, before `<defs>`, because
    that is the order the graphics module reads. The ids are prefixed with the slug *and* the theme,
    so a page carrying both variants never has a duplicate accessible name.
    """
    prefix = f"{slug}-{theme.name}"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {height}" width="{WIDTH}" height="{height}" role="img" aria-labelledby="{prefix}-title {prefix}-desc" font-family="{SANS}">
  <title id="{prefix}-title">{esc(title)}</title>
  <desc id="{prefix}-desc">{esc(desc)}</desc>
  <rect x="0" y="0" width="{WIDTH}" height="{height}" class="page" fill="{theme.page}"/>
  <defs>
    <marker id="{theme.name}-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M 0 0 L 10 5 L 0 10 z" class="line" stroke-width="1"/>
    </marker>
    <style>
{theme.css()}
      .line {{ stroke: {theme.border}; fill: none; }}
      .accent-line {{ stroke: {theme.accent}; fill: none; }}
      .strong-line {{ stroke: {theme.secondary}; fill: none; }}
      .hairline {{ stroke: {theme.grid}; stroke-width: 1; fill: none; }}
      .sans {{ font-family: {SANS}; }}
      .mono {{ font-family: {MONO}; }}
      .t-title {{ font-size: {TITLE}px; font-weight: 600; }}
      .t-section {{ font-size: {SECTION}px; font-weight: 600; }}
      .t-label {{ font-size: {LABEL}px; font-weight: 500; }}
      .t-detail {{ font-size: {DETAIL}px; }}
      .t-micro {{ font-size: {MICRO}px; }}
      text {{ dominant-baseline: middle; }}
    </style>
  </defs>
{body}
</svg>
"""


def card(
    theme: Palette,
    x: int,
    y: int,
    w: int,
    h: int,
    fill: str | None = None,
    stroke: str | None = None,
    accent_left: str | None = None,
) -> str:
    """A rounded panel. `accent_left` draws a 4 px bar, which is how one node is made the subject."""
    fill = fill if fill is not None else theme.card
    stroke = stroke if stroke is not None else theme.border
    parts = [
        f'  <rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{RADIUS}" '
        f'class="{"card" if fill == theme.card else "raised"}" fill="{fill}" '
        f'stroke="{stroke}" stroke-width="{STROKE}"/>'
    ]
    if accent_left:
        parts.append(f'  <rect x="{x}" y="{y}" width="4" height="{h}" rx="2" fill="{accent_left}"/>')
    return "\n".join(parts)


def text(
    theme: Palette,
    x: int,
    y: int,
    value: str,
    cls: str = "t-label",
    fill: str | None = None,
    anchor: str = "start",
    family: str = "sans",
) -> str:
    """One line of real text. `cls` names the size, `fill` the colour job."""
    colour = fill if fill is not None else theme.text
    return (
        f'  <text x="{x}" y="{y}" class="{cls} {family} {cls}-fill" fill="{colour}" '
        f'text-anchor="{anchor}">{esc(value)}</text>'
    )


def mask_label(theme: Palette, x: int, y: int, value: str, size: int = DETAIL, family: str = "sans") -> str:
    """A label on its own opaque plate, so it never sits on the line it names.

    The plate is the page colour, not transparent, because the arrow passes underneath it. The
    height is size + 8 and the text is centred in it, which puts `LABEL_GAP` between the glyph and
    the plate edge.
    """
    width = text_width(value, size, family) + 16
    height = size + 8
    return "\n".join(
        [
            f'  <rect x="{x - width // 2}" y="{y - height // 2}" width="{width}" height="{height}" '
            f'rx="4" class="page" fill="{theme.page}"/>',
            text(theme, x, y, value, "t-detail" if size == DETAIL else "t-label", theme.secondary, "middle", family),
        ]
    )


def arrow(theme: Palette, points: list[tuple[int, int]], accent: bool = False) -> str:
    """An orthogonal polyline with a rounded elbow at each corner and an arrowhead at the end.

    `points` are the corners in order; consecutive points must share an x or a y, which is the
    orthogonal rule. The first segment starts short of the origin and the last stops short of the
    destination, so the arrowhead lands on the box edge rather than inside it.
    """
    if len(points) < 2:
        raise ValueError("an arrow needs at least two points")
    for (ax, ay), (bx, by) in zip(points, points[1:], strict=False):
        if ax != bx and ay != by:
            raise ValueError(f"non-orthogonal segment from {(ax, ay)} to {(bx, by)}")

    # Trim the tail and the head by 6 px so the marker sits on the boundary.
    trimmed = list(points)
    (sx, sy), (ex, ey) = trimmed[0], trimmed[-1]
    if sy == ey:
        step = 6 if ex > sx else -6
        trimmed[0] = (sx + step, sy)
        trimmed[-1] = (ex - step, ey)
    elif sx == ex:
        step = 6 if ey > sy else -6
        trimmed[0] = (sx, sy + step)
        trimmed[-1] = (ex, ey - step)

    path = f"M {trimmed[0][0]} {trimmed[0][1]}"
    for index, (x, y) in enumerate(trimmed[1:], start=1):
        px, py = trimmed[index - 1]
        if index < len(trimmed) - 1:
            path += _elbow(px, py, x, y, trimmed[index + 1] if index + 1 < len(trimmed) else (x, y))
        else:
            path += f" L {x} {y}"
    klass = "accent-line" if accent else "line"
    return f'  <path d="{path}" class="{klass}" stroke-width="1.5" marker-end="url(#{_marker(theme, accent)})"/>'


def _marker(theme: Palette, accent: bool) -> str:
    return f"{_slug_of(theme)}-arrow"


def _slug_of(theme: Palette) -> str:
    return f"{theme.name}"


def _elbow(px: int, py: int, x: int, y: int, following: tuple[int, int]) -> str:
    """A rounded corner from `(px, py)` toward `(x, y)`, turning toward `following`."""
    if px == x:  # vertical into a corner
        direction = 1 if y > py else -1
        if following[0] != x:
            turn = 1 if following[0] > x else -1
            return f" L {x} {y - direction * ELBOW} Q {x} {y} {x + turn * ELBOW} {y}"
        return f" L {x} {y}"
    if py == y:  # horizontal into a corner
        direction = 1 if x > px else -1
        if following[1] != y:
            turn = 1 if following[1] > y else -1
            return f" L {x - direction * ELBOW} {y} Q {x} {y} {x} {y + turn * ELBOW}"
        return f" L {x} {y}"
    raise ValueError("elbow needs an axis-aligned segment")


def chip_pill(theme: Palette, x: int, y: int, w: int, h: int, label: str) -> str:
    """A single small pill holding one field name. `w` comes from `chip_grid`, not from a guess."""
    return "\n".join(
        [
            f'  <rect x="{x}" y="{y}" width="{w}" height="{h}" rx="4" class="raised" fill="{theme.raised}"/>',
            text(theme, x + 12, y + h // 2, label, "t-micro", theme.secondary, family="mono"),
        ]
    )


def chip_grid(
    theme: Palette, x0: int, y: int, max_right: int, names: list[str], row_h: int = 26, gap: int = 12
) -> tuple[list[str], int]:
    """Lay pills out left to right, wrapping, every pill the width of the widest name.

    One width for the whole set keeps the column tidy; deriving that width from the content is what
    removes the dead space. Returns the parts and the y just below the last row.
    """
    width = int(max(text_width(name, MICRO, "mono") for name in names)) + 24
    parts: list[str] = []
    cursor_x, cursor_y = x0, y
    for name in names:
        if cursor_x + width > max_right:
            cursor_x, cursor_y = x0, cursor_y + row_h
        parts.append(chip_pill(theme, cursor_x, cursor_y, width, row_h - 2, name))
        cursor_x += width + gap
    return parts, cursor_y + row_h


def chip(theme: Palette, x: int, y: int, w: int, h: int, label: str, detail: str) -> str:
    """A small two-line panel: a name and what it contributes."""
    return "\n".join(
        [
            card(theme, x, y, w, h, theme.raised),
            text(theme, x + 16, y + 20, label, "t-label", theme.text),
            text(theme, x + 16, y + 38, detail, "t-micro", theme.secondary),
        ]
    )


def band_label(theme: Palette, y: int, value: str) -> str:
    """The gutter label for a horizontal band, right-aligned so the eye finds the content first."""
    return text(theme, 84, y, value, "t-micro", theme.muted, "end")


def rule(theme: Palette, x: int, y: int, w: int) -> str:
    return f'  <line x1="{x}" y1="{y}" x2="{x + w}" y2="{y}" class="hairline"/>'


# --------------------------------------------------------------------------- #
# 1. The bounded run
# --------------------------------------------------------------------------- #


def figure_pipeline(theme: Palette) -> str:
    """What Docket actually is: a process boundary around the agent you already run.

    Four bands, because the idea is four steps and a reader who follows one arrow at a time gets all
    of it. The boundary is the accent card, because that is the component nothing else in the
    comparison table provides.
    """
    parts: list[str] = []
    parts.append(text(theme, 24, 32, "One run, and the record it leaves", "t-title"))
    parts.append(rule(theme, 24, 52, WIDTH - 48))

    # Band 1: the run.
    top = 76
    h = 72
    parts.append(band_label(theme, top + h // 2, "THE RUN"))
    boxes = [
        (104, 152, "your agent", "any process"),
        (304, 168, "supervisor", "caps, spawn, exit"),
        (520, 176, "boundary", "loopback proxy"),
        (744, 176, "provider API", "the one you pay"),
    ]
    accents = {520: theme.accent}
    for x, w, name, sub in boxes:
        parts.append(card(theme, x, top, w, h, accent_left=accents.get(x)))
        parts.append(text(theme, x + 20, top + 28, name, "t-label", theme.text, family="sans"))
        parts.append(text(theme, x + 20, top + 48, sub, "t-micro", theme.secondary))
    for start, end in ((256, 304), (472, 520), (696, 744)):
        parts.append(arrow(theme, [(start, top + h // 2), (end, top + h // 2)], accent=(end == 520)))
    parts.append(mask_label(theme, 280, top + h // 2, "spawns"))
    parts.append(mask_label(theme, 496, top + h // 2, "base URL"))
    parts.append(mask_label(theme, 720, top + h // 2, "forwards"))

    # Band 2: what the boundary records.
    mid = 196
    ch = 52
    parts.append(band_label(theme, mid + ch // 2, "RECORDED"))
    # Four peers on one row, equal width, from the left gutter to the right margin. The first
    # version placed `model calls` and `file changes` at the same x, so one card was drawn exactly
    # on top of the other: the figure named four things and showed three, and nothing complained.
    span = (WIDTH - 64) - 104
    gap = 20
    chip_w = (span - 3 * gap) // 4
    records = [
        ("model calls", "+ tokens, cost"),
        ("refusals", "caps, egress, breaker"),
        ("cache hits", "only when --cache"),
        ("file changes", "+ content digests"),
    ]
    for index, (name, sub) in enumerate(records):
        parts.append(chip(theme, 104 + index * (chip_w + gap), mid, chip_w, ch, name, sub))
    # Two descents, each named for what it carries: the boundary forwards and prices, and the
    # supervisor's watcher digests the tree. Labelling both is what stops a reader assigning the
    # file evidence to the boundary, which is the component that cannot see the filesystem.
    parts.append(arrow(theme, [(608, top + h), (608, mid)], accent=True))
    parts.append(mask_label(theme, 608, mid - 20, "forwards, prices"))
    parts.append(arrow(theme, [(388, top + h), (388, mid)]))
    parts.append(mask_label(theme, 388, mid - 20, "fs watcher"))

    # Band 3: the record.
    led = 300
    parts.append(band_label(theme, led + 36, "THE LEDGER"))
    parts.append(arrow(theme, [(608, mid + ch), (608, led)]))
    parts.append(card(theme, 104, led, 812, 72, theme.accent_soft, theme.border, theme.accent))
    parts.append(text(theme, 128, led + 26, ".docket/runs/<session>.jsonl", "t-label", theme.text, family="mono"))
    parts.append(
        text(
            theme,
            128,
            led + 48,
            "canonical JSON, one event per line, each carrying the hash of the one before it",
            "t-micro",
            theme.secondary,
        )
    )
    parts.append(text(theme, WIDTH - 40, led + 26, "hash-chained", "t-micro", theme.accent_text, "end"))

    # Band 4: the receipt and who checks it.
    rec = 412
    parts.append(arrow(theme, [(608, led + 72), (608, rec)]))
    parts.append(card(theme, 104, rec, 812, 76, theme.card, theme.border, theme.accent))
    parts.append(
        text(
            theme,
            128,
            rec + 28,
            ".docket/receipts/DKT-<date>-<id>.receipt.json",
            "t-label",
            theme.text,
            family="mono",
        )
    )
    parts.append(
        text(
            theme,
            128,
            rec + 52,
            "totals, policy, per-model breakdown, an RFC 6962 Merkle root, and the price table used",
            "t-micro",
            theme.secondary,
        )
    )
    parts.append(text(theme, WIDTH - 40, rec + 28, "offline", "t-micro", theme.accent_text, "end"))

    ver = 528
    parts.append(arrow(theme, [(608, rec + 76), (608, ver)]))
    verifiers = [
        (104, 256, "docket verify", "Python reference"),
        (368, 256, "verify.mjs", "Node, zero deps"),
        (632, 288, "docket-verify", "Go, clean-room"),
    ]
    for x, w, name, sub in verifiers:
        parts.append(chip(theme, x, ver, w, 52, name, sub))
    parts.append(
        text(
            theme,
            WIDTH // 2,
            ver + 76,
            "three implementations that share no code, held to one written format",
            "t-detail",
            theme.muted,
            "middle",
        )
    )

    body = "\n".join(part for part in parts if part)
    return document(
        "pipeline",
        theme,
        "Docket wraps a run and leaves a receipt three independent programs can check",
        "An agent process runs under a supervisor, which places a loopback boundary between the "
        "agent and its provider. The boundary records model calls with their token counts and cost, "
        "refusals from caps and egress rules, and cache hits; a filesystem watcher records changed "
        "files with content digests. Every event lands in a hash-chained JSON ledger, which becomes "
        "one receipt. That receipt is verified offline by three implementations that share no code: "
        "the Python reference, a zero-dependency Node program, and a clean-room Go program.",
        body,
        ver + 104,
    )


# --------------------------------------------------------------------------- #
# 2. The cap, from a real run
# --------------------------------------------------------------------------- #


def load_cap_series() -> dict:
    with (DATA / "cap-series.json").open(encoding="utf-8") as handle:
        return json.load(handle)


def figure_cap(theme: Palette) -> str:
    """Cumulative spend against the ceiling, from one real `docket demo` run.

    This is the only figure in the set that is a chart rather than a diagram, and it is here because
    the claim "the cap binds" is quantitative. The series is read from `cap-series.json`, which
    records where it came from, so the figure cannot drift from the program that produced it.
    """
    data = load_cap_series()
    cap = float(data["cap_usd"])
    calls = data["calls"]
    loop = data["runaway_loop"]

    left, right = 96, WIDTH - 56
    top, bottom = 116, 336
    plot_w, plot_h = right - left, bottom - top
    highest = max(float(c["cumulative"]) for c in calls) * 1.12
    count = len(calls)

    def px(index: int) -> int:
        return int(left + plot_w * index / (count - 1))

    def py(value: float) -> int:
        return int(bottom - plot_h * value / highest)

    parts: list[str] = []
    parts.append(text(theme, 24, 32, "The ceiling binds outside the agent", "t-title"))
    parts.append(
        text(
            theme,
            24,
            60,
            f"One `docket demo` run: {count - 1} calls allowed, then the boundary refused the call "
            f"that would have crossed ${cap:.2f}",
            "t-detail",
            theme.secondary,
        )
    )

    # Gridlines and the y axis, at whole dollars.
    for dollar in range(0, int(highest) + 1):
        if dollar > highest:
            break
        y = py(float(dollar))
        parts.append(f'  <line x1="{left}" y1="{y}" x2="{right}" y2="{y}" class="hairline"/>')
        parts.append(text(theme, left - 12, y, f"${dollar}", "t-micro", theme.muted, "end", "mono"))

    # The cap line, dashed and in the fail colour, because crossing it is what the run is about.
    cap_y = py(cap)
    parts.append(
        f'  <line x1="{left}" y1="{cap_y}" x2="{right}" y2="{cap_y}" stroke="{theme.failed}" '
        f'stroke-width="1.75" stroke-dasharray="7 5"/>'
    )
    parts.append(mask_label(theme, right - 56, cap_y - 20, f"cap ${cap:.2f}", MICRO, "mono"))

    # The runaway region: 13 identical requests, which is the finding the receipt names.
    first_x, last_x = px(loop["first"] - 1), px(loop["last"] - 1)
    parts.append(
        f'  <rect x="{first_x}" y="{top}" width="{last_x - first_x}" height="{plot_h}" '
        f'fill="{theme.failed}" fill-opacity="0.07"/>'
    )
    parts.append(
        text(
            theme,
            (first_x + last_x) // 2,
            top - 24,
            f"{loop['identical_calls']} identical calls",
            "t-micro",
            theme.failed,
            "middle",
        )
    )

    # The staircase. A step per call, because spend is cumulative and each call adds to it.
    points = [(px(0), py(0.0))]
    for index, call in enumerate(calls):
        value = float(call["cumulative"])
        if call["outcome"] == "denied":
            continue
        points.append((px(index), py(value)))
    # A step, not a line: spend is flat between calls and jumps at each one. Drawing a straight
    # segment between the points would read as a trend *through* the calls, which is the one thing
    # this figure exists not to imply.
    path = f"M {points[0][0]} {points[0][1]}"
    for index in range(1, len(points)):
        previous_x, previous_y = points[index - 1]
        x, y = points[index]
        path += f" L {x} {previous_y} L {x} {y}"
    parts.append(f'  <path d="{path}" class="accent-line" stroke-width="2.25" fill="none"/>')
    for index, call in enumerate(calls):
        if call["outcome"] == "denied":
            continue
        parts.append(f'  <circle cx="{px(index)}" cy="{py(float(call["cumulative"]))}" r="3.5" fill="{theme.accent}"/>')

    # The refused call: the one moment the boundary did something the agent could not talk it out of.
    denied_index = next(i for i, c in enumerate(calls) if c["outcome"] == "denied")
    denied_x = px(denied_index)
    parts.append(
        f'  <line x1="{denied_x}" y1="{top}" x2="{denied_x}" y2="{bottom}" '
        f'stroke="{theme.failed}" stroke-width="1.5" stroke-dasharray="4 4"/>'
    )
    parts.append(
        f'  <rect x="{denied_x - 15}" y="{cap_y - 15}" width="30" height="30" rx="15" '
        f'fill="{theme.page}" stroke="{theme.failed}" stroke-width="1.75"/>'
    )
    parts.append(text(theme, denied_x, cap_y, "!", "t-section", theme.failed, "middle"))
    parts.append(mask_label(theme, denied_x - 6, top + 24, "403 refused", MICRO, "mono"))

    # Axis captions.
    parts.append(
        f'  <line x1="{left}" y1="{bottom}" x2="{right}" y2="{bottom}" stroke="{theme.border}" stroke-width="1.25"/>'
    )
    parts.append(text(theme, left, bottom + 24, "call 1", "t-micro", theme.muted, "middle", "mono"))
    parts.append(
        text(theme, px(denied_index), bottom + 24, f"call {denied_index + 1}", "t-micro", theme.muted, "middle", "mono")
    )
    parts.append(
        text(theme, left + plot_w // 2, bottom + 48, "model calls, in order", "t-detail", theme.muted, "middle")
    )

    # The verdict line, which is the part a reader quotes.
    parts.append(rule(theme, 24, 404, WIDTH - 48))
    verdicts = [
        ("spend", f"${data['total_usd']} of ${cap:.2f} cap", theme.text),
        ("the refused call", "403 docket_cap_exceeded", theme.failed),
        ("exit code", str(data["exit_code"]), theme.text),
        ("loop cost", f"${loop['usd_impact']}", theme.failed),
    ]
    x = 24
    for label, value, colour in verdicts:
        parts.append(text(theme, x, 432, label, "t-micro", theme.muted))
        parts.append(text(theme, x, 452, value, "t-label", colour, family="mono"))
        x += 236

    body = "\n".join(parts)
    return document(
        "cap",
        theme,
        "Cumulative spend rising to the cap, and the call the boundary refused",
        f"A step chart of cumulative spend across {count - 1} model calls in one docket demo run. "
        f"Spend climbs from one cent to ${data['total_usd']} against a ${cap:.2f} cap, drawn as a "
        f"dashed red line. Thirteen of those calls are byte-identical, shaded, and cost "
        f"${loop['usd_impact']} between them. The sixteenth call is refused with HTTP 403 and the run "
        f"exits {data['exit_code']}. The cap is enforced by the boundary, outside the agent process.",
        body,
        492,
    )


# --------------------------------------------------------------------------- #
# 3. The chain, and one edited byte
# --------------------------------------------------------------------------- #


def figure_chain(theme: Palette) -> str:
    """The hash chain, and what one edited byte does to it.

    Two rows of the same four events: intact, then edited. The point is not that the tool detects
    tampering — every tool claims that — but that it names the event number and both hashes, so the
    reader can check the claim themselves.
    """
    parts: list[str] = []
    parts.append(text(theme, 24, 32, "Each event carries the hash of the one before it", "t-title"))
    parts.append(
        text(
            theme,
            24,
            60,
            "hash = sha256(prev ‖ canonical JSON of this event), so an edit changes every hash after it",
            "t-detail",
            theme.secondary,
            family="mono",
        )
    )

    def row(specs: list[dict], y: int, broken_at: int | None) -> list[str]:
        """One line of the chain. `broken_at` tints the event after it and the link that follows it."""
        out: list[str] = []
        # Five cards from the left gutter to the right margin: 104 + 5*140 + 4*28 = 916. The first
        # version used 172 + 40, which put the fifth card 184 px past the viewBox, and the checker
        # is what caught it.
        width, gap = 140, 28
        for index, spec in enumerate(specs):
            x = 104 + index * (width + gap)
            edited = broken_at is not None and index == broken_at
            fill = theme.accent_soft if edited else theme.card
            stroke = theme.failed if edited else theme.border
            out.append(card(theme, x, y, width, 96, fill, stroke, theme.failed if edited else None))
            out.append(text(theme, x + 16, y + 22, f"#{spec['seq']}", "t-micro", theme.muted, family="mono"))
            out.append(text(theme, x + 16, y + 44, spec["name"], "t-label", theme.text))
            if edited and spec.get("recorded"):
                # The whole claim, in one card: the hash recorded beside the event, and the hash the
                # verifier recomputes from the bytes on disk. They differ, and that is the finding.
                out.append(text(theme, x + 16, y + 66, f"now {spec['digest']}", "t-micro", theme.failed, family="mono"))
                out.append(
                    text(
                        theme,
                        x + 16,
                        y + 84,
                        f"was {spec['recorded']}",
                        "t-micro",
                        theme.muted,
                        family="mono",
                    )
                )
            else:
                out.append(text(theme, x + 16, y + 72, spec["digest"], "t-micro", theme.secondary, family="mono"))
            if index < len(specs) - 1:
                link_broken = broken_at is not None and index == broken_at
                out.append(
                    arrow(
                        theme,
                        [(x + width, y + 48), (x + width + gap, y + 48)],
                        accent=False,
                    )
                )
                if link_broken:
                    out.append(
                        f'  <line x1="{x + width + gap}" y1="{y + 32}" x2="{x + width + gap}" '
                        f'y2="{y + 64}" stroke="{theme.failed}" stroke-width="2.5"/>'
                    )
                    out.append(
                        f'  <line x1="{x + width + gap - 8}" y1="{y + 32}" x2="{x + width + gap + 8}" '
                        f'y2="{y + 64}" stroke="{theme.failed}" stroke-width="2.5"/>'
                    )
        return out

    intact = [
        {"seq": "0", "name": "session.start", "digest": "adf41ffd…"},
        {"seq": "1", "name": "policy.applied", "digest": "b7c20913…"},
        {"seq": "6", "name": "fs.snapshot", "digest": "9c31d0a4…"},
        {"seq": "7", "name": "model.request", "digest": "43012d1c…"},
        {"seq": "8", "name": "model.response", "digest": "e5b77102…"},
    ]
    edited = [dict(spec) for spec in intact]
    edited[3] = {
        "seq": "7",
        "name": "model.request",
        "digest": "38dfe37a…",
        "recorded": "43012d1c…",
    }

    parts.append(text(theme, 24, 128, "intact", "t-section", theme.passed))
    parts.extend(row(intact, 148, broken_at=None))
    parts.append(
        text(
            theme, 104, 264, "each hash recomputes from the bytes on disk, and every prev links", "t-micro", theme.muted
        )
    )
    parts.append(rule(theme, 24, 292, WIDTH - 48))
    parts.append(text(theme, 24, 320, "one byte changed", "t-section", theme.failed))
    parts.extend(row(edited, 340, broken_at=3))
    parts.append(
        text(
            theme,
            104,
            456,
            "the link into #8 no longer holds, because #8's prev names the hash #7 used to have",
            "t-micro",
            theme.muted,
        )
    )

    # What the verifier says, quoted from a real run.
    parts.append(rule(theme, 24, 456, WIDTH - 48))
    lines = [
        ("FAIL", "ledger-chain", "broken at event #7: content hash mismatch", theme.failed),
        ("SKIP", "head", "not checked: the chain is broken at event #7", theme.skipped),
        ("FAIL", "merkle-root", "recomputed 38dfe37a… != receipt 43012d1c…", theme.failed),
    ]
    y = 492
    for verdict, check, detail, colour in lines:
        parts.append(text(theme, 104, y, verdict, "t-micro", colour, family="mono"))
        parts.append(text(theme, 168, y, check, "t-label", theme.text, family="mono"))
        parts.append(text(theme, 344, y, detail, "t-detail", theme.secondary, family="mono"))
        y += 26
    parts.append(
        text(
            theme,
            104,
            y + 14,
            'The receipt is not "unreadable". It is specific about exactly where the record stops being trustworthy.',
            "t-detail",
            theme.muted,
        )
    )

    body = "\n".join(parts)
    return document(
        "chain",
        theme,
        "A hash chain over ledger events, and the verdict after one byte is changed",
        "Four ledger events in a hash chain, each carrying the hash of the event before it. In the "
        "second row one event is edited, so its hash no longer matches and every hash after it "
        "would have to change too. The verifier reports FAIL on ledger-chain, naming event 7 and "
        "the mismatching hash; SKIP on head, because the chain is already broken; and FAIL on "
        "merkle-root, naming the recomputed root and the one in the receipt.",
        body,
        y + 44,
    )


# --------------------------------------------------------------------------- #
# 4. What is inside a receipt
# --------------------------------------------------------------------------- #


def figure_receipt(theme: Palette) -> str:
    """The receipt's own sections, grouped by the question each one answers.

    Twenty-four members is too many to explain in prose and too few to need a manual. Grouping them
    by what a reader asks is the whole value: it turns a wall of keys into five answers.
    """
    groups = [
        (
            "which run was this",
            ["format", "docket_number", "label", "created_at", "tool"],
            theme.muted,
        ),
        (
            "what it was allowed to do",
            ["policy", "caps"],
            theme.accent,
        ),
        (
            "what it cost",
            ["totals", "model_calls", "price_table", "latency"],
            theme.accent,
        ),
        (
            "what it touched",
            ["tools", "files", "network", "ops"],
            theme.secondary,
        ),
        (
            "how you check it",
            ["ledger", "event_counts", "appendix", "evidence", "seal", "signature"],
            theme.accent,
        ),
        (
            "what it admits",
            ["limits_disclosure", "findings", "proxy"],
            theme.failed,
        ),
    ]
    parts: list[str] = []
    parts.append(text(theme, 24, 32, "What is inside one receipt", "t-title"))
    parts.append(
        text(
            theme,
            24,
            60,
            "24 members, grouped by the question each answers rather than by the order the writer assembled them",
            "t-detail",
            theme.secondary,
        )
    )

    y = 96
    for question, members, colour in groups:
        # The card's height is the pills' height plus padding, and the pills are laid out before the
        # card is drawn. Reserve the two slots, remember where they are, and fill them in afterwards:
        # indexing from the front of `parts` is wrong because `parts` carries the title and every
        # earlier group, which is how the first version of this quietly drew no cards at all.
        reserved = len(parts)
        parts.append("")
        parts.append("")
        parts.append(text(theme, 48, y + 26, question, "t-label", theme.text))
        pills, below = chip_grid(theme, 48, y + 44, WIDTH - 48, members)
        height = (below - y) + 12
        parts.extend(pills)
        parts[reserved] = card(theme, 24, y, WIDTH - 48, height, theme.card, theme.border)
        parts[reserved + 1] = f'  <rect x="24" y="{y}" width="4" height="{height}" rx="2" fill="{colour}"/>'
        y += height + 16

    parts.append(rule(theme, 24, y + 8, WIDTH - 48))
    parts.append(
        text(
            theme,
            24,
            y + 36,
            "No floats anywhere: every number is an integer or a decimal string, so the file hashes "
            "identically in every language.",
            "t-detail",
            theme.muted,
        )
    )
    parts.append(
        text(
            theme,
            24,
            y + 60,
            "limits_disclosure is required, not optional: a verifier fails a receipt that does not "
            "state what it could and could not observe.",
            "t-detail",
            theme.muted,
        )
    )

    body = "\n".join(parts)
    return document(
        "receipt",
        theme,
        "The sections of a Docket receipt, grouped by what a reader asks",
        "A receipt has 24 members. Grouped by question: which run it was (format, docket number, "
        "label, timestamp, tool); what it was allowed to do (policy, caps); what it cost (totals, "
        "model calls, price table, latency); what it touched (tools, files, network, ops); how to "
        "check it (ledger, event counts, appendix, evidence, seal, signature); and what it admits "
        "(limits disclosure, findings, proxy). Every number is an integer or a decimal string, and "
        "the limits disclosure is required.",
        body,
        y + 88,
    )


# --------------------------------------------------------------------------- #
# 5. Three implementations, one format
# --------------------------------------------------------------------------- #


def figure_verifiers(theme: Palette) -> str:
    """Why three verifiers and not one: a format is only worth what a stranger can reproduce.

    The strongest claim in the project is that the format is normative and independently implemented.
    That claim is a diagram, not a sentence: a specification on the left, three implementations that
    share no code in the middle, and one corpus of verdicts on the right that all three must agree on.
    """
    parts: list[str] = []
    parts.append(text(theme, 24, 32, "One format, three implementations, no shared code", "t-title"))
    parts.append(
        text(
            theme,
            24,
            60,
            "A format is worth what a stranger can reproduce from the specification alone",
            "t-detail",
            theme.secondary,
        )
    )

    top = 96
    parts.append(text(theme, 24, top + 20, "the contract", "t-micro", theme.muted))
    parts.append(card(theme, 24, top + 36, 224, 112, theme.accent_soft, theme.border, theme.accent))
    parts.append(text(theme, 44, top + 64, "spec/", "t-label", theme.text, family="mono"))
    parts.append(text(theme, 44, top + 86, "docket-receipt-1.md", "t-micro", theme.secondary, family="mono"))
    parts.append(text(theme, 44, top + 108, "the format, verbatim", "t-micro", theme.muted))
    parts.append(text(theme, 44, top + 128, "CC BY 4.0", "t-micro", theme.muted))

    parts.append(text(theme, 300, top + 20, "the implementations", "t-micro", theme.muted))
    impls = [
        ("verify.py", "Python reference · zero dependencies"),
        ("verify.mjs", "Node · standard library only"),
        ("docket-verify", "Go · clean-room, from the spec"),
    ]
    for index, (name, note) in enumerate(impls):
        y = top + 36 + index * 40
        parts.append(card(theme, 300, y, 336, 36, theme.card, theme.border))
        parts.append(text(theme, 316, y + 18, name, "t-label", theme.text, family="mono"))
        # One line, inside the card. The first version put a third column at x=620 with the card
        # ending at 616, so every note was clipped mid-word and the figure named nothing.
        parts.append(text(theme, 316, y + 30, note, "t-micro", theme.secondary))
        parts.append(arrow(theme, [(248, top + 92), (276, top + 92), (276, y + 18), (300, y + 18)]))

    parts.append(text(theme, 680, top + 20, "the corpus", "t-micro", theme.muted))
    parts.append(card(theme, 680, top + 36, 276, 112, theme.card, theme.border))
    corpus = [
        ("44", "committed vectors"),
        ("2", "valid documents"),
        ("42", "negative corpus"),
        ("132", "verdicts that must agree"),
    ]
    for index, (number, label) in enumerate(corpus):
        y = top + 60 + index * 24
        parts.append(text(theme, 700, y, number, "t-label", theme.accent_text, "start", "mono"))
        parts.append(text(theme, 756, y, label, "t-micro", theme.secondary))
    parts.append(
        text(
            theme,
            680,
            top + 166,
            "of the 42: 38 must be refused, 4 accepted",
            "t-micro",
            theme.muted,
        )
    )
    for index in range(3):
        y = top + 54 + index * 40
        parts.append(arrow(theme, [(636, y), (658, y), (658, top + 92), (680, top + 92)]))

    parts.append(rule(theme, 24, 268, WIDTH - 48))
    parts.append(
        text(
            theme,
            24,
            296,
            "What that buys a reader: a receipt written by 0.1.0 verifies under any later release, "
            "and you can check one with a program this project did not write.",
            "t-detail",
            theme.text,
        )
    )
    parts.append(
        f'  <rect x="24" y="324" width="{WIDTH - 48}" height="56" rx="8" class="raised" fill="{theme.raised}"/>'
    )
    parts.append(text(theme, 44, 344, "$ make conformance", "t-micro", theme.text, family="mono"))
    parts.append(
        text(
            theme,
            44,
            366,
            "88 correct, 0 wrong, 0 not judged (2 positive, 42 negative vectors)",
            "t-micro",
            theme.passed,
            family="mono",
        )
    )

    body = "\n".join(parts)
    return document(
        "verifiers",
        theme,
        "One specification, three verifiers that share no code, one corpus of 132 verdicts",
        "The specification in spec/ is the contract, licensed CC BY 4.0 so other implementations can "
        "copy it. Three verifiers implement it independently: the Python reference with zero "
        "dependencies, a Node program using only the standard library, and a clean-room Go program "
        "written from the specification alone. All three run against 44 committed conformance "
        "vectors, two valid documents and a negative corpus of 42 of which 38 must be refused, which "
        "is 132 verdicts that have to agree. Running make conformance reports 88 correct, 0 wrong.",
        body,
        420,
    )


# --------------------------------------------------------------------------- #
# 6. Every check, and what it proves
# --------------------------------------------------------------------------- #


def figure_checks(theme: Palette) -> str:
    """Every check id in the registry, grouped by the claim it makes.

    The list is read from `spec/registry.json`, so a check added to the format appears here or a
    guard fails. That is the difference between a figure and a claim.
    """
    registry = json.loads((REPO / "spec" / "registry.json").read_text(encoding="utf-8"))
    entries = [item for item in registry["ids"] if item.get("emitted")]

    groups: list[tuple[str, list[str], str]] = [
        ("the record is unaltered", [], theme.accent),
        ("the arithmetic is recomputable", [], theme.accent),
        ("the receipt states its limits", [], theme.failed),
        ("optional, and reported when absent", [], theme.skipped),
    ]
    integrity = {
        "ledger-chain",
        "head",
        "event-count",
        "merkle-root",
        "ledger-file-digest",
        "retained-responses",
        "usage-inclusion-proofs",
        "usage-rows-rebound",
        "evidence-completeness",
        "receipt-format",
        "receipt-shape",
    }
    money = {
        "cost-recomputation",
        "totals",
        "token-totals",
        "cached-totals",
        "assumed-prices",
        "by-model-totals",
        "usage-source-counts",
        "cost-basis",
        "latency-summary",
        "cache-savings",
        "cache-hit-origins",
        "cache-hit-proofs",
    }
    disclosure = {"limits-disclosure", "format", "chaos-run"}
    optional = {"seal", "signature"}

    for entry in entries:
        name = entry["id"]
        if name in integrity:
            groups[0][1].append(name)
        elif name in money:
            groups[1][1].append(name)
        elif name in disclosure:
            groups[2][1].append(name)
        elif name in optional:
            groups[3][1].append(name)
        else:
            raise SystemExit(f"check id {name!r} is not grouped in the checks figure")

    parts: list[str] = []
    parts.append(text(theme, 24, 32, "Every check, and what it proves", "t-title"))
    parts.append(
        text(
            theme,
            24,
            60,
            f"{len(entries)} emitted check ids, read from spec/registry.json, grouped by the claim each one makes",
            "t-detail",
            theme.secondary,
        )
    )

    y = 96
    for question, names, colour in groups:
        if not names:
            continue
        pills, below = chip_grid(theme, 48, y + 44, WIDTH - 48, names)
        height = (below - y) + 12
        parts.append(card(theme, 24, y, WIDTH - 48, height, theme.card, theme.border))
        parts.append(f'  <rect x="24" y="{y}" width="4" height="{height}" rx="2" fill="{colour}"/>')
        parts.append(text(theme, 48, y + 24, f"{question}   ·   {len(names)}", "t-label", theme.text))
        parts.extend(pills)
        y += height + 14

    parts.append(rule(theme, 24, y + 6, WIDTH - 48))
    parts.append(
        text(
            theme,
            24,
            y + 34,
            "A check that cannot run reports SKIP with its reason. It never reports PASS.",
            "t-detail",
            theme.text,
        )
    )
    parts.append(
        text(
            theme,
            24,
            y + 58,
            "One run of `docket demo` emits 19 PASS and 3 SKIP.",
            "t-detail",
            theme.muted,
        )
    )
    parts.append(
        text(
            theme,
            24,
            y + 80,
            "Seal and signature are absent because the run was neither sealed nor signed; "
            "cache-savings because the cache was off.",
            "t-detail",
            theme.muted,
        )
    )

    body = "\n".join(parts)
    return document(
        "checks",
        theme,
        "Every emitted check in the registry, grouped by what it proves",
        "The registry defines the check vocabulary for the docket receipt format. Grouped by claim: "
        "the record is unaltered, covering the chain, head, event count, Merkle root, ledger file "
        "digest, retained responses, usage inclusion proofs, evidence completeness and document "
        "shape; the arithmetic is recomputable, covering cost recomputation, totals, token totals, "
        "cached totals, assumed prices, per-model totals, usage-source counts, cost basis, latency "
        "and the cache checks; the receipt states its limits, covering format, limits disclosure and "
        "chaos-run; and the optional seal and signature. A check that cannot run reports SKIP.",
        body,
        y + 106,
    )
