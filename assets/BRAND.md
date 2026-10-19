# Docket brand assets

This directory is the approved Docket identity: concept A, the bounded ledger — three connected
records ending at one common boundary. The palette is warm ivory `#F7F5EF`, deep charcoal
`#17212B`, vivid cobalt `#2457F5`, bright cobalt `#7FA6FF` for dark surfaces, with muted slate
`#5F7082` and warm sand `#E7E0D3` as supporting neutrals. Nothing here adds a GUI, a hosted
service or a runtime dependency, and none of it is a security guarantee.

## What each file is for

| File | Role |
|---|---|
| `logo.svg` | White micro symbol, for the small colored Material toolbar. Not for a white page. |
| `favicon.svg` | Adaptive micro favicon; switches to bright cobalt under `prefers-color-scheme: dark`. |
| `logo-color.svg` | Standalone symbol on a light surface. |
| `logo-horizontal.svg` | Named lockup on a light surface. |
| `logo-horizontal-dark.svg` | Named lockup on a dark surface. |
| `app-icon.svg` | Full-bleed 256x256 vector source of `icon.png`. |
| `icon.png` | Opaque 512x512 warm-ivory-on-cobalt avatar tile. |
| `social-preview.svg` | Vector source of `social-preview.png`. |
| `social-preview.png` | Opaque 1280x640 social artwork, the size GitHub crops from. |
| `favicon.ico` | Legacy fallback carrying 16x16, 32x32 and 48x48 at 32bpp. |
| `apple-touch-icon.png` | Opaque 180x180 iOS home-screen icon. |
| `brand.css` | Brand palette written over Material's CSS custom properties. |
| `make-assets.py` | Renders `icon.png` and `social-preview.png` from their SVGs. |
| `requirements-brand.txt` | CairoSVG pin for that generator; not a Docket dependency. |
| `FONT-LICENSE.txt` | Bitstream Vera / DejaVu notice, retained with the outlined type. |

The wordmark is outlined vector paths, never live text, so no font has to be installed to display
any file here. `make-assets.py` was reformatted to this repository's `ruff` configuration when the
kit was adopted; the render calls are unchanged and the output is byte-identical, which
`tests/test_brand_assets.py` measures rather than asserts.

## What the kit also ships, and why it is not here

The kit's `web/` directory carries a `site.webmanifest` with `display: standalone` and a pair of
maskable icons. Those exist to make a site installable as a standalone app, and the kit itself
labels them optional and notes that Docket does not ship a browser app or PWA. Adding them would
make the documentation site installable, which is a behaviour change this project has not made,
so they are deliberately left out rather than shipped unused. The separate 16/32/48px PNGs are
left out for a plainer reason: `favicon.ico` already contains all three at 32bpp, so they would
be three more files describing artwork that is already there.

## Regenerating

```bash
python3 -m pip install -r assets/requirements-brand.txt   # separate build environment preferred
python3 assets/make-assets.py
```

Do not hand-edit the rasters: the SVGs are the source, and the generator is what keeps the two
from drifting. Minimum sizes, clear space and misuse rules are the kit's `GUIDELINES.md`; the
approved palette is the table above.
