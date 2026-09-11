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
APP_VERSION = "0.6.1"
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
FONTS_DIR = DATA_DIR / "fonts"        # user-supplied caption fonts (.ttf/.otf)
MODELS_DIR = Path(__file__).resolve().parent / "models"   # optional AI models
TRACK_CACHE_DIR = DATA_DIR / "cache"  # cached subject-tracking analyses
STATE_FILE = DATA_DIR / "state.json"

for _d in (DATA_DIR, MEDIA_DIR, CLIPS_DIR, THUMBS_DIR, SUBS_DIR, AUDIO_DIR,
           FONTS_DIR, TRACK_CACHE_DIR):
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
MIN_CLIP_SECONDS = 25           # shortest allowed short
MAX_CLIP_SECONDS = 90           # longest allowed short (Shorts allow 3 min)
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
#
# v0.6.1: Groq retired ``llama-3.1-8b-instant`` on 2026-08-16 and Google
# deprecated the 2.0 Flash family on 2026-06-01 — the old defaults made every
# call fail with a 404 and forced the "AI unavailable during render" fallback.
# Both defaults now point at the providers' current free-tier models.
AI_PROVIDERS = ("offline", "gemini", "groq", "custom")
DEFAULT_AI_PROVIDER = "offline"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_MODEL = "openai/gpt-oss-20b"
GEMINI_MODEL = "gemini-2.5-flash"
CUSTOM_MODEL = "gpt-4o-mini"
AI_TIMEOUT = 25
# The render path used to hard-code a 12 s polish timeout, which turned a
# cold-start provider call into "AI unavailable during render". Renders get
# the full budget plus one automatic retry on transient errors.
AI_RENDER_TIMEOUT = 25.0
AI_RETRIES = 1
AI_RETRY_BACKOFF = 1.5          # seconds before the retry
SECRET_SETTING_KEYS = ("ai_key", "gemini_key", "groq_key")
ENGINE_SETTINGS_KEYS = ("ai_provider", "ai_model", "ai_base_url")

# Subtitle languages tried one at a time to avoid burst requests / HTTP 429s.
SUB_LANG_CHAIN = ["en", "hi", "en-orig", "en.*", "hi.*"]

# Optional LLM refinement (any OpenAI-compatible endpoint). Off by default —
# the built-in heuristic highlight engine works fully offline.
LLM_API_KEY = os.environ.get("OPENAI_API_KEY", "")
LLM_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

# ==========================================================================
# v0.6.0 — subject tracking, caption fonts/animations, languages, transitions
# ==========================================================================

# --- Subject tracking (the "keep the person in frame" engine) ---------------
# ``auto`` uses a real Haar face cascade when OpenCV + a cascade file are both
# installed and otherwise falls back to the built-in ffmpeg skin+motion heat
# map; ``vision`` forces the built-in tracker (no OpenCV at all); ``face``
# demands the cascade and gives up (static crop) when it is missing; ``off``
# keeps the v0.5.0 left/center/right thirds behaviour.
TRACK_MODES = ("auto", "vision", "face", "off")
DEFAULT_TRACK_MODE = "auto"
TRACK_ZOOMS = ("auto", "tight", "normal", "wide")
DEFAULT_TRACK_ZOOM = "auto"
TRACK_FPS = 5.0                    # analysis frames per second
TRACK_EPSILON = 0.006              # path simplification tolerance (normalised)
TRACK_MAX_KEYFRAMES = 64           # caps the generated ffmpeg expression depth

# v0.6.1 — speaker-aware tracking. The old tracker followed *a* person (the
# biggest face / the middle of the skin+motion blob); in a two-person frame it
# jumped between people or framed nobody. The tracker now:
#   1. separates the skin and motion heat planes and finds up to
#      TRACK_MAX_PERSONS distinct people (connected skin blobs),
#   2. reads a voice-activity envelope from the clip's own audio (ffmpeg
#      ebur128, free and offline),
#   3. scores each person by motion *while the voice is active* — the active
#      speaker's head/mouth moves during speech, a listener sits still — and
#      follows the winner, switching when the other person clearly takes the
#      turn (hysteresis: SWITCH_RATIO for SWITCH_HOLD seconds).
# With no audio (or no speech) it falls back to the person who is most
# persistently present — "mainly present in the frame".
TRACK_ALGO_VERSION = 2             # bumped so v0.6.0 track caches are re-analysed
TRACK_MAX_PERSONS = 3              # candidates kept per frame
PERSON_MIN_MASS = 0.06             # blob must hold >= 6% of the frame's skin
TRACK_MATCH_JUMP = 0.22            # max normalised jump between frames
TRACK_MAX_MISS = 1.0               # seconds a track survives without a match
SWITCH_RATIO = 1.4                 # challenger must beat incumbent by this...
SWITCH_HOLD = 1.0                  # ...for this many seconds, before a switch
SPEAK_EMA = 0.70                   # per-frame smoothing of speech motion
PRESENCE_EMA = 0.85
VOICE_GRID = 0.1                   # seconds per loudness sample (ebur128 rate)
VOICE_LO_REL = 0.15                # percentile used only for the flat-curve test
VOICE_HI_REL = 0.90                # percentile taken as the speech ceiling
VOICE_DYNAMIC_DB = 25.0            # speech ceiling minus this = the voice threshold
VOICE_SPREAD_DB = 6.0              # below this dynamic range the curve is flat
VOICE_FLOOR_DB = -85.0             # below this the frame is digital silence

# --- Caption fonts ----------------------------------------------------------
# A font *id*, not a font name: Qyro resolves the id against the fonts actually
# installed on the machine (``fc-list`` when available) and walks the stack
# until it finds one, so the same job renders on a phone, a Mac and Windows.
CAPTION_FONTS = (
    "auto", "bold", "rounded", "condensed", "serif", "mono",
    "impact", "hand", "devanagari", "devanagari-serif",
)
DEFAULT_CAPTION_FONT = "auto"

FONT_STACKS = {
    "auto": [],                       # keep config.CAPTION_FONT
    "bold": ["Montserrat ExtraBold", "Montserrat", "Archivo Black",
             "Poppins SemiBold", "Poppins", "DejaVu Sans"],
    "rounded": ["Nunito", "Baloo 2", "Quicksand", "Comic Sans MS",
                "DejaVu Sans"],
    "condensed": ["Oswald", "Barlow Condensed", "Roboto Condensed",
                  "DejaVu Sans Condensed", "DejaVu Sans"],
    "serif": ["Playfair Display", "Merriweather", "Georgia",
              "DejaVu Serif"],
    "mono": ["JetBrains Mono", "Fira Code", "DejaVu Sans Mono"],
    "impact": ["Impact", "Anton", "Haettenschweiler", "Archivo Black",
               "DejaVu Sans"],
    "hand": ["Caveat", "Patrick Hand", "Kalam", "Comic Sans MS",
             "DejaVu Sans"],
    "devanagari": ["Noto Sans Devanagari", "Mukta", "Mangal", "Nirmala UI",
                   "Kohinoor Devanagari", "Shobhika", "Hind"],
    "devanagari-serif": ["Noto Serif Devanagari", "Tiro Devanagari Hindi",
                         "Shobhika", "Kohinoor Devanagari", "Mangal"],
}
# Fonts that can actually draw Devanagari. Used to auto-rescue Hindi captions
# when the chosen font only has Latin glyphs (otherwise every word is tofu).
DEVANAGARI_FONTS = (
    "Noto Sans Devanagari", "Noto Serif Devanagari", "Mukta", "Mangal",
    "Nirmala UI", "Kohinoor Devanagari", "Shobhika", "Hind",
    "Tiro Devanagari Hindi", "Sanskrit Text", "Devanagari",
)
FONT_EXTENSIONS = (".ttf", ".otf", ".ttc", ".otc")

# --- Caption animation ------------------------------------------------------
# libass override tags burned into every Dialogue line. Each entry maps to the
# tag prefix Qyro emits; ``karaoke`` additionally re-times the words with \k.
CAPTION_ANIMS = (
    "none", "fade", "pop", "zoom", "bounce", "glow", "blurin",
    "karaoke", "drop",
)
DEFAULT_CAPTION_ANIM = "fade"
CAPTION_ANIM_LABELS = {
    "none": "No animation",
    "fade": "Fade in / out",
    "pop": "Pop in (word punch)",
    "zoom": "Zoom settle",
    "bounce": "Bounce",
    "glow": "Glow pulse",
    "blurin": "Blur in",
    "karaoke": "Karaoke sweep",
    "drop": "Drop in",
}
ANIM_FADE_MS = (110, 110)          # \fad(in, out)
ANIM_POP_MS = 150
ANIM_KARAOKE_CS = 40               # centiseconds per word for \k timing

# --- Clip transitions -------------------------------------------------------
# Every variant only touches the clip's first/last frames and is
# *duration-preserving*, so burned-in captions can never drift out of sync.
# Jump cuts stay hard cuts on purpose: cross-fading 30 rapid speech cuts turns
# a punchy short into mush, which is why there is no xfade option.
TRANSITIONS = ("none", "fade", "dip", "flash", "slide")
DEFAULT_TRANSITION = "fade"
TRANSITION_LABELS = {
    "none": "No transition",
    "fade": "Fade to black",
    "dip": "Dip to white",
    "flash": "Opening flash",
    "slide": "Slide up",
}
TRANSITION_SECONDS = 0.28          # edge fade / dip length
TRANSITION_XFADE_SECONDS = 0.18    # cross-fade between jump-cut segments
TRANSITION_MAX_XFADE = 60          # beyond this many cuts, xfade is skipped

# --- Languages --------------------------------------------------------------
# ``language`` drives three things: which subtitle language is requested from
# YouTube, how captions are chunked/cased, and which font is auto-selected.
LANGUAGES = ("auto", "en", "hi", "hinglish")
DEFAULT_LANGUAGE = "auto"
LANGUAGE_LABELS = {
    "auto": "Auto-detect",
    "en": "English",
    "hi": "हिन्दी (Hindi)",
    "hinglish": "Hinglish",
}
LANGUAGE_SUBS = {          # subtitle language chain per choice (429-safe order)
    "en": ["en", "en-orig", "en.*"],
    "hi": ["hi", "hi.*", "en"],
    "hinglish": ["hi", "en", "hi.*", "en.*"],
    "auto": ["en", "hi", "en-orig", "en.*", "hi.*"],
}
DEVANAGARI_RANGE = (0x0900, 0x097F)
# Hindi reads better with more words on screen and no ALL-CAPS shouting.
HINDI_WORDS_PER_LINE = 6
HINDI_FONT_SIZE_SCALE = 1.10

# --- "Only the good parts" quality gate -------------------------------------
# Applied to every automatic highlight before it is rendered: dead air at the
# edges is trimmed, windows that are mostly silence are re-scored, and the
# first/last partial words are pushed to the nearest transcript boundary.
QUALITY_GATE_DEFAULT = True
TRIM_EDGE_SILENCE = 0.35           # trim pauses longer than this at the edges
MIN_SPEECH_RATIO = 0.55            # below this, the window is penalised
MAX_INNER_SILENCE = 2.5            # a single pause longer than this = penalty
QUALITY_PENALTY = 0.55             # score multiplier for a failing window

# --- v0.6.1 endings: never cut a line in half --------------------------------
# A short must end when the *speaker* stops, not wherever the score window
# happens to stop. Windows that end mid-flow are pushed to the next natural
# stop (a pause after a sentence, terminal punctuation, or the source end),
# and every cut gets a breath of silence after the last word.
SENTENCE_END_GAP = 0.35            # a pause >= this after a sentence = clean stop
WINDOW_END_SLACK = 3.0             # a window may run past max_dur to land a clean stop
MID_FLOW_PENALTY = 0.70            # score multiplier for windows ending mid-flow
CLEAN_END_BONUS = 1.10             # score bonus for windows ending on a pause
SWEET_SPOT_RATIO = 0.62            # preferred duration as a fraction of max_dur
END_TAIL_SECONDS = 0.45            # breathing room after the last spoken word
END_TAIL_MAX = 0.9                 # never grow a window by more than this for the tail
END_TAIL_MIN = 0.18                # even a tight cut gets this much air
BOUNDARY_MAX_SHIFT = 1.2           # beat-snapped / manual bounds may move this much
BOUNDARY_SWALLOW_LIMIT = 3.0       # max speech a boundary repair may swallow/skip
