"""Shared test helpers: fresh stores, fake clips on disk, port picking."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def fresh_state(store) -> None:
    """Empty every collection and persist — a clean slate between tests."""
    with store._lock:
        store._data["settings"] = {
            "playlist_url": "",
            "playlist_title": "",
            "channel": "",
            "last_loaded": None,
            "autopilot": False,
        }
        store._data["episodes"].clear()
        store._data["clips"].clear()
        store._data["jobs"].clear()
        store._save()


def make_fake_clip(store, config_mod, episode_id="demo-chhetri-223", with_files=True):
    """Insert a clip record (with real files on disk when asked) for tests."""
    clip_id = f"test-{episode_id[:12]}-{int(time.time()*1000) % 100000}"
    clip_file = f"{clip_id}.mp4"
    thumb_file = f"{clip_id}.jpg"
    if with_files:
        config_mod.CLIPS_DIR.mkdir(parents=True, exist_ok=True)
        config_mod.THUMBS_DIR.mkdir(parents=True, exist_ok=True)
        (config_mod.CLIPS_DIR / clip_file).write_bytes(b"fake-mp4-bytes")
        (config_mod.THUMBS_DIR / thumb_file).write_bytes(b"fake-jpg-bytes")
    store.add_clip(
        {
            "id": clip_id,
            "episode_id": episode_id,
            "episode_title": "Test episode",
            "title": "Test clip",
            "start": 10.0,
            "end": 40.0,
            "duration": 30.0,
            "score": 7.5,
            "reasons": ["manual pick"],
            "breakdown": {"hook": 1.0},
            "file": clip_file,
            "thumb": thumb_file if with_files else None,
            "render": {
                "style": "blur", "quality": "fast", "format": "vertical",
                "captions": "classic", "captions_pos": "standard",
                "captions_box": False, "speed": 1.0,
                "progress": False, "silence": False, "loud": False,
            },
        }
    )
    return clip_id


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def spawn_server(port: int, data_dir: Path, module: str = "autoshorts.server_stdlib") -> subprocess.Popen:
    env = dict(os.environ)
    env["AUTOSHORTS_DATA"] = str(data_dir)
    return subprocess.Popen(
        [sys.executable, "-m", module, "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def wait_http_ready(port: int, timeout: float = 30.0) -> None:
    import urllib.request

    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as resp:
                if resp.status == 200:
                    return
        except Exception as exc:  # not up yet
            last_error = exc
        time.sleep(0.25)
    raise RuntimeError(f"server did not come up: {last_error}")


def http_request(method: str, url: str, payload=None, timeout: float = 30.0):
    """Tiny urllib JSON client: returns (status, body, headers).

    JSON responses are parsed; anything else comes back as raw ``bytes``.
    """
    import urllib.error
    import urllib.request

    data = None
    headers = {}
    if payload is not None:
        if isinstance(payload, (dict, list)):
            data = json.dumps(payload).encode("utf-8")
        else:
            data = payload
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            body = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            parsed = json.loads(body) if "json" in ctype else body
            return resp.status, parsed, dict(resp.headers)
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = body
        return exc.code, parsed, dict(exc.headers)

