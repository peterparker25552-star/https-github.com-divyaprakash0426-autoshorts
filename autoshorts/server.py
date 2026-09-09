"""FastAPI app: JSON API + static web UI + clip file serving."""
from __future__ import annotations

import tempfile
import time
import zipfile
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from . import __version__, config, demo, highlights, youtube
from .pipeline import Pipeline
from .store import Store
from .transcripts import Segment, to_sentences

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
_PROFILE_PATTERN = "^(viral|story|facts|energy)$"
_STYLE_PATTERN = "^(crop|blur)$"
_QUALITY_PATTERN = "^(fast|full)$"

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


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class PlaylistReq(BaseModel):
    url: str = Field(min_length=8)
    limit: int = Field(default=config.EPISODE_PAGE_SIZE, ge=1, le=100)


class ShortsReq(BaseModel):
    count: int = Field(default=config.DEFAULT_CLIP_COUNT, ge=1, le=12)
    min_dur: float = Field(default=config.MIN_CLIP_SECONDS, ge=8, le=120)
    max_dur: float = Field(default=config.MAX_CLIP_SECONDS, ge=10, le=180)
    profile: str = Field(default="viral", pattern=_PROFILE_PATTERN)
    style: str = Field(default=config.DEFAULT_STYLE, pattern=_STYLE_PATTERN)
    quality: str = Field(default=config.DEFAULT_QUALITY, pattern=_QUALITY_PATTERN)


class PreviewReq(BaseModel):
    count: int = Field(default=config.DEFAULT_CLIP_COUNT, ge=1, le=12)
    min_dur: float = Field(default=config.MIN_CLIP_SECONDS, ge=5, le=180)
    max_dur: float = Field(default=config.MAX_CLIP_SECONDS, ge=5, le=180)
    profile: str = Field(default="viral", pattern=_PROFILE_PATTERN)


class ManualReq(BaseModel):
    start: float
    end: float
    title: str = Field(default="", max_length=120)
    style: str = Field(default=config.DEFAULT_STYLE, pattern=_STYLE_PATTERN)
    quality: str = Field(default=config.DEFAULT_QUALITY, pattern=_QUALITY_PATTERN)


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
@app.get("/api/health")
def health():
    return {
        "app": "autoshorts",
        "version": __version__,
        "youtube_reachable": youtube_reachable(),
        "demo_available": True,
        "ffmpeg": config.FFMPEG_BIN,
    }


@app.get("/api/state")
def state():
    episodes = store.episodes()
    clips = store.clips()
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
        "settings": store.settings(),
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
            "style": config.DEFAULT_STYLE,
            "quality": config.DEFAULT_QUALITY,
        },
    }


@app.post("/api/playlist")
def load_playlist(req: PlaylistReq):
    if not youtube_reachable():
        raise HTTPException(
            status_code=503,
            detail=(
                "YouTube is not reachable from this machine — "
                "use Demo mode to try the pipeline on synthetic media, "
                "or run AutoShorts where YouTube is accessible."
            ),
        )
    try:
        entries = youtube.list_playlist(req.url, limit=req.limit)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
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
    store.update_settings(
        playlist_url=req.url,
        last_loaded=time.time(),
    )
    return {"added": len(entries), "total_episodes": len(store.episodes())}


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


@app.post("/api/episodes/{ep_id}/shorts")
def generate_shorts(ep_id: str, req: ShortsReq):
    episode = store.get_episode(ep_id)
    if not episode:
        raise HTTPException(status_code=404, detail="Episode not found")
    if episode.get("status") == "processing":
        raise HTTPException(status_code=409, detail="Already processing this episode")
    if episode.get("source") != "demo" and not youtube_reachable():
        raise HTTPException(
            status_code=503,
            detail="YouTube is not reachable — demo mode still works.",
        )
    job = pipeline.start_job(ep_id, req.model_dump())
    return {"job_id": job["id"]}


@app.post("/api/episodes/{ep_id}/preview")
def preview_highlights(ep_id: str, req: PreviewReq):
    episode = store.get_episode(ep_id)
    if not episode:
        raise HTTPException(status_code=404, detail="Episode not found")
    if req.max_dur < req.min_dur:
        raise HTTPException(status_code=422, detail="max_dur must be >= min_dur")
    try:
        segments, _source = _episode_segments(episode)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    moments = highlights.find_highlights(
        to_sentences(segments),
        count=req.count,
        min_dur=req.min_dur,
        max_dur=req.max_dur,
        profile=req.profile,
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
            }
            for moment in moments
        ]
    }


@app.post("/api/episodes/{ep_id}/manual")
def generate_manual(ep_id: str, req: ManualReq):
    episode = store.get_episode(ep_id)
    if not episode:
        raise HTTPException(status_code=404, detail="Episode not found")
    if req.end - req.start < 5:
        raise HTTPException(status_code=422, detail="Manual clips must be at least 5 seconds")
    if episode.get("status") == "processing":
        raise HTTPException(status_code=409, detail="Already processing this episode")
    if episode.get("source") != "demo" and not youtube_reachable():
        raise HTTPException(
            status_code=503,
            detail="YouTube is not reachable — demo mode still works.",
        )
    job = pipeline.start_job(ep_id, {**req.model_dump(), "kind": "manual"})
    return {"job_id": job["id"]}


@app.get("/api/episodes/{ep_id}/transcript")
def episode_transcript(ep_id: str):
    episode = store.get_episode(ep_id)
    if not episode:
        raise HTTPException(status_code=404, detail="Episode not found")
    try:
        segments, source = _episode_segments(episode)
        return {"source": source, "segments": [segment.__dict__ for segment in segments]}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


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


# ---------------------------------------------------------------------------
# Static web UI
# ---------------------------------------------------------------------------
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")
