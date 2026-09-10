"""ffmpeg wrappers: render captioned shorts, thumbnails, demo media.

v0.4.0: formats (vertical/square/wide) × quality (fast/full), styles
(blur/crop/fill/fit/smart), caption styles (classic/pop/minimal),
silence jump-cuts, speed (atempo), loudness norm, progress bar,
smart-crop motion analysis, waveforms.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from . import config
from .smart import (
    CHUNK_SECONDS,
    build_crop_x_expr,
    pick_side,
    remap_proxy_to_source,
    side_to_x,
    static_center_x,
    warp_time,
)
from .transcripts import Segment


def _run(args: list[str], timeout: int = 900) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(
            [config.FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-y", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("ffmpeg timed out while rendering a clip.")
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed: {(proc.stderr or '').strip()[-400:]}"
        )
    return proc


def _run_capture(args: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    """Run ffmpeg capturing all output (for analysis filters)."""
    try:
        proc = subprocess.run(
            [config.FFMPEG_BIN, "-hide_banner", "-y", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("ffmpeg analysis timed out.")
    return proc


def probe_duration(path: Path) -> float | None:
    try:
        proc = subprocess.run(
            [config.FFMPEG_BIN, "-hide_banner", "-i", str(path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        for line in proc.stderr.splitlines():
            if "Duration:" in line:
                tok = line.split("Duration:")[1].split(",")[0].strip()
                h, m, s = tok.split(":")
                return int(h) * 3600 + int(m) * 60 + float(s)
    except Exception:
        pass
    return None


def probe_dimensions(path: Path) -> tuple[int, int] | None:
    """Parse Video dimensions from `ffmpeg -i` stderr (no ffprobe needed)."""
    try:
        proc = subprocess.run(
            [config.FFMPEG_BIN, "-hide_banner", "-i", str(path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        out = (proc.stderr or "") + (proc.stdout or "")
        # e.g. "Video: h264 ..., 1280x720 [SAR ...], ..."
        matches = re.findall(r"Video:.*?,\s(\d{2,5})x(\d{2,5})\b", out)
        if matches:
            w, h = matches[0]
            return int(w), int(h)
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------
# ASS caption generation
# --------------------------------------------------------------------------
def _ass_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _autofit_fontsize(base_size: int, segments: list[Segment], play_w: int,
                      min_size: int = 24) -> int:
    """Shrink Fontsize for long words so 9:16 captions never overflow.

    Rough width model: avg glyph ≈ 0.62 × fontsize. Shrink until the
    longest word fits within ~92% of the play width, floored at 24.
    """
    longest = 0
    for seg in segments:
        for w in (seg.text or "").split():
            longest = max(longest, len(w))
    if longest <= 0:
        return base_size
    size = base_size
    while size > min_size and longest * size * 0.62 > play_w * 0.92:
        size -= 2
    return max(size, min_size)


def make_ass(
    segments: list[Segment],
    clip_start: float,
    clip_end: float,
    out_path: Path,
    play_w: int,
    play_h: int,
    words_per_caption: int = config.CAPTION_WORDS_PER_LINE,
    captions: str = "classic",
    captions_pos: str = "standard",
    captions_box: bool = False,
    speed: float = 1.0,
    cuts: list[tuple[float, float]] | None = None,
) -> Path:
    """Build an ASS subtitle file for [clip_start, clip_end].

    * ``captions``: classic (chunked caps) / pop (karaoke highlight) /
      minimal (lower-third).
    * ``captions_pos``: standard (MarginV 30% height) / low (MarginV ~8%).
    * ``captions_box``: True → BorderStyle=3 + near-opaque BackColour,
      else BorderStyle=1.
    * ``speed`` rescales event times (output = t / speed).
    * ``cuts`` are removed silence intervals in clip-relative seconds;
      captions are remapped through keeps via warp_time.
    * Auto-fit shrinks Fontsize for long words (min 24).
    """
    if speed is None or speed <= 0:
        speed = 1.0
    cuts = sorted(cuts or [])
    captions = captions if captions in ("classic", "pop", "minimal") else "classic"
    captions_pos = captions_pos if captions_pos in ("standard", "low") else "standard"

    base_size = max(36, int(play_h * 0.045))
    if captions == "minimal":
        base_size = max(28, int(play_h * 0.032))
    font_size = _autofit_fontsize(base_size, segments, play_w, min_size=24)

    if captions_pos == "low":
        margin_v = int(play_h * 0.08)
    else:
        margin_v = int(play_h * 0.30) if captions != "minimal" else int(play_h * 0.12)

    border_style = 3 if captions_box else 1
    back_colour = "&HCC000000" if captions_box else "&H64000000"
    outline = max(2, font_size // 18)

    if captions == "pop":
        style_line = (
            f"Style: Cap,{config.CAPTION_FONT},{font_size},&H00FFFFFF,&H000080FF,"
            f"&H00000000,{back_colour},-1,0,0,0,100,100,0,0,"
            f"{border_style},{outline},1,2,60,60,{margin_v},1"
        )
    elif captions == "minimal":
        style_line = (
            f"Style: Cap,{config.CAPTION_FONT},{font_size},&H00F2F2F2,&H00FFFFFF,"
            f"&H80000000,{back_colour},0,0,0,0,100,100,0,0,"
            f"{border_style},{max(1, outline - 1)},0,2,40,40,{margin_v},1"
        )
    else:
        style_line = (
            f"Style: Cap,{config.CAPTION_FONT},{font_size},&H00FFFFFF,&H00FFFFFF,"
            f"&H00000000,{back_colour},-1,0,0,0,100,100,0,0,"
            f"{border_style},{outline},1,2,60,60,{margin_v},1"
        )

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {play_w}
PlayResY: {play_h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
{style_line}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []

    # Split caption text into small word chunks spread over line duration
    chunks: list[tuple[float, float, str, list[str]]] = []
    for seg in segments:
        words = seg.text.split()
        if not words:
            continue
        dur = max(seg.end - seg.start, 0.4)
        for k in range(0, len(words), words_per_caption):
            part = words[k : k + words_per_caption]
            frac0 = k / len(words)
            frac1 = min((k + len(part)) / len(words), 1.0)
            chunks.append(
                (
                    seg.start + dur * frac0,
                    seg.start + dur * frac1,
                    " ".join(part),
                    part,
                )
            )

    def _warp_clip_rel(t_clip: float) -> float:
        # remap through silence keeps, then speed
        return warp_time(max(t_clip, 0.0), cuts, speed)

    # Caption timing relative to clip start; clamp to the clip window,
    # then warp through cuts + speed onto the output timeline.
    for c_start, c_end, text, part_words in chunks:
        if c_end <= clip_start or c_start >= clip_end:
            continue
        rel_start = max(0.0, c_start - clip_start)
        rel_end = min(clip_end - clip_start, c_end - clip_start)
        # drop chunks fully inside a removed silence cut
        mid = (rel_start + rel_end) / 2.0
        if any(a < mid < b for a, b in cuts):
            # if the middle is cut but edges survive, clamp; else skip
            pass
        out_start = _warp_clip_rel(rel_start)
        out_end = _warp_clip_rel(rel_end)
        if out_end - out_start < 0.12:
            continue
        safe = text.replace("{", "(").replace("}", ")")
        if captions == "classic":
            body = safe.upper()
        elif captions == "minimal":
            body = safe
        else:  # pop: karaoke word highlight via \k tags
            per_word_cs = max(int((out_end - out_start) * 100 / max(len(part_words), 1)), 8)
            words_tagged = "".join(
                "{\\k%d}%s " % (per_word_cs, w.replace("{", "(").replace("}", ")").upper())
                for w in part_words
            ).strip()
            body = words_tagged
        events.append(
            f"Dialogue: 0,{_ass_time(out_start)},{_ass_time(out_end)},Cap,,0,0,0,,{body}"
        )

    out_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return out_path


# --------------------------------------------------------------------------
# Silence detection + jump-cut helpers
# --------------------------------------------------------------------------
_SIL_START_RE = re.compile(r"silence_start:\s*([0-9.]+)")
_SIL_END_RE = re.compile(r"silence_end:\s*([0-9.]+)")


def detect_silences(src: Path, start: float, end: float,
                    min_dur: float = 0.4, noise_db: int = -30,
                    proxy_scale: float = 1.0, proxy_offset: float = 0.0,
                    timeout: int = 300) -> list[tuple[float, float]]:
    """Detect silences in [start, end] via silencedetect; return clip-relative cuts.

    GLOBAL RULE: Silence cuts detected on any scaled proxy must be
    remapped to source timestamps before cutting — proxy detections are
    passed through remap_proxy_to_source() with the proxy scale/offset.
    """
    dur = max(end - start, 0.5)
    try:
        proc = _run_capture(
            [
                "-loglevel", "info",
                "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
                "-af", f"silencedetect=noise={noise_db}dB:d={min_dur}",
                "-f", "null", "-",
            ],
            timeout=timeout,
        )
    except Exception:
        return []
    out = (proc.stderr or "") + (proc.stdout or "")
    # Pair silence_start / silence_end lines in order.
    starts: list[float] = [float(m.group(1)) for m in _SIL_START_RE.finditer(out)]
    ends: list[float] = [float(m.group(1)) for m in _SIL_END_RE.finditer(out)]
    cuts_proxy: list[tuple[float, float]] = []
    # ffmpeg emits start then end; pair sequentially. A trailing start
    # without end extends to the window end.
    ei = 0
    for s in starts:
        # find the first end after s
        while ei < len(ends) and ends[ei] < s:
            ei += 1
        if ei < len(ends):
            cuts_proxy.append((s, ends[ei]))
            ei += 1
        else:
            cuts_proxy.append((s, dur))
    # Leading silence before the first start is reported as silence_end
    # without a preceding start; capture it.
    if ends and (not starts or ends[0] < starts[0]):
        cuts_proxy.append((0.0, ends[0]))
    # Clamp + drop tiny cuts, then remap proxy → source timestamps.
    cleaned = []
    for a, b in cuts_proxy:
        a = max(0.0, min(a, dur))
        b = max(0.0, min(b, dur))
        if b - a >= max(min_dur * 0.9, 0.2):
            # Keep a small padding so speech onsets aren't clipped.
            a = min(a + 0.08, b)
            b = max(b - 0.08, a)
            if b - a >= 0.15:
                cleaned.append((a, b))
    # Remap each proxy cut to source (clip-relative) timestamps.
    remapped = [
        (remap_proxy_to_source(a, proxy_scale, proxy_offset),
         remap_proxy_to_source(b, proxy_scale, proxy_offset))
        for a, b in cleaned
    ]
    # Merge overlaps after remap.
    remapped.sort()
    merged: list[tuple[float, float]] = []
    for a, b in remapped:
        if merged and a <= merged[-1][1] + 0.05:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def invert_cuts_to_keeps(cuts: list[tuple[float, float]], dur: float) -> list[tuple[float, float]]:
    """Invert removed cuts into kept intervals over [0, dur]."""
    cuts = sorted(cuts or [])
    keeps: list[tuple[float, float]] = []
    cursor = 0.0
    for a, b in cuts:
        if a > cursor:
            keeps.append((cursor, min(a, dur)))
        cursor = max(cursor, b)
    if cursor < dur:
        keeps.append((cursor, dur))
    return [(a, b) for a, b in keeps if b - a > 0.05]


def build_select_expr(keeps: list[tuple[float, float]]) -> str:
    """Build select/aselect expression from kept intervals."""
    parts = [f"between(t,{a:.3f},{b:.3f})" for a, b in keeps]
    return "+".join(parts) if parts else "1"


def kept_duration(dur: float, cuts: list[tuple[float, float]]) -> float:
    removed = sum(max(b - a, 0.0) for a, b in (cuts or []))
    return max(dur - removed, 0.5)


# --------------------------------------------------------------------------
# Smart-crop motion analysis (signalstats YDIF per third)
# --------------------------------------------------------------------------
_YDIF_RE = re.compile(r"YDIF[:=]\s*([0-9.]+)", re.IGNORECASE)


def _ydif_for_region(src: Path, start: float, dur: float, src_w: int,
                     region: str, timeout: int = 120) -> float:
    """Average YDIF motion for one third (left/center/right) of a chunk.

    Uses a small downscaled proxy + low fps for speed; spatial thirds map
    1:1 so no timestamp remap is needed here.
    """
    third = max(src_w // 3, 8)
    x = {"left": 0, "center": third, "right": third * 2}.get(region, third)
    vf = (
        f"fps=4,scale=320:-2,"
        f"crop=106:ih:{int(x / max(src_w, 1) * 320)}:0,"
        f"signalstats,metadata=print:key=lavfi.signalstats.YDIF:file=-:direct=1"
    )
    # Simpler robust crop on the scaled frame:
    x_scaled = {"left": 0, "center": 106, "right": 212}.get(region, 106)
    vf = (
        f"fps=4,scale=320:-2,crop=106:ih:{x_scaled}:0,"
        f"signalstats,metadata=print:key=lavfi.signalstats.YDIF:file=-"
    )
    try:
        proc = _run_capture(
            [
                "-loglevel", "info",
                "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
                "-vf", vf,
                "-f", "null", "-",
            ],
            timeout=timeout,
        )
    except Exception:
        raise
    out = (proc.stderr or "") + (proc.stdout or "")
    vals = [float(m.group(1)) for m in _YDIF_RE.finditer(out)]
    if not vals:
        # Fallback: try alternate metadata key spelling
        alt = re.findall(r"signalstats\.YDIF['\"]?\s*[:=]\s*([0-9.]+)", out, re.IGNORECASE)
        vals = [float(v) for v in alt]
    if not vals:
        return 0.0
    # Trim extremes and average.
    vals.sort()
    if len(vals) > 4:
        vals = vals[1:-1]
    return sum(vals) / len(vals)


def analyze_motion_sides(src: Path, start: float, end: float,
                         chunk_seconds: float = CHUNK_SECONDS) -> list[dict]:
    """Per-chunk {left,center,right} YDIF motion. [] on ANY failure.

    Render must NEVER break because of analysis — callers fall back to a
    static center crop when this returns [].
    """
    try:
        dims = probe_dimensions(src)
        src_w = dims[0] if dims else 640
        dur = max(end - start, 0.5)
        chunks: list[dict] = []
        t = start
        while t < end - 0.2:
            c_dur = min(chunk_seconds, end - t)
            try:
                left = _ydif_for_region(src, t, c_dur, src_w, "left")
                center = _ydif_for_region(src, t, c_dur, src_w, "center")
                right = _ydif_for_region(src, t, c_dur, src_w, "right")
            except Exception:
                return []
            chunks.append({"left": left, "center": center, "right": right})
            t += chunk_seconds
        return chunks
    except Exception:
        return []


def smart_crop_x_expr(src: Path, start: float, end: float,
                      crop_w: int, src_w: int | None = None,
                      cuts: list[tuple[float, float]] | None = None,
                      speed: float = 1.0) -> str:
    """Build the stepped numeric x(t) expression for a smart crop.

    Analyzes motion per 2s chunk, applies hysteresis + calm→center bias,
    remaps chunk positions through silence cuts + speed onto the output
    timeline. ANY failure → static center crop value.
    """
    try:
        if src_w is None:
            dims = probe_dimensions(src)
            src_w = dims[0] if dims else 640
        chunks = analyze_motion_sides(src, start, end)
        if not chunks:
            return str(static_center_x(src_w, crop_w))
        # Hysteresis walk over chunks (clip-relative start times).
        positions: list[tuple[float, str]] = []
        prev: str | None = None
        for i, scores in enumerate(chunks):
            side = pick_side(scores, prev=prev)
            positions.append((i * CHUNK_SECONDS, side))
            prev = side
        # Remap chunk positions through silence cuts + speed onto output t.
        from .smart import warp_positions as _warp_positions

        warped = _warp_positions(positions, cuts or [], speed or 1.0)
        return build_crop_x_expr(warped, src_w, crop_w)
    except Exception:
        try:
            if src_w is None:
                src_w = 640
            return str(static_center_x(src_w, crop_w))
        except Exception:
            return "0"


# Re-export warp helpers for tests importing from ffmpeg.
warp_time = warp_time  # noqa: F811
remap_proxy_to_source = remap_proxy_to_source  # noqa: F811


# --------------------------------------------------------------------------
# Waveform (24 loudness bars via ebur128)
# --------------------------------------------------------------------------
_EBUR128_M_RE = re.compile(r"\bM:\s*(-?[0-9.]+)", re.IGNORECASE)
_EBUR128_S_RE = re.compile(r"\bS:\s*(-?[0-9.]+)", re.IGNORECASE)


def get_waveform(clip: Path, bars: int = 24, timeout: int = 120) -> list[float]:
    """Return ``bars`` normalized loudness values via ebur128, else [].

    Runs a single ebur128 pass with per-frame logging, buckets momentary
    loudness into ``bars`` windows and normalizes to 0..1. ANY failure →
    [] (callers store [] and the UI hides the strip).
    """
    try:
        if not clip.is_file() or clip.stat().st_size < 1000:
            return []
        dur = probe_duration(clip) or 0.0
        if dur <= 0:
            return []
        proc = _run_capture(
            [
                "-loglevel", "info",
                "-i", str(clip),
                "-filter:a", "ebur128=peak=true:framelog=info",
                "-f", "null", "-",
            ],
            timeout=timeout,
        )
        out = (proc.stderr or "") + (proc.stdout or "")
        # ebur128 framelog lines look like:
        #   [Parsed_ebur128_0 ...] t: 0.1  TARGET: ...  M: -23.1  S: ...  I: ...
        moments = [float(m.group(1)) for m in _EBUR128_M_RE.finditer(out)]
        if not moments:
            moments = [float(m.group(1)) for m in _EBUR128_S_RE.finditer(out)]
        if not moments:
            # Fallback within ebur128: parse integrated summary into flat bars
            m = re.search(r"Integrated loudness:.*?(-?[0-9.]+)\s*LUFS", out)
            if m:
                v = max(0.0, min(1.0, (float(m.group(1)) + 40.0) / 40.0))
                return [round(v, 3)] * bars
            return []
        # Bucket into `bars` windows (average), then normalize -40..0 LUFS → 0..1
        per = max(len(moments) // bars, 1)
        buckets: list[float] = []
        for i in range(bars):
            sl = moments[i * per:(i + 1) * per] or moments[-per:]
            avg = sum(sl) / len(sl)
            norm = (avg + 40.0) / 40.0
            buckets.append(round(max(0.02, min(1.0, norm)), 3))
        # If everything is flat-zero (digital silence), return gentle bars
        if max(buckets) <= 0.03:
            return []
        return buckets
    except Exception:
        return []


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
def _escape_filter(path: str) -> str:
    """Escape a path for use inside an ffmpeg filter argument."""
    return (
        path.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace(",", "\\,")
    )


def _atempo_chain(speed: float) -> str:
    """Single atempo (our validated range 0.5–2.0 fits one filter)."""
    if abs(speed - 1.0) < 1e-6:
        return ""
    # Clamp defensively; validation already enforces 0.5–2.0.
    s = max(0.5, min(2.0, speed))
    return f"atempo={s:.4f}"


def render_clip(
    src: Path,
    start: float,
    end: float,
    out: Path,
    ass_path: Path,
    style: str = config.DEFAULT_STYLE,
    height: int = 1280,
    fmt: str | None = None,
    quality: str | None = None,
    captions: str = "classic",
    captions_pos: str = "standard",
    captions_box: bool = False,
    speed: float = 1.0,
    silence: bool = False,
    loud: bool = False,
    progress: bool = False,
    format: str | None = None,  # alias for fmt
    silence_cuts: list[tuple[float, float]] | None = None,
    smart_x_expr: str | None = None,
) -> Path:
    """Cut [start, end] from src into a captioned output mp4.

    * ``fmt``/``format`` + ``quality`` select output geometry from
      config.FORMAT_SIZES (default vertical/fast). ``height`` is kept for
      backwards compatibility when no format is given.
    * ``style``: blur|crop|fill|fit|smart. Wide output ignores style
      (plain scale).
    * ``speed`` uses atempo (audio) + setpts (video).
    * ``silence`` jump-cuts silences detected in the window.
    * ``loud`` normalizes loudness (loudnorm); ``progress`` draws an
      amber top bar.
    """
    out_format = format or fmt or config.DEFAULT_FORMAT
    if out_format not in config.FORMATS:
        out_format = config.DEFAULT_FORMAT
    q = quality if quality in config.QUALITIES else config.DEFAULT_QUALITY
    if fmt is None and format is None and quality is None and height != 1280:
        # Backwards-compat: explicit height without format → vertical-ish.
        width = round(height * 9 / 16 / 2) * 2
        out_w, out_h = width, height
    else:
        out_w, out_h = config.output_size(out_format, q)
    style = style if style in config.STYLES else config.DEFAULT_STYLE
    if speed is None or speed <= 0:
        speed = 1.0

    dur = max(end - start, 1.0)

    # --- silence cuts (clip-relative) -------------------------------------
    cuts: list[tuple[float, float]] = list(silence_cuts or [])
    if silence and not cuts:
        try:
            cuts = detect_silences(src, start, end)
        except Exception:
            cuts = []
    keeps = invert_cuts_to_keeps(cuts, dur) if cuts else [(0.0, dur)]
    use_jumpcut = bool(cuts) and len(keeps) >= 1 and sum(b - a for a, b in keeps) < dur - 0.15
    if not keeps:
        keeps = [(0.0, dur)]
        use_jumpcut = False
    kept_dur = sum(b - a for a, b in keeps)
    out_dur = kept_dur / speed if speed else kept_dur

    ass_esc = _escape_filter(str(ass_path))

    # --- audio filter ------------------------------------------------------
    af_parts: list[str] = []
    if use_jumpcut:
        sel = build_select_expr(keeps)
        af_parts.append(f"aselect='{sel}',asetpts=N/SR/TB")
    atempo = _atempo_chain(speed)
    if atempo:
        af_parts.append(atempo)
    if loud:
        af_parts.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    af = ",".join(af_parts) if af_parts else None

    # --- video filter ------------------------------------------------------
    # Time-base helpers: after jump-cut, t is output-before-speed time;
    # after setpts(PTS/speed), t is final output time. Progress bar must use
    # final t, so it is applied AFTER setpts.
    pre_parts: list[str] = []   # applied before geometry (jumpcut)
    post_speed_parts: list[str] = []  # setpts for speed
    if use_jumpcut:
        sel = build_select_expr(keeps)
        pre_parts.append(f"select='{sel}',setpts=N/FRAME_RATE/TB")
    if abs(speed - 1.0) > 1e-6:
        post_speed_parts.append(f"setpts=PTS/{speed:.4f}")

    # Geometry per style (wide ignores style → plain scale).
    ass_filter = f"ass={ass_esc}"
    # Amber progress bar uses final output t and total out_dur.
    progress_filter = None
    if progress and out_dur > 0:
        # 10px amber bar growing left→right across the top.
        progress_filter = (
            f"drawbox=y=0:h=10:c=0xFFB020:t=fill:w='iw*min(t/{out_dur:.3f},1)'"
        )

    def _suffix(base: str) -> str:
        parts = [base]
        if progress_filter:
            parts.append(progress_filter)
        parts.append(ass_filter)
        return ",".join(p for p in parts if p)

    if out_format == "wide":
        vf_simple = _suffix(f"scale={out_w}:{out_h}")
        return _render_simple(src, start, dur, out, vf_simple, af, pre_parts, post_speed_parts)

    if style == "crop":
        base = (
            f"crop='min(iw,ih*{out_w}/{out_h})':'min(ih,iw*{out_h}/{out_w})',"
            f"scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
            f"crop={out_w}:{out_h}"
        )
        return _render_simple(src, start, dur, out, _suffix(base), af, pre_parts, post_speed_parts)

    if style == "fill":
        base = (
            f"scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
            f"crop={out_w}:{out_h}"
        )
        return _render_simple(src, start, dur, out, _suffix(base), af, pre_parts, post_speed_parts)

    if style == "fit":
        base = (
            f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
            f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:color=black"
        )
        return _render_simple(src, start, dur, out, _suffix(base), af, pre_parts, post_speed_parts)

    if style == "smart":
        # Smart: crop a portrait window with motion-guided x(t), then scale.
        dims = probe_dimensions(src) or (640, 360)
        src_w = dims[0]
        # Target crop aspect = output aspect, using full source height.
        crop_w = max(8, min(src_w, int(src_w and (dims[1] * out_w / out_h)) or src_w // 2))
        crop_w = (crop_w // 2) * 2
        crop_h = dims[1] if dims[1] % 2 == 0 else dims[1] - 1
        try:
            x_expr = smart_x_expr or smart_crop_x_expr(
                src, start, end, crop_w, src_w, cuts=cuts if use_jumpcut else [], speed=speed
            )
        except Exception:
            x_expr = str(static_center_x(src_w, crop_w))
        # Guard: x expr must be numeric/if-chains only (never ow/iw).
        if "ow" in x_expr or "iw" in x_expr:
            x_expr = str(static_center_x(src_w, crop_w))
        # Quote x(t): commas inside if(lt(t,…),…) must not split filters.
        base = f"crop={crop_w}:{crop_h}:'{x_expr}':(ih-{crop_h})/2,scale={out_w}:{out_h}"
        return _render_simple(src, start, dur, out, _suffix(base), af, pre_parts, post_speed_parts)

    # default: blur background + centered foreground
    # NOTE: jump-cut + speed + blur needs filter_complex; keep the simple
    # path for the common case and use complex only when needed.
    if use_jumpcut or abs(speed - 1.0) > 1e-6:
        return _render_blur_complex(src, start, dur, out, ass_filter, af,
                                    pre_parts, post_speed_parts, out_w, out_h,
                                    progress_filter)
    vf_complex = (
        f"[0:v]split=2[bg][fg];"
        f"[bg]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
        f"crop={out_w}:{out_h},gblur=sigma=24[bgb];"
        f"[fg]scale={out_w}:-2[fgs];"
        f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2[ov];"
        f"[ov]{ass_filter}"
        + (f",{progress_filter}" if progress_filter else "")
        + "[v]"
    )
    args = ["-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
            "-filter_complex", vf_complex, "-map", "[v]", "-map", "0:a?"]
    if af:
        args += ["-af", af]
    args += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(out),
    ]
    _run(args)
    return out


def _render_simple(src: Path, start: float, dur: float, out: Path,
                   vf_body: str, af: str | None,
                   pre_parts: list[str], post_speed_parts: list[str]) -> Path:
    vf_parts = [*pre_parts, *post_speed_parts, vf_body] if (pre_parts or post_speed_parts) else [vf_body]
    # pre/post parts operate pre-geometry; geometry is inside vf_body.
    # Order: jumpcut → speed → geometry → progress → ass.
    vf = ",".join(p for p in vf_parts if p)
    args = ["-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
            "-vf", vf]
    if af:
        args += ["-af", af]
    args += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(out),
    ]
    _run(args)
    return out


def _render_blur_complex(src: Path, start: float, dur: float, out: Path,
                         ass_filter: str, af: str | None,
                         pre_parts: list[str], post_speed_parts: list[str],
                         out_w: int, out_h: int,
                         progress_filter: str | None) -> Path:
    spd = ",".join(post_speed_parts) if post_speed_parts else ""
    pre = ",".join(pre_parts) if pre_parts else ""
    # Apply jump-cut + speed to the input first, then split for blur.
    chain = ",".join(p for p in [pre, spd] if p)
    prefix = f"[0:v]{chain}[cut];[cut]" if chain else "[0:v]"
    tail = ass_filter + (f",{progress_filter}" if progress_filter else "")
    vf_complex = (
        f"{prefix}split=2[bg][fg];"
        f"[bg]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
        f"crop={out_w}:{out_h},gblur=sigma=24[bgb];"
        f"[fg]scale={out_w}:-2[fgs];"
        f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2[ov];"
        f"[ov]{tail}[v]"
    )
    args = ["-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
            "-filter_complex", vf_complex, "-map", "[v]", "-map", "0:a?"]
    if af:
        args += ["-af", af]
    args += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(out),
    ]
    _run(args)
    return out


def extract_thumbnail(clip: Path, out: Path) -> Path:
    _run(
        [
            "-ss", "0.5", "-i", str(clip),
            "-frames:v", "1", "-vf", "scale=360:-2",
            str(out),
        ],
        timeout=120,
    )
    return out


# --------------------------------------------------------------------------
# Demo media synthesis (used when YouTube is unreachable)
# --------------------------------------------------------------------------
def synth_demo_video(out: Path, duration: int = 90, hue: int = 0) -> Path:
    """Generate a placeholder 16:9 'episode' video with a talking-testcard look."""
    if out.exists() and out.stat().st_size > 10_000:
        return out
    tone = 220 + hue  # distinct audio pitch per episode
    vf = (
        f"testsrc2=size=640x360:rate=15,hue=h={hue}:s=1.2"
    )
    _run(
        [
            "-f", "lavfi", "-i", vf,
            "-f", "lavfi", "-i", f"sine=frequency={tone}:duration={duration}",
            "-t", str(duration),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
            "-c:a", "aac", "-b:a", "64k",
            "-shortest",
            str(out),
        ],
        timeout=600,
    )
    return out
