"""FastAPI app: JSON API + static web UI + clip file serving."""
from __future__ import annotations

import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, config, demo, youtube
from .pipeline import Pipeline
from .store import Store

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
    style: str = Field(default=config.DEFAULT_STYLE, pattern="^(crop|blur)$")
    quality: str = Field(default=config.DEFAULT_QUALITY, pattern="^(fast|full)$")


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
    jobs = {j["id"]: j for j in store.active_jobs()}
    by_episode: dict[str, list] = {}
    for c in clips:
        by_episode.setdefault(c["episode_id"], []).append(c)
    for ep in episodes:
        ep["clip_count"] = len(by_episode.get(ep["id"], []))
    return {
        "settings": store.settings(),
        "episodes": episodes,
        "clips": clips,
        "jobs": list(jobs.values()),
        "defaults": {
            "playlist_url": config.DEFAULT_PLAYLIST,
            "count": config.DEFAULT_CLIP_COUNT,
            "min_dur": config.MIN_CLIP_SECONDS,
            "max_dur": config.MAX_CLIP_SECONDS,
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
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    for e in entries:
        store.upsert_episode(
            {
                **e,
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
    for ep in demo.DEMO_EPISODES:
        store.upsert_episode(
            {
                "id": ep["id"],
                "title": ep["title"],
                "url": ep["url"],
                "duration": ep["duration"],
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
    ep = store.get_episode(ep_id)
    if not ep:
        raise HTTPException(status_code=404, detail="Episode not found")
    if ep.get("status") == "processing":
        raise HTTPException(status_code=409, detail="Already processing this episode")
    if ep.get("source") != "demo" and not youtube_reachable():
        raise HTTPException(
            status_code=503,
            detail="YouTube is not reachable — demo mode still works.",
        )
    job = pipeline.start_job(ep_id, req.model_dump())
    return {"job_id": job["id"]}


@app.get("/api/episodes/{ep_id}/transcript")
def episode_transcript(ep_id: str):
    ep = store.get_episode(ep_id)
    if not ep:
        raise HTTPException(status_code=404, detail="Episode not found")
    if demo.is_demo(ep):
        segs = demo.demo_segments(ep_id)
        return {"source": "demo", "segments": [s.__dict__ for s in segs]}
    try:
        segs, src = youtube.get_transcript(ep["id"], ep["url"])
        return {"source": src, "segments": [s.__dict__ for s in segs]}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.delete("/api/clips/{clip_id}")
def delete_clip(clip_id: str):
    clip = store.get_clip(clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    for d in (config.CLIPS_DIR / clip["file"],):
        d.unlink(missing_ok=True)
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
