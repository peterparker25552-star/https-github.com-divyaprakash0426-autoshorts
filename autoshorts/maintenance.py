"""Maintenance helpers shared by both servers: option validation, chapters,
sidecar SRT files, re-render/polish, ingest, batch queueing, storage tools,
backups, search, CSV export, job cancel/retry and episode deletion.

Every function here is transport-agnostic — the FastAPI and stdlib layers only
translate :class:`ServiceError` into an HTTP status plus ``{"detail": ...}``,
which keeps the two servers byte-for-byte identical in behaviour.
"""
from __future__ import annotations

import csv
import io
import json
import platform
import subprocess
import time
from pathlib import Path

from . import config, demo, highlights, llm, youtube
from .store import Store
from .transcripts import segments_for_window, to_sentences, to_srt

CLEAN_TARGETS = ("subs", "thumbs", "clips")
VERSIONS_TTL = 300.0  # seconds

RENDER_DEFAULTS = {
    "style": config.DEFAULT_STYLE,
    "quality": config.DEFAULT_QUALITY,
    "format": config.DEFAULT_FORMAT,
    "captions": config.DEFAULT_CAPTIONS,
    "captions_pos": config.DEFAULT_CAPTIONS_POS,
    "captions_box": config.DEFAULT_CAPTIONS_BOX,
    "speed": config.DEFAULT_SPEED,
    "progress": False,
    "silence": False,
    "loud": False,
}

_FLAG_KEYS = ("progress", "silence", "loud", "captions_box")
_CHOICES = {
    "style": config.STYLES,
    "quality": ("fast", "full"),
    "format": config.FORMATS,
    "captions": config.CAPTION_STYLES,
    "captions_pos": config.CAPTION_POSITIONS,
}

RENDER_OPT_KEYS = frozenset(RENDER_DEFAULTS)
SHORTS_KEYS = RENDER_OPT_KEYS | {"count", "min_dur", "max_dur", "profile"}
PREVIEW_KEYS = SHORTS_KEYS
MANUAL_KEYS = SHORTS_KEYS | {"title", "start", "end"}
BATCH_KEYS = SHORTS_KEYS | {"urls", "episodes"}
RERENDER_KEYS = RENDER_OPT_KEYS | {"title"}
SETTINGS_KEYS = frozenset(
    ("playlist_url", "playlist_title", "channel", "autopilot")
)
PROFILES = ("viral", "story", "facts", "energy")
COUNT_RANGE = (1, 12)


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
# Body/key validation (both servers call the exact same functions)
# --------------------------------------------------------------------------
def check_unknown_keys(body: dict, allowed, message: str = "Unknown options") -> None:
    """Reject bodies that carry keys outside ``allowed`` (422 ``message``)."""
    if not isinstance(body, dict):
        return
    extra = sorted(str(key) for key in body if key not in allowed)
    if extra:
        raise ServiceError(422, message)


def _int_in(
    body: dict, name: str, default: int, lo: int, hi: int
) -> int:
    value = body.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ServiceError(
            422, f"{name} must be an integer between {lo} and {hi}"
        )
    if not lo <= value <= hi:
        raise ServiceError(
            422, f"{name} must be an integer between {lo} and {hi}"
        )
    return int(value)


def _num_in(
    body: dict, name: str, default: float, lo: float, hi: float
) -> float:
    value = body.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ServiceError(422, f"{name} must be a number between {lo} and {hi}")
    fv = float(value)
    if not lo <= fv <= hi:
        raise ServiceError(422, f"{name} must be a number between {lo} and {hi}")
    return fv


def _choice_in(
    body: dict, name: str, default: str, choices: tuple[str, ...]
) -> str:
    value = body.get(name, default)
    if value not in choices:
        raise ServiceError(422, f"{name} must be one of: {', '.join(choices)}")
    return str(value)


# --------------------------------------------------------------------------
# Render options
# --------------------------------------------------------------------------
def render_opts_from(body: dict, base: dict | None = None) -> dict:
    """Validate render options from a request body.

    Missing keys fall back to ``base`` (a clip's stored options — so
    ``captions_pos``/``captions_box`` survive a re-render) and then to
    :data:`RENDER_DEFAULTS`. Bad values raise :class:`ServiceError` 422:
    unknown choices, non-boolean flags, and any ``speed`` that is not a
    number inside :data:`config.SPEED_RANGE` (booleans are rejected — rule 7).
    """
    body = body if isinstance(body, dict) else {}
    opts = dict(RENDER_DEFAULTS)
    for key, value in (base or {}).items():
        if key in opts and value is not None:
            opts[key] = value

    for key in ("style", "quality", "format", "captions", "captions_pos"):
        if key not in body:
            continue
        value = body[key]
        if not isinstance(value, str) or value not in _CHOICES[key]:
            raise _bad(key)
        opts[key] = value

    if "speed" in body:
        value = body["speed"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ServiceError(
                422,
                "speed must be a number between "
                f"{config.SPEED_RANGE[0]:g} and {config.SPEED_RANGE[1]:g}",
            )
        speed = float(value)
        lo, hi = config.SPEED_RANGE
        if not (lo <= speed <= hi):
            raise ServiceError(
                422,
                "speed must be a number between "
                f"{lo:g} and {hi:g}",
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
    for key in ("style", "quality", "format", "captions", "captions_pos"):
        if opts[key] not in _CHOICES[key]:
            opts[key] = RENDER_DEFAULTS[key]
    try:
        opts["speed"] = float(opts["speed"])
    except (TypeError, ValueError):
        opts["speed"] = config.DEFAULT_SPEED
    lo, hi = config.SPEED_RANGE
    if not lo <= opts["speed"] <= hi:
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


def shorts_params_from(body: dict) -> dict:
    """Validated ``POST /shorts`` body (also used by batch)."""
    check_unknown_keys(body, SHORTS_KEYS)
    body = body if isinstance(body, dict) else {}
    min_dur = _num_in(body, "min_dur", config.MIN_CLIP_SECONDS, 8, 120)
    max_dur = _num_in(body, "max_dur", config.MAX_CLIP_SECONDS, 10, 180)
    if max_dur < min_dur:
        raise ServiceError(422, "max_dur must be >= min_dur")
    return {
        "count": _int_in(body, "count", config.DEFAULT_CLIP_COUNT, *COUNT_RANGE),
        "min_dur": min_dur,
        "max_dur": max_dur,
        "profile": _choice_in(body, "profile", "viral", PROFILES),
        **render_opts_from(body),
    }


def preview_params_from(body: dict) -> dict:
    """Validated ``POST /preview`` body (same selectors, wider duration box)."""
    check_unknown_keys(body, PREVIEW_KEYS)
    body = body if isinstance(body, dict) else {}
    min_dur = _num_in(body, "min_dur", config.MIN_CLIP_SECONDS, 5, 180)
    max_dur = _num_in(body, "max_dur", config.MAX_CLIP_SECONDS, 5, 180)
    if max_dur < min_dur:
        raise ServiceError(422, "max_dur must be >= min_dur")
    return {
        "count": _int_in(body, "count", config.DEFAULT_CLIP_COUNT, *COUNT_RANGE),
        "min_dur": min_dur,
        "max_dur": max_dur,
        "profile": _choice_in(body, "profile", "viral", PROFILES),
        **render_opts_from(body),
    }


def manual_params_from(body: dict) -> dict:
    """Validated ``POST /manual`` body."""
    check_unknown_keys(body, MANUAL_KEYS)
    body = body if isinstance(body, dict) else {}
    start = body.get("start")
    end = body.get("end")
    if (
        isinstance(start, bool) or not isinstance(start, (int, float))
        or isinstance(end, bool) or not isinstance(end, (int, float))
    ):
        raise ServiceError(422, "start and end must be numbers")
    title = body.get("title", "")
    if not isinstance(title, str):
        raise ServiceError(422, "title must be a string")
    if len(title) > 120:
        raise ServiceError(422, "title must be at most 120 characters")
    if float(end) - float(start) < 5:
        raise ServiceError(422, "Manual clips must be at least 5 seconds")
    return {
        "start": float(start),
        "end": float(end),
        "title": title,
        "profile": _choice_in(body, "profile", "viral", PROFILES),
        **render_opts_from(body),
        "kind": "manual",
    }


def rerender_params_from(body: dict, base: dict | None) -> dict:
    """Validated re-render overrides: the render options + title only."""
    check_unknown_keys(body, RERENDER_KEYS)
    body = body if isinstance(body, dict) else {}
    opts = render_opts_from(body, base=base)
    title = body.get("title")
    if title is not None and (
        not isinstance(title, str) or not title.strip() or len(title) > 120
    ):
        raise ServiceError(422, "title must be a non-empty string of at most 120 characters")
    return opts


def settings_update_from(body: dict) -> dict:
    """Validated ``POST /settings`` body — persists the given setting keys."""
    if not isinstance(body, dict):
        raise ServiceError(422, "Settings must be a JSON object")
    update: dict = {}
    if "autopilot" in body:
        if not isinstance(body["autopilot"], bool):
            raise ServiceError(422, "autopilot must be a boolean")
        update["autopilot"] = body["autopilot"]
    for key in ("playlist_url", "playlist_title", "channel"):
        if key not in body:
            continue
        value = body[key]
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise ServiceError(422, f"{key} must be a string")
        if key == "playlist_url" and value and len(value) < 8:
            raise ServiceError(422, "playlist_url must be at least 8 characters")
        update[key] = value
    if not update:
        raise ServiceError(
            422,
            "Provide at least one setting: "
            + ", ".join(sorted(SETTINGS_KEYS)),
        )
    return update


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
            "start": round(moment.start, 2),
            "title": moment.title,
            "text": f"{_mmss(moment.start)} {moment.title}",
        }
        for moment in moments
    ]
    if chapters and chapters[0]["start"] > 5:
        chapters.insert(
            0, {"start": 0.0, "title": "Intro", "text": "0:00 Intro"}
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
# CSV export
# --------------------------------------------------------------------------
def export_csv(
    store: Store, ep_id: str, count, profile
) -> tuple[str, str]:
    """``(filename, csv_text)`` for an episode's top moments.

    ``count`` must be an int in 1–12 and ``profile`` a known profile (422
    otherwise); unknown episodes are 404.
    """
    if isinstance(count, bool) or not isinstance(count, int) or not (
        COUNT_RANGE[0] <= count <= COUNT_RANGE[1]
    ):
        raise ServiceError(
            422, f"count must be an integer between {COUNT_RANGE[0]} and {COUNT_RANGE[1]}"
        )
    if profile not in PROFILES:
        raise ServiceError(422, f"profile must be one of: {', '.join(PROFILES)}")
    episode = _episode_or_404(store, ep_id)
    try:
        segments = _episode_segments(episode)
    except ServiceError:
        raise
    sentences = to_sentences(segments)
    moments = highlights.find_highlights(
        sentences, count=count, min_dur=config.MIN_CLIP_SECONDS,
        max_dur=config.MAX_CLIP_SECONDS, profile=profile,
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        ["start", "end", "duration", "title", "score", "reasons"]
    )
    for moment in moments:
        writer.writerow(
            [
                f"{moment.start:.2f}",
                f"{moment.end:.2f}",
                f"{moment.duration:.2f}",
                moment.title,
                f"{moment.score:.2f}",
                "; ".join(moment.reasons),
            ]
        )
    return f"autoshorts-{ep_id}-moments.csv", buffer.getvalue()


# --------------------------------------------------------------------------
# Search (offline, over demo + cached transcripts)
# --------------------------------------------------------------------------
def search_transcripts(store: Store, q, limit: int = 50) -> dict:
    """Case-insensitive transcript search across loaded episodes.

    Only offline sources are consulted: built-in demo transcripts plus the
    cached ``<id>.segments.json`` transcript caches — never the network.
    """
    if not isinstance(q, str) or len(q.strip()) < 2:
        raise ServiceError(422, "q must be at least 2 characters")
    needle = q.strip().lower()
    results: list[dict] = []
    for episode in store.episodes():
        if demo.is_demo(episode):
            try:
                segments = demo.demo_segments(episode["id"])
            except Exception:
                continue
        else:
            segments = youtube.load_cached_transcript(episode["id"])
        for segment in segments:
            if needle in str(segment.text).lower():
                results.append(
                    {
                        "episode_id": episode["id"],
                        "episode_title": episode.get("title") or episode["id"],
                        "start": round(float(segment.start), 2),
                        "end": round(float(segment.end), 2),
                        "text": segment.text,
                    }
                )
                if len(results) >= max(1, int(limit)):
                    return {"query": q.strip(), "results": results}
    return {"query": q.strip(), "results": results}


# --------------------------------------------------------------------------
# Ingest: playlists AND single videos
# --------------------------------------------------------------------------
def ingest_url(
    store: Store,
    pipeline,
    url: str,
    limit: int = config.EPISODE_PAGE_SIZE,
    youtube_ok: bool = False,
) -> dict:
    """Load a playlist or a single video; queue nothing unless auto-pilot is on.

    ``youtube_ok`` is the ONE reachability verdict the caller made for this
    request — the ingest itself never re-probes.
    """
    if not youtube_ok:
        raise ServiceError(
            503,
            "YouTube is not reachable from this machine — "
            "use Demo mode to try the pipeline on synthetic media, "
            "or run AutoShorts where YouTube is accessible.",
        )
    playlist = youtube.is_playlist_url(url)
    try:
        if playlist:
            entries = youtube.list_playlist(url, limit=limit)
        else:
            entries = [youtube.fetch_video_meta(url)]
    except RuntimeError as exc:
        raise ServiceError(502, str(exc)) from exc

    for entry in entries:
        store.upsert_episode(
            {
                **entry,
                "source": "youtube",
                "status": "new",
                "clips": [],
                "added_at": time.time(),
            }
        )
    store.update_settings(playlist_url=url, last_loaded=time.time())

    # Auto-pilot: queue every freshly added episode with default options.
    auto_queued = 0
    if store.settings().get("autopilot"):
        params = default_job_params()
        for entry in entries:
            episode = store.get_episode(entry["id"])
            if not episode or episode.get("status") == "processing":
                continue
            pipeline.start_job(entry["id"], dict(params))
            auto_queued += 1
    return {
        "kind": "playlist" if playlist else "video",
        "added": len(entries),
        "total_episodes": len(store.episodes()),
        "auto_queued": auto_queued,
    }


# --------------------------------------------------------------------------
# Clips: re-render + polish + rename
# --------------------------------------------------------------------------
def rerender_clip(
    store: Store, pipeline, clip_id: str, overrides: dict | None = None
) -> dict:
    """Queue a fresh manual render of a clip's stored range and options."""
    overrides = overrides if isinstance(overrides, dict) else {}
    clip = store.get_clip(clip_id)
    if not clip:
        raise ServiceError(404, "Clip not found")
    episode = store.get_episode(clip["episode_id"])
    if not episode:
        raise ServiceError(404, "Episode not found")
    if episode.get("status") == "processing":
        raise ServiceError(409, "Already processing this episode")

    opts = rerender_params_from(overrides, base=clip.get("render"))
    params = {
        "kind": "manual",
        "start": float(clip.get("start") or 0.0),
        "end": float(clip.get("end") or 0.0),
        "title": (
            overrides.get("title")
            or clip.get("title")
            or "Re-render"
        ),
        **opts,
    }
    job = pipeline.start_job(clip["episode_id"], params)
    return {"job_id": job["id"], "clip_id": clip_id, "options": opts}


def rename_clip(store: Store, clip_id: str, body: dict) -> dict:
    """Rename a clip: trimmed title, at most 120 characters."""
    clip = store.get_clip(clip_id)
    if not clip:
        raise ServiceError(404, "Clip not found")
    body = body if isinstance(body, dict) else {}
    if "title" not in body:
        raise ServiceError(422, "title is required")
    title = body["title"]
    if not isinstance(title, str):
        raise ServiceError(422, "title must be a string")
    title = title.strip()
    if not title:
        raise ServiceError(422, "title must not be blank")
    if len(title) > 120:
        raise ServiceError(422, "title must be at most 120 characters")
    stored = store.update_clip(clip_id, title=title)
    return {"id": clip_id, "title": (stored or {}).get("title", title)}


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
    store: Store,
    pipeline,
    params: dict,
    youtube_ok: bool,
    urls: list[str] | None = None,
    episodes: list[str] | None = None,
) -> dict:
    """Queue automatic clips.

    ``urls`` ingest-then-queue each playlist/video; ``episodes`` queues the
    named episode ids; with neither, every episode that is new or errored is
    queued. ``youtube_ok`` is the ONE reachability probe for the whole call —
    this function never re-probes.
    """
    queued: list[dict] = []
    skipped: list[dict] = []
    added = 0

    if urls:
        for url in urls:
            if not isinstance(url, str) or len(url.strip()) < 8:
                skipped.append({"url": str(url), "reason": "invalid url"})
                continue
            try:
                result = ingest_url(
                    store, pipeline, url.strip(),
                    limit=config.EPISODE_PAGE_SIZE, youtube_ok=youtube_ok,
                )
            except ServiceError as exc:
                skipped.append({"url": url.strip(), "reason": exc.detail})
                continue
            added += result.get("added", 0)
            for entry in store.episodes():
                if entry.get("status") != "new" and entry.get("status") != "error":
                    continue
                if any(q["episode_id"] == entry["id"] for q in queued):
                    continue
                job = pipeline.start_job(entry["id"], dict(params))
                queued.append(
                    {"episode_id": entry["id"], "job_id": job["id"]}
                )
    else:
        if episodes:
            targets = []
            for ep_id in episodes:
                episode = store.get_episode(str(ep_id))
                if not episode:
                    skipped.append({"id": str(ep_id), "reason": "Episode not found"})
                    continue
                targets.append(episode)
        else:
            targets = [
                episode for episode in store.episodes()
                if str(episode.get("status") or "new") in ("new", "error")
            ]
        for episode in targets:
            if episode.get("status") == "processing":
                continue
            if not demo.is_demo(episode) and not youtube_ok:
                skipped.append(
                    {"id": episode["id"], "reason": "YouTube unreachable"}
                )
                continue
            job = pipeline.start_job(episode["id"], dict(params))
            queued.append({"episode_id": episode["id"], "job_id": job["id"]})

    result = {
        "queued": len(queued),
        "skipped": len(skipped),
        "jobs": queued,
        "skipped_episodes": skipped,
    }
    if urls:
        result["added"] = added
    return result


def retry_job(store: Store, pipeline, job_id: str) -> dict:
    """Re-queue a finished job with exactly the same episode and params."""
    job = store.job(job_id)
    if not job:
        raise ServiceError(404, "Job not found")
    if job.get("status") in ("queued", "running"):
        raise ServiceError(409, "Job is still running")
    if not store.get_episode(job["episode_id"]):
        raise ServiceError(404, "Episode not found")
    if store.get_episode(job["episode_id"]).get("status") == "processing":
        raise ServiceError(409, "Already processing this episode")
    new_job = pipeline.start_job(job["episode_id"], dict(job.get("params") or {}))
    return {"job_id": new_job["id"], "retried_from": job_id}


def cancel_job(store: Store, job_id: str) -> dict:
    """Cancel a queued job; the worker will skip it without running it."""
    job = store.job(job_id)
    if not job:
        raise ServiceError(404, "Job not found")
    if job.get("status") != "queued":
        raise ServiceError(
            409, f"Only queued jobs can be cancelled (this one is {job.get('status')})"
        )
    store.cancel_job(job_id)
    reset_episode_if_idle(store, job.get("episode_id"))
    return {"cancelled": job_id}


def reset_episode_if_idle(store: Store, ep_id: str | None) -> None:
    """Reset an episode to ``new`` once no active jobs remain for it."""
    if not ep_id:
        return
    episode = store.get_episode(ep_id)
    if not episode:
        return
    if episode.get("status") == "processing" and not store.has_active_jobs(ep_id):
        store.update_episode(ep_id, status="new", error=None)


# --------------------------------------------------------------------------
# Episode deletion
# --------------------------------------------------------------------------
def delete_episode(store: Store, ep_id: str) -> dict:
    """Delete an episode, its clips and their media files."""
    episode = _episode_or_404(store, ep_id)
    if episode.get("status") == "processing":
        raise ServiceError(409, "Already processing this episode")
    for clip in store.clips(ep_id):
        if clip.get("file"):
            (config.CLIPS_DIR / clip["file"]).unlink(missing_ok=True)
        if clip.get("thumb"):
            (config.THUMBS_DIR / clip["thumb"]).unlink(missing_ok=True)
    removed = store.delete_episode(ep_id)
    return {"deleted": ep_id, "clips_removed": removed}


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
def update_settings(store: Store, body: dict) -> dict:
    return {"settings": store.update_settings(**settings_update_from(body))}


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
        usage = _shutil_disk_usage(path or config.DATA_DIR)
    except OSError:
        return {"free": 0, "total": 0, "used": 0}
    return {"free": usage.free, "total": usage.total, "used": usage.used}


def _shutil_disk_usage(path: Path):
    import shutil

    return shutil.disk_usage(path)


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
    """Free space for one storage area without breaking the library.

    Every file's size is measured BEFORE the unlink so ``freed_bytes`` always
    equals the real number of bytes deleted.
    """
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
            size = path.stat().st_size  # measure BEFORE deleting (rule 5)
            path.unlink()
        except OSError:
            return
        removed_files += 1
        freed += size

    if target == "subs":
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
        "removed": removed_files,
        "removed_files": removed_files,
        "freed_bytes": int(freed),
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
    """yt-dlp version straight from ``yt_dlp.version`` — never CLI text."""
    try:
        from yt_dlp.version import __version__  # type: ignore

        return str(__version__)
    except Exception:
        return "missing"
