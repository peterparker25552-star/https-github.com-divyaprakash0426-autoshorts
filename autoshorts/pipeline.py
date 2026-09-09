"""Pipeline orchestration: one background job per 'generate shorts' request.

Steps: transcript -> highlights -> media -> render each clip (+ thumbnail),
with progress reported through the store so the UI can poll it.
"""
from __future__ import annotations

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

    # ------------------------------------------------------------------
    def start_job(self, episode_id: str, params: dict) -> dict:
        job = self.store.create_job(episode_id, params)
        self.store.update_episode(episode_id, status="processing")
        t = threading.Thread(target=self._run_job, args=(job["id"],), daemon=True)
        t.start()
        return job

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
            self._generate(ep, params, job_id)
            self.store.update_job(job_id, status="done", progress=1.0, step="done")
            self.store.update_episode(ep_id, status="done")
        except Exception as e:  # surface readable errors to the UI
            self.store.update_job(
                job_id, status="error", error=str(e)[:500], step="error"
            )
            self.store.update_episode(ep_id, status="error", error=str(e)[:500])
            traceback.print_exc()
        finally:
            self.store.prune_jobs()

    # ------------------------------------------------------------------
    def _generate(self, ep: dict, params: dict, job_id: str) -> None:
        def progress(step: str, frac: float, msg: str = "") -> None:
            self.store.update_job(job_id, status="running", step=step,
                                  progress=round(frac, 3), message=msg)

        count = int(params.get("count", config.DEFAULT_CLIP_COUNT))
        min_dur = float(params.get("min_dur", config.MIN_CLIP_SECONDS))
        max_dur = float(params.get("max_dur", config.MAX_CLIP_SECONDS))
        style = params.get("style", config.DEFAULT_STYLE)
        height = config.RENDER_HEIGHTS.get(
            params.get("quality", config.DEFAULT_QUALITY), 1280
        )

        # 1) transcript ---------------------------------------------------
        progress("transcript", 0.05, "Fetching transcript…")
        if demo.is_demo(ep):
            segments = demo.demo_segments(ep["id"])
        else:
            try:
                segments, _src = youtube.get_transcript(ep["id"], ep["url"])
            except youtube.TranscriptUnavailable as e:
                raise RuntimeError(f"Transcript unavailable: {e}")
        if not segments:
            raise RuntimeError("No transcript could be produced for this episode.")

        # 2) highlights ---------------------------------------------------
        progress("highlights", 0.2, "Scoring moments for viral potential…")
        sentences = to_sentences(segments)
        if not sentences:
            raise RuntimeError("Transcript parsed to zero sentences.")
        moments = highlights.find_highlights(
            sentences, count=count, min_dur=min_dur, max_dur=max_dur
        )
        if not moments:
            raise RuntimeError(
                "No highlight moments found — try longer clip durations."
            )

        # 3) source media -------------------------------------------------
        progress("media", 0.35, "Preparing source media…")
        if demo.is_demo(ep):
            media = demo.prepare_demo_media(ep)
        else:
            media = youtube.download_video(ep["id"], ep["url"])

        # 4) render clips -------------------------------------------------
        total = len(moments)
        for i, m in enumerate(moments):
            progress(
                "render",
                0.45 + 0.5 * (i / max(total, 1)),
                f"Rendering short {i + 1} of {total}…",
            )
            clip_id = f"{ep['id'][:24]}-{uuid.uuid4().hex[:6]}"
            ass_path = make_ass(
                segments_for_window(segments, m.start, m.end),
                m.start,
                m.end,
                config.SUBS_DIR / f"{clip_id}.ass",
                play_w=round(height * 9 / 16 / 2) * 2,
                play_h=height,
            )
            clip_path = config.CLIPS_DIR / f"{clip_id}.mp4"
            render_clip(
                media, m.start, m.end, clip_path, ass_path,
                style=style, height=height,
            )
            thumb_path = config.THUMBS_DIR / f"{clip_id}.jpg"
            try:
                extract_thumbnail(clip_path, thumb_path)
            except Exception:
                thumb_path = None  # non-fatal

            self.store.add_clip(
                {
                    "id": clip_id,
                    "episode_id": ep["id"],
                    "episode_title": ep["title"],
                    "title": m.title,
                    "start": round(m.start, 2),
                    "end": round(m.end, 2),
                    "duration": round(m.end - m.start, 2),
                    "score": m.score,
                    "reasons": m.reasons,
                    "style": style,
                    "height": height,
                    "file": clip_path.name,
                    "thumb": thumb_path.name if thumb_path else None,
                }
            )
        progress("done", 1.0, f"Generated {total} shorts")
