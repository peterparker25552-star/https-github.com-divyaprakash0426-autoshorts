"""ffmpeg wrappers: render shorts (captions, formats, speeds, silence cuts),
thumbnails and demo media.
"""
from __future__ import annotations

import re
import subprocess
import uuid
from pathlib import Path

from . import config
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
        }
    if caption_style == "minimal":
        font_size = max(24, int(play_h * 0.030))
        return {
            "name": "Min", "font_size": font_size, "bold": 0,
            "outline": max(1, font_size // 22), "margin_v": int(play_h * 0.08),
        }
    font_size = max(36, int(play_h * 0.045))
    return {
        "name": "Cap", "font_size": font_size, "bold": -1,
        "outline": max(2, font_size // 18), "margin_v": int(play_h * 0.30),
    }


def _ass_header(play_w: int, play_h: int, params: dict) -> str:
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
        f"&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,{params['bold']},0,0,0,"
        f"100,100,0,0,1,{params['outline']},1,2,60,60,{params['margin_v']},1\n\n"
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
) -> Path:
    """Build an ASS subtitle file for [clip_start, clip_end], 0-based times.

    ``caption_style`` picks the look:

    * ``classic`` — uppercase 3-4 word chunks, big and bold (the original look)
    * ``pop`` — one event per word with the active word recoloured, upsized
      and bolded (``{\\c&H0000FFFF&\\b1\\fsN}word{\\r}``)
    * ``minimal`` — small lower-third line, original capitalisation

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

    params = _style_params(style, play_h)
    window = max(float(clip_end) - float(clip_start), 0.01)
    big_size = int(params["font_size"] * 1.3)

    events: list[str] = []

    def emit(rel_start: float, rel_end: float, text: str) -> None:
        start_scaled = max(0.0, rel_start) / rate
        end_scaled = max(start_scaled + 0.05, rel_end / rate)
        events.append(
            f"Dialogue: 0,{_ass_time(start_scaled)},{_ass_time(end_scaled)},"
            f"{params['name']},,0,0,0,,{text}"
        )

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

        for offset in range(0, len(words), group):
            part = words[offset : offset + group]
            frac0 = offset / len(words)
            frac1 = min((offset + len(part)) / len(words), 1.0)
            chunk_start = segment.start + duration * frac0
            chunk_end = segment.start + duration * frac1
            if chunk_end <= clip_start or chunk_start >= clip_end:
                continue
            rel_start = max(0.0, chunk_start - clip_start)
            rel_end = min(window, chunk_end - clip_start)
            if rel_end - rel_start < 0.15:
                rel_end = min(window, rel_start + 0.4)

            if style == "pop":
                span = max(rel_end - rel_start, 0.2) / len(part)
                for index, word in enumerate(part):
                    piece = [
                        _ass_escape(other) for other in part
                    ]
                    piece[index] = (
                        f"{{\\c&H0000FFFF&\\b1\\fs{big_size}}}"
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

    header = _ass_header(play_w, play_h, params)
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


def video_chain(
    style: str,
    width: int,
    height: int,
    ass: Path | str | None = None,
    out_dur: float | None = None,
    progress: bool = False,
    vin: str = "0:v",
) -> str:
    """Video filter chain that ends in ``[v]``.

    ``style`` doubles as the framing mode:

    * ``wide`` — scale down and pad with black bars (letterbox), ignoring the
      blur/crop style because a 16:9 cut always needs the full frame
    * ``crop`` — centre crop, parameterised by the target width/height ratio
    * anything else — the blur-background + centred foreground look

    ``progress`` appends a ``drawbox`` bar whose width is
    ``iw*min(t/D\\,1)`` (the comma stays backslash-escaped for ffmpeg's filter
    parser).
    """
    width = max(2, int(width))
    height = max(2, int(height))

    if style == "wide":
        head = (
            f"[{vin}]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
        )
    elif style == "crop":
        head = (
            f"[{vin}]crop='min(iw,ih*{width}/{height})':"
            f"'min(ih,iw*{height}/{width})',"
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}"
        )
    else:
        head = (
            f"[{vin}]split=2[bg][fg];"
            f"[bg]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},gblur=sigma=24[bgb];"
            f"[fg]scale={width}:-2[fgs];"
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2"
        )

    tail = ""
    if ass:
        tail += f",ass={_escape_filter(str(ass))}"
    if progress and out_dur and float(out_dur) > 0:
        tail += (
            f",drawbox=x=0:y=0:w='iw*min(t/{float(out_dur):.3f}\\,1)':h=6:"
            "color=0xFF4D6D@0.9:t=fill"
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

    Times inside a removed silence clamp to the next kept range's start, so an
    event spanning a cut simply shrinks — :func:`remap_ass_file` drops it when
    what is left is shorter than the drop threshold.
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
) -> str:
    """trim + concat the kept ranges into ``[ccv][cca]`` (``[ccv]`` w/o audio).

    ``speed`` is applied per trimmed segment (``setpts``/``atempo``) so the
    concatenated stream is already at final speed.
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
        video = f"[0:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS"
        if abs(rate - 1.0) > 1e-9:
            video += f",setpts=PTS/{rate:g}"
        chains.append(f"{video}[v{index}]")
        if audio:
            audio_chain = (
                f"[0:a]atrim=start={start:.3f}:end={end:.3f},"
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
    except Exception:
        return []
    return parse_silences(proc.stderr or "", min_dur=min_dur)


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
) -> Path:
    """Cut ``[start, end]`` out of ``src`` into a captioned short.

    With ``silence`` the window is cut to a scratch file first, silences are
    detected, and when at least :data:`MIN_SILENCE_REMOVED` seconds of dead air
    can go the clip is rebuilt with a trim+concat filtergraph (and the ASS file
    remapped onto the new timeline). Otherwise — or when nothing meaningful was
    found — it falls through to the normal single-pass render.
    """
    src = Path(src)
    out = Path(out)
    window = max(float(end) - float(start), 0.5)
    try:
        rate = float(speed)
    except (TypeError, ValueError):
        rate = 1.0
    if rate <= 0:
        rate = 1.0

    audio_ok = has_audio(src)
    afilter = audio_filter(rate, loud) if audio_ok else ""
    scratch: list[Path] = []

    try:
        if silence:
            cut_path = config.MEDIA_DIR / f".cut-{uuid.uuid4().hex[:10]}.mp4"
            scratch.append(cut_path)
            cut_window(src, float(start), float(end), cut_path)
            silences = detect_silences(cut_path)
            removed = sum(b - a for a, b in silences)
            ranges = invert_ranges(silences, 0.0, window) if silences else []
            if removed >= MIN_SILENCE_REMOVED and ranges:
                mapped_ass: Path | None = None
                if ass_path:
                    mapped_ass = config.SUBS_DIR / f"{out.stem}-cut.ass"
                    scratch.append(mapped_ass)
                    remap_ass_file(ass_path, ranges, mapped_ass, speed=rate)
                out_dur = sum(b - a for a, b in ranges) / rate
                graph = build_concat_filter(ranges, speed=rate, audio=audio_ok)
                graph += ";" + video_chain(
                    style, width, height, mapped_ass, out_dur, progress,
                    vin="ccv",
                )
                audio_label: str | None = None
                if audio_ok:
                    audio_label = "[cca]"
                    if loud:
                        graph += ";[cca]loudnorm=I=-16:TP=-1.5:LRA=11[a]"
                        audio_label = "[a]"
                args = ["-i", str(cut_path), "-filter_complex", graph, "-map", "[v]"]
                if audio_label:
                    args += ["-map", audio_label]
                args += [*_ENCODE, str(out)]
                _run(args)
                return out
    finally:
        for path in scratch:
            path.unlink(missing_ok=True)

    # Single pass (also the fallback when silence removal found nothing).
    graph = video_chain(
        style, width, height, ass_path, window / rate, progress, vin="0:v"
    )
    args = [
        "-ss", f"{float(start):.3f}",
        "-t", f"{window:.3f}",
        "-i", str(src),
        "-filter_complex", graph,
        "-map", "[v]", "-map", "0:a?",
    ]
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
