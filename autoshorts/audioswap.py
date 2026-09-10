"""T3 — Audio Swap: user music beds plus the offline mix filter graphs.

Tracks are uploaded through ``POST /api/audio`` (base64 in the JSON body, so
the stdlib server needs no multipart parser) and land in ``data/audio/`` with a
tiny JSON index next to them — media, not library state, so a state backup or
restore never drags audio files around.

The two mix modes are pure filter-graph builders, which keeps them unit testable
and identical on both servers:

``replace``
    the clip's own audio is dropped entirely; the track is trimmed to the exact
    clip length, loudness-matched with ``loudnorm`` and given short fades.
``duck``
    the original bed keeps :data:`config.DUCK_GAIN` (20%) of its level, the track
    rides on top, mixed with ``amix`` (``normalize=0`` so neither side is
    halved).

Nothing here raises for a missing or unreadable file — the render keeps the
original audio instead, because a short must always come out of ffmpeg.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

from . import config

INDEX_FILE = config.AUDIO_DIR / "tracks.json"
_SAFE_NAME = re.compile(r"[^A-Za-z0-9 ._-]")
_ALLOWED_EXT = tuple(f".{ext}" for ext in config.AUDIO_EXAMPLES)
LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"


# --------------------------------------------------------------------------
# Track library (data/audio/tracks.json)
# --------------------------------------------------------------------------
def _read_index() -> dict:
    try:
        data = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("tracks"), dict):
            return data
    except Exception:
        pass
    return {"tracks": {}}


def _write_index(data: dict) -> None:
    try:
        tmp = INDEX_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(INDEX_FILE)
    except OSError:
        pass


def list_tracks() -> list[dict]:
    """Newest-first list of uploaded audio tracks."""
    tracks = list(_read_index()["tracks"].values())
    tracks.sort(key=lambda track: float(track.get("added") or 0), reverse=True)
    return tracks


def get_track(track_id: str) -> dict | None:
    if not track_id:
        return None
    entry = _read_index()["tracks"].get(str(track_id))
    return dict(entry) if entry else None


def track_file(track_id: str) -> Path | None:
    """Absolute path of a track's audio file, or ``None`` when unusable."""
    entry = get_track(track_id)
    if not entry:
        return None
    path = config.AUDIO_DIR / str(entry.get("file"))
    return path if path.is_file() else None


def save_track(name, data: bytes) -> dict:
    """Validate + persist one uploaded track. Raises ``ValueError`` (→ 422)."""
    raw_name = _SAFE_NAME.sub("", str(name or "").strip())[:80]
    if not raw_name:
        raw_name = "track.mp3"
    ext = Path(raw_name).suffix.lower()
    if ext not in _ALLOWED_EXT:
        raw_name = f"{Path(raw_name).stem or 'track'}.mp3"
        ext = ".mp3"
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ValueError("data_b64 must decode to a non-empty audio file")
    if len(data) > config.MAX_AUDIO_BYTES:
        raise ValueError(
            "audio file is too large (max "
            f"{config.MAX_AUDIO_BYTES // 1_000_000} MB)"
        )

    track_id = uuid.uuid4().hex[:12]
    path = config.AUDIO_DIR / f"track-{track_id}{ext}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(data))

    from .ffmpeg import probe_duration  # local: avoids an import cycle

    try:
        duration = probe_duration(path)
    except Exception:
        duration = None
    if duration is None or duration <= 0.05:
        path.unlink(missing_ok=True)
        raise ValueError(
            "that file has no readable audio stream — use MP3, M4A, WAV or OGG"
        )

    entry = {
        "id": track_id,
        "name": Path(raw_name).name,
        "file": path.name,
        "bytes": len(data),
        "duration": round(float(duration), 3) if duration else None,
        "added": time.time(),
    }
    index = _read_index()
    index["tracks"][track_id] = entry
    _write_index(index)
    return entry


def delete_track(track_id: str) -> bool:
    index = _read_index()
    entry = index["tracks"].pop(str(track_id), None)
    if not entry:
        return False
    (config.AUDIO_DIR / str(entry.get("file"))).unlink(missing_ok=True)
    _write_index(index)
    return True


# --------------------------------------------------------------------------
# Filter graphs (the unit-tested part)
# --------------------------------------------------------------------------
def fade_chain(duration: float, fade: float) -> str:
    """``afade`` in/out around a clip of ``duration`` seconds ('' when too short)."""
    try:
        total = max(0.05, float(duration))
        seconds = max(0.0, float(fade))
    except (TypeError, ValueError):
        return ""
    seconds = min(seconds, total / 3.0)
    if seconds <= 0.001:
        return ""
    out_start = max(0.0, total - seconds)
    return (
        f",afade=t=in:st=0:d={seconds:.3f}"
        f",afade=t=out:st={out_start:.3f}:d={seconds:.3f}"
    )


def track_tail(duration: float, *, loud: bool = False, fade: float = 0.0,
               start_label: str = "1:a", out_label: str = "aswap") -> str:
    """Trim/loop the uploaded stream to ``duration`` (+ loudness match, fades)."""
    chain = (
        f"[{start_label}]atrim=0:{max(0.05, float(duration)):.3f},"
        "asetpts=PTS-STARTPTS"
    )
    if loud:
        chain += f",{LOUDNORM}"
    chain += fade_chain(duration, fade)
    return f"{chain}[{out_label}]"


def replace_graph(duration: float, *, track_index: int = 1, loud: bool = True,
                  fade: float = config.AUDIO_FADE,
                  out_label: str = "aout") -> str:
    """Track only — the clip's own audio is never mapped."""
    return track_tail(
        duration, loud=loud, fade=fade,
        start_label=f"{track_index}:a", out_label=out_label,
    )


def duck_graph(duration: float, *, src_label: str = "0:a", gain: float = config.DUCK_GAIN,
               speed: float = 1.0, loud: bool = False, track_index: int = 1,
               fade: float = config.AUDIO_FADE, out_label: str = "aout") -> str:
    """Original bed at ``gain`` with the track mixed on top."""
    try:
        rate = float(speed)
    except (TypeError, ValueError):
        rate = 1.0
    gain = max(0.0, min(1.0, float(gain)))
    source = f"[{src_label}]volume={gain:g}"
    if rate > 0 and abs(rate - 1.0) > 1e-9:
        source += f",atempo={rate:g}"
    track = track_tail(
        duration, loud=False, fade=0.0,
        start_label=f"{track_index}:a", out_label="aduck",
    )
    mix = (
        f"[avo][aduck]amix=inputs=2:duration=first:dropout_transition=0:normalize=0"
    )
    if loud:
        mix += f",{LOUDNORM}"
    mix += fade_chain(duration, fade)
    return f"{source}[avo];{track};{mix}[{out_label}]"


def mix_plan(mix: str, duration: float, *, source_label: str | None = "0:a",
             speed: float = 1.0, loud: bool = False,
             gain: float = config.DUCK_GAIN) -> dict:
    """What ``render_clip`` needs to wire a track into the ffmpeg command.

    ``{"graph", "label", "use_source_audio", "mode"}`` — with no source audio
    ``duck`` degrades to ``replace`` (the track is all there is), which is why
    the caller only has to check ``label``.
    """
    mode = str(mix or config.DEFAULT_AUDIO_MIX)
    if mode not in config.AUDIO_MIXES:
        mode = config.DEFAULT_AUDIO_MIX
    if mode == "duck" and source_label:
        graph = duck_graph(duration, src_label=source_label, speed=speed,
                           loud=loud, gain=gain)
        return {"graph": graph, "label": "[aout]", "use_source_audio": True,
                "mode": "duck"}
    graph = replace_graph(duration, loud=True)
    return {"graph": graph, "label": "[aout]", "use_source_audio": False,
            "mode": "replace"}


def extract_audio(src: Path | str, out: Path | str, *, bitrate: str = "192k",
                  timeout: int = 300) -> Path:
    """``src`` → MP3 (Audio Extract on a clip or an episode's media)."""
    from .ffmpeg import run_ffmpeg

    run_ffmpeg(
        ["-i", str(src), "-vn", "-map", "0:a:0", "-c:a", "libmp3lame",
         "-b:a", bitrate, str(out)],
        timeout=timeout,
    )
    return Path(out)
