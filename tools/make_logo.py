#!/usr/bin/env python3
"""Qyro logo generator: one geometry, crisp SVG + raster icons.

The mark is a "Q" whose tail is a right-pointing play triangle cut out of the
ring, painted with the brand gradient (violet #7C3AED -> cyan #22D3EE) on
near-black #0A0A0F.

Dev-only asset tool: Pillow is NOT a runtime dependency of Qyro — the PNGs it
writes are checked in, so the app itself stays stdlib + ffmpeg + yt-dlp.

    python3 tools/make_logo.py          # needs Pillow just for this script

Outputs (relative to the repo root):
    web/logo.svg, web/icons/favicon.svg, icon-192.png, icon-512.png,
    icon-maskable-192.png, icon-maskable-512.png, apple-touch-icon.png,
    favicon-32.png
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# --- geometry on a 64x64 design grid (shared by SVG and the rasteriser) -----
BOX = 64.0
CX, CY = 27.0, 27.0           # ring centre, offset up-left to free the tail
R_OUT = 20.8                  # ring outer radius
RING = 9.4                    # ring thickness (donut band)
TRI = [(36.0, 38.5), (55.0, 50.5), (36.0, 62.5)]    # the play triangle
CUT = [(29.0, 31.0), (62.0, 47.0), (33.5, 68.5)]    # the wedge it is cut from
VIOLET = (0x7C, 0x3A, 0xED)
CYAN = (0x22, 0xD3, 0xEE)
BG = (0x0A, 0x0A, 0x0F)


def _d(points) -> str:
    body = " ".join(
        f"{'M' if i == 0 else 'L'}{x:.2f} {y:.2f}" for i, (x, y) in enumerate(points)
    )
    return body + " Z"


def _circle_path(cx: float, cy: float, r: float, fill: str) -> str:
    """Full circle as a path (so the hole can knock out the outer disc)."""
    return (
        f'<path fill="{fill}" d="M{cx - r:.2f} {cy:.2f}'
        f'a{r:.2f} {r:.2f} 0 1 0 {2 * r:.2f} 0'
        f'a{r:.2f} {r:.2f} 0 1 0 {-2 * r:.2f} 0 Z"/>\n'
        f'    '
    )


def svg_mark(*, size=None, background=None, radius=0, pad=0.0) -> str:
    """The inline SVG (also written to web/logo.svg)."""
    wh = f' width="{size}" height="{size}"' if size else ""
    bg = ""
    if background:
        rx = f' rx="{radius}"' if radius else ""
        bg = f'<rect width="64" height="64" fill="{background}"{rx}/>'
    shift = ""
    if pad:
        k = 1.0 - pad
        shift = f' transform="translate({32 - 32 * k:.2f} {32 - 32 * k:.2f}) scale({k:.3f})"'
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"{wh}'
        f' role="img" aria-label="Qyro">\n'
        f'  <defs>\n'
        f'    <linearGradient id="qg" x1="6" y1="4" x2="58" y2="60"'
        f' gradientUnits="userSpaceOnUse">\n'
        f'      <stop offset="0" stop-color="#7C3AED"/>\n'
        f'      <stop offset="0.55" stop-color="#9D5CF5"/>\n'
        f'      <stop offset="1" stop-color="#22D3EE"/>\n'
        f'    </linearGradient>\n'
        f'    <mask id="qcut">\n'
        f'      <rect width="64" height="64" fill="#fff"/>\n'
        f'      <path d="{_d(CUT)}" fill="#000"/>\n'
        f'    </mask>\n'
        f'  </defs>\n'
        f'  {bg}\n'
        f'  <g{shift}>\n'
        f'    <g mask="url(#qcut)">\n'
        f'      {_circle_path(CX, CY, R_OUT, "url(#qg)")}'
        f'      {_circle_path(CX, CY, R_OUT - RING, "#000")}'
        f'    </g>\n'
        f'    <path d="{_d(TRI)}" fill="url(#qg)"/>\n'
        f'  </g>\n'
        f'</svg>\n'
    )


def render_png(pixel: int, *, transparent: bool = False, pad: float = 0.05,
               ss: int = 4) -> bytes:
    """Supersampled raster of exactly the SVG geometry above."""
    import io

    from PIL import Image, ImageDraw, ImageFilter

    n = int(pixel) * ss
    scale = n / BOX
    inner = int(n * (1.0 - 2 * pad))

    # gradient plate: built small and stretched (cheap, perfectly smooth)
    small = 256
    plate_small = Image.new("RGB", (small, small))
    px = plate_small.load()
    for y in range(small):
        for x in range(small):
            t = (x / (small - 1) + y / (small - 1)) / 2.0
            t **= 0.85
            px[x, y] = tuple(
                int(round(VIOLET[i] + (CYAN[i] - VIOLET[i]) * t)) for i in range(3)
            )
    plate = plate_small.resize((n, n), Image.BILINEAR)

    def shape(canvas) -> None:
        """Exactly the SVG construction: disc, hole, cut wedge, play triangle."""
        d = ImageDraw.Draw(canvas)
        off = (n - inner) // 2

        def P(x, y):
            return (off + x * scale, off + y * scale)

        ro, ri = R_OUT * scale, (R_OUT - RING) * scale
        cx, cy = off + CX * scale, off + CY * scale
        d.ellipse([cx - ro, cy - ro, cx + ro, cy + ro], fill=255)
        if ri > 0:
            d.ellipse([cx - ri, cy - ri, cx + ri, cy + ri], fill=0)
        d.polygon([P(x, y) for x, y in CUT], fill=0)
        d.polygon([P(x, y) for x, y in TRI], fill=255)

    mask = Image.new("L", (n, n), 0)
    shape(mask)
    mask = mask.filter(ImageFilter.GaussianBlur(radius=0.35 * ss))

    glyph = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    glyph.paste(plate, (0, 0), mask)

    if transparent:
        out = glyph
    else:
        alpha = Image.new("L", (n, n), 0)
        ImageDraw.Draw(alpha).rounded_rectangle([0, 0, n - 1, n - 1],
                                                radius=int(n * 0.225), fill=255)
        back = Image.composite(Image.new("RGBA", (n, n), BG + (255,)),
                              Image.new("RGBA", (n, n), (0, 0, 0, 0)), alpha)
        out = Image.alpha_composite(back, glyph)
    out = out.resize((pixel, pixel), Image.LANCZOS)
    buf = io.BytesIO()
    out.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def main() -> None:
    import io

    from PIL import Image

    web = ROOT / "web"
    icons = web / "icons"
    icons.mkdir(parents=True, exist_ok=True)

    (web / "logo.svg").write_text(svg_mark(background="#0A0A0F"), encoding="utf-8")
    (icons / "favicon.svg").write_text(svg_mark(), encoding="utf-8")

    files = {
        "icon-512.png": render_png(512),
        "icon-192.png": render_png(192),
        "icon-maskable-512.png": render_png(512, pad=0.16),
        "icon-maskable-192.png": render_png(192, pad=0.16),
        "apple-touch-icon.png": render_png(180),
        "favicon-32.png": render_png(32, ss=6),
    }
    for name, data in files.items():
        (icons / name).write_bytes(data)
        with Image.open(io.BytesIO(data)) as im:
            print(f"{name:26s} {im.size[0]}x{im.size[1]}  {len(data):>7} bytes")


if __name__ == "__main__":
    main()
