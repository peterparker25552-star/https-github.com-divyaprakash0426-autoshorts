"""YouTube ingestion via yt-dlp: playlist listing, transcripts, video download.

Everything here runs yt-dlp as a subprocess so the system ffmpeg flag and
timeouts stay explicit. Functions raise RuntimeError with a readable message
so the API layer can surface errors in the UI.

A caption fetch that comes back empty is *diagnosed* here, not guessed at: see
the ``KIND_*`` taxonomy below. "No captions available" is a claim about the
video, and it is only made once yt-dlp's own metadata for the episode agrees.
"""
from __future__ import annotations

import datetime
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

    Caption calls keep yt-dlp's warnings on purpose. YouTube's refusals are
    *warnings* — "Some web client subtitles require a PO Token … they will be
    discarded", "Skipping unsupported client", "Sign in to confirm you're not a
    bot" — and ``--no-warnings`` deleted the only evidence of them, which is how
    a refused caption track came to be reported as "this episode has no
    captions". Everything else stays quiet so ``-J`` output remains clean JSON.
    """
    common = [
        "--no-playlist-reverse",
        "--socket-timeout", "15",
        "--retries", str(max(0, int(retries))),
        "--ffmpeg-location", config.FFMPEG_BIN,
    ]
    if not is_caption_call(args):
        common.insert(0, "--no-warnings")
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


# --------------------------------------------------------------------------
# Why a caption request failed — the taxonomy behind every message in the app
# --------------------------------------------------------------------------
# Until v0.6.9 there were two outcomes: an HTTP 429, and "No captions
# available for this episode". Everything else — a bot check, a TLS reset, an
# extractor too old to parse YouTube, a private video, and above all YouTube
# *refusing* an anonymous client caption tracks the episode plainly has (a
# "PO Token" skip, which yt-dlp reports as a warning and exits 0 for) — was
# folded into that second answer, which is a claim about the video. It was
# wrong most of the time, it stopped the player-client rotation dead after one
# client, and it sent users looking for captions that existed instead of
# installing the session that would fetch them.
KIND_OK = "ok"
KIND_NO_CAPTIONS = "no_captions"    # asked cleanly; the episode has no such track
KIND_PO_TOKEN = "po_token"          # captions exist, withheld without a token
KIND_RATE_LIMIT = "rate_limit"      # HTTP 429
KIND_BOT_CHECK = "bot_check"        # "Sign in to confirm you're not a bot"
KIND_VIDEO = "video_unavailable"    # private / removed / age- or region-locked
KIND_NETWORK = "network"            # DNS, TLS, timeout — this machine's problem
KIND_EXTRACTOR = "extractor"        # yt-dlp could not parse YouTube: update it
KIND_UNKNOWN = "unknown"

# Which of two failures is worth reporting: the more specific cause wins, and
# "the episode has no captions" is the weakest claim of all, because it is the
# one that needs proof.
_KIND_PRIORITY = (
    KIND_RATE_LIMIT, KIND_VIDEO, KIND_BOT_CHECK, KIND_PO_TOKEN,
    KIND_EXTRACTOR, KIND_NETWORK, KIND_UNKNOWN, KIND_NO_CAPTIONS, KIND_OK,
)

# A caption call is one that writes or lists subtitle files: those keep
# yt-dlp's warnings, because a refusal only ever shows up as one.
_CAPTION_FLAGS = ("--write-subs", "--write-auto-subs", "--list-subs",
                  "--all-subs", "--write-automatic-subs")


def is_caption_call(args) -> bool:
    """True when these yt-dlp ``args`` are asking for subtitle files."""
    return any(str(flag) in _CAPTION_FLAGS for flag in (args or []))


_VIDEO_MARKERS = (
    "video unavailable", "this video is unavailable", "private video",
    "this video is private", "has been removed", "no longer available",
    "not available in your country", "not available in your region",
    "not made this video available", "available in your country",
    "members-only", "join this channel", "age-restricted",
    "sign in to confirm your age", "this video is not available",
    "is a live stream", "is currently live", "scheduled for",
)
_BOT_CHECK_MARKERS = (
    "sign in to confirm", "not a bot", "please sign in", "sign in to youtube",
    "login required", "log in to confirm", "authentication required",
    "cookies are required", "http error 403", "forbidden",
)
_PO_TOKEN_MARKERS = (
    "po token", "po_token", "potoken", "will be discarded",
    "missing subtitles languages", "subtitles for these languages are missing",
    "there are missing subtitles",
)
_NETWORK_MARKERS = (
    "tls/ssl", "ssl:", "sslerror", "eof occurred", "connection",
    "timed out", "timeout", "unreachable", "name resolution",
    "temporary failure", "urlopen error", "remote end closed", "reset by peer",
    "unable to download api page", "http error 5", "bad gateway",
    "service unavailable", "no route to host",
)
_EXTRACTOR_MARKERS = (
    "unable to extract", "failed to extract", "cannot extract", "nsig",
    "n-sig", "signature", "unsupported url", "report this issue",
    "unsupported client", "skipping unsupported client", "javascript runtime",
    "unable to download video metadata", "unable to download webpage",
)
_JS_RUNTIME_MARKER = "javascript runtime"


def worse_kind(*kinds: str) -> str:
    """The most informative of several failure kinds."""
    present = [kind for kind in kinds if kind and kind != KIND_OK]
    if not present:
        return KIND_OK
    return min(present, key=lambda kind: _KIND_PRIORITY.index(kind)
               if kind in _KIND_PRIORITY else len(_KIND_PRIORITY))


def classify_failure(text: str) -> str:
    """Which kind of failure a yt-dlp *error* describes.

    Order matters: a 429 is the one YouTube says in numbers, a video-level
    refusal ("private", "not available in your country") must not be read as a
    bot check just because it also says "sign in", and a TLS reset must not be
    read as an extractor bug just because yt-dlp asks to be updated at the end
    of every error it prints.
    """
    low = str(text or "").lower()
    if not low.strip():
        return KIND_UNKNOWN
    if is_rate_limit_error(low):
        return KIND_RATE_LIMIT
    for markers, kind in (
        (_VIDEO_MARKERS, KIND_VIDEO),
        (_PO_TOKEN_MARKERS, KIND_PO_TOKEN),
        (_BOT_CHECK_MARKERS, KIND_BOT_CHECK),
        (_NETWORK_MARKERS, KIND_NETWORK),
        (_EXTRACTOR_MARKERS, KIND_EXTRACTOR),
    ):
        if any(marker in low for marker in markers):
            return kind
    return KIND_UNKNOWN


def classify_caption_output(text: str) -> str:
    """Read a caption pass that *exited 0* — the silent failures live here.

    This is the case v0.6.8 had no answer for. YouTube refuses an anonymous
    ``web`` client the caption tracks it plainly has, yt-dlp discards them with
    a warning, prints ``[info] There are no subtitles for the requested
    languages`` and exits **0**: a clean run with nothing on disk. Only the
    warning says what really happened.
    """
    low = str(text or "").lower()
    if not low.strip():
        return KIND_NO_CAPTIONS
    if any(marker in low for marker in _PO_TOKEN_MARKERS):
        return KIND_PO_TOKEN
    if is_rate_limit_error(low):
        return KIND_RATE_LIMIT
    if any(marker in low for marker in _BOT_CHECK_MARKERS):
        return KIND_BOT_CHECK
    return KIND_NO_CAPTIONS


def detail_line(text: str, limit: int = 220) -> str:
    """The one line of yt-dlp output worth showing a user, trimmed."""
    best = ""
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # The most specific line wins: an ERROR beats a WARNING beats anything.
        if stripped.startswith("ERROR"):
            best = stripped
            break
        if stripped.startswith("WARNING") and not best.startswith("ERROR"):
            best = stripped
        elif not best:
            best = stripped
    best = best.replace("yt-dlp failed:", "").strip()
    # yt-dlp appends two boilerplate tails to most of its errors: a
    # "(caused by …)" repeat of the same failure and a "please report this
    # issue" link. Both are noise next to the one line that actually
    # diagnoses anything, and both eat the space a message has.
    best = re.split(r";?\s*please report this issue", best)[0]
    best = re.sub(r"\s*\(caused by .*\)\s*$", "", best)
    best = re.sub(r"\s{2,}", " ", best).strip(" .;")
    if len(best) > limit:
        best = best[: limit - 1].rstrip() + "…"
    return best


def sys_python() -> str:
    import sys

    return sys.executable or "python3"


def ytdlp_version() -> str:
    """The installed yt-dlp's version string (``""`` when it is not there)."""
    try:
        from yt_dlp.version import __version__  # type: ignore

        return str(__version__ or "")
    except Exception:
        return ""


def ytdlp_age_days(version: str | None = None) -> int | None:
    """How many days old the installed yt-dlp is, or ``None`` if unparseable.

    yt-dlp versions are calendar dates (``2026.08.19``), which makes "how old
    is this extractor" a real question — and the first one to ask when YouTube
    stops handing over captions, because a stale extractor is the cause often
    enough that every caption failure message names the age.
    """
    text = str(version if version is not None else ytdlp_version()).strip()
    parts = text.split(".")
    if len(parts) < 3:
        return None
    try:
        year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
        released = datetime.date(year, month, day)
    except (TypeError, ValueError):
        return None
    age = (datetime.date.today() - released).days
    return age if age >= 0 else None


def ytdlp_status() -> dict:
    """UI-safe view of the installed extractor: version, age, and staleness."""
    version = ytdlp_version()
    age = ytdlp_age_days(version)
    return {
        "version": version or "missing",
        "age_days": age,
        "stale_days": int(config.YTDLP_STALE_DAYS),
        "stale": bool(age is not None and age > int(config.YTDLP_STALE_DAYS)),
        "present": bool(version),
    }


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
    """Player clients to try, in order, the configured one first.

    An empty entry means "pass no ``--extractor-args`` at all" — i.e. let the
    *installed* yt-dlp choose, which is a list maintained against a YouTube
    that changes weekly. It leads the chain unless the user pinned a client
    with ``AUTOSHORTS_PLAYER_CLIENT``, in which case that pin is respected as
    the first pass and yt-dlp's own default becomes one of the fallbacks.
    """
    ordered: list[str] = []
    pinned = str(config.YTDLP_PLAYER_CLIENT or "").strip()
    groups = (pinned,) if pinned else ()
    for group in (*groups, *config.PLAYER_CLIENT_CHAIN):
        for name in str(group or "").split(","):
            name = name.strip()
            if name and name not in ordered:
                ordered.append(name)
    if pinned:
        return ordered or [""]
    return ["", *ordered]


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


# --------------------------------------------------------------------------
# The rest of the diagnosis — one honest message per way a caption fetch fails
# --------------------------------------------------------------------------
# Every one of these replaces what used to be a single wrong sentence ("No
# captions available for this episode"), and every one of them ends in the thing
# the user can actually do next. They are also kept short on purpose: the job
# error the dashboard shows is truncated, and a remedy cut off mid-sentence is
# no remedy at all.
_SESSION_HELP = (
    "Fix: add a signed-in cookies.txt in Tools ▸ YouTube session (or save it "
    "as data/cookies.txt), then press Retry."
)
_UPDATE_HELP = "Also update yt-dlp: `pip install -U yt-dlp`."
# How much of yt-dlp's own line a message quotes.
_DETAIL_LIMIT = 150


def _join(*parts: str) -> str:
    """Sentence-join the non-empty parts, so a missing note leaves no gap."""
    return " ".join(str(part).strip() for part in parts if str(part or "").strip())


def _yt_dlp_note() -> str:
    """Which yt-dlp is installed, and whether it is old enough to be the cause."""
    status = ytdlp_status()
    age = status["age_days"]
    if age is None:
        return f"Installed yt-dlp: {status['version']}."
    if status["stale"]:
        return (f"Installed yt-dlp: {status['version']} ({age} days old — "
                f"older than the {status['stale_days']} days Qyro trusts).")
    return f"Installed yt-dlp: {status['version']} ({age} days old)."


def _lang_note(inventory: dict | None) -> str:
    """What the episode's own metadata says it has, for the message."""
    if not inventory:
        return ""
    languages = inventory.get("languages") or []
    if not languages:
        return "YouTube's metadata for this episode lists no caption track."
    shown = ", ".join(languages[:8]) + ("…" if len(languages) > 8 else "")
    kind = "manual" if inventory.get("manual") else "auto-generated"
    return f"This episode does have {kind} captions ({shown})."


def _bot_check_message(detail: str, inventory: dict | None = None) -> str:
    reason = detail_line(detail, _DETAIL_LIMIT) or "Sign in to confirm you're not a bot"
    return _join(
        f"YouTube refused the caption download with a bot check — “{reason}”.",
        _lang_note(inventory),
        _SESSION_HELP,
        _UPDATE_HELP,
    )


def _refused_message(detail: str, inventory: dict | None = None,
                     clients: int = 0) -> str:
    """Captions exist, and every client we tried was refused them."""
    reason = detail_line(detail, _DETAIL_LIMIT)
    where = f" (tried {clients} player clients)" if clients > 1 else ""
    return _join(
        "This episode has captions, but YouTube withheld them from an "
        f"anonymous request{where}"
        + (f" — “{reason}”." if reason else "."),
        _lang_note(inventory),
        _SESSION_HELP,
        _UPDATE_HELP,
    )


def _network_message(detail: str) -> str:
    reason = detail_line(detail, _DETAIL_LIMIT) or "the connection was closed"
    return _join(
        f"YouTube's caption servers could not be reached — “{reason}”.",
        "That is a connection problem, not a missing transcript: check the "
        "network, VPN or DNS — some campus, office and mobile networks block "
        "YouTube's API endpoints outright.",
        "Transcripts already fetched stay cached; press Retry once it is back.",
    )


def _extractor_message(detail: str) -> str:
    reason = detail_line(detail, _DETAIL_LIMIT)
    js = _JS_RUNTIME_MARKER in str(detail or "").lower()
    return _join(
        "yt-dlp could not read YouTube's answer"
        + (f" — “{reason}”." if reason else "."),
        "That is nearly always an extractor YouTube has outrun: run "
        "`pip install -U yt-dlp`, then press Retry.",
        "YouTube now also needs a JavaScript runtime — install Node.js or "
        "deno if updating alone does not help." if js else "",
        _yt_dlp_note(),
    )


def _video_message(detail: str) -> str:
    reason = detail_line(detail, _DETAIL_LIMIT) or "the video is not available"
    return _join(
        f"This episode cannot be fetched from YouTube — “{reason}”.",
        "It is private, removed, age- or region-restricted, still live, or not "
        "yet premiered; no player client or session changes that.",
        "Pick another episode.",
    )


def _no_captions_message(inventory: dict | None = None) -> str:
    """The only path allowed to say the words the user saw in v0.6.8."""
    return _join(
        "No captions available for this episode — YouTube's own metadata for "
        "it lists no subtitle or auto-caption track, so there is nothing to "
        "transcribe.",
        "Pick another episode, or cut one yourself with Exact range on the "
        "episode card — a manual clip renders fine without a transcript.",
    )


def _wrong_language_message(language: str, inventory: dict | None) -> str:
    label = config.LANGUAGE_LABELS.get(language, language)
    return _join(
        f"This episode has no {label} captions.",
        _lang_note(inventory),
        "Set the caption language to Auto-detect and generate again — Qyro "
        "then takes whichever track the episode actually has.",
    )


def _unknown_message(detail: str) -> str:
    reason = detail_line(detail, _DETAIL_LIMIT)
    return _join(
        "The caption download failed and Qyro could not tell why"
        + (f" — “{reason}”." if reason else "."),
        "Press Retry; if it fails again, add a signed-in cookies.txt in "
        "Tools ▸ YouTube session (or data/cookies.txt) and update yt-dlp "
        "with `pip install -U yt-dlp`.",
    )


def failure_message(failures, inventory: dict | None = None) -> str:
    """Pick the message that matches the worst thing that happened.

    ``failures`` is the list of ``(kind, client, detail)`` every pass left
    behind. The most specific kind wins — a bot check is more useful to hear
    about than a network blip that happened on the way to it.
    """
    failures = list(failures or [])
    kinds = [kind for kind, _client, _detail in failures]
    details = [detail for _kind, _client, detail in failures if detail]
    detail = details[-1] if details else ""
    clients = len({client for _kind, client, _detail in failures})
    worst = worse_kind(*kinds) if kinds else KIND_UNKNOWN
    if worst == KIND_RATE_LIMIT:
        return _blocked_message(_mark_rate_limited())
    if worst == KIND_VIDEO:
        return _video_message(detail)
    if worst == KIND_BOT_CHECK:
        return _bot_check_message(detail, inventory)
    if worst == KIND_PO_TOKEN:
        return _refused_message(detail, inventory, clients)
    if worst == KIND_EXTRACTOR:
        return _extractor_message(detail)
    if worst == KIND_NETWORK:
        return _network_message(detail)
    if worst == KIND_NO_CAPTIONS:
        return _no_captions_message(inventory)
    return _unknown_message(detail)


def _language_keys(mapping) -> list[str]:
    """Sorted language codes out of a yt-dlp ``subtitles`` mapping."""
    if not isinstance(mapping, dict):
        return []
    keys = [str(key) for key in mapping.keys() if str(key or "").strip()]
    # The original audio language first: for an auto chain it is the best
    # transcript there is, and it reads best in a message.
    return sorted(keys, key=lambda code: (not code.endswith("-orig"), code))


def caption_inventory(video_url: str, timeout: int = 180):
    """Ask YouTube which caption tracks this episode really has.

    Returns ``(inventory, kind, error)``; ``inventory`` is ``None`` when the
    question could not be answered — which is *not* the same answer as "there
    are none", and the caller must never treat it as one. This one request is
    what turns "no captions available" from a guess into a fact.
    """
    _pace()
    try:
        proc = run_ytdlp(
            ["-J", "--skip-download", "--no-playlist", video_url],
            timeout=timeout,
            retries=_SUBTITLE_RETRIES,
        )
    except RuntimeError as exc:
        return None, classify_failure(str(exc)), str(exc)
    try:
        data = json.loads(proc.stdout or "")
    except (json.JSONDecodeError, TypeError, ValueError):
        return None, KIND_UNKNOWN, ""
    if not isinstance(data, dict):
        return None, KIND_UNKNOWN, ""
    manual = _language_keys(data.get("subtitles"))
    automatic = _language_keys(data.get("automatic_captions"))
    ordered: list[str] = []
    for code in (*manual, *automatic):
        if code not in ordered:
            ordered.append(code)
    inventory = {
        "manual": manual,
        "auto": automatic,
        "languages": ordered,
        "title": str(data.get("title") or ""),
        "id": str(data.get("id") or ""),
    }
    return inventory, KIND_OK, ""


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
    """Fetch and cache one caption track, diagnosing every way it can fail.

    ``language`` (v0.6.0) picks which subtitle track to ask for first — ``hi``
    prefers Hindi, ``hinglish`` Hindi-then-English, ``auto`` keeps the v0.5.0
    English-first chain. A cached transcript always wins, so switching language
    on an already-fetched episode needs no new request.

    The order matters once YouTube starts refusing anonymous caption requests,
    and it is the whole fix for the "still rate-limiting" loop:

    1. the normalized cache — no request at all;
    2. a caption file already on disk from an interrupted run — still no
       request, and the reason a block no longer throws away real work;
    3. an active cooldown — refuse without touching YouTube;
    4. one pass per player client, spaced apart, because a refusal on one
       client usually leaves another working while retrying it never will.

    v0.6.9 fixed what step 4 *counted as* a refusal. It used to be "an HTTP
    429, and nothing else": a bot check, a PO-token skip, a TLS reset or an
    extractor too old to parse YouTube all ended the walk after one client and
    were reported as "No captions available for this episode" — a claim about
    the video that nobody had checked. Now every empty pass says *why* it was
    empty, the walk continues for anything YouTube-side, and "no captions" is
    only said once :func:`caption_inventory` — the episode's own metadata —
    agrees that there is nothing to fetch.
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

    chain = sub_lang_chain(language)
    clients = player_client_chain()
    limit = max(1, min(int(config.TRANSCRIPT_MAX_PASSES), len(clients)))
    failures: list[tuple[str, str, str]] = []   # (kind, client, detail)
    inventory: dict | None = None
    inventory_kind = KIND_UNKNOWN
    inventory_error = ""
    probed = False
    blocked = False

    for index, client in enumerate(clients):
        # A 429 keeps walking the whole chain (that is the v0.6.7 escape and
        # it costs nothing extra); anything else is capped, because rotating
        # clients cannot fix a dead connection or a stale extractor.
        if index >= limit and not blocked:
            break
        if index:
            time.sleep(_RATE_LIMIT_BACKOFF if blocked
                       else config.CLIENT_ROTATION_PAUSE)

        langs = _languages_for_pass(chain, inventory, index > 0, language)
        if not langs:
            # The episode's own metadata says the language the user picked is
            # not one it has. Say so instead of asking YouTube again.
            raise TranscriptUnavailable(
                _wrong_language_message(language, inventory)
            )

        segments, source, kind, error = _subtitle_pass(
            video_id, video_url, langs, client, cache
        )
        if segments:
            _clear_rate_limit()
            return segments, source

        failures.append((kind, client, error))
        if kind == KIND_RATE_LIMIT:
            blocked = True
            continue
        if kind == KIND_VIDEO:
            # Private, removed, region-locked: no client and no session helps.
            raise TranscriptUnavailable(_video_message(error))
        if kind != KIND_NO_CAPTIONS:
            # Refused, unreachable, unparsed — another client may still work.
            continue

        # A clean pass that produced nothing is *evidence*, not proof: YouTube
        # answers exactly this way when it withholds caption tracks that exist.
        # Ask the episode what it has before telling the user it has nothing.
        if not probed:
            inventory, inventory_kind, inventory_error = caption_inventory(
                video_url
            )
            probed = True
        if inventory is None:
            # The question itself failed — that answer is more honest than
            # "no captions", unless it failed for a reason we cannot name.
            if inventory_kind == KIND_VIDEO:
                raise TranscriptUnavailable(_video_message(inventory_error))
            if inventory_kind in (KIND_RATE_LIMIT, KIND_BOT_CHECK,
                                  KIND_PO_TOKEN, KIND_NETWORK, KIND_EXTRACTOR):
                failures.append((inventory_kind, client, inventory_error))
                blocked = blocked or inventory_kind == KIND_RATE_LIMIT
                continue
            if inventory_error:
                # An error we could not classify is not evidence of a
                # caption-less video either.
                raise TranscriptUnavailable(_unknown_message(inventory_error))
            raise TranscriptUnavailable(_no_captions_message(None))
        if inventory["languages"]:
            # Captions exist and this client was refused them. Rotate — and
            # from here on ask only for languages the episode really has.
            failures.append((KIND_PO_TOKEN, client, error))
            continue
        if _refusal_somewhere(failures):
            # The metadata lists nothing, but YouTube has already refused this
            # episode once in this run — and a refusal empties a metadata
            # listing exactly the way it empties a caption pass. Believe the
            # refusal, not the silence: keep rotating, and let the final
            # message tell the story the refusals add up to.
            continue
        raise TranscriptUnavailable(_no_captions_message(inventory))

    raise TranscriptUnavailable(failure_message(failures, inventory))


# Kinds that mean YouTube declined to answer rather than "there is nothing":
# any of them makes an empty caption listing untrustworthy, because the same
# refusal empties the metadata that lists captions.
_REFUSAL_KINDS = (KIND_RATE_LIMIT, KIND_BOT_CHECK, KIND_PO_TOKEN)


def _refusal_somewhere(failures) -> bool:
    """Did YouTube refuse this episode at any point in this run?"""
    return any(kind in _REFUSAL_KINDS
               for kind, _client, _detail in (failures or []))


def _matches_language(pattern: str, language: str) -> bool:
    """Does a ``--sub-langs`` pattern (``en``, ``en.*``, ``all``) want this?"""
    pattern = str(pattern or "").strip()
    language = str(language or "").strip()
    if not pattern or not language:
        return False
    if pattern in ("all", ".*") or pattern == language:
        return True
    if "*" in pattern:
        stem = pattern.split("*", 1)[0].rstrip(".-")
        return bool(stem) and language.startswith(stem)
    return False


def _languages_for_pass(
    chain: list[str],
    inventory: dict | None,
    rotation: bool,
    language: str = "auto",
) -> list[str]:
    """Which languages the next player client should be asked for.

    The first pass keeps the configured chain untouched. Once we are rotating,
    the point is to spend as few requests as possible on a client that has not
    been refused yet — so if the episode's metadata is known, ask only for the
    tracks it actually has (an ``auto`` choice also takes the episode's own
    language, which is the transcript the user wanted all along), and if it is
    not known, ask the first few.

    An empty list means "the language the user picked does not exist on this
    episode", which the caller turns into a message rather than a request.
    """
    chain = list(chain or [])
    if not rotation:
        return chain
    cap = max(1, int(config.TRANSCRIPT_ROTATION_LANGUAGES))
    known = (inventory or {}).get("languages") or []
    if known:
        wanted = [code for code in known
                  if any(_matches_language(pattern, code) for pattern in chain)]
        if not wanted and str(language or "auto").strip().lower() == "auto":
            # Auto-detect means auto-detect: take the episode's own track.
            wanted = known[:1]
        return wanted[:cap]
    if str(language or "auto").strip().lower() != "auto":
        # An explicit choice with nothing to aim at: let the caller say so.
        return []
    return chain[:cap]


def _subtitle_pass(
    video_id: str,
    video_url: str,
    langs: list[str],
    client: str,
    cache: Path,
) -> tuple[list[Segment], str, str, str]:
    """One caption fetch against a single player client.

    Returns ``(segments, source_name, kind, error)``. ``kind`` is one of the
    ``KIND_*`` values and is the reason the pass came back empty — the
    distinction the caller walks the client chain on. Languages are walked one
    per request on purpose — asking yt-dlp for several at once is what triggers
    YouTube's HTTP 429s — and a 429 stops the walk at once so the caller can
    move to the next client instead of spending more requests on one that is
    already blocked.
    """
    prefix = config.SUBS_DIR / video_id
    error = ""
    kind = KIND_NO_CAPTIONS

    for sub_lang in langs:
        _pace()
        output = ""
        try:
            proc = run_ytdlp(
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
            # Tolerate a stub/None return: the *output* is a diagnostic, and
            # a missing one must never be what fails a caption fetch.
            output = "" if proc is None else (
                f"{getattr(proc, 'stdout', '') or ''}\n"
                f"{getattr(proc, 'stderr', '') or ''}"
            )
        except RuntimeError as exc:      # RateLimited included
            error = str(exc)
            seen = classify_failure(error)
            kind = worse_kind(kind, seen)
            if seen != KIND_UNKNOWN:
                # None of these are about the *language* that was asked for: a
                # block, a bot check, a withheld caption track, a private video,
                # a dead connection or a broken extractor refuses every language
                # the same way. Stop the walk here and let the caller decide
                # whether another player client is worth asking — which is what
                # v0.6.8 never did, because it only ever stopped for a 429.
                return [], "", kind, error
            # An unrecognised failure might still be per-language, so the walk
            # continues and the disk is checked: yt-dlp can leave a usable
            # subtitle file behind even when a later request fails.

        files = _subtitle_files(video_id)
        if not files:
            if not error:
                # A clean exit with nothing on disk: read the output, because
                # this is where YouTube's PO-token refusal shows up — and where
                # v0.6.8 saw only silence and said "no captions".
                verdict = classify_caption_output(output)
                if verdict != KIND_NO_CAPTIONS:
                    kind = worse_kind(kind, verdict)
                    error = error or detail_line(output)
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
            kind = worse_kind(kind, KIND_UNKNOWN)
            error = error or f"yt-dlp wrote {source_name} but it held no readable captions"
            continue

        try:
            _write_cache(cache, segments)
        except OSError:
            return [], "", kind, error
        _discard_subtitle_files(video_id)
        return segments, source_name, KIND_OK, ""

    return [], "", kind, error



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
            kind = classify_failure(str(exc))
            if kind in (KIND_BOT_CHECK, KIND_PO_TOKEN):
                # The raw yt-dlp line is honest but not actionable; the cure
                # for a refused download is the same one captions need.
                what = ("a bot check" if kind == KIND_BOT_CHECK
                        else "a withheld stream (YouTube's PO Token check)")
                raise RuntimeError(
                    f"YouTube refused this video download with {what} — "
                    f"{detail_line(str(exc))}. " + _SESSION_HELP
                ) from exc
            if kind != KIND_RATE_LIMIT:
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
