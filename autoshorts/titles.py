"""Offline "upload pack" generator: clip titles, hashtags and a description.

Everything here is pure stdlib heuristics — no API key, no network — so a
rendered clip always ships with publishable copy, even on Termux or a machine
with no internet. :func:`autoshorts.llm.polish_pack` can optionally rewrite the
pack when ``OPENAI_API_KEY`` is configured.
"""
from __future__ import annotations

import re

from . import config

# --------------------------------------------------------------------------
# Lexicons
# --------------------------------------------------------------------------
_HINGLISH = (
    "ki", "ke", "ka", "ko", "hai", "hain", "hoga", "hogi", "nahi", "nahin",
    "kya", "kyun", "kyu", "kaise", "kab", "kahan", "yeh", "ye", "wo", "woh",
    "aur", "bhi", "toh", "to", "mein", "se", "par", "ek", "tha", "thi", "the",
    "kuch", "bahut", "bada", "badi", "log", "baat", "baatein", "karna", "kare",
    "kar", "raha", "rahi", "rahe", "hua", "hui", "gaya", "gayi", "matlab",
    "abhi", "phir", "apna", "apni", "hum", "main", "mai", "tum", "aap", "sab",
    "jab", "tab", "lekin", "magar", "isliye", "kyunki", "sirf", "bilkul",
    "shayad", "bataun", "batao", "sunao", "dekho", "dekhiye", "chalo", "accha",
    "haan", "naa", "ji", "bhai", "yaar", "paaji", "beta",
)

# Verbs that carry no search value in a title ("X says...", "X talks about...").
_JUNK_VERBS = (
    "says", "say", "said", "tells", "tell", "told", "talks", "talk", "talked",
    "speaks", "spoke", "shares", "shared", "reveals", "revealed", "explains",
    "explained", "discusses", "discussed", "answers", "answered", "asks",
    "asked", "reacts", "reacted", "opens", "opened", "breaks", "broke",
    "recalls", "remembered", "feels", "felt", "thinks", "thought", "goes",
    "went", "comes", "came", "gets", "got", "make", "makes", "made", "take",
    "takes", "took", "give", "gives", "gave", "watch", "watches", "listen",
    "hear", "heard", "see", "saw", "know", "knows", "knew",
)

_ENGLISH_STOPWORDS = (
    "a", "about", "after", "again", "against", "all", "also", "am", "an",
    "and", "any", "are", "as", "at", "be", "because", "been", "before",
    "being", "below", "between", "both", "but", "by", "can", "did", "do",
    "does", "doing", "don", "down", "during", "each", "few", "for", "from",
    "further", "had", "has", "have", "having", "he", "her", "here", "hers",
    "herself", "him", "himself", "his", "how", "i", "if", "in", "into", "is",
    "it", "its", "itself", "just", "me", "more", "most", "my", "myself", "no",
    "nor", "not", "now", "of", "off", "on", "once", "only", "or", "other",
    "our", "ours", "ourselves", "out", "over", "own", "same", "she", "should",
    "so", "some", "such", "than", "that", "the", "their", "theirs", "them",
    "themselves", "then", "there", "these", "they", "this", "those",
    "through", "too", "under", "until", "up", "very", "was", "we", "were",
    "what", "when", "where", "which", "while", "who", "whom", "why", "will",
    "with", "would", "you", "your", "yours", "yourself", "yourselves",
    # spoken-podcast filler that never belongs in a hashtag or keyword
    "episode", "podcast", "video", "channel", "subscribe", "like", "comment",
    "share", "full", "part", "clip", "shorts", "short", "today", "thing",
    "things", "stuff", "really", "actually", "basically", "literally",
    "someone", "something", "anything", "everything", "nothing", "nobody",
    "people", "guy", "guys",
    "yeah", "okay", "right", "well", "much", "many", "lot", "ever", "never",
    "always", "even", "still", "back", "first", "one", "two", "three",
)

STOPWORDS = frozenset(_HINGLISH + _JUNK_VERBS + _ENGLISH_STOPWORDS)
JUNK_VERBS = frozenset(_JUNK_VERBS)

# Words that score high on frequency but make a title look silly when used as
# the "topic" ("the truth about truth"), so Title Lab skips them if it can.
_META_WORDS = frozenset({
    "truth", "thing", "things", "time", "life", "year", "years", "day", "people",
    "really", "know", "think", "want", "going", "said", "told", "question",
    "story", "part", "lot", "way", "thing", "best", "great", "much", "still",
})
_HOOK_HINTS = (
    "the truth is", "nobody", "no one", "i remember", "one day", "the day",
    "biggest", "never", "secret", "mistake", "here's the thing", "shocking",
    "sach", "log", "people think",
)

_WORD_RE = re.compile(r"[a-z0-9']+")
_WS_RE = re.compile(r"\s+")
_QUESTION_SPLIT = re.compile(r"(?<=[.!?…])\s+")

_GENERIC_TAGS = ("#podcast", "#interview", "#clips", "#fyp")
_PROFILE_TAGS = {
    "viral": ("#viral", "#trending"),
    "story": ("#storytime", "#motivation"),
    "facts": ("#facts", "#didyouknow"),
    "energy": ("#highlights", "#motivation"),
}


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------
def _clean_text(text: str) -> str:
    return _WS_RE.sub(" ", str(text or "").replace("\n", " ")).strip()


def _clip(text: str, limit: int) -> str:
    text = _clean_text(text)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip(" ,;:-") + "…"


def _title_case(text: str) -> str:
    words = []
    for word in _clean_text(text).split(" "):
        if not word:
            continue
        words.append(word if word.isupper() and len(word) > 1 else word[:1].upper() + word[1:])
    return " ".join(words)


def _first_question(text: str) -> str:
    """First question-looking sentence in ``text`` ('' when there is none)."""
    clean = _clean_text(text)
    if not clean:
        return ""
    for sentence in _QUESTION_SPLIT.split(clean):
        sentence = sentence.strip()
        if not sentence:
            continue
        if sentence.endswith("?") or re.match(
            r"^(what|why|how|when|who|which|kya|kyun|kaise)\b", sentence, re.I
        ):
            return sentence
    return ""


# --------------------------------------------------------------------------
# Guest / keyword extraction
# --------------------------------------------------------------------------
def guest_from_episode(episode_title: str) -> str:
    """Pull the guest (or lead topic) out of an episode title.

    ``"Sunil Chhetri On Indian Football, Retirement… | FO 223 (Demo)"``
    becomes ``"Sunil Chhetri"``. Titles without a guest fall back to their
    first topic phrase, e.g. ``"Human Trafficking, Child Crime…"`` becomes
    ``"Human Trafficking"``. Returns ``""`` when nothing sane survives.
    """
    text = _clean_text(episode_title)
    if not text:
        return ""
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(r"\|\s*[^|]*$", " ", text)              # " | FO 223"
    text = re.sub(r"\b(?:f\.?o\.?|ep|episode)\.?\s*\d+.*$", " ", text, flags=re.I)
    text = re.sub(r"\s+[-–—]\s+.*$", " ", text)           # " - Ex DIG In CBI"
    text = re.split(r"\s+(?:on|about)\s+", text, maxsplit=1, flags=re.I)[0]
    text = re.split(r"\s*[:|–—]\s*", text)[0]
    head = text.split(",")[0]                             # first topic / full name
    head = re.sub(r"\s+(?:ft|feat|featuring)\.?\s+.*$", " ", head, flags=re.I)
    words = []
    for word in head.split():
        bare = word.strip(".,!?;:\"'").lower()
        if not bare or bare in STOPWORDS:
            continue
        words.append(word.strip(".,!?;:\"'"))
        if len(words) == 3:
            break
    name = " ".join(words).strip(" -–—,.")
    if len(name) < 3 or len(name) > 42:
        return ""
    return _title_case(name)


def keywords(text: str, limit: int = 8, exclude: tuple[str, ...] | set[str] | list[str] = ()) -> list[str]:
    """Rank meaningful lowercase words by frequency then first appearance."""
    excluded = {str(w).strip().lower() for w in exclude if str(w).strip()}
    counts: dict[str, int] = {}
    order: dict[str, int] = {}
    for index, token in enumerate(_WORD_RE.findall(str(text or "").lower())):
        if token in STOPWORDS or token in excluded:
            continue
        if len(token) < 4 or token.isdigit() or token in ("http", "https", "www"):
            continue
        counts[token] = counts.get(token, 0) + 1
        order.setdefault(token, index)
    ranked = sorted(counts, key=lambda word: (-counts[word], order[word]))
    return ranked[: max(1, limit)]


def topic_phrases(text: str, kws: list[str], limit: int = 3) -> list[str]:
    """Topic strings for a title: a strong bigram beats a bare adjective.

    ``"indian"`` alone reads badly ("Stop worrying about indian"); if the two
    most frequent words sit next to each other in the text we use the phrase
    instead. Falls back to single keywords.
    """
    tokens = [t for t in _WORD_RE.findall(str(text or "").lower()) if t]
    counts: dict[str, int] = {}
    for first, second in zip(tokens, tokens[1:]):
        if first in STOPWORDS or second in STOPWORDS:
            continue
        if len(first) < 4 or len(second) < 4:
            continue
        phrase = f"{first} {second}"
        counts[phrase] = counts.get(phrase, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    out: list[str] = []
    pool = list(kws or [])
    for phrase, count in ranked:
        if count < 2:
            break
        first, second = phrase.split(" ")
        if first in pool or second in pool:
            value = " ".join(word.capitalize() for word in phrase.split())
            if value.lower() not in [item.lower() for item in out]:
                out.append(value)
                pool = [word for word in pool if word not in (first, second)]
                if len(out) >= limit:
                    return out
    for word in pool:
        value = word.capitalize()
        if value.lower() not in [item.lower() for item in out]:
            out.append(value)
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------
# Pack generation
# --------------------------------------------------------------------------
def _punchy(title: str) -> str:
    text = _clean_text(title).strip("\"'“‘")
    text = re.sub(r"^(?:so|and|but|well|okay|ok|now|then|yeah)\s+", "", text, flags=re.I)
    text = text.rstrip(" .,;:—–-")
    if text[:1].islower() and not text.lower().startswith(("i ", "i'")):
        text = text[0].upper() + text[1:]
    text = _clip(text, 90).rstrip(" .,;:—–-")
    return text or "Untitled moment"


def _curiosity(title: str, window: str, guest: str, kws: list[str]) -> str:
    question = _first_question(window) or _first_question(title)
    if question:
        cleaned = question.rstrip("?").strip()
        if cleaned:
            return _clip(cleaned + "?", 92)
    topic = _title_case(kws[0]) if kws else (guest or _punchy(title))
    templates = (
        f"The truth about {topic}",
        f"What nobody tells you about {topic}",
        f"{topic} — the part most people miss",
    )
    return _clip(templates[len(title) % len(templates)], 92)


def _seo(guest: str, kws: list[str], secs: int, title: str) -> str:
    topic = " & ".join(_title_case(word) for word in kws[:2])
    if guest and topic:
        core = f"{guest} On {topic}"
    elif topic:
        core = topic
    else:
        core = _clip(title, 60)
    return _clip(f"{core} | {secs}s Short", 100)


def _hashtags(guest: str, kws: list[str], profile: str, limit: int = 12) -> list[str]:
    tags: list[str] = []

    def add(raw: str) -> None:
        tag = "#" + re.sub(r"[^A-Za-z0-9]", "", str(raw).lstrip("#"))
        if len(tag) < 3:
            return
        if tag.lower() in (existing.lower() for existing in tags):
            return
        tags.append(tag)

    add("shorts")  # always first
    if guest:
        add(guest)
    for word in kws[:5]:
        add(word)
    for tag in _PROFILE_TAGS.get(str(profile).lower(), _PROFILE_TAGS["viral"]):
        add(tag)
    for tag in _GENERIC_TAGS:
        if len(tags) >= limit:
            break
        add(tag)
    return tags[:limit]


def _description(
    title: str,
    window: str,
    episode_title: str,
    guest: str,
    secs: int,
    hashtags: list[str],
) -> str:
    lines = [title]
    # The two contract lines: 🎙 (who/what) and ⏱ (how long).
    lines.append(f"🎙 {guest if guest else title}")
    lines.append(f"⏱ {secs}s clip")
    lines.append("")
    blurb = _clip(window, 280)
    if blurb:
        lines.append(blurb)
        lines.append("")
    source = _clean_text(episode_title)
    if source:
        lines.append(f"From: {source}")
    lines.append(f"Clip length: {secs}s")
    lines.append("")
    lines.append(" ".join(hashtags[:8]))
    lines.append(f"Made with {config.APP_NAME}")
    return "\n".join(lines).strip()


def generate_pack(
    moment_title: str,
    window_text: str,
    episode_title: str = "",
    profile: str = "viral",
    duration: float = 0.0,
) -> dict:
    """Build an offline upload pack for one clip.

    Returns ``{"titles": [punchy, curiosity, seo], "hashtags": [...],
    "description": "..."}`` plus the intermediate ``guest``/``keywords``/
    ``seconds`` values the UI shows while editing.
    """
    title = _punchy(moment_title)
    window = _clean_text(window_text)
    guest = guest_from_episode(episode_title)
    exclude = {word.strip(".,!?'\"").lower() for word in guest.split()}
    kws = keywords(f"{title} {window}", limit=8, exclude=exclude)
    secs = max(1, int(round(float(duration or 0))))
    titles = [
        title,
        _curiosity(title, window, guest, kws),
        _seo(guest, kws, secs, title),
    ]
    hashtags = _hashtags(guest, kws, profile)
    return {
        "titles": titles,
        "hashtags": hashtags,
        "description": _description(
            titles[0], window, episode_title, guest, secs, hashtags
        ),
        "guest": guest,
        "keywords": kws,
        "seconds": secs,
    }


# --------------------------------------------------------------------------
# Title Lab — offline variations (the engine may rewrite these with AI)
# --------------------------------------------------------------------------
# Each template is a different *hook shape* that works on Shorts. They only use
# words already present in the clip, so nothing is invented when no key is set.
def variations(
    text: str,
    profile: str = "viral",
    count: int = config.TITLE_VARIATIONS,
    episode_title: str = "",
) -> dict:
    """Ten offline title variations + hashtags for one transcript snippet.

    Pure stdlib, no key, no network. The templates deliberately use different
    shapes (question, number, confession, contrast, list, direct address …) so
    the picker in the UI offers real choices rather than one line ten times.
    """
    clean = _clean_text(text)
    pack = generate_pack(clean, clean, episode_title, profile, 0.0)
    guest = pack["guest"]
    kws = [word for word in (pack["keywords"] or []) if word not in _META_WORDS]
    if not kws:
        kws = pack["keywords"] or ["this story"]
    phrases = topic_phrases(clean, kws)
    topic = phrases[0] if phrases else kws[0].title()
    second = phrases[1] if len(phrases) > 1 else (
        kws[1].title() if len(kws) > 1 else "")
    # Lead on the punchiest *sentence*, not the whole snippet — a 160-char
    # quote is unreadable in a Shorts title.
    sentences = [part.strip() for part in _QUESTION_SPLIT.split(clean) if part.strip()]
    hooky = [part for part in sentences if any(h in part.lower() for h in _HOOK_HINTS)]
    lead_source = (hooky or sentences or [clean])[0]
    lead = _punchy(lead_source[:110])
    who = guest or "He"
    numbers = re.findall(r"\b\d[\d,.]*\s?(?:%|percent|crore|lakh|million|billion)?",
                         clean, flags=re.I)
    number = numbers[0].strip() if numbers else ""

    ideas = [
        lead,
        f"{topic}: what nobody tells you",
        f"Why {topic.lower()} changed everything for {who}",
        f"{who} on {topic.lower()}" + (f", {second.lower()} and what it cost" if second else ""),
        f"The {topic.lower()} moment that shocked {who}",
        (f"{number} — and everything changed" if number else
         f"The {topic.lower()} part most people miss"),
        f"{topic}" + (f" vs {second}" if second else "") + " — the honest answer",
        f"Stop getting {topic.lower()} wrong",
        f"{who}'s biggest lesson about {topic.lower()}",
        f"Watch this before you judge {topic.lower()}",
    ]

    seen: set[str] = set()
    titles: list[str] = []
    for idea in ideas:
        value = _clip(str(idea).strip(), 90).rstrip(" .,;:")
        key = value.lower()
        if len(value) < 6 or key in seen:
            continue
        seen.add(key)
        titles.append(value)
    extra = (
        "the untold part", "what it really costs", "the turning point",
        "why it matters", "the first ten years", "what the data says",
    )
    while len(titles) < max(1, int(count)) and len(titles) < len(ideas) + len(extra):
        filler = _clip(f"{topic} — {extra[len(titles) % len(extra)]}", 90).rstrip(" .,;:")
        if filler.lower() in seen:
            filler = _clip(f"{who} on {topic.lower()} — {extra[len(titles) % len(extra)]}", 90)
            if filler.lower() in seen:
                break
        seen.add(filler.lower())
        titles.append(filler)
    titles = titles[: max(1, int(count))]

    hashtags = _hashtags(guest, kws, profile, limit=12)
    return {
        "titles": titles,
        "hashtags": hashtags,
        "keywords": kws,
        "guest": guest,
        "profile": profile if profile in config.PROFILES else "viral",
    }

