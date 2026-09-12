"""YouTube ingestion via yt-dlp: playlist listing, transcripts, video download.

Everything here runs yt-dlp as a subprocess so the system ffmpeg flag and
timeouts stay explicit. Functions raise RuntimeError with a readable message
so the API layer can surface errors in the UI.
"""
from __future__ import annotations

import json
import re
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


class RateLimited(RuntimeError):
    """YouTube answered HTTP 429 (Too Many Requests).

    Subclasses :class:`RuntimeError` so every existing ``except RuntimeError``
    around :func:`run_ytdlp` keeps working. Callers that can do something
    cleverer than fail — rotate the player client, note a cooldown — catch
    this one instead.
    """


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


def run_ytdlp(
    args: list[str], timeout: int = 600, retries: int = 3
) -> subprocess.CompletedProcess:
    """Run yt-dlp with common flags; raise RuntimeError on failure.

    ``retries`` is yt-dlp's own retry count. The subtitle passes drop it to 1
    on purpose: Qyro paces and rotates those itself, and letting yt-dlp retry
    a 429 three more times *per pass* is what stretched one block into a
    ten-minute lock-out.

    A YouTube 429 raises :class:`RateLimited` (still a ``RuntimeError``) so a
    caller can tell "blocked" apart from "this video has no captions".
    """
    common = [
        "--no-warnings",
        "--no-playlist-reverse",
        "--socket-timeout", "15",
        "--retries", str(max(0, int(retries))),
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
        message = f"yt-dlp failed: {detail[:300]}"
        # Classify on the *whole* output, not just the last line: the 429
        # often sits further up, or in stdout when --no-warnings swallowed the
        # stderr copy. Judging by the last line alone missed real blocks and
        # reported them as "no captions available".
        blob = f"{proc.stderr or ''}\n{proc.stdout or ''}"
        if is_rate_limit_error(blob):
            raise RateLimited(message)
        raise RuntimeError(message)
    return proc


# YouTube says this as "HTTP Error 429: Too Many Requests", and sometimes only
# as a status code buried in a warning line. Match the code as a standalone
# number so a "429 seconds" duration can never be read as a block.
_HTTP_429 = re.compile(r"(?:^|[^0-9])429(?![0-9])")


def is_rate_limit_error(text: str) -> bool:
    """True when yt-dlp output reads like a YouTube 429 / rate-limit block."""
    low = str(text or "").lower()
    if "too many requests" in low or "rate limit" in low or "rate-limit" in low:
        return True
    return bool(_HTTP_429.search(low)) and ("http" in low or "error" in low)


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
# Playlist + single video
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


def is_playlist_url(url: str) -> bool:
    """True when ``url`` clearly points at a playlist (``list=`` / ``/playlist``)."""
    lowered = str(url or "").lower()
    return "list=" in lowered or "/playlist" in lowered


def fetch_video_meta(video_url: str) -> dict:
    """Metadata for a single video via ``yt-dlp -J --no-playlist``."""
    _pace()
    proc = run_ytdlp(["-J", "--no-playlist", video_url], timeout=180)
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError("Could not parse video data from yt-dlp.")
    vid = data.get("id")
    if not vid:
        raise RuntimeError("Video fetched but no id was returned.")
    return {
        "id": vid,
        "title": data.get("title") or vid,
        "duration": data.get("duration") or 0,
        "url": f"https://www.youtube.com/watch?v={vid}",
        "channel": data.get("channel") or data.get("uploader") or "",
    }


# --------------------------------------------------------------------------
# Transcripts
# --------------------------------------------------------------------------
_SUB_EXTENSIONS = (".json3", ".vtt", ".srt")

# A 429 is a symptom, not a diagnosis: the two things that actually clear it
# are a logged-in session and a current extractor, so every block message says
# so rather than just asking the user to sit and wait.
_RATE_LIMIT_HELP = (
    "To get past this for good: add a signed-in cookies.txt (Tools ▸ YouTube "
    "session, or save it as data/cookies.txt), or update yt-dlp with "
    "`pip install -U yt-dlp`."
)
# Seconds between passes on *different* player clients. The old code spent
# 30s + 60s retrying the same blocked client, which only fed the block.
_RATE_LIMIT_BACKOFF = 15.0
# yt-dlp's own retry count for a subtitle pass; Qyro does the pacing.
_SUBTITLE_RETRIES = 1


def _youtube_extractor_args(client: str | None = None) -> list[str]:
    """yt-dlp flags that make YouTube caption/media requests less likely to 429.

    A logged-in ``cookies.txt`` is the reliable fix for YouTube's bot check on
    anonymous subtitle downloads. ``client`` overrides the player client for a
    single call: walking :func:`player_client_chain` hits a *different*
    YouTube endpoint per client, which is what gets past a block that
    retrying the same client never does. ``None`` keeps the configured
    default (``AUTOSHORTS_PLAYER_CLIENT``).
    """
    args: list[str] = []
    cookies = config.COOKIES_FILE
    if cookies and cookies.exists():
        args += ["--cookies", str(cookies)]
    chosen = config.YTDLP_PLAYER_CLIENT if client is None else client
    if str(chosen or "").strip():
        args += ["--extractor-args", f"youtube:player_client={chosen}"]
    return args


def player_client_chain() -> list[str]:
    """Player clients to try, in order, the configured one first."""
    ordered: list[str] = []
    for group in (config.YTDLP_PLAYER_CLIENT, *config.PLAYER_CLIENT_CHAIN):
        for name in str(group or "").split(","):
            name = name.strip()
            if name and name not in ordered:
                ordered.append(name)
    return ordered or [""]


def _rate_limit_state() -> dict:
    """The remembered block, or ``{}`` when there is none / it is unreadable."""
    try:
        raw = json.loads(config.RATE_LIMIT_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _rate_limit_remaining() -> float | None:
    """Seconds until a previous 429 block expires, or ``None`` when clear.

    A YouTube block outlives a single job; without this note a user who hits
    Retry a minute later would hammer YouTube again and restart the timer.
    """
    try:
        until = float(_rate_limit_state().get("until") or 0.0)
    except (TypeError, ValueError):
        return None
    left = until - time.time()
    return left if left > 0 else None


def _rate_limit_hits() -> int:
    """Consecutive blocks still standing — drives the cooldown ladder."""
    if _rate_limit_remaining() is None:
        return 0
    try:
        return max(0, int(_rate_limit_state().get("hits") or 0))
    except (TypeError, ValueError):
        return 0


def _mark_rate_limited(seconds: float | None = None) -> float:
    """Remember that YouTube told us to back off (best-effort, never fatal).

    A block recorded while another is still standing steps up the ladder, so
    someone who retries the instant a block lifts waits a little longer next
    time instead of re-triggering it every ten minutes.
    """
    hits = _rate_limit_hits() + 1
    if seconds is None:
        ladder = config.RATE_LIMIT_COOLDOWNS
        seconds = ladder[min(hits, len(ladder)) - 1]
    seconds = max(0.0, min(float(seconds), float(config.RATE_LIMIT_MAX_COOLDOWN)))
    try:
        config.RATE_LIMIT_STATE.write_text(
            json.dumps(
                {
                    "until": time.time() + seconds,
                    "at": time.time(),
                    "hits": hits,
                    "seconds": seconds,
                },
            ),
            encoding="utf-8",
        )
    except Exception:
        pass
    return seconds


def _clear_rate_limit() -> None:
    """Forget the block: a request just succeeded, so YouTube is talking again."""
    try:
        config.RATE_LIMIT_STATE.unlink(missing_ok=True)
    except OSError:
        pass


def rate_limit_status() -> dict:
    """UI-safe view of any block standing (no paths, no file internals)."""
    remaining = _rate_limit_remaining()
    return {
        "blocked": remaining is not None,
        "minutes": _minutes(remaining) if remaining is not None else 0,
    }


def _minutes(seconds: float) -> int:
    return max(1, int(round(float(seconds) / 60.0)))


def _cooldown_message(remaining: float) -> str:
    return (
        "YouTube is still rate-limiting subtitle downloads (HTTP 429). "
        f"Retry in about {_minutes(remaining)} minute(s) — transcripts that "
        "were already fetched are cached, so nothing finished is lost. "
        + _RATE_LIMIT_HELP
    )


def _blocked_message(wait: float) -> str:
    return (
        "YouTube is rate-limiting subtitle downloads (HTTP 429) on every "
        f"player client we tried. Wait about {_minutes(wait)} minutes and "
        "retry — transcripts that were already fetched are cached, so nothing "
        "finished is lost. " + _RATE_LIMIT_HELP
    )


def _subtitle_files(video_id: str) -> list[Path]:
    files = [
        path
        for extension in _SUB_EXTENSIONS
        for path in config.SUBS_DIR.glob(f"{video_id}*{extension}")
    ]
    return sorted(files, key=lambda path: 0 if path.suffix == ".json3" else 1)


def _discard_subtitle_files(video_id: str) -> None:
    """Drop the raw caption files once the normalized cache holds them."""
    for path in _subtitle_files(video_id):
        path.unlink(missing_ok=True)


def _write_cache(cache: Path, segments: list[Segment]) -> None:
    cache.write_text(
        json.dumps(
            [
                {
                    "start": round(segment.start, 3),
                    "end": round(segment.end, 3),
                    "text": segment.text,
                    **({"words": [
                        [str(w), round(float(ws), 3), round(float(we), 3)]
                        for (w, ws, we) in (segment.words or [])
                    ]} if getattr(segment, "words", None) else {}),
                }
                for segment in segments
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _segments_from_disk(
    video_id: str, cache: Path
) -> tuple[list[Segment], str]:
    """Normalize a caption file that is already sitting in ``data/subs``.

    A run interrupted *after* yt-dlp wrote ``<id>.en.json3`` but before it was
    normalized used to throw that transcript away and go straight back to
    YouTube — the worst possible move during a block, and the reason
    "transcripts are cached, so no progress is lost" was not actually true.
    """
    for path in _subtitle_files(video_id):
        try:
            segments = load_transcript_file(path)
        except Exception:
            segments = []
        if not segments:
            continue
        try:
            _write_cache(cache, segments)
        except OSError:
            return [], ""
        source = path.name
        _discard_subtitle_files(video_id)
        return segments, source
    return [], ""


def load_cached_transcript(video_id: str) -> list[Segment]:
    """Load a normalized transcript cache without making a network request."""
    return _load_cached_segments(config.SUBS_DIR / f"{video_id}.segments.json")


def get_transcript(
    video_id: str, video_url: str, language: str = "auto"
) -> tuple[list[Segment], str]:
    """Fetch and cache one caption track, with paced HTTP 429 retries.

    ``language`` (v0.6.0) picks which subtitle track to ask for first — ``hi``
    prefers Hindi, ``hinglish`` Hindi-then-English, ``auto`` keeps the v0.5.0
    English-first chain. A cached transcript always wins, so switching language
    on an already-fetched episode needs no new request.

    The order matters once YouTube starts blocking anonymous caption
    requests, and it is the whole fix for the "still rate-limiting" loop:

    1. the normalized cache — no request at all;
    2. a caption file already on disk from an interrupted run — still no
       request, and the reason a block no longer throws away real work;
    3. an active cooldown — refuse without touching YouTube;
    4. one pass per player client, spaced apart, because a block on ``web``
       usually leaves another client working while retrying ``web`` never will.
    """
    cache = config.SUBS_DIR / f"{video_id}.segments.json"

    segments = _load_cached_segments(cache)
    if segments:
        return segments, "cached"

    segments, source = _segments_from_disk(video_id, cache)
    if segments:
        return segments, source or "recovered"

    # A previous run may have been blocked minutes ago; honour that window
    # instead of hammering YouTube again the instant the user hits Retry.
    # Checked *after* the disk recovery: a transcript we already hold must
    # never be gated behind a cooldown.
    remaining = _rate_limit_remaining()
    if remaining is not None:
        raise TranscriptUnavailable(_cooldown_message(remaining))

    clients = player_client_chain()
    blocked = False
    for index, client in enumerate(clients):
        segments, source, hit, _error = _subtitle_pass(
            video_id, video_url, language, client, cache
        )
        if segments:
            _clear_rate_limit()
            return segments, source
        if not hit:
            # A complete pass without a 429 tried every language; another
            # client would only repeat the same no-caption result.
            break
        blocked = True
        if index + 1 < len(clients):
            time.sleep(_RATE_LIMIT_BACKOFF)

    if blocked:
        raise TranscriptUnavailable(_blocked_message(_mark_rate_limited()))
    raise TranscriptUnavailable("No captions available for this episode")


def _subtitle_pass(
    video_id: str,
    video_url: str,
    language: str,
    client: str,
    cache: Path,
) -> tuple[list[Segment], str, bool, str]:
    """One caption fetch against a single player client.

    Returns ``(segments, source_name, rate_limited, error)``. Languages are
    walked one per request on purpose — asking yt-dlp for several at once is
    what triggers YouTube's HTTP 429s — and a 429 stops the walk at once so
    the caller can move to the next client instead of spending more requests
    on one that is already blocked.
    """
    prefix = config.SUBS_DIR / video_id
    error = ""

    for sub_lang in sub_lang_chain(language):
        _pace()
        try:
            run_ytdlp(
                [
                    "--skip-download",
                    "--write-subs", "--write-auto-subs",
                    "--sub-langs", sub_lang,
                    "--sub-format", "json3/vtt/srt/best",
                    "--sleep-subtitles", "2",
                    "--sleep-requests", "1.5",
                    "--no-overwrites",
                    *_youtube_extractor_args(client),
                    "-o", str(prefix),
                    video_url,
                ],
                timeout=300,
                retries=_SUBTITLE_RETRIES,
            )
        except RuntimeError as exc:      # RateLimited included
            error = str(exc)
            if is_rate_limit_error(error):
                return [], "", True, error
            # A missing language or unavailable track is not fatal. Still
            # inspect disk first because yt-dlp can leave a usable subtitle
            # file even when a later metadata request fails.

        files = _subtitle_files(video_id)
        if not files:
            continue

        source_name = files[0].name
        try:
            segments = load_transcript_file(files[0])
        except Exception:
            segments = []
        if not segments:
            # Do not let an unparseable no-overwrite file poison later
            # language attempts.
            for path in files:
                path.unlink(missing_ok=True)
            continue

        try:
            _write_cache(cache, segments)
        except OSError:
            return [], "", False, error
        _discard_subtitle_files(video_id)
        return segments, source_name, False, ""

    return [], "", False, error


def _load_cached_segments(path: Path) -> list[Segment]:
    """Read a normalized transcript cache, keeping any per-word timings.

    Caches written before v0.6.0 simply have no ``words`` key, which is why
    the lookup is optional rather than a hard requirement.
    """
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        out: list[Segment] = []
        for row in raw:
            words = []
            for item in row.get("words") or []:
                try:
                    words.append((str(item[0]), float(item[1]), float(item[2])))
                except (TypeError, ValueError, IndexError):
                    words = []
                    break
            out.append(
                Segment(row["start"], row["end"], row["text"], words)
            )
        return out
    except Exception:
        return []


def sub_lang_chain(language: str = "auto") -> list[str]:
    """Subtitle language order for a caption language choice.

    One language per request on purpose — asking yt-dlp for several at once is
    what triggers YouTube's HTTP 429s.
    """
    key = str(language or "auto").strip().lower()
    return list(config.LANGUAGE_SUBS.get(key) or config.SUB_LANG_CHAIN)


# --------------------------------------------------------------------------
# Media
# --------------------------------------------------------------------------
def download_video(video_id: str, video_url: str) -> Path:
    """Download a video at <=720p mp4 (cached).

    The cache honours a "already downloaded" file only if it is real footage:
    a synthetic demo test card that once landed at this path must never be
    handed to a YouTube render (that is how a colour-bar placeholder with a
    sine beep ended up shipped as someone's short).
    """
    from . import ffmpeg as _ffmpeg

    _pace()
    dest = config.MEDIA_DIR / f"{video_id}.mp4"
    if dest.exists() and dest.stat().st_size > 10_000:
        if not _ffmpeg.is_placeholder_media(dest):
            return dest
        # a placeholder in the real-media slot: drop it and download properly
        for stale in config.MEDIA_DIR.glob(f"{video_id}.mp4*"):
            if stale.suffix == ".mp4" or stale.name.endswith(".qyro-demo.json"):
                stale.unlink(missing_ok=True)
    tmp_out = config.MEDIA_DIR / f"{video_id}.%(ext)s"
    request = [
        "-f", "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]/b",
        "--merge-output-format", "mp4",
        "--no-part",
    ]
    # Media comes off a different CDN than captions, so a subtitle block does
    # not mean downloads are blocked — always try. Only when YouTube really
    # answers 429 do we fall back to another player client, and then note the
    # block so the next job does not walk into it blind.
    clients = player_client_chain()[:2]
    for index, client in enumerate(clients):
        _pace()
        try:
            run_ytdlp(
                [
                    *request,
                    *_youtube_extractor_args(client),
                    "-o", str(tmp_out),
                    video_url,
                ],
                timeout=1800,
            )
            break
        except RuntimeError as exc:
            if not is_rate_limit_error(str(exc)):
                raise
            if index + 1 >= len(clients):
                _mark_rate_limited()
                raise RuntimeError(
                    "YouTube is rate-limiting this download (HTTP 429). "
                    + _RATE_LIMIT_HELP
                ) from exc
            time.sleep(_RATE_LIMIT_BACKOFF)
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
