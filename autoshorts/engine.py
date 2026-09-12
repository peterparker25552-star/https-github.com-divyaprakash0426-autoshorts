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
    # v0.6.5 — Google's new "AQ." auth keys cannot authenticate on any
    # OpenAI-compatible (Bearer) route — they come back 400/401 there — so a
    # Google key pasted into the OpenAI-compatible box could only ever fail.
    # When the Google box is empty, hand the key to the provider that works.
    # (Legacy AIza keys stay put: Google's own OpenAI shim still takes them.)
    if (keys["custom"].startswith("AQ") and not keys["gemini"]
            and config.is_gemini_key(keys["custom"])):
        keys["gemini"] = keys["custom"]
        keys["custom"] = ""
    base_url = str(settings.get("ai_base_url") or env["ai_base_url"]).strip()
    if not base_url and keys["custom"]:
        base_url = config.LLM_BASE_URL
    # Google writes its own ids as ``models/gemini-…`` in error messages, so a
    # model copied out of one arrives with that prefix and every URL built from
    # it is a double path segment. Strip it once, here, for every caller.
    model = config.normalise_model(settings.get("ai_model"))
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
    if provider == "custom" and not key_for(cfg, "custom") and key_for(cfg, "gemini"):
        # An AQ. key was pasted into the OpenAI-compatible box and read() moved
        # it to the Google provider — follow it instead of dying on an empty key.
        return ["gemini"]
    return [provider]


def model_for(cfg: dict, provider: str) -> str:
    """The id actually worth calling for ``provider`` right now.

    The stored ``ai_model`` wins *unless* it names a model the provider has
    retired — a value from an older ``state.json`` (or from the Settings form
    re-posting what it was prefilled with) used to pin every render to a 404.
    """
    requested = config.normalise_model(cfg.get("model"))
    default = _MODEL_DEFAULT.get(provider, "")
    if requested and not config.is_retired_model(requested):
        return requested
    return default or requested


def model_candidates(cfg: dict, provider: str) -> list[str]:
    """Models to try for one provider, most likely to work first.

    Two directions are covered on purpose:

    * a **retired** id (any Gemini 2.x, Groq's retired ``llama-3.1-8b-instant``)
      is demoted behind the provider's current default, so a key Google closed
      that model for never blocks a render;
    * a live-but-unusable id — a typo, a model retired *next* month — still
      gets its honest first attempt, and Qyro's default follows it.
    """
    requested = config.normalise_model(cfg.get("model"))
    default = _MODEL_DEFAULT.get(provider, "")
    out: list[str] = []
    if requested and not config.is_retired_model(requested):
        out.append(requested)
    if default and default not in out:
        out.append(default)
    if requested and requested not in out:
        out.append(requested)
    return out


def retired_model_note(settings: dict | None) -> str:
    """A one-line human explanation when the stored model id is retired."""
    cfg = read(settings)
    requested = config.normalise_model(cfg.get("model"))
    if not requested or not config.is_retired_model(requested):
        return ""
    return (f"{requested} has been retired by its provider — Qyro calls "
            f"{model_for(cfg, cfg['provider']) or 'the current free-tier model'} "
            f"instead. Saving the settings below keeps the fix.")


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
    for entry in providers:
        entry["model_effective"] = model_for(cfg, entry["id"]) or entry["model"]
    return {
        "provider": cfg["provider"],
        "model": cfg["model"],
        "model_effective": model_for(cfg, cfg["provider"]) or cfg["model"],
        "model_note": retired_model_note(settings),
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


def _model_major(model: str) -> int:
    """Leading version number of a Gemini model id (``gemini-3.6-flash`` → 3)."""
    match = re.search(r"gemini-(\d+)", str(model or ""))
    return int(match.group(1)) if match else 0


def _gemini_thinking(model: str) -> dict:
    """The right thinking control for a Gemini model id.

    2.5-class models "think" by default; left on, the thinking tokens eat the
    whole output budget and the reply arrives empty — pin ``thinkingBudget: 0``.
    Gemini 3 models dropped that field in favour of ``thinkingLevel``, so 3.x
    gets ``low``: these tasks are JSON rewrites, not reasoning problems.
    """
    if _model_major(model) >= 3:
        return {"thinkingConfig": {"thinkingLevel": "low"}}
    return {"thinkingConfig": {"thinkingBudget": 0}}


_TRANSIENT_HTTP = (429, 500, 502, 503, 504)

# What a provider says when the *model id* is the problem, as opposed to the
# key, the quota or the network. Only these justify trying a second id.
_DEAD_MODEL_HINTS = (
    "no longer available",
    "not available to new users",
    "not found for api",
    "was not found",
    "is not found",
    "unknown model",
    "unsupported model",
    "not supported",
    "does not exist",
    "deprecated",
)
_MESSAGE_FIELD = re.compile(r'"message"\s*:\s*"([^"]{4,300})"')


def _error_status(reply: dict | None) -> int:
    try:
        return int((reply or {}).get("__status__") or 0)
    except (TypeError, ValueError):
        return 0


def _dead_model(reply: dict | None) -> bool:
    """True when the provider refused the model id itself (404/400 + wording)."""
    if not isinstance(reply, dict) or "__error__" not in reply:
        return False
    if _error_status(reply) not in (400, 404):
        return False
    text = str(reply.get("__error__") or "").lower()
    return any(hint in text for hint in _DEAD_MODEL_HINTS)


def _humanise(reply: dict | None, provider: str, model: str) -> str:
    """One readable sentence for a provider failure — never a JSON dump.

    Users used to get the raw body pasted into a toast (``HTTP 404 { "error":
    { … no longer available to new users … } }``), which says nothing about
    what to do. The status is decoded instead, and the provider's own message —
    the useful half of that body — is kept when there is one.
    """
    label = _LABEL.get(provider, provider)
    raw = str((reply or {}).get("__error__") or "").strip()
    message = ""
    found = _MESSAGE_FIELD.search(raw)
    if found:
        message = " ".join(found.group(1).split())
    status = _error_status(reply) or 0
    # The number stays in every sentence: "HTTP 404" is what someone quotes
    # when they ask for help, and it is what distinguishes a retired model
    # from a bad key from an exhausted quota at a glance.
    tag = f" (HTTP {status})" if status else ""
    lowered = (message or raw).lower()
    if status in (401, 403) or ("api key" in lowered and "not valid" in lowered):
        return (f"{label} refused the API key{tag} — open Settings and paste "
                f"the whole key again")
    if status == 429 or "quota" in lowered or "rate limit" in lowered:
        return f"{label} hit the free-tier limit{tag} — try again in a minute"
    if status in (400, 404) and ("model" in lowered or not message):
        return (f"{label} does not serve {model or 'that model'}{tag} — leave "
                f"Model blank in Settings to follow the current free-tier "
                f"default")
    if message:
        return f"{label}: {message[:200]}{tag}"
    if status in _TRANSIENT_HTTP:
        return f"{label} is unreachable right now{tag}"
    if "urlopen error" in lowered or "timed out" in lowered or "ssl" in lowered:
        return (f"{label} could not be reached from this machine — check the "
                f"connection and try again")
    return raw[:180] or f"{label} did not answer"


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
            last_error = {"__error__": f"HTTP {exc.code} {_scrub(detail)}",
                          "__status__": exc.code}
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
            return {"__error__": f"HTTP {status}", "__status__": status}
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


def _request_for(provider: str, model: str, system: str, user: str,
                 cfg: dict, key: str) -> tuple[str, dict, dict]:
    """``(url, payload, headers)`` for one call against one provider."""
    if provider == "gemini":
        base = config.GEMINI_BASE_URL.rstrip("/")
        url = f"{base}/models/{model}:generateContent"
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": 0.85,
                "maxOutputTokens": 2048,
                **_gemini_thinking(model),
            },
        }
        # x-goog-api-key (not Bearer): it is what Google's own endpoint
        # speaks, and the only header the new AQ. auth keys work with —
        # on OpenAI-compatible Bearer routes they come back 400/401.
        headers = {"x-goog-api-key": key}
        return url, payload, headers
    if provider == "groq":
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
    return url, payload, headers


def ask_json(system: str, user: str, settings: dict | None,
             timeout: float = config.AI_TIMEOUT,
             report: dict | None = None) -> tuple[dict | None, str, str]:
    """Ask the provider chain for one JSON object.

    Returns ``(data, provider, notice)`` — ``data`` is ``None`` when the whole
    chain failed (offline fallback is then the caller's job), ``provider`` names
    the one that answered, and ``notice`` is a short user-facing explanation.

    Every provider is tried across the model ids worth calling (see
    :func:`model_candidates`), so a retired id in the settings demotes itself
    instead of failing the render. When ``report`` is passed it receives
    ``provider``/``model`` (what answered) and ``replaced_model`` (what was
    skipped because the provider retired it), which is how the upload pack and
    the Settings panel tell the truth about which model wrote them.
    """
    cfg = read(settings)
    providers = chain(cfg)
    if not providers:
        return None, "offline", ""
    if report is not None:
        report["requested_model"] = str(cfg.get("model") or "")
    last_error = ""
    for name in providers:
        key = key_for(cfg, name)
        if not key:
            last_error = f"{_LABEL.get(name, name)} needs an API key"
            continue
        models = model_candidates(cfg, name) or [_MODEL_DEFAULT.get(name, "")]
        swap_error = ""
        for index, model in enumerate(models):
            if not model:
                continue
            url, payload, headers = _request_for(name, model, system, user,
                                                 cfg, key)
            parsed = _post_json(url, payload, headers, timeout)
            if not parsed:
                last_error = f"{_LABEL.get(name, name)} replied with nothing"
                continue
            if "__error__" in parsed:
                nxt = models[index + 1] if index + 1 < len(models) else ""
                if _dead_model(parsed) and nxt:
                    # The id is retired for this key: that is a *model*
                    # problem, so the next candidate is worth the call. A bad
                    # key or an empty quota is not, hence the break below.
                    # The headline stays the failure of the model Qyro prefers
                    # to use, not of the retired one it skipped past.
                    if report is not None:
                        report["replaced_model"] = model
                        report["model"] = nxt
                    swap_error = swap_error or _humanise(parsed, name, model)
                    last_error = swap_error
                    continue
                last_error = swap_error or _humanise(parsed, name, model)
                break
            content = _chat_text(parsed)
            data = _extract_json(content or "")
            if data is None:
                last_error = f"{model} replied, but not with JSON"
                continue
            if report is not None:
                report["provider"] = name
                report["model"] = model
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
    report: dict = {}
    data, provider, notice = ask_json(system, user, settings, report=report)
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
        "engine_model": report.get("model") or "",
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
                timeout: float = config.AI_TIMEOUT,
                report: dict | None = None) -> tuple[dict | None, str, str]:
    """Rewrite an upload pack through the engine.

    Returns ``(pack_or_None, provider, notice)`` — ``None`` pack means the
    caller keeps the offline one. ``report`` (optional) learns which model
    actually answered, which is not always the one in the settings: a retired
    id is called second, not first, so the pack can say so honestly.
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
    mine: dict = {}
    data, provider, notice = ask_json(_PACK_SYSTEM, user, settings, timeout=timeout,
                                      report=mine)
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
    used = mine.get("model") or model_for(cfg, provider)
    by = f"{provider}:{used}"
    wanted = str(mine.get("requested_model") or "")
    if wanted and wanted != used:
        # name the swap, or the fix looks like it never happened
        by += f" (settings said {wanted})"
    polished["polished_by"] = by
    polished["polished_model"] = used
    if report is not None:
        report.update(mine)
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
