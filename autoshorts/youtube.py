"""YouTube ingestion via yt-dlp: playlist listing, transcripts, video download.

Everything here runs yt-dlp as a subprocess so the system ffmpeg flag and
timeouts stay explicit. Functions raise RuntimeError with a readable message
so the API layer can surface errors in the UI.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from . import config
from .transcripts import Segment, load_transcript_file


class TranscriptUnavailable(RuntimeError):
    pass


# Serialize and space every yt-dlp invocation made by this module. Holding the
# lock while sleeping prevents another worker from starting inside the gap.
_rate_lock = threading.Lock()
_last_call = 0.0
_MIN_INTERVAL = 4.0


def _pace() -> None:
    """Ensure all yt-dlp calls begin at least ``_MIN_INTERVAL`` apart."""
    global _last_call
    with _rate_lock:
        wait = _MIN_INTERVAL - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


def _ytdlp() -> str | None:
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
        detail = next(
            (line for line in reversed(err) if line.strip() and "WARNING" not in line),
            "unknown error",
        )
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
    _pace()
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
    for entry in entries:
        vid = entry.get("id")
        if not vid:
            continue
        out.append(
            {
                "id": vid,
                "title": entry.get("title") or vid,
                "duration": entry.get("duration") or 0,
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
_SUB_EXTENSIONS = (".json3", ".vtt", ".srt")
_RATE_LIMIT_MESSAGE = (
    "YouTube is rate-limiting subtitle downloads (HTTP 429). Wait 10-15 "
    "minutes and retry — transcripts are cached so no progress is lost."
)


def _subtitle_files(video_id: str) -> list[Path]:
    files = [
        path
        for extension in _SUB_EXTENSIONS
        for path in config.SUBS_DIR.glob(f"{video_id}*{extension}")
    ]
    return sorted(files, key=lambda path: 0 if path.suffix == ".json3" else 1)


def load_cached_transcript(video_id: str) -> list[Segment]:
    """Load a normalized transcript cache without making a network request."""
    return _load_cached_segments(config.SUBS_DIR / f"{video_id}.segments.json")


def get_transcript(video_id: str, video_url: str) -> tuple[list[Segment], str]:
    """Fetch and cache one caption track, with paced HTTP 429 retries."""
    cache = config.SUBS_DIR / f"{video_id}.segments.json"
    segments = _load_cached_segments(cache)
    if segments:
        return segments, "cached"

    prefix = config.SUBS_DIR / video_id
    rate_limited_passes = 0

    for attempt in range(3):
        hit_rate_limit = False
        for language in config.SUB_LANG_CHAIN:
            _pace()
            try:
                run_ytdlp(
                    [
                        "--skip-download",
                        "--write-subs", "--write-auto-subs",
                        "--sub-langs", language,
                        "--sub-format", "json3/vtt/srt/best",
                        "--sleep-subtitles", "2",
                        "--sleep-requests", "1.5",
                        "--no-overwrites",
                        "-o", str(prefix),
                        video_url,
                    ],
                    timeout=300,
                )
            except RuntimeError as exc:
                error = str(exc)
                if "429" in error or "Too Many Requests" in error:
                    hit_rate_limit = True
                    rate_limited_passes += 1
                    break
                # A missing language or unavailable track is not fatal. Still
                # inspect disk first because yt-dlp can leave a usable subtitle
                # file even when a later metadata request fails.

            files = _subtitle_files(video_id)
            if not files:
                continue

            source_name = files[0].name
            segments = load_transcript_file(files[0])
            if not segments:
                # Do not let an unparseable no-overwrite file poison later
                # language attempts.
                for path in files:
                    path.unlink(missing_ok=True)
                continue

            cache.write_text(
                json.dumps(
                    [
                        {
                            "start": round(segment.start, 3),
                            "end": round(segment.end, 3),
                            "text": segment.text,
                        }
                        for segment in segments
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            for path in _subtitle_files(video_id):
                path.unlink(missing_ok=True)
            return segments, source_name

        if hit_rate_limit:
            if attempt < 2:
                time.sleep(20 * (attempt + 1))
            continue
        # A complete pass without a 429 tried every language; another pass
        # would only repeat the same no-caption result.
        break

    if rate_limited_passes == 3:
        raise TranscriptUnavailable(_RATE_LIMIT_MESSAGE)
    raise TranscriptUnavailable("No captions available for this episode")


def _load_cached_segments(path: Path) -> list[Segment]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return [Segment(row["start"], row["end"], row["text"]) for row in raw]
    except Exception:
        return []


# --------------------------------------------------------------------------
# Media
# --------------------------------------------------------------------------
def download_video(video_id: str, video_url: str) -> Path:
    """Download a video at <=720p mp4 (cached)."""
    _pace()
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
            path for path in config.MEDIA_DIR.glob(f"{video_id}.*")
            if path.suffix.lower() in (".mp4", ".mkv", ".webm")
        ]
        if not alt:
            raise RuntimeError("Download finished but no video file was produced.")
        alt[0].replace(dest)
    return dest
