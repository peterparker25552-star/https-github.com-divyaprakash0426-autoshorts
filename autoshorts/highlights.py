"""Highlight engine: score transcript windows and pick the most short-able
moments — no external API required.

Scoring mixes signals that correlate with engaging short-form moments:
  * hook phrases ("the truth is", "nobody tells you", ...)
  * statistics & concrete numbers
  * questions (curiosity gaps)
  * emotional / superlative language
  * delivery energy (words per second)
  * penalties for intros, sponsor reads, greetings and fillers
Windows are sentence-aligned, 20–60s by default, and the top non-overlapping
windows are returned with human-readable reasons.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .transcripts import Sentence

# --------------------------------------------------------------------------
# Signal lexicons (English + common Hinglish podcast phrasing)
# --------------------------------------------------------------------------
HOOKS = [
    "the truth is", "truth is", "nobody tells you", "no one tells you",
    "this changed my life", "changed my life", "let me tell you",
    "here's the thing", "here is the thing", "the biggest mistake",
    "biggest mistake", "the secret", "the reality is", "believe it or not",
    "what most people", "most people don't", "i realized", "i realised",
    "i remember when", "one day", "that day", "the day i",
    "story behind", "never told anyone", "first time ever",
    "sach bataun", "sach bataoonga", "sach mein", "ek din aisa aaya",
    "badi baat", "galat hai", "sab kehte hain", "log sochte hain",
]
EMOTION = [
    "amazing", "shocking", "shocked", "crazy", "insane", "incredible",
    "unbelievable", "scary", "terrifying", "failed", "failure", "struggled",
    "struggle", "cried", "broke", "broken", "fought", "fight", "risk",
    "dangerous", "worst", "best", "huge", "massive", "wild", "brutal",
    "honest", "honestly", "proud", "ashamed", "regret", "fear", "love",
    "hate", "painful", "beautiful", "darr", "dard", "sapna", "mehnat",
    "sangharsh", "himmat",
]
SUPERLATIVES = [
    "biggest", "largest", "best", "worst", "first", "only", "never",
    "always", "everyone", "nobody", "no one", "most", "least",
]
INTRO_NOISE = [
    "welcome back", "welcome to", "subscribe", "like the video", "comment",
    "notification", "this podcast", "sponsored", "sponsor", "patreon",
    "instagram", "follow me", "check the description", "links in the",
    "namaskar", "welcome back to figuring out", "greetings",
]
FILLERS = ["um", "uh", "you know", "i mean", "kind of", "sort of", "basically"]

PROFILES = {
    "viral": {
        "hook": 3.5, "numbers": 1.2, "questions": 1.0, "emotion": 1.0,
        "superlative": 0.6, "wps": 3.0, "noise": -4.0, "filler": -1.0,
    },
    "story": {
        "hook": 4.5, "numbers": 0.4, "questions": 1.0, "emotion": 2.2,
        "superlative": 0.4, "wps": 1.5, "noise": -4.0, "filler": -1.0,
    },
    "facts": {
        "hook": 2.0, "numbers": 3.0, "questions": 0.8, "emotion": 0.4,
        "superlative": 1.6, "wps": 2.0, "noise": -4.0, "filler": -1.0,
    },
    "energy": {
        "hook": 2.5, "numbers": 0.8, "questions": 1.2, "emotion": 1.2,
        "superlative": 0.8, "wps": 5.0, "noise": -5.0, "filler": -2.0,
    },
}

_NUM_RE = re.compile(
    r"\b\d[\d,.]*\b|crore|lakh|million|billion|percent|%"
)
_QUESTION_RE = re.compile(r"\?|^(what|why|how|when|who|kya|kyun|kaise)\b", re.I)
_TERMINAL_END = re.compile(r"[.!?…][\"')\]]*$")


@dataclass
class Highlight:
    start: float
    end: float
    score: float
    title: str
    reasons: list[str] = field(default_factory=list)
    sentences: list[Sentence] = field(default_factory=list)
    signals: dict = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return self.end - self.start


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------
def _count_phrases(text: str, phrases: list[str]) -> int:
    t = f" {text.lower()} "
    return sum(1 for p in phrases if p in t)


def score_sentence(s: Sentence) -> dict:
    """Per-sentence signal breakdown (unnormalized)."""
    low = s.text.lower()
    words = s.words or low.split()
    n_words = max(len(words), 1)
    return {
        "hook": _count_phrases(low, HOOKS),
        "numbers": len(_NUM_RE.findall(low)) + _count_phrases(
            low, ["crore", "lakh", "million", "percent"]
        ),
        "questions": len(_QUESTION_RE.findall(low)),
        "emotion": _count_phrases(low, EMOTION),
        "superlative": _count_phrases(low, SUPERLATIVES),
        "noise": _count_phrases(low, INTRO_NOISE),
        "filler": _count_phrases(low, FILLERS),
        "wps": s.wps,
    }


def _window_reasons(breakdown: dict) -> list[str]:
    r = []
    if breakdown["hook"]:
        r.append("strong hook")
    if breakdown["numbers"] >= 2:
        r.append("hard numbers / stats")
    if breakdown["questions"] >= 1:
        r.append("curiosity question")
    if breakdown["emotion"] >= 2:
        r.append("emotional language")
    if breakdown["superlative"]:
        r.append("superlatives")
    if breakdown["wps"] >= 2.6:
        r.append("high-energy delivery")
    return r


# --- signal breakdown --------------------------------------------------------
# The seven UI-facing signal keys, in display order. Each value is the signal
# count multiplied by the active profile weight (so it is directly the number
# of points that signal contributed to the window score).
SIGNAL_KEYS = (
    "hook", "numbers", "questions", "emotion", "superlatives", "energy",
    "penalties",
)


def _signal_breakdown(weights: dict, agg: dict) -> dict:
    """Weighted, rounded signal contributions for one scored window."""
    return {
        "hook": round(weights["hook"] * min(agg["hook"], 3), 2),
        "numbers": round(weights["numbers"] * min(agg["numbers"], 6), 2),
        "questions": round(weights["questions"] * min(agg["questions"], 4), 2),
        "emotion": round(weights["emotion"] * min(agg["emotion"], 5), 2),
        "superlatives": round(
            weights["superlative"] * min(agg["superlative"], 4), 2
        ),
        "energy": round(weights["wps"] * min(agg["wps"], 4.0), 2),
        "penalties": round(
            weights["noise"] * min(agg["noise"], 3)
            + weights["filler"] * min(agg["filler"], 4),
            2,
        ),
    }


def aggregate_signals(sentences: list[Sentence], profile: str = "viral") -> dict:
    """Signal breakdown for an arbitrary set of sentences (used by manual cuts)."""
    weights = PROFILES.get(profile, PROFILES["viral"])
    if not sentences:
        return {key: 0.0 for key in SIGNAL_KEYS}
    agg: dict[str, float] = {
        k: 0 for k in ("hook", "numbers", "questions", "emotion", "superlative",
                       "noise", "filler")
    }
    for sentence in sentences:
        scores = score_sentence(sentence)
        for key in agg:
            agg[key] += scores[key]
    span = max(sentences[-1].end - sentences[0].start, 0.5)
    agg["wps"] = sum(len(s.words) for s in sentences) / span
    return _signal_breakdown(weights, agg)


def signals_for_window(
    sentences: list[Sentence], start: float, end: float, profile: str = "viral"
) -> dict:
    """Signals for the sentences overlapping an explicit time window."""
    inside = [
        s for s in sentences if s.end > start and s.start < end
    ]
    return aggregate_signals(inside, profile)


def transcript_stats(sentences: list[Sentence]) -> dict:
    """Cheap corpus stats shown next to a preview so users can judge a source."""
    if not sentences:
        return {
            "words": 0, "sentences": 0, "questions": 0, "numbers": 0,
            "hooks": 0, "wpm": 0.0, "span": 0.0,
        }
    words = sum(len(s.words) or len(s.text.split()) for s in sentences)
    span = max(sentences[-1].end - sentences[0].start, 0.0)
    return {
        "words": words,
        "sentences": len(sentences),
        "questions": sum(
            1 for s in sentences if _QUESTION_RE.search(s.text)
        ),
        "numbers": sum(len(_NUM_RE.findall(s.text)) for s in sentences),
        "hooks": sum(
            1 for s in sentences if _count_phrases(s.text, HOOKS)
        ),
        "wpm": round(words / span * 60.0, 1) if span > 0 else 0.0,
        "span": round(span, 2),
    }



def _pick_title(sentences: list[Sentence]) -> str:
    """Choose the punchiest short sentence as the clip title."""
    best, best_score = None, -1.0
    for s in sentences:
        n = len(s.words)
        if n < 4 or n > 16:
            continue
        b = score_sentence(s)
        sc = (
            3.0 * b["hook"]
            + 1.5 * min(b["numbers"], 3)
            + 1.2 * b["questions"]
            + 1.0 * b["emotion"]
            + 0.8 * b["superlative"]
            - 2.0 * b["noise"]
        )
        # slight preference for mid-length titles
        sc *= 1.0 - abs(n - 9) * 0.02
        if sc > best_score:
            best, best_score = s, sc
    if best is None and sentences:
        best = max(sentences, key=lambda s: len(s.words))
    title = (best.text if best else "Untitled moment").strip()
    title = re.sub(r"\s+", " ", title)
    if len(title) > 90:
        title = title[:87].rstrip() + "…"
    # Title Case for readability
    if title.islower():
        title = title[0].upper() + title[1:]
    return title


# --------------------------------------------------------------------------
# Window search
# --------------------------------------------------------------------------
def find_highlights(
    sentences: list[Sentence],
    count: int = 5,
    min_dur: float = 20.0,
    max_dur: float = 60.0,
    min_gap: float = 5.0,
    profile: str = "viral",
) -> list[Highlight]:
    """Slide sentence-aligned windows, score, return top non-overlapping."""
    if not sentences:
        return []

    weights = PROFILES.get(profile, PROFILES["viral"])

    # Pre-score each sentence once
    per_sentence = [score_sentence(s) for s in sentences]
    cached: dict[int, dict] = {}

    def window_score(i: int, j: int) -> tuple[float, dict]:
        """Score sentences[i..j] inclusive."""
        key = i * 10_000 + j
        if key in cached:
            return cached[key]
        start = sentences[i].start
        end = sentences[j].end
        dur = end - start
        agg = {
            k: sum(per_sentence[m][k] for m in range(i, j + 1))
            for k in per_sentence[i]
        }
        agg["wps"] = sum(len(sentences[m].words) for m in range(i, j + 1)) / max(
            dur, 0.5
        )

        score = (
            weights["hook"] * min(agg["hook"], 3)
            + weights["numbers"] * min(agg["numbers"], 6)
            + weights["questions"] * min(agg["questions"], 4)
            + weights["emotion"] * min(agg["emotion"], 5)
            + weights["superlative"] * min(agg["superlative"], 4)
            + weights["wps"] * min(agg["wps"], 4.0)
            + weights["noise"] * min(agg["noise"], 3)
            + weights["filler"] * min(agg["filler"], 4)
        )
        # duration preference: peak around ~35s
        sweet = min(dur, 90.0) / 35.0
        score *= 1.6 - 0.6 * abs(1.0 - sweet) if sweet < 1.6 else 0.4
        # bonus when the window opens on a hook
        if per_sentence[i]["hook"] or _QUESTION_RE.match(sentences[i].text):
            score *= 1.25
        # slightly prefer windows that don't end mid-story
        if _TERMINAL_END.search(sentences[j].text.strip()):
            score *= 1.05
        out = (score, agg)
        cached[key] = out
        return out

    n = len(sentences)
    candidates: list[Highlight] = []
    for i in range(n):
        j = i
        while j < n and sentences[j].end - sentences[i].start <= max_dur + 2:
            dur = sentences[j].end - sentences[i].start
            if dur >= min_dur:
                score, agg = window_score(i, j)
                window = sentences[i : j + 1]
                candidates.append(
                    Highlight(
                        start=sentences[i].start,
                        end=sentences[j].end,
                        score=round(score, 2),
                        title=_pick_title(window),
                        reasons=_window_reasons(agg),
                        sentences=window,
                        signals=_signal_breakdown(weights, agg),
                    )
                )
            j += 1
        # also consider the single best long window even if it exceeds min_dur
        if j - 1 > i and sentences[j - 1].end - sentences[i].start < min_dur:
            score, agg = window_score(i, j - 1)
            window = sentences[i:j]
            candidates.append(
                Highlight(
                    start=sentences[i].start,
                    end=sentences[j - 1].end,
                    score=round(score, 2),
                    title=_pick_title(window),
                    reasons=_window_reasons(agg),
                    sentences=window,
                    signals=_signal_breakdown(weights, agg),
                )
            )

    if not candidates:
        return []

    candidates.sort(key=lambda h: h.score, reverse=True)
    picked: list[Highlight] = []
    for cand in candidates:
        if len(picked) >= count:
            break
        if any(
            cand.start < p.end + min_gap and cand.end > p.start - min_gap
            for p in picked
        ):
            continue
        picked.append(cand)
    picked.sort(key=lambda h: h.start)
    return picked
