"""Tiny thread-safe JSON store for episodes, clips and jobs."""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import config


DEFAULT_SETTINGS = {
    "playlist_url": "",
    "playlist_title": "",
    "channel": "",
    "last_loaded": None,
    "autopilot": False,
}


class Store:
    def __init__(self, path: Path = config.STATE_FILE):
        self.path = path
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {
            "settings": dict(DEFAULT_SETTINGS),
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
                    if k in raw and isinstance(raw[k], dict):
                        if k == "settings":
                            merged = dict(DEFAULT_SETTINGS)
                            merged.update(raw[k])
                            self._data[k] = merged
                        else:
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

    def snapshot(self) -> dict:
        """Return a deep-ish copy of the whole state (for backup)."""
        with self._lock:
            return json.loads(json.dumps(self._data, ensure_ascii=False))

    def restore(self, state: dict) -> None:
        """Replace state after validating shape; raises ValueError when bad."""
        if not isinstance(state, dict):
            raise ValueError("state must be an object")
        for key in ("episodes", "clips", "jobs", "settings"):
            if key not in state or not isinstance(state[key], dict):
                raise ValueError(f"state.{key} must be an object")
        with self._lock:
            settings = dict(DEFAULT_SETTINGS)
            settings.update(state["settings"])
            self._data = {
                "settings": settings,
                "episodes": dict(state["episodes"]),
                "clips": dict(state["clips"]),
                "jobs": dict(state["jobs"]),
            }
            self._save()

    # -- settings ----------------------------------------------------------
    def settings(self) -> dict:
        with self._lock:
            merged = dict(DEFAULT_SETTINGS)
            merged.update(self._data.get("settings", {}))
            return merged

    def update_settings(self, **kw) -> dict:
        with self._lock:
            self._data["settings"].update(kw)
            self._save()
            return dict(self._data["settings"])

    # -- episodes ----------------------------------------------------------
    def upsert_episode(self, ep: dict) -> dict:
        with self._lock:
            existing = self._data["episodes"].get(ep["id"], {})
            # preserve clips list unless explicitly overwritten
            if "clips" not in ep and "clips" in existing:
                ep = {**ep, "clips": existing["clips"]}
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

    def delete_episode(self, ep_id: str) -> tuple[dict, int]:
        """Remove episode + its clip records. Returns (episode, clips_removed)."""
        with self._lock:
            ep = self._data["episodes"].pop(ep_id, None)
            if not ep:
                raise KeyError(ep_id)
            removed = 0
            for cid in list(self._data["clips"]):
                if self._data["clips"][cid].get("episode_id") == ep_id:
                    del self._data["clips"][cid]
                    removed += 1
            self._save()
            return dict(ep), removed

    def remove_episode_clips(self, ep_id: str) -> None:
        with self._lock:
            for cid in list(self._data["clips"]):
                if self._data["clips"][cid]["episode_id"] == ep_id:
                    del self._data["clips"][cid]
            if ep_id in self._data["episodes"]:
                self._data["episodes"][ep_id]["clips"] = []
            self._save()

    def episode_is_processing(self, ep_id: str) -> bool:
        with self._lock:
            ep = self._data["episodes"].get(ep_id)
            if ep and ep.get("status") == "processing":
                return True
            for j in self._data["jobs"].values():
                if j.get("episode_id") == ep_id and j.get("status") in ("queued", "running"):
                    return True
            return False

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

    def update_clip(self, cid: str, **kw) -> dict | None:
        with self._lock:
            if cid in self._data["clips"]:
                self._data["clips"][cid].update(kw)
                self._save()
                return dict(self._data["clips"][cid])
            return None

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

    def jobs(self) -> list[dict]:
        """All jobs, newest first (for /api/jobs + dashboard)."""
        with self._lock:
            all_jobs = [dict(j) for j in self._data["jobs"].values()]
        all_jobs.sort(key=lambda j: j.get("created", 0), reverse=True)
        return all_jobs

    def active_jobs(self) -> list[dict]:
        with self._lock:
            return [
                dict(j)
                for j in self._data["jobs"].values()
                if j["status"] in ("queued", "running")
            ]

    def active_jobs_for_episode(self, ep_id: str) -> list[dict]:
        with self._lock:
            return [
                dict(j)
                for j in self._data["jobs"].values()
                if j.get("episode_id") == ep_id and j.get("status") in ("queued", "running")
            ]

    def cancel_job(self, jid: str) -> dict | None:
        """Mark a queued job cancelled. Returns job or None if unknown."""
        with self._lock:
            j = self._data["jobs"].get(jid)
            if not j:
                return None
            j["status"] = "cancelled"
            j["step"] = "cancelled"
            j["message"] = "Cancelled"
            self._save()
            return dict(j)

    def prune_jobs(self, keep: int = 40) -> None:
        with self._lock:
            jobs = sorted(
                self._data["jobs"].values(), key=lambda j: j["created"], reverse=True
            )
            for j in jobs[keep:]:
                self._data["jobs"].pop(j["id"], None)
            self._save()
