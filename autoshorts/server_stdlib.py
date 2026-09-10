"""Stdlib-only HTTP server: the same JSON API + web UI as ``server.py`` with
zero third-party dependencies.

This is the default on Android/Termux (and the automatic fallback anywhere
FastAPI/uvicorn is not installed), because FastAPI's compiled dependencies
(pydantic-core, a Rust extension) ship no wheels for Termux's Python — pip
fails there with ``ResolutionImpossible`` across every fastapi version.
The app logic (store, pipeline, highlight engine, ffmpeg rendering) is shared;
only the HTTP layer differs.

Routes mirror ``server.py`` exactly, including status codes and the
``{"detail": ...}`` error shape the web UI expects.
"""
from __future__ import annotations

import csv
import io
import json
import mimetypes
import re
import tempfile
import threading
import time
import traceback
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__, config, demo, highlights, youtube
from .pipeline import Pipeline, render_opts_from_params
from .service import (
    build_moments_csv,
    clean_storage,
    episode_segments,
    episode_segments_offline,
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
    need_bool,
    need_choice,
    need_float,
    need_int,
    need_speed,
    normalize_title,
    parse_render_opts,
    parse_selectors,
)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

_MAX_BODY = 5_000_000  # 5 MB cap on JSON request bodies

mimetypes.add_type("application/manifest+json", ".webmanifest")

store = Store()
pipeline = Pipeline(store)

_reachable_cache: tuple[float, bool] = (0.0, False)
_reachable_lock = threading.Lock()


def youtube_reachable(ttl: float = 60.0) -> bool:
    global _reachable_cache
    now = time.time()
    with _reachable_lock:
        if now - _reachable_cache[0] <= ttl:
            return _reachable_cache[1]
    ok = youtube.check_reachable()
    with _reachable_lock:
        _reachable_cache = (now, ok)
    return ok


# ---------------------------------------------------------------------------
# Errors + validation (mirrors server.py via shared validate.py)
# ---------------------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _as_api_error(exc: ValidationError) -> ApiError:
    return ApiError(exc.status, exc.detail)


def _need_url(body: dict) -> str:
    v = body.get("url", "")
    if not isinstance(v, str) or len(v) < 8:
        raise ApiError(422, "url must be at least 8 characters")
    return v


# ---------------------------------------------------------------------------
# Endpoint logic (mirrors server.py)
# ---------------------------------------------------------------------------
def api_health() -> dict:
    return {
        "app": "autoshorts",
        "version": __version__,
        "youtube_reachable": youtube_reachable(),
        "demo_available": True,
        "ffmpeg": config.FFMPEG_BIN,
        "ytdlp_version": youtube.ytdlp_version(),
    }


def api_state() -> dict:
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


def api_load_playlist(body: dict) -> dict:
    url = _need_url(body)
    try:
        limit = need_int(body, "limit", config.EPISODE_PAGE_SIZE, 1, 100)
    except ValidationError as exc:
        raise _as_api_error(exc)
    if not youtube_reachable():
        raise ApiError(
            503,
            "YouTube is not reachable from this machine — "
            "use Demo mode to try the pipeline on synthetic media, "
            "or run AutoShorts where YouTube is accessible.",
        )
    auto_queued = 0
    autopilot = bool(store.settings().get("autopilot"))
    if youtube.is_playlist_url(url):
        try:
            entries = youtube.list_playlist(url, limit=limit)
        except RuntimeError as exc:
            raise ApiError(502, str(exc)) from exc
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
            raise ApiError(502, str(exc)) from exc
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


def api_load_demo() -> dict:
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


def _get_episode_or_404(ep_id: str) -> dict:
    episode = store.get_episode(ep_id)
    if not episode:
        raise ApiError(404, "Episode not found")
    return episode


def _guard_ready(episode: dict) -> None:
    if store.episode_is_processing(episode["id"]):
        raise ApiError(409, "Already processing this episode")
    if episode.get("source") != "demo" and not youtube_reachable():
        raise ApiError(503, "YouTube is not reachable — demo mode still works.")


def api_generate_shorts(ep_id: str, body: dict) -> dict:
    episode = _get_episode_or_404(ep_id)
    _guard_ready(episode)
    try:
        selectors = parse_selectors(body)
        opts = parse_render_opts(body)
    except ValidationError as exc:
        raise _as_api_error(exc)
    job = pipeline.start_job(ep_id, {**selectors, **opts})
    return {"job_id": job["id"]}


def api_preview(ep_id: str, body: dict) -> dict:
    episode = _get_episode_or_404(ep_id)
    try:
        selectors = parse_selectors(body)
        # Render opts are accepted + validated on preview too (bad values → 422).
        parse_render_opts(body)
    except ValidationError as exc:
        raise _as_api_error(exc)
    try:
        segments, _source = episode_segments(episode)
    except Exception as exc:
        raise ApiError(502, str(exc)) from exc
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


def api_manual(ep_id: str, body: dict) -> dict:
    episode = _get_episode_or_404(ep_id)
    start = body.get("start")
    end = body.get("end")
    if (
        isinstance(start, bool) or not isinstance(start, (int, float))
        or isinstance(end, bool) or not isinstance(end, (int, float))
    ):
        raise ApiError(422, "start and end must be numbers")
    if end - start < 5:
        raise ApiError(422, "Manual clips must be at least 5 seconds")
    if end - start > 180:
        raise ApiError(422, "Manual clips must be at most 180 seconds")
    title = body.get("title", "")
    if not isinstance(title, str):
        raise ApiError(422, "title must be a string")
    if len(title.strip()) > 120:
        raise ApiError(422, "title must be at most 120 characters")
    try:
        opts = parse_render_opts(body)
    except ValidationError as exc:
        raise _as_api_error(exc)
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


def api_transcript(ep_id: str) -> dict:
    episode = _get_episode_or_404(ep_id)
    try:
        segments, source = episode_segments(episode)
        return {"source": source, "segments": [segment.__dict__ for segment in segments]}
    except Exception as exc:
        raise ApiError(502, str(exc)) from exc


def api_chapters(ep_id: str) -> dict:
    episode = _get_episode_or_404(ep_id)
    try:
        return get_chapters(episode)
    except Exception as exc:
        raise ApiError(502, str(exc)) from exc


def api_export(ep_id: str, query: dict) -> tuple[str, str]:
    episode = _get_episode_or_404(ep_id)
    count_raw = (query.get("count", ["8"])[0] if isinstance(query.get("count"), list) else query.get("count", "8"))
    profile = (query.get("profile", ["viral"])[0] if isinstance(query.get("profile"), list) else query.get("profile", "viral"))
    try:
        count = int(float(count_raw)) if str(count_raw).replace(".", "", 1).lstrip("-").isdigit() else int(count_raw)
    except (TypeError, ValueError):
        raise ApiError(422, "count must be an integer between 1 and 12")
    if isinstance(count, bool) or not (1 <= count <= 12):
        raise ApiError(422, "count must be an integer between 1 and 12")
    if profile not in config.PROFILES:
        raise ApiError(422, "profile must be one of: viral, story, facts, energy")
    try:
        csv_text = build_moments_csv(episode, count, profile)
    except ValidationError as exc:
        raise _as_api_error(exc)
    except RuntimeError as exc:
        raise ApiError(502, str(exc)) from exc
    return csv_text, f"autoshorts-{ep_id}-moments.csv"


def api_delete_episode(ep_id: str) -> dict:
    episode = store.get_episode(ep_id)
    if not episode:
        raise ApiError(404, "Episode not found")
    if store.episode_is_processing(ep_id):
        raise ApiError(409, "Episode is processing")
    # Remove clip + thumb files first.
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


def api_clip_rerender(clip_id: str, body: dict) -> dict:
    clip = store.get_clip(clip_id)
    if not clip:
        raise ApiError(404, "Clip not found")
    try:
        check_rerender_allowlist(body)
    except ValidationError as exc:
        raise _as_api_error(exc)
    ep = store.get_episode(clip["episode_id"])
    if not ep:
        raise ApiError(404, "Episode not found")
    if store.episode_is_processing(ep["id"]):
        raise ApiError(409, "Already processing this episode")
    # Base = stored clip.render so pos/box survive; validate merged result.
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
        raise _as_api_error(exc)
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


def api_clip_polish(clip_id: str) -> dict:
    clip = store.get_clip(clip_id)
    if not clip:
        raise ApiError(404, "Clip not found")
    from .llm import is_configured, polish_clip

    if not is_configured():
        raise ApiError(503, "No LLM configured — set OPENAI_API_KEY to enable polish.")
    try:
        pack = polish_clip(clip.get("title", ""), clip.get("episode_title", ""),
                           clip.get("reasons") or [])
    except Exception as exc:
        raise ApiError(502, str(exc)[:300]) from exc
    store.update_clip(clip_id, upload_pack=pack)
    return {"id": clip_id, "upload_pack": pack}


def api_clip_rename(clip_id: str, body: dict) -> dict:
    clip = store.get_clip(clip_id)
    if not clip:
        raise ApiError(404, "Clip not found")
    if "title" not in body:
        raise ApiError(422, "title is required")
    try:
        title = normalize_title(body["title"], allow_empty=False)
    except ValidationError as exc:
        raise _as_api_error(exc)
    store.update_clip(clip_id, title=title)
    return {"id": clip_id, "title": title}


def api_jobs_list() -> dict:
    return {"jobs": store.jobs()}


def api_job_retry(job_id: str) -> dict:
    job = store.job(job_id)
    if not job:
        raise ApiError(404, "Job not found")
    ep = store.get_episode(job["episode_id"])
    if not ep:
        raise ApiError(404, "Episode not found")
    if store.episode_is_processing(ep["id"]):
        raise ApiError(409, "Already processing this episode")
    params = dict(job.get("params") or {})
    new_job = pipeline.start_job(ep["id"], params)
    return {"job_id": new_job["id"]}


def api_job_cancel(job_id: str) -> dict:
    job = store.job(job_id)
    if not job:
        raise ApiError(404, "Job not found")
    if job.get("status") != "queued":
        raise ApiError(409, "Only queued jobs can be cancelled")
    store.cancel_job(job_id)
    ep_id = job["episode_id"]
    if not store.active_jobs_for_episode(ep_id):
        ep = store.get_episode(ep_id)
        if ep and ep.get("status") == "processing":
            store.update_episode(ep_id, status="new", error=None)
    return {"cancelled": job_id}


def api_settings(body: dict) -> dict:
    allowed = {"playlist_url", "playlist_title", "channel", "autopilot", "last_loaded"}
    updates = {k: v for k, v in body.items() if k in allowed}
    # Validate autopilot bool when present.
    if "autopilot" in updates and not isinstance(updates["autopilot"], bool):
        raise ApiError(422, "autopilot must be true or false")
    if "playlist_url" in updates and not isinstance(updates["playlist_url"], str):
        raise ApiError(422, "playlist_url must be a string")
    return store.update_settings(**updates)


def api_batch(body: dict) -> dict:
    urls = body.get("urls", [])
    episodes = body.get("episodes", [])
    if urls is None:
        urls = []
    if episodes is None:
        episodes = []
    if not isinstance(urls, list) or not isinstance(episodes, list):
        raise ApiError(422, "urls and episodes must be arrays")
    if not urls and not episodes:
        raise ApiError(422, "Provide urls[] and/or episodes[]")
    try:
        selectors = parse_selectors(body)
        opts = parse_render_opts(body)
    except ValidationError as exc:
        raise _as_api_error(exc)
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


def api_storage() -> dict:
    return storage_info()


def api_storage_clean(body: dict) -> dict:
    target = body.get("target", "")
    try:
        return clean_storage(target)
    except ValidationError as exc:
        raise _as_api_error(exc)


def api_backup() -> dict:
    return store.snapshot()


def api_restore(body: dict) -> dict:
    state = body.get("state", None) if isinstance(body, dict) else None
    if state is None:
        # Accept a raw state object as the body too.
        if isinstance(body, dict) and all(k in body for k in ("episodes", "clips", "jobs", "settings")):
            state = body
        else:
            raise ApiError(422, "body must contain a state object")
    try:
        store.restore(state)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ApiError(422, f"bad state shape: {exc}")
    return {"ok": True}


def api_search(query: dict) -> dict:
    q = query.get("q", [""])[0] if isinstance(query.get("q"), list) else query.get("q", "")
    try:
        return search_transcripts(store, q)
    except ValidationError as exc:
        raise _as_api_error(exc)


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "AutoShorts/" + __version__

    def log_message(self, fmt: str, *args) -> None:  # quieter than the default
        print(f"{self.address_string()} {self.command} {self.path} -> {fmt % args}")

    # -- responses ------------------------------------------------------
    @property
    def _want_body(self) -> bool:
        return self.command != "HEAD"

    def _send_json(self, obj: dict, status: int = 200) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self._want_body:
            self.wfile.write(data)

    def _send_text(self, text: str, content_type: str, download_name: str | None = None,
                   status: int = 200) -> None:
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if download_name:
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.end_headers()
        if self._want_body:
            self.wfile.write(data)

    def _read_json(self) -> dict:
        length = self.headers.get("Content-Length")
        if not length:
            return {}
        try:
            count = int(length)
        except ValueError:
            raise ApiError(400, "Bad Content-Length")
        if count > _MAX_BODY:
            raise ApiError(413, "Request body too large")
        raw = self.rfile.read(count) if count > 0 else b""
        if not raw.strip():
            return {}
        try:
            obj = json.loads(raw.decode("utf-8"))
        except Exception:
            raise ApiError(422, "Invalid JSON body")
        if not isinstance(obj, dict):
            raise ApiError(422, "JSON body must be an object")
        return obj

    def _serve_file(
        self,
        path: Path,
        content_type: str,
        download_name: str | None = None,
        attachment: bool = False,
    ) -> None:
        if not path.is_file():
            raise ApiError(404, "Not found")
        size = path.stat().st_size
        start, end = 0, size - 1
        status = 200
        range_header = (self.headers.get("Range") or "").strip()
        if range_header:
            match = re.match(r"bytes=(\d*)-(\d*)$", range_header)
            if match:
                first, last = match.groups()
                if first == "" and last != "":  # suffix range: last N bytes
                    start = max(size - int(last), 0)
                else:
                    if first:
                        start = int(first)
                    if last:
                        end = int(last)
                    end = min(end, size - 1)
                if size == 0 or start >= size or end < start:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(length))
        if download_name:
            disp = "attachment" if attachment else "inline"
            self.send_header(
                "Content-Disposition", f'{disp}; filename="{download_name}"'
            )
        self.end_headers()
        if not self._want_body:
            return
        with open(path, "rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(65536, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _serve_static(self, path: str) -> None:
        rel = path[len("/static/"):] if path != "/static" else ""
        target = (WEB_DIR / rel).resolve()
        if WEB_DIR.resolve() not in target.parents and target != WEB_DIR.resolve():
            raise ApiError(404, "Not found")
        if target.is_dir():
            raise ApiError(404, "Not found")
        content_type, _ = mimetypes.guess_type(str(target))
        self._serve_file(target, content_type or "application/octet-stream")

    def _serve_zip(self, episode_id: str | None) -> None:
        clips = store.clips(episode_id=episode_id)
        files = [
            (clip, config.CLIPS_DIR / clip["file"])
            for clip in clips
            if (config.CLIPS_DIR / clip["file"]).is_file()
        ]
        if not files:
            raise ApiError(404, "No clips available to download")
        stamp = int(time.time())
        suffix = f"-{episode_id}" if episode_id else ""
        download_name = f"autoshorts{suffix}-{stamp}.zip"
        with tempfile.NamedTemporaryFile(
            prefix="autoshorts-", suffix=".zip", dir=config.DATA_DIR, delete=False
        ) as temporary:
            zip_path = Path(temporary.name)
        try:
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as archive:
                for clip, path in files:
                    archive.write(path, arcname=f"autoshort-{clip['id']}.mp4")
            self._serve_file(zip_path, "application/zip", download_name, attachment=True)
        finally:
            zip_path.unlink(missing_ok=True)

    # -- routing ---------------------------------------------------------
    def do_GET(self) -> None:
        self._dispatch()

    def do_HEAD(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_DELETE(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self._dispatch()

    def do_PATCH(self) -> None:
        self._dispatch()

    def _dispatch(self) -> None:
        try:
            parsed = urlparse(self.path)
            path = unquote(parsed.path or "/")
            if len(path) > 1:
                path = path.rstrip("/")
            query = parse_qs(parsed.query)
            method = self.command

            if path == "/":
                if method not in ("GET", "HEAD"):
                    raise ApiError(405, "Method not allowed")
                self._serve_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
                return

            if path == "/static" or path.startswith("/static/"):
                if method not in ("GET", "HEAD"):
                    raise ApiError(405, "Method not allowed")
                self._serve_static(path)
                return

            parts = [p for p in path.split("/") if p]
            if len(parts) >= 2 and parts[0] == "api":
                self._dispatch_api(parts[1:], query, method)
                return

            raise ApiError(404, "Not found")
        except ApiError as exc:
            try:
                self._send_json({"detail": exc.detail}, status=exc.status)
            except (BrokenPipeError, ConnectionResetError):
                pass
        except (BrokenPipeError, ConnectionResetError):
            pass  # client went away (e.g. cancelled video load)
        except Exception:
            traceback.print_exc()
            try:
                self._send_json({"detail": "Internal server error"}, status=500)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _dispatch_api(self, parts: list[str], query: dict, method: str) -> None:
        body = self._read_json() if method in ("POST", "PUT", "PATCH") else {}

        # GET /api/health
        if parts == ["health"]:
            if method not in ("GET", "HEAD"):
                raise ApiError(405, "Method not allowed")
            self._send_json(api_health())
            return

        # GET /api/state
        if parts == ["state"]:
            if method not in ("GET", "HEAD"):
                raise ApiError(405, "Method not allowed")
            self._send_json(api_state())
            return

        # POST /api/playlist
        if parts == ["playlist"]:
            if method != "POST":
                raise ApiError(405, "Method not allowed")
            self._send_json(api_load_playlist(body))
            return

        # POST /api/demo/load
        if parts == ["demo", "load"]:
            if method != "POST":
                raise ApiError(405, "Method not allowed")
            self._send_json(api_load_demo())
            return

        # POST /api/settings
        if parts == ["settings"]:
            if method != "POST":
                raise ApiError(405, "Method not allowed")
            self._send_json(api_settings(body))
            return

        # POST /api/batch
        if parts == ["batch"]:
            if method != "POST":
                raise ApiError(405, "Method not allowed")
            self._send_json(api_batch(body))
            return

        # GET /api/storage, POST /api/storage/clean
        if parts == ["storage"]:
            if method not in ("GET", "HEAD"):
                raise ApiError(405, "Method not allowed")
            self._send_json(api_storage())
            return
        if parts == ["storage", "clean"]:
            if method != "POST":
                raise ApiError(405, "Method not allowed")
            self._send_json(api_storage_clean(body))
            return

        # GET /api/backup, POST /api/restore
        if parts == ["backup"]:
            if method not in ("GET", "HEAD"):
                raise ApiError(405, "Method not allowed")
            data = json.dumps(api_backup(), ensure_ascii=False)
            self._send_text(data, "application/json; charset=utf-8",
                            "autoshorts-backup.json")
            return
        if parts == ["restore"]:
            if method != "POST":
                raise ApiError(405, "Method not allowed")
            self._send_json(api_restore(body))
            return

        # GET /api/search
        if parts == ["search"]:
            if method not in ("GET", "HEAD"):
                raise ApiError(405, "Method not allowed")
            self._send_json(api_search(query))
            return

        # GET /api/jobs, POST /api/jobs/{id}/retry|cancel
        if parts == ["jobs"]:
            if method not in ("GET", "HEAD"):
                raise ApiError(405, "Method not allowed")
            self._send_json(api_jobs_list())
            return
        if len(parts) == 3 and parts[0] == "jobs" and parts[2] in ("retry", "cancel"):
            if method != "POST":
                raise ApiError(405, "Method not allowed")
            if parts[2] == "retry":
                self._send_json(api_job_retry(parts[1]))
            else:
                self._send_json(api_job_cancel(parts[1]))
            return

        # /api/episodes/{id}[...]
        if len(parts) >= 2 and parts[0] == "episodes":
            ep_id = parts[1]
            if len(parts) == 2 and method == "DELETE":
                self._send_json(api_delete_episode(ep_id))
                return
            if len(parts) == 3:
                action = parts[2]
                if action == "transcript":
                    if method not in ("GET", "HEAD"):
                        raise ApiError(405, "Method not allowed")
                    self._send_json(api_transcript(ep_id))
                    return
                if action == "chapters":
                    if method not in ("GET", "HEAD"):
                        raise ApiError(405, "Method not allowed")
                    self._send_json(api_chapters(ep_id))
                    return
                if action == "export":
                    if method not in ("GET", "HEAD"):
                        raise ApiError(405, "Method not allowed")
                    csv_text, fname = api_export(ep_id, query)
                    self._send_text(csv_text, "text/csv; charset=utf-8", fname)
                    return
                if method != "POST":
                    raise ApiError(405, "Method not allowed")
                if action == "shorts":
                    self._send_json(api_generate_shorts(ep_id, body))
                    return
                if action == "preview":
                    self._send_json(api_preview(ep_id, body))
                    return
                if action == "manual":
                    self._send_json(api_manual(ep_id, body))
                    return
            raise ApiError(404, "Not found")

        # GET /api/clips/zip
        if parts == ["clips", "zip"]:
            if method not in ("GET", "HEAD"):
                raise ApiError(405, "Method not allowed")
            episode_id = query.get("episode_id", [None])[0]
            self._serve_zip(episode_id)
            return

        # /api/clips/{id}[...]
        if len(parts) >= 2 and parts[0] == "clips":
            clip_id = parts[1]
            if len(parts) == 2 and method == "DELETE":
                clip = store.get_clip(clip_id)
                if not clip:
                    raise ApiError(404, "Clip not found")
                (config.CLIPS_DIR / clip["file"]).unlink(missing_ok=True)
                if clip.get("thumb"):
                    (config.THUMBS_DIR / clip["thumb"]).unlink(missing_ok=True)
                for suffix in (".ass", ".srt"):
                    (config.SUBS_DIR / f"{clip_id}{suffix}").unlink(missing_ok=True)
                store.delete_clip(clip_id)
                self._send_json({"deleted": clip_id})
                return
            if len(parts) == 3 and method in ("GET", "HEAD"):
                clip = store.get_clip(clip_id)
                if not clip:
                    raise ApiError(404, "Clip not found")
                if parts[2] == "file":
                    path = config.CLIPS_DIR / clip["file"]
                    if not path.exists():
                        raise ApiError(410, "Clip file missing on disk")
                    self._serve_file(
                        path, "video/mp4", f"autoshort-{clip_id}.mp4"
                    )
                    return
                if parts[2] == "thumb":
                    if not clip.get("thumb"):
                        raise ApiError(404, "No thumbnail")
                    path = config.THUMBS_DIR / clip["thumb"]
                    if not path.exists():
                        raise ApiError(410, "Thumbnail missing")
                    self._serve_file(path, "image/jpeg")
                    return
                if parts[2] == "srt":
                    text = get_clip_srt_text(clip, store)
                    if not text:
                        raise ApiError(404, "No captions available for this clip")
                    self._send_text(text, "text/srt; charset=utf-8",
                                    f"autoshort-{clip_id}.srt")
                    return
            if len(parts) == 3 and method == "POST":
                if parts[2] == "rerender":
                    self._send_json(api_clip_rerender(clip_id, body))
                    return
                if parts[2] == "polish":
                    self._send_json(api_clip_polish(clip_id))
                    return
                if parts[2] == "rename":
                    self._send_json(api_clip_rename(clip_id, body))
                    return
            raise ApiError(404, "Not found")

        raise ApiError(404, "Not found")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(host: str = "0.0.0.0", port: int = 8000) -> None:
    httpd = Server((host, port), Handler)
    print(f"AutoShorts UI → http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="AutoShorts stdlib server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    serve(args.host, args.port)
