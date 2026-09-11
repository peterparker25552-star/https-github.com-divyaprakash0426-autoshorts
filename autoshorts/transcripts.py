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
    # Optional per-word timings ``(word, start, end)``. YouTube's json3
    # (``segs[].tOffsetMs``) and tagged WebVTT (``<00:00:01.000>``) both carry
    # them; when present the caption engine times every chunk from the real
    # words instead of dividing the line evenly, so captions stop drifting.
    words: list = field(default_factory=list)

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


def _json3_words(segs: list, start: float, end: float) -> list:
    """``(word, start, end)`` triples from a json3 event's ``segs``.

    YouTube only puts ``tOffsetMs`` on some segments; a word without one keeps
    the previous word's clock. Returns ``[]`` when nothing is timed, so the
    caller falls back to even splitting.
    """
    # Without a single tOffsetMs there is no real timing to use, and inventing
    # one (every word "spoken" at the line start) is worse than even splitting.
    if not any(seg.get("tOffsetMs") is not None for seg in segs):
        return []
    out: list = []
    cursor = float(start)
    for seg in segs:
        text = _clean(str(seg.get("utf8") or ""))
        if not text:
            continue
        offset = seg.get("tOffsetMs")
        if offset is not None:
            try:
                cursor = float(start) + float(offset) / 1000.0
            except (TypeError, ValueError):
                pass
        out.append([text, max(cursor, float(start)), 0.0])
    if not out:
        return []
    for index in range(len(out) - 1):
        out[index][2] = max(out[index + 1][1], out[index][1] + 0.04)
    out[-1][2] = max(float(end), out[-1][1] + 0.04)
    return [tuple(item) for item in out]


def parse_json3(raw: str | bytes) -> list[Segment]:
    """Parse YouTube json3 timedtext into line-level segments (+ word times)."""
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
        end = start + max(dur, 0.5)
        segments.append(
            Segment(start, end, text, _json3_words(ev["segs"], start, end))
        )
        prev_text = text
    return segments


_VTT_STAMP = re.compile(r"<(\d{1,2}:\d{2}:\d{2}\.\d{3})>")
_VTT_TAGS = re.compile(r"</?(?:c|v[^>]*|b|i|u|lang[^>]*)>")


def _vtt_ts(tok: str) -> float:
    h, m, rest = tok.split(":")
    sec, ms = rest.split(".")
    return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms) / 1000.0


def strip_vtt_markup(body: str) -> str:
    """Remove inline ``<00:00:01.000>``/``<c>`` tags, keeping the words.

    YouTube's auto-caption VTT wraps every word in markup; without this the
    raw timestamps end up burned into the caption.
    """
    return _clean(_VTT_TAGS.sub(" ", _VTT_STAMP.sub(" ", str(body or ""))))


def _vtt_words(body: str, start: float, end: float) -> list:
    """``(word, start, end)`` triples from inline ``<00:00:01.000>`` tags.

    YouTube's auto-caption VTT timestamps each word inside the cue; the text
    between two stamps belongs to the earlier one. A cue without stamps yields
    ``[]`` so the caption engine falls back to even splitting.
    """
    body = str(body or "")
    marks = list(_VTT_STAMP.finditer(body))
    if not marks:
        return []
    out: list = []
    for index, match in enumerate(marks):
        stop = marks[index + 1].start() if index + 1 < len(marks) else len(body)
        text = strip_vtt_markup(body[match.end():stop])
        if not text:
            continue
        try:
            stamp = _vtt_ts(match.group(1))
        except Exception:
            continue
        out.append([text, max(stamp, float(start)), 0.0])
    if not out:
        return []
    for index in range(len(out) - 1):
        out[index][2] = max(out[index + 1][1], out[index][1] + 0.04)
    out[-1][2] = max(float(end), out[-1][1] + 0.04)
    return [tuple(item) for item in out]


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
        joined = " ".join(text_lines)
        text = strip_vtt_markup(joined)
        if not text or text in (s.text for s in segments[-3:]):
            continue  # rolling auto-caption dedupe
        segments.append(Segment(start, end, text, _vtt_words(joined, start, end)))
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
    # v0.6.1 — ending signals. ``terminal`` is True when the utterance ends
    # with sentence punctuation; ``pause_after`` is the silence between this
    # utterance's last word and the next caption line (None when unknown).
    # The highlight engine uses them to make shorts stop where the *speaker*
    # stops instead of wherever a scored window happens to stop.
    terminal: bool = False
    pause_after: float | None = None

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def wps(self) -> float:
        return len(self.words) / self.duration if self.duration > 0 else 0.0


_TERMINALS = re.compile(r"[.!?…][\"')\]]*$")
_MAX_SENTENCE_WORDS = 40


def to_sentences(segments: Iterable[Segment], max_gap: float = 1.2) -> list[Sentence]:
    """Merge caption segments into sentence-like utterances.

    Utterances break on punctuation, on a pause longer than ``max_gap`` or at
    40 words — whichever comes first. Each utterance records whether it ended
    with punctuation (``terminal``) and how much silence followed it
    (``pause_after``), so cuts can land on natural stops.
    """
    segments = [s for s in segments if s.text.strip()]
    sentences: list[Sentence] = []
    buf_text: list[str] = []
    buf_start: float | None = None
    buf_end = None
    buf_words = 0

    def flush(terminal: bool = False, pause_after: float | None = None):
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
                        terminal=terminal,
                        pause_after=pause_after,
                    )
                )
        buf_text, buf_start, buf_end, buf_words = [], None, None, 0

    for index, seg in enumerate(segments):
        text = seg.text.strip()
        if not text:
            continue
        if buf_start is None:
            buf_start = seg.start
        # gap too large -> new utterance; the silence belongs to the *previous*
        # utterance, so it is recorded on it before the buffer resets
        if buf_end is not None and seg.start - buf_end > max_gap:
            gap = max(0.0, seg.start - buf_end)
            flush(pause_after=gap)
            buf_start = seg.start
        buf_text.append(text)
        buf_end = max(buf_end or seg.end, seg.end)
        buf_words += len(text.split())
        joined = " ".join(buf_text)
        if _TERMINALS.search(joined.strip()) or buf_words >= _MAX_SENTENCE_WORDS:
            # how much silence follows the last word? peek at the next caption
            # line — overlapping auto-caption lines simply yield a small gap
            pause = None
            if index + 1 < len(segments):
                pause = max(0.0, float(segments[index + 1].start) - float(buf_end))
            flush(
                terminal=bool(_TERMINALS.search(joined.strip())),
                pause_after=pause,
            )
    flush()
    return sentences


def segments_for_window(
    segments: list[Segment], start: float, end: float
) -> list[Segment]:
    """Caption lines overlapping [start, end], trimmed to the window.

    Per-word timings survive the trim (words outside the window are dropped)
    so the caption engine still lands on the real syllables at a clip edge.
    """
    out: list[Segment] = []
    for s in segments:
        if s.end <= start or s.start >= end:
            continue
        words = [
            (w, max(float(ws), start), min(float(we), end))
            for (w, ws, we) in (getattr(s, "words", None) or [])
            if float(we) > start and float(ws) < end
        ]
        out.append(
            Segment(max(s.start, start), min(s.end, end), s.text, words)
        )
    return out


# --------------------------------------------------------------------------
# SubRip export
# --------------------------------------------------------------------------
def _srt_time(seconds: float) -> str:
    total_ms = int(round(max(0.0, float(seconds)) * 1000))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def to_srt(segments: Iterable[Segment], offset: float = 0.0) -> str:
    """Render ``segments`` as valid SubRip (``.srt``) text.

    ``offset`` is *added* to every timestamp, so passing ``-clip_start`` makes
    a windowed transcript start at ``00:00:00,000`` — exactly what a clip's
    sidecar subtitle file needs.
    """
    blocks: list[str] = []
    index = 0
    for segment in sorted(segments, key=lambda s: (s.start, s.end)):
        text = _clean(segment.text)
        if not text:
            continue
        start = max(0.0, segment.start + offset)
        end = max(start + 0.05, segment.end + offset)
        index += 1
        blocks.append(
            f"{index}\n{_srt_time(start)} --> {_srt_time(end)}\n{text}\n"
        )
    if not blocks:
        return ""
    return "\n".join(blocks) + "\n"
