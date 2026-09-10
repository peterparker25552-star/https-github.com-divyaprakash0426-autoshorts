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
    breakdown: dict = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict:
        return {
            "start": round(self.start, 2),
            "end": round(self.end, 2),
            "duration": round(self.duration, 2),
            "title": self.title,
            "score": self.score,
            "reasons": list(self.reasons),
            "breakdown": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in self.breakdown.items()},
        }


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
    cached: dict[int, tuple[float, dict]] = {}

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
                        breakdown={k: (round(v, 3) if isinstance(v, float) else int(v)) for k, v in agg.items()},
                    )
                )
            j += 1
        # also consider the single best long window even if it exceeds min_dur
        if j - 1 > i and sentences[j - 1].end - sentences[i].start < min_dur:
            score, agg = window_score(i, j - 1)
            candidates.append(
                Highlight(
                    start=sentences[i].start,
                    end=sentences[j - 1].end,
                    score=round(score, 2),
                    title=_pick_title(sentences[i:j]),
                    reasons=_window_reasons(agg),
                    sentences=sentences[i:j],
                    breakdown={k: (round(v, 3) if isinstance(v, float) else int(v)) for k, v in agg.items()},
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


# --------------------------------------------------------------------------
# Preview stats + chapters + upload pack (v0.4.0)
# --------------------------------------------------------------------------
def preview_stats(moments: list[Highlight], profile: str,
                  sentences: list[Sentence] | None = None) -> dict:
    """Aggregate stats for the preview endpoint."""
    scores = [m.score for m in moments]
    durs = [m.duration for m in moments]
    return {
        "count": len(moments),
        "profile": profile,
        "avg_score": round(sum(scores) / len(scores), 2) if scores else 0.0,
        "best_score": max(scores) if scores else 0.0,
        "avg_duration": round(sum(durs) / len(durs), 2) if durs else 0.0,
        "sentences": len(sentences or []),
    }


def build_chapters(sentences: list[Sentence], max_chapters: int = 8) -> list[dict]:
    """Derive lightweight chapters from transcript sentences (offline).

    Splits sentences into ~even groups and titles each from its first
    sentence. Always returns at least one chapter when sentences exist.
    """
    if not sentences:
        return []
    import math
    n = len(sentences)
    # Aim for a chapter every ~4 sentences, capped.
    per = max(1, math.ceil(n / max(1, max_chapters)))
    chapters: list[dict] = []
    for i in range(0, n, per):
        s = sentences[i]
        title = re.sub(r"\s+", " ", s.text.strip())
        if len(title) > 60:
            title = title[:57].rstrip() + "…"
        chapters.append({"start": round(s.start, 2), "title": title or f"Part {len(chapters)+1}"})
        if len(chapters) >= max_chapters:
            break
    return chapters


_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "in", "on", "of", "to", "for",
    "with", "is", "are", "was", "were", "you", "your", "this", "that",
    "from", "they", "them", "his", "her", "our", "out", "about", "into",
})


def build_upload_pack(title: str, episode_title: str = "", duration: float = 0.0,
                      reasons: list[str] | None = None) -> dict:
    """Build per-clip upload pack: titles, hashtags, description."""
    base = (title or "Untitled moment").strip()
    short_ep = re.sub(r"\s*\(Demo\)\s*", "", episode_title or "").strip()
    # 3 title variants
    punchy = base if len(base) <= 70 else base[:67].rstrip() + "…"
    curiosity = base.rstrip(".!?…")
    if not curiosity.endswith("?"):
        curiosity = curiosity + "?"
    seo_bits = [punchy]
    if short_ep:
        # keep SEO title compact
        guest = short_ep.split("|")[0].split(" On ")[0].strip()
        seo_bits.append(f"{guest} #shorts" if guest else "#shorts")
    seo = " | ".join(seo_bits)[:100]
    titles = [punchy, curiosity[:100], seo]

    # hashtags: derive from title words + evergreen tags, ≤12, always #shorts
    words = re.findall(r"[A-Za-z]{3,}", base.lower())
    tags: list[str] = []
    for w in words:
        if w in _STOPWORDS:
            continue
        tag = "#" + w
        if tag not in tags:
            tags.append(tag)
        if len(tags) >= 7:
            break
    for evergreen in ("#shorts", "#podcast", "#viral", "#motivation", "#india", "#success"):
        if evergreen not in tags:
            tags.append(evergreen)
    if "#shorts" not in tags:
        tags.insert(0, "#shorts")
    hashtags = tags[:12]
    if "#shorts" not in hashtags:
        hashtags = (["#shorts"] + hashtags)[:12]

    dur_txt = f"{duration:.1f}s" if duration else "highlight"
    ep_line = f"🎙 {short_ep or episode_title or 'AutoShorts'}"
    time_line = f"⏱ {dur_txt} — {punchy}"
    description = f"{ep_line}\n{time_line}\n\n{base}\n\n{' '.join(hashtags)}"
    return {"titles": titles, "hashtags": hashtags, "description": description}
