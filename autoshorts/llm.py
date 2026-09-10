"""Optional LLM-assisted polish (any OpenAI-compatible endpoint).

Stdlib-only (urllib). Disabled unless OPENAI_API_KEY is set — the heuristic
engine is the default and runs fully offline.
"""
from __future__ import annotations

import json
import os
import urllib.request


def is_configured() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY", "").strip())


def strip_fences(text: str) -> str:
    """Strip ``` fences from any LLM JSON before parsing.

    Handles ```json ... ```, ``` ... ``` and stray backtick runs.
    """
    s = (text or "").strip()
    if "```" not in s:
        return s
    # Remove fenced blocks, keeping inner content.
    out_lines: list[str] = []
    in_fence = False
    for line in s.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence or True:
            # keep inner lines; drop only the fence markers themselves
            if in_fence or not stripped.startswith("```"):
                out_lines.append(line)
    cleaned = "\n".join(out_lines).strip()
    # Fallback: brute-strip backticks if structure was odd.
    if "```" in cleaned:
        cleaned = cleaned.replace("```json", "").replace("```", "").strip()
    return cleaned


def parse_llm_json(text: str) -> dict:
    """Strip fences then parse LLM JSON output."""
    cleaned = strip_fences(text)
    return json.loads(cleaned)


def polish_clip(title: str, episode_title: str = "", reasons: list[str] | None = None) -> dict:
    """Call the LLM endpoint for polished titles/hashtags. Raises on failure."""
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("No LLM configured")
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    prompt = (
        "You polish short-form video metadata. Reply with ONLY a JSON object "
        '{"titles": [3 strings], "hashtags": [up to 12 starting with #], '
        '"description": "2-4 lines"} — no markdown fences.\n'
        f"Clip title: {title}\nEpisode: {episode_title}\n"
        f"Signals: {', '.join(reasons or [])}"
    )
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = data["choices"][0]["message"]["content"]
    parsed = parse_llm_json(content)
    # Normalize + guarantee #shorts.
    titles = [str(t)[:100] for t in (parsed.get("titles") or [])][:3]
    tags = [str(t) for t in (parsed.get("hashtags") or [])][:12]
    if "#shorts" not in tags:
        tags = (["#shorts"] + tags)[:12]
    desc = str(parsed.get("description") or "")
    if not titles:
        titles = [title]
    return {"titles": titles, "hashtags": tags, "description": desc}
