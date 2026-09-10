"""Configuration: brand, paths, ffmpeg binary discovery, tunable defaults.

Qyro (v0.5.0) keeps every v0.4.0 knob and adds the tool-chain defaults: 1440p
quality, caption brand presets, the logo-remover box, offline beat snapping,
user audio tracks, the silence tuner and the free AI engine chain.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

# --- Brand -----------------------------------------------------------------
APP_NAME = "Qyro"
APP_VERSION = "0.5.0"
APP_TAGLINE = "long podcasts → captioned vertical shorts"
BRAND_CREDIT = "Made with Qyro"

# Brand gradient (violet -> cyan) on near-black. One place for CSS, SVG,
# manifest and README so the logo never drifts from the UI.
BRAND_VIOLET = "#7C3AED"
BRAND_CYAN = "#22D3EE"
BRAND_BG = "#0A0A0F"

# --- Paths -----------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("AUTOSHORTS_DATA", BASE_DIR / "data"))
MEDIA_DIR = DATA_DIR / "media"        # downloaded / generated source videos
CLIPS_DIR = DATA_DIR / "clips"        # rendered shorts
THUMBS_DIR = DATA_DIR / "thumbs"      # clip thumbnails + pick candidates
AUDIO_DIR = DATA_DIR / "audio"        # user audio tracks + extracted mp3s
SUBS_DIR = DATA_DIR / "subs"          # raw + normalized transcripts
STATE_FILE = DATA_DIR / "state.json"

for _d in (DATA_DIR, MEDIA_DIR, CLIPS_DIR, THUMBS_DIR, SUBS_DIR, AUDIO_DIR):
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
    "1440p": 2560,
}
DEFAULT_QUALITY = "fast"
DEFAULT_STYLE = "blur"

# Qualities the API accepts. ``1440p`` renders at QHD (see OUTPUT_SIZES) and
# the UI labels it "slower on phone" because it encodes ~5x the 720p pixels.
QUALITIES = ("fast", "full", "1440p")
QUALITY_HINTS = {
    "fast": "720p class — fastest",
    "full": "1080p class — balanced",
    "1440p": "1440p QHD — slower on phone",
}

# --- v0.4.0 render options ---------------------------------------------------
# Framing formats, caption styles and delivery speeds the API accepts. Every
# one of them is validated by both servers before a job is queued.
FORMATS = ("vertical", "square", "wide")
PROFILES = ("viral", "story", "facts", "energy")
STYLES = ("blur", "crop", "fill", "fit", "smart")
CAPTION_STYLES = ("classic", "pop", "minimal")
CAPTION_POSITIONS = ("standard", "low")
DEFAULT_CAPTIONS_POS = "standard"
DEFAULT_CAPTIONS_BOX = False

SPEED_RANGE = (0.5, 2.0)        # inclusive; atempo stays clean inside this
DEFAULT_SPEED = 1.0

OUTPUT_SIZES = {                # format -> quality -> (width, height)
    "vertical": {"fast": (720, 1280), "full": (1080, 1920), "1440p": (1440, 2560)},
    "square": {"fast": (720, 720), "full": (1080, 1080), "1440p": (1440, 1440)},
    "wide": {"fast": (1280, 720), "full": (1920, 1080), "1440p": (2560, 1440)},
}
DEFAULT_FORMAT = "vertical"
DEFAULT_CAPTIONS = "classic"

CAPTION_FONT = os.environ.get("AUTOSHORTS_FONT", "DejaVu Sans")
CAPTION_WORDS_PER_LINE = 4      # words shown on screen at once
CAPTION_MIN_FONT = 24           # auto-fit never shrinks a caption below this
CAPTION_BOX_ALPHA = 0xC8        # near-opaque BackColour when captions_box

# --- v0.5.0 animated caption brand presets ---------------------------------
# A brand preset is a *bundle*: font size + colour + outline + position + box,
# applied on top of the user's caption style. ``none`` keeps the v0.4.0 look.
CAPTION_BRANDS = ("none", "qyro-pop", "qyro-minimal", "qyro-neon")
DEFAULT_CAPTION_BRAND = "none"
CAPTION_BRAND_PRESETS = {
    #  label: UI name
    #  font_size_scale: multiplier on the base style's size
    #  primary / outline_colour / emphasis: ASS colours as &HAABBGGRR
    #  box: force the opaque caption box
    #  pos: preferred vertical placement
    #  pop: emphasise word-pop timing even for chunked styles
    "qyro-pop": {
        "label": "Qyro Pop", "font_size_scale": 1.18,
        "primary": "&H00FFFFFF", "outline_colour": "&H00ED3A7C",
        "emphasis": "&H00EED322", "box": False, "pos": "standard",
        "pop": True, "bold": -1,
    },
    "qyro-minimal": {
        "label": "Qyro Minimal", "font_size_scale": 0.86,
        "primary": "&H00F0F4FF", "outline_colour": "&H00000000",
        "emphasis": "&H00ED3A7C", "box": False, "pos": "low",
        "pop": False, "bold": 0,
    },
    "qyro-neon": {
        "label": "Qyro Neon", "font_size_scale": 1.30,
        "primary": "&H00EED322", "outline_colour": "&H00ED3A7C",
        "emphasis": "&H00FFFFFF", "box": True, "pos": "standard",
        "pop": True, "bold": -1,
    },
}

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

# --- v0.5.0 logo remover (static corner marks) ------------------------------
# A brand logo sits in one of four corners; S/M/L size are fractions of the
# frame's SHORT edge, and every rect is expressed as fractions of the source
# frame so it scales with the output resolution. ``custom`` carries explicit
# fractions instead. ``feather`` is the delogo band width in source pixels.
LOGO_PRESETS = ("topleft", "topright", "bottomleft", "bottomright", "custom")
LOGO_SIZES = ("S", "M", "L")
LOGO_SIZE_FRACTION = {"S": 0.10, "M": 0.16, "L": 0.24}
LOGO_ASPECT = 0.30              # box height = width * aspect
LOGO_MARGIN = 0.012             # gap from the frame edge, as a fraction
LOGO_FEATHER_RANGE = (0, 24)    # delogo band width (px); 0 = hard box
DEFAULT_LOGO_SIZE = "M"
DEFAULT_LOGO_FEATHER = 8

# --- v0.5.0 beat sync + audio swap -----------------------------------------
BEAT_MIN_GAP = 0.24             # never closer than ~250 BPM
BEAT_NEIGHBOURS = 4             # a peak must beat its +-N samples
BEAT_ONSET_K = 0.6              # threshold = mean + K * stddev of onsets
BEAT_SNAP_WINDOW = 1.2          # max seconds a cut may move onto a beat
BEAT_CACHE_SUFFIX = ".beats.json"
BEAT_MAX_COUNT = 400            # payload cap; 400 markers cover a 2 h episode
AUDIO_MIXES = ("replace", "duck")
DEFAULT_AUDIO_MIX = "duck"
DUCK_GAIN = 0.2                 # original bed at 20% while ducking
AUDIO_FADE = 0.35               # afade in/out seconds on a swapped track
MAX_AUDIO_BYTES = 40_000_000    # decoded cap for POST /api/audio
AUDIO_EXAMPLES = ("mp3", "m4a", "aac", "wav", "ogg", "opus", "flac", "mka")

# --- v0.5.0 silence tuner ----------------------------------------------------
SILENCE_NOISE_RANGE = (-70.0, -18.0)   # dB floor the tuner may dial
SILENCE_MIN_RANGE = (0.1, 2.0)         # seconds of dead air to qualify
DEFAULT_SILENCE_NOISE = -35.0          # ffmpeg.py's historical default
DEFAULT_SILENCE_MIN = 0.4

# --- v0.5.0 clip tools -------------------------------------------------------
THUMB_CANDIDATES = (1, 12)             # ?n= bounds for the thumb picker
DEFAULT_THUMB_CANDIDATES = 6
TITLE_VARIATIONS = 10                  # Title Lab asks the engine for 10
PROBE_CACHE_SECONDS = 3600.0

# --- v0.5.0 free AI engine ---------------------------------------------------
# Provider chain, cheapest first: offline templates (always work, no key)
# then the two free tiers, then any OpenAI-compatible endpoint. Every request
# key lives in settings, is masked out of every response and never logged.
AI_PROVIDERS = ("offline", "gemini", "groq", "custom")
DEFAULT_AI_PROVIDER = "offline"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_MODEL = "llama-3.1-8b-instant"
GEMINI_MODEL = "gemini-2.0-flash"
CUSTOM_MODEL = "gpt-4o-mini"
AI_TIMEOUT = 25
SECRET_SETTING_KEYS = ("ai_key", "gemini_key", "groq_key")
ENGINE_SETTINGS_KEYS = ("ai_provider", "ai_model", "ai_base_url")

# Subtitle languages tried one at a time to avoid burst requests / HTTP 429s.
SUB_LANG_CHAIN = ["en", "hi", "en-orig", "en.*", "hi.*"]

# Optional LLM refinement (any OpenAI-compatible endpoint). Off by default —
# the built-in heuristic highlight engine works fully offline.
LLM_API_KEY = os.environ.get("OPENAI_API_KEY", "")
LLM_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
