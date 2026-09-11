#!/usr/bin/env python3
"""Qyro v6.4 logo generator: the chrome "ion Q" mark.

The mark is a thick chrome crescent ring (the Q bowl) lit by an electric-blue
rim glow, crossed by a sharp double-pointed blade (the Q tail), wrapped by a
thin tilted orbit ellipse and finished with a four-point spark. It sits on
deep space black. Chrome runs #FFFFFF -> #E4EDFF -> #A8C6F2 -> #5F8DD6 from the
upper-left light; the bloom around every shape is #1E5BFF.

Both the SVG and the PNGs are produced from the *same* sampled geometry below,
so the vector and raster marks can never drift apart.

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
# Measured off the source artwork: the bowl is a near-closed crescent whose
# tips taper to points at the lower-left opening, the blade runs from inside
# the bowl out past its lower-right, and the orbit is a thin tilted ellipse.
BOX = 64.0

# the Q bowl — a thick ring with a tapered crescent opening
Q_CX, Q_CY = 29.8, 30.2
Q_R_OUT = 15.8
Q_R_IN = 10.9
Q_GAP_A, Q_GAP_B = 82.0, 126.0      # degrees (screen space, +y down)

# the blade tail — a sharp lens crossing the bowl to the lower right
BLADE_A = (24.6, 26.4)
BLADE_B = (56.2, 53.6)
BLADE_BULGE = (4.75, 3.55)          # control offsets on the bright side
BLADE_THIN = (-1.75, -2.85)         # control offsets on the shadow side
BLADE_CUT = 1.15                    # dark gap punched where the blade crosses

# the orbit — a thin tilted ellipse passing behind the bowl
ORB_CX, ORB_CY = 32.2, 32.6
ORB_RX, ORB_RY = 23.4, 9.4
ORB_ROT = -15.0
ORB_W = 1.15

# the spark — a four-point star at the upper right
SPK_CX, SPK_CY = 48.0, 16.6
SPK_RV, SPK_RH, SPK_W = 5.6, 3.4, 0.82

# --- palette ---------------------------------------------------------------
CHROME_HI = (0xFF, 0xFF, 0xFF)
CHROME_MID = (0xE4, 0xED, 0xFF)
CHROME_LO = (0xA8, 0xC6, 0xF2)
CHROME_DEEP = (0x5F, 0x8D, 0xD6)
ION = (0x1E, 0x5B, 0xFF)            # the electric-blue rim glow
ION_LIT = (0x4D, 0x8C, 0xFF)
ICE = (0x9F, 0xC9, 0xFF)
BG = (0x01, 0x03, 0x0B)             # deep space black


def _hex(rgb) -> str:
    return "#%02X%02X%02X" % rgb


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


def ring_polygon(steps: int = 84):
    """The bowl: outer arc out, inner arc back, tapered to points at the tips."""
    a0 = Q_GAP_B
    a1 = Q_GAP_A + 360.0
    outer, inner = [], []
    for i in range(steps + 1):
        t = i / steps
        angle = math.radians(a0 + (a1 - a0) * t)
        # ease the inner radius out to the outer one near both ends -> sharp tips
        taper = min(1.0, min(t, 1.0 - t) / 0.11) ** 0.62
        r_in = Q_R_OUT - (Q_R_OUT - Q_R_IN) * taper
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        outer.append((Q_CX + Q_R_OUT * cos_a, Q_CY + Q_R_OUT * sin_a))
        inner.append((Q_CX + r_in * cos_a, Q_CY + r_in * sin_a))
    return outer + inner[::-1]


def _blade_sides(steps: int, grow: float = 0.0):
    """The two bezier flanks of the blade, optionally fattened by ``grow``."""
    ax, ay = BLADE_A
    bx, by = BLADE_B
    dx, dy = bx - ax, by - ay
    length = math.hypot(dx, dy)
    ux, uy = dx / length, dy / length
    px, py = -uy, ux
    # growing the lens also pushes its points outwards along the axis
    ax, ay = ax - ux * grow, ay - uy * grow
    bx, by = bx + ux * grow, by + uy * grow

    def side(w1, w2, sign):
        w1, w2 = w1 + sign * grow, w2 + sign * grow
        c1 = (ax + ux * length * 0.30 + px * w1, ay + uy * length * 0.30 + py * w1)
        c2 = (ax + ux * length * 0.63 + px * w2, ay + uy * length * 0.63 + py * w2)
        return _sample_cubic((ax, ay), c1, c2, (bx, by), steps)

    return side(*BLADE_BULGE, 1.0), side(*BLADE_THIN, -1.0)


def blade_polygon(steps: int = 26):
    """The tail: a lens with a point at each end, fatter on the lit side."""
    lit, shade = _blade_sides(steps)
    return lit + shade[::-1]


def blade_cut_polygon(steps: int = 26):
    """A slightly fatter blade — punched out of the bowl so the tail reads
    as passing in front of it, the way the source artwork does."""
    lit, shade = _blade_sides(steps, grow=BLADE_CUT)
    return lit + shade[::-1]


def orbit_rings(steps: int = 96):
    """The orbit: (outer, inner) ellipse rims, rotated, as an even-odd pair."""
    rot = math.radians(ORB_ROT)
    cos_r, sin_r = math.cos(rot), math.sin(rot)

    def rim(rx, ry):
        pts = []
        for i in range(steps + 1):
            a = 2.0 * math.pi * i / steps
            x, y = rx * math.cos(a), ry * math.sin(a)
            pts.append((ORB_CX + x * cos_r - y * sin_r, ORB_CY + x * sin_r + y * cos_r))
        return pts

    half = ORB_W / 2.0
    return rim(ORB_RX + half, ORB_RY + half), rim(ORB_RX - half, ORB_RY - half)


def sparkle_polygon():
    """The spark: a concave-waisted four-point star."""
    cx, cy, w = SPK_CX, SPK_CY, SPK_W
    return [
        (cx, cy - SPK_RV), (cx + w, cy - w), (cx + SPK_RH, cy), (cx + w, cy + w),
        (cx, cy + SPK_RV), (cx - w, cy + w), (cx - SPK_RH, cy), (cx - w, cy - w),
    ]


def _path_d(*loops) -> str:
    """One or more closed loops as SVG path data."""
    parts = []
    for loop in loops:
        parts.append(
            " ".join(
                f"{'M' if i == 0 else 'L'}{x:.2f} {y:.2f}"
                for i, (x, y) in enumerate(loop)
            )
            + " Z"
        )
    return " ".join(parts)


def svg_mark(*, size=None, background=None, radius=0, pad=0.0) -> str:
    """The inline SVG (also written to web/logo.svg)."""
    wh = f' width="{size}" height="{size}"' if size else ""
    bg = ""
    if background:
        rx = radius or 14
        bg = f'  <rect width="64" height="64" fill="{background}" rx="{rx}"/>\n'
    shift = ""
    if pad:
        k = 1.0 - pad
        shift = f' transform="translate({32 - 32 * k:.2f} {32 - 32 * k:.2f}) scale({k:.3f})"'

    # Sampling is kept modest here: at r≈16 the chord error of a 72-step arc is
    # ~0.02 design units, invisible at any size the mark is used, and it keeps
    # the inline copy in app.js small. The PNGs sample far denser (below).
    orbit_out, orbit_in = orbit_rings(72)
    d_ring = _path_d(ring_polygon(72))
    d_blade = _path_d(blade_polygon(20))
    d_cut = _path_d(blade_cut_polygon(20))
    d_orbit = _path_d(orbit_out, orbit_in)
    d_spark = _path_d(sparkle_polygon())

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"{wh}'
        f' role="img" aria-label="Qyro">\n'
        f'  <defs>\n'
        f'    <linearGradient id="qchrome" x1="14" y1="10" x2="50" y2="54" gradientUnits="userSpaceOnUse">\n'
        f'      <stop offset="0%" stop-color="{_hex(CHROME_HI)}"/>\n'
        f'      <stop offset="0.38" stop-color="{_hex(CHROME_MID)}"/>\n'
        f'      <stop offset="0.72" stop-color="{_hex(CHROME_LO)}"/>\n'
        f'      <stop offset="1" stop-color="{_hex(CHROME_DEEP)}"/>\n'
        f'    </linearGradient>\n'
        f'    <linearGradient id="qblade" x1="26" y1="28" x2="53" y2="50" gradientUnits="userSpaceOnUse">\n'
        f'      <stop offset="0%" stop-color="{_hex(CHROME_HI)}"/>\n'
        f'      <stop offset="0.55" stop-color="{_hex(CHROME_MID)}"/>\n'
        f'      <stop offset="1" stop-color="{_hex(CHROME_LO)}"/>\n'
        f'    </linearGradient>\n'
        f'    <linearGradient id="qorbit" x1="10" y1="40" x2="56" y2="26" gradientUnits="userSpaceOnUse">\n'
        f'      <stop offset="0%" stop-color="{_hex(ION)}"/>\n'
        f'      <stop offset="0.5" stop-color="{_hex(ICE)}"/>\n'
        f'      <stop offset="1" stop-color="{_hex(ION_LIT)}"/>\n'
        f'    </linearGradient>\n'
        f'    <filter id="qbloom" x="-45%" y="-45%" width="190%" height="190%">\n'
        f'      <feGaussianBlur stdDeviation="2.9"/>\n'
        f'    </filter>\n'
        f'    <filter id="qrim" x="-30%" y="-30%" width="160%" height="160%">\n'
        f'      <feGaussianBlur stdDeviation="0.9"/>\n'
        f'    </filter>\n'
        f'    <path id="qring" d="{d_ring}"/>\n'
        f'    <path id="qtail" d="{d_blade}"/>\n'
        f'    <path id="qorb" fill-rule="evenodd" d="{d_orbit}"/>\n'
        f'    <path id="qspk" d="{d_spark}"/>\n'
        f'    <mask id="qbladecut" maskUnits="userSpaceOnUse">\n'
        f'      <rect width="64" height="64" fill="#fff"/>\n'
        f'      <path d="{d_cut}" fill="#000"/>\n'
        f'    </mask>\n'
        f'    <g id="qback" mask="url(#qbladecut)">\n'
        f'      <use href="#qorb"/><use href="#qring"/>\n'
        f'    </g>\n'
        f'    <g id="qall">\n'
        f'      <use href="#qback"/><use href="#qtail"/><use href="#qspk"/>\n'
        f'    </g>\n'
        f'  </defs>\n'
        f'{bg}'
        f'  <g{shift}>\n'
        f'    <use href="#qall" fill="{_hex(ION)}" filter="url(#qbloom)" opacity="0.9"/>\n'
        f'    <use href="#qall" fill="{_hex(ION_LIT)}" filter="url(#qrim)" opacity="0.9"/>\n'
        f'    <g mask="url(#qbladecut)">\n'
        f'      <use href="#qorb" fill="url(#qorbit)"/>\n'
        f'      <use href="#qring" fill="url(#qchrome)"/>\n'
        f'    </g>\n'
        f'    <use href="#qtail" fill="url(#qblade)"/>\n'
        f'    <use href="#qspk" fill="{_hex(CHROME_HI)}"/>\n'
        f'  </g>\n'
        f'</svg>\n'
    )


def _plate(size, stops):
    """A diagonal gradient plate from (offset, rgb) stops."""
    from PIL import Image

    small = 256
    img = Image.new("RGB", (small, small))
    px = img.load()
    for y in range(small):
        for x in range(small):
            t = (x / (small - 1) + y / (small - 1)) / 2.0
            for i in range(len(stops) - 1):
                o0, c0 = stops[i]
                o1, c1 = stops[i + 1]
                if t <= o1 or i == len(stops) - 2:
                    tt = 0.0 if o1 <= o0 else min(1.0, max(0.0, (t - o0) / (o1 - o0)))
                    px[x, y] = tuple(
                        int(round(c0[k] + (c1[k] - c0[k]) * tt)) for k in range(3)
                    )
                    break
    return img.resize((size, size), Image.BILINEAR)


def render_png(pixel: int, *, transparent: bool = False, pad: float = 0.05, ss: int = 4) -> bytes:
    """Supersampled raster of exactly the SVG geometry above."""
    import io

    from PIL import Image, ImageDraw, ImageFilter

    n = int(pixel) * ss
    inner = n * (1.0 - 2 * pad)
    scale = inner / BOX
    off = (n - inner) / 2.0

    def P(pt):
        return (off + pt[0] * scale, off + pt[1] * scale)

    def mask_of(draw_fn) -> "Image.Image":
        m = Image.new("L", (n, n), 0)
        draw_fn(ImageDraw.Draw(m))
        return m

    cut = [P(p) for p in blade_cut_polygon(80)]

    def _ring(d):
        d.polygon([P(p) for p in ring_polygon(240)], fill=255)
        d.polygon(cut, fill=0)          # the tail passes in front of the bowl

    m_ring = mask_of(_ring)
    m_blade = mask_of(lambda d: d.polygon([P(p) for p in blade_polygon(80)], fill=255))
    m_spark = mask_of(lambda d: d.polygon([P(p) for p in sparkle_polygon()], fill=255))

    orbit_out, orbit_in = orbit_rings(320)

    def _orbit(d):
        d.polygon([P(p) for p in orbit_out], fill=255)
        d.polygon([P(p) for p in orbit_in], fill=0)
        d.polygon(cut, fill=0)

    m_orbit = mask_of(_orbit)

    soft = ImageFilter.GaussianBlur(radius=0.30 * ss)
    m_ring = m_ring.filter(soft)
    m_blade = m_blade.filter(soft)
    m_spark = m_spark.filter(soft)
    m_orbit = m_orbit.filter(soft)

    # every shape together, for the bloom passes
    m_all = Image.new("L", (n, n), 0)
    for m in (m_orbit, m_ring, m_blade, m_spark):
        m_all.paste(m, (0, 0), m)

    canvas = Image.new("RGBA", (n, n), (0, 0, 0, 0))

    def add_glow(mask, colour, blur, strength):
        g = mask.filter(ImageFilter.GaussianBlur(radius=blur * scale))
        g = g.point(lambda v, s=strength: int(min(255, v * s)))
        layer = Image.new("RGBA", (n, n), colour + (255,))
        layer.putalpha(g)
        return Image.alpha_composite(canvas, layer)

    canvas = add_glow(m_all, ION, 2.9, 0.80)
    canvas = add_glow(m_all, ION, 1.5, 0.70)
    canvas = add_glow(m_all, ION_LIT, 0.6, 0.85)

    chrome = _plate(n, [(0.0, CHROME_HI), (0.38, CHROME_MID), (0.72, CHROME_LO), (1.0, CHROME_DEEP)])
    blade_plate = _plate(n, [(0.0, CHROME_HI), (0.55, CHROME_MID), (1.0, CHROME_LO)])
    orbit_plate = _plate(n, [(0.0, ION), (0.5, ICE), (1.0, ION_LIT)])

    for plate, mask in ((orbit_plate, m_orbit), (chrome, m_ring),
                        (blade_plate, m_blade)):
        layer = Image.new("RGBA", (n, n), (0, 0, 0, 0))
        layer.paste(plate, (0, 0), mask)
        canvas = Image.alpha_composite(canvas, layer)

    spark_layer = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    spark_layer.paste(Image.new("RGBA", (n, n), CHROME_HI + (255,)), (0, 0), m_spark)
    canvas = Image.alpha_composite(canvas, spark_layer)

    if transparent:
        out = canvas
    else:
        alpha = Image.new("L", (n, n), 0)
        ImageDraw.Draw(alpha).rounded_rectangle(
            [0, 0, n - 1, n - 1], radius=int(n * 0.225), fill=255
        )
        back = Image.composite(
            Image.new("RGBA", (n, n), BG + (255,)),
            Image.new("RGBA", (n, n), (0, 0, 0, 0)),
            alpha,
        )
        out = Image.alpha_composite(back, canvas)
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

    (web / "logo.svg").write_text(svg_mark(background=_hex(BG), radius=14), encoding="utf-8")
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
