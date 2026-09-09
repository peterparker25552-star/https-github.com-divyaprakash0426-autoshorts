"""Configuration: paths, ffmpeg binary discovery, tunable defaults."""
from __future__ import annotations

import os
import shutil
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
    raise RuntimeError(
        "No ffmpeg found. Install ffmpeg on your system or "
        "`pip install imageio-ffmpeg`."
    )


FFMPEG_BIN = _find_ffmpeg()

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

RENDER_HEIGHTS = {              # quality presets -> output height (9:16)
    "fast": 1280,
    "full": 1920,
}
DEFAULT_QUALITY = "fast"
DEFAULT_STYLE = "blur"          # "blur" (safe) or "crop" (center-crop)

CAPTION_FONT = os.environ.get("AUTOSHORTS_FONT", "DejaVu Sans")
CAPTION_WORDS_PER_LINE = 4      # words shown on screen at once

# Subtitle languages requested from YouTube (first match wins)
SUB_LANGS = "en.*,hi.*,hi-en,hinglish"

# Optional LLM refinement (any OpenAI-compatible endpoint). Off by default —
# the built-in heuristic highlight engine works fully offline.
LLM_API_KEY = os.environ.get("OPENAI_API_KEY", "")
LLM_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
