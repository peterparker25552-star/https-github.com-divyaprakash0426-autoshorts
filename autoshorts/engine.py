"""T5 — the FREE AI engine: a provider chain that always lands on offline text.

Order of preference, cheapest and most private first:

1. **offline templates** (default, no key, no network) — :mod:`autoshorts.titles`
2. **gemini** — a free Google AI Studio key
3. **groq** — a free Groq key (OpenAI-compatible endpoint)
4. **custom** — any OpenAI-compatible ``base_url`` + key (auto-detected from
   ``OPENAI_API_KEY`` so v0.4.0 setups keep working)

Everything the user configures lives in the server-side *settings* store: keys
are never returned by an API (masked as ``gemini_key_set: true``), never
written to a log line, and scrubbed out of any error text. Every network,
status, parse or validation failure returns the offline result plus a
``notice`` the UI shows — so the app works with no key, and a bad key can only
ever make Qyro slower by a timeout, never break a render.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request

from . import config, titles

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)

PROVIDERS = config.AI_PROVIDERS + ("auto",)
_LABEL = {
    "offline": "Offline templates",
    "auto": "Auto (free tier first)",
    "gemini": "Google AI Studio (Gemini)",
    "groq": "Groq (free tier)",
    "custom": "Custom OpenAI-compatible",
}
_MODEL_DEFAULT = {
    "gemini": config.GEMINI_MODEL,
    "groq": config.GROQ_MODEL,
    "custom": config.CUSTOM_MODEL,
}


# --------------------------------------------------------------------------
# Settings plumbing
# --------------------------------------------------------------------------
def _env_defaults() -> dict:
    return {
        "ai_key": config.LLM_API_KEY or "",
        "ai_base_url": config.LLM_BASE_URL if config.LLM_API_KEY else "",
        "ai_model": config.LLM_MODEL if config.LLM_API_KEY else "",
    }


def read(settings: dict | None) -> dict:
    """Normalised engine config from a settings dict (never contains a secret)."""
    settings = settings if isinstance(settings, dict) else {}
    env = _env_defaults()
    provider = str(settings.get("ai_provider") or "").strip().lower()
    if provider not in PROVIDERS:
        provider = config.DEFAULT_AI_PROVIDER
    keys = {
        "gemini": str(settings.get("gemini_key") or "").strip(),
        "groq": str(settings.get("groq_key") or "").strip(),
        "custom": str(settings.get("ai_key") or env["ai_key"]).strip(),
    }
    base_url = str(settings.get("ai_base_url") or env["ai_base_url"]).strip()
    if not base_url and keys["custom"]:
        base_url = config.LLM_BASE_URL
    model = str(settings.get("ai_model") or "").strip()
    return {
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "keys": keys,
        "legacy_custom": bool(env["ai_key"] and not settings.get("ai_key")),
    }


def key_for(cfg: dict, provider: str) -> str:
    return str((cfg.get("keys") or {}).get(provider) or "").strip()


def chain(cfg: dict) -> list[str]:
    """Providers to try, in order. Empty list means "offline only"."""
    provider = cfg.get("provider") or config.DEFAULT_AI_PROVIDER
    if provider in ("offline", "", None):
        return []
    if provider == "auto":
        return [name for name in ("gemini", "groq", "custom") if key_for(cfg, name)]
    return [provider]


def public_state(settings: dict | None) -> dict:
    """What ``/api/state`` and the settings panel get — keys are never echoed."""
    cfg = read(settings)
    providers = [
        {
            "id": name,
            "label": _LABEL[name],
            "has_key": bool(key_for(cfg, name)),
            "model": cfg["model"] or _MODEL_DEFAULT.get(name, ""),
        }
        for name in ("offline", "gemini", "groq", "custom")
    ]
    return {
        "provider": cfg["provider"],
        "model": cfg["model"],
        "base_url": cfg["base_url"],
        "label": _LABEL.get(cfg["provider"], "Offline templates"),
        "active": bool(chain(cfg)),
        "key_set": any(key_for(cfg, name) for name in ("gemini", "groq", "custom")),
        "providers": providers,
        "auto_model_note": "Free tier only — no card, no charge, ever.",
    }


def mask_settings(settings: dict) -> dict:
    """Strip secret values from a settings dict and expose booleans instead."""
    out = dict(settings or {})
    for key in config.SECRET_SETTING_KEYS:
        value = out.pop(key, None)
        out[f"{key}_set"] = bool(str(value or "").strip())
    return out


def available(settings: dict | None) -> bool:
    """True when the chain has at least one reachable provider configured."""
    return bool(chain(read(settings)))


# --------------------------------------------------------------------------
# HTTP + JSON plumbing
# --------------------------------------------------------------------------
def _scrub(text: str) -> str:
    """Remove any API key that a URL-exception might have echoed back."""
    cleaned = str(text or "")
    for match in re.findall(r"[A-Za-z0-9_\-]{20,}", cleaned):
        cleaned = cleaned.replace(match, "[redacted]")
    return cleaned[:300]


_TRANSIENT_HTTP = (429, 500, 502, 503, 504)


def _post_json(url: str, payload: dict, headers: dict, timeout: float,
               retries: int | None = None) -> dict | None:
    """POST once (plus retries on transient failures) and parse the reply.

    Timeouts, connection resets and 429/5xx responses are retried — a single
    cold-start blip should never demote a render to the offline pack. A
    non-transient HTTP error (401/403/404 …) fails immediately: retrying a bad
    key or a retired model would only waste the render's time.
    """
    if retries is None:
        retries = config.AI_RETRIES
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", **headers},
    )
    attempts = max(0, int(retries)) + 1
    last_error: dict | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = getattr(response, "status", 200)
                body = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            last_error = {"__error__": f"HTTP {exc.code} {_scrub(detail)}"}
            if exc.code in _TRANSIENT_HTTP and attempt + 1 < attempts:
                time.sleep(config.AI_RETRY_BACKOFF)
                continue
            return last_error
        except Exception as exc:
            # timeouts / DNS / refused connections are worth one more try
            last_error = {"__error__": _scrub(str(exc)) or "network error"}
            if attempt + 1 < attempts:
                time.sleep(config.AI_RETRY_BACKOFF)
                continue
            return last_error
        if status != 200:
            return {"__error__": f"HTTP {status}"}
        try:
            parsed = json.loads(body)
        except Exception:
            return {"__error__": "unreadable reply"}
        return parsed if isinstance(parsed, dict) else {"__error__": "unreadable reply"}
    return last_error


def _chat_text(payload: dict) -> str | None:
    """Reply text out of an OpenAI-compatible or Gemini response body."""
    try:
        choice = payload["choices"][0]["message"]["content"]
        if isinstance(choice, str) and choice.strip():
            return choice
    except Exception:
        pass
    try:  # Gemini: candidates[0].content.parts[*].text
        parts = payload["candidates"][0]["content"]["parts"]
        text = "".join(part.get("text", "") for part in parts if isinstance(part, dict))
        return text or None
    except Exception:
        return None


def _extract_json(content: str) -> dict | None:
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


def ask_json(system: str, user: str, settings: dict | None,
             timeout: float = config.AI_TIMEOUT) -> tuple[dict | None, str, str]:
    """Ask the provider chain for one JSON object.

    Returns ``(data, provider, notice)`` — ``data`` is ``None`` when the whole
    chain failed (offline fallback is then the caller's job), ``provider`` names
    the one that answered, and ``notice`` is a short user-facing explanation.
    """
    cfg = read(settings)
    providers = chain(cfg)
    if not providers:
        return None, "offline", ""
    last_error = ""
    for name in providers:
        key = key_for(cfg, name)
        if not key:
            last_error = f"{_LABEL.get(name, name)} needs an API key"
            continue
        model = cfg["model"] or _MODEL_DEFAULT.get(name, "")
        if name == "gemini":
            base = config.GEMINI_BASE_URL.rstrip("/")
            url = f"{base}/models/{model}:generateContent"
            payload = {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {
                    "temperature": 0.85,
                    "maxOutputTokens": 2048,
                    # 2.5-class models "think" by default; left on, the thinking
                    # tokens eat the whole output budget and the reply arrives
                    # empty. These tasks don't need thinking — switch it off.
                    "thinkingConfig": {"thinkingBudget": 0},
                },
            }
            headers = {"x-goog-api-key": key}
        else:
            if name == "groq":
                base = cfg["base_url"] or config.GROQ_BASE_URL
            else:
                base = cfg["base_url"] or config.LLM_BASE_URL
            url = f"{str(base).rstrip('/')}/chat/completions"
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.85,
                "max_tokens": 1024,
            }
            headers = {"Authorization": f"Bearer {key}"}
        parsed = _post_json(url, payload, headers, timeout)
        if not parsed:
            last_error = "empty reply"
            continue
        if "__error__" in parsed:
            last_error = str(parsed["__error__"])
            continue
        content = _chat_text(parsed)
        data = _extract_json(content or "")
        if data is None:
            last_error = "the model did not return JSON"
            continue
        return data, name, ""
    return None, "offline", last_error or "AI provider unavailable"


# --------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------
_TITLE_SYSTEM = (
    "You write YouTube Shorts titles. Reply with JSON only, no markdown, "
    'shaped exactly like {"titles": ["...", "..."], "hashtags": ["#shorts", '
    '"..."]}. Return exactly N title strings, each under 90 characters, each a '
    "different hook (question, number, confession, contrast, direct address). "
    "Never invent facts, names or numbers that are not in the input. Hashtags "
    "start with #shorts and are at most 12."
)


def _clean_titles(raw, wanted: int) -> list[str]:
    out: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, str):
            continue
        text = re.sub(r"\s+", " ", item).strip().strip("\"'“”")
        text = re.sub(r"^\d+[).:-]\s*", "", text)
        if len(text) < 6:
            continue
        if len(text) > 90:
            text = text[:89].rstrip() + "…"
        if text.lower() in {done.lower() for done in out}:
            continue
        out.append(text)
        if len(out) >= wanted:
            break
    return out


def _clean_tags(raw, limit: int = 12) -> list[str]:
    tags: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, str):
            continue
        body = re.sub(r"[^A-Za-z0-9_]", "", item.strip().lstrip("#"))
        if len(body) < 2:
            continue
        tag = f"#{body}"
        if tag.lower() in {done.lower() for done in tags}:
            continue
        tags.append(tag)
    if tags and tags[0].lower() != "#shorts":
        tags = ["#shorts"] + [t for t in tags if t.lower() != "#shorts"]
    elif not tags:
        tags = ["#shorts"]
    return tags[:limit]


def title_variations(text, profile: str = "viral", count: int = config.TITLE_VARIATIONS,
                     settings: dict | None = None, episode_title: str = "") -> dict:
    """Title Lab: N variations + hashtags, AI when configured, offline always."""
    count = max(3, min(int(count or config.TITLE_VARIATIONS), 20))
    base = titles.variations(str(text or ""), profile, count, episode_title)
    result = {
        "titles": base["titles"],
        "hashtags": base["hashtags"],
        "engine": "offline",
        "notice": "",
        "keywords": base.get("keywords") or [],
    }
    cfg = read(settings)
    if not chain(cfg):
        result["notice"] = "Offline templates — add a free AI key in Settings for more angles."
        return result

    system = _TITLE_SYSTEM.replace("N", str(count))
    user = (
        f"Topic / transcript snippet:\n{str(text or '')[:1400]}\n\n"
        f"Highlight profile: {profile}\n"
        + (f"Episode: {episode_title}\n" if episode_title else "")
        + f"Return exactly {count} titles."
    )
    data, provider, notice = ask_json(system, user, settings)
    titles_out = _clean_titles((data or {}).get("titles"), count) if data else []
    if len(titles_out) < 3:
        result["notice"] = notice or "AI reply was unusable — offline titles kept."
        return result
    tags = _clean_tags((data or {}).get("hashtags")) if data else []
    merged = titles_out + [t for t in base["titles"] if t not in titles_out]
    return {
        "titles": merged[:count],
        "hashtags": (tags or base["hashtags"])[:12],
        "engine": provider,
        "notice": "",
        "keywords": base.get("keywords") or [],
    }


_PACK_SYSTEM = (
    "You rewrite YouTube Shorts metadata. Reply with JSON only, no markdown, "
    'shaped exactly like {"titles": ["a", "b", "c"], "hashtags": ["#shorts", '
    '"..."], "description": "..."}. Keep every fact, keep names spelled as '
    "given, keep the description 2-4 short lines."
)


def polish_pack(pack: dict, context: str = "", settings: dict | None = None,
                timeout: float = config.AI_TIMEOUT) -> tuple[dict | None, str, str]:
    """Rewrite an upload pack through the engine.

    Returns ``(pack_or_None, provider, notice)`` — ``None`` pack means the
    caller keeps the offline one.
    """
    cfg = read(settings)
    if not chain(cfg):
        return None, "offline", "No AI provider configured — offline pack kept."
    payload = {
        "current_titles": (pack or {}).get("titles") or [],
        "current_hashtags": (pack or {}).get("hashtags") or [],
        "current_description": (pack or {}).get("description") or "",
        "context": str(context or "")[:1200],
    }
    user = (
        "Improve this upload pack for a vertical short. Return JSON only.\n"
        + json.dumps(payload, ensure_ascii=False)
    )
    data, provider, notice = ask_json(_PACK_SYSTEM, user, settings, timeout=timeout)
    if not data:
        return None, "offline", notice or "AI polish failed — offline pack kept."
    titles_out = _clean_titles(data.get("titles"), 3)
    description = data.get("description")
    if len(titles_out) < 2 or not isinstance(description, str) or not description.strip():
        return None, "offline", "AI reply was incomplete — offline pack kept."
    polished = dict(pack or {})
    polished["titles"] = (titles_out + list((pack or {}).get("titles") or []))[:3]
    polished["hashtags"] = _clean_tags(data.get("hashtags")) or list(
        (pack or {}).get("hashtags") or ["#shorts"])
    polished["description"] = re.sub(r"\s+\n", "\n", description.strip())[:4000]
    polished["polished_by"] = f"{provider}:{cfg['model'] or _MODEL_DEFAULT.get(provider, '')}"
    return polished, provider, ""


def refine_pack(pack: dict, context: str, settings: dict | None = None,
                timeout: float = config.AI_RENDER_TIMEOUT) -> tuple[dict, str, str]:
    """Optional polish right after an offline pack is built (render path).

    The caller keeps the offline pack on any failure, but the notice now says
    *why* the engine was skipped (timeout, 404, bad key, quota) instead of a
    generic "unavailable" — a fixable problem should be reported fixably. The
    budget is :data:`config.AI_RENDER_TIMEOUT` (the old hard-coded 12 s turned
    a cold provider start into a false "AI unavailable").
    """
    cfg = read(settings)
    if not chain(cfg):
        return pack, "offline", ""
    polished, provider, notice = polish_pack(pack, context, settings, timeout=timeout)
    if not polished:
        reason = notice or "provider unreachable"
        return pack, "offline", (
            f"AI unavailable during render ({reason}) — offline pack used."
        )
    return polished, provider, ""
