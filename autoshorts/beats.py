"""T3 — Beat Sync: offline beat markers from the ffmpeg loudness curve.

No audio library and no network: ffmpeg's ``ebur128`` filter already reports
momentary loudness every 100 ms, so Qyro reads that curve, looks at where the
energy *rises* (the onset), and peak-picks those rises with a minimum spacing
of :data:`config.BEAT_MIN_GAP` seconds. The result is a list of beat times that
cuts can snap onto — good enough to sit on the downbeat of a music bed or a
punch-in, without a single dependency.

Every step degrades to an empty list; a render must never fail because beats
could not be measured.
"""
from __future__ import annotations

import json
import math
import re
import subprocess
from pathlib import Path

from . import config

_R128_FRAME = re.compile(r"pts_time:([-\d.]+)")
_R128_VALUE = re.compile(r"lavfi\.r128\.M=([-+\d.eE]+|-inf|inf|nan)")

GRID = 0.1            # seconds per loudness bin (ebur128 updates at ~10 Hz)
CURVE_SUFFIX = config.BEAT_CACHE_SUFFIX


# --------------------------------------------------------------------------
# Pure math (unit-tested on synthetic curves)
# --------------------------------------------------------------------------
def resample_curve(samples: list[tuple[float, float]], grid: float = GRID) -> list[float]:
    """Bucket ``(time, dBFS)`` pairs onto a fixed ``grid``-second lattice.

    Empty bins carry the previous value forward (silence between words should
    not look like a beat), and the curve is returned normalised to 0..1.
    """
    if not samples:
        return []
    grid = max(0.01, float(grid))
    end = max(float(t) for t, _ in samples)
    n = max(1, int(math.floor(end / grid)) + 1)
    sums = [0.0] * n
    counts = [0] * n
    for t, value in samples:
        index = min(n - 1, max(0, int(float(t) / grid)))
        sums[index] += float(value)
        counts[index] += 1
    out: list[float] = []
    last = 0.0
    for i in range(n):
        if counts[i]:
            last = sums[i] / counts[i]
        out.append(last)
    lo, hi = min(out), max(out)
    if hi - lo < 1e-6:
        return [0.0] * n
    return [(v - lo) / (hi - lo) for v in out]


def onset_curve(curve: list[float], smooth: int = 1) -> list[float]:
    """Positive derivative of ``curve`` (energy rises only), lightly smoothed.

    A beat is where loudness *jumps*, not where it is loud, so falls are
    clipped to zero — that alone kills most of the false positives a plain
    peak-pick finds on speech.
    """
    if len(curve) < 2:
        return [0.0] * len(curve)
    raw = [0.0] + [max(0.0, float(curve[i]) - float(curve[i - 1]))
                   for i in range(1, len(curve))]
    smooth = max(0, int(smooth))
    if not smooth:
        return raw
    out: list[float] = []
    for i in range(len(raw)):
        lo = max(0, i - smooth)
        hi = min(len(raw), i + smooth + 1)
        out.append(sum(raw[lo:hi]) / (hi - lo))
    return out


def find_peaks(values: list[float], min_gap: int = 2, neighbours: int = 4,
               k: float = 0.6, threshold_floor: float = 0.02) -> list[int]:
    """Indices of prominence peaks in ``values``.

    A candidate must (a) beat its ``+/-neighbours`` on both sides, (b) sit at
    least ``min_gap`` indices after the last accepted peak, and (c) clear
    ``mean + k * stdev`` of the whole curve (and ``threshold_floor``), so a
    flat or noisy curve yields *no* beats instead of a beat every 100 ms.
    """
    n = len(values)
    if n < 3:
        return []
    vals = [float(v) for v in values]
    lo_all, hi_all = min(vals), max(vals)
    if hi_all - lo_all < 1e-9:
        return []                       # a flat curve has no accents at all
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / n
    cutoff = mean + max(0.0, float(k)) * math.sqrt(var)
    cutoff = max(cutoff, float(threshold_floor))
    gap = max(1, int(min_gap))
    side = max(1, int(neighbours))

    picked: list[int] = []
    for i in range(1, n - 1):
        value = vals[i]
        if value <= cutoff:             # strict: plateaus are not beats
            continue
        # A plateau counts as its first sample: strictly above the left side,
        # never below the right. (An exact tie on the left means we are inside
        # the plateau, and the interior of a bump is not a beat.)
        left = vals[max(0, i - side):i]
        right = vals[i + 1:min(n, i + side + 1)]
        if not left and not right:
            continue
        if left and value <= max(left):
            continue
        if right and value < max(right):
            continue
        if picked and i - picked[-1] < gap:
            # keep whichever of the two is the stronger peak
            if value > vals[picked[-1]]:
                picked[-1] = i
            continue
        picked.append(i)
    return picked


def top_peaks(values: list[float], min_gap: int = 2, neighbours: int = 4,
              limit: int = 400) -> list[int]:
    """Strongest local maxima, ranked — the safety net when statistics fail.

    :func:`find_peaks` deliberately returns nothing for a flat curve, which is
    right for a beat detector but wrong for a *cut snapper*: a spoken-word
    episode with a constant level still has relative accents. This pass drops
    the mean+k*sigma gate, keeps only true local maxima and takes the loudest
    ``limit`` of them under the same spacing rule. A genuinely flat curve still
    yields ``[]`` because no value is above its neighbours.
    """
    n = len(values)
    if n < 3:
        return []
    vals = [float(v) for v in values]
    if max(vals) - min(vals) < 1e-9:
        return []
    gap = max(1, int(min_gap))
    side = max(1, int(neighbours))
    candidates: list[tuple[float, int]] = []
    for i in range(1, n - 1):
        value = vals[i]
        if value <= 0.0:
            continue
        left = vals[max(0, i - side):i]
        right = vals[i + 1:min(n, i + side + 1)]
        if left and value <= max(left):
            continue                      # inside a plateau, not its start
        if right and value < max(right):
            continue
        candidates.append((value, i))
    candidates.sort(key=lambda pair: (-pair[0], pair[1]))
    taken: list[int] = []
    for _, index in candidates:
        if any(abs(index - other) < gap for other in taken):
            continue
        taken.append(index)
        if len(taken) >= max(1, int(limit)):
            break
    return sorted(taken)


def beats_from_curve(curve: list[float], grid: float = GRID,
                     min_gap: float = config.BEAT_MIN_GAP,
                     neighbours: int = config.BEAT_NEIGHBOURS,
                     k: float = config.BEAT_ONSET_K) -> list[float]:
    """Beat times (seconds) for a normalised loudness curve."""
    if not curve:
        return []
    step = max(1, int(round(min_gap / grid)))
    onsets = onset_curve(curve)
    idx = find_peaks(onsets, min_gap=step, neighbours=neighbours, k=k,
                     threshold_floor=0.0)
    if len(idx) < 4 and len(curve) > 60:
        # steady narrator, no clear accents: fall back to relative peaks
        relaxed = top_peaks(onsets, min_gap=step, neighbours=neighbours,
                            limit=config.BEAT_MAX_COUNT)
        if len(relaxed) > len(idx):
            idx = relaxed
    return [round(i * float(grid), 3) for i in idx[:config.BEAT_MAX_COUNT]]


def snap_to_beats(start: float, end: float, beats: list[float],
                  window: float = config.BEAT_SNAP_WINDOW) -> tuple[float, float, bool]:
    """Nearest beat per boundary within ``window`` seconds.

    Returns ``(start, end, snapped)``; boundaries only move inward-safe
    distances and a cut shorter than 5 s after snapping is left alone.
    """
    start = float(start)
    end = float(end)
    if not beats:
        return start, end, False

    def nearest(value: float) -> float | None:
        best: float | None = None
        best_delta = abs(float(window))
        for beat in beats:
            delta = abs(float(beat) - value)
            if delta <= best_delta:
                best, best_delta = float(beat), delta
        return best if best_delta > 0.001 else None

    new_start = nearest(start)
    new_end = nearest(end)
    moved = False
    candidate_start = start if new_start is None else new_start
    candidate_end = end if new_end is None else new_end
    if candidate_end - candidate_start >= 5.0:
        if abs(candidate_start - start) > 0.001 or abs(candidate_end - end) > 0.001:
            moved = True
        start, end = candidate_start, candidate_end
    return round(start, 3), round(end, 3), moved


# --------------------------------------------------------------------------
# ffmpeg side
# --------------------------------------------------------------------------
def loudness_samples(path: Path | str, timeout: int = 420) -> list[tuple[float, float]]:
    """``(pts_time, momentary loudness)`` from ebur128 — ``[]`` on any failure."""
    try:
        proc = subprocess.run(
            [
                config.FFMPEG_BIN, "-hide_banner", "-nostats", "-loglevel", "error",
                "-i", str(path),
                "-filter_complex",
                "ebur128=metadata=1:peak=none,ametadata=print:key=lavfi.r128.M:file=-",
                "-f", "null", "-",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            return []
    except Exception:
        return []

    samples: list[tuple[float, float]] = []
    current: float | None = None
    for line in (proc.stdout or "").splitlines():
        frame = _R128_FRAME.search(line)
        if frame:
            current = float(frame.group(1))
            continue
        value = _R128_VALUE.search(line)
        if value and current is not None:
            try:
                m = float(value.group(1))
            except ValueError:
                m = float("nan")
            if math.isfinite(m):
                samples.append((current, m))
            current = None
    return samples


def cache_path(media: Path | str) -> Path:
    return Path(str(media) + CURVE_SUFFIX)


def load_cache(media: Path | str) -> dict | None:
    path = cache_path(media)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("beats"), list):
            return data
    except Exception:
        return None
    return None


def beats_for_media(media: Path | str, force: bool = False) -> dict:
    """Beat markers for a media file, cached next to it.

    Never raises: no file, no audio stream or a dead ffmpeg all answer with an
    empty beat list plus a human-readable ``reason`` the UI can show.
    """
    media = Path(media)
    if not media.is_file():
        return {"beats": [], "count": 0, "duration": 0.0, "cached": False,
                "reason": "No media file yet — render a clip first."}
    if not force:
        cached = load_cache(media)
        if cached is not None:
            cached = {**cached, "cached": True}
            cached.setdefault("reason", "")
            return cached

    samples = loudness_samples(media)
    if not samples:
        return {"beats": [], "count": 0, "duration": 0.0, "cached": False,
                "reason": "No loudness curve (silent or unreadable audio)."}
    duration = max(t for t, _ in samples)
    curve = resample_curve(samples)
    beats = beats_from_curve(curve)
    payload = {
        "beats": beats,
        "count": len(beats),
        "duration": round(duration, 3),
        "grid": GRID,
        "curve": [round(v, 4) for v in curve[:1200]],
        "cached": False,
        "reason": "" if beats else "Audio too steady for beat markers.",
    }
    try:
        cache_path(media).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass
    return payload
