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

from . import __version__, config, demo, highlights, youtube
from .pipeline import Pipeline
from .store import Store
from .transcripts import to_sentences

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

_PROFILES = ("viral", "story", "facts", "energy")
_STYLES = ("crop", "blur")
_QUALITIES = ("fast", "full")
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


def _episode_segments(ep: dict):
    if demo.is_demo(ep):
        return demo.demo_segments(ep["id"]), "demo"
    return youtube.get_transcript(ep["id"], ep["url"])


# ---------------------------------------------------------------------------
# Errors + validation (mirrors the pydantic constraints in server.py)
# ---------------------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _need_int(body: dict, name: str, default: int, lo: int, hi: int) -> int:
    v = body.get(name, default)
    if isinstance(v, bool):
        raise ApiError(422, f"{name} must be an integer between {lo} and {hi}")
    if isinstance(v, float):
        if not v.is_integer():
            raise ApiError(422, f"{name} must be an integer between {lo} and {hi}")
        v = int(v)
    if not isinstance(v, int) or not (lo <= v <= hi):
        raise ApiError(422, f"{name} must be an integer between {lo} and {hi}")
    return v


def _need_float(body: dict, name: str, default: float, lo: float, hi: float) -> float:
    v = body.get(name, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ApiError(422, f"{name} must be a number between {lo} and {hi}")
    fv = float(v)
    if not (lo <= fv <= hi):
        raise ApiError(422, f"{name} must be a number between {lo} and {hi}")
    return fv


def _need_choice(body: dict, name: str, default: str, choices: tuple[str, ...]) -> str:
    v = body.get(name, default)
    if v not in choices:
        raise ApiError(422, f"{name} must be one of: {', '.join(choices)}")
    return v


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
            "style": config.DEFAULT_STYLE,
            "quality": config.DEFAULT_QUALITY,
        },
    }


def api_load_playlist(body: dict) -> dict:
    url = _need_url(body)
    limit = _need_int(body, "limit", config.EPISODE_PAGE_SIZE, 1, 100)
    if not youtube_reachable():
        raise ApiError(
            503,
            "YouTube is not reachable from this machine — "
            "use Demo mode to try the pipeline on synthetic media, "
            "or run AutoShorts where YouTube is accessible.",
        )
    try:
        entries = youtube.list_playlist(url, limit=limit)
    except RuntimeError as exc:
        raise ApiError(502, str(exc)) from exc
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
    return {"added": len(entries), "total_episodes": len(store.episodes())}


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
    episode = _get_episode_or_404(ep_id)
    _guard_ready(episode)
    params = {
        "count": _need_int(body, "count", config.DEFAULT_CLIP_COUNT, 1, 12),
        "min_dur": _need_float(body, "min_dur", config.MIN_CLIP_SECONDS, 8, 120),
        "max_dur": _need_float(body, "max_dur", config.MAX_CLIP_SECONDS, 10, 180),
        "profile": _need_choice(body, "profile", "viral", _PROFILES),
        "style": _need_choice(body, "style", config.DEFAULT_STYLE, _STYLES),
        "quality": _need_choice(body, "quality", config.DEFAULT_QUALITY, _QUALITIES),
    }
    job = pipeline.start_job(ep_id, params)
    return {"job_id": job["id"]}


def api_preview(ep_id: str, body: dict) -> dict:
    episode = _get_episode_or_404(ep_id)
    count = _need_int(body, "count", config.DEFAULT_CLIP_COUNT, 1, 12)
    min_dur = _need_float(body, "min_dur", config.MIN_CLIP_SECONDS, 5, 180)
    max_dur = _need_float(body, "max_dur", config.MAX_CLIP_SECONDS, 5, 180)
    profile = _need_choice(body, "profile", "viral", _PROFILES)
    if max_dur < min_dur:
        raise ApiError(422, "max_dur must be >= min_dur")
    try:
        segments, _source = _episode_segments(episode)
    except Exception as exc:
        raise ApiError(502, str(exc)) from exc
    moments = highlights.find_highlights(
        to_sentences(segments),
        count=count,
        min_dur=min_dur,
        max_dur=max_dur,
        profile=profile,
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
    title = body.get("title", "")
    if not isinstance(title, str):
        raise ApiError(422, "title must be a string")
    if len(title) > 120:
        raise ApiError(422, "title must be at most 120 characters")
    _guard_ready(episode)
    job = pipeline.start_job(
        ep_id,
        {
            "start": float(start),
            "end": float(end),
            "title": title,
            "style": _need_choice(body, "style", config.DEFAULT_STYLE, _STYLES),
            "quality": _need_choice(body, "quality", config.DEFAULT_QUALITY, _QUALITIES),
            "kind": "manual",
        },
    )
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

        # /api/episodes/{id}/...
        if len(parts) == 3 and parts[0] == "episodes":
            ep_id, action = parts[1], parts[2]
            if action == "transcript":
                if method not in ("GET", "HEAD"):
                    raise ApiError(405, "Method not allowed")
                self._send_json(api_transcript(ep_id))
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
