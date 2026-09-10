"""Configuration: paths, ffmpeg binary discovery, tunable defaults."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

# --- Paths -----------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("AUTOSHORTS_DATA", BASE_DIR / "data"))
MEDIA_DIR = DATA_DIR / "media"        # downloaded / generated source videos
CLIPS_DIR = DATA_DIR / "clips"        # rendered shorts
THUMBS_DIR = DATA_DIR / "thumbs"      # clip thumbnails
SUBS_DIR = DATA_DIR / "subs"          # raw + normalized transcripts
STATE_FILE = DATA_DIR / "state.json"

for _d in (DATA_DIR, MEDIA_DIR, CLIPS_DIR, THUMBS_DIR, SUBS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# --- ffmpeg ----------------------------------------------------------------
def _find_ffmpeg() -> str:
    """Locate an ffmpeg binary: env override, imageio-ffmpeg wheel, PATH."""
    env = os.environ.get("AUTOSHORTS_FFMPEG")
    if env and Path(env).exists():
        return env
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    found = shutil.which("ffmpeg")
    if found:
        return found
    # Last resort: common Termux/system locations
    for cand in ("/data/data/com.termux/files/usr/bin/ffmpeg", "/usr/bin/ffmpeg"):
        if Path(cand).exists():
            return cand
    raise RuntimeError(
        "No ffmpeg found. Install ffmpeg on your system or "
        "`pip install imageio-ffmpeg`."
    )


try:
    FFMPEG_BIN = _find_ffmpeg()
except RuntimeError:
    # Allow import without ffmpeg (e.g. for pure validation tests);
    # rendering will raise a clear error when attempted.
    FFMPEG_BIN = os.environ.get("AUTOSHORTS_FFMPEG", "ffmpeg")

# --- Defaults ----------------------------------------------------------------
# The playlist this project was seeded with: "Figuring Out With Raj Shamani"
DEFAULT_PLAYLIST = (
    "https://www.youtube.com/playlist"
    "?list=PLE0Jo6NF_JYO5-phess8GKafKMtPv3tfZ"
)

DEFAULT_CLIP_COUNT = 5          # shorts generated per episode
MIN_CLIP_SECONDS = 20           # shortest allowed short
MAX_CLIP_SECONDS = 60           # longest allowed short
EPISODE_PAGE_SIZE = 25          # episodes fetched per playlist page

# Output geometry: format -> quality -> (width, height)
FORMAT_SIZES: dict[str, dict[str, tuple[int, int]]] = {
    "vertical": {"fast": (720, 1280), "full": (1080, 1920)},
    "square": {"fast": (720, 720), "full": (1080, 1080)},
    "wide": {"fast": (1280, 720), "full": (1920, 1080)},
}

RENDER_HEIGHTS = {              # quality presets -> output height (9:16)
    "fast": 1280,
    "full": 1920,
}
DEFAULT_QUALITY = "fast"
DEFAULT_STYLE = "blur"          # blur|crop|fill|fit|smart
DEFAULT_FORMAT = "vertical"     # vertical|square|wide
DEFAULT_CAPTIONS = "classic"    # classic|pop|minimal
DEFAULT_CAPTIONS_POS = "standard"  # standard|low
DEFAULT_CAPTIONS_BOX = False
DEFAULT_SPEED = 1.0
DEFAULT_PROGRESS = False
DEFAULT_SILENCE = False
DEFAULT_LOUD = False

# Render-option allowlist for rerender (these keys + title only)
RERENDER_ALLOWLIST = frozenset({
    "style", "quality", "format", "captions", "captions_pos",
    "captions_box", "speed", "progress", "silence", "loud", "title",
})

STYLES = ("blur", "crop", "fill", "fit", "smart")
QUALITIES = ("fast", "full")
FORMATS = ("vertical", "square", "wide")
CAPTIONS_STYLES = ("classic", "pop", "minimal")
CAPTIONS_POSITIONS = ("standard", "low")
PROFILES = ("viral", "story", "facts", "energy")

SPEED_MIN = 0.5
SPEED_MAX = 2.0

CAPTION_FONT = os.environ.get("AUTOSHORTS_FONT", "DejaVu Sans")
CAPTION_WORDS_PER_LINE = 4      # words shown on screen at once

# Subtitle languages tried one at a time to avoid burst requests / HTTP 429s.
SUB_LANG_CHAIN = ["en", "hi", "en-orig", "en.*", "hi.*"]

# Optional LLM refinement (any OpenAI-compatible endpoint). Off by default —
# the built-in heuristic highlight engine works fully offline.
LLM_API_KEY = os.environ.get("OPENAI_API_KEY", "")
LLM_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")


def output_size(fmt: str = DEFAULT_FORMAT, quality: str = DEFAULT_QUALITY) -> tuple[int, int]:
    """Return (width, height) for a format+quality pair."""
    try:
        return FORMAT_SIZES[fmt][quality]
    except KeyError:
        return FORMAT_SIZES["vertical"]["fast"]
