"""Shared service helpers used by BOTH servers (FastAPI + stdlib).

Centralizing storage/search/chapters/export logic here keeps the two
servers' behavior identical.
"""
from __future__ import annotations

import csv
import io
from pathlib import Path

from . import config, demo, highlights, youtube
from .store import Store
from .transcripts import Segment, to_sentences
from .validate import ValidationError


def episode_segments(ep: dict) -> tuple[list[Segment], str]:
    if demo.is_demo(ep):
        return demo.demo_segments(ep["id"]), "demo"
    return youtube.get_transcript(ep["id"], ep["url"])


def episode_segments_offline(ep: dict, store: Store | None = None) -> tuple[list[Segment], str]:
    """Offline transcript: demo or cached only (no network)."""
    if demo.is_demo(ep):
        try:
            return demo.demo_segments(ep["id"]), "demo"
        except KeyError:
            return [], "demo"
    cached = youtube.load_cached_transcript(ep["id"])
    if cached:
        return cached, "cached"
    return [], "missing"


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------
def _dir_stats(path: Path) -> dict:
    count = 0
    total = 0
    try:
        for p in path.iterdir():
            if p.is_file():
                count += 1
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return {"count": count, "bytes": total}


def storage_info() -> dict:
    clips = _dir_stats(config.CLIPS_DIR)
    thumbs = _dir_stats(config.THUMBS_DIR)
    subs = _dir_stats(config.SUBS_DIR)
    media = _dir_stats(config.MEDIA_DIR)
    total = clips["bytes"] + thumbs["bytes"] + subs["bytes"] + media["bytes"]
    return {
        "clips": clips,
        "thumbs": thumbs,
        "subs": subs,
        "media": media,
        "total_bytes": total,
    }


def clean_storage(target: str) -> dict:
    """Remove files for target dir; measure bytes BEFORE deleting.

    GLOBAL RULE: freed_bytes must equal the real deleted sizes, so each
    file's size is stat'ed before unlink.
    """
    mapping = {
        "subs": config.SUBS_DIR,
        "thumbs": config.THUMBS_DIR,
        "clips": config.CLIPS_DIR,
    }
    if target not in mapping:
        raise ValidationError("target must be one of: subs, thumbs, clips")
    directory = mapping[target]
    removed = 0
    freed_bytes = 0
    try:
        entries = list(directory.iterdir())
    except OSError:
        return {"removed": 0, "freed_bytes": 0}
    for path in entries:
        if not path.is_file():
            continue
        # Measure BEFORE deleting so freed_bytes is exact.
        try:
            size = path.stat().st_size
        except OSError:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        freed_bytes += size
        removed += 1
    return {"removed": removed, "freed_bytes": freed_bytes}


# Also expose under an alternate name for test discovery.
clean_target = clean_storage


# --------------------------------------------------------------------------
# Search (offline over demo + cached transcripts)
# --------------------------------------------------------------------------
def search_transcripts(store: Store, q: str, limit: int = 50) -> dict:
    query = (q or "").strip()
    if len(query) < 2:
        raise ValidationError("q must be at least 2 characters")
    ql = query.lower()
    results: list[dict] = []
    for ep in store.episodes():
        segments, _src = episode_segments_offline(ep, store)
        if not segments:
            continue
        for seg in segments:
            if ql in (seg.text or "").lower():
                results.append({
                    "episode_id": ep["id"],
                    "episode_title": ep.get("title", ""),
                    "start": round(seg.start, 2),
                    "end": round(seg.end, 2),
                    "text": seg.text,
                })
                if len(results) >= limit:
                    return {"results": results}
    return {"results": results}


# --------------------------------------------------------------------------
# Chapters + export
# --------------------------------------------------------------------------
def get_chapters(ep: dict) -> dict:
    # Prefer yt-dlp chapters when stored on the episode record.
    stored = ep.get("chapters")
    if isinstance(stored, list) and stored:
        chapters = []
        for c in stored:
            try:
                chapters.append({
                    "start": round(float(c.get("start", 0)), 2),
                    "title": str(c.get("title", ""))[:120] or "Chapter",
                })
            except (TypeError, ValueError):
                continue
        if chapters:
            return {"chapters": chapters}
    # Offline derivation from transcript.
    if demo.is_demo(ep):
        segments = demo.demo_segments(ep["id"])
    else:
        segments = youtube.load_cached_transcript(ep["id"])
        if not segments:
            try:
                segments, _s = youtube.get_transcript(ep["id"], ep["url"])
            except Exception:
                segments = []
    sentences = to_sentences(segments)
    return {"chapters": highlights.build_chapters(sentences)}


def build_moments_csv(ep: dict, count: int, profile: str) -> str:
    if not (1 <= count <= 12):
        raise ValidationError("count must be an integer between 1 and 12")
    if profile not in config.PROFILES:
        raise ValidationError("profile must be one of: viral, story, facts, energy")
    if demo.is_demo(ep):
        segments = demo.demo_segments(ep["id"])
    else:
        try:
            segments, _s = youtube.get_transcript(ep["id"], ep["url"])
        except youtube.TranscriptUnavailable as exc:
            raise ValidationError(str(exc)[:300])
        except Exception as exc:
            raise RuntimeError(str(exc))
    sentences = to_sentences(segments)
    moments = highlights.find_highlights(
        sentences, count=count, min_dur=float(config.MIN_CLIP_SECONDS),
        max_dur=float(config.MAX_CLIP_SECONDS), profile=profile,
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["start", "end", "duration", "title", "score", "reasons"])
    for m in moments:
        writer.writerow([
            round(m.start, 2), round(m.end, 2), round(m.duration, 2),
            m.title, m.score, " | ".join(m.reasons),
        ])
    return buf.getvalue()


# --------------------------------------------------------------------------
# Clip SRT
# --------------------------------------------------------------------------
def get_clip_srt_text(clip: dict, store: Store) -> str | None:
    """Return SRT text for a clip: cached file first, else regenerate."""
    srt_path = config.SUBS_DIR / f"{clip['id']}.srt"
    if srt_path.is_file():
        try:
            text = srt_path.read_text(encoding="utf-8")
            if text.strip():
                return text
        except OSError:
            pass
    # Regenerate from transcript window (offline; speed-aware if stored).
    ep = store.get_episode(clip.get("episode_id", ""))
    if not ep:
        return None
    segments, _s = episode_segments_offline(ep, store)
    if not segments:
        return None
    from .smart import warp_time as _warp
    from .transcripts import Segment as _Seg
    from .transcripts import segments_for_window, segments_to_srt

    render = clip.get("render") or {}
    speed = float(render.get("speed") or 1.0)
    window = segments_for_window(segments, float(clip.get("start", 0)), float(clip.get("end", 0)))
    rebased = []
    for s in window:
        rs = max(s.start - float(clip.get("start", 0)), 0.0)
        re_ = max(s.end - float(clip.get("start", 0)), 0.0)
        os_ = _warp(rs, [], speed)
        oe = _warp(re_, [], speed)
        if oe - os_ > 0.1:
            rebased.append(_Seg(os_, oe, s.text))
    text = segments_to_srt(rebased, clip_start=0.0, speed=1.0)
    if text:
        try:
            srt_path.write_text(text, encoding="utf-8")
        except OSError:
            pass
        return text
    return None
