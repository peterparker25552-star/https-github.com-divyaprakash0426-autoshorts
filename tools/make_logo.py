#!/usr/bin/env python3
"""Qyro v6.2 logo generator: an original six-petal orbit mark.

The mark uses a soft six-fold petal rhythm as a reference to friendly AI
identities, but the open lower-right orbit, curved tail and cyan cross-spark
are original Qyro geometry. It is painted with the brand gradient
#7C3AED -> #C084FC -> #22D3EE on near-black #0A0A0F.

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

# --- geometry on a 64x64 design grid ---------------------------------------
# An original six-petal orbit mark: soft organic petals reference the warmth
# of modern AI identities without copying any existing logo, while the open
# lower-right orbit makes the Q and gives the mark its own silhouette.
BOX = 64.0
CX, CY = 32.0, 32.0
PETAL_PATH = "M32 30.5C26.8 27.1 24.8 18.4 29.1 8.9C30.2 6.4 33.8 6.4 34.9 8.9C39.2 18.4 37.2 27.1 32 30.5Z"
PETAL_CURVE = (
    (32.0, 30.5), (26.8, 27.1), (24.8, 18.4), (29.1, 8.9),
    (30.2, 6.4), (33.8, 6.4), (34.9, 8.9),
    (39.2, 18.4), (37.2, 27.1), (32.0, 30.5),
)
R_INNER = 7.2
CUT = [(36.0, 33.0), (47.0, 36.0), (58.0, 45.0), (68.0, 57.0), (59.0, 68.0), (38.0, 49.0)]
ORBIT = [(38.0, 39.0), (44.0, 42.0), (50.0, 48.0), (57.0, 57.0)]
SPARKLE = [(53.5, 8.2), (54.2, 11.1), (57.1, 11.8), (54.2, 12.5), (53.5, 15.4), (52.8, 12.5), (49.9, 11.8), (52.8, 11.1)]

VIOLET = (0x7C, 0x3A, 0xED)
VIOLET2 = (0xC0, 0x84, 0xFC)
CYAN = (0x22, 0xD3, 0xEE)
BG = (0x0A, 0x0A, 0x0F)


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
        rx = radius or 14
        bg = f'<rect width="64" height="64" fill="{background}" rx="{rx}"/>'
    shift = ""
    if pad:
        k = 1.0 - pad
        shift = f' transform="translate({32 - 32 * k:.2f} {32 - 32 * k:.2f}) scale({k:.3f})"'
    uses = "\n".join(
        f'      <use href="#qpetal" transform="rotate({angle} 32 32)"/>'
        for angle in (0, 60, 120, 180, 240, 300)
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"{wh}'
        f' role="img" aria-label="Qyro">\n'
        f'  <defs>\n'
        f'    <linearGradient id="qg" x1="8" y1="6" x2="56" y2="58" gradientUnits="userSpaceOnUse">\n'
        f'      <stop offset="0%" stop-color="#7C3AED"/>\n'
        f'      <stop offset="0.48" stop-color="#C084FC"/>\n'
        f'      <stop offset="1" stop-color="#22D3EE"/>\n'
        f'    </linearGradient>\n'
        f'    <linearGradient id="qg2" x1="36" y1="38" x2="58" y2="58" gradientUnits="userSpaceOnUse">\n'
        f'      <stop offset="0%" stop-color="#C084FC"/>\n'
        f'      <stop offset="1" stop-color="#22D3EE"/>\n'
        f'    </linearGradient>\n'
        f'    <path id="qpetal" d="{PETAL_PATH}"/>\n'
        f'    <mask id="qcut" maskUnits="userSpaceOnUse">\n'
        f'      <rect width="64" height="64" fill="#fff"/>\n'
        f'      <path d="{_d(CUT)}" fill="#000"/>\n'
        f'      <circle cx="32" cy="32" r="{R_INNER}" fill="#000"/>\n'
        f'    </mask>\n'
        f'  </defs>\n'
        f'  {bg}\n'
        f'  <g{shift}>\n'
        f'    <g mask="url(#qcut)">\n'
        f'      <g fill="url(#qg)">\n{uses}\n      </g>\n'
        f'    </g>\n'
        f'    <path d="M38 39C44 42 50 48 57 57" fill="none" stroke="url(#qg2)" stroke-width="4.7" stroke-linecap="round"/>\n'
        f'    <path d="M52.6 52.2L59.2 58L51.8 56.1" fill="url(#qg2)"/>\n'
        f'    <path d="M53.5 8.2V15.4M49.9 11.8H57.1" stroke="#67E8F9" stroke-width="1.35" stroke-linecap="round"/>\n'
        f'    <circle cx="53.5" cy="11.8" r="1.55" fill="#22D3EE"/>\n'
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


def _sample_cubic(p0, p1, p2, p3, steps=10):
    """Sample points along a cubic bezier."""
    points = []
    for i in range(steps + 1):
        t = i / steps
        inv = 1 - t
        x = (inv ** 3) * p0[0] + 3 * (inv ** 2) * t * p1[0] + 3 * inv * (t ** 2) * p2[0] + (t ** 3) * p3[0]
        y = (inv ** 3) * p0[1] + 3 * (inv ** 2) * t * p1[1] + 3 * inv * (t ** 2) * p2[1] + (t ** 3) * p3[1]
        points.append((x, y))
    return points


def _rotate(point, angle):
    radians = math.radians(angle)
    x, y = point[0] - CX, point[1] - CY
    return (
        CX + x * math.cos(radians) - y * math.sin(radians),
        CY + x * math.sin(radians) + y * math.cos(radians),
    )


def _petal_polygon(angle):
    p = PETAL_CURVE
    points = []
    points += _sample_cubic(p[0], p[1], p[2], p[3], 12)
    points += _sample_cubic(p[3], p[4], p[5], p[6], 8)[1:]
    points += _sample_cubic(p[6], p[7], p[8], p[9], 12)[1:]
    return [_rotate(point, angle) for point in points]


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

    def P(x, y, off):
        return (off + x * scale, off + y * scale)

    def shape_petals(canvas, off):
        d = ImageDraw.Draw(canvas)
        for angle in (0, 60, 120, 180, 240, 300):
            d.polygon([P(x, y, off) for x, y in _petal_polygon(angle)], fill=255)
        cx = off + CX * scale
        cy = off + CY * scale
        ri = R_INNER * scale
        d.ellipse([cx - ri, cy - ri, cx + ri, cy + ri], fill=0)
        d.polygon([P(x, y, off) for x, y in CUT], fill=0)

    def shape_orbit(canvas, off):
        d = ImageDraw.Draw(canvas)
        points = _sample_cubic(*ORBIT, steps=24)
        xy = [P(x, y, off) for x, y in points]
        width = max(1, int(4.7 * scale))
        d.line(xy, fill=255, width=width, joint="curve")
        r = width / 2
        x, y = xy[-1]
        d.ellipse([x - r, y - r, x + r, y + r], fill=255)
        d.polygon([P(x, y, off) for x, y in [(52.6, 52.2), (59.2, 58.0), (51.8, 56.1)]], fill=255)

    def shape_sparkle(canvas, off):
        d = ImageDraw.Draw(canvas)
        d.polygon([P(x, y, off) for x, y in SPARKLE], fill=255)
        cx, cy = P(53.5, 11.8, off)
        r = max(1, 1.55 * scale)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)

    # main petal mask
    mask_star = Image.new("L", (n, n), 0)
    off = (n - inner) // 2
    shape_petals(mask_star, off)
    mask_star = mask_star.filter(ImageFilter.GaussianBlur(radius=0.35 * ss))

    # orbit-tail mask
    mask_tri = Image.new("L", (n, n), 0)
    shape_orbit(mask_tri, off)
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
