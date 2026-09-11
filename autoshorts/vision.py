"""Person / speaker tracking — the free, on-device "AI" that keeps the right
person inside the vertical frame instead of blindly cropping the middle of a
16:9 shot.

v0.6.1 — **follow the speaker, not just a person.** The v0.6.0 tracker picked
one subject per clip: the biggest face (which jumps between people whenever
someone leans closer to the camera) or the centre of the summed skin+motion
blob (which frames *nobody* in a two-person shot). Qyro now answers "who is
talking?" the cheap, offline way:

1. **Separate people.** The ffmpeg heat pass now emits *two* stacked maps per
   frame — a skin mask and a motion mask. Connected components on the skin
   mask yield up to ``TRACK_MAX_PERSONS`` distinct people per frame (a
   dilation pass keeps a face and its gesticulating hands together).
2. **Hear the voice.** One ``ebur128`` pass over the clip's own audio gives a
   momentary-loudness envelope; an adaptive threshold turns it into
   voice-active / voice-idle flags. No network, no model, no key.
3. **Score speakers.** A person who is talking moves (mouth, head, hands)
   *while the voice is active* and sits still between lines; a listener moves
   regardless. Each candidate therefore accumulates motion-during-speech vs
   motion-during-idle energy, and the tracker follows the winner. When the
   other person clearly takes the turn — bigger speech-correlated motion for
   ``SWITCH_HOLD`` seconds by at least ``SWITCH_RATIO`` — the camera glides
   across to them.
4. **Fall back sanely.** No audio (or no detectable speech) → the most
   persistently present person wins ("mainly present in the frame"). No
   OpenCV → the skin/motion path. No blobs at all → the old merged-heat
   single-subject behaviour. Every failure degrades one step, never to a
   broken render.

All the pixel work still runs inside ffmpeg's optimised C filters; Python only
reads a few hundred bytes of heat map per frame, so this stays fast enough for
a phone and needs no numpy, no model download and no network. The optional
``cv2`` backend upgrades candidate quality to real Haar face detections (with
speaker scoring on top) when OpenCV and a cascade file are installed.

Design goals, in order:

1. **Zero required dependencies.**
2. **Never break a render.** Every public entry point returns ``None`` on any
   failure; the caller then falls back to a static centre crop.
3. **Smooth.** Raw per-frame detections are low-pass filtered, velocity
   limited and simplified into a handful of keyframes ffmpeg interpolates —
   the camera *glides* between speakers, it never snaps.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import config

# Heat-map geometry. 160 px wide is enough to localise a subject to ~6% of the
# frame while keeping the geq pass cheap; the grid is what Python actually
# reads, so it stays small (20 x 11 skin cells + 20 x 11 motion cells per
# frame for a 16:9 source).
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

# Speaker scoring (v0.6.1): how much of a candidate's motion has to line up
# with the voice before the camera commits to them.
IDLE_WEIGHT = 0.6         # subtract this much uncorrelated (listener) motion
PRESENCE_WEIGHT = 0.1     # small tie-break for the person who fills the frame
PRESENCE_SCALE = 8.0      # skin covering 12% of the frame saturates presence


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
    switches: int = 0            # v0.6.1: how often the camera changed speaker

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
            "switches": int(self.switches),
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
            switches=int((data or {}).get("switches") or 0),
        )


# --------------------------------------------------------------------------
# 1. The heat map (one ffmpeg pass; all pixel maths stay in ffmpeg)
#
# v0.6.1 emits TWO stacked grids per frame: the skin mask (top) and the
# motion mask (bottom). The old single blended map could not tell "a person"
# from "movement anywhere", which is what let two speakers smear into one
# subject. ``heat_frames`` still returns one bytes-per-frame list; the frames
# are just twice as tall now.
# --------------------------------------------------------------------------
_SKIN_EXPR = "if(between(cb(X\\,Y)\\,77\\,127)*between(cr(X\\,Y)\\,133\\,173)\\,255\\,0)"
_MOTION_LUT = "if(gt(val\\,14)\\,255\\,0)"


def heat_filter(proxy_w: int = PROXY_WIDTH, grid_x: int = GRID_X,
                grid_y: int = 11) -> str:
    """The filtergraph that turns a video into skin + motion heat maps.

    Exposed for tests so the exact filter text is covered without ffmpeg.

    ``yuv444p`` is not cosmetic: ``geq``'s ``cb(X,Y)``/``cr(X,Y)`` read the
    chroma planes at their *native* (half) resolution, so on yuv420p the whole
    skin mask lands at half the correct position. Full-resolution chroma keeps
    the mask aligned with the luma plane. The two maps are downscaled
    separately and vstacked, so byte row ``y < grid_y`` of a frame is skin and
    ``y >= grid_y`` is motion.
    """
    return (
        f"scale={int(proxy_w)}:-2,fps=%FPS%,format=yuv444p,split=2[hm][dm];"
        "[dm]tblend=all_mode=difference,format=gray,"
        f"lut=y='{_MOTION_LUT}'[mm];"
        "[hm]geq=lum='" + _SKIN_EXPR + "':cb=128:cr=128,format=gray[sm];"
        f"[sm]scale={int(grid_x)}:{int(grid_y)}:flags=area[sg];"
        f"[mm]scale={int(grid_x)}:{int(grid_y)}:flags=area[mg];"
        "[sg][mg]vstack"
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

    Each frame holds ``grid_x * grid_y`` skin bytes followed by the same
    number of motion bytes (see :func:`heat_filter`). Any failure — bad path,
    dead ffmpeg, timeout, short file — returns ``[]``.
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
    per = grid_x * grid_y * 2
    count = len(raw) // per
    return [raw[i * per:(i + 1) * per] for i in range(count)]


def split_planes(
    frames: list[bytes], grid_x: int, grid_y: int
) -> list[tuple[bytes, bytes]]:
    """Split two-plane heat frames into ``[(skin, motion)]`` pairs.

    Tolerant by design: a legacy single-plane frame (or a truncated tail
    frame) yields ``(skin, empty)`` so older callers keep working.
    """
    skin_size = int(grid_x) * int(grid_y)
    out: list[tuple[bytes, bytes]] = []
    for frame in frames or []:
        if len(frame) >= skin_size * 2:
            out.append((frame[:skin_size], frame[skin_size:skin_size * 2]))
        elif len(frame) >= skin_size:
            out.append((frame[:skin_size], b""))
        else:
            out.append((b"", b""))
    return out


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
    """Per-frame subject centres from *skin* heat maps.

    Returns ``(targets, confidence, subject_width)`` where each target is
    ``(t, cx, cy)`` in *window* seconds and normalised frame coordinates, and
    ``subject_width`` is the typical width of the hot region (0..1).

    This is the single-subject fallback: when the speaker tracker cannot
    separate people at all (e.g. heavy stylisation), the merged skin mass
    still deserves a camera. Frames with no signal are skipped — a black
    frame must not drag the camera anywhere, the smoother simply holds.
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
# 3. People: connected components on the skin mask (pure maths)
# --------------------------------------------------------------------------
@dataclass
class Person:
    """One candidate person in one analysed frame (all coords normalised)."""

    cx: float
    cy: float
    mass: float            # skin mass, fraction of the frame's cells
    width: float           # bounding-box width, normalised
    height: float          # bounding-box height, normalised
    x0: float = 0.0        # bounding box, normalised
    x1: float = 1.0
    y0: float = 0.0
    y1: float = 1.0
    energy: float = 0.0    # motion energy inside the box for THIS frame


def _label_blobs(
    cells: list[list[bool]], grid_x: int, grid_y: int
) -> list[list[tuple[int, int]]]:
    """4-connected components over a boolean grid (dilated once for labelling).

    The dilation keeps a face and its on-screen hands — or a head and the
    torso below it — in one component, so one human does not count as three
    "people". Mass and centroids are computed from the *original* cells only.
    """
    seen = [[False] * grid_x for _ in range(grid_y)]

    def dilated(y: int, x: int) -> bool:
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                yy, xx = y + dy, x + dx
                if 0 <= yy < grid_y and 0 <= xx < grid_x and cells[yy][xx]:
                    return True
        return False

    blobs: list[list[tuple[int, int]]] = []
    for y in range(grid_y):
        for x in range(grid_x):
            if seen[y][x] or not cells[y][x]:
                seen[y][x] = True
                continue
            blob: list[tuple[int, int]] = []
            stack = [(y, x)]
            seen[y][x] = True
            while stack:
                cy, cx = stack.pop()
                if cells[cy][cx]:
                    blob.append((cy, cx))
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    yy, xx = cy + dy, cx + dx
                    if (
                        0 <= yy < grid_y and 0 <= xx < grid_x
                        and not seen[yy][xx] and dilated(yy, xx)
                    ):
                        seen[yy][xx] = True
                        stack.append((yy, xx))
            real = [(cy, cx) for cy, cx in blob if cells[cy][cx]]
            if real:
                blobs.append(real)
    return blobs


def find_persons(
    skin: bytes, grid_x: int, grid_y: int
) -> list[Person]:
    """Distinct people in one skin mask — ``[]`` when nothing separates.

    A blob must own at least ``config.PERSON_MIN_MASS`` of the frame's total
    skin mass (so a stray lit cell is not a person) and no blob may cover the
    whole frame (a sepia backdrop is not a person either).
    """
    grid_x = max(2, int(grid_x))
    grid_y = max(2, int(grid_y))
    if not skin or len(skin) < grid_x * grid_y:
        return []
    peak = max(skin)
    if peak <= 0:
        return []
    threshold = max(24.0, 0.30 * peak)
    cells = [
        [skin[y * grid_x + x] >= threshold for x in range(grid_x)]
        for y in range(grid_y)
    ]
    total = sum(1 for row in cells for value in row if value)
    if total == 0:
        return []
    blobs = _label_blobs(cells, grid_x, grid_y)
    persons: list[Person] = []
    frame_cells = float(grid_x * grid_y)
    for blob in blobs:
        mass = len(blob)
        if mass < max(2.0, config.PERSON_MIN_MASS * total):
            continue
        xs = [x for _y, x in blob]
        ys = [y for y, _x in blob]
        w = (max(xs) - min(xs) + 1) / grid_x
        h = (max(ys) - min(ys) + 1) / grid_y
        if w > 0.95 and h > 0.95:
            continue  # the "person" is the whole frame — that is a backdrop
        cx = min(1.0, max(0.0, sum(x + 0.5 for _y, x in blob) / len(blob) / grid_x))
        cy = min(1.0, max(0.05, sum(y + 0.5 for y, _x in blob) / len(blob) / grid_y))
        persons.append(Person(
            cx=cx,
            cy=cy,
            mass=mass / frame_cells,
            width=w,
            height=h,
            x0=min(xs) / grid_x,
            x1=(max(xs) + 1) / grid_x,
            y0=min(ys) / grid_y,
            y1=(max(ys) + 1) / grid_y,
        ))
    # keep the most substantial candidates, biggest first
    persons.sort(key=lambda p: p.mass, reverse=True)
    return persons[: max(1, int(config.TRACK_MAX_PERSONS))]


def person_frames_from_heat(
    frames: list[bytes], grid: tuple[int, int]
) -> list[list[Person]]:
    """Per-frame ``Person`` lists (with motion energy) from two-plane maps.

    A person's ``energy`` is the mean motion value inside their (slightly
    widened) bounding box, 0..1 — the raw material for speaker scoring.
    """
    grid_x, grid_y = int(grid[0]), int(grid[1])
    pairs = split_planes(frames, grid_x, grid_y)
    out: list[list[Person]] = []
    for skin, motion in pairs:
        persons = find_persons(skin, grid_x, grid_y)
        if persons and motion:
            for person in persons:
                lo_x = max(0, int(person.x0 * grid_x) - 1)
                hi_x = min(grid_x - 1, int(person.x1 * grid_x))
                lo_y = max(0, int(person.y0 * grid_y) - 1)
                hi_y = min(grid_y - 1, int(person.y1 * grid_y))
                cells = max(1, (hi_x - lo_x + 1) * (hi_y - lo_y + 1))
                total = 0
                for y in range(lo_y, hi_y + 1):
                    row = y * grid_x
                    for x in range(lo_x, hi_x + 1):
                        total += motion[row + x]
                person.energy = min(1.0, total / cells / 255.0)
        out.append(persons)
    return out


# --------------------------------------------------------------------------
# 4. Voice activity from the clip's own audio (offline, free)
# --------------------------------------------------------------------------
_R128_FRAME = re.compile(r"pts_time:([-\d.]+)")
_R128_VALUE = re.compile(r"lavfi\.r128\.M=([-\d.eE]+|-inf|inf|nan)")


def voice_envelope(
    src: Path | str,
    start: float,
    end: float,
    grid: float = config.VOICE_GRID,
    timeout: int = 300,
) -> list[float]:
    """Momentary loudness (dB) on a fixed ``grid``-second lattice.

    Reads the window's own audio with ffmpeg's ``ebur128`` filter — the same
    trick the beat sync uses — resampled to one value per ``VOICE_GRID``
    seconds. Empty when there is no audio or ffmpeg fails; callers must treat
    that as "voice unknown", never as "silent".
    """
    dur = float(end) - float(start)
    if dur <= 0.2:
        return []
    try:
        proc = subprocess.run(
            [
                config.FFMPEG_BIN, "-hide_banner", "-nostats", "-loglevel", "info",
                # audio only: the loudness pass must not decode video
                "-vn", "-ss", f"{float(start):.3f}", "-t", f"{dur:.3f}",
                "-i", str(src),
                "-filter_complex",
                "ebur128=metadata=1:peak=none,ametadata=print:"
                "key=lavfi.r128.M:file=-",
                "-f", "null", "-",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception:
        return []
    samples: list[tuple[float, float]] = []
    current: float | None = None
    for line in (proc.stdout or "").splitlines() + (proc.stderr or "").splitlines():
        frame = _R128_FRAME.search(line)
        if frame:
            try:
                current = float(frame.group(1))
            except ValueError:
                current = None
            continue
        value = _R128_VALUE.search(line)
        if value and current is not None:
            try:
                level = float(value.group(1))
            except ValueError:
                level = -99.0
            if not math.isfinite(level):
                level = -99.0
            samples.append((current, level))
            current = None
    if not samples:
        return []
    grid = max(0.02, float(grid))
    t0 = min(t for t, _v in samples)
    n = int(math.floor((max(t for t, _v in samples) - t0) / grid)) + 1
    buckets: list[list[float]] = [[] for _ in range(max(1, n))]
    for t, v in samples:
        idx = min(len(buckets) - 1, max(0, int((t - t0) / grid)))
        buckets[idx].append(v)
    out: list[float] = []
    last = -99.0
    for bucket in buckets:
        if bucket:
            last = sum(bucket) / len(bucket)
        out.append(last)
    return out


def _run_smooth(flags: list[bool], min_on: int, min_off: int) -> list[bool]:
    """Drop speech runs shorter than ``min_on`` and gaps shorter than ``min_off``."""
    if not flags:
        return []
    out = list(flags)
    n = len(out)

    def runs(source: list[bool], value: bool) -> list[tuple[int, int]]:
        found: list[tuple[int, int]] = []
        start = None
        for i, flag in enumerate(source):
            if flag and start is None:
                start = i
            elif not flag and start is not None:
                found.append((start, i - 1))
                start = None
        if start is not None:
            found.append((start, n - 1))
        return found

    for a, b in runs(out, True):
        if b - a + 1 < max(1, min_on):
            for i in range(a, b + 1):
                out[i] = False
    for a, b in runs(out, False):
        if b - a + 1 < max(1, min_off):
            for i in range(a, b + 1):
                out[i] = True
    return out


def voice_activity(
    envelope: list[float],
    grid: float = config.VOICE_GRID,
    lo_rel: float = config.VOICE_LO_REL,  # unused since v0.6.1; kept for callers
    hi_rel: float = config.VOICE_HI_REL,
    dynamic_db: float = config.VOICE_DYNAMIC_DB,
    spread_db: float = config.VOICE_SPREAD_DB,
    floor_db: float = config.VOICE_FLOOR_DB,
) -> list[bool]:
    """Voice-active flags (True = someone is talking) from a loudness curve.

    The threshold hangs ``dynamic_db`` below the speech ceiling (the 90th
    percentile), which works whatever the clip's absolute level is. A plain
    percentile band fails exactly where it matters — a podcast clip is ~90%
    speech, so the 15th percentile lands *inside* speech and the spread looks
    flat — while speech-to-silence contrasts of 40+ dB are universal, so
    "ceiling minus 25 dB" separates word gaps (a few dB) from real pauses
    (tens of dB) on any recording. A genuinely flat curve (constant tone or
    constant noise) carries no pause information at all: it counts as
    "always voice" when audible, so the tracker follows the moving person.
    """
    values = [float(v) for v in (envelope or [])]
    n = len(values)
    if n == 0:
        return []
    # the floor only answers "is there any audible audio at all?"
    audible = [v for v in values if v > float(floor_db)]
    if len(audible) < max(3, int(0.03 * n)):
        return [False] * n
    ordered = sorted(values)
    hi = ordered[min(n - 1, int(float(hi_rel) * (n - 1)))]
    low = ordered[min(n - 1, int(0.02 * (n - 1)))]
    # flat = even the near-minimum sits at the ceiling: a constant tone or
    # constant noise, with no pause information at all. (The 15th percentile
    # would sit *inside* speech on a speech-dominated clip and misread it as
    # flat, which is exactly the podcast case this module is built for.)
    if hi - low < float(spread_db):
        return [True] * n if hi > float(floor_db) + 10.0 else [False] * n
    threshold = hi - float(dynamic_db)
    raw = [v > threshold for v in values]
    return _run_smooth(
        raw,
        min_on=int(round(0.25 / max(0.02, float(grid)))),
        min_off=int(round(0.30 / max(0.02, float(grid)))),
    )


def voice_flag_at(voice: list[bool], t: float, grid: float = config.VOICE_GRID) -> bool | None:
    """The voice flag at window time ``t`` — ``None`` when voice is unknown."""
    if not voice:
        return None
    index = int(round(float(t) / max(0.02, float(grid))))
    return voice[min(len(voice) - 1, max(0, index))]


# --------------------------------------------------------------------------
# 5. The speaker tracker: identity matching + speech-correlated scoring
# --------------------------------------------------------------------------
class _Track:
    """One person followed across frames."""

    __slots__ = (
        "x", "y", "width", "presence", "speak", "idle", "fresh",
        "misses", "age", "matched", "last_seen", "energy",
    )

    def __init__(self, person: Person) -> None:
        self.x = person.cx
        self.y = person.cy
        self.width = person.width
        self.presence = min(1.0, person.mass * PRESENCE_SCALE)
        self.speak = 0.0
        self.idle = 0.0
        self.fresh = True
        self.misses = 0
        self.age = 0
        self.matched = True
        self.last_seen = 0
        self.energy = person.energy

    def observe(self, person: Person) -> None:
        self.x = person.cx
        self.y = person.cy
        self.width = person.width
        self.energy = person.energy
        self.presence = (
            config.PRESENCE_EMA * self.presence
            + (1.0 - config.PRESENCE_EMA) * min(1.0, person.mass * PRESENCE_SCALE)
        )
        self.fresh = True
        self.matched = True
        self.misses = 0


@dataclass
class TrackerResult:
    """Output of :func:`run_speaker_tracker`."""

    targets: list[tuple[float, float, float]] = field(default_factory=list)
    confidence: float = 0.0
    subject_width: float = 0.0
    switches: int = 0
    voice_used: bool = False
    persons_seen: int = 0


def run_speaker_tracker(
    frames_persons: list[list[Person]],
    voice: list[bool] | None,
    fps: float,
    match_jump: float = config.TRACK_MATCH_JUMP,
    switch_ratio: float = config.SWITCH_RATIO,
    switch_hold: float = config.SWITCH_HOLD,
    max_miss: float = config.TRACK_MAX_MISS,
    idle_weight: float = IDLE_WEIGHT,
    presence_weight: float = PRESENCE_WEIGHT,
) -> TrackerResult:
    """Follow the person whose motion lines up with the voice.

    ``frames_persons[i]`` is the candidate list for frame ``i`` (each with its
    motion ``energy``); voice lookups come from :func:`voice_flag_at`. With no
    voice data the score degenerates to presence — the most persistently
    visible person wins, which is the best available reading of "mainly
    present in the frame".

    Switching is deliberately reluctant: a challenger must outscore the
    incumbent by ``switch_ratio`` for ``switch_hold`` seconds before the
    camera commits, so a nod or a laugh cannot steal the shot mid-sentence.
    """
    result = TrackerResult()
    tracks: list[_Track] = []
    incumbent: _Track | None = None
    challenger: _Track | None = None
    challenger_since = -1.0
    had_incumbent = False
    switches = 0
    fps = max(0.5, float(fps))
    dt = 1.0 / fps
    max_miss_frames = max(1, int(round(float(max_miss) / dt)))
    voice_used = bool(voice) and any(voice)
    widths: list[float] = []

    def score(track: _Track) -> float:
        if voice_used:
            return (
                track.speak - float(idle_weight) * track.idle
                + float(presence_weight) * track.presence
            )
        return track.presence

    for index, persons in enumerate(list(frames_persons)):
        t = index / fps
        persons = list(persons)          # never mutate the caller's frame
        for track in tracks:
            track.fresh = False
            track.matched = False
            track.misses += 1
        # greedy nearest matching, longest-lived tracks first
        for track in sorted(tracks, key=lambda tr: tr.misses):
            best, best_dist = None, float(match_jump)
            for person in persons:
                dist = math.hypot(person.cx - track.x, (person.cy - track.y) * 0.6)
                if dist <= best_dist:
                    best, best_dist = person, dist
            if best is not None:
                track.observe(best)
                persons.remove(best)
        for person in persons:                      # leftovers are new people
            tracks.append(_Track(person))
        tracks = [tr for tr in tracks if tr.misses <= max_miss_frames]

        # update the speech/idle motion EMAs for every track seen this frame.
        # The *inactive* counter decays toward zero on every observation, so a
        # person who stops talking loses their speaker score within ~2 s and
        # the camera can hand over to the person who just started.
        flag = voice_flag_at(voice or [], t) if voice is not None else None
        if flag is not None:
            alpha = config.SPEAK_EMA
            for track in tracks:
                if not track.matched:
                    continue
                if flag:
                    track.speak = alpha * track.speak + (1.0 - alpha) * track.energy
                    track.idle = alpha * track.idle
                else:
                    track.idle = alpha * track.idle + (1.0 - alpha) * track.energy
                    track.speak = alpha * track.speak

        fresh = [tr for tr in tracks if tr.matched]
        if not fresh:
            if incumbent is not None and incumbent.misses > max_miss_frames:
                incumbent = None
                challenger = None
                challenger_since = -1.0
            continue

        best = max(fresh, key=score)
        if incumbent is None:
            if had_incumbent:
                # a speaker we were following is gone and someone new took
                # the floor — the camera travels, so that is a hand-off
                switches += 1
            incumbent = best
            had_incumbent = True
            challenger = None
            challenger_since = -1.0
        elif incumbent not in tracks:
            # the incumbent's track died (they left the frame): whoever takes
            # over still moves the camera, so that counts as a hand-off
            if best is not incumbent:
                switches += 1
            incumbent = best
            had_incumbent = True
            challenger = None
            challenger_since = -1.0
        elif best is not incumbent:
            if challenger is not best:
                challenger = best
                challenger_since = t
            if (
                t - challenger_since >= float(switch_hold)
                and score(best) > score(incumbent) * max(1.0, float(switch_ratio)) + 1e-6
            ):
                incumbent = best
                had_incumbent = True
                challenger = None
                challenger_since = -1.0
                switches += 1
        else:
            challenger = None
            challenger_since = -1.0
        if incumbent is not None and incumbent.matched:
            result.targets.append((t, incumbent.x, incumbent.y))
            widths.append(incumbent.width)

    result.switches = switches
    result.voice_used = voice_used
    result.targets = [
        (round(t, 4),
         round(min(1.0, max(0.0, x)), 5),
         round(min(1.0, max(0.05, y)), 5))
        for t, x, y in result.targets
    ]
    if result.targets:
        coverage = len(result.targets) / max(1, len(frames_persons))
        widths.sort()
        result.subject_width = widths[len(widths) // 2] if widths else 0.0
        if voice_used and voice:
            voice_fraction = sum(1 for v in voice if v) / max(1, len(voice))
            result.confidence = max(0.0, min(
                1.0, 0.65 * coverage + 0.35 * voice_fraction
            ))
        else:
            result.confidence = max(0.0, min(1.0, 0.85 * coverage))
    return result


# --------------------------------------------------------------------------
# 6. Smoothing: a camera that glides instead of snapping
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
      teleported after — and a speaker hand-off becomes a glide;
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
# 7. Zoom: how tight should the crop be?
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
# 8. Optional OpenCV upgrade (real Haar cascade face detection)
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


def face_frames(
    src: Path | str,
    start: float,
    end: float,
    src_w: int,
    src_h: int,
    fps: float = DEFAULT_FPS,
    proxy_w: int = 320,
    timeout: int = ANALYSIS_TIMEOUT,
) -> tuple[list, float]:
    """Grayscale proxy frames for Haar detection — ``([], 0.0)`` on failure."""
    cascade = cascade_path()
    if not cascade or src_w <= 0 or src_h <= 0:
        return [], 0.0
    dur = float(end) - float(start)
    if dur <= 0.2:
        return [], 0.0
    try:
        import numpy  # type: ignore
    except Exception:
        return [], 0.0
    rate = plan_fps(dur, fps)
    proxy_w = max(160, int(proxy_w))
    proxy_h = max(2, int(round(proxy_w * src_h / src_w)) // 2 * 2)
    try:
        proc = subprocess.run(
            [
                config.FFMPEG_BIN, "-hide_banner", "-nostats", "-loglevel", "error",
                "-ss", f"{float(start):.3f}", "-t", f"{dur:.3f}", "-i", str(src),
                "-vf", f"scale={proxy_w}:{proxy_h},fps={rate:g},format=gray",
                "-f", "rawvideo", "-pix_fmt", "gray", "-",
            ],
            capture_output=True,
            timeout=timeout,
        )
    except Exception:
        return [], 0.0
    if proc.returncode != 0:
        return [], 0.0
    per = proxy_w * proxy_h
    raw = proc.stdout or b""
    count = len(raw) // per
    if count <= 0:
        return [], 0.0
    frames = [
        numpy.frombuffer(raw[i * per:(i + 1) * per], dtype=numpy.uint8)
        .reshape(proxy_h, proxy_w)
        for i in range(count)
    ]
    return frames, rate


def person_frames_from_faces(
    gray_frames: list, rate: float
) -> list[list[Person]]:
    """Per-frame ``Person`` candidates from Haar faces + frame-diff motion.

    Faces are detected on every analysed frame (all of them — the *biggest*
    face is no longer automatically the subject), and each face's motion
    energy comes from the mean absolute difference of its region between
    consecutive frames. Speaker scoring then does the choosing.
    """
    cascade = cascade_path()
    if not cascade or not gray_frames:
        return []
    try:
        import cv2  # type: ignore
        import numpy  # type: ignore
    except Exception:
        return []
    detector = cv2.CascadeClassifier(cascade)
    if detector.empty():
        return []
    proxy_h, proxy_w = gray_frames[0].shape[:2]
    min_side = max(20, proxy_w // 14)
    out: list[list[Person]] = []
    prev = None
    frame_area = float(proxy_w * proxy_h)
    for frame in gray_frames:
        persons: list[Person] = []
        faces = detector.detectMultiScale(
            frame, scaleFactor=1.2, minNeighbors=5, minSize=(min_side, min_side),
        )
        diff = None
        if prev is not None:
            diff = cv2.absdiff(frame, prev)
        prev = frame
        boxes = sorted(
            (tuple(int(v) for v in box) for box in faces),
            key=lambda b: b[2] * b[3], reverse=True,
        )[: max(1, int(config.TRACK_MAX_PERSONS))]
        for (x, y, w, h) in boxes:
            energy = 0.0
            if diff is not None and w > 0 and h > 0:
                lo_x, hi_x = max(0, x), min(proxy_w, x + w)
                lo_y, hi_y = max(0, y), min(proxy_h, y + h)
                region = diff[lo_y:hi_y, lo_x:hi_x]
                if region.size:
                    energy = min(1.0, float(region.mean()) / 64.0)
            persons.append(Person(
                cx=(x + w / 2.0) / proxy_w,
                # frame the chest, not the forehead: sit the centre lower
                cy=(y + h * 1.9) / proxy_h,
                mass=min(1.0, (w * h) / frame_area * 8.0),
                width=w / proxy_w,
                height=h / proxy_h,
                x0=x / proxy_w,
                x1=(x + w) / proxy_w,
                y0=y / proxy_h,
                y1=(y + h) / proxy_h,
                energy=energy,
            ))
        out.append(persons)
    return out


# --------------------------------------------------------------------------
# 9. Cache (re-renders must be instant)
# --------------------------------------------------------------------------
def _cache_key(src: Path, start: float, end: float, fps: float,
               zoom: str, mode: str) -> Path:
    try:
        stat = src.stat()
        stamp = f"{stat.st_size}-{int(stat.st_mtime)}"
    except Exception:
        stamp = "nostat"
    digest = hashlib.sha1(
        f"v{config.TRACK_ALGO_VERSION}|{src.name}|{stamp}|{float(start):.2f}|"
        f"{float(end):.2f}|{float(fps):.2f}|{zoom}|{mode}".encode("utf-8")
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
# 10. The public entry point
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
    """Track the **speaker** across ``[start, end]`` — ``None`` on any failure.

    ``mode`` is ``auto`` (best available backend), ``vision`` (heat maps only,
    no OpenCV) or ``off``. ``crop_frac_x``/``crop_frac_y`` say how much of the
    source frame the biggest possible crop already covers, so the tracker knows
    how much freedom it has.

    Backends, best first: ``haar-speaker`` (Haar faces + voice-correlated
    motion), ``heat-speaker`` (skin blobs + voice-correlated motion),
    ``heat-presence`` (skin blobs, most-present person — used when the clip
    has no usable speech audio), and finally the legacy merged-heat
    single-subject pass.
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

    mode_l = str(mode).strip().lower()
    rate = plan_fps(float(end) - float(start), fps)

    # 1) voice: does anyone speak during this window, and when?
    envelope = voice_envelope(src, start, end)
    voice = voice_activity(envelope) if envelope else []

    targets: list[tuple[float, float, float]] = []
    backend = "heuristic"
    confidence = 0.0
    subject_width = 0.0
    switches = 0

    # 2) real faces when allowed (and OpenCV + a cascade exist)
    if mode_l in ("auto", "face"):
        gray, haar_rate = face_frames(src, start, end, src_w, src_h, fps)
        if gray:
            candidates = person_frames_from_faces(gray, haar_rate or rate)
            if candidates and any(candidates):
                result = run_speaker_tracker(candidates, voice, haar_rate or rate)
                if result.targets:
                    targets = result.targets
                    confidence = result.confidence
                    subject_width = result.subject_width
                    switches = result.switches
                    backend = (
                        "haar-speaker" if result.voice_used else "haar-presence"
                    )

    # 3) skin blobs from the ffmpeg heat maps (the free, always-available path)
    if not targets and mode_l in ("auto", "vision"):
        grid = grid_for(src_w, src_h)
        frames = heat_frames(src, start, end, rate, grid)
        if frames:
            candidates = person_frames_from_heat(frames, grid)
            if candidates and any(candidates):
                result = run_speaker_tracker(candidates, voice, rate)
                if result.targets:
                    targets = result.targets
                    confidence = result.confidence
                    subject_width = result.subject_width
                    switches = result.switches
                    backend = (
                        "heat-speaker" if result.voice_used else "heat-presence"
                    )
            if not targets:
                # last resort: the v0.6.0 merged single-subject behaviour on
                # the skin plane — better than losing the subject entirely
                skin_only = [pair[0] for pair in split_planes(frames, *grid)]
                targets, confidence, subject_width = heat_targets(
                    skin_only, grid, crop_frac_x, crop_frac_y, rate
                )
                backend = "heuristic" if targets else backend

    if mode_l == "face" and backend not in ("haar-speaker", "haar-presence"):
        return None  # "face" demands the cascade; a static crop is safer
    if not targets:
        return None

    smooth = smooth_targets(targets)
    if not smooth:
        return None
    if subject_width <= 0 and backend in ("haar-speaker", "haar-presence"):
        subject_width = 0.30  # faces are typically ~30% of a 16:9 frame wide
    fraction, note = choose_zoom(
        zoom, subject_width, crop_frac_x, src_w, out_w,
    )
    keyframes = simplify_path(smooth, epsilon=0.006, max_points=64)
    if len(keyframes) < 2:
        keyframes = [smooth[0], smooth[-1]]
    detail = f"{backend}"
    if switches:
        detail += f", {switches} speaker hand-off"
        if switches > 1:
            detail += "s"
    plan = TrackPath(
        keyframes=keyframes,
        zoom=fraction,
        backend=backend,
        fps=plan_fps(float(end) - float(start), fps),
        frames=len(targets),
        confidence=confidence,
        travel=path_travel(smooth),
        subject_width=subject_width,
        note=f"{detail}; {note}",
        switches=switches,
    )
    if use_cache:
        save_cache(cache_path, plan)
    return plan
