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
    """Locate an ffmpeg binary: env override, system PATH, imageio wheel."""
    env = os.environ.get("AUTOSHORTS_FFMPEG")
    if env and Path(env).exists():
        return env
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    raise RuntimeError(
        "No ffmpeg found. Install ffmpeg on your system (Termux: "
        "`pkg install ffmpeg`) or `pip install imageio-ffmpeg`."
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
DEFAULT_STYLE = "blur"

# --- v0.4.0 render options ---------------------------------------------------
# Framing formats, caption styles and delivery speeds the API accepts. Every
# one of them is validated by both servers before a job is queued.
FORMATS = ("vertical", "square", "wide")
STYLES = ("blur", "crop", "fill", "fit", "smart")
CAPTION_STYLES = ("classic", "pop", "minimal")
CAPTION_POSITIONS = ("standard", "low")
DEFAULT_CAPTIONS_POS = "standard"
DEFAULT_CAPTIONS_BOX = False

SPEED_RANGE = (0.5, 2.0)        # inclusive; atempo stays clean inside this
DEFAULT_SPEED = 1.0

OUTPUT_SIZES = {                # format -> quality -> (width, height)
    "vertical": {"fast": (720, 1280), "full": (1080, 1920)},
    "square": {"fast": (720, 720), "full": (1080, 1080)},
    "wide": {"fast": (1280, 720), "full": (1920, 1080)},
}
DEFAULT_FORMAT = "vertical"
DEFAULT_CAPTIONS = "classic"

CAPTION_FONT = os.environ.get("AUTOSHORTS_FONT", "DejaVu Sans")
CAPTION_WORDS_PER_LINE = 4      # words shown on screen at once
CAPTION_MIN_FONT = 24           # auto-fit never shrinks a caption below this
CAPTION_BOX_ALPHA = 0xC8        # near-opaque BackColour when captions_box

# SMART framing: motion is measured per 2s chunk across left/center/right
# thirds of a tiny grayscale proxy. A challenger third must clearly beat the
# incumbent (hysteresis) or the crop stays put; calm scenes pull the crop to
# the center; any analysis failure falls back to a static center crop.
SMART_CHUNK_SECONDS = 2.0
SMART_HYSTERESIS = 1.25         # challenger must exceed incumbent * 1.25
SMART_CALM_YDIF = 0.35          # below this mean YDIF a chunk counts as calm
SMART_PROXY_WIDTH = 240         # proxy scale width for motion analysis
SMART_POSITIONS = ("left", "center", "right")

# Progress bar (drawbox) colour: amber, top edge.
PROGRESS_COLOR = "0xFFBF00"

# Waveform: loudness bars saved on every clip for the card UI.
WAVEFORM_BARS = 24

# Subtitle languages tried one at a time to avoid burst requests / HTTP 429s.
SUB_LANG_CHAIN = ["en", "hi", "en-orig", "en.*", "hi.*"]

# Optional LLM refinement (any OpenAI-compatible endpoint). Off by default —
# the built-in heuristic highlight engine works fully offline.
LLM_API_KEY = os.environ.get("OPENAI_API_KEY", "")
LLM_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
