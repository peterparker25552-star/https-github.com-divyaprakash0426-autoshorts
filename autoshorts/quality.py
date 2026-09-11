""""Only the good parts" — a quality gate between picking a moment and
rendering it.

The highlight scorer works from *words*, which is the right signal for finding
an interesting story but a blind one for finding a watchable *clip*: it cannot
see that a chosen window opens on three seconds of dead air, ends mid-word, or
contains one long pause that makes a 40-second short feel like a minute.

This module fixes exactly that, using the transcript's own timing (no extra
ffmpeg pass, so it is free):

* **edge trim** — pull the cut in to the first/last spoken word so the short
  starts on a voice, not on silence;
* **speech density** — a window that is mostly gaps gets penalised so the next
  best moment wins instead;
* **long inner pause** — a single multi-second hole is a visible stutter, so
  it is flagged (and ``silence`` jump-cuts remove it at render time);
* **boundary snap** — never start or end inside a word.

Every function is pure and total: given any transcript it returns something
sane, and a failure can only ever leave the moment unchanged.
"""
from __future__ import annotations

from dataclasses import replace

from . import config
from .highlights import Highlight
from .transcripts import Segment


def speech_ratio(segments: list[Segment], start: float, end: float) -> float:
    """Fraction of ``[start, end]`` that is actually spoken (0.0..1.0)."""
    window = max(float(end) - float(start), 1e-6)
    covered = 0.0
    for seg in segments or []:
        lo = max(float(seg.start), float(start))
        hi = min(float(seg.end), float(end))
        if hi > lo:
            covered += hi - lo
    return max(0.0, min(1.0, covered / window))


def longest_gap(segments: list[Segment], start: float, end: float) -> float:
    """Longest stretch of non-speech inside the window, in seconds."""
    inside = sorted(
        (max(float(s.start), float(start)), min(float(s.end), float(end)))
        for s in (segments or [])
        if float(s.end) > float(start) and float(s.start) < float(end)
    )
    if not inside:
        return max(0.0, float(end) - float(start))
    worst = max(0.0, inside[0][0] - float(start))
    worst = max(worst, float(end) - inside[-1][1])
    cursor = inside[0][1]
    for lo, hi in inside[1:]:
        worst = max(worst, lo - cursor)
        cursor = max(cursor, hi)
    return max(0.0, worst)


def trim_edges(
    segments: list[Segment],
    start: float,
    end: float,
    pad: float = 0.12,
    max_trim: float = 2.5,
) -> tuple[float, float]:
    """Pull ``[start, end]`` in to the first/last spoken word.

    Leaves a ``pad`` of breathing room, never trims more than ``max_trim``
    seconds from either side (a window that is *all* silence must not collapse
    to nothing), and always returns a window of at least 3 seconds.
    """
    start = float(start)
    end = float(end)
    inside = [
        s for s in (segments or [])
        if float(s.end) > start and float(s.start) < end
    ]
    if not inside:
        return start, end
    first = max(start, min(float(inside[0].start) - pad, start + max_trim))
    last = min(end, max(float(inside[-1].end) + pad, end - max_trim))
    first = max(start, min(first, end - 3.0))
    last = min(end, max(last, first + 3.0))
    return round(first, 3), round(last, 3)


def grade(
    segments: list[Segment],
    start: float,
    end: float,
    min_ratio: float = config.MIN_SPEECH_RATIO,
    max_gap: float = config.MAX_INNER_SILENCE,
    penalty: float = config.QUALITY_PENALTY,
) -> tuple[float, list[str]]:
    """Score multiplier (<= 1.0) plus human-readable reasons.

    ``1.0`` means "nothing wrong". A window that is mostly silence or holds one
    long hole is multiplied by ``penalty`` so a denser moment is picked instead.
    """
    ratio = speech_ratio(segments, start, end)
    gap = longest_gap(segments, start, end)
    reasons: list[str] = []
    multiplier = 1.0
    if ratio < float(min_ratio):
        multiplier *= float(penalty)
        reasons.append(f"only {ratio:.0%} speech")
    if gap > float(max_gap):
        multiplier *= float(penalty)
        reasons.append(f"{gap:.1f}s pause inside")
    if ratio >= 0.85:
        reasons.append("tight, no dead air")
    return round(multiplier, 4), reasons


def refine_moments(
    moments: list[Highlight],
    segments: list[Segment],
    enabled: bool = config.QUALITY_GATE_DEFAULT,
) -> list[Highlight]:
    """Trim, grade and re-rank a list of highlights.

    Returns new ``Highlight`` objects (the inputs are never mutated) with the
    trimmed window, the quality multiplier folded into ``score`` and the
    reasons appended, re-sorted by the *adjusted* score so the densest,
    best-formed moment is rendered first.
    """
    if not moments:
        return []
    if not enabled:
        return list(moments)
    out: list[Highlight] = []
    for moment in moments:
        start, end = trim_edges(segments, moment.start, moment.end)
        multiplier, reasons = grade(segments, start, end)
        kept = [
            sentence for sentence in (moment.sentences or [])
            if float(sentence.end) > start and float(sentence.start) < end
        ] or moment.sentences
        out.append(replace(
            moment,
            start=start,
            end=end,
            score=round(float(moment.score) * multiplier, 2),
            reasons=list(moment.reasons) + [
                f"quality: {reason}" for reason in reasons
            ],
            sentences=kept,
        ))
    out.sort(key=lambda item: item.score, reverse=True)
    return out


def window_report(segments: list[Segment], start: float, end: float) -> dict:
    """The numbers behind a grade — shown in the preview/inspector UI."""
    ratio = speech_ratio(segments, start, end)
    gap = longest_gap(segments, start, end)
    trimmed = trim_edges(segments, start, end)
    multiplier, reasons = grade(segments, start, end)
    return {
        "speech_ratio": round(ratio, 3),
        "longest_gap": round(gap, 2),
        "trimmed_start": trimmed[0],
        "trimmed_end": trimmed[1],
        "quality": multiplier,
        "notes": reasons,
    }
