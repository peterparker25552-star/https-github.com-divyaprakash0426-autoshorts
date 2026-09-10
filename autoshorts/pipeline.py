"""Serial pipeline orchestration for automatic and manual clip jobs.

Steps stay transcript -> highlights -> media -> render. A single background
worker processes the queue so render jobs and YouTube requests never fan out.

Jobs flow queued → running → done|error; cancelled jobs are skipped by the
worker and never run.
"""
from __future__ import annotations

import queue
import threading
import traceback
import uuid
from pathlib import Path

from . import config, demo, highlights, youtube
from .ffmpeg import (
    detect_silences,
    extract_thumbnail,
    get_waveform,
    kept_duration,
    make_ass,
    render_clip,
)
from .store import Store
from .transcripts import Segment, segments_for_window, segments_to_srt, to_sentences


def default_render_opts() -> dict:
    return {
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


def render_opts_from_params(params: dict) -> dict:
    base = default_render_opts()
    for k in base:
        if k in params:
            base[k] = params[k]
    return base


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
        # Cancelled jobs are skipped by the worker and never run.
        if job.get("status") == "cancelled":
            return
        ep_id = job["episode_id"]
        params = job["params"]
        ep = self.store.get_episode(ep_id)
        if not ep:
            self.store.update_job(job_id, status="error", error="Episode not found")
            return
        try:
            kind = params.get("kind") or "auto"
            if kind == "manual":
                self._generate_manual(ep, params, job_id)
            elif kind == "rerender":
                self._generate_rerender(ep, params, job_id)
            else:
                self._generate_auto(ep, params, job_id)
            # Don't overwrite a cancel that landed mid-run... (only queued
            # jobs are cancellable, so a running job finishing is done.)
            cur = self.store.job(job_id)
            if cur and cur.get("status") != "cancelled":
                self.store.update_job(
                    job_id, status="done", progress=1.0, step="done"
                )
            self._maybe_finish_episode(ep_id)
        except Exception as exc:  # surface readable errors to the UI
            self.store.update_job(
                job_id, status="error", error=str(exc)[:500], step="error"
            )
            # Only mark episode error if no other active jobs remain.
            if not self.store.active_jobs_for_episode(ep_id):
                self.store.update_episode(
                    ep_id, status="error", error=str(exc)[:500]
                )
            traceback.print_exc()
        finally:
            self.store.prune_jobs()

    def _maybe_finish_episode(self, ep_id: str) -> None:
        if not self.store.active_jobs_for_episode(ep_id):
            self.store.update_episode(ep_id, status="done", error=None)

    @staticmethod
    def _progress(store: Store, job_id: str, step: str, frac: float, msg: str) -> None:
        cur = store.job(job_id)
        if cur and cur.get("status") == "cancelled":
            return
        store.update_job(
            job_id,
            status="running",
            step=step,
            progress=round(frac, 3),
            message=msg,
        )

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
        opts = render_opts_from_params(params)

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
            self._render_one(ep, media, segments, moment, opts)
        self._progress(
            self.store, job_id, "done", 1.0, f"Generated {total} shorts"
        )

    # ------------------------------------------------------------------
    def _generate_manual(self, ep: dict, params: dict, job_id: str) -> None:
        opts = render_opts_from_params(params)

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
            breakdown={},
        )

        # 3) source media -------------------------------------------------
        self._progress(self.store, job_id, "media", 0.35, "Preparing source media…")
        media = self._prepare_media(ep)

        # 4) render -------------------------------------------------------
        self._progress(self.store, job_id, "render", 0.45, "Rendering manual clip…")
        self._render_one(ep, media, segments, moment, opts)
        self._progress(self.store, job_id, "done", 1.0, "Generated manual clip")

    # ------------------------------------------------------------------
    def _generate_rerender(self, ep: dict, params: dict, job_id: str) -> None:
        """Re-render a stored clip window with new render options."""
        opts = render_opts_from_params(params)
        self._progress(self.store, job_id, "transcript", 0.05, "Loading transcript…")
        if demo.is_demo(ep):
            segments = demo.demo_segments(ep["id"])
        else:
            segments = youtube.load_cached_transcript(ep["id"])
            if not segments:
                try:
                    segments, _s = youtube.get_transcript(ep["id"], ep["url"])
                except Exception:
                    segments = []
        self._progress(self.store, job_id, "highlights", 0.2, "Preparing rerender…")
        start = float(params["start"])
        end = float(params["end"])
        moment = highlights.Highlight(
            start=start,
            end=end,
            score=float(params.get("score") or 0),
            title=(params.get("title") or "Rerender").strip() or "Rerender",
            reasons=list(params.get("reasons") or ["rerender"]),
            breakdown=dict(params.get("breakdown") or {}),
        )
        self._progress(self.store, job_id, "media", 0.35, "Preparing source media…")
        media = self._prepare_media(ep)
        self._progress(self.store, job_id, "render", 0.45, "Re-rendering clip…")
        self._render_one(ep, media, segments, moment, opts)
        self._progress(self.store, job_id, "done", 1.0, "Re-rendered clip")

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
        opts: dict,
    ) -> dict:
        """Render and persist one automatic, manual or rerender highlight."""
        clip_id = f"{ep['id'][:24]}-{uuid.uuid4().hex[:6]}"
        out_w, out_h = config.output_size(
            opts.get("format", config.DEFAULT_FORMAT),
            opts.get("quality", config.DEFAULT_QUALITY),
        )
        speed = float(opts.get("speed", 1.0) or 1.0)
        want_silence = bool(opts.get("silence"))

        # Silence cuts (clip-relative) detected once, shared by captions + render.
        cuts: list[tuple[float, float]] = []
        if want_silence:
            try:
                cuts = detect_silences(media, moment.start, moment.end)
            except Exception:
                cuts = []

        window_segs = segments_for_window(segments, moment.start, moment.end)
        ass_path = make_ass(
            window_segs,
            moment.start,
            moment.end,
            config.SUBS_DIR / f"{clip_id}.ass",
            play_w=out_w,
            play_h=out_h,
            captions=str(opts.get("captions", config.DEFAULT_CAPTIONS)),
            captions_pos=str(opts.get("captions_pos", config.DEFAULT_CAPTIONS_POS)),
            captions_box=bool(opts.get("captions_box", False)),
            speed=speed,
            cuts=cuts,
        )
        clip_path = config.CLIPS_DIR / f"{clip_id}.mp4"
        render_clip(
            media,
            moment.start,
            moment.end,
            clip_path,
            ass_path,
            style=str(opts.get("style", config.DEFAULT_STYLE)),
            fmt=str(opts.get("format", config.DEFAULT_FORMAT)),
            quality=str(opts.get("quality", config.DEFAULT_QUALITY)),
            captions=str(opts.get("captions", config.DEFAULT_CAPTIONS)),
            captions_pos=str(opts.get("captions_pos", config.DEFAULT_CAPTIONS_POS)),
            captions_box=bool(opts.get("captions_box", False)),
            speed=speed,
            silence=want_silence,
            loud=bool(opts.get("loud", False)),
            progress=bool(opts.get("progress", False)),
            silence_cuts=cuts,
        )
        thumb_path = config.THUMBS_DIR / f"{clip_id}.jpg"
        try:
            extract_thumbnail(clip_path, thumb_path)
        except Exception:
            thumb_path = None  # non-fatal

        # Waveform (24 bars via ebur128, [] fallback) + upload pack + SRT.
        try:
            waveform = get_waveform(clip_path)
        except Exception:
            waveform = []
        final_dur = kept_duration(moment.end - moment.start, cuts) / (speed or 1.0)
        try:
            upload_pack = highlights.build_upload_pack(
                moment.title, ep.get("title", ""), final_dur, moment.reasons
            )
        except Exception:
            upload_pack = {"titles": [moment.title], "hashtags": ["#shorts"],
                           "description": moment.title}
        srt_text = ""
        try:
            # SRT rebased to the output timeline (cuts + speed aware).
            from .smart import warp_time as _warp

            rebased: list[Segment] = []
            for s in window_segs:
                rs = max(s.start - moment.start, 0.0)
                re_ = max(s.end - moment.start, 0.0)
                os_ = _warp(rs, cuts, speed)
                oe = _warp(re_, cuts, speed)
                if oe - os_ > 0.1:
                    rebased.append(Segment(os_, oe, s.text))
            srt_text = segments_to_srt(rebased, clip_start=0.0, speed=1.0)
            (config.SUBS_DIR / f"{clip_id}.srt").write_text(srt_text, encoding="utf-8")
        except Exception:
            srt_text = ""

        breakdown = getattr(moment, "breakdown", {}) or {}
        if isinstance(breakdown, dict):
            breakdown = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in breakdown.items()}
        else:
            breakdown = {}

        return self.store.add_clip(
            {
                "id": clip_id,
                "episode_id": ep["id"],
                "episode_title": ep["title"],
                "title": moment.title,
                "start": round(moment.start, 2),
                "end": round(moment.end, 2),
                "duration": round(final_dur, 2),
                "score": moment.score,
                "reasons": moment.reasons,
                "breakdown": breakdown,
                "style": opts.get("style", config.DEFAULT_STYLE),
                "height": out_h,
                "width": out_w,
                "render": dict(opts),
                "waveform": waveform or [],
                "upload_pack": upload_pack,
                "has_srt": bool(srt_text),
                "file": clip_path.name,
                "thumb": thumb_path.name if thumb_path else None,
            }
        )
