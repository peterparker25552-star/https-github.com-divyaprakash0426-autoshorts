"""Serial pipeline orchestration for automatic and manual clip jobs.

Steps stay transcript -> highlights -> media -> render. A single background
worker processes the queue so render jobs and YouTube requests never fan out.
Jobs cancelled while still queued are skipped by the worker and never run.
"""
from __future__ import annotations

import queue
import threading
import traceback
import uuid
from pathlib import Path

from . import audioswap, beats, config, demo, engine, highlights, logofx
from . import maintenance, titles, youtube
from .ffmpeg import (
    extract_thumbnail,
    loudness_waveform,
    make_ass,
    render_clip,
)
from .store import Store
from .transcripts import Segment, segments_for_window, to_sentences




def _bounded(value, default: float, bounds: tuple[float, float]) -> float:
    """Float clamp for the silence tuner (never fails a long render)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    lo, hi = bounds
    if not lo <= number <= hi:
        return float(default)
    return number


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

        # Cancelled while queued → skip silently; the job keeps its status.
        if job.get("status") == "cancelled":
            maintenance.reset_episode_if_idle(self.store, ep_id)
            return

        params = job["params"]
        ep = self.store.get_episode(ep_id)
        if not ep:
            self.store.update_job(job_id, status="error", error="Episode not found")
            maintenance.reset_episode_if_idle(self.store, ep_id)
            return
        try:
            if params.get("kind") == "manual":
                self._generate_manual(ep, params, job_id)
            else:
                self._generate_auto(ep, params, job_id)
            self.store.update_job(
                job_id, status="done", progress=1.0, step="done"
            )
            self._settle_episode(ep_id, "done")
        except Exception as exc:  # surface readable errors to the UI
            self.store.update_job(
                job_id, status="error", error=str(exc)[:500], step="error"
            )
            self._settle_episode(ep_id, "error", message=str(exc)[:500])
            traceback.print_exc()
        finally:
            self.store.prune_jobs()

    def _settle_episode(self, ep_id: str, status: str, message: str = "") -> None:
        """Only mark the episode finished when no other job is waiting."""
        if self.store.has_active_jobs(ep_id):
            self.store.update_episode(ep_id, status="processing")
            return
        self.store.update_episode(
            ep_id, status=status, error=None if status == "done" else message
        )

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
    def _render_settings(params: dict) -> dict:
        """Resolve the render options a job asked for.

        Unknown values fall back to the project defaults instead of failing a
        long render; the API layers validate first and return 422 anyway.
        """
        style = str(params.get("style") or config.DEFAULT_STYLE)
        if style not in config.STYLES:
            style = config.DEFAULT_STYLE
        quality = str(params.get("quality") or config.DEFAULT_QUALITY)
        if quality not in config.RENDER_HEIGHTS:
            quality = config.DEFAULT_QUALITY
        brand = str(params.get("captions_brand") or config.DEFAULT_CAPTION_BRAND)
        if brand not in config.CAPTION_BRANDS:
            brand = config.DEFAULT_CAPTION_BRAND
        fmt = str(params.get("format") or config.DEFAULT_FORMAT)
        if fmt not in config.OUTPUT_SIZES:
            fmt = config.DEFAULT_FORMAT
        captions = str(params.get("captions") or config.DEFAULT_CAPTIONS)
        if captions not in config.CAPTION_STYLES:
            captions = config.DEFAULT_CAPTIONS
        captions_pos = str(
            params.get("captions_pos") or config.DEFAULT_CAPTIONS_POS
        )
        if captions_pos not in config.CAPTION_POSITIONS:
            captions_pos = config.DEFAULT_CAPTIONS_POS
        try:
            speed = float(params.get("speed", config.DEFAULT_SPEED))
        except (TypeError, ValueError):
            speed = config.DEFAULT_SPEED
        lo, hi = config.SPEED_RANGE
        if not lo <= speed <= hi:
            speed = config.DEFAULT_SPEED
        # T4 logo remover: a bad spec must never stop a render, so it is
        # re-validated here (the API already 422'd it) and dropped on error.
        logo = None
        raw_logo = params.get("logo_box")
        if isinstance(raw_logo, dict) and raw_logo:
            try:
                logo = logofx.clean_spec(raw_logo)
            except Exception:
                logo = None
        # T3 audio swap: a track deleted after queueing just means "no swap".
        track = None
        track_id = params.get("audio_track")
        if isinstance(track_id, str) and track_id:
            track = audioswap.track_file(track_id)
        mix = str(params.get("audio_mix") or config.DEFAULT_AUDIO_MIX)
        if mix not in config.AUDIO_MIXES:
            mix = config.DEFAULT_AUDIO_MIX
        width, height = config.OUTPUT_SIZES[fmt][quality]
        return {
            "style": style,
            "quality": quality,
            "format": fmt,
            "width": width,
            "height": height,
            "captions": captions,
            "captions_pos": captions_pos,
            "captions_box": bool(params.get("captions_box", False)),
            "captions_brand": brand,
            "speed": speed,
            "progress": bool(params.get("progress", False)),
            "silence": bool(params.get("silence", False)),
            "loud": bool(params.get("loud", False)),
            "logo_box": logo,
            "sync_beats": bool(params.get("sync_beats", False)),
            "audio_track": track_id if track else None,
            "audio_track_path": str(track) if track else None,
            "audio_mix": mix if track else config.DEFAULT_AUDIO_MIX,
            "silence_noise": _bounded(
                params.get("silence_noise"), config.DEFAULT_SILENCE_NOISE,
                config.SILENCE_NOISE_RANGE,
            ),
            "silence_min": _bounded(
                params.get("silence_min"), config.DEFAULT_SILENCE_MIN,
                config.SILENCE_MIN_RANGE,
            ),
        }


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
        settings = self._render_settings(params)

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

        # 3b) T3 beat sync - snap the chosen windows onto the loudness peaks.
        moments = self._snap_moments(ep, media, moments, settings, job_id)

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
            self._render_one(
                ep, media, segments, moment, settings, profile=profile
            )
        self._progress(
            self.store, job_id, "done", 1.0, f"Generated {total} shorts"
        )

    # ------------------------------------------------------------------
    def _generate_manual(self, ep: dict, params: dict, job_id: str) -> None:
        settings = self._render_settings(params)
        profile = params.get("profile", "viral")

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
            signals=highlights.signals_for_window(
                to_sentences(segments), start, end, profile
            ),
        )

        # 3) source media -------------------------------------------------
        self._progress(self.store, job_id, "media", 0.35, "Preparing source media…")
        media = self._prepare_media(ep)

        # 4) render -------------------------------------------------------
        self._progress(self.store, job_id, "render", 0.45, "Rendering manual clip…")
        # 3b) T3 beat sync also honours a manual cut - the range is the user's,
        # the boundary just lands on the nearest downbeat.
        snapped = self._snap_moments(ep, media, [moment], settings, job_id)
        moment = snapped[0] if snapped else moment

        # 4) render -------------------------------------------------------
        self._render_one(ep, media, segments, moment, settings, profile=profile)
        self._progress(self.store, job_id, "done", 1.0, "Generated manual clip")

    # ------------------------------------------------------------------
    def _snap_moments(self, ep, media, moments, settings: dict, job_id: str) -> list:
        """Move each window boundary to the nearest beat (T3).

        Fully optional and fully safe: no toggle, no media, no beats or any
        analysis error returns the moments untouched, so a render never depends
        on beat detection succeeding.
        """
        if not settings.get("sync_beats") or not moments:
            return moments
        try:
            info = beats.beats_for_media(media)
        except Exception:
            return moments
        markers = info.get("beats") or []
        if not markers:
            return moments
        out = []
        moved = 0
        for moment in moments:
            start, end, changed = beats.snap_to_beats(moment.start, moment.end, markers)
            if not changed:
                out.append(moment)
                continue
            moved += 1
            out.append(highlights.Highlight(
                start=start,
                end=end,
                score=moment.score,
                title=moment.title,
                reasons=list(moment.reasons)
                + [f"beat-synced ({len(markers)} markers)"],
                sentences=[
                    sentence for sentence in (moment.sentences or [])
                    if sentence.end > start and sentence.start < end
                ] or moment.sentences,
                signals=moment.signals,
            ))
        try:
            self.store.update_job(
                job_id, message=f"Beat sync moved {moved} of {len(moments)} cut(s)"
            )
        except Exception:
            pass
        return out

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
        settings: dict,
        profile: str = "viral",
    ) -> dict:
        """Render and persist one automatic or manual highlight."""
        clip_id = f"{ep['id'][:24]}-{uuid.uuid4().hex[:6]}"
        window_segments = segments_for_window(segments, moment.start, moment.end)
        ass_path = make_ass(
            window_segments,
            moment.start,
            moment.end,
            config.SUBS_DIR / f"{clip_id}.ass",
            play_w=settings["width"],
            play_h=settings["height"],
            caption_style=settings["captions"],
            speed=settings["speed"],
            pos=settings["captions_pos"],
            box=settings["captions_box"],
            brand=settings.get("captions_brand", config.DEFAULT_CAPTION_BRAND),
        )
        clip_path = config.CLIPS_DIR / f"{clip_id}.mp4"
        render_clip(
            media,
            moment.start,
            moment.end,
            clip_path,
            ass_path,
            style=settings["style"],
            width=settings["width"],
            height=settings["height"],
            speed=settings["speed"],
            progress=settings["progress"],
            silence=settings["silence"],
            loud=settings["loud"],
            fmt=settings["format"],
            logo=settings.get("logo_box"),
            track=settings.get("audio_track_path"),
            audio_mix=settings.get("audio_mix", config.DEFAULT_AUDIO_MIX),
            silence_noise=settings.get("silence_noise", config.DEFAULT_SILENCE_NOISE),
            silence_min=settings.get("silence_min", config.DEFAULT_SILENCE_MIN),
        )

        # 24 loudness bars for the card UI (empty list on any failure).
        try:
            waveform = loudness_waveform(clip_path)
        except Exception:
            waveform = []

        thumb_path = config.THUMBS_DIR / f"{clip_id}.jpg"
        try:
            extract_thumbnail(clip_path, thumb_path)
        except Exception:
            thumb_path = None  # non-fatal

        duration = round(moment.end - moment.start, 2)
        window_text = " ".join(
            sentence.text for sentence in (moment.sentences or [])
        ) or " ".join(segment.text for segment in window_segments)
        pack = titles.generate_pack(
            moment.title,
            window_text,
            str(ep.get("title") or ""),
            profile,
            duration,
        )
        # T5: when a provider is configured the pack is rewritten through the
        # engine (short timeout); any failure keeps these offline words.
        pack, engine_name, engine_notice = engine.refine_pack(
            pack,
            f"{ep.get('title') or ''} | {moment.title} | {duration:.0f}s clip",
            self.store.settings(),
        )

        render_opts = {
            "style": settings["style"],
            "quality": settings["quality"],
            "format": settings["format"],
            "captions": settings["captions"],
            "captions_pos": settings["captions_pos"],
            "captions_box": settings["captions_box"],
            "captions_brand": settings["captions_brand"],
            "speed": settings["speed"],
            "progress": settings["progress"],
            "silence": settings["silence"],
            "loud": settings["loud"],
            "logo_box": settings["logo_box"],
            "sync_beats": settings["sync_beats"],
            "audio_track": settings["audio_track"],
            "audio_mix": settings["audio_mix"],
            "silence_noise": settings["silence_noise"],
            "silence_min": settings["silence_min"],
        }
        return self.store.add_clip(
            {
                "id": clip_id,
                "episode_id": ep["id"],
                "episode_title": ep["title"],
                "title": moment.title,
                "start": round(moment.start, 2),
                "end": round(moment.end, 2),
                "duration": duration,
                "score": moment.score,
                "reasons": moment.reasons,
                "breakdown": dict(moment.signals or {}),
                "pack": pack,
                "style": settings["style"],
                "format": settings["format"],
                "captions": settings["captions"],
                "captions_pos": settings["captions_pos"],
                "captions_box": settings["captions_box"],
                "captions_brand": settings["captions_brand"],
                "quality": settings["quality"],
                "speed": settings["speed"],
                "progress": settings["progress"],
                "silence": settings["silence"],
                "loud": settings["loud"],
                "sync_beats": settings["sync_beats"],
                "audio_mix": settings["audio_mix"],
                "logo_box": settings["logo_box"],
                "width": settings["width"],
                "height": settings["height"],
                "render": dict(render_opts),
                "waveform": waveform,
                "audio_track": settings["audio_track"],
                "logo": logofx.describe(settings["logo_box"], settings["width"],
                                        settings["height"]),
                "engine": engine_name,
                "engine_notice": engine_notice,
                "file": clip_path.name,
                "thumb": thumb_path.name if thumb_path else None,
            }
        )
