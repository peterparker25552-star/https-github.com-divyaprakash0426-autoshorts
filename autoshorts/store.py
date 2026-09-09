"""Tiny thread-safe JSON store for episodes, clips and jobs."""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import config


class Store:
    def __init__(self, path: Path = config.STATE_FILE):
        self.path = path
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {
            "settings": {
                "playlist_url": "",
                "playlist_title": "",
                "channel": "",
                "last_loaded": None,
            },
            "episodes": {},   # id -> episode dict
            "clips": {},      # clip_id -> clip dict
            "jobs": {},       # job_id -> job dict
        }
        self._load()

    # -- persistence -------------------------------------------------------
    def _load(self) -> None:
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                for k in self._data:
                    if k in raw:
                        self._data[k] = raw[k]
            except Exception:
                pass  # start fresh on corrupt state

    def _save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            tmp.replace(self.path)

    # -- settings ----------------------------------------------------------
    def settings(self) -> dict:
        with self._lock:
            return dict(self._data["settings"])

    def update_settings(self, **kw) -> dict:
        with self._lock:
            self._data["settings"].update(kw)
            self._save()
            return dict(self._data["settings"])

    # -- episodes ----------------------------------------------------------
    def upsert_episode(self, ep: dict) -> dict:
        with self._lock:
            existing = self._data["episodes"].get(ep["id"], {})
            merged = {**existing, **ep}
            self._data["episodes"][ep["id"]] = merged
            self._save()
            return dict(merged)

    def get_episode(self, ep_id: str) -> dict | None:
        with self._lock:
            ep = self._data["episodes"].get(ep_id)
            return dict(ep) if ep else None

    def episodes(self) -> list[dict]:
        with self._lock:
            eps = [dict(e) for e in self._data["episodes"].values()]
        eps.sort(key=lambda e: e.get("added_at") or 0, reverse=True)
        return eps

    def update_episode(self, ep_id: str, **kw) -> None:
        with self._lock:
            if ep_id in self._data["episodes"]:
                self._data["episodes"][ep_id].update(kw)
                self._save()

    def remove_episode_clips(self, ep_id: str) -> None:
        with self._lock:
            for cid in list(self._data["clips"]):
                if self._data["clips"][cid]["episode_id"] == ep_id:
                    del self._data["clips"][cid]
            self._data["episodes"][ep_id]["clips"] = []
            self._save()

    # -- clips -------------------------------------------------------------
    def add_clip(self, clip: dict) -> dict:
        with self._lock:
            cid = clip.get("id") or uuid.uuid4().hex[:12]
            clip["id"] = cid
            clip["created"] = time.time()
            self._data["clips"][cid] = clip
            ep = self._data["episodes"].get(clip["episode_id"])
            if ep is not None:
                ep.setdefault("clips", [])
                if cid not in ep["clips"]:
                    ep["clips"].append(cid)
            self._save()
            return dict(clip)

    def get_clip(self, cid: str) -> dict | None:
        with self._lock:
            c = self._data["clips"].get(cid)
            return dict(c) if c else None

    def delete_clip(self, cid: str) -> None:
        with self._lock:
            c = self._data["clips"].pop(cid, None)
            if c:
                ep = self._data["episodes"].get(c["episode_id"])
                if ep and cid in ep.get("clips", []):
                    ep["clips"].remove(cid)
                self._save()

    def clips(self, episode_id: str | None = None) -> list[dict]:
        with self._lock:
            clips = [dict(c) for c in self._data["clips"].values()]
        if episode_id:
            clips = [c for c in clips if c["episode_id"] == episode_id]
        clips.sort(key=lambda c: c.get("created", 0), reverse=True)
        return clips

    # -- jobs --------------------------------------------------------------
    def create_job(self, episode_id: str, params: dict) -> dict:
        with self._lock:
            jid = uuid.uuid4().hex[:12]
            job = {
                "id": jid,
                "episode_id": episode_id,
                "params": params,
                "status": "queued",
                "step": "queued",
                "progress": 0.0,
                "message": "",
                "error": None,
                "created": time.time(),
            }
            self._data["jobs"][jid] = job
            self._save()
            return dict(job)

    def update_job(self, jid: str, **kw) -> None:
        with self._lock:
            if jid in self._data["jobs"]:
                self._data["jobs"][jid].update(kw)
                self._save()

    def job(self, jid: str) -> dict | None:
        with self._lock:
            j = self._data["jobs"].get(jid)
            return dict(j) if j else None

    def active_jobs(self) -> list[dict]:
        with self._lock:
            return [
                dict(j)
                for j in self._data["jobs"].values()
                if j["status"] in ("queued", "running")
            ]

    def prune_jobs(self, keep: int = 40) -> None:
        with self._lock:
            jobs = sorted(
                self._data["jobs"].values(), key=lambda j: j["created"], reverse=True
            )
            for j in jobs[keep:]:
                self._data["jobs"].pop(j["id"], None)
            self._save()
