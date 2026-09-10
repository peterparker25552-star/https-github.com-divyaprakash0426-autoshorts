"""FastAPI app: JSON API + static web UI + clip file serving.

Mirrors ``server_stdlib.py`` exactly (same routes, status codes and the
``{"detail": ...}`` error shape). Validation is shared via
``autoshorts.validate`` + ``autoshorts.service`` so both servers behave
identically — request bodies are accepted as raw dicts and validated with
the same helpers (including boolean-speed rejection).
"""
from __future__ import annotations

import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from . import __version__, config, demo, highlights, youtube
from .pipeline import Pipeline
from .service import (
    build_moments_csv,
    clean_storage,
    episode_segments,
    get_chapters,
    get_clip_srt_text,
    search_transcripts,
    storage_info,
)
from .store import Store
from .transcripts import to_sentences
from .validate import (
    ValidationError,
    check_rerender_allowlist,
    need_int,
    normalize_title,
    parse_render_opts,
    parse_selectors,
)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="AutoShorts", version=__version__)
store = Store()
pipeline = Pipeline(store)

_reachable_cache: tuple[float, bool] = (0.0, False)


def youtube_reachable(ttl: float = 60.0) -> bool:
    global _reachable_cache
    now = time.time()
    if now - _reachable_cache[0] > ttl:
        _reachable_cache = (now, youtube.check_reachable())
    return _reachable_cache[1]


@app.exception_handler(RequestValidationError)
async def _validation_handler(_request: Request, exc: RequestValidationError):
    # Normalize FastAPI/pydantic errors to {"detail": message} (422).
    try:
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", []) if p != "body")
        msg = first.get("msg", "Invalid request")
        detail = f"{loc}: {msg}" if loc else str(msg)
    except Exception:
        detail = "Invalid request"
    return JSONResponse(status_code=422, content={"detail": detail})


@app.exception_handler(ValidationError)
async def _shared_validation_handler(_request: Request, exc: ValidationError):
    return JSONResponse(status_code=exc.status, content={"detail": exc.detail})


def _v_error(exc: ValidationError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.detail)


def _need_url(body: dict) -> str:
    v = body.get("url", "")
    if not isinstance(v, str) or len(v) < 8:
        raise HTTPException(status_code=422, detail="url must be at least 8 characters")
    return v


def _default_auto_params() -> dict:
    return {
        "count": config.DEFAULT_CLIP_COUNT,
        "min_dur": float(config.MIN_CLIP_SECONDS),
        "max_dur": float(config.MAX_CLIP_SECONDS),
        "profile": "viral",
        "style": config.DEFAULT_STYLE,
        "quality": config.DEFAULT_QUALITY,
        "format": config.DEFAULT_FORMAT,
        "captions": config.DEFAULT_CAPTIONS,
        "captions_pos": config.DEFAULT_CAPTIONS_POS,
        "captions_box": config.DEFAULT_CAPTIONS_BOX,
        "speed": config.DEFAULT_SPEED,
        "progress": config.DEFAULT_PROGRESS,
        "silence": config.DEFAULT_SILENCE,
        "loud": config.DEFAULT_LOUD,
    }


def _get_episode_or_404(ep_id: str) -> dict:
    episode = store.get_episode(ep_id)
    if not episode:
        raise HTTPException(status_code=404, detail="Episode not found")
    return episode


def _guard_ready(episode: dict) -> None:
    if store.episode_is_processing(episode["id"]):
        raise HTTPException(status_code=409, detail="Already processing this episode")
    if episode.get("source") != "demo" and not youtube_reachable():
        raise HTTPException(status_code=503, detail="YouTube is not reachable — demo mode still works.")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@app.get("/api/health")
def health():
    return {
        "app": "autoshorts",
        "version": __version__,
        "youtube_reachable": youtube_reachable(),
        "demo_available": True,
        "ffmpeg": config.FFMPEG_BIN,
        "ytdlp_version": youtube.ytdlp_version(),
    }


@app.get("/api/state")
def state():
    episodes = store.episodes()
    clips = store.clips()
    jobs = store.jobs()
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
        "settings": store.settings(),
        "episodes": episodes,
        "clips": clips,
        "jobs": jobs,
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
            "style": config.DEFAULT_STYLE,
            "quality": config.DEFAULT_QUALITY,
            "format": config.DEFAULT_FORMAT,
            "captions": config.DEFAULT_CAPTIONS,
            "captions_pos": config.DEFAULT_CAPTIONS_POS,
            "captions_box": config.DEFAULT_CAPTIONS_BOX,
            "speed": config.DEFAULT_SPEED,
            "progress": config.DEFAULT_PROGRESS,
            "silence": config.DEFAULT_SILENCE,
            "loud": config.DEFAULT_LOUD,
        },
    }


@app.post("/api/playlist")
def load_playlist(body: dict[str, Any] = Body(default={})):
    url = _need_url(body)
    try:
        limit = need_int(body, "limit", config.EPISODE_PAGE_SIZE, 1, 100)
    except ValidationError as exc:
        raise _v_error(exc)
    if not youtube_reachable():
        raise HTTPException(
            status_code=503,
            detail=(
                "YouTube is not reachable from this machine — "
                "use Demo mode to try the pipeline on synthetic media, "
                "or run AutoShorts where YouTube is accessible."
            ),
        )
    auto_queued = 0
    autopilot = bool(store.settings().get("autopilot"))
    if youtube.is_playlist_url(url):
        try:
            entries = youtube.list_playlist(url, limit=limit)
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        new_ids: list[str] = []
        for entry in entries:
            existed = store.get_episode(entry["id"]) is not None
            store.upsert_episode(
                {
                    **entry,
                    "source": "youtube",
                    "status": "new",
                    "clips": [],
                    "added_at": time.time(),
                }
            )
            if not existed:
                new_ids.append(entry["id"])
        store.update_settings(playlist_url=url, last_loaded=time.time())
        if autopilot:
            for ep_id in new_ids:
                try:
                    pipeline.start_job(ep_id, _default_auto_params())
                    auto_queued += 1
                except Exception:
                    continue
        return {"kind": "playlist", "added": len(entries),
                "total_episodes": len(store.episodes()), "auto_queued": auto_queued}
    else:
        try:
            info = youtube.get_video_info(url)
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        existed = store.get_episode(info["id"]) is not None
        store.upsert_episode(
            {
                **info,
                "source": "youtube",
                "status": "new",
                "clips": [],
                "added_at": time.time(),
            }
        )
        store.update_settings(playlist_url=url, last_loaded=time.time())
        if autopilot and not existed:
            try:
                pipeline.start_job(info["id"], _default_auto_params())
                auto_queued = 1
            except Exception:
                pass
        return {"kind": "video", "added": 1,
                "total_episodes": len(store.episodes()), "auto_queued": auto_queued}


@app.post("/api/demo/load")
def load_demo():
    for episode in demo.DEMO_EPISODES:
        store.upsert_episode(
            {
                "id": episode["id"],
                "title": episode["title"],
                "url": episode["url"],
                "duration": episode["duration"],
                "hue": episode.get("hue", 0),
                "source": "demo",
                "status": "new",
                "clips": [],
                "added_at": time.time(),
            }
        )
    store.update_settings(last_loaded=time.time())
    return {"added": len(demo.DEMO_EPISODES)}


@app.post("/api/episodes/{ep_id}/shorts")
def generate_shorts(ep_id: str, body: dict[str, Any] = Body(default={})):
    episode = _get_episode_or_404(ep_id)
    _guard_ready(episode)
    try:
        selectors = parse_selectors(body)
        opts = parse_render_opts(body)
    except ValidationError as exc:
        raise _v_error(exc)
    job = pipeline.start_job(ep_id, {**selectors, **opts})
    return {"job_id": job["id"]}


@app.post("/api/episodes/{ep_id}/preview")
def preview_highlights(ep_id: str, body: dict[str, Any] = Body(default={})):
    episode = _get_episode_or_404(ep_id)
    try:
        selectors = parse_selectors(body)
        parse_render_opts(body)
    except ValidationError as exc:
        raise _v_error(exc)
    try:
        segments, _source = episode_segments(episode)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    sentences = to_sentences(segments)
    moments = highlights.find_highlights(
        sentences,
        count=selectors["count"],
        min_dur=selectors["min_dur"],
        max_dur=selectors["max_dur"],
        profile=selectors["profile"],
    )
    return {
        "moments": [m.to_dict() for m in moments],
        "stats": highlights.preview_stats(moments, selectors["profile"], sentences),
    }


@app.post("/api/episodes/{ep_id}/manual")
def generate_manual(ep_id: str, body: dict[str, Any] = Body(default={})):
    episode = _get_episode_or_404(ep_id)
    start = body.get("start")
    end = body.get("end")
    if (
        isinstance(start, bool) or not isinstance(start, (int, float))
        or isinstance(end, bool) or not isinstance(end, (int, float))
    ):
        raise HTTPException(status_code=422, detail="start and end must be numbers")
    if end - start < 5:
        raise HTTPException(status_code=422, detail="Manual clips must be at least 5 seconds")
    if end - start > 180:
        raise HTTPException(status_code=422, detail="Manual clips must be at most 180 seconds")
    title = body.get("title", "")
    if not isinstance(title, str):
        raise HTTPException(status_code=422, detail="title must be a string")
    if len(title.strip()) > 120:
        raise HTTPException(status_code=422, detail="title must be at most 120 characters")
    try:
        opts = parse_render_opts(body)
    except ValidationError as exc:
        raise _v_error(exc)
    _guard_ready(episode)
    job = pipeline.start_job(
        ep_id,
        {
            "start": float(start),
            "end": float(end),
            "title": title.strip() or "Manual clip",
            **opts,
            "kind": "manual",
        },
    )
    return {"kind": "manual", "job_id": job["id"]}


@app.get("/api/episodes/{ep_id}/transcript")
def episode_transcript(ep_id: str):
    episode = _get_episode_or_404(ep_id)
    try:
        segments, source = episode_segments(episode)
        return {"source": source, "segments": [segment.__dict__ for segment in segments]}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/episodes/{ep_id}/chapters")
def episode_chapters(ep_id: str):
    episode = _get_episode_or_404(ep_id)
    try:
        return get_chapters(episode)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/episodes/{ep_id}/export")
def episode_export(ep_id: str, count: str = "8", profile: str = "viral"):
    episode = _get_episode_or_404(ep_id)
    try:
        n = int(float(count)) if str(count).replace(".", "", 1).lstrip("-").isdigit() else int(count)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="count must be an integer between 1 and 12")
    if isinstance(n, bool) or not (1 <= n <= 12):
        raise HTTPException(status_code=422, detail="count must be an integer between 1 and 12")
    if profile not in config.PROFILES:
        raise HTTPException(status_code=422, detail="profile must be one of: viral, story, facts, energy")
    try:
        csv_text = build_moments_csv(episode, n, profile)
    except ValidationError as exc:
        raise _v_error(exc)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="autoshorts-{ep_id}-moments.csv"'},
    )


@app.delete("/api/episodes/{ep_id}")
def delete_episode(ep_id: str):
    episode = store.get_episode(ep_id)
    if not episode:
        raise HTTPException(status_code=404, detail="Episode not found")
    if store.episode_is_processing(ep_id):
        raise HTTPException(status_code=409, detail="Episode is processing")
    clips = store.clips(episode_id=ep_id)
    for clip in clips:
        try:
            (config.CLIPS_DIR / clip["file"]).unlink(missing_ok=True)
        except OSError:
            pass
        if clip.get("thumb"):
            try:
                (config.THUMBS_DIR / clip["thumb"]).unlink(missing_ok=True)
            except OSError:
                pass
        for suffix in (".ass", ".srt"):
            try:
                (config.SUBS_DIR / f"{clip['id']}{suffix}").unlink(missing_ok=True)
            except OSError:
                pass
    _ep, removed = store.delete_episode(ep_id)
    return {"deleted": ep_id, "clips_removed": removed}


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
    download_name = f"autoshorts{suffix}-{stamp}.zip"
    with tempfile.NamedTemporaryFile(
        prefix="autoshorts-", suffix=".zip", dir=config.DATA_DIR, delete=False
    ) as temporary:
        zip_path = Path(temporary.name)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as archive:
        for clip, path in files:
            archive.write(path, arcname=f"autoshort-{clip['id']}.mp4")

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
    for suffix in (".ass", ".srt"):
        (config.SUBS_DIR / f"{clip_id}{suffix}").unlink(missing_ok=True)
    store.delete_clip(clip_id)
    return {"deleted": clip_id}


@app.get("/api/clips/{clip_id}/file")
def clip_file(clip_id: str):
    clip = store.get_clip(clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    path = config.CLIPS_DIR / clip["file"]
    if not path.exists():
        raise HTTPException(status_code=410, detail="Clip file missing on disk")
    return FileResponse(path, media_type="video/mp4", filename=f"autoshort-{clip_id}.mp4")


@app.get("/api/clips/{clip_id}/thumb")
def clip_thumb(clip_id: str):
    clip = store.get_clip(clip_id)
    if not clip or not clip.get("thumb"):
        raise HTTPException(status_code=404, detail="No thumbnail")
    path = config.THUMBS_DIR / clip["thumb"]
    if not path.exists():
        raise HTTPException(status_code=410, detail="Thumbnail missing")
    return FileResponse(path, media_type="image/jpeg")


@app.get("/api/clips/{clip_id}/srt")
def clip_srt(clip_id: str):
    clip = store.get_clip(clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    text = get_clip_srt_text(clip, store)
    if not text:
        raise HTTPException(status_code=404, detail="No captions available for this clip")
    return Response(
        content=text,
        media_type="text/srt; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="autoshort-{clip_id}.srt"'},
    )


@app.post("/api/clips/{clip_id}/rerender")
def clip_rerender(clip_id: str, body: dict[str, Any] = Body(default={})):
    clip = store.get_clip(clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    try:
        check_rerender_allowlist(body)
    except ValidationError as exc:
        raise _v_error(exc)
    ep = store.get_episode(clip["episode_id"])
    if not ep:
        raise HTTPException(status_code=404, detail="Episode not found")
    if store.episode_is_processing(ep["id"]):
        raise HTTPException(status_code=409, detail="Already processing this episode")
    base = dict(clip.get("render") or {})
    if not base:
        base = {
            "style": clip.get("style", config.DEFAULT_STYLE),
            "quality": config.DEFAULT_QUALITY,
            "format": config.DEFAULT_FORMAT,
            "captions": config.DEFAULT_CAPTIONS,
            "captions_pos": config.DEFAULT_CAPTIONS_POS,
            "captions_box": False,
            "speed": 1.0,
            "progress": False,
            "silence": False,
            "loud": False,
        }
    title = body.get("title", clip.get("title", ""))
    try:
        if "title" in body:
            title = normalize_title(body["title"], allow_empty=False)
        merged = {**base, **{k: v for k, v in body.items() if k != "title"}}
        opts = parse_render_opts(merged)
    except ValidationError as exc:
        raise _v_error(exc)
    job = pipeline.start_job(ep["id"], {
        "kind": "rerender",
        "clip_id": clip_id,
        "start": float(clip["start"]),
        "end": float(clip["end"]),
        "title": title,
        "score": float(clip.get("score") or 0),
        "reasons": list(clip.get("reasons") or ["rerender"]),
        "breakdown": dict(clip.get("breakdown") or {}),
        **opts,
    })
    return {"job_id": job["id"]}


@app.post("/api/clips/{clip_id}/polish")
def clip_polish(clip_id: str):
    clip = store.get_clip(clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    from .llm import is_configured, polish_clip

    if not is_configured():
        raise HTTPException(status_code=503, detail="No LLM configured — set OPENAI_API_KEY to enable polish.")
    try:
        pack = polish_clip(clip.get("title", ""), clip.get("episode_title", ""),
                           clip.get("reasons") or [])
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:300]) from exc
    store.update_clip(clip_id, upload_pack=pack)
    return {"id": clip_id, "upload_pack": pack}


@app.post("/api/clips/{clip_id}/rename")
def clip_rename(clip_id: str, body: dict[str, Any] = Body(default={})):
    clip = store.get_clip(clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    if "title" not in body:
        raise HTTPException(status_code=422, detail="title is required")
    try:
        title = normalize_title(body["title"], allow_empty=False)
    except ValidationError as exc:
        raise _v_error(exc)
    store.update_clip(clip_id, title=title)
    return {"id": clip_id, "title": title}


@app.get("/api/jobs")
def jobs_list():
    return {"jobs": store.jobs()}


@app.post("/api/jobs/{job_id}/retry")
def job_retry(job_id: str):
    job = store.job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    ep = store.get_episode(job["episode_id"])
    if not ep:
        raise HTTPException(status_code=404, detail="Episode not found")
    if store.episode_is_processing(ep["id"]):
        raise HTTPException(status_code=409, detail="Already processing this episode")
    params = dict(job.get("params") or {})
    new_job = pipeline.start_job(ep["id"], params)
    return {"job_id": new_job["id"]}


@app.post("/api/jobs/{job_id}/cancel")
def job_cancel(job_id: str):
    job = store.job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.get("status") != "queued":
        raise HTTPException(status_code=409, detail="Only queued jobs can be cancelled")
    store.cancel_job(job_id)
    ep_id = job["episode_id"]
    if not store.active_jobs_for_episode(ep_id):
        ep = store.get_episode(ep_id)
        if ep and ep.get("status") == "processing":
            store.update_episode(ep_id, status="new", error=None)
    return {"cancelled": job_id}


@app.post("/api/settings")
def update_settings(body: dict[str, Any] = Body(default={})):
    allowed = {"playlist_url", "playlist_title", "channel", "autopilot", "last_loaded"}
    updates = {k: v for k, v in body.items() if k in allowed}
    if "autopilot" in updates and not isinstance(updates["autopilot"], bool):
        raise HTTPException(status_code=422, detail="autopilot must be true or false")
    if "playlist_url" in updates and not isinstance(updates["playlist_url"], str):
        raise HTTPException(status_code=422, detail="playlist_url must be a string")
    return store.update_settings(**updates)


@app.post("/api/batch")
def batch(body: dict[str, Any] = Body(default={})):
    urls = body.get("urls", [])
    episodes = body.get("episodes", [])
    if urls is None:
        urls = []
    if episodes is None:
        episodes = []
    if not isinstance(urls, list) or not isinstance(episodes, list):
        raise HTTPException(status_code=422, detail="urls and episodes must be arrays")
    if not urls and not episodes:
        raise HTTPException(status_code=422, detail="Provide urls[] and/or episodes[]")
    try:
        selectors = parse_selectors(body)
        opts = parse_render_opts(body)
    except ValidationError as exc:
        raise _v_error(exc)
    params = {**selectors, **opts}
    # GLOBAL RULE: probe YouTube reachability ONCE total for the whole batch.
    reachable = youtube_reachable()
    queued: list[str] = []
    added = 0
    errors: list[str] = []
    for url in urls:
        if not isinstance(url, str) or len(url) < 8:
            errors.append(f"bad url: {url}")
            continue
        if not reachable:
            errors.append(f"unreachable, skipped: {url}")
            continue
        try:
            if youtube.is_playlist_url(url):
                entries = youtube.list_playlist(url, limit=config.EPISODE_PAGE_SIZE)
                for entry in entries:
                    store.upsert_episode({**entry, "source": "youtube",
                                          "status": "new", "clips": [],
                                          "added_at": time.time()})
                    added += 1
                    if not store.episode_is_processing(entry["id"]):
                        try:
                            j = pipeline.start_job(entry["id"], dict(params))
                            queued.append(j["id"])
                        except Exception as exc:
                            errors.append(f"{entry['id']}: {exc}")
            else:
                info = youtube.get_video_info(url)
                store.upsert_episode({**info, "source": "youtube",
                                      "status": "new", "clips": [],
                                      "added_at": time.time()})
                added += 1
                if not store.episode_is_processing(info["id"]):
                    j = pipeline.start_job(info["id"], dict(params))
                    queued.append(j["id"])
        except RuntimeError as exc:
            errors.append(f"{url}: {exc}")
    for ep_id in episodes:
        ep = store.get_episode(str(ep_id))
        if not ep:
            errors.append(f"unknown episode: {ep_id}")
            continue
        if store.episode_is_processing(ep["id"]):
            errors.append(f"busy, skipped: {ep_id}")
            continue
        if ep.get("source") != "demo" and not reachable:
            errors.append(f"unreachable, skipped: {ep_id}")
            continue
        j = pipeline.start_job(ep["id"], dict(params))
        queued.append(j["id"])
    return {"queued": len(queued), "jobs": queued, "added": added, "errors": errors}


@app.get("/api/storage")
def get_storage():
    return storage_info()


@app.post("/api/storage/clean")
def post_storage_clean(body: dict[str, Any] = Body(default={})):
    target = body.get("target", "")
    try:
        return clean_storage(target)
    except ValidationError as exc:
        raise _v_error(exc)


@app.get("/api/backup")
def get_backup():
    data = store.snapshot()
    return JSONResponse(
        content=data,
        headers={"Content-Disposition": 'attachment; filename="autoshorts-backup.json"'},
    )


@app.post("/api/restore")
def post_restore(body: dict[str, Any] = Body(default={})):
    state = body.get("state", None) if isinstance(body, dict) else None
    if state is None:
        if isinstance(body, dict) and all(k in body for k in ("episodes", "clips", "jobs", "settings")):
            state = body
        else:
            raise HTTPException(status_code=422, detail="body must contain a state object")
    try:
        store.restore(state)
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=422, detail=f"bad state shape: {exc}")
    return {"ok": True}


@app.get("/api/search")
def get_search(q: str = ""):
    try:
        return search_transcripts(store, q)
    except ValidationError as exc:
        raise _v_error(exc)


# ---------------------------------------------------------------------------
# Static web UI
# ---------------------------------------------------------------------------
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")
