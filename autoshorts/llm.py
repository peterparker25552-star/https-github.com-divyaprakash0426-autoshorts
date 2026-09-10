"""Optional LLM polish for upload packs (any OpenAI-compatible endpoint).

Disabled by default: without ``OPENAI_API_KEY`` :func:`available` is ``False``
and the API returns 503 instead of pretending to work. Every network, parse or
validation failure returns ``None`` — the caller keeps the offline pack, so a
flaky LLM can never break a clip.
"""
from __future__ import annotations

import json
import re
import urllib.request

from . import config

_TIMEOUT_SLACK = 5
_MAX_DESCRIPTION = 4000
_TARGET_TITLES = 3
_MAX_HASHTAGS = 12

_SYSTEM = (
    "You write YouTube Shorts metadata for podcast clips. Reply with JSON only "
    "(no markdown fences, no commentary) shaped exactly like: "
    '{"titles": ["punchy", "curiosity", "seo"], "hashtags": ["#shorts", "..."], '
    '"description": "..."}. Titles are under 90 characters, the third includes '
    "the guest/host plus a keyword, hashtags start with #shorts and must be at "
    "most 12, and the description is 2-4 short lines plus hashtags."
)

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def available() -> bool:
    """True when an OpenAI-compatible endpoint has been configured."""
    return bool(config.LLM_API_KEY)


def _endpoint() -> str:
    base = (config.LLM_BASE_URL or "https://api.openai.com/v1").rstrip("/")
    return f"{base}/chat/completions"


def _user_prompt(pack: dict, context: str) -> str:
    payload = {
        "current_titles": (pack or {}).get("titles") or [],
        "current_hashtags": (pack or {}).get("hashtags") or [],
        "current_description": (pack or {}).get("description") or "",
        "context": str(context or "")[:1200],
    }
    return (
        "Improve this upload pack for a vertical short. Keep the facts, keep "
        "any guest name spelled correctly, and return the JSON object only.\n"
        + json.dumps(payload, ensure_ascii=False)
    )


def _extract_json(content: str) -> dict | None:
    """Pull a JSON object out of a model reply (tolerates fences/prose)."""
    if not isinstance(content, str) or not content.strip():
        return None
    text = content.strip()
    candidates = [text]
    fenced = _FENCE.search(text)
    if fenced:
        candidates.insert(0, fenced.group(1).strip())
    block = _JSON_BLOCK.search(text)
    if block:
        candidates.append(block.group(0))
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _clean_str(value, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = re.sub(r"\s+", " ", value).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _validate(data: dict) -> dict | None:
    """Strictly validate a model reply; return ``None`` if anything is off."""
    if not isinstance(data, dict):
        return None

    raw_titles = data.get("titles")
    if not isinstance(raw_titles, list):
        return None
    titles: list[str] = []
    for item in raw_titles:
        title = _clean_str(item, 100)
        if title and title not in titles:
            titles.append(title)
    if len(titles) < _TARGET_TITLES:
        return None
    titles = titles[:_TARGET_TITLES]

    raw_tags = data.get("hashtags")
    if not isinstance(raw_tags, list):
        return None
    hashtags: list[str] = []
    for item in raw_tags:
        if not isinstance(item, str):
            return None
        tag = re.sub(r"[^A-Za-z0-9_]", "", item.strip().lstrip("#"))
        if not tag:
            continue
        tag = "#" + tag
        if tag.lower() not in (existing.lower() for existing in hashtags):
            hashtags.append(tag)
    if not hashtags:
        return None
    if hashtags[0].lower() != "#shorts":
        hashtags = ["#shorts"] + [t for t in hashtags if t.lower() != "#shorts"]
    hashtags = hashtags[:_MAX_HASHTAGS]

    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        return None
    description = description.strip()[:_MAX_DESCRIPTION]
    if not description:
        return None

    return {
        "titles": titles,
        "hashtags": hashtags,
        "description": description,
        "polished_by": config.LLM_MODEL,
    }


def polish_pack(
    pack: dict, context: str = "", timeout: int = 25
) -> dict | None:
    """Ask the configured LLM to rewrite ``pack``.

    Returns the validated pack (with ``polished_by``) or ``None`` on *any*
    failure — missing key, network error, bad status, unparseable reply or a
    reply that fails validation.
    """
    if not available():
        return None
    body = json.dumps(
        {
            "model": config.LLM_MODEL,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": _user_prompt(pack, context)},
            ],
            "temperature": 0.7,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        _endpoint(),
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.LLM_API_KEY}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if getattr(response, "status", 200) != 200:
                return None
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except Exception:
        return None

    try:
        content = payload["choices"][0]["message"]["content"]
    except Exception:
        return None
    parsed = _extract_json(content)
    if parsed is None:
        return None
    return _validate(parsed)
