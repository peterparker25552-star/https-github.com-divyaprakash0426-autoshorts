"""Person / speaker tracking — the free, on-device "AI" that keeps the subject
inside the vertical frame instead of blindly cropping the middle of a 16:9 shot.

Design goals, in order:

1. **Zero new dependencies.** Every pixel operation runs inside ffmpeg's own
   optimised C filters. Python only reads a tiny heat map (a few hundred bytes
   per frame), so this is fast enough for a phone and needs no numpy, no model
   download and no network.
2. **Never break a render.** Every public entry point returns ``None`` on any
   failure; the caller then falls back to a static center crop.
3. **Smooth.** Raw per-frame detections are noisy. The path is low-pass
   filtered, velocity limited and then simplified into a handful of keyframes
   that ffmpeg interpolates linearly — the camera *glides*, it never snaps.

How the heat map is made (one ffmpeg pass over a 160 px proxy):

* ``skin``  — the classic YCbCr skin range (Cb 77..127, Cr 133..173) via ``geq``
* ``motion`` — ``tblend=difference`` thresholded, so anything that moves lights up
* both are summed and area-downscaled to a ``GRID`` of bytes per frame

A talking head is skin-coloured *and* moving, which is exactly what the sum
responds to; a static skin-coloured wall or a beige lower-third only lights up
once. The optional ``cv2`` backend (see :func:`face_targets`) upgrades this
with a real Haar cascade when OpenCV *and* a cascade file are both present.
"""
from __future__ import annotations

import hashlib
import json
import math
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import config

# Heat-map geometry. 160 px wide is enough to localise a subject to ~6% of the
# frame while keeping the geq pass cheap; the grid is what Python actually
# reads, so it stays small (20 x 11 bytes/frame for a 16:9 source).
PROXY_WIDTH = 160
GRID_X = 20
GRID_Y_MIN = 6
GRID_Y_MAX = 14

DEFAULT_FPS = 5.0
MAX_FRAMES = 420          # a 60 s clip at 5 fps = 300 frames; 420 is the cap
ANALYSIS_TIMEOUT = 600

# Camera behaviour, in *normalised* units (1.0 = the full source width).
MAX_PAN_SPEED = 0.55      # the crop may cross ~55% of the frame per second
DEADBAND = 0.030          # ignore subject drift below 3% of the width
SMOOTH_ALPHA = 0.70       # low-pass: 0 = frozen camera, 1 = glued to the subject
HOLD_FIRST_SECONDS = 0.6  # keep the opening framing still for a beat

# Crop sizing: the largest possible crop always has the output aspect, so the
# only freedom is *zooming in*. ``auto`` picks the tightest framing that still
# contains the subject and still has enough source pixels to stay sharp.
ZOOM_FRACTIONS = {"tight": 0.66, "normal": 0.82, "wide": 1.0}
ZOOM_CHOICES = ("auto", "tight", "normal", "wide")
DEFAULT_ZOOM = "auto"
ZOOM_FLOOR = 0.55
ZOOM_COVERAGE = 0.85      # keep this fraction of the subject mass inside


@dataclass
class TrackPath:
    """A subject-following crop plan for one source window.

    ``keyframes`` are ``(t, cx, cy)`` with ``t`` in *source-window* seconds
    (0 = the first frame of the window) and ``cx``/``cy`` the subject centre as
    a fraction of the source frame, so the caller can turn them into pixel
    offsets for any crop size.
    """

    keyframes: list[tuple[float, float, float]] = field(default_factory=list)
    zoom: float = 1.0
    backend: str = "heuristic"
    fps: float = DEFAULT_FPS
    frames: int = 0
    confidence: float = 0.0
    travel: float = 0.0          # total horizontal travel, normalised
    subject_width: float = 0.0   # typical subject width, normalised
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "keyframes": [[round(t, 4), round(x, 5), round(y, 5)]
                          for t, x, y in self.keyframes],
            "zoom": round(float(self.zoom), 4),
            "backend": self.backend,
            "fps": round(float(self.fps), 3),
            "frames": int(self.frames),
            "confidence": round(float(self.confidence), 4),
            "travel": round(float(self.travel), 4),
            "subject_width": round(float(self.subject_width), 4),
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TrackPath":
        frames = []
        for row in (data or {}).get("keyframes") or []:
            try:
                frames.append((float(row[0]), float(row[1]), float(row[2])))
            except (TypeError, ValueError, IndexError):
                continue
        if not frames:
            raise ValueError("track path has no keyframes")
        return cls(
            keyframes=frames,
            zoom=float((data or {}).get("zoom", 1.0)),
            backend=str((data or {}).get("backend") or "heuristic"),
            fps=float((data or {}).get("fps", DEFAULT_FPS)),
            frames=int((data or {}).get("frames", 0)),
            confidence=float((data or {}).get("confidence", 0.0)),
            travel=float((data or {}).get("travel", 0.0)),
            subject_width=float((data or {}).get("subject_width", 0.0)),
            note=str((data or {}).get("note") or ""),
        )


# --------------------------------------------------------------------------
# 1. The heat map (one ffmpeg pass; all pixel maths stay in ffmpeg)
# --------------------------------------------------------------------------
_SKIN_EXPR = "if(between(cb(X\\,Y)\\,77\\,127)*between(cr(X\\,Y)\\,133\\,173)\\,255\\,0)"
_MOTION_LUT = "if(gt(val\\,14)\\,255\\,0)"


def heat_filter(proxy_w: int = PROXY_WIDTH, grid_x: int = GRID_X,
                grid_y: int = 11) -> str:
    """The filtergraph that turns a video into a skin+motion heat map.

    Exposed for tests so the exact filter text is covered without ffmpeg.

    ``yuv444p`` is not cosmetic: ``geq``'s ``cb(X,Y)``/``cr(X,Y)`` read the
    chroma planes at their *native* (half) resolution, so on yuv420p the whole
    skin mask lands at half the correct position. Full-resolution chroma keeps
    the mask aligned with the luma plane.
    """
    return (
        f"scale={int(proxy_w)}:-2,fps=%FPS%,format=yuv444p,split=2[hm][dm];"
        "[dm]tblend=all_mode=difference,format=gray,"
        f"lut=y='{_MOTION_LUT}'[mm];"
        "[hm]geq=lum='" + _SKIN_EXPR + "':cb=128:cr=128,format=gray[sm];"
        "[sm][mm]blend=all_mode=addition,format=gray,"
        f"scale={int(grid_x)}:{int(grid_y)}:flags=area"
    )


def grid_for(src_w: int, src_h: int) -> tuple[int, int]:
    """Heat-map grid size for a source frame (always small, always >= 6 rows)."""
    src_w = max(2, int(src_w))
    src_h = max(2, int(src_h))
    gy = int(round(GRID_X * src_h / src_w))
    return GRID_X, max(GRID_Y_MIN, min(GRID_Y_MAX, gy))


def plan_fps(duration: float, fps: float = DEFAULT_FPS,
             max_frames: int = MAX_FRAMES) -> float:
    """Analysis fps: never above ``fps``, thinned for very long windows."""
    try:
        dur = max(0.5, float(duration))
        want = max(0.5, float(fps))
    except (TypeError, ValueError):
        return DEFAULT_FPS
    cap = max(0.5, float(max_frames) / dur)
    return round(min(want, cap), 3)


def heat_frames(
    src: Path | str,
    start: float,
    end: float,
    fps: float = DEFAULT_FPS,
    grid: tuple[int, int] = (GRID_X, 11),
    proxy_w: int = PROXY_WIDTH,
    timeout: int = ANALYSIS_TIMEOUT,
) -> list[bytes]:
    """One row-major ``bytes`` heat map per analysed frame (``[]`` on failure).

    Returns raw ffmpeg output split into ``grid_x * grid_y`` byte frames. Any
    failure — bad path, dead ffmpeg, timeout, short file — returns ``[]``.
    """
    dur = float(end) - float(start)
    if dur <= 0.2:
        return []
    grid_x, grid_y = int(grid[0]), int(grid[1])
    rate = plan_fps(dur, fps)
    graph = heat_filter(proxy_w, grid_x, grid_y).replace("%FPS%", f"{rate:g}")
    try:
        proc = subprocess.run(
            [
                config.FFMPEG_BIN, "-hide_banner", "-nostats", "-loglevel", "error",
                "-ss", f"{float(start):.3f}", "-t", f"{dur:.3f}", "-i", str(src),
                "-filter_complex", graph,
                "-f", "rawvideo", "-pix_fmt", "gray", "-",
            ],
            capture_output=True,
            timeout=timeout,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    raw = proc.stdout or b""
    per = grid_x * grid_y
    count = len(raw) // per
    return [raw[i * per:(i + 1) * per] for i in range(count)]


# --------------------------------------------------------------------------
# 2. Heat map -> per-frame subject centre (pure maths, unit-testable)
# --------------------------------------------------------------------------
def column_mass(frame: bytes, grid_x: int) -> list[float]:
    """Column sums of one heat map, as floats."""
    grid_y = len(frame) // max(1, grid_x)
    return [
        float(sum(frame[y * grid_x + x] for y in range(grid_y)))
        for x in range(grid_x)
    ]


def row_mass(frame: bytes, grid_x: int) -> list[float]:
    """Row sums of one heat map, as floats."""
    grid_y = len(frame) // max(1, grid_x)
    return [
        float(sum(frame[y * grid_x + x] for x in range(grid_x)))
        for y in range(grid_y)
    ]


def best_window(mass: list[float], width: float) -> tuple[float, float, float]:
    """Best ``width``-wide window over a 1-D mass profile.

    Returns ``(centre_in_units, mass, coverage)`` where units are 0..len(mass)
    and ``coverage`` is the fraction of the profile's total mass inside the
    window.

    Two details matter for accuracy:

    * the window slides in quarter cells, so the centre resolves finer than
      the heat-map grid;
    * ``centre`` is the **mass centroid inside the winning window**, not the
      window's geometric middle. When the crop is wider than the subject
      (the normal case) every fully-covering position ties on mass, and
      reporting the window middle biases the camera to one side by half the
      slack. The centroid is tie-free.
    """
    n = len(mass)
    if n == 0:
        return 0.0, 0.0, 0.0
    total = float(sum(mass))
    width = max(1.0, min(float(width), float(n)))
    best_mass = -1.0
    best_centre = n / 2.0
    best_moment = 0.0
    step = 0.25
    positions = int((n - width) / step) + 1
    for i in range(max(1, positions)):
        left = i * step
        right = left + width
        lo = int(math.floor(left))
        hi = int(math.ceil(right))
        value = 0.0
        moment = 0.0
        for idx in range(lo, min(hi, n)):
            # partial cells at both edges keep the sliding window honest
            overlap = min(right, idx + 1.0) - max(left, float(idx))
            if overlap > 0:
                weight = mass[idx] * overlap
                value += weight
                moment += weight * (idx + 0.5)
        if value > best_mass:
            best_mass = value
            best_moment = moment
            best_centre = (left + right) / 2.0
    if best_mass > 0:
        best_centre = best_moment / best_mass
    return best_centre, max(0.0, best_mass), (
        best_mass / total if total > 0 else 0.0
    )


def heat_targets(
    frames: list[bytes],
    grid: tuple[int, int],
    crop_frac_x: float,
    crop_frac_y: float,
    fps: float,
    vertical_prior: bool = True,
) -> tuple[list[tuple[float, float, float]], float, float]:
    """Per-frame subject centres from the heat maps.

    Returns ``(targets, confidence, subject_width)`` where each target is
    ``(t, cx, cy)`` in *window* seconds and normalised frame coordinates, and
    ``subject_width`` is the typical width of the hot region (0..1).

    Frames with no signal at all are skipped — a black frame must not drag the
    camera anywhere, the smoother simply holds the last known position.
    """
    grid_x, grid_y = int(grid[0]), int(grid[1])
    if not frames or grid_x < 2 or grid_y < 2:
        return [], 0.0, 0.0
    win_x = max(1.0, min(float(grid_x), float(crop_frac_x) * grid_x))
    win_y = max(1.0, min(float(grid_y), float(crop_frac_y) * grid_y))
    targets: list[tuple[float, float, float]] = []
    widths: list[float] = []
    coverages: list[float] = []
    for index, frame in enumerate(frames):
        cols = column_mass(frame, grid_x)
        rows = row_mass(frame, grid_x)
        peak = max(cols) if cols else 0.0
        if peak <= 0.0:
            continue
        cx_cells, mass, coverage = best_window(cols, win_x)
        cy_cells, _, _ = best_window(rows, win_y)
        if vertical_prior:
            # a person's mass sits above the middle; nudge the vertical centre
            # up a little so a desk or a lower-third never wins the crop
            rows_biased = [
                value * (1.0 + 0.35 * (0.5 - y / max(1, grid_y - 1)))
                for y, value in enumerate(rows)
            ]
            cy_cells, _, _ = best_window(rows_biased, win_y)
        targets.append((
            index / max(0.5, float(fps)),
            min(1.0, max(0.0, cx_cells / grid_x)),
            min(1.0, max(0.0, cy_cells / grid_y)),
        ))
        widths.append(_hot_width(cols, peak))
        coverages.append(coverage)
    if not targets:
        return [], 0.0, 0.0
    # confidence: how much of the frame's signal the subject window owns.
    # Two people at opposite edges halve it, which is exactly the case where
    # the caller should prefer a wider crop.
    spread = sorted(coverages)
    median_coverage = spread[len(spread) // 2]
    confidence = max(0.0, min(1.0, median_coverage * 1.2))
    widths.sort()
    subject_width = widths[len(widths) // 2] / max(1, grid_x) if widths else 0.0
    return targets, round(confidence, 4), round(subject_width, 4)


def _hot_width(cols: list[float], peak: float) -> float:
    """Width (in cells) of the region holding >= 35% of the column peak."""
    if peak <= 0:
        return 0.0
    return float(sum(1 for value in cols if value >= peak * 0.35))


# --------------------------------------------------------------------------
# 3. Smoothing: a camera that glides instead of snapping
# --------------------------------------------------------------------------
def smooth_targets(
    targets: list[tuple[float, float, float]],
    max_speed: float = MAX_PAN_SPEED,
    deadband: float = DEADBAND,
    alpha: float = SMOOTH_ALPHA,
    hold: float = HOLD_FIRST_SECONDS,
) -> list[tuple[float, float, float]]:
    """Low-pass + velocity-limit a raw detection path.

    * ``alpha`` blends the raw subject centre into the camera position, which
      removes single-frame jitter;
    * ``max_speed`` caps how fast the crop may travel (normalised units per
      second) so a subject that jumps across the frame is *followed*, not
      teleported after;
    * ``deadband`` freezes the camera while the subject only drifts a little —
      this is what stops the "breathing frame" look;
    * ``hold`` keeps the very first framing still, so a short never opens with
      a pan.
    """
    if not targets:
        return []
    if len(targets) == 1:
        return [targets[0]]
    a = min(1.0, max(0.05, float(alpha)))
    speed = max(0.02, float(max_speed))
    band = max(0.0, float(deadband))
    out: list[tuple[float, float, float]] = []
    # clamp the seed too: a garbage first detection must not put the crop
    # outside the frame before the smoother has had a chance to move
    cx = min(1.0, max(0.0, float(targets[0][1])))
    cy = min(1.0, max(0.0, float(targets[0][2])))
    prev_t = float(targets[0][0])
    for index, (t, tx, ty) in enumerate(targets):
        dt = max(0.0, float(t) - prev_t)
        prev_t = float(t)
        if index == 0:
            out.append((float(t), cx, cy))
            continue
        if float(t) <= float(hold):
            # hold the opening framing still so a short never opens on a pan
            out.append((float(t), cx, cy))
            continue
        # low-pass toward the target, then clamp the travel to max_speed * dt
        want_x = cx + (float(tx) - cx) * a
        want_y = cy + (float(ty) - cy) * a
        limit = speed * dt
        step_x = want_x - cx
        if abs(step_x) > limit:
            step_x = math.copysign(limit, step_x)
        step_y = want_y - cy
        if abs(step_y) > limit:
            step_y = math.copysign(limit, step_y)
        # deadband: drop moves too small to be worth the viewer's attention
        if abs(step_x) < band * 0.5:
            step_x = 0.0
        if abs(step_y) < band * 0.5:
            step_y = 0.0
        cx = min(1.0, max(0.0, cx + step_x))
        cy = min(1.0, max(0.0, cy + step_y))
        out.append((float(t), cx, cy))
    return out


def simplify_path(
    points: list[tuple[float, float, float]],
    epsilon: float,
    max_points: int = 64,
) -> list[tuple[float, float, float]]:
    """Ramer–Douglas–Peucker on the (t, cx) plane.

    The smoothed path is dense (5 points/second); ffmpeg gets a short list of
    keyframes it interpolates linearly between, so this trades a few hundredths
    of a pixel for a dramatically shorter filter expression. ``max_points``
    hard-caps the nesting depth of the generated expression.
    """
    pts = [(float(t), float(x), float(y)) for t, x, y in (points or [])]
    if len(pts) <= 2:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        lo, hi = stack.pop()
        if hi - lo < 2:
            continue
        t0, x0, _ = pts[lo]
        t1, x1, _ = pts[hi]
        span = t1 - t0
        worst, worst_index = 0.0, -1
        for i in range(lo + 1, hi):
            t, x, _ = pts[i]
            if span <= 0:
                distance = abs(x - x0)
            else:
                predicted = x0 + (x1 - x0) * (t - t0) / span
                distance = abs(x - predicted)
            if distance > worst:
                worst, worst_index = distance, i
        if worst > float(epsilon) and worst_index > 0:
            keep[worst_index] = True
            stack.append((lo, worst_index))
            stack.append((worst_index, hi))
    result = [p for p, flag in zip(pts, keep) if flag]
    # Hard cap: keep the biggest jumps, always including both ends.
    while len(result) > max(2, int(max_points)):
        gaps = [
            (abs(result[i + 1][1] - result[i][1]), i)
            for i in range(1, len(result) - 2)
        ]
        if not gaps:
            break
        gaps.sort()
        result.pop(gaps[0][1] + 1)
    return result


def path_travel(points: list[tuple[float, float, float]]) -> float:
    """Total horizontal travel of a path, in normalised units."""
    if len(points) < 2:
        return 0.0
    return round(
        sum(abs(points[i + 1][1] - points[i][1]) for i in range(len(points) - 1)), 4
    )


# --------------------------------------------------------------------------
# 4. Zoom: how tight should the crop be?
# --------------------------------------------------------------------------
def choose_zoom(
    mode: str,
    subject_width: float,
    crop_frac_x: float,
    src_w: int,
    out_w: int,
    max_upscale: float = 2.2,
) -> tuple[float, str]:
    """Crop-size fraction (of the largest possible crop) and a human note.

    A 9:16 short cut from a 16:9 frame is *always* an upscale, so ``auto``
    measures how much upscaling the widest crop already needs and refuses to
    zoom in past ``max_upscale`` total. On a 1080p source that still leaves a
    modest punch-in; on 4K it leaves a real one. Never upscales into mush.
    """
    mode = str(mode or "auto").strip().lower()
    if mode in ZOOM_FRACTIONS:
        return ZOOM_FRACTIONS[mode], f"zoom={mode}"
    if not crop_frac_x or crop_frac_x <= 0 or src_w <= 0 or out_w <= 0:
        return 1.0, "zoom=auto (no crop freedom)"
    widest_crop_px = float(src_w) * float(crop_frac_x)
    base_upscale = float(out_w) / max(1.0, widest_crop_px)
    floor = max(ZOOM_FLOOR, min(1.0, base_upscale / max(0.1, float(max_upscale))))
    # how much of the widest crop the subject already fills
    fill = 0.0
    if subject_width > 0:
        fill = min(1.0, float(subject_width) / max(1e-6, float(crop_frac_x)))
    if fill <= 0.05:
        return 1.0, "zoom=auto (no subject found)"
    wanted = max(ZOOM_FLOOR, min(1.0, fill / ZOOM_COVERAGE * 0.9))
    zoom = max(floor, min(wanted, 1.0))
    return round(zoom, 3), (
        f"zoom=auto (subject fills {fill:.0%} of the crop, "
        f"{base_upscale / zoom:.2f}x upscale)"
    )


# --------------------------------------------------------------------------
# 5. Optional OpenCV upgrade (real Haar cascade face detection)
# --------------------------------------------------------------------------
def cv2_available() -> bool:
    """True when OpenCV imports *and* a face cascade file can be found."""
    return bool(cascade_path())


def cascade_path() -> str:
    """Path to a Haar cascade XML, or ``""`` when none is installed.

    OpenCV >= 5 stopped bundling the cascades, so the search order is: the
    wheel's own data dir, then Qyro's model dir, then the user's data dir.
    """
    names = ("haarcascade_frontalface_default.xml", "haarcascade_frontalface_alt2.xml")
    roots: list[Path] = []
    try:
        import cv2  # type: ignore

        candidate = getattr(getattr(cv2, "data", None), "haarcascades", "")
        if candidate:
            roots.append(Path(str(candidate)))
    except Exception:
        pass
    roots.append(config.MODELS_DIR)
    roots.append(config.DATA_DIR / "models")
    for root in roots:
        for name in names:
            path = root / name
            try:
                if path.is_file() and path.stat().st_size > 10_000:
                    return str(path)
            except Exception:
                continue
    return ""


def face_targets(
    src: Path | str,
    start: float,
    end: float,
    src_w: int,
    src_h: int,
    fps: float = DEFAULT_FPS,
    proxy_w: int = 320,
) -> tuple[list[tuple[float, float, float]], float]:
    """Face-centre targets from a Haar cascade. ``([], 0.0)`` on any failure."""
    cascade = cascade_path()
    if not cascade or src_w <= 0 or src_h <= 0:
        return [], 0.0
    dur = float(end) - float(start)
    if dur <= 0.2:
        return [], 0.0
    try:
        import cv2  # type: ignore
        import numpy  # type: ignore

        detector = cv2.CascadeClassifier(cascade)
        if detector.empty():
            return [], 0.0
        rate = plan_fps(dur, fps)
        proxy_w = max(160, int(proxy_w))
        proxy_h = max(2, int(round(proxy_w * src_h / src_w)) // 2 * 2)
        proc = subprocess.run(
            [
                config.FFMPEG_BIN, "-hide_banner", "-nostats", "-loglevel", "error",
                "-ss", f"{float(start):.3f}", "-t", f"{dur:.3f}", "-i", str(src),
                "-vf", f"scale={proxy_w}:{proxy_h},fps={rate:g},format=bgr24",
                "-f", "rawvideo", "-pix_fmt", "bgr24", "-",
            ],
            capture_output=True,
            timeout=ANALYSIS_TIMEOUT,
        )
        if proc.returncode != 0:
            return [], 0.0
        per = proxy_w * proxy_h * 3
        raw = proc.stdout or b""
        count = len(raw) // per
        targets: list[tuple[float, float, float]] = []
        for index in range(count):
            frame = numpy.frombuffer(
                raw[index * per:(index + 1) * per], dtype=numpy.uint8
            ).reshape(proxy_h, proxy_w, 3)
            faces = detector.detectMultiScale(
                frame, scaleFactor=1.2, minNeighbors=5,
                minSize=(max(20, proxy_w // 14), max(20, proxy_w // 14)),
            )
            if len(faces) == 0:
                continue
            # biggest face = the person we should be framing
            x, y, w, h = max(faces, key=lambda box: int(box[2]) * int(box[3]))
            targets.append((
                index / max(0.5, rate),
                min(1.0, max(0.0, (x + w / 2.0) / proxy_w)),
                # frame the chest, not the forehead: sit the centre lower
                min(1.0, max(0.0, (y + h * 1.9) / proxy_h)),
            ))
        if not targets:
            return [], 0.0
        return targets, min(1.0, len(targets) / max(1.0, count))
    except Exception:
        return [], 0.0


# --------------------------------------------------------------------------
# 6. Cache (re-renders must be instant)
# --------------------------------------------------------------------------
def _cache_key(src: Path, start: float, end: float, fps: float,
               zoom: str, mode: str) -> Path:
    try:
        stat = src.stat()
        stamp = f"{stat.st_size}-{int(stat.st_mtime)}"
    except Exception:
        stamp = "nostat"
    digest = hashlib.sha1(
        f"{src.name}|{stamp}|{float(start):.2f}|{float(end):.2f}|"
        f"{float(fps):.2f}|{zoom}|{mode}".encode("utf-8")
    ).hexdigest()[:24]
    return config.TRACK_CACHE_DIR / f"track-{digest}.json"


def load_cache(path: Path) -> TrackPath | None:
    try:
        return TrackPath.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return None


def save_cache(path: Path, plan: TrackPath) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(plan.as_dict()), encoding="utf-8")
    except Exception:
        pass


# --------------------------------------------------------------------------
# 7. The public entry point
# --------------------------------------------------------------------------
def track_window(
    src: Path | str,
    start: float,
    end: float,
    src_w: int,
    src_h: int,
    out_w: int,
    out_h: int,
    crop_frac_x: float,
    crop_frac_y: float,
    mode: str = "auto",
    zoom: str = DEFAULT_ZOOM,
    fps: float = DEFAULT_FPS,
    use_cache: bool = True,
) -> TrackPath | None:
    """Track the subject across ``[start, end]`` — ``None`` on any failure.

    ``mode`` is ``auto`` (best available backend), ``vision`` (heat map only,
    no OpenCV) or ``off``. ``crop_frac_x``/``crop_frac_y`` say how much of the
    source frame the biggest possible crop already covers, so the tracker knows
    how much freedom it has.
    """
    src = Path(src)
    if str(mode).strip().lower() == "off":
        return None
    if src_w <= 0 or src_h <= 0 or out_w <= 0 or out_h <= 0:
        return None
    if float(crop_frac_x) >= 0.999 and float(crop_frac_y) >= 0.999:
        return None  # nothing to track: the crop is already the whole frame
    cache_path = _cache_key(src, start, end, fps, zoom, mode)
    if use_cache:
        cached = load_cache(cache_path)
        if cached and cached.keyframes:
            return cached

    targets: list[tuple[float, float, float]] = []
    backend = "heuristic"
    confidence = 0.0
    subject_width = 0.0
    if str(mode).strip().lower() in ("auto", "face"):
        targets, confidence = face_targets(src, start, end, src_w, src_h, fps)
        if targets:
            backend = "haar"
    if not targets:
        grid = grid_for(src_w, src_h)
        rate = plan_fps(float(end) - float(start), fps)
        frames = heat_frames(src, start, end, rate, grid)
        if not frames:
            return None
        targets, confidence, subject_width = heat_targets(
            frames, grid, crop_frac_x, crop_frac_y, rate
        )
    if not targets:
        return None

    smooth = smooth_targets(targets)
    if not smooth:
        return None
    fraction, note = choose_zoom(
        zoom, subject_width if backend == "heuristic" else 0.30,
        crop_frac_x, src_w, out_w,
    )
    keyframes = simplify_path(smooth, epsilon=0.006, max_points=64)
    if len(keyframes) < 2:
        keyframes = [smooth[0], smooth[-1]]
    plan = TrackPath(
        keyframes=keyframes,
        zoom=fraction,
        backend=backend,
        fps=plan_fps(float(end) - float(start), fps),
        frames=len(targets),
        confidence=confidence,
        travel=path_travel(smooth),
        subject_width=subject_width,
        note=note,
    )
    if use_cache:
        save_cache(cache_path, plan)
    return plan
