"""T4 — Logo Remover: static corner marks gone before anything is framed.

A ``logo_box`` render option describes the mark to erase::

    {"preset": "topleft|topright|bottomleft|bottomright|custom",
     "size":   "S|M|L",           # ignored for custom
     "feather": 0..24,            # px of soft margin around the box
     "x": 0.0, "y": 0.0, "w": 0.2, "h": 0.08}   # custom only, frame fractions

Every rect is stored as a *fraction of the source frame* so the same box
removes the mark at 720p, 1080p or 1440p. Removal uses ffmpeg ``delogo``
(bilinear inpainting of the patch); when the box cannot be used — it touches a
frame edge, or the source size is unknown — the builder degrades to a
region-limited ``boxblur`` so the mark is still hidden and the render still
runs. Nothing here ever raises: a bad spec simply means "no filter".
"""
from __future__ import annotations

from . import config


def clean_spec(spec) -> dict | None:
    """Validate/normalise a user ``logo_box`` — ``None`` means "off".

    Accepts ``None``, ``false``, ``{}`` (all off) or a spec dict. Raises
    ``ValueError`` for values that are clearly wrong so the HTTP layer can
    answer 422 instead of silently ignoring a typo.
    """
    if spec is None or spec is False:
        return None
    if spec is True:
        spec = {}
    if not isinstance(spec, dict):
        raise ValueError("logo_box must be an object")
    if not spec or spec.get("enabled") is False:
        return None

    preset = str(spec.get("preset") or "topleft").strip().lower()
    if preset not in config.LOGO_PRESETS:
        raise ValueError(
            "logo_box.preset must be one of: " + ", ".join(config.LOGO_PRESETS)
        )
    # sizes are the uppercase family names (S/M/L); accept "m" too
    size = str(spec.get("size") or config.DEFAULT_LOGO_SIZE).strip().upper()
    if size not in config.LOGO_SIZES:
        raise ValueError(
            "logo_box.size must be one of: " + ", ".join(config.LOGO_SIZES)
        )
    raw_feather = spec.get("feather", config.DEFAULT_LOGO_FEATHER)
    if isinstance(raw_feather, bool):
        raise ValueError("logo_box.feather must be an integer")
    try:
        feather = int(raw_feather)
    except (TypeError, ValueError):
        raise ValueError("logo_box.feather must be an integer")
    lo, hi = config.LOGO_FEATHER_RANGE
    if not lo <= feather <= hi:
        raise ValueError(
            f"logo_box.feather must be between {lo} and {hi}"
        )

    out: dict = {
        "preset": preset,
        "size": size,
        "feather": max(lo, min(hi, feather)),
    }
    if preset == "custom":
        for key in ("x", "y", "w", "h"):
            raw = spec.get(key, 0.0 if key in ("x", "y") else 0.18)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise ValueError(f"logo_box.{key} must be a number between 0 and 1")
            value = float(raw)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"logo_box.{key} must be a number between 0 and 1")
            out[key] = round(value, 4)
        if out["w"] < 0.01 or out["h"] < 0.01:
            raise ValueError("logo_box.w and logo_box.h must be >= 0.01")
    return out


def rect_for(spec: dict, frame_w: int, frame_h: int) -> tuple[int, int, int, int]:
    """Pixel ``(x, y, w, h)`` of the mark inside a ``frame_w``x``frame_h`` frame.

    Corner presets size the box from the frame width (marks are wide), keep a
    small margin off the edge and clamp so the box never leaves the frame.
    """
    frame_w = max(16, int(frame_w))
    frame_h = max(16, int(frame_h))
    if spec.get("preset") == "custom":
        w = int(round(float(spec["w"]) * frame_w))
        h = int(round(float(spec["h"]) * frame_h))
        x = int(round(float(spec["x"]) * frame_w))
        y = int(round(float(spec["y"]) * frame_h))
    else:
        frac = config.LOGO_SIZE_FRACTION.get(str(spec.get("size")), 0.16)
        w = int(round(frac * frame_w))
        h = max(8, int(round(w * config.LOGO_ASPECT)))
        margin_x = int(round(config.LOGO_MARGIN * frame_w))
        margin_y = int(round(config.LOGO_MARGIN * frame_h))
        x = margin_x if "left" in str(spec["preset"]) else frame_w - margin_x - w
        y = margin_y if str(spec["preset"]).startswith("top") else frame_h - margin_y - h

    w = max(8, min(w, frame_w - 2))
    h = max(8, min(h, frame_h - 2))
    x = max(0, min(x, frame_w - w))
    y = max(0, min(y, frame_h - h))
    return x, y, w, h


def apply_feather(rect: tuple[int, int, int, int], frame_w: int, frame_h: int,
                  feather: int) -> tuple[int, int, int, int]:
    """Grow the box by ``feather`` px on each side, clamped to the frame."""
    x, y, w, h = rect
    f = max(0, int(feather))
    nx = max(0, x - f)
    ny = max(0, y - f)
    nw = min(int(frame_w), w + (x - nx) + f)
    nh = min(int(frame_h), h + (y - ny) + f)
    return nx, ny, nw, nh


def delogo_usable(rect: tuple[int, int, int, int], frame_w: int,
                  frame_h: int) -> bool:
    """``delogo`` needs the patch strictly inside the frame (1px border)."""
    x, y, w, h = rect
    return x >= 1 and y >= 1 and 0 < w < frame_w and 0 < h < frame_h and (
        x + w <= frame_w - 1 and y + h <= frame_h - 1
    )


def delogo_filter(rect: tuple[int, int, int, int]) -> str:
    x, y, w, h = rect
    return f"delogo=x={x}:y={y}:w={w}:h={h}"


def blur_filter(rect: tuple[int, int, int, int], strength: int = 3) -> str:
    """The fallback: blur just the patch (crop + boxblur + overlay).

    Returned as a fragment that consumes ``[lbin]`` and produces ``[lbout]``
    so the caller can splice it into a larger filtergraph.
    """
    x, y, w, h = rect
    r = max(1, int(strength))
    return (
        f"[lbin]split=2[lbkeep][lbcut];"
        f"[lbcut]crop={w}:{h}:{x}:{y},boxblur={r}:{r * 2}[lbblur];"
        f"[lbkeep][lbblur]overlay={x}:{y}[lbout]"
    )


def logo_stage(spec: dict | None, frame_w: int, frame_h: int,
               vin: str = "0:v") -> tuple[str, str] | None:
    """(graph_prefix, output_label) for the logo pass, or ``None`` for "off".

    The prefix is a complete filtergraph fragment ending in a label the
    caller feeds to the framing chain. ``delogo`` is preferred; a patch that
    cannot use it (or a feathered box that reaches the frame edge) falls back
    to a blurred region, so removal never fails a render.
    """
    if not spec or frame_w <= 0 or frame_h <= 0:
        return None
    try:
        rect = rect_for(spec, frame_w, frame_h)
        rect = apply_feather(rect, frame_w, frame_h, int(spec.get("feather", 0)))
        if delogo_usable(rect, frame_w, frame_h):
            return f"[{vin}]{delogo_filter(rect)}[vlogo]", "vlogo"
        strength = max(2, min(30, int(min(rect[2], rect[3]) * 0.12) + 2))
        graph = blur_filter(rect, strength).replace("[lbin]", f"[{vin}]")
        graph = graph.replace("[lbout]", "[vlogo]")
        return graph, "vlogo"
    except Exception:
        return None  # never break a render over a logo box


def describe(spec: dict | None, frame_w: int = 0, frame_h: int = 0) -> dict:
    """UI-facing summary of what the remover will do (shown on the card)."""
    if not spec:
        return {"enabled": False}
    info: dict = {
        "enabled": True,
        "preset": spec.get("preset", "topleft"),
        "size": spec.get("size", config.DEFAULT_LOGO_SIZE),
        "feather": int(spec.get("feather", config.DEFAULT_LOGO_FEATHER)),
        "method": "delogo",
    }
    if frame_w and frame_h:
        try:
            rect = apply_feather(
                rect_for(spec, frame_w, frame_h), frame_w, frame_h,
                int(spec.get("feather", 0)),
            )
            x, y, w, h = rect
            info["rect"] = {"x": x, "y": y, "w": w, "h": h}
            # Fractions too, so the UI can draw the same box over a preview of
            # any resolution (the mark is defined that way on purpose).
            info["box"] = {
                "x": round(x / frame_w, 4), "y": round(y / frame_h, 4),
                "w": round(w / frame_w, 4), "h": round(h / frame_h, 4),
            }
            if not delogo_usable(rect, frame_w, frame_h):
                info["method"] = "boxblur"
        except Exception:
            pass
    return info
