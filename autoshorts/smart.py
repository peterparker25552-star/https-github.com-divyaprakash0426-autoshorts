"""Smart-crop helpers: motion winners, time warping, stepped crop expressions.

SMART (portrait from landscape) strategy:
  * Split the source window into 2-second chunks.
  * For each chunk, measure motion (signalstats YDIF) in left/center/right
    thirds and pick a winner with hysteresis + calm→center bias.
  * Build a stepped NUMERIC x(t) crop expression using if(lt(t,…)) chains
    (never ow/iw inside x) with even pixel offsets.
  * Remap chunk positions through silence cuts + speed onto the output
    timeline.
  * ANY analysis failure → static center crop (render must NEVER break).
"""
from __future__ import annotations

SIDES = ("left", "center", "right")

# YDIF-ish motion below this is considered "calm" → bias to center.
CALM_THRESHOLD = 1.0
# A side must beat the previous winner by this much to force a switch.
HYSTERESIS = 0.6
# Chunk length in seconds for motion analysis.
CHUNK_SECONDS = 2.0


def _score(side: str, scores) -> float:
    if scores is None:
        return 0.0
    if isinstance(scores, dict):
        v = scores.get(side, 0.0)
    elif isinstance(scores, (list, tuple)) and len(scores) >= 3:
        v = {"left": scores[0], "center": scores[1], "right": scores[2]}.get(side, 0.0)
    else:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def pick_side(scores=None, prev: str | None = None,
              calm_thresh: float = CALM_THRESHOLD,
              hysteresis: float = HYSTERESIS) -> str:
    """Pick left|center|right from per-third motion scores.

    * empty / all-zero / all-calm scores → "center"
    * otherwise the highest side wins, but hysteresis keeps the previous
      winner unless a challenger beats it clearly.
    """
    vals = {s: _score(s, scores) for s in SIDES}
    best_side = max(SIDES, key=lambda s: vals[s])
    best_val = vals[best_side]
    # empty / calm → center bias
    if best_val <= 0 or best_val < calm_thresh:
        return "center"
    # hysteresis: stay with prev unless challenger is clearly higher
    if prev in SIDES and prev != best_side:
        if best_val - vals[prev] < hysteresis:
            # still require prev to have *some* motion, else allow switch
            if vals[prev] >= calm_thresh:
                return prev
    return best_side


# Aliases so the motion helper is easy to find / test.
choose_side = pick_side
motion_winner = pick_side
pick_motion_side = pick_side
pick_motion_winner = pick_side


def pick_sides_for_chunks(chunk_scores: list, calm_thresh: float = CALM_THRESHOLD,
                           hysteresis: float = HYSTERESIS) -> list[str]:
    """Apply :func:`pick_side` sequentially with hysteresis memory."""
    out: list[str] = []
    prev: str | None = None
    for scores in chunk_scores or []:
        side = pick_side(scores, prev=prev, calm_thresh=calm_thresh, hysteresis=hysteresis)
        out.append(side)
        prev = side
    return out


# ---------------------------------------------------------------------------
# Time warping: source timeline → output timeline through cuts + speed
# ---------------------------------------------------------------------------
def warp_time(t: float, cuts: list[tuple[float, float]] | None = None,
              speed: float = 1.0) -> float:
    """Map a source timestamp to the output timeline.

    * ``cuts`` are removed (silence) intervals in source seconds.
    * ``speed`` is applied after cutting (output = kept / speed).
    * Identity: warp_time(t, [], 1.0) == t.
    * t inside a removed interval clamps to that cut's start position.
    """
    cuts = sorted(cuts or [])
    if speed is None or speed <= 0:
        speed = 1.0
    removed_before = 0.0
    for c0, c1 in cuts:
        if t >= c1:
            removed_before += max(c1 - c0, 0.0)
        elif t >= c0:
            # inside a cut → clamp to cut start
            t = c0
            break
        else:
            break
    kept = max(t - removed_before, 0.0)
    return kept / speed


# Aliases
remap_time = warp_time
warp_timestamp = warp_time
map_time_through_cuts = warp_time


def warp_positions(positions: list[tuple[float, str]],
                   cuts: list[tuple[float, float]] | None = None,
                   speed: float = 1.0) -> list[tuple[float, str]]:
    """Remap [(source_t, side), …] onto the output timeline via warp_time."""
    return [(warp_time(t, cuts, speed), side) for t, side in (positions or [])]


remap_positions = warp_positions


def remap_proxy_to_source(t_proxy: float, proxy_scale: float = 1.0,
                           proxy_offset: float = 0.0) -> float:
    """Remap a timestamp measured on a scaled proxy to source time.

    GLOBAL RULE: Silence cuts detected on any scaled proxy must be
    remapped to source timestamps before cutting.
    """
    if not proxy_scale:
        proxy_scale = 1.0
    return t_proxy * proxy_scale + proxy_offset


def remap_cuts_proxy_to_source(cuts_proxy: list[tuple[float, float]],
                                proxy_scale: float = 1.0,
                                proxy_offset: float = 0.0) -> list[tuple[float, float]]:
    """Remap a list of proxy-detected cuts to source timestamps."""
    return [
        (remap_proxy_to_source(a, proxy_scale, proxy_offset),
         remap_proxy_to_source(b, proxy_scale, proxy_offset))
        for a, b in (cuts_proxy or [])
    ]


# ---------------------------------------------------------------------------
# Stepped numeric crop expression
# ---------------------------------------------------------------------------
def _even(x: float) -> int:
    """Round to an even integer (ffmpeg crop offsets must be even)."""
    return int(round(float(x) / 2.0)) * 2


def side_to_x(side: str, src_w: int, crop_w: int) -> int:
    """Map a side to an even numeric x offset for crop=crop_w:crop_h:x:y."""
    src_w = max(int(src_w), 2)
    crop_w = max(min(int(crop_w), src_w), 2)
    span = max(src_w - crop_w, 0)
    if side == "left":
        return _even(0)
    if side == "right":
        return _even(span)
    return _even(span / 2.0)


def build_crop_x_expr(chunks: list[tuple[float, str]], src_w: int, crop_w: int,
                      chunk_seconds: float = CHUNK_SECONDS) -> str:
    """Build a stepped NUMERIC x(t) expression from [(start_t, side), …].

    Uses nested ``if(lt(t,…))`` chains with literal numbers only — never
    ow/iw inside x — and even offsets. ``chunks`` must be sorted by time.
    Empty input → static center crop value.
    """
    center_x = side_to_x("center", src_w, crop_w)
    if not chunks:
        return str(center_x)
    # Sort + collapse consecutive duplicates for a compact expression.
    ordered = sorted(chunks, key=lambda c: c[0])
    collapsed: list[tuple[float, str]] = []
    for t, side in ordered:
        s = side if side in SIDES else "center"
        if collapsed and collapsed[-1][1] == s:
            continue
        collapsed.append((float(t), s))
    if len(collapsed) == 1:
        return str(side_to_x(collapsed[0][1], src_w, crop_w))
    # Build nested if(lt(t,boundary),x_prev, …) from the end backwards.
    # boundaries are the start times of each subsequent chunk.
    expr = str(side_to_x(collapsed[-1][1], src_w, crop_w))
    for i in range(len(collapsed) - 2, -1, -1):
        boundary = collapsed[i + 1][0]
        x_here = side_to_x(collapsed[i][1], src_w, crop_w)
        expr = f"if(lt(t,{boundary:.3f}),{x_here},{expr})"
    return expr


# Aliases
build_crop_expr = build_crop_x_expr
smart_crop_expr = build_crop_x_expr
build_smart_crop_expr = build_crop_x_expr


def static_center_x(src_w: int, crop_w: int) -> int:
    """Static center-crop fallback (used when analysis fails)."""
    return side_to_x("center", src_w, crop_w)
