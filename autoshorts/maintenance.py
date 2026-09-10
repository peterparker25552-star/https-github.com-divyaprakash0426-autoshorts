"""Maintenance helpers shared by both servers: option validation, chapters,
sidecar SRT files, re-render/polish, batch queueing, storage tools and backups.

Every function here is transport-agnostic — the FastAPI and stdlib layers only
translate :class:`ServiceError` into an HTTP status plus ``{"detail": ...}``,
which keeps the two servers byte-for-byte identical in behaviour.
"""
from __future__ import annotations

import json
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import config, demo, highlights, llm, youtube
from .store import Store
from .transcripts import segments_for_window, to_sentences, to_srt

CLEAN_TARGETS = ("media", "subs", "thumbs", "clips")
VERSIONS_TTL = 300.0  # seconds

RENDER_DEFAULTS = {
    "style": config.DEFAULT_STYLE,
    "quality": config.DEFAULT_QUALITY,
    "format": config.DEFAULT_FORMAT,
    "captions": config.DEFAULT_CAPTIONS,
    "speed": config.DEFAULT_SPEED,
    "progress": False,
    "silence": False,
    "loud": False,
}

_FLAG_KEYS = ("progress", "silence", "loud")
_CHOICES = {
    "style": ("crop", "blur"),
    "quality": ("fast", "full"),
    "format": config.FORMATS,
    "captions": config.CAPTION_STYLES,
}


class ServiceError(Exception):
    """An HTTP-shaped error: ``status`` + the user-facing ``detail``."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _bad(option: str) -> ServiceError:
    choices = _CHOICES[option]
    return ServiceError(422, f"{option} must be one of: {', '.join(choices)}")


# --------------------------------------------------------------------------
# Render options
# --------------------------------------------------------------------------
def render_opts_from(body: dict, base: dict | None = None) -> dict:
    """Validate render options from a request body.

    Unknown keys are ignored, missing keys fall back to ``base`` (a clip's
    stored options) and then to :data:`RENDER_DEFAULTS`. Bad values raise
    :class:`ServiceError` 422 — including speeds that are not in
    :data:`config.SPEEDS` and non-boolean flags.
    """
    body = body if isinstance(body, dict) else {}
    opts = dict(RENDER_DEFAULTS)
    for key, value in (base or {}).items():
        if key in opts and value is not None:
            opts[key] = value

    for key in ("style", "quality", "format", "captions"):
        if key not in body:
            continue
        value = body[key]
        if not isinstance(value, str) or value not in _CHOICES[key]:
            raise _bad(key)
        opts[key] = value

    if "speed" in body:
        value = body["speed"]
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ServiceError(
                422,
                "speed must be one of: "
                + ", ".join(str(s) for s in config.SPEEDS),
            )
        try:
            speed = float(value)
        except (TypeError, ValueError):
            speed = -1.0
        if speed not in config.SPEEDS:
            raise ServiceError(
                422,
                "speed must be one of: "
                + ", ".join(str(s) for s in config.SPEEDS),
            )
        opts["speed"] = speed

    for key in _FLAG_KEYS:
        if key not in body:
            continue
        value = body[key]
        if not isinstance(value, bool):
            raise ServiceError(422, f"{key} must be a boolean")
        opts[key] = value

    # Stored/replayed bases can be stale — normalise before use.
    for key in ("style", "quality", "format", "captions"):
        if opts[key] not in _CHOICES[key]:
            opts[key] = RENDER_DEFAULTS[key]
    try:
        opts["speed"] = float(opts["speed"])
    except (TypeError, ValueError):
        opts["speed"] = config.DEFAULT_SPEED
    if opts["speed"] not in config.SPEEDS:
        opts["speed"] = config.DEFAULT_SPEED
    for key in _FLAG_KEYS:
        opts[key] = bool(opts[key])
    return opts


def default_job_params() -> dict:
    """Automatic-clip parameters used by batch/auto-pilot queueing."""
    return {
        "count": config.DEFAULT_CLIP_COUNT,
        "min_dur": config.MIN_CLIP_SECONDS,
        "max_dur": config.MAX_CLIP_SECONDS,
        "profile": "viral",
        **RENDER_DEFAULTS,
    }


# --------------------------------------------------------------------------
# Transcripts
# --------------------------------------------------------------------------
def _episode_segments(episode: dict) -> list:
    """Demo transcript, cached transcript, else a YouTube fetch."""
    if demo.is_demo(episode):
        return demo.demo_segments(episode["id"])
    cached = youtube.load_cached_transcript(episode["id"])
    if cached:
        return cached
    try:
        segments, _source = youtube.get_transcript(
            episode["id"], episode.get("url", "")
        )
    except Exception as exc:
        raise ServiceError(404, f"No transcript available: {exc}") from exc
    return segments


def _episode_or_404(store: Store, ep_id: str) -> dict:
    episode = store.get_episode(ep_id)
    if not episode:
        raise ServiceError(404, "Episode not found")
    return episode


def episode_chapters(store: Store, ep_id: str, count: int = 10) -> dict:
    """YouTube-style chapters from the top story highlights."""
    episode = _episode_or_404(store, ep_id)
    segments = _episode_segments(episode)
    sentences = to_sentences(segments)
    if not sentences:
        raise ServiceError(404, "No transcript available for this episode")

    moments = highlights.find_highlights(
        sentences,
        count=max(1, int(count)),
        min_dur=config.MIN_CLIP_SECONDS,
        max_dur=max(config.MAX_CLIP_SECONDS, 90.0),
        profile="story",
    )
    if not moments:
        raise ServiceError(404, "No chapters could be generated")

    chapters = [
        {
            "time": round(moment.start, 2),
            "title": moment.title,
            "text": f"{_mmss(moment.start)} {moment.title}",
        }
        for moment in moments
    ]
    if chapters and chapters[0]["time"] > 5:
        chapters.insert(
            0, {"time": 0.0, "title": "Intro", "text": "0:00 Intro"}
        )
    text = "\n".join(chapter["text"] for chapter in chapters)
    return {
        "episode_id": ep_id,
        "episode_title": episode.get("title", ""),
        "chapters": chapters,
        "text": text,
    }


def _mmss(seconds: float) -> str:
    total = max(0, int(round(float(seconds))))
    return f"{total // 60}:{total % 60:02d}"


def clip_srt(store: Store, clip_id: str) -> Path:
    """Write ``<clip>.srt`` (0-based, clipped window) and return its path."""
    clip = store.get_clip(clip_id)
    if not clip:
        raise ServiceError(404, "Clip not found")
    episode = store.get_episode(clip["episode_id"])
    if not episode:
        raise ServiceError(404, "Episode not found")

    start = float(clip.get("start") or 0.0)
    end = float(clip.get("end") or 0.0)
    segments = segments_for_window(_episode_segments(episode), start, end)
    if not segments:
        raise ServiceError(404, "No transcript available for this clip")

    text = to_srt(segments, offset=-start)
    if not text.strip():
        raise ServiceError(404, "No transcript available for this clip")
    path = config.SUBS_DIR / f"{clip_id}.srt"
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# Clips: re-render + polish
# --------------------------------------------------------------------------
def rerender_clip(
    store: Store, pipeline, clip_id: str, overrides: dict | None = None
) -> dict:
    """Queue a fresh manual render of a clip's stored range and options."""
    overrides = overrides if isinstance(overrides, dict) else {}
    # Validate the overrides before touching the store: FastAPI rejects a bad
    # body (422) before the handler runs, so this path must match.
    render_opts_from(overrides)

    clip = store.get_clip(clip_id)
    if not clip:
        raise ServiceError(404, "Clip not found")
    episode = store.get_episode(clip["episode_id"])
    if not episode:
        raise ServiceError(404, "Episode not found")
    if episode.get("status") == "processing":
        raise ServiceError(409, "Already processing this episode")

    opts = render_opts_from(overrides, base=clip.get("render"))
    params = {
        "kind": "manual",
        "start": float(clip.get("start") or 0.0),
        "end": float(clip.get("end") or 0.0),
        "title": clip.get("title") or "Re-render",
        **opts,
    }
    job = pipeline.start_job(clip["episode_id"], params)
    return {"job_id": job["id"], "clip_id": clip_id, "options": opts}


def polish_clip(store: Store, clip_id: str, timeout: int = 25) -> dict:
    """Rewrite a clip's upload pack with the configured LLM (503/502 if not)."""
    clip = store.get_clip(clip_id)
    if not clip:
        raise ServiceError(404, "Clip not found")
    if not llm.available():
        raise ServiceError(
            503,
            "No LLM configured — set OPENAI_API_KEY (and optionally "
            "OPENAI_BASE_URL / OPENAI_MODEL) to enable polish.",
        )

    pack = dict(clip.get("pack") or {})
    if not pack.get("titles"):
        pack = _fallback_pack(store, clip)
    context = " | ".join(
        part
        for part in (
            str(clip.get("episode_title") or ""),
            str(clip.get("title") or ""),
            f"{round(float(clip.get('duration') or 0))}s clip",
        )
        if part
    )
    polished = llm.polish_pack(pack, context, timeout=timeout)
    if not polished:
        raise ServiceError(502, "LLM polish failed — the offline pack is unchanged")

    stored = store.update_clip(clip_id, pack=polished)
    return {"clip_id": clip_id, "pack": (stored or {}).get("pack", polished)}


def _fallback_pack(store: Store, clip: dict) -> dict:
    """Build an offline pack for clips rendered before v0.3.0."""
    from . import titles  # local import keeps the module import graph flat

    return titles.generate_pack(
        str(clip.get("title") or ""),
        str(clip.get("title") or ""),
        str(clip.get("episode_title") or ""),
        "viral",
        float(clip.get("duration") or 0),
    )


# --------------------------------------------------------------------------
# Batch queueing
# --------------------------------------------------------------------------
def batch_queue(
    store: Store, pipeline, params: dict, youtube_ok: bool
) -> dict:
    """Queue automatic clips for every episode that is new or errored."""
    queued: list[dict] = []
    skipped: list[dict] = []
    for episode in store.episodes():
        if str(episode.get("status") or "new") not in ("new", "error"):
            continue
        if not demo.is_demo(episode) and not youtube_ok:
            skipped.append(
                {"id": episode["id"], "reason": "YouTube unreachable"}
            )
            continue
        job = pipeline.start_job(episode["id"], dict(params))
        queued.append({"episode_id": episode["id"], "job_id": job["id"]})
    return {"queued": len(queued), "skipped": len(skipped),
            "jobs": queued, "skipped_episodes": skipped}


def retry_job(store: Store, pipeline, job_id: str) -> dict:
    """Re-queue a finished job with exactly the same episode and params."""
    job = store.job(job_id)
    if not job:
        raise ServiceError(404, "Job not found")
    if job.get("status") in ("queued", "running"):
        raise ServiceError(409, "Job is still running")
    if not store.get_episode(job["episode_id"]):
        raise ServiceError(404, "Episode not found")
    new_job = pipeline.start_job(job["episode_id"], dict(job.get("params") or {}))
    return {"job_id": new_job["id"], "retried_from": job_id}


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------
def _dir_stats(path: Path) -> tuple[int, int]:
    total = 0
    count = 0
    if path.exists():
        for entry in path.rglob("*"):
            try:
                if entry.is_file():
                    total += entry.stat().st_size
                    count += 1
            except OSError:
                continue
    return total, count


def disk_usage(path: Path | None = None) -> dict:
    """Free/total/used bytes for the data volume (zeros when unknown)."""
    try:
        usage = shutil.disk_usage(path or config.DATA_DIR)
    except OSError:
        return {"free": 0, "total": 0, "used": 0}
    return {"free": usage.free, "total": usage.total, "used": usage.used}


def storage_info(store: Store | None = None) -> dict:
    """Per-directory sizes plus disk usage and library counts."""
    dirs = {}
    for name, path in (
        ("media", config.MEDIA_DIR),
        ("clips", config.CLIPS_DIR),
        ("thumbs", config.THUMBS_DIR),
        ("subs", config.SUBS_DIR),
    ):
        size, files = _dir_stats(path)
        dirs[name] = {
            "path": str(path),
            "bytes": size,
            "files": files,
        }
    disk = disk_usage()

    clips = store.clips() if store else []
    episodes = store.episodes() if store else []
    return {
        "dirs": dirs,
        "total_bytes": sum(entry["bytes"] for entry in dirs.values()),
        "total_files": sum(entry["files"] for entry in dirs.values()),
        "disk": disk,
        "counts": {
            "episodes": len(episodes),
            "clips": len(clips),
            "media": dirs["media"]["files"],
            "thumbs": dirs["thumbs"]["files"],
            "subs": dirs["subs"]["files"],
        },
    }


def clean_storage(store: Store, target: str) -> dict:
    """Free space for one storage area without breaking the library."""
    if target not in CLEAN_TARGETS:
        raise ServiceError(
            422, "target must be one of: " + ", ".join(CLEAN_TARGETS)
        )
    removed_files = 0
    freed = 0
    entries = 0

    def _unlink(path: Path) -> None:
        nonlocal removed_files, freed
        try:
            size = path.stat().st_size
            path.unlink()
        except OSError:
            return
        removed_files += 1
        freed += size

    if target == "media":
        for path in sorted(config.MEDIA_DIR.rglob("*")):
            if path.is_file():
                _unlink(path)
    elif target == "subs":
        # Keep the normalised <id>.segments.json caches — dropping them forces
        # a fresh (rate-limited) YouTube caption download on the next render.
        for path in sorted(config.SUBS_DIR.rglob("*")):
            if path.is_file() and not path.name.endswith(".segments.json"):
                _unlink(path)
    elif target == "thumbs":
        for path in sorted(config.THUMBS_DIR.rglob("*")):
            if path.is_file():
                _unlink(path)
        for clip in store.clips():
            if clip.get("thumb"):
                store.update_clip(clip["id"], thumb=None)
    else:  # clips
        for path in sorted(config.CLIPS_DIR.rglob("*")):
            if path.is_file():
                _unlink(path)
        for clip in store.clips():
            store.delete_clip(clip["id"])
            entries += 1

    return {
        "target": target,
        "removed_files": removed_files,
        "freed_bytes": freed,
        "removed_clips": entries,
    }


# --------------------------------------------------------------------------
# Backup / restore
# --------------------------------------------------------------------------
def backup_bytes(store: Store) -> bytes:
    """The state file, pretty-printed, ready to download."""
    payload = store.export_state()
    return json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8")


def restore_state(store: Store, data) -> dict:
    if not isinstance(data, dict):
        raise ServiceError(422, "State must be a JSON object")
    try:
        result = store.import_state(data)
    except ValueError as exc:
        raise ServiceError(422, str(exc)) from exc
    return result


# --------------------------------------------------------------------------
# Versions (cached)
# --------------------------------------------------------------------------
_versions_cache: tuple[float, dict] = (0.0, {})
_YTDLP_VERSION = re.compile(r"^(\d{4}\.\d{2}\.\d{2}(?:\.\d+)?)", re.MULTILINE)


def versions_info(ttl: float = VERSIONS_TTL) -> dict:
    """python / ffmpeg / yt-dlp versions, cached for ``ttl`` seconds."""
    global _versions_cache
    cached_at, cached = _versions_cache
    if cached and (time.time() - cached_at) < ttl:
        return dict(cached)
    info = {
        "python": platform.python_version(),
        "ffmpeg": _ffmpeg_version(),
        "yt_dlp": _ytdlp_version(),
    }
    _versions_cache = (time.time(), info)
    return dict(info)


def _first_line(proc: subprocess.CompletedProcess | None) -> str:
    if not proc:
        return ""
    for stream in (proc.stdout, proc.stderr):
        for line in (stream or "").splitlines():
            if line.strip():
                return line.strip()
    return ""


def _ffmpeg_version() -> str:
    try:
        proc = subprocess.run(
            [config.FFMPEG_BIN, "-version"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception:
        return "missing"
    return _first_line(proc) or "unknown"


def _ytdlp_version() -> str:
    """yt-dlp version string, or "missing" when it is not installed."""
    candidates = []
    found = shutil.which("yt-dlp")
    if found:
        candidates.append([found, "--version"])
    candidates.append([sys.executable or "python3", "-m", "yt_dlp", "--version"])
    for command in candidates:
        try:
            proc = subprocess.run(
                command, capture_output=True, text=True, timeout=30
            )
        except Exception:
            continue
        if proc.returncode != 0:
            continue
        match = _YTDLP_VERSION.search(_first_line(proc))
        if match:
            return match.group(1)
        line = _first_line(proc)
        if line:
            return line
    return "missing"
