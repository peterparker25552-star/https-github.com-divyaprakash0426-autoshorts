"""ffmpeg wrappers: render 9:16 captioned shorts, thumbnails, demo media."""
from __future__ import annotations

import subprocess
from pathlib import Path

from . import config
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


# --------------------------------------------------------------------------
# ASS caption generation
# --------------------------------------------------------------------------
def _ass_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def make_ass(
    segments: list[Segment],
    clip_start: float,
    clip_end: float,
    out_path: Path,
    play_w: int,
    play_h: int,
    words_per_caption: int = config.CAPTION_WORDS_PER_LINE,
) -> Path:
    """Build an ASS subtitle file for [clip_start, clip_end], 0-based times."""
    font_size = max(36, int(play_h * 0.045))
    margin_v = int(play_h * 0.30)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {play_w}
PlayResY: {play_h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{config.CAPTION_FONT},{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,{max(2, font_size // 18)},1,2,60,60,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []

    # Split caption text into small word chunks spread over line duration
    chunks: list[tuple[float, float, str]] = []
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
                    " ".join(part).upper(),
                )
            )

    # Caption timing relative to clip start; clamp to the clip window
    for c_start, c_end, text in chunks:
        if c_end <= clip_start or c_start >= clip_end:
            continue
        rel_start = max(0.0, c_start - clip_start)
        rel_end = min(clip_end - clip_start, c_end - clip_start)
        if rel_end - rel_start < 0.15:
            rel_end = rel_start + 0.4
        text = text.replace("{", "(").replace("}", ")")
        events.append(
            f"Dialogue: 0,{_ass_time(rel_start)},{_ass_time(rel_end)},Cap,,0,0,0,,{text}"
        )

    out_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return out_path


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
def render_clip(
    src: Path,
    start: float,
    end: float,
    out: Path,
    ass_path: Path,
    style: str = config.DEFAULT_STYLE,
    height: int = 1280,
) -> Path:
    """Cut [start, end] from src into a captioned 9:16 mp4."""
    width = round(height * 9 / 16 / 2) * 2  # even
    if style == "crop":
        vf = (
            f"crop='min(iw,ih*9/16)':'min(ih,iw*16/9)',"
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},"
            f"ass={_escape_filter(str(ass_path))}"
        )
    else:  # blur background + centered foreground
        vf = (
            f"[0:v]split=2[bg][fg];"
            f"[bg]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},gblur=sigma=24[bgb];"
            f"[fg]scale={width}:-2[fgs];"
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2[ov];"
            f"[ov]ass={_escape_filter(str(ass_path))}[v]"
        )

    args = ["-ss", f"{start:.3f}", "-t", f"{max(end - start, 1.0):.3f}", "-i", str(src)]
    if style == "crop":
        args += ["-vf", vf]
    else:
        args += ["-filter_complex", vf, "-map", "[v]", "-map", "0:a?"]
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
