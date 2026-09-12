"""FastAPI app: JSON API + static web UI + clip file serving.

A byte-for-byte mirror of ``server_stdlib.py`` — every request body is
validated by the same maintenance helpers, so both servers return identical
status codes and ``{"detail": ...}`` messages. (Termux cannot install
FastAPI's compiled wheels, which is why the stdlib twin exists.)
"""
from __future__ import annotations

import tempfile
import time
import zipfile
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.exceptions import RequestValidationError

from . import (__version__, config, demo, engine, highlights, llm, maintenance,
               youtube)
from .maintenance import ServiceError
from .pipeline import Pipeline
from .store import Store
from .transcripts import Segment, to_sentences

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(
    title=f"{config.APP_NAME} API",
    version=__version__,
    description=(
        f"{config.APP_NAME} — turn long podcast episodes into captioned "
        "vertical shorts. Same JSON contract as the stdlib server."
    ),
)
store = Store()
pipeline = Pipeline(store)

_reachable_cache: tuple[float, bool] = (0.0, False)


def _fail(exc: ServiceError) -> HTTPException:
    """Translate a shared service error into the HTTP error shape."""
    return HTTPException(status_code=exc.status, detail=exc.detail)


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, exc: RequestValidationError):
    """Match the stdlib server's plain-string detail for bad bodies."""
    return JSONResponse(
        status_code=422,
        content={"detail": "Invalid JSON body"},
    )


@app.exception_handler(StarletteHTTPException)
async def _http_error(_: Request, exc: StarletteHTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


def youtube_reachable(ttl: float = 60.0) -> bool:
    global _reachable_cache
    now = time.time()
    if now - _reachable_cache[0] > ttl:
        _reachable_cache = (now, youtube.check_reachable())
    return _reachable_cache[1]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _episode_segments(ep: dict) -> tuple[list[Segment], str]:
    if demo.is_demo(ep):
        return demo.demo_segments(ep["id"]), "demo"
    return youtube.get_transcript(ep["id"], ep["url"])


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@app.post("/api/fonts/install")
def fonts_install(body: dict | None = None):
    """Install the bundled Devanagari fonts (Hindi captions need them)."""
    return maintenance.fonts_install(bool((body or {}).get("force")))


@app.get("/api/health")
def health():
    disk = maintenance.disk_usage()
    return {
        "app": config.APP_NAME,
        "brand": config.APP_NAME,
        "version": __version__,
        "youtube_reachable": youtube_reachable(),
        "demo_available": True,
        "ffmpeg": config.FFMPEG_BIN,
        "versions": maintenance.versions_info(),
        "disk_free": disk["free"],
        "disk_total": disk["total"],
        "llm_available": bool(engine.available(store.settings()) or llm.available()),
        "engine": engine.public_state(store.settings()),
        "options": maintenance.render_options_catalog(),
    }


@app.get("/api/state")
def state():
    episodes = store.episodes()
    clips = maintenance.mark_clip_sources(store.clips(), episodes)
    jobs = {job["id"]: job for job in store.active_jobs()}
    by_episode: dict[str, list] = {}
    for clip in clips:
        by_episode.setdefault(clip["episode_id"], []).append(clip)
    for episode in episodes:
        episode["clip_count"] = len(by_episode.get(episode["id"], []))

    clip_seconds = sum(float(clip.get("duration") or 0) for clip in clips)
    avg_score = (
        sum(float(clip.get("score") or 0) for clip in clips) / len(clips)
        if clips else 0.0
    )
    return {
        "settings": maintenance.public_settings(store),
        "episodes": episodes,
        "clips": clips,
        "jobs": list(jobs.values()),
        "stats": {
            "episodes": len(episodes),
            "clips": len(clips),
            "clip_seconds": clip_seconds,
            "avg_score": avg_score,
        },
        "defaults": {
            "playlist_url": config.DEFAULT_PLAYLIST,
            "count": config.DEFAULT_CLIP_COUNT,
            "min_dur": config.MIN_CLIP_SECONDS,
            "max_dur": config.MAX_CLIP_SECONDS,
            "profile": "viral",
            **maintenance.RENDER_DEFAULTS,
        },
        "engine": maintenance.engine_settings(store),
        "audio_tracks": maintenance.audio_tracks()["tracks"],
    }


@app.post("/api/playlist")
def load_url(payload: dict = Body(...)):
    """Accepts playlists AND single videos (one reachability probe)."""
    url = payload.get("url", "")
    if not isinstance(url, str) or len(url) < 8:
        raise HTTPException(status_code=422, detail="url must be at least 8 characters")
    limit = payload.get("limit", config.EPISODE_PAGE_SIZE)
    if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 100):
        raise HTTPException(
            status_code=422, detail="limit must be an integer between 1 and 100"
        )
    try:
        return maintenance.ingest_url(
            store, pipeline, url, limit, youtube_reachable()
        )
    except ServiceError as exc:
        raise _fail(exc) from exc


@app.post("/api/demo/load")
def load_demo():
    for episode in demo.DEMO_EPISODES:
        store.upsert_episode(
            {
                "id": episode["id"],
                "title": episode["title"],
                "url": episode["url"],
                "duration": episode["duration"],
                "source": "demo",
                "status": "new",
                "clips": [],
                "added_at": time.time(),
            }
        )
    store.update_settings(last_loaded=time.time())
    return {"added": len(demo.DEMO_EPISODES)}


def _get_episode_or_404(ep_id: str) -> dict:
    episode = store.get_episode(ep_id)
    if not episode:
        raise HTTPException(status_code=404, detail="Episode not found")
    return episode


def _guard_ready(episode: dict) -> None:
    if episode.get("status") == "processing":
        raise HTTPException(status_code=409, detail="Already processing this episode")
    if episode.get("source") != "demo" and not youtube_reachable():
        raise HTTPException(
            status_code=503,
            detail="YouTube is not reachable — demo mode still works.",
        )


@app.post("/api/episodes/{ep_id}/shorts")
def generate_shorts(ep_id: str, payload: dict = Body(...)):
    try:
        params = maintenance.shorts_params_from(payload)
    except ServiceError as exc:
        raise _fail(exc) from exc
    episode = _get_episode_or_404(ep_id)
    _guard_ready(episode)
    job = pipeline.start_job(ep_id, params)
    return {"job_id": job["id"]}


@app.post("/api/episodes/{ep_id}/preview")
def preview_highlights(ep_id: str, payload: dict = Body(...)):
    try:
        params = maintenance.preview_params_from(payload)
    except ServiceError as exc:
        raise _fail(exc) from exc
    episode = _get_episode_or_404(ep_id)
    try:
        segments, _source = _episode_segments(episode)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    sentences = to_sentences(segments)
    moments = highlights.find_highlights(
        sentences,
        count=params["count"],
        min_dur=params["min_dur"],
        max_dur=params["max_dur"],
        profile=params["profile"],
    )
    return {
        "moments": [
            {
                "start": moment.start,
                "end": moment.end,
                "duration": round(moment.duration, 2),
                "title": moment.title,
                "score": moment.score,
                "reasons": moment.reasons,
                "breakdown": moment.signals,
                "signals": moment.signals,
            }
            for moment in moments
        ],
        "stats": highlights.transcript_stats(sentences),
    }


@app.post("/api/episodes/{ep_id}/manual")
def generate_manual(ep_id: str, payload: dict = Body(...)):
    try:
        params = maintenance.manual_params_from(payload)
    except ServiceError as exc:
        raise _fail(exc) from exc
    episode = _get_episode_or_404(ep_id)
    _guard_ready(episode)
    job = pipeline.start_job(ep_id, params)
    return {"job_id": job["id"]}


@app.post("/api/audio")
def audio_upload(payload: dict = Body(...)):
    """Upload a music bed: ``{name, data_b64}`` -> ``{track_id}`` (T3)."""
    try:
        return maintenance.audio_upload(store, payload)
    except ServiceError as exc:
        raise _fail(exc) from exc


@app.get("/api/audio")
def audio_list():
    """Uploaded tracks a render may use as ``audio_track``."""
    return maintenance.audio_tracks()


@app.get("/api/audio/{track_id}/file")
def audio_file(track_id: str):
    """Stream one stored bed back to the UI's preview player."""
    try:
        path = maintenance.audio_track_file(track_id)
    except ServiceError as exc:
        raise _fail(exc) from exc
    return FileResponse(path, media_type="audio/mpeg")


@app.delete("/api/audio/{track_id}")
def audio_delete(track_id: str):
    """Forget an uploaded bed (T3)."""
    try:
        return maintenance.audio_delete(track_id)
    except ServiceError as exc:
        raise _fail(exc) from exc


@app.post("/api/titles")
def titles_lab(payload: dict = Body(...)):
    """Title Lab — title variations + hashtags through the free engine (T6)."""
    try:
        return maintenance.title_lab(store, payload)
    except ServiceError as exc:
        raise _fail(exc) from exc


@app.get("/api/episodes/{ep_id}/beats")
def episode_beats(ep_id: str, refresh: str = "0"):
    """Offline beat markers for an episode's media (T3 Beat Sync)."""
    try:
        return maintenance.episode_beats(
            store, ep_id, refresh in ("1", "true", "yes")
        )
    except ServiceError as exc:
        raise _fail(exc) from exc


@app.get("/api/episodes/{ep_id}/audio.mp3")
def episode_audio(ep_id: str):
    """Extract an episode's media audio as MP3 (T6 Audio Extract)."""
    try:
        path, name = maintenance.extract_mp3(store, ep_id)
    except ServiceError as exc:
        raise _fail(exc) from exc
    return FileResponse(
        path, media_type="audio/mpeg", filename=name,
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@app.get("/api/episodes/{ep_id}/transcript")
def episode_transcript(ep_id: str):
    episode = _get_episode_or_404(ep_id)
    try:
        segments, source = _episode_segments(episode)
        return {"source": source, "segments": [
            {"start": float(segment.start),
             "end": float(segment.end),
             "text": segment.text}
            for segment in segments
        ]}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/episodes/{ep_id}/chapters")
def episode_chapters(ep_id: str):
    try:
        return maintenance.episode_chapters(store, ep_id)
    except ServiceError as exc:
        raise _fail(exc) from exc


@app.get("/api/episodes/{ep_id}/export")
def episode_export(ep_id: str, count: str = "5", profile: str = "viral"):
    try:
        parsed = int(count)
    except (TypeError, ValueError):
        parsed = None
    if parsed is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"count must be an integer between {maintenance.COUNT_RANGE[0]} "
                f"and {maintenance.COUNT_RANGE[1]}"
            ),
        )
    try:
        filename, text = maintenance.export_csv(store, ep_id, parsed, profile)
    except ServiceError as exc:
        raise _fail(exc) from exc
    return Response(
        content=text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.delete("/api/episodes/{ep_id}")
def delete_episode(ep_id: str):
    try:
        return maintenance.delete_episode(store, ep_id)
    except ServiceError as exc:
        raise _fail(exc) from exc


@app.get("/api/clips/zip")
def clips_zip(episode_id: str | None = None):
    clips = store.clips(episode_id=episode_id)
    files = [
        (clip, config.CLIPS_DIR / clip["file"])
        for clip in clips
        if (config.CLIPS_DIR / clip["file"]).is_file()
    ]
    if not files:
        raise HTTPException(status_code=404, detail="No clips available to download")

    stamp = int(time.time())
    suffix = f"-{episode_id}" if episode_id else ""
    download_name = f"qyro{suffix}-{stamp}.zip"
    with tempfile.NamedTemporaryFile(
        prefix="autoshorts-", suffix=".zip", dir=config.DATA_DIR, delete=False
    ) as temporary:
        zip_path = Path(temporary.name)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as archive:
        for clip, path in files:
            archive.write(path, arcname=f"qyro-{clip['id']}.mp4")

    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=download_name,
        background=BackgroundTask(zip_path.unlink, missing_ok=True),
    )


@app.delete("/api/clips/{clip_id}")
def delete_clip(clip_id: str):
    clip = store.get_clip(clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    (config.CLIPS_DIR / clip["file"]).unlink(missing_ok=True)
    if clip.get("thumb"):
        (config.THUMBS_DIR / clip["thumb"]).unlink(missing_ok=True)
    store.delete_clip(clip_id)
    return {"deleted": clip_id}


@app.get("/api/clips/{clip_id}/file")
def clip_file(clip_id: str, dl: int = 0):
    """Stream a rendered short.

    ``?dl=1`` asks for it as an attachment (the card's download button): a
    ``download`` attribute on the link is ignored by Android WebViews, so
    without a real ``Content-Disposition`` a tap could open a player instead of
    saving the file. Plain requests stay inline for the <video> element.
    """
    clip = store.get_clip(clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    path = config.CLIPS_DIR / clip["file"]
    if not path.exists():
        raise HTTPException(status_code=410, detail="Clip file missing on disk")
    name = f"qyro-{clip_id}.mp4"
    if dl:
        return FileResponse(
            path, media_type="video/mp4", filename=name,
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )
    # Starlette's default disposition for a named file is *attachment*, which
    # is right for a download and wrong for the <video> element that reads this
    # same URL: inline matches what the stdlib server answers (v0.6.6).
    return FileResponse(
        path, media_type="video/mp4", filename=name,
        content_disposition_type="inline",
    )


@app.get("/api/clips/{clip_id}/thumb")
def clip_thumb(clip_id: str, index: int | None = None):
    clip = store.get_clip(clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="Clip not found")
    if index is not None:
        # v0.5.0 thumb picker: ?index=N serves a candidate frame
        if index < 0 or index >= config.THUMB_CANDIDATES[1]:
            raise HTTPException(
                status_code=422,
                detail=(
                    "index must be between 0 and "
                    f"{config.THUMB_CANDIDATES[1] - 1}"
                ),
            )
        path = config.THUMBS_DIR / f"{clip_id}-cand-{index}.jpg"
        if not path.is_file():
            raise HTTPException(
                status_code=404, detail="No candidate at that index"
            )
        return FileResponse(path, media_type="image/jpeg")
    if not clip.get("thumb"):
        raise HTTPException(status_code=404, detail="No thumbnail")
    path = config.THUMBS_DIR / clip["thumb"]
    if not path.exists():
        raise HTTPException(status_code=410, detail="Thumbnail missing")
    return FileResponse(path, media_type="image/jpeg")


@app.get("/api/clips/{clip_id}/probe")
def clip_probe(clip_id: str):
    """Clip Inspector — real resolution/fps/codec/duration/size (T6)."""
    try:
        return maintenance.clip_probe(store, clip_id)
    except ServiceError as exc:
        raise _fail(exc) from exc


@app.get("/api/clips/{clip_id}/audio.mp3")
def clip_audio(clip_id: str):
    """Audio Extract — the clip's soundtrack as a downloadable MP3 (T6)."""
    try:
        path, name = maintenance.extract_mp3(store, clip_id)
    except ServiceError as exc:
        raise _fail(exc) from exc
    return FileResponse(
        path, media_type="audio/mpeg", filename=name,
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@app.get("/api/clips/{clip_id}/thumb-candidates")
def clip_thumb_candidates(clip_id: str, n: str = "6"):
    """Evenly spread frames to choose a poster from (T6 Thumbnail Picker)."""
    try:
        return maintenance.thumb_candidates(store, clip_id, n)
    except ServiceError as exc:
        raise _fail(exc) from exc


@app.post("/api/clips/{clip_id}/thumb-pick")
def clip_thumb_pick(clip_id: str, payload: dict = Body(...)):
    """Make one candidate the clip's thumbnail (T6)."""
    try:
        return maintenance.thumb_pick(store, clip_id, payload)
    except ServiceError as exc:
        raise _fail(exc) from exc


@app.get("/api/clips/{clip_id}/srt")
def clip_srt_file(clip_id: str):
    try:
        path = maintenance.clip_srt(store, clip_id)
    except ServiceError as exc:
        raise _fail(exc) from exc
    return FileResponse(
        path, media_type="application/x-subrip", filename=f"{clip_id}.srt"
    )


@app.post("/api/clips/{clip_id}/rename")
def rename_clip(clip_id: str, payload: dict = Body(...)):
    try:
        return maintenance.rename_clip(store, clip_id, payload)
    except ServiceError as exc:
        raise _fail(exc) from exc


@app.post("/api/clips/{clip_id}/rerender")
def rerender_clip(clip_id: str, payload: dict = Body(...)):
    try:
        return maintenance.rerender_clip(store, pipeline, clip_id, payload)
    except ServiceError as exc:
        raise _fail(exc) from exc


@app.post("/api/clips/{clip_id}/polish")
def polish_clip(clip_id: str):
    try:
        return maintenance.polish_clip(store, clip_id)
    except ServiceError as exc:
        raise _fail(exc) from exc


# ---------------------------------------------------------------------------
# Settings, search, batch, jobs
# ---------------------------------------------------------------------------
@app.post("/api/settings")
def update_settings(payload: dict = Body(...)):
    try:
        return maintenance.update_settings(store, payload)
    except ServiceError as exc:
        raise _fail(exc) from exc


@app.get("/api/search")
def search(q: str = ""):
    try:
        return maintenance.search_transcripts(store, q)
    except ServiceError as exc:
        raise _fail(exc) from exc


@app.post("/api/batch")
def batch_jobs(payload: dict = Body(...)):
    payload = payload if isinstance(payload, dict) else {}
    try:
        maintenance.check_unknown_keys(payload, maintenance.BATCH_KEYS)
        selector_body = {
            key: value for key, value in payload.items()
            if key not in ("urls", "episodes")
        }
        if selector_body:
            params = maintenance.shorts_params_from(selector_body)
        else:
            params = maintenance.default_job_params()
    except ServiceError as exc:
        raise _fail(exc) from exc
    urls = payload.get("urls")
    episodes = payload.get("episodes")
    if urls is not None and (
        not isinstance(urls, list) or any(not isinstance(u, str) for u in urls)
    ):
        raise HTTPException(status_code=422, detail="urls must be a list of strings")
    if episodes is not None and (
        not isinstance(episodes, list)
        or any(not isinstance(e, str) for e in episodes)
    ):
        raise HTTPException(
            status_code=422, detail="episodes must be a list of strings"
        )
    return maintenance.batch_queue(
        store, pipeline, params, youtube_reachable(), urls=urls, episodes=episodes
    )


@app.get("/api/jobs")
def list_jobs(limit: str = "20"):
    try:
        parsed = int(limit)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="limit must be an integer")
    parsed = max(1, min(parsed, 200))
    return {"jobs": store.recent_jobs(parsed)}


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str):
    try:
        return maintenance.retry_job(store, pipeline, job_id)
    except ServiceError as exc:
        raise _fail(exc) from exc


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    try:
        return maintenance.cancel_job(store, job_id)
    except ServiceError as exc:
        raise _fail(exc) from exc


# ---------------------------------------------------------------------------
# Storage + backup
# ---------------------------------------------------------------------------
@app.get("/api/storage")
def storage():
    return maintenance.storage_info(store)


@app.post("/api/storage/clean")
def storage_clean(payload: dict = Body(...)):
    target = payload.get("target")
    if not isinstance(target, str) or target not in maintenance.CLEAN_TARGETS:
        raise HTTPException(
            status_code=422,
            detail="target must be one of: " + ", ".join(maintenance.CLEAN_TARGETS),
        )
    try:
        return maintenance.clean_storage(store, target)
    except ServiceError as exc:
        raise _fail(exc) from exc


@app.get("/api/backup")
def backup():
    with tempfile.NamedTemporaryFile(
        prefix="autoshorts-backup-", suffix=".json",
        dir=config.DATA_DIR, delete=False,
    ) as temporary:
        backup_path = Path(temporary.name)
        temporary.write(maintenance.backup_bytes(store))
    return FileResponse(
        backup_path,
        media_type="application/json",
        filename=f"qyro-state-{int(time.time())}.json",
        background=BackgroundTask(backup_path.unlink, missing_ok=True),
    )


@app.post("/api/restore")
def restore(payload: dict = Body(...)):
    try:
        return maintenance.restore_state(store, payload)
    except ServiceError as exc:
        raise _fail(exc) from exc


# ---------------------------------------------------------------------------
# Static web UI
# ---------------------------------------------------------------------------
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.get("/sw.js")
def service_worker():
    """Served from the origin root so the worker can control "/". """
    path = WEB_DIR / "sw.js"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="No service worker")
    return FileResponse(path, media_type="text/javascript; charset=utf-8")


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")
