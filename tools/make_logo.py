#!/usr/bin/env python3
"""Qyro v6.2 logo generator: Claude + Grok inspired mark, purple UI.

The mark is a soft 6-point rounded star (Claude's friendly organic shape)
forming the Q ring, with a sharp play triangle tail (Grok's angular cut)
and a tiny cyan sparkle accent. Painted with the brand gradient
violet #7C3AED -> #A855F7 -> cyan #22D3EE on near-black #0A0A0F.

Dev-only asset tool: Pillow is NOT a runtime dependency of Qyro — the PNGs it
writes are checked in, so the app itself stays stdlib + ffmpeg + yt-dlp.

    python3 tools/make_logo.py          # needs Pillow just for this script

Outputs (relative to the repo root):
    web/logo.svg, web/icons/favicon.svg, icon-192.png, icon-512.png,
    icon-maskable-192.png, icon-maskable-512.png, apple-touch-icon.png,
    favicon-32.png
"""
from __future__ import annotations

import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# --- geometry on a 64x64 design grid (shared by SVG and the rasteriser) -----
BOX = 64.0
CX, CY = 28.0, 27.0
STAR_OUTER = 19.0
STAR_INNER = 11.0
STAR_POINTS = 6
STAR_ROUND = 0.42
R_INNER = 10.2  # hole
TRI = [(35.2, 38.8), (57.2, 51.0), (35.2, 63.2)]  # sharp Grok-like play tail
CUT = [(30.5, 32.5), (62.5, 46.8), (34.2, 68.5)]  # Q gap wedge
SPARKLE = [(47.0, 12.6), (47.85, 14.55), (49.8, 15.4), (47.85, 16.25), (47.0, 18.2), (46.15, 16.25), (44.2, 15.4), (46.15, 14.55)]

VIOLET = (0x7C, 0x3A, 0xED)
VIOLET2 = (0xA8, 0x55, 0xF7)
CYAN = (0x22, 0xD3, 0xEE)
BG = (0x0A, 0x0A, 0x0F)

# precomputed SVG path for the rounded star (6 pts, rnd 0.42)
STAR_PATH = (
    "M 25.69 11.98 Q 28.00 8.00 30.31 11.98 L 31.19 13.49 Q 33.50 17.47 38.10 17.48 "
    "L 39.85 17.49 Q 44.45 17.50 42.16 21.49 L 41.29 23.01 Q 39.00 27.00 41.29 30.99 "
    "L 42.16 32.51 Q 44.45 36.50 39.85 36.51 L 38.10 36.52 Q 33.50 36.53 31.19 40.51 "
    "L 30.31 42.02 Q 28.00 46.00 25.69 42.02 L 24.81 40.51 Q 22.50 36.53 17.90 36.52 "
    "L 16.15 36.51 Q 11.55 36.50 13.84 32.51 L 14.71 30.99 Q 17.00 27.00 14.71 23.01 "
    "L 13.84 21.49 Q 11.55 17.50 16.15 17.49 L 17.90 17.48 Q 22.50 17.47 24.81 13.49 Z"
)


def _d(points) -> str:
    body = " ".join(
        f"{'M' if i == 0 else 'L'}{x:.2f} {y:.2f}" for i, (x, y) in enumerate(points)
    )
    return body + " Z"


def svg_mark(*, size=None, background=None, radius=0, pad=0.0) -> str:
    """The inline SVG (also written to web/logo.svg)."""
    wh = f' width="{size}" height="{size}"' if size else ""
    bg = ""
    if background:
        rx = f' rx="{radius or 14}"' if radius or background else ""
        # logo.svg uses rx=14 for the app icon background
        if radius == 0:
            rx = ' rx="14"'
        bg = f'<rect width="64" height="64" fill="{background}"{rx}/>'
    else:
        bg = ""
    shift = ""
    if pad:
        k = 1.0 - pad
        shift = f' transform="translate({32 - 32 * k:.2f} {32 - 32 * k:.2f}) scale({k:.3f})"'
    # for transparent favicon we need evenodd donut
    if background:
        inner = f'<circle cx="{CX}" cy="{CY}" r="{R_INNER}" fill="{background}"/>'
        star = f'<path fill="url(#qg)" d="{STAR_PATH}"/>\n    {inner}'
    else:
        # evenodd star donut for transparent bg
        star = f'<path fill="url(#qg)" fill-rule="evenodd" d="{STAR_PATH} M {CX} {CY - R_INNER} A {R_INNER} {R_INNER} 0 1 0 {CX} {CY + R_INNER} A {R_INNER} {R_INNER} 0 1 0 {CX} {CY - R_INNER} Z"/>'

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"{wh}'
        f' role="img" aria-label="Qyro">\n'
        f'  <defs>\n'
        f'    <linearGradient id="qg" x1="8" y1="6" x2="56" y2="58" gradientUnits="userSpaceOnUse">\n'
        f'      <stop offset="0%" stop-color="#7C3AED"/>\n'
        f'      <stop offset="0.52" stop-color="#A855F7"/>\n'
        f'      <stop offset="1%" stop-color="#22D3EE"/>\n'
        f'    </linearGradient>\n'
        f'    <linearGradient id="qg2" x1="34" y1="38" x2="58" y2="62" gradientUnits="userSpaceOnUse">\n'
        f'      <stop offset="0%" stop-color="#A855F7"/>\n'
        f'      <stop offset="1%" stop-color="#22D3EE"/>\n'
        f'    </linearGradient>\n'
        f'    <mask id="qcut">\n'
        f'      <rect width="64" height="64" fill="#fff"/>\n'
        f'      <path d="{_d(CUT)}" fill="#000"/>\n'
        f'    </mask>\n'
        f'  </defs>\n'
        f'  {bg}\n'
        f'  <g{shift}>\n'
        f'    <g mask="url(#qcut)">\n'
        f'      {star}\n'
        f'    </g>\n'
        f'    <path d="{_d(TRI)}" fill="url(#qg2)"/>\n'
        f'    <path d="{_d(SPARKLE)}" fill="#22D3EE" opacity="0.95"/>\n'
        f'  </g>\n'
        f'</svg>\n'
    )


def _sample_quad(p0, p1, p2, steps=8):
    """Sample points along quadratic bezier p0->p2 with control p1."""
    pts = []
    for i in range(steps + 1):
        t = i / steps
        inv = 1 - t
        x = inv * inv * p0[0] + 2 * inv * t * p1[0] + t * t * p2[0]
        y = inv * inv * p0[1] + 2 * inv * t * p1[1] + t * t * p2[1]
        pts.append((x, y))
    return pts


def _rounded_star_polygon(cx, cy, outer, inner, points=6, roundness=0.42, quad_steps=6):
    """Return dense polygon approximating the rounded star path."""
    # base alternating points
    base = []
    for i in range(points * 2):
        ang = math.radians(-90 + i * 180 / points)
        r = outer if i % 2 == 0 else inner
        base.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    # build rounded polygon: for each base point, we have incoming edge and outgoing edge
    poly = []
    for i in range(len(base)):
        p_prev = base[(i - 1) % len(base)]
        p_curr = base[i]
        p_next = base[(i + 1) % len(base)]

        def lerp(a, b, t):
            return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)

        p_in = lerp(p_prev, p_curr, 1 - roundness)
        p_out = lerp(p_curr, p_next, roundness)
        if i == 0:
            # start at first p_in
            poly.append(p_in)
        else:
            # line from previous p_out to p_in is already covered by previous quad's end,
            # but we add p_in as start of next quad
            # Actually we need to add straight edge point if needed
            # For simplicity, just add p_in
            poly.append(p_in)
        # quad from p_in to p_out via p_curr
        quad_pts = _sample_quad(p_in, p_curr, p_out, steps=quad_steps)
        # quad_pts[0] is p_in already added, so skip it
        poly.extend(quad_pts[1:])
    return poly


def render_png(pixel: int, *, transparent: bool = False, pad: float = 0.05, ss: int = 4) -> bytes:
    """Supersampled raster of exactly the SVG geometry above."""
    import io

    from PIL import Image, ImageDraw, ImageFilter

    n = int(pixel) * ss
    scale = n / BOX
    inner = int(n * (1.0 - 2 * pad))

    # gradient plate: violet -> violet2 -> cyan diagonal
    small = 256
    plate_small = Image.new("RGB", (small, small))
    px = plate_small.load()
    for y in range(small):
        for x in range(small):
            t = (x / (small - 1) + y / (small - 1)) / 2.0
            # ease
            t_e = t ** 0.85
            # two-stop gradient: violet to violet2 to cyan
            if t_e < 0.52:
                # violet -> violet2
                tt = t_e / 0.52
                r = int(round(VIOLET[0] + (VIOLET2[0] - VIOLET[0]) * tt))
                g = int(round(VIOLET[1] + (VIOLET2[1] - VIOLET[1]) * tt))
                b = int(round(VIOLET[2] + (VIOLET2[2] - VIOLET[2]) * tt))
            else:
                tt = (t_e - 0.52) / 0.48
                r = int(round(VIOLET2[0] + (CYAN[0] - VIOLET2[0]) * tt))
                g = int(round(VIOLET2[1] + (CYAN[1] - VIOLET2[1]) * tt))
                b = int(round(VIOLET2[2] + (CYAN[2] - VIOLET2[2]) * tt))
            px[x, y] = (r, g, b)
    plate = plate_small.resize((n, n), Image.BILINEAR)

    # second plate for tail (violet2 -> cyan)
    plate2_small = Image.new("RGB", (small, small))
    px2 = plate2_small.load()
    for y in range(small):
        for x in range(small):
            t = (x / (small - 1) + y / (small - 1)) / 2.0
            t **= 0.9
            r = int(round(VIOLET2[0] + (CYAN[0] - VIOLET2[0]) * t))
            g = int(round(VIOLET2[1] + (CYAN[1] - VIOLET2[1]) * t))
            b = int(round(VIOLET2[2] + (CYAN[2] - VIOLET2[2]) * t))
            px2[x, y] = (r, g, b)
    plate2 = plate2_small.resize((n, n), Image.BILINEAR)

    def shape_star(canvas, off):
        d = ImageDraw.Draw(canvas)
        # star polygon in 64-grid, then mapped with off+scale
        poly64 = _rounded_star_polygon(CX, CY, STAR_OUTER, STAR_INNER, STAR_POINTS, STAR_ROUND, quad_steps=8)

        def P(x, y):
            return (off + x * scale, off + y * scale)

        poly = [P(x, y) for x, y in poly64]
        d.polygon(poly, fill=255)

        # inner hole
        cx = off + CX * scale
        cy = off + CY * scale
        ri = R_INNER * scale
        d.ellipse([cx - ri, cy - ri, cx + ri, cy + ri], fill=0)
        # cut wedge
        d.polygon([P(x, y) for x, y in CUT], fill=0)

    def shape_tri(canvas, off):
        d = ImageDraw.Draw(canvas)

        def P(x, y):
            return (off + x * scale, off + y * scale)

        d.polygon([P(x, y) for x, y in TRI], fill=255)

    def shape_sparkle(canvas, off):
        d = ImageDraw.Draw(canvas)

        def P(x, y):
            return (off + x * scale, off + y * scale)

        d.polygon([P(x, y) for x, y in SPARKLE], fill=255)

    # main mask for star
    mask_star = Image.new("L", (n, n), 0)
    off = (n - inner) // 2
    shape_star(mask_star, off)
    mask_star = mask_star.filter(ImageFilter.GaussianBlur(radius=0.35 * ss))

    # triangle mask
    mask_tri = Image.new("L", (n, n), 0)
    shape_tri(mask_tri, off)
    mask_tri = mask_tri.filter(ImageFilter.GaussianBlur(radius=0.25 * ss))

    # sparkle mask
    mask_spark = Image.new("L", (n, n), 0)
    shape_sparkle(mask_spark, off)
    mask_spark = mask_spark.filter(ImageFilter.GaussianBlur(radius=0.15 * ss))

    # combine glyphs
    glyph = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    # star with main gradient
    glyph.paste(plate, (0, 0), mask_star)
    # triangle with second gradient (on top)
    tri_layer = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    tri_layer.paste(plate2, (0, 0), mask_tri)
    glyph = Image.alpha_composite(glyph, tri_layer)
    # sparkle in solid cyan (no gradient, just cyan)
    sparkle_layer = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    # cyan solid
    cyan_img = Image.new("RGBA", (n, n), CYAN + (255,))
    sparkle_layer.paste(cyan_img, (0, 0), mask_spark)
    glyph = Image.alpha_composite(glyph, sparkle_layer)

    if transparent:
        out = glyph
    else:
        alpha = Image.new("L", (n, n), 0)
        ImageDraw.Draw(alpha).rounded_rectangle([0, 0, n - 1, n - 1], radius=int(n * 0.225), fill=255)
        back = Image.composite(Image.new("RGBA", (n, n), BG + (255,)), Image.new("RGBA", (n, n), (0, 0, 0, 0)), alpha)
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

    (web / "logo.svg").write_text(svg_mark(background="#0A0A0F", radius=14), encoding="utf-8")
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
