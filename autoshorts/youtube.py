"""YouTube ingestion via yt-dlp: playlist listing, transcripts, video download.

Everything here runs yt-dlp as a subprocess so the system ffmpeg flag and
timeouts stay explicit. Functions raise RuntimeError with a readable message
so the API layer can surface errors in the UI.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import urllib.request
from pathlib import Path

from . import config
from .transcripts import Segment, load_transcript_file


class TranscriptUnavailable(RuntimeError):
    pass


def _ytdlp() -> str:
    """Find the yt-dlp executable (module or binary)."""
    if shutil.which("yt-dlp"):
        return "yt-dlp"
    # fall back to python -m yt_dlp
    try:
        import yt_dlp  # noqa: F401

        return None  # signal: use python -m
    except ImportError:
        raise RuntimeError("yt-dlp is not installed. Run: pip install yt-dlp")


def run_ytdlp(args: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    """Run yt-dlp with common flags; raise RuntimeError on failure."""
    common = [
        "--no-warnings",
        "--no-playlist-reverse",
        "--socket-timeout", "15",
        "--retries", "3",
        "--ffmpeg-location", config.FFMPEG_BIN,
    ]
    exe = _ytdlp()
    cmd = ([sys_python(), "-m", "yt_dlp"] if exe is None else [exe]) + common + args
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("yt-dlp timed out — try again or pick fewer episodes.")
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        detail = next((l for l in reversed(err) if l.strip() and "WARNING" not in l), "unknown error")
        raise RuntimeError(f"yt-dlp failed: {detail[:300]}")
    return proc


def sys_python() -> str:
    import sys

    return sys.executable or "python3"


def check_reachable(timeout: float = 6.0) -> bool:
    """Can this machine reach YouTube? (False in restricted sandboxes.)"""
    try:
        req = urllib.request.Request(
            "https://www.youtube.com/robots.txt",
            method="HEAD",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# Playlist
# --------------------------------------------------------------------------
def list_playlist(playlist_url: str, limit: int = config.EPISODE_PAGE_SIZE) -> list[dict]:
    """Flat-list a playlist (id/title/duration) without downloading media."""
    proc = run_ytdlp(
        [
            "-J", "--flat-playlist",
            "--playlist-items", f"1:{limit}",
            playlist_url,
        ],
        timeout=180,
    )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError("Could not parse playlist data from yt-dlp.")
    entries = data.get("entries") or []
    out = []
    for e in entries:
        vid = e.get("id")
        if not vid:
            continue
        out.append(
            {
                "id": vid,
                "title": e.get("title") or vid,
                "duration": e.get("duration") or 0,
                "url": f"https://www.youtube.com/watch?v={vid}",
                "channel": data.get("channel") or data.get("uploader") or "",
            }
        )
    if not out:
        raise RuntimeError("Playlist fetched but no videos were found.")
    return out


# --------------------------------------------------------------------------
# Transcripts
# --------------------------------------------------------------------------
_SUB_GLOB = ("*.json3", "*.vtt", "*.srt")


def get_transcript(video_id: str, video_url: str) -> tuple[list[Segment], str]:
    """Fetch (and cache) a transcript for one video.

    Returns (segments, source_label). Raises TranscriptUnavailable when
    YouTube has no captions for the video.
    """
    cache = config.SUBS_DIR / f"{video_id}.segments.json"
    if cache.exists():
        segs = _load_cached_segments(cache)
        if segs:
            return segs, "cached"

    # download subs next to nothing else in a temp prefix
    prefix = config.SUBS_DIR / video_id
    try:
        run_ytdlp(
            [
                "--skip-download",
                "--write-subs", "--write-auto-subs",
                "--sub-langs", config.SUB_LANGS,
                "--sub-format", "json3/vtt/srt/best",
                "--no-overwrites",
                "-o", str(prefix),
                video_url,
            ],
            timeout=300,
        )
    except RuntimeError as e:
        raise TranscriptUnavailable(str(e))

    files = sorted(
        (p for pat in _SUB_GLOB for p in Path(config.SUBS_DIR).glob(f"{video_id}*{pat}")),
        key=lambda p: 0 if p.suffix == ".json3" else 1,
    )
    if not files:
        raise TranscriptUnavailable(
            "No captions available for this episode "
            "(yt-dlp found no subtitle tracks)."
        )
    segments = load_transcript_file(files[0])
    if not segments:
        raise TranscriptUnavailable("Caption track downloaded but could not be parsed.")

    # normalize + cache, clean raw files
    cache.write_text(
        json.dumps(
            [
                {"start": round(s.start, 3), "end": round(s.end, 3), "text": s.text}
                for s in segments
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    for p in Path(config.SUBS_DIR).glob(f"{video_id}*"):
        if p.suffix in (".json3", ".vtt", ".srt"):
            p.unlink(missing_ok=True)
    return segments, files[0].name


def _load_cached_segments(path: Path) -> list[Segment]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return [Segment(r["start"], r["end"], r["text"]) for r in raw]
    except Exception:
        return []


# --------------------------------------------------------------------------
# Media
# --------------------------------------------------------------------------
def download_video(video_id: str, video_url: str) -> Path:
    """Download a video at <=720p mp4 (cached)."""
    dest = config.MEDIA_DIR / f"{video_id}.mp4"
    if dest.exists() and dest.stat().st_size > 10_000:
        return dest
    tmp_out = config.MEDIA_DIR / f"{video_id}.%(ext)s"
    run_ytdlp(
        [
            "-f", "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]/b",
            "--merge-output-format", "mp4",
            "--no-part",
            "-o", str(tmp_out),
            video_url,
        ],
        timeout=1800,
    )
    if not dest.exists():
        # maybe merged under a different extension
        alt = [
            p for p in config.MEDIA_DIR.glob(f"{video_id}.*")
            if p.suffix.lower() in (".mp4", ".mkv", ".webm")
        ]
        if not alt:
            raise RuntimeError("Download finished but no video file was produced.")
        alt[0].replace(dest)
    return dest
