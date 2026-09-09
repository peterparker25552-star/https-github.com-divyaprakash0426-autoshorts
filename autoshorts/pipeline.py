"""Serial pipeline orchestration for automatic and manual clip jobs.

Steps stay transcript -> highlights -> media -> render. A single background
worker processes the queue so render jobs and YouTube requests never fan out.
"""
from __future__ import annotations

import queue
import threading
import traceback
import uuid
from pathlib import Path

from . import config, demo, highlights, youtube
from .ffmpeg import extract_thumbnail, make_ass, render_clip
from .store import Store
from .transcripts import Segment, segments_for_window, to_sentences


class Pipeline:
    def __init__(self, store: Store):
        self.store = store
        self._queue: queue.Queue[str] = queue.Queue()
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="autoshorts-worker",
            daemon=True,
        )
        self._worker.start()

    # ------------------------------------------------------------------
    def start_job(self, episode_id: str, params: dict) -> dict:
        job = self.store.create_job(episode_id, params)
        self.store.update_episode(episode_id, status="processing", error=None)
        self._queue.put(job["id"])
        return job

    def _worker_loop(self) -> None:
        while True:
            job_id = self._queue.get()
            try:
                self._run_job(job_id)
            finally:
                self._queue.task_done()

    # ------------------------------------------------------------------
    def _run_job(self, job_id: str) -> None:
        job = self.store.job(job_id)
        if not job:
            return
        ep_id = job["episode_id"]
        params = job["params"]
        ep = self.store.get_episode(ep_id)
        if not ep:
            self.store.update_job(job_id, status="error", error="Episode not found")
            return
        try:
            if params.get("kind") == "manual":
                self._generate_manual(ep, params, job_id)
            else:
                self._generate_auto(ep, params, job_id)
            self.store.update_job(
                job_id, status="done", progress=1.0, step="done"
            )
            self.store.update_episode(ep_id, status="done", error=None)
        except Exception as exc:  # surface readable errors to the UI
            self.store.update_job(
                job_id, status="error", error=str(exc)[:500], step="error"
            )
            self.store.update_episode(
                ep_id, status="error", error=str(exc)[:500]
            )
            traceback.print_exc()
        finally:
            self.store.prune_jobs()

    @staticmethod
    def _progress(store: Store, job_id: str, step: str, frac: float, msg: str) -> None:
        store.update_job(
            job_id,
            status="running",
            step=step,
            progress=round(frac, 3),
            message=msg,
        )

    @staticmethod
    def _render_settings(params: dict) -> tuple[str, int]:
        style = params.get("style", config.DEFAULT_STYLE)
        height = config.RENDER_HEIGHTS.get(
            params.get("quality", config.DEFAULT_QUALITY), 1280
        )
        return style, height

    @staticmethod
    def _prepare_media(ep: dict) -> Path:
        if demo.is_demo(ep):
            return demo.prepare_demo_media(ep)
        return youtube.download_video(ep["id"], ep["url"])

    # ------------------------------------------------------------------
    def _generate_auto(self, ep: dict, params: dict, job_id: str) -> None:
        count = int(params.get("count", config.DEFAULT_CLIP_COUNT))
        min_dur = float(params.get("min_dur", config.MIN_CLIP_SECONDS))
        max_dur = float(params.get("max_dur", config.MAX_CLIP_SECONDS))
        profile = params.get("profile", "viral")
        style, height = self._render_settings(params)

        # 1) transcript ---------------------------------------------------
        self._progress(self.store, job_id, "transcript", 0.05, "Fetching transcript…")
        if demo.is_demo(ep):
            segments = demo.demo_segments(ep["id"])
        else:
            try:
                segments, _source = youtube.get_transcript(ep["id"], ep["url"])
            except youtube.TranscriptUnavailable as exc:
                raise RuntimeError(f"Transcript unavailable: {exc}") from exc
        if not segments:
            raise RuntimeError("No transcript could be produced for this episode.")

        # 2) highlights ---------------------------------------------------
        self._progress(
            self.store,
            job_id,
            "highlights",
            0.2,
            f"Scoring {profile} moments…",
        )
        sentences = to_sentences(segments)
        if not sentences:
            raise RuntimeError("Transcript parsed to zero sentences.")
        moments = highlights.find_highlights(
            sentences,
            count=count,
            min_dur=min_dur,
            max_dur=max_dur,
            profile=profile,
        )
        if not moments:
            raise RuntimeError(
                "No highlight moments found — try longer clip durations."
            )

        # 3) source media -------------------------------------------------
        self._progress(self.store, job_id, "media", 0.35, "Preparing source media…")
        media = self._prepare_media(ep)

        # 4) render clips -------------------------------------------------
        total = len(moments)
        for index, moment in enumerate(moments):
            self._progress(
                self.store,
                job_id,
                "render",
                0.45 + 0.5 * (index / max(total, 1)),
                f"Rendering short {index + 1} of {total}…",
            )
            self._render_one(ep, media, segments, moment, style, height)
        self._progress(
            self.store, job_id, "done", 1.0, f"Generated {total} shorts"
        )

    # ------------------------------------------------------------------
    def _generate_manual(self, ep: dict, params: dict, job_id: str) -> None:
        style, height = self._render_settings(params)

        # 1) transcript: manual clips use only an existing cache. Captions
        # are optional and a network transcript request must not block a cut.
        self._progress(
            self.store, job_id, "transcript", 0.05, "Loading cached transcript…"
        )
        if demo.is_demo(ep):
            segments = demo.demo_segments(ep["id"])
        else:
            segments = youtube.load_cached_transcript(ep["id"])

        # 2) highlights: preserve the common progress contract while using
        # the user's chosen range rather than running the scorer.
        self._progress(
            self.store, job_id, "highlights", 0.2, "Preparing manual range…"
        )
        start, end = self._clamp_manual_range(
            float(params["start"]), float(params["end"]), float(ep.get("duration") or 0)
        )
        moment = highlights.Highlight(
            start=start,
            end=end,
            score=0,
            title=(params.get("title") or "Manual clip").strip() or "Manual clip",
            reasons=["manual pick"],
        )

        # 3) source media -------------------------------------------------
        self._progress(self.store, job_id, "media", 0.35, "Preparing source media…")
        media = self._prepare_media(ep)

        # 4) render -------------------------------------------------------
        self._progress(self.store, job_id, "render", 0.45, "Rendering manual clip…")
        self._render_one(ep, media, segments, moment, style, height)
        self._progress(self.store, job_id, "done", 1.0, "Generated manual clip")

    @staticmethod
    def _clamp_manual_range(start: float, end: float, episode_duration: float) -> tuple[float, float]:
        """Clamp a manual cut to the source and a 5–180 second duration."""
        start = max(0.0, start)
        if episode_duration > 0:
            start = min(start, max(0.0, episode_duration - 5.0))

        end = max(end, start + 5.0)
        end = min(end, start + 180.0)
        if episode_duration > 0:
            end = min(end, episode_duration)
            if end - start < 5.0:
                start = max(0.0, end - 5.0)
        return start, end

    # ------------------------------------------------------------------
    def _render_one(
        self,
        ep: dict,
        media: Path,
        segments: list[Segment],
        moment: highlights.Highlight,
        style: str,
        height: int,
    ) -> dict:
        """Render and persist one automatic or manual highlight."""
        clip_id = f"{ep['id'][:24]}-{uuid.uuid4().hex[:6]}"
        ass_path = make_ass(
            segments_for_window(segments, moment.start, moment.end),
            moment.start,
            moment.end,
            config.SUBS_DIR / f"{clip_id}.ass",
            play_w=round(height * 9 / 16 / 2) * 2,
            play_h=height,
        )
        clip_path = config.CLIPS_DIR / f"{clip_id}.mp4"
        render_clip(
            media,
            moment.start,
            moment.end,
            clip_path,
            ass_path,
            style=style,
            height=height,
        )
        thumb_path = config.THUMBS_DIR / f"{clip_id}.jpg"
        try:
            extract_thumbnail(clip_path, thumb_path)
        except Exception:
            thumb_path = None  # non-fatal

        return self.store.add_clip(
            {
                "id": clip_id,
                "episode_id": ep["id"],
                "episode_title": ep["title"],
                "title": moment.title,
                "start": round(moment.start, 2),
                "end": round(moment.end, 2),
                "duration": round(moment.end - moment.start, 2),
                "score": moment.score,
                "reasons": moment.reasons,
                "style": style,
                "height": height,
                "file": clip_path.name,
                "thumb": thumb_path.name if thumb_path else None,
            }
        )
