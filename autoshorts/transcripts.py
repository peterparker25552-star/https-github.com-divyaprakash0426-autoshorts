"""Transcript parsing: yt-dlp subtitle files -> timed segments -> sentences.

Supports YouTube's json3 format (auto-captions) plus .vtt and .srt, then
splits the stream into sentences/utterances for the highlight engine.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


@dataclass
class Segment:
    start: float
    end: float
    text: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


# --------------------------------------------------------------------------
# Parsers
# --------------------------------------------------------------------------
_WS = re.compile(r"\s+")


def _clean(text: str) -> str:
    text = text.replace("\n", " ").replace("\u200b", "")
    return _WS.sub(" ", text).strip()


def parse_json3(raw: str | bytes) -> list[Segment]:
    """Parse YouTube json3 timedtext into line-level segments."""
    data = json.loads(raw)
    segments: list[Segment] = []
    prev_text = None
    for ev in data.get("events", []):
        if "segs" not in ev or ev.get("aAppend"):  # skip append/dup events
            continue
        text = _clean("".join(s.get("utf8") or "" for s in ev["segs"]))
        if not text or text == prev_text:
            continue
        start = ev.get("tStartMs", 0) / 1000.0
        dur = ev.get("dDurationMs", 0) / 1000.0
        segments.append(Segment(start, start + max(dur, 0.5), text))
        prev_text = text
    return segments


def parse_vtt(raw: str) -> list[Segment]:
    """Parse WebVTT, de-duplicating rolling auto-caption lines."""
    segments: list[Segment] = []

    def ts(tok: str) -> float:
        h, m, rest = tok.split(":")
        s, ms = rest.split(".")
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0

    for block in re.split(r"\n\s*\n", raw):
        lines = [l for l in block.strip().splitlines() if l.strip()]
        timing_idx = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if timing_idx is None:
            continue
        times, text_lines = lines[timing_idx], lines[timing_idx + 1 :]
        try:
            start_tok, end_tok = [t.strip() for t in times.split("-->")]
            start, end = ts(start_tok), ts(end_tok.split()[0])
        except Exception:
            continue
        text = _clean(" ".join(text_lines))
        if not text or text in (s.text for s in segments[-3:]):
            continue  # rolling auto-caption dedupe
        segments.append(Segment(start, end, text))
    return segments


def parse_srt(raw: str) -> list[Segment]:
    def ts(tok: str) -> float:
        h, m, rest = tok.strip().replace(",", ".").split(":")
        return int(h) * 3600 + int(m) * 60 + float(rest)

    segments: list[Segment] = []
    for block in re.split(r"\n\s*\n", raw):
        lines = [l for l in block.strip().splitlines() if l.strip()]
        if len(lines) < 2 or "-->" not in lines[1]:
            continue
        try:
            a, b = [t.strip() for t in lines[1].split("-->")]
            start, end = ts(a), ts(b)
        except Exception:
            continue
        text = _clean(" ".join(lines[2:]))
        if text:
            segments.append(Segment(start, end, text))
    return segments


def load_transcript_file(path: str | Path) -> list[Segment]:
    p = Path(path)
    raw = p.read_text(encoding="utf-8", errors="replace").lstrip("\ufeff")
    suffix = p.suffix.lower()
    if suffix == ".json3" or raw.startswith("{"):
        return parse_json3(raw)
    if suffix == ".vtt" or "WEBVTT" in raw[:200]:
        return parse_vtt(raw)
    if suffix == ".srt":
        return parse_srt(raw)
    # last-ditch: try vtt then srt
    for parser in (parse_vtt, parse_srt):
        try:
            segs = parser(raw)
            if segs:
                return segs
        except Exception:
            continue
    return []


# --------------------------------------------------------------------------
# Sentence building
# --------------------------------------------------------------------------
@dataclass
class Sentence:
    start: float
    end: float
    text: str
    words: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def wps(self) -> float:
        return len(self.words) / self.duration if self.duration > 0 else 0.0


_TERMINALS = re.compile(r"[.!?…][\"')\]]*$")
_MAX_SENTENCE_WORDS = 40


def to_sentences(segments: Iterable[Segment], max_gap: float = 1.8) -> list[Sentence]:
    """Merge caption segments into sentence-like utterances."""
    sentences: list[Sentence] = []
    buf_text: list[str] = []
    buf_start = buf_end = None
    buf_words = 0

    def flush():
        nonlocal buf_text, buf_start, buf_end, buf_words
        if buf_text and buf_start is not None:
            text = _clean(" ".join(buf_text))
            if text:
                sentences.append(
                    Sentence(
                        buf_start,
                        max(buf_end, buf_start + 0.6),
                        text,
                        text.split(),
                    )
                )
        buf_text, buf_start, buf_end, buf_words = [], None, None, 0

    for seg in segments:
        text = seg.text.strip()
        if not text:
            continue
        if buf_start is None:
            buf_start = seg.start
        # gap too large -> new utterance
        if buf_end is not None and seg.start - buf_end > max_gap:
            flush()
            buf_start = seg.start
        buf_text.append(text)
        buf_end = max(buf_end or seg.end, seg.end)
        buf_words += len(text.split())
        joined = " ".join(buf_text)
        if _TERMINALS.search(joined.strip()) or buf_words >= _MAX_SENTENCE_WORDS:
            flush()
    flush()
    return sentences


def segments_for_window(
    segments: list[Segment], start: float, end: float
) -> list[Segment]:
    """Caption lines overlapping [start, end], trimmed to the window."""
    out: list[Segment] = []
    for s in segments:
        if s.end <= start or s.start >= end:
            continue
        out.append(
            Segment(max(s.start, start), min(s.end, end), s.text)
        )
    return out


# --------------------------------------------------------------------------
# SRT output (v0.4.0)
# --------------------------------------------------------------------------
def _srt_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    if ms == 1000:
        s += 1
        ms = 0
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def segments_to_srt(segments: list[Segment], clip_start: float = 0.0,
                    speed: float = 1.0) -> str:
    """Render segments as SRT, rebased to 0 and rescaled by speed."""
    if speed is None or speed <= 0:
        speed = 1.0
    blocks: list[str] = []
    idx = 1
    for seg in segments:
        start = max(seg.start - clip_start, 0.0) / speed
        end = max(seg.end - clip_start, 0.0) / speed
        if end <= start:
            continue
        text = _clean(seg.text)
        if not text:
            continue
        blocks.append(f"{idx}\n{_srt_time(start)} --> {_srt_time(end)}\n{text}")
        idx += 1
    return ("\n\n".join(blocks) + "\n") if blocks else ""


def write_srt(segments: list[Segment], out_path: Path,
              clip_start: float = 0.0, speed: float = 1.0) -> Path:
    out_path.write_text(segments_to_srt(segments, clip_start, speed), encoding="utf-8")
    return out_path
