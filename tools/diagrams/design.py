"""Shared design system for Docket's documentation figures.

Every constant here is a decision someone can argue with, so each one says why it is that value.
Nothing in this module draws: it defines the palette, the type scale, the primitives that
`tools/make_diagrams.py` composes, and the rules the figures are held to.

The rules, and where each comes from:

- **Orthogonal connectors only.** A diagonal between two boxes is the single clearest signal that a
  diagram was not designed. Elbows are rounded at r=8.
- **Arrow labels sit on an opaque mask**, never on the stroke, with a gap of at least 8 px. A label
  drawn over its own line is unreadable at the width GitHub renders a README.
- **Text contrast is at least 4.5:1** against whatever it sits on. `palette.py`-style measurement is
  not optional here: `tests/test_diagrams.py` recomputes every pair from the emitted SVG.
- **Every coordinate, width and gap is divisible by 4.** This is what stops the figures looking
  auto-generated.
- **Corner radius is 10 px or less.** A 24 px radius on a 72 px card reads as a toy.
- **Real text, not outlined paths.** WCAG 1.4.5 prefers text over images of text; a label the reader
  can select is a label a screen reader can read and a translator can change. The wordmark in
  `assets/` is the documented exception, because it is a logotype rather than a diagram.
- **Mono type is for technical content only** — hashes, paths, commands, field names. Node names are
  sans. Using mono everywhere is the most common way a technical diagram looks amateur.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Sans stack. Web-safe and metric-stable, so a label that fits on the author's machine fits on the
#: reader's. No embedded font: an SVG that carries one is an order of magnitude larger, and the
#: system stack is what every reader already has.
SANS = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, 'Noto Sans', sans-serif"

#: Mono stack, for hashes, paths, commands and field names.
MONO = "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, 'Liberation Mono', 'DejaVu Sans Mono', monospace"

#: The type scale. Four sizes only: a figure needs a hierarchy, not a menu.
TITLE = 19
SECTION = 15
LABEL = 13
DETAIL = 11
MICRO = 10

#: Connector geometry. r=8 is the elbow radius; gap is the clearance between an arrow label and its
#: own stroke.
ELBOW = 8
LABEL_GAP = 8

#: Cards. One radius family, one hairline weight.
RADIUS = 8
STROKE = 1.25


@dataclass(frozen=True)
class Palette:
    """One theme's colours, named by the job they do rather than by the hue they happen to be."""

    name: str
    page: str
    card: str
    raised: str
    text: str
    secondary: str
    muted: str
    accent: str
    accent_soft: str
    accent_text: str
    border: str
    grid: str
    passed: str
    failed: str
    skipped: str
    optional: str

    def css(self) -> str:
        """The palette as a `<style>` block, so a rule can name a job instead of a hex value."""
        return "\n".join(
            f"      .{key} {{ fill: {value}; }}"
            for key, value in (
                ("page", self.page),
                ("card", self.card),
                ("raised", self.raised),
                ("text", self.text),
                ("secondary", self.secondary),
                ("muted", self.muted),
                ("accent", self.accent),
                ("accent-soft", self.accent_soft),
                ("accent-text", self.accent_text),
                ("border", self.border),
                ("grid", self.grid),
                ("passed", self.passed),
                ("failed", self.failed),
                ("skipped", self.skipped),
                ("optional", self.optional),
            )
        )

    def stroke(self) -> str:
        return f"      .line {{ stroke: {self.border}; fill: none; }}\n"


#: Light. The page is Docket's warm ivory and the cards lift to white, which is the only place in
#: this project where white is a surface rather than a text colour.
LIGHT = Palette(
    name="light",
    page="#F7F5EF",
    card="#FFFFFF",
    raised="#EFEDE6",
    text="#17212B",
    secondary="#3D4C5A",
    muted="#55677A",
    accent="#2457F5",
    accent_soft="#E4EBFE",
    accent_text="#12307F",
    border="#D5CEBF",
    grid="#DDD6C8",
    passed="#17663F",
    failed="#B3261E",
    skipped="#7A5300",
    optional="#55677A",
)

#: Dark. Charcoal page, cards a shade lighter, and the bright cobalt for accent because the vivid
#: cobalt loses separation against a dark ground.
DARK = Palette(
    name="dark",
    page="#17212B",
    card="#1F2B37",
    raised="#26333F",
    text="#F7F5EF",
    secondary="#B4C4D4",
    muted="#93A6B8",
    accent="#7FA6FF",
    accent_soft="#1D2C4C",
    accent_text="#A9C4FF",
    border="#2E3D4C",
    grid="#28343F",
    passed="#5BD68B",
    failed="#FF9C92",
    skipped="#E8C56A",
    optional="#93A6B8",
)

THEMES = (LIGHT, DARK)

#: Average glyph advance as a fraction of font size, measured well enough to catch a label that
#: overflows its card. Deliberately pessimistic: it is better for a guard to ask a question than to
#: wave a real overflow through.
ADVANCE = {"sans": 0.56, "mono": 0.62, "bold": 0.60}


def text_width(label: str, size: float, family: str = "sans", bold: bool = False) -> float:
    """Estimate the rendered width of `label`.

    A figure is not finished until its labels fit. Rather than eyeballing it, every label goes
    through here and `tests/test_diagrams.py` compares the result against the box that contains it.
    """
    key = "bold" if bold else family
    return len(label) * ADVANCE[key] * size
