"""Shared render-option + selector validation used by BOTH servers.

Keeping validation in one place guarantees `server.py` (FastAPI) and
`server_stdlib.py` implement the SAME API, status codes and error shapes.

All failures raise :class:`ValidationError` carrying an HTTP status
(422 for bad values) and a human-readable message. Servers translate that
into ``{"detail": message}`` JSON.
"""
from __future__ import annotations

from . import config


class ValidationError(Exception):
    def __init__(self, detail: str, status: int = 422):
        super().__init__(detail)
        self.detail = detail
        self.status = status


def need_int(body: dict, name: str, default: int, lo: int, hi: int) -> int:
    v = body.get(name, default)
    # bool is a subclass of int — reject explicitly (e.g. count=true)
    if isinstance(v, bool):
        raise ValidationError(f"{name} must be an integer between {lo} and {hi}")
    if isinstance(v, float):
        if not v.is_integer():
            raise ValidationError(f"{name} must be an integer between {lo} and {hi}")
        v = int(v)
    if not isinstance(v, int) or not (lo <= v <= hi):
        raise ValidationError(f"{name} must be an integer between {lo} and {hi}")
    return v


def need_float(body: dict, name: str, default: float, lo: float, hi: float) -> float:
    v = body.get(name, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValidationError(f"{name} must be a number between {lo} and {hi}")
    fv = float(v)
    if not (lo <= fv <= hi):
        raise ValidationError(f"{name} must be a number between {lo} and {hi}")
    return fv


def need_choice(body: dict, name: str, default: str, choices: tuple[str, ...]) -> str:
    v = body.get(name, default)
    if v not in choices:
        raise ValidationError(f"{name} must be one of: {', '.join(choices)}")
    return v


def need_bool(body: dict, name: str, default: bool) -> bool:
    v = body.get(name, default)
    if not isinstance(v, bool):
        raise ValidationError(f"{name} must be true or false")
    return v


def need_speed(body: dict, name: str = "speed", default: float = config.DEFAULT_SPEED) -> float:
    """Speed must be a number; boolean true/false is rejected with 422."""
    v = body.get(name, default)
    if isinstance(v, bool):
        raise ValidationError(f"{name} must be a number between {config.SPEED_MIN} and {config.SPEED_MAX}")
    if not isinstance(v, (int, float)):
        raise ValidationError(f"{name} must be a number between {config.SPEED_MIN} and {config.SPEED_MAX}")
    fv = float(v)
    if not (config.SPEED_MIN <= fv <= config.SPEED_MAX):
        raise ValidationError(f"{name} must be a number between {config.SPEED_MIN} and {config.SPEED_MAX}")
    return fv


def parse_render_opts(body: dict) -> dict:
    """Validate + normalize render options shared by shorts/preview/manual/rerender.

    Keys: style, quality, format, captions, captions_pos, captions_box,
    speed, progress, silence, loud.
    """
    return {
        "style": need_choice(body, "style", config.DEFAULT_STYLE, config.STYLES),
        "quality": need_choice(body, "quality", config.DEFAULT_QUALITY, config.QUALITIES),
        "format": need_choice(body, "format", config.DEFAULT_FORMAT, config.FORMATS),
        "captions": need_choice(body, "captions", config.DEFAULT_CAPTIONS, config.CAPTIONS_STYLES),
        "captions_pos": need_choice(body, "captions_pos", config.DEFAULT_CAPTIONS_POS, config.CAPTIONS_POSITIONS),
        "captions_box": need_bool(body, "captions_box", config.DEFAULT_CAPTIONS_BOX),
        "speed": need_speed(body, "speed", config.DEFAULT_SPEED),
        "progress": need_bool(body, "progress", config.DEFAULT_PROGRESS),
        "silence": need_bool(body, "silence", config.DEFAULT_SILENCE),
        "loud": need_bool(body, "loud", config.DEFAULT_LOUD),
    }


def parse_selectors(body: dict) -> dict:
    """Validate count/min_dur/max_dur/profile selectors."""
    count = need_int(body, "count", config.DEFAULT_CLIP_COUNT, 1, 12)
    min_dur = need_float(body, "min_dur", float(config.MIN_CLIP_SECONDS), 5, 180)
    max_dur = need_float(body, "max_dur", float(config.MAX_CLIP_SECONDS), 5, 180)
    if max_dur < min_dur:
        raise ValidationError("max_dur must be >= min_dur")
    profile = need_choice(body, "profile", "viral", config.PROFILES)
    return {"count": count, "min_dur": min_dur, "max_dur": max_dur, "profile": profile}


def check_rerender_allowlist(body: dict) -> None:
    """Rerender accepts ONLY render-opt keys + title; unknown keys → 422."""
    unknown = [k for k in body.keys() if k not in config.RERENDER_ALLOWLIST]
    if unknown:
        raise ValidationError(f"Unknown options: {', '.join(sorted(unknown))}")


def normalize_title(title, *, allow_empty: bool = False, max_len: int = 120) -> str:
    if not isinstance(title, str):
        raise ValidationError("title must be a string")
    t = title.strip()
    if not allow_empty and not t:
        raise ValidationError("title must not be blank")
    if len(t) > max_len:
        raise ValidationError(f"title must be at most {max_len} characters")
    return t
