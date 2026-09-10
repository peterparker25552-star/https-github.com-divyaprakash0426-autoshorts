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

from . import __version__, config, demo, highlights, llm, maintenance, youtube
from .maintenance import ServiceError
from .pipeline import Pipeline
from .store import Store
from .transcripts import to_sentences

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

_MAX_BODY = 5_000_000  # 5 MB cap on JSON request bodies
_JOB_LIMIT = (1, 200)   # GET /api/jobs?limit=

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


def _episode_segments(ep: dict):
    if demo.is_demo(ep):
        return demo.demo_segments(ep["id"]), "demo"
    return youtube.get_transcript(ep["id"], ep["url"])


# ---------------------------------------------------------------------------
# Errors + validation (delegates to the shared maintenance validators so both
# servers reject identical bodies with identical messages)
# ---------------------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _service(fn, *args, **kwargs):
    """Run a shared maintenance helper, mapping ServiceError -> HTTP error."""
    try:
        return fn(*args, **kwargs)
    except ServiceError as exc:
        raise ApiError(exc.status, exc.detail) from exc


# ---------------------------------------------------------------------------
# Endpoint logic (mirrors server.py)
# ---------------------------------------------------------------------------
def api_health() -> dict:
    disk = maintenance.disk_usage()
    return {
        "app": "autoshorts",
        "version": __version__,
        "youtube_reachable": youtube_reachable(),
        "demo_available": True,
        "ffmpeg": config.FFMPEG_BIN,
        "versions": maintenance.versions_info(),
        "disk_free": disk["free"],
        "disk_total": disk["total"],
        "llm_available": llm.available(),
    }


def api_state() -> dict:
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
            **maintenance.RENDER_DEFAULTS,
        },
    }


def api_load_url(body: dict) -> dict:
    """POST /api/playlist — accepts playlists AND single videos."""
    url = body.get("url", "")
    if not isinstance(url, str) or len(url) < 8:
        raise ApiError(422, "url must be at least 8 characters")
    limit = body.get("limit", config.EPISODE_PAGE_SIZE)
    if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 100):
        raise ApiError(422, "limit must be an integer between 1 and 100")
    # ONE reachability probe for the whole request (rule 2).
    return _service(
        maintenance.ingest_url, store, pipeline, url, limit, youtube_reachable()
    )


def api_load_demo() -> dict:
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
        raise ApiError(404, "Episode not found")
    return episode


def _guard_ready(episode: dict) -> None:
    if episode.get("status") == "processing":
        raise ApiError(409, "Already processing this episode")
    if episode.get("source") != "demo" and not youtube_reachable():
        raise ApiError(503, "YouTube is not reachable — demo mode still works.")


def api_generate_shorts(ep_id: str, body: dict) -> dict:
    # Body validation first: FastAPI rejects a bad body (422) before the
    # handler ever sees the episode, so the stdlib server must match.
    params = _service(maintenance.shorts_params_from, body)
    episode = _get_episode_or_404(ep_id)
    _guard_ready(episode)
    job = pipeline.start_job(ep_id, params)
    return {"job_id": job["id"]}


def api_batch(body: dict) -> dict:
    """Queue many episodes — ONE reachability probe for the whole call."""
    body = body if isinstance(body, dict) else {}
    maintenance.check_unknown_keys(body, maintenance.BATCH_KEYS)
    selector_body = {
        key: value for key, value in body.items()
        if key not in ("urls", "episodes")
    }
    if selector_body:
        params = _service(maintenance.shorts_params_from, selector_body)
    else:
        params = maintenance.default_job_params()
    urls = body.get("urls")
    episodes = body.get("episodes")
    if urls is not None and (
        not isinstance(urls, list)
        or any(not isinstance(u, str) for u in urls)
    ):
        raise ApiError(422, "urls must be a list of strings")
    if episodes is not None and (
        not isinstance(episodes, list)
        or any(not isinstance(e, str) for e in episodes)
    ):
        raise ApiError(422, "episodes must be a list of strings")
    return maintenance.batch_queue(
        store, pipeline, params, youtube_reachable(), urls=urls, episodes=episodes
    )


def api_jobs(query: dict) -> dict:
    raw = (query.get("limit") or ["20"])[0]
    try:
        limit = int(raw)
    except (TypeError, ValueError):
        raise ApiError(422, "limit must be an integer")
    limit = max(_JOB_LIMIT[0], min(limit, _JOB_LIMIT[1]))
    return {"jobs": store.recent_jobs(limit)}


def api_settings(body: dict) -> dict:
    return _service(maintenance.update_settings, store, body)


def api_retry_job(job_id: str) -> dict:
    return _service(maintenance.retry_job, store, pipeline, job_id)


def api_cancel_job(job_id: str) -> dict:
    return _service(maintenance.cancel_job, store, job_id)


def api_delete_episode(ep_id: str) -> dict:
    return _service(maintenance.delete_episode, store, ep_id)


def api_chapters(ep_id: str) -> dict:
    return _service(maintenance.episode_chapters, store, ep_id)


def api_export(ep_id: str, query: dict) -> tuple[str, str]:
    """Validate + build the CSV export; returns (filename, csv_text)."""
    raw_count = (query.get("count") or [str(config.DEFAULT_CLIP_COUNT)])[0]
    profile = (query.get("profile") or ["viral"])[0]
    try:
        count = int(raw_count)
    except (TypeError, ValueError):
        raise ApiError(
            422,
            f"count must be an integer between {maintenance.COUNT_RANGE[0]} "
            f"and {maintenance.COUNT_RANGE[1]}",
        )
    return _service(maintenance.export_csv, store, ep_id, count, profile)


def api_rename(clip_id: str, body: dict) -> dict:
    return _service(maintenance.rename_clip, store, clip_id, body)


def api_rerender(clip_id: str, body: dict) -> dict:
    return _service(maintenance.rerender_clip, store, pipeline, clip_id, body)


def api_polish(clip_id: str) -> dict:
    return _service(maintenance.polish_clip, store, clip_id)


def api_search(query: dict) -> dict:
    q = (query.get("q") or [""])[0]
    return _service(maintenance.search_transcripts, store, q)


def api_storage() -> dict:
    return maintenance.storage_info(store)


def api_clean_storage(body: dict) -> dict:
    target = body.get("target")
    if not isinstance(target, str) or target not in maintenance.CLEAN_TARGETS:
        raise ApiError(
            422,
            "target must be one of: " + ", ".join(maintenance.CLEAN_TARGETS),
        )
    return _service(maintenance.clean_storage, store, target)


def api_restore(body: dict) -> dict:
    return _service(maintenance.restore_state, store, body)


def api_preview(ep_id: str, body: dict) -> dict:
    params = _service(maintenance.preview_params_from, body)
    episode = _get_episode_or_404(ep_id)
    try:
        segments, _source = _episode_segments(episode)
    except Exception as exc:
        raise ApiError(502, str(exc)) from exc
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


def api_manual(ep_id: str, body: dict) -> dict:
    # Body validation first (FastAPI parity), then 404, then readiness.
    params = _service(maintenance.manual_params_from, body)
    episode = _get_episode_or_404(ep_id)
    _guard_ready(episode)
    job = pipeline.start_job(ep_id, params)
    return {"job_id": job["id"]}


def api_transcript(ep_id: str) -> dict:
    episode = _get_episode_or_404(ep_id)
    try:
        segments, source = _episode_segments(episode)
        return {"source": source, "segments": [segment.__dict__ for segment in segments]}
    except Exception as exc:
        raise ApiError(502, str(exc)) from exc


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

    def _serve_clip_zip(self, episode_id: str | None) -> None:
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

    def _serve_backup(self) -> None:
        """Stream a state backup as a downloadable JSON attachment."""
        with tempfile.NamedTemporaryFile(
            prefix="autoshorts-backup-", suffix=".json",
            dir=config.DATA_DIR, delete=False,
        ) as temporary:
            backup_path = Path(temporary.name)
            temporary.write(maintenance.backup_bytes(store))
        try:
            self._serve_file(
                backup_path,
                "application/json",
                f"autoshorts-state-{int(time.time())}.json",
                attachment=True,
            )
        finally:
            backup_path.unlink(missing_ok=True)

    def _serve_csv(self, filename: str, text: str) -> None:
        data = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header(
            "Content-Disposition", f'attachment; filename="{filename}"'
        )
        self.end_headers()
        if self._want_body:
            self.wfile.write(data)

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

        # POST /api/playlist (playlists AND single videos)
        if parts == ["playlist"]:
            if method != "POST":
                raise ApiError(405, "Method not allowed")
            self._send_json(api_load_url(body))
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

        # GET /api/search?q=
        if parts == ["search"]:
            if method not in ("GET", "HEAD"):
                raise ApiError(405, "Method not allowed")
            self._send_json(api_search(query))
            return

        # GET /api/jobs
        if parts == ["jobs"]:
            if method not in ("GET", "HEAD"):
                raise ApiError(405, "Method not allowed")
            self._send_json(api_jobs(query))
            return

        # POST /api/jobs/{id}/retry · /cancel
        if len(parts) == 3 and parts[0] == "jobs" and parts[2] in ("retry", "cancel"):
            if method != "POST":
                raise ApiError(405, "Method not allowed")
            if parts[2] == "retry":
                self._send_json(api_retry_job(parts[1]))
            else:
                self._send_json(api_cancel_job(parts[1]))
            return

        # GET /api/storage · POST /api/storage/clean
        if parts == ["storage"]:
            if method not in ("GET", "HEAD"):
                raise ApiError(405, "Method not allowed")
            self._send_json(api_storage())
            return
        if parts == ["storage", "clean"]:
            if method != "POST":
                raise ApiError(405, "Method not allowed")
            self._send_json(api_clean_storage(body))
            return

        # GET /api/backup · POST /api/restore
        if parts == ["backup"]:
            if method not in ("GET", "HEAD"):
                raise ApiError(405, "Method not allowed")
            self._serve_backup()
            return
        if parts == ["restore"]:
            if method != "POST":
                raise ApiError(405, "Method not allowed")
            self._send_json(api_restore(body))
            return

        # /api/episodes/{id}[/{action}]
        if len(parts) >= 2 and parts[0] == "episodes":
            ep_id, action = parts[1], (parts[2] if len(parts) == 3 else None)
            if action in ("transcript", "chapters") and len(parts) == 3:
                if method not in ("GET", "HEAD"):
                    raise ApiError(405, "Method not allowed")
                if action == "transcript":
                    self._send_json(api_transcript(ep_id))
                else:
                    self._send_json(api_chapters(ep_id))
                return
            if action == "export" and len(parts) == 3:
                if method not in ("GET", "HEAD"):
                    raise ApiError(405, "Method not allowed")
                filename, text = api_export(ep_id, query)
                self._serve_csv(filename, text)
                return
            if action is None and method == "DELETE":
                self._send_json(api_delete_episode(ep_id))
                return
            if len(parts) == 3 and method == "POST":
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
            self._serve_clip_zip(episode_id)
            return

        # /api/clips/{id}[...]
        if len(parts) >= 2 and parts[0] == "clips":
            clip_id = parts[1]
            if len(parts) == 2:
                # DELETE /api/clips/{id}
                if method != "DELETE":
                    raise ApiError(405, "Method not allowed")
                clip = store.get_clip(clip_id)
                if not clip:
                    raise ApiError(404, "Clip not found")
                (config.CLIPS_DIR / clip["file"]).unlink(missing_ok=True)
                if clip.get("thumb"):
                    (config.THUMBS_DIR / clip["thumb"]).unlink(missing_ok=True)
                store.delete_clip(clip_id)
                self._send_json({"deleted": clip_id})
                return
            if len(parts) == 3:
                action = parts[2]
                if action == "srt":
                    if method not in ("GET", "HEAD"):
                        raise ApiError(405, "Method not allowed")
                    path = _service(maintenance.clip_srt, store, clip_id)
                    self._serve_file(
                        path, "application/x-subrip", f"{clip_id}.srt",
                        attachment=True,
                    )
                    return
                if action == "rerender":
                    if method != "POST":
                        raise ApiError(405, "Method not allowed")
                    self._send_json(api_rerender(clip_id, body))
                    return
                if action == "polish":
                    if method != "POST":
                        raise ApiError(405, "Method not allowed")
                    self._send_json(api_polish(clip_id))
                    return
                if action == "rename":
                    if method != "POST":
                        raise ApiError(405, "Method not allowed")
                    self._send_json(api_rename(clip_id, body))
                    return
                if action in ("file", "thumb"):
                    if method not in ("GET", "HEAD"):
                        raise ApiError(405, "Method not allowed")
                    clip = store.get_clip(clip_id)
                    if not clip:
                        raise ApiError(404, "Clip not found")
                    if action == "file":
                        path = config.CLIPS_DIR / clip["file"]
                        if not path.exists():
                            raise ApiError(410, "Clip file missing on disk")
                        self._serve_file(
                            path, "video/mp4", f"autoshort-{clip_id}.mp4"
                        )
                        return
                    if not clip.get("thumb"):
                        raise ApiError(404, "No thumbnail")
                    path = config.THUMBS_DIR / clip["thumb"]
                    if not path.exists():
                        raise ApiError(410, "Thumbnail missing")
                    self._serve_file(path, "image/jpeg")
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
