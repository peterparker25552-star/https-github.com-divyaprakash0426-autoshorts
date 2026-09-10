"""ffmpeg wrappers: render shorts (captions, formats, speeds, silence cuts,
smart motion-tracking crops), loudness waveforms, thumbnails and demo media.

Everything is plain stdlib + the ffmpeg binary. The render must never break:
every analysis helper (motion thirds, silence detection, waveforms, probing)
degrades to a safe fallback instead of raising.
"""
from __future__ import annotations

import math
import re
import subprocess
import uuid
from pathlib import Path

from . import config
from . import logofx
from .transcripts import Segment

# libx264/aac output settings shared by every render path.
_ENCODE = [
    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
    "-pix_fmt", "yuv420p",
    "-c:a", "aac", "-b:a", "128k",
    "-movflags", "+faststart",
]

# A silence pass has to remove at least this much dead air to be worth the
# extra encode; anything less falls back to a normal single-pass render.
MIN_SILENCE_REMOVED = 0.5
SILENCE_MIN_DURATION = 0.4


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


def run_ffmpeg(args: list[str], timeout: int = 900) -> subprocess.CompletedProcess:
    """Public alias of :func:`_run` for sibling modules (audio extract, tools)."""
    return _run(args, timeout=timeout)


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


_VIDEO_SIZE = re.compile(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})")


def probe_video_size(path: Path | str) -> tuple[int, int] | None:
    """(width, height) of the first video stream, or None when unknown."""
    try:
        proc = subprocess.run(
            [config.FFMPEG_BIN, "-hide_banner", "-i", str(path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception:
        return None
    for line in (proc.stderr or "").splitlines():
        if "Video:" not in line:
            continue
        match = _VIDEO_SIZE.search(line)
        if match:
            try:
                return int(match.group(1)), int(match.group(2))
            except ValueError:
                return None
    return None


def has_audio(path: Path, timeout: int = 60) -> bool:
    """True when the file exposes at least one audio stream."""
    try:
        proc = subprocess.run(
            [config.FFMPEG_BIN, "-hide_banner", "-i", str(path)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception:
        return False
    return "Audio:" in (proc.stderr or "")


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


def _escape_expr(expr: str) -> str:
    """Escape a numeric expression for ffmpeg's filter parser ('if(lt(t,..))')."""
    return expr.replace(",", "\\,").replace("'", "")


# --------------------------------------------------------------------------
# ASS caption generation
# --------------------------------------------------------------------------
def _ass_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _ass_seconds(stamp: str) -> float:
    h, m, s = stamp.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def _ass_escape(text: str) -> str:
    """Make arbitrary text safe inside an ASS event body."""
    return (
        text.replace("\\", " / ")
        .replace("{", "(")
        .replace("}", ")")
    )


def _style_params(caption_style: str, play_h: int) -> dict:
    """Font size, weight and placement for one caption style."""
    if caption_style == "pop":
        font_size = max(40, int(play_h * 0.052))
        return {
            "name": "Pop", "font_size": font_size, "bold": -1,
            "outline": max(3, font_size // 16), "margin_v": int(play_h * 0.30),
            "low_margin_v": max(16, int(play_h * 0.045)),
        }
    if caption_style == "minimal":
        font_size = max(24, int(play_h * 0.030))
        return {
            "name": "Min", "font_size": font_size, "bold": 0,
            "outline": max(1, font_size // 22), "margin_v": int(play_h * 0.08),
            "low_margin_v": max(12, int(play_h * 0.025)),
        }
    font_size = max(36, int(play_h * 0.045))
    return {
        "name": "Cap", "font_size": font_size, "bold": -1,
        "outline": max(2, font_size // 18), "margin_v": int(play_h * 0.30),
        "low_margin_v": max(16, int(play_h * 0.045)),
    }


# The default word-pop accent (yellow) when a brand preset says nothing.
_POP_EMPHASIS = "&H0000FFFF"


def brand_bundle(brand: str | None) -> dict | None:
    """Look up a caption brand preset; ``None`` for ``none``/unknown/bad input."""
    if not isinstance(brand, str):
        return None
    return config.CAPTION_BRAND_PRESETS.get(brand.strip().lower())


def brand_style_params(brand: str | None, caption_style: str, play_h: int) -> dict:
    """Style params with a brand bundle (size/colour/outline) folded in.

    Pure function so the preset math is unit-testable without ffmpeg: the brand
    scales the base style's size, replaces the outline thickness rule and
    carries the ASS colours plus the ``pop``/``box``/``pos`` hints.
    """
    params = _style_params(caption_style, play_h)
    bundle = brand_bundle(brand)
    if not bundle:
        return params
    size = max(config.CAPTION_MIN_FONT,
               int(round(params["font_size"] * float(bundle["font_size_scale"]))))
    params["font_size"] = size
    params["outline"] = max(2, int(size * 0.09))
    params["bold"] = int(bundle.get("bold", params["bold"]))
    params["primary"] = bundle["primary"]
    params["outline_colour"] = bundle["outline_colour"]
    params["emphasis"] = bundle["emphasis"]
    params["brand_box"] = bool(bundle.get("box"))
    params["brand_pos"] = str(bundle.get("pos", "standard"))
    params["brand_pop"] = bool(bundle.get("pop"))
    params["name"] = {"qyro-pop": "QPop", "qyro-minimal": "QMin",
                      "qyro-neon": "QNeon"}.get(
        str(brand).strip().lower(), params["name"])
    return params


# Rough average glyph width for DejaVu Sans as a fraction of the font size —
# good enough to decide when a long word would overflow the frame.
_CHAR_WIDTH_FACTOR = 0.62


def autofit_fontsize(
    font_size: int,
    longest_word: int,
    play_w: int,
    min_size: int = config.CAPTION_MIN_FONT,
    side_margin: float = 0.08,
) -> int:
    """Shrink ``font_size`` until the longest word fits the frame width.

    9:16 frames are narrow; a 24-letter word at 67px would clip. The estimate
    is deliberately conservative and never shrinks below ``min_size``.
    """
    if longest_word <= 0:
        return int(font_size)
    usable = int(play_w * (1.0 - side_margin * 2))
    fitting = int(usable / (_CHAR_WIDTH_FACTOR * max(1, longest_word)))
    return max(int(min_size), min(int(font_size), fitting))


def _ass_header(play_w: int, play_h: int, params: dict, box: bool = False) -> str:
    border_style = 3 if box else 1
    if box:
        # Opaque-box look: semi-dark outline box + near-opaque shadow colour.
        outline_colour = "&H64000000"
        back_colour = f"&H{config.CAPTION_BOX_ALPHA:02X}000000"
    else:
        outline_colour = "&H00000000"
        back_colour = "&H64000000"
    # A brand preset may carry its own colours; otherwise the v0.4.0 defaults.
    primary = str(params.get("primary") or "&H00FFFFFF")
    outline_colour = str(params.get("outline_colour") or outline_colour)
    return (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {play_w}\n"
        f"PlayResY: {play_h}\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour,"
        " OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX,"
        " ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment,"
        " MarginL, MarginR, MarginV, Encoding\n"
        f"Style: {params['name']},{config.CAPTION_FONT},{params['font_size']},"
        f"{primary},{primary},{outline_colour},{back_colour},{params['bold']},0,0,0,"
        f"100,100,0,0,{border_style},{params['outline']},1,2,60,60,{params['margin_v']},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV,"
        " Effect, Text\n"
    )


def make_ass(
    segments: list[Segment],
    clip_start: float,
    clip_end: float,
    out_path: Path,
    play_w: int,
    play_h: int,
    words_per_caption: int = config.CAPTION_WORDS_PER_LINE,
    caption_style: str = config.DEFAULT_CAPTIONS,
    speed: float = config.DEFAULT_SPEED,
    pos: str | None = None,
    box: bool | None = None,
    brand: str = config.DEFAULT_CAPTION_BRAND,
) -> Path:
    """Build an ASS subtitle file for [clip_start, clip_end], 0-based times.

    ``caption_style`` picks the look:

    * ``classic`` — uppercase 3-4 word chunks, big and bold (the original look)
    * ``pop`` — one event per word with the active word recoloured, upsized
      and bolded (``{\\c&H0000FFFF&\\b1\\fsN}word{\\r}``)
    * ``minimal`` — small lower-third line, original capitalisation

    ``pos`` — ``standard`` keeps the style's usual bottom margin, ``low``
    lowers MarginV toward the frame edge (thumb-safe zones). ``None`` (the
    default) lets a caption brand choose, then falls back to ``standard``.

    ``box`` — draws a BackgroundStyle box (BorderStyle 3 with a near-opaque
    BackColour) instead of a plain outline (BorderStyle 1).

    ``brand`` (T2) — a Qyro caption brand preset (``qyro-pop``,
    ``qyro-minimal``, ``qyro-neon``) layered on top of the style: it resizes,
    recolours and re-outlines the base look, may force the box, may move the
    line, and switches word-pop timing on for chunked styles. ``none`` (the
    default) reproduces the v0.4.0 file byte-for-byte.

    Font auto-fit shrinks the style font whenever the longest word in the clip
    would overflow the frame width (never below
    :data:`config.CAPTION_MIN_FONT`).

    ``speed`` scales every event timestamp by ``1/speed`` because the renderer
    speeds the media up by the same factor.
    """
    style = caption_style if caption_style in config.CAPTION_STYLES else config.DEFAULT_CAPTIONS
    try:
        rate = float(speed)
    except (TypeError, ValueError):
        rate = 1.0
    if rate <= 0:
        rate = 1.0

    bundle = brand_bundle(brand)
    params = brand_style_params(brand, style, play_h)
    # ``pos``/``box`` are optional here: ``None`` means "the caller did not
    # choose", so a brand bundle may supply them. Anything explicit — what the
    # pipeline always passes after render_opts_from resolved the request —
    # wins, which is how "Qyro Neon with my box off" keeps working.
    if bundle:
        if box is None and bundle.get("box"):
            box = True
        if pos is None and bundle.get("pos"):
            pos = str(bundle["pos"])
    if pos is None:
        pos = config.DEFAULT_CAPTIONS_POS
    if box is None:
        box = config.DEFAULT_CAPTIONS_BOX
    word_pop = style == "pop" or bool(bundle and bundle.get("pop"))
    if str(pos) == "low":
        params["margin_v"] = params["low_margin_v"]
    window = max(float(clip_end) - float(clip_start), 0.01)
    big_size = int(params["font_size"] * 1.3)

    # Pass 1: collect the chunks so auto-fit sees the longest word of the
    # whole clip, then size the font once for the whole file.
    chunks: list[tuple[float, float, list[str]]] = []
    for segment in segments:
        words = [w for w in segment.text.split() if w]
        if not words:
            continue
        duration = max(segment.end - segment.start, 0.4)
        group = words_per_caption
        if style == "minimal":
            group = max(words_per_caption * 2, 6)
        elif style == "pop":
            group = max(1, min(words_per_caption, 3))
        if word_pop and style != "pop":
            group = max(1, min(words_per_caption, 3))
        for offset in range(0, len(words), group):
            part = words[offset : offset + group]
            frac0 = offset / len(words)
            frac1 = min((offset + len(part)) / len(words), 1.0)
            chunk_start = segment.start + duration * frac0
            chunk_end = segment.start + duration * frac1
            if chunk_end <= clip_start or chunk_start >= clip_end:
                continue
            chunks.append((chunk_start, chunk_end, part))

    longest = max((len(max(part, key=len)) for _, _, part in chunks), default=0)
    if longest:
        fitted = autofit_fontsize(params["font_size"], longest, play_w)
        if fitted < params["font_size"]:
            params["font_size"] = fitted
            params["outline"] = max(1, fitted // (11 if bundle else 18))
        big_size = int(params["font_size"] * 1.3)

    emphasis = str(params.get("emphasis") or _POP_EMPHASIS)
    events: list[str] = []

    def emit(rel_start: float, rel_end: float, text: str) -> None:
        start_scaled = max(0.0, rel_start) / rate
        end_scaled = max(start_scaled + 0.05, rel_end / rate)
        events.append(
            f"Dialogue: 0,{_ass_time(start_scaled)},{_ass_time(end_scaled)},"
            f"{params['name']},,0,0,0,,{text}"
        )

    for chunk_start, chunk_end, part in chunks:
        rel_start = max(0.0, chunk_start - clip_start)
        rel_end = min(window, chunk_end - clip_start)
        if rel_end - rel_start < 0.15:
            rel_end = min(window, rel_start + 0.4)

        if word_pop:
            span = max(rel_end - rel_start, 0.2) / len(part)
            for index, word in enumerate(part):
                piece = [_ass_escape(other) for other in part]
                piece[index] = (
                    f"{{\\c{emphasis}&\\b1\\fs{big_size}}}"
                    f"{_ass_escape(word)}{{\\r}}"
                )
                emit(
                    rel_start + index * span,
                    min(rel_end, rel_start + (index + 1) * span),
                    " ".join(piece),
                )
        else:
            text = " ".join(_ass_escape(word) for word in part)
            if style == "classic":
                text = text.upper()
            emit(rel_start, rel_end, text)

    header = _ass_header(play_w, play_h, params, box=bool(box))
    out_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return out_path


# --------------------------------------------------------------------------
# Filter builders
# --------------------------------------------------------------------------
def audio_filter(speed: float = config.DEFAULT_SPEED, loud: bool = False) -> str:
    """Audio chain for ``-af``: ``"atempo=x,loudnorm=..."`` or ``""``."""
    parts: list[str] = []
    try:
        rate = float(speed)
    except (TypeError, ValueError):
        rate = 1.0
    if rate > 0 and abs(rate - 1.0) > 1e-9:
        parts.append(f"atempo={rate:g}")
    if loud:
        parts.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    return ",".join(parts)


def _even(value: float) -> int:
    """Round to the nearest even int (chroma-friendly). Never negative."""
    n = int(round(float(value) / 2.0) * 2)
    return max(0, n)


def crop_dims(
    src_w: int, src_h: int, out_w: int, out_h: int
) -> tuple[int, int]:
    """Even (w, h) crop window with the output aspect, inside the source."""
    src_w = max(2, int(src_w))
    src_h = max(2, int(src_h))
    ratio = float(out_w) / max(1, float(out_h))
    cw = _even(src_h * ratio)
    if cw <= src_w:
        return cw, _even(src_h)
    ch = _even(src_w / ratio)
    return _even(src_w), min(ch, _even(src_h))


def _stepped_x_expr(points: list[tuple[float, int]]) -> str:
    """Build a stepped numeric x(t) expression: a chain of ``if(lt(t,…))``.

    ``points`` is ascending ``[(t, x)]`` where ``x`` applies from ``t`` until
    the next boundary (the last x holds to the end). Never references
    ``ow``/``iw`` — every offset is a baked numeric literal.
    """
    if not points:
        return "0"
    expr = str(int(points[-1][1]))
    for i in range(len(points) - 2, -1, -1):
        t_next = float(points[i + 1][0])
        expr = f"if(lt(t,{t_next:.2f}),{int(points[i][1])},{expr})"
    return expr


def smart_x_expression(
    positions: list[tuple[float, int]],
    keeps: list[tuple[float, float]],
    speed: float,
    crop_w: int,
    src_w: int,
) -> str:
    """Map per-chunk winners onto the OUTPUT timeline as a stepped x(t).

    ``positions`` are ``(source_window_time, third_index)`` pairs from the
    motion proxy. Each source timestamp is remapped through the silence keeps
    (jump cuts) and divided by ``speed`` so the crop lands on the same spoken
    moment in the rendered clip. Offsets are even ints clamped to
    ``[0, src_w - crop_w]``.
    """
    try:
        rate = float(speed)
    except (TypeError, ValueError):
        rate = 1.0
    if rate <= 0:
        rate = 1.0
    span = max(int(src_w) - int(crop_w), 0)
    mapped: dict[float, int] = {}
    for t_src, third in positions:
        t_out = remap_time(float(t_src), keeps or [(0.0, math.inf)]) / rate
        x = min(_even(third * span / 2.0), span) if span else 0
        mapped[round(t_out, 3)] = x  # later chunks win identical timestamps
    if not mapped:
        return "0"
    points = sorted(mapped.items())
    if points[0][0] > 0.011:  # anchor t=0 to the earliest winner
        points.insert(0, (0.0, points[0][1]))
    # merge consecutive equal offsets (keeps the expression short)
    merged: list[tuple[float, int]] = []
    for t, x in points:
        if merged and merged[-1][1] == x:
            continue
        merged.append((t, x))
    if len(merged) == 1:
        return str(merged[0][1])
    return _stepped_x_expr(merged)


def pick_third(
    scores: dict[str, float],
    incumbent: str,
    calm: float = config.SMART_CALM_YDIF,
    hysteresis: float = config.SMART_HYSTERESIS,
) -> str:
    """Motion decision for one chunk — pure math, easy to unit-test.

    * empty/no-signal scores → ``center``
    * a calm chunk (max mean YDIF below ``calm``) → ``center``
    * the incumbent holds unless a challenger is *clearly* higher
      (``score > incumbent * hysteresis``)
    """
    vals = {
        side: float(scores.get(side) or 0.0)
        for side in config.SMART_POSITIONS
    }
    best = max(vals, key=lambda side: vals[side])
    if not scores or vals[best] <= 0.0:
        return "center"
    if vals[best] < calm:
        return "center"
    if best == incumbent:
        return incumbent
    if vals[best] > vals.get(incumbent, 0.0) * hysteresis:
        return best
    return incumbent


_YDIF_FRAME = re.compile(r"frame:\d+\s+pts:\d+\s+pts_time:([-\d.]+)")
_YDIF_VALUE = re.compile(r"lavfi\.signalstats\.YDIF=([-+\d.eE]+|inf|-inf|nan)")


def _third_ydif(
    src: Path, start: float, dur: float, third: int, proxy_w: int
) -> list[tuple[float, float]]:
    """Per-frame (pts_time, YDIF) for one third of a tiny grayscale proxy."""
    third_w = max(8, int(proxy_w) // 3)
    offset = min(third * third_w, max(0, int(proxy_w) - third_w))
    vf = (
        f"scale={int(proxy_w)}:-2,format=gray,"
        f"crop={third_w}:ih:{offset}:0,"
        "signalstats,metadata=print:key=lavfi.signalstats.YDIF:file=-"
    )
    proc = subprocess.run(
        [
            config.FFMPEG_BIN, "-hide_banner", "-nostats", "-loglevel", "error",
            "-ss", f"{float(start):.3f}", "-t", f"{float(dur):.3f}",
            "-i", str(src),
            "-vf", vf,
            "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-200:] if proc.stderr else "YDIF analysis failed")
    series: list[tuple[float, float]] = []
    current_t: float | None = None
    for line in (proc.stdout or "").splitlines():
        frame = _YDIF_FRAME.search(line)
        if frame:
            current_t = float(frame.group(1))
            continue
        value = _YDIF_VALUE.search(line)
        if value and current_t is not None:
            try:
                ydif = float(value.group(1))
            except ValueError:
                ydif = 0.0
            if math.isfinite(ydif):
                series.append((current_t, max(0.0, ydif)))
            current_t = None
    return series


def _chunk_means(
    series: list[tuple[float, float]], duration: float, chunk: float
) -> list[float]:
    """Mean YDIF per ``chunk``-second bucket (0.0 for empty buckets)."""
    n = max(1, int(math.ceil(float(duration) / float(chunk))))
    sums = [0.0] * n
    counts = [0] * n
    for t, v in series:
        idx = min(n - 1, max(0, int(t / chunk)))
        sums[idx] += v
        counts[idx] += 1
    return [sums[i] / counts[i] if counts[i] else 0.0 for i in range(n)]


def analyze_motion(
    src: Path,
    start: float,
    end: float,
    chunk: float = config.SMART_CHUNK_SECONDS,
    proxy_w: int = config.SMART_PROXY_WIDTH,
) -> list[tuple[float, int]]:
    """Per-chunk motion winners as ``(source_window_time, third_index)``.

    The source window is analysed on a scaled-down proxy — every returned
    timestamp is already source-window time, ready to be remapped through any
    silence cuts. Raises on failure; the caller falls back to a static crop.
    """
    dur = max(float(end) - float(start), 0.5)
    n_chunks = max(1, int(math.ceil(dur / float(chunk))))
    means: dict[str, list[float]] = {}
    for index, side in enumerate(config.SMART_POSITIONS):
        series = _third_ydif(src, float(start), dur, index, proxy_w)
        means[side] = _chunk_means(series, dur, chunk)
    positions: list[tuple[float, int]] = []
    incumbent = "center"
    for i in range(n_chunks):
        scores = {side: means[side][i] for side in config.SMART_POSITIONS}
        incumbent = pick_third(scores, incumbent)
        positions.append((round(i * float(chunk), 3),
                          config.SMART_POSITIONS.index(incumbent)))
    return positions


def smart_crop_for_window(
    src: Path,
    start: float,
    end: float,
    out_w: int,
    out_h: int,
    keeps: list[tuple[float, float]] | None,
    speed: float,
) -> tuple[int, int, str] | None:
    """(crop_w, crop_h, x-expression) for a smart render, or None on failure.

    ANY failure (missing source size, dead ffmpeg, empty analysis) returns
    ``None`` so the render falls back to a static center crop instead of
    breaking.
    """
    try:
        size = probe_video_size(src)
        if not size:
            return None
        src_w, src_h = size
        cw, ch = crop_dims(src_w, src_h, int(out_w), int(out_h))
        if src_w - cw < 8:  # nothing to track — already full-width
            return cw, ch, "0"
        positions = analyze_motion(src, float(start), float(end))
        expr = smart_x_expression(
            positions, keeps or [], float(speed), cw, src_w
        )
        return cw, ch, expr
    except Exception:
        return None


def video_chain(
    style: str,
    width: int,
    height: int,
    ass: Path | str | None = None,
    out_dur: float | None = None,
    progress: bool = False,
    vin: str = "0:v",
    fmt: str = config.DEFAULT_FORMAT,
    smart: tuple[int, int, str] | None = None,
    speed: float = config.DEFAULT_SPEED,
) -> str:
    """Video filter chain that ends in ``[v]``.

    ``fmt`` drives the frame geometry; ``style`` drives the framing look:

    * ``wide`` format — plain scale + pad; styles are ignored because a 16:9
      cut always needs the full frame
    * ``blur`` — blurred background + sharp fitted foreground
    * ``crop`` / ``fill`` — scale to fill, center crop
    * ``fit`` — scale to fit, padded with black
    * ``smart`` — motion-tracking crop (``smart`` carries the pre-computed
      ``(crop_w, crop_h, x_expr)``; without it, a static center crop)

    ``speed`` > 1 compresses the *single-pass* timeline (``setpts``); the
    silence path already bakes speed into its per-segment chains, so it passes
    1.0 here.

    ``progress`` appends an amber ``drawbox`` bar along the top edge whose
    width is ``iw*min(t/D\\,1)``.
    """
    width = max(2, int(width))
    height = max(2, int(height))
    try:
        rate = float(speed)
    except (TypeError, ValueError):
        rate = 1.0
    if rate <= 0:
        rate = 1.0
    wide = fmt == "wide" or style == "wide"

    if wide:
        head = (
            f"[{vin}]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
        )
    elif style == "smart":
        if smart:
            cw, ch, expr = smart
            head = (
                f"[{vin}]crop={int(cw)}:{int(ch)}:'{_escape_expr(expr)}':0,"
                f"scale={width}:{height}"
            )
        else:
            head = (
                f"[{vin}]scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height}"
            )
    elif style in ("crop", "fill"):
        head = (
            f"[{vin}]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}"
        )
    elif style == "fit":
        head = (
            f"[{vin}]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
        )
    else:  # blur (default)
        head = (
            f"[{vin}]split=2[bg][fg];"
            f"[bg]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},gblur=sigma=24[bgb];"
            f"[fg]scale={width}:-2[fgs];"
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2"
        )

    if abs(rate - 1.0) > 1e-9:
        head += f",setpts=PTS/{rate:g}"

    tail = ""
    if ass:
        tail += f",ass={_escape_filter(str(ass))}"
    if progress and out_dur and float(out_dur) > 0:
        tail += (
            f",drawbox=x=0:y=0:w='iw*min(t/{float(out_dur):.3f}\\,1)':h=6:"
            f"color={config.PROGRESS_COLOR}@0.9:t=fill"
        )
    return f"{head}{tail}[v]"


# --------------------------------------------------------------------------
# Silence detection + jump cuts
# --------------------------------------------------------------------------
_SILENCE_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?[\d.]+)")


def parse_silences(
    stderr: str, min_dur: float = SILENCE_MIN_DURATION
) -> list[tuple[float, float]]:
    """Parse ``silencedetect`` stderr into ``(start, end)`` pairs."""
    found: list[tuple[float, float]] = []
    open_start: float | None = None
    for line in str(stderr or "").splitlines():
        match = _SILENCE_START.search(line)
        if match:
            open_start = float(match.group(1))
            continue
        match = _SILENCE_END.search(line)
        if match and open_start is not None:
            end = float(match.group(1))
            if end - open_start >= min_dur:
                found.append((max(0.0, open_start), max(0.0, end)))
            open_start = None
    found.sort()
    return found


def invert_ranges(
    silences: list[tuple[float, float]],
    start: float,
    end: float,
    min_keep: float = 0.35,
) -> list[tuple[float, float]]:
    """Complement of ``silences`` inside ``[start, end]`` — the speech to keep."""
    keep: list[tuple[float, float]] = []
    cursor = float(start)
    for raw_start, raw_end in sorted(silences):
        silence_start = max(float(raw_start), float(start))
        silence_end = min(float(raw_end), float(end))
        if silence_end <= cursor:
            continue
        if silence_start > cursor:
            keep.append((cursor, silence_start))
        cursor = max(cursor, silence_end)
    if cursor < float(end):
        keep.append((cursor, float(end)))
    return [(a, b) for a, b in keep if b - a >= min_keep]


def remap_time(t: float, ranges: list[tuple[float, float]]) -> float:
    """Map a source time onto the jump-cut (concatenated) timeline.

    With the identity keeps ``[(0, inf)]`` this is the identity. Times inside
    a removed silence clamp to the next kept range's start, so an event
    spanning a cut simply shrinks.
    """
    offset = 0.0
    for start, end in ranges:
        if t < start:
            return offset
        if t < end:
            return offset + (t - start)
        offset += end - start
    return offset


def remap_ass_file(
    src: Path | str,
    ranges: list[tuple[float, float]],
    out_path: Path | str,
    speed: float = config.DEFAULT_SPEED,
    drop: float = 0.05,
) -> Path:
    """Rewrite an ASS file onto the jump-cut timeline, dropping tiny events."""
    src_path = Path(src)
    out_path = Path(out_path)
    try:
        rate = float(speed)
    except (TypeError, ValueError):
        rate = 1.0
    if rate <= 0:
        rate = 1.0

    lines: list[str] = []
    for line in src_path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("Dialogue:"):
            lines.append(line)
            continue
        fields = line.split(",", 9)
        if len(fields) < 10:
            continue
        try:
            # ASS times are already speed-scaled; undo that to get source time.
            start_source = _ass_seconds(fields[1]) * rate
            end_source = _ass_seconds(fields[2]) * rate
        except ValueError:
            continue
        new_start = remap_time(start_source, ranges) / rate
        new_end = remap_time(end_source, ranges) / rate
        if new_end - new_start < drop:
            continue
        lines.append(
            f"{fields[0]},{_ass_time(new_start)},{_ass_time(new_end)},"
            + ",".join(fields[3:])
        )
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path


def build_concat_filter(
    ranges: list[tuple[float, float]],
    speed: float = config.DEFAULT_SPEED,
    audio: bool = True,
    vin: str = "0:v",
    ain: str = "0:a",
) -> str:
    """trim + concat the kept ranges into ``[ccv][cca]`` (``[ccv]`` w/o audio).

    ``speed`` is applied per trimmed segment (``setpts``/``atempo``) so the
    concatenated stream is already at final speed. ``vin``/``ain`` let the
    caller feed a pre-processed input (the logo stage renames ``0:v``).
    """
    try:
        rate = float(speed)
    except (TypeError, ValueError):
        rate = 1.0
    if rate <= 0:
        rate = 1.0

    chains: list[str] = []
    labels: list[str] = []
    for index, (start, end) in enumerate(ranges):
        video = f"[{vin}]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS"
        if abs(rate - 1.0) > 1e-9:
            video += f",setpts=PTS/{rate:g}"
        chains.append(f"{video}[v{index}]")
        if audio:
            audio_chain = (
                f"[{ain}]atrim=start={start:.3f}:end={end:.3f},"
                "asetpts=PTS-STARTPTS"
            )
            if abs(rate - 1.0) > 1e-9:
                audio_chain += f",atempo={rate:g}"
            chains.append(f"{audio_chain}[a{index}]")
            labels.append(f"[v{index}][a{index}]")
        else:
            labels.append(f"[v{index}]")

    count = len(ranges)
    if audio:
        chains.append(
            f"{''.join(labels)}concat=n={count}:v=1:a=1[ccv][cca]"
        )
    else:
        chains.append(f"{''.join(labels)}concat=n={count}:v=1:a=0[ccv]")
    return ";".join(chains)


def cut_window(src: Path | str, start: float, end: float, out: Path | str) -> Path:
    """Re-encode just ``[start, end]`` of ``src`` into ``out`` (timestamps at 0)."""
    out = Path(out)
    _run(
        [
            "-ss", f"{float(start):.3f}",
            "-t", f"{max(float(end) - float(start), 0.5):.3f}",
            "-i", str(src),
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(out),
        ]
    )
    return out


def detect_silences(
    path: Path | str,
    min_dur: float = SILENCE_MIN_DURATION,
    noise: str = "-35dB",
    timeout: int = 600,
) -> list[tuple[float, float]]:
    """Run ffmpeg's silencedetect over a file (never raises)."""
    try:
        proc = subprocess.run(
            [
                config.FFMPEG_BIN, "-hide_banner", "-nostats",
                "-i", str(path),
                "-af", f"silencedetect=noise={noise}:d={min_dur}",
                "-f", "null", "-",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            return []
        return parse_silences(proc.stderr or "", min_dur=min_dur)
    except Exception:
        return []


# --------------------------------------------------------------------------
# Loudness waveform (per-clip mini bars for the card UI)
# --------------------------------------------------------------------------
_R128_FRAME = re.compile(r"pts_time:([-\d.]+)")
_R128_VALUE = re.compile(r"lavfi\.r128\.M=([-+\d.eE]+|-inf|inf|nan)")


def loudness_waveform(
    path: Path | str, bars: int = config.WAVEFORM_BARS, timeout: int = 300
) -> list[float]:
    """``bars`` loudness values in [0, 1] via ebur128 momentary loudness.

    Returns ``[]`` on any failure (no audio, dead ffmpeg, unparsable output) —
    the card UI simply hides the strip.
    """
    path = Path(path)
    bars = max(4, int(bars))
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
        samples: list[tuple[float, float]] = []
        current_t: float | None = None
        for line in (proc.stdout or "").splitlines():
            frame = _R128_FRAME.search(line)
            if frame:
                current_t = float(frame.group(1))
                continue
            value = _R128_VALUE.search(line)
            if value and current_t is not None:
                try:
                    m = float(value.group(1))
                except ValueError:
                    m = float("nan")
                if math.isfinite(m):
                    samples.append((current_t, m))
                current_t = None
        if not samples:
            return []
        duration = max(samples[-1][0], 0.1)
        means = _chunk_means(samples, duration, duration / bars)
        lo, hi = min(means), max(means)
        if hi - lo < 0.5:  # near-constant loudness → flat, visible strip
            return [round(0.6, 3)] * bars
        span = hi - lo
        return [round(0.06 + 0.94 * max(0.0, min(1.0, (m - lo) / span)), 3)
                for m in means]
    except Exception:
        return []


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
def render_clip(
    src: Path,
    start: float,
    end: float,
    out: Path,
    ass_path: Path | str | None = None,
    style: str = config.DEFAULT_STYLE,
    width: int = 720,
    height: int = 1280,
    speed: float = config.DEFAULT_SPEED,
    progress: bool = False,
    silence: bool = False,
    loud: bool = False,
    fmt: str = config.DEFAULT_FORMAT,
    logo: dict | None = None,
    track: Path | str | None = None,
    audio_mix: str = config.DEFAULT_AUDIO_MIX,
    silence_noise: float = config.DEFAULT_SILENCE_NOISE,
    silence_min: float = config.DEFAULT_SILENCE_MIN,
) -> Path:
    """Render one short, degrading *optional* stages instead of failing.

    :func:`_render_clip` does the work. This wrapper exists for the v0.5.0
    promise that nothing may break: if the logo pass or the audio swap is what
    ffmpeg choked on, the same clip is retried without that stage (logo, then
    track, then both), so a bad box or an unreadable music file costs a
    feature, never the short.
    """
    base = dict(
        ass_path=ass_path, style=style, width=width, height=height, speed=speed,
        progress=progress, silence=silence, loud=loud, fmt=fmt,
        audio_mix=audio_mix, silence_noise=silence_noise, silence_min=silence_min,
    )
    variants: list[tuple[dict | None, Path | str | None]] = [(logo, track)]
    if logo:
        variants.append((None, track))
    if track:
        variants.append((logo, None))
    if logo and track:
        variants.append((None, None))
    for index, (logo_try, track_try) in enumerate(variants):
        try:
            return _render_clip(Path(src), float(start), float(end), Path(out),
                                logo=logo_try, track=track_try, **base)
        except RuntimeError:
            if index + 1 >= len(variants):
                raise
            continue
    raise RuntimeError("ffmpeg failed while rendering a clip.")


def _render_clip(
    src: Path,
    start: float,
    end: float,
    out: Path,
    ass_path: Path | str | None = None,
    style: str = config.DEFAULT_STYLE,
    width: int = 720,
    height: int = 1280,
    speed: float = config.DEFAULT_SPEED,
    progress: bool = False,
    silence: bool = False,
    loud: bool = False,
    fmt: str = config.DEFAULT_FORMAT,
    logo: dict | None = None,
    track: Path | str | None = None,
    audio_mix: str = config.DEFAULT_AUDIO_MIX,
    silence_noise: float = config.DEFAULT_SILENCE_NOISE,
    silence_min: float = config.DEFAULT_SILENCE_MIN,
) -> Path:
    """Cut ``[start, end]`` out of ``src`` into a captioned short.

    With ``silence`` the window is cut to a scratch file first, silences are
    detected there (timestamps are already source-window time), and when at
    least :data:`MIN_SILENCE_REMOVED` seconds of dead air can go the clip is
    rebuilt with a trim+concat filtergraph (and the ASS file remapped onto the
    new timeline). Otherwise — or when nothing meaningful was found — it
    falls through to the normal single-pass render.

    ``style`` ``smart`` tracks the active third of the frame: motion winners
    are measured on a scaled proxy, remapped through the silence cuts + speed
    onto the output timeline, and baked into a stepped numeric crop
    expression. Any analysis failure degrades to a static center crop.

    v0.5.0 additions, all optional and all safe to ignore:

    * ``logo`` — a validated ``logo_box`` spec. The removal filter is spliced
      in **first** (on the source frame, before framing/tracking) so the mark
      is gone before anything scales it; ``delogo`` with a ``boxblur`` fallback.
    * ``track`` + ``audio_mix`` — T3 audio swap. ``replace`` drops the clip's
      own audio and uses the track (trimmed to length, loudness matched);
      ``duck`` keeps the original at 20% with the track on top.
    * ``silence_noise`` / ``silence_min`` — T6 silence tuner: the dB floor and
      minimum pause length handed to ``silencedetect``.
    """
    window = max(float(end) - float(start), 0.5)
    try:
        rate = float(speed)
    except (TypeError, ValueError):
        rate = 1.0
    if rate <= 0:
        rate = 1.0

    src_size = probe_video_size(src)
    src_w, src_h = src_size if src_size else (0, 0)

    def smart_plan(keeps: list[tuple[float, float]] | None):
        if style != "smart" or fmt == "wide":
            return None
        return smart_crop_for_window(src, float(start), float(end),
                                     width, height, keeps, rate)

    audio_ok = has_audio(src)
    track_path = Path(track) if track else None
    track_ok = bool(track_path and track_path.is_file())

    def logo_prefix(vin: str) -> tuple[str, str]:
        """(graph prefix, input label) for the logo pass in source space."""
        if not logo or src_w <= 0 or src_h <= 0:
            return "", vin
        stage = logofx.logo_stage(logo, src_w, src_h, vin=vin)
        if not stage:
            return "", vin
        return stage[0] + ";", stage[1]

    afilter = audio_filter(rate, loud) if (audio_ok and not track_ok) else ""
    scratch: list[Path] = []

    try:
        if silence:
            cut_path = config.MEDIA_DIR / f".cut-{uuid.uuid4().hex[:10]}.mp4"
            scratch.append(cut_path)
            cut_window(src, float(start), float(end), cut_path)
            silences = detect_silences(
                cut_path, min_dur=silence_min, noise=f"{float(silence_noise):g}dB"
            )
            removed = sum(b - a for a, b in silences)
            ranges = invert_ranges(silences, 0.0, window) if silences else []
            if removed >= MIN_SILENCE_REMOVED and ranges:
                mapped_ass: Path | None = None
                if ass_path:
                    mapped_ass = config.SUBS_DIR / f"{out.stem}-cut.ass"
                    scratch.append(mapped_ass)
                    remap_ass_file(ass_path, ranges, mapped_ass, speed=rate)
                out_dur = sum(b - a for a, b in ranges) / rate
                # The original bed only needs cutting when it survives (no
                # track, or ducking under one); ``replace`` skips the work.
                needs_source_audio = audio_ok and (not track_ok or audio_mix == "duck")
                prefix, vin = logo_prefix("0:v")
                graph = build_concat_filter(
                    ranges, speed=rate, audio=needs_source_audio, vin=vin
                )
                graph += ";" + video_chain(
                    style, width, height, mapped_ass, out_dur, progress,
                    vin="ccv", fmt=fmt, smart=smart_plan(ranges), speed=1.0,
                )
                audio_label: str | None = None
                if track_ok:
                    from . import audioswap

                    plan = audioswap.mix_plan(
                        audio_mix, out_dur,
                        source_label="cca" if needs_source_audio else None,
                        speed=1.0, loud=loud,
                    )
                    graph += ";" + plan["graph"]
                    audio_label = plan["label"]
                elif needs_source_audio:
                    audio_label = "[cca]"
                    if loud:
                        graph += ";[cca]loudnorm=I=-16:TP=-1.5:LRA=11[a]"
                        audio_label = "[a]"
                args = ["-i", str(cut_path)]
                if track_ok:
                    args += ["-stream_loop", "-1", "-i", str(track_path)]
                args += ["-filter_complex", f"{prefix}{graph}", "-map", "[v]"]
                if audio_label:
                    args += ["-map", audio_label]
                args += [*_ENCODE, str(out)]
                _run(args)
                return out
    finally:
        for path in scratch:
            path.unlink(missing_ok=True)

    # Single pass (also the fallback when silence removal found nothing).
    prefix, vin = logo_prefix("0:v")
    graph = video_chain(
        style, width, height, ass_path, window / rate, progress, vin=vin,
        fmt=fmt, smart=smart_plan([(0.0, window)]), speed=rate,
    )
    args = [
        "-ss", f"{float(start):.3f}",
        "-t", f"{window:.3f}",
        "-i", str(src),
    ]
    if track_ok:
        args += ["-stream_loop", "-1", "-i", str(track_path)]
        from . import audioswap

        plan = audioswap.mix_plan(
            audio_mix, window / rate, source_label="0:a" if audio_ok else None,
            speed=rate, loud=loud,
        )
        graph = f"{prefix}{graph};{plan['graph']}"
        args += ["-filter_complex", graph, "-map", "[v]", "-map", plan["label"]]
    else:
        graph = f"{prefix}{graph}" if prefix else graph
        args += ["-filter_complex", graph, "-map", "[v]", "-map", "0:a?"]
    if afilter:
        args += ["-af", afilter]
    args += [*_ENCODE, str(out)]
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
# Clip Inspector (T6) + Thumbnail Picker (T6)
# --------------------------------------------------------------------------
_PROBE_VIDEO = re.compile(r"Video:\s*([A-Za-z0-9_\-]+)")
_PROBE_SIZE = re.compile(r"(\d{2,5})x(\d{2,5})")
_PROBE_FPS = re.compile(r"([\d.]+)\s*fps")
_PROBE_AUDIO = re.compile(r"Audio:\s*([A-Za-z0-9_\-]+)")
_PROBE_RATE = re.compile(r"([\d.]+)\s*Hz")


def probe_media(path: Path | str, timeout: int = 60) -> dict:
    """Resolution / fps / codecs / duration / size of a media file.

    Parsed from ``ffmpeg -i`` (a standalone ``ffprobe`` is *not* a dependency
    of this project, so probing must survive without it). Every field is
    ``None`` when it cannot be read — the endpoint still answers 200.
    """
    path = Path(path)
    info: dict = {
        "path": str(path),
        "exists": path.is_file(),
        "size_bytes": None,
        "duration": None,
        "width": None,
        "height": None,
        "fps": None,
        "video_codec": None,
        "audio_codec": None,
        "sample_rate": None,
        "aspect": None,
        "orientation": None,
    }
    if not info["exists"]:
        return info
    try:
        info["size_bytes"] = path.stat().st_size
    except OSError:
        pass
    try:
        proc = subprocess.run(
            [config.FFMPEG_BIN, "-hide_banner", "-i", str(path)],
            capture_output=True, text=True, timeout=timeout,
        )
        text = (proc.stderr or "") + (proc.stdout or "")
    except Exception:
        return info
    info["duration"] = probe_duration(path)
    for line in text.splitlines():
        if "Video:" in line and info["video_codec"] is None:
            codec = _PROBE_VIDEO.search(line)
            size = _PROBE_SIZE.search(line)
            fps = _PROBE_FPS.search(line)
            if codec:
                info["video_codec"] = codec.group(1)
            if size:
                info["width"], info["height"] = int(size.group(1)), int(size.group(2))
            if fps:
                try:
                    info["fps"] = round(float(fps.group(1)), 3)
                except ValueError:
                    info["fps"] = None
        elif "Audio:" in line and info["audio_codec"] is None:
            codec = _PROBE_AUDIO.search(line)
            rate = _PROBE_RATE.search(line)
            if codec:
                info["audio_codec"] = codec.group(1)
            if rate:
                try:
                    info["sample_rate"] = int(float(rate.group(1)))
                except ValueError:
                    info["sample_rate"] = None
    if info["width"] and info["height"]:
        info["aspect"] = f"{info['width']}x{info['height']}"
        if info["height"]:
            info["orientation"] = (
                "vertical" if info["width"] < info["height"]
                else "square" if info["width"] == info["height"] else "wide"
            )
    return info


def frame_candidates(clip: Path | str, count: int, out_dir: Path,
                     stem: str, quality: int = 4) -> list[dict]:
    """``count`` evenly spread JPEG candidates for the thumbnail picker.

    Frames are grabbed at the centre of each slice (so no black fade-in frame,
    no two frames from the same second). Failures yield fewer entries instead
    of raising — the picker just shows what it could read.
    """
    clip = Path(clip)
    count = max(1, min(int(count), config.THUMB_CANDIDATES[1]))
    duration = probe_duration(clip) or 0.0
    if duration <= 0:
        return []
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    found: list[dict] = []
    for index in range(count):
        stamp = round(min(max(duration * (index + 0.5) / count, 0.05),
                          max(duration - 0.05, 0.05)), 3)
        target = out_dir / f"{stem}-cand-{index}.jpg"
        try:
            _run(
                [
                    "-ss", f"{stamp:.3f}", "-i", str(clip),
                    "-frames:v", "1", "-vf", "scale=720:-2",
                    "-q:v", str(max(1, min(int(quality), 31))),
                    str(target),
                ],
                timeout=90,
            )
        except Exception:
            continue
        if target.is_file() and target.stat().st_size > 0:
            found.append({"index": index, "time": stamp, "file": target.name})
    return found


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
