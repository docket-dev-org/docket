#!/usr/bin/env python3
"""Render checked-in brand SVGs. Optional build tool; no Docket runtime dependencies.
Install: python3 -m pip install -r assets/requirements-brand.txt
Run:     python3 assets/make-assets.py
All lettering is outlined; raster and vector geometry cannot drift independently.
"""

from pathlib import Path

import cairosvg

HERE = Path(__file__).resolve().parent
for source, target, w, h in [
    ("app-icon.svg", "icon.png", 512, 512),
    ("social-preview.svg", "social-preview.png", 1280, 640),
]:
    cairosvg.svg2png(url=str(HERE / source), write_to=str(HERE / target), output_width=w, output_height=h)
    print(f"wrote {target} ({w}x{h})")
