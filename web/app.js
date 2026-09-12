/* Qyro UI — vanilla JS, no build step, no emoji. Polls /api/state. */
const $ = (sel) => document.querySelector(sel);

const LENGTH_PRESETS = {
  short: [25, 40],
  medium: [40, 65],
  long: [60, 90],
  any: [25, 90],
};

const FORMAT_OPTIONS = [
  ["vertical", "vertical 9:16"],
  ["square", "square 1:1"],
  ["wide", "wide 16:9"],
];
const CAPTION_OPTIONS = [
  ["classic", "classic"],
  ["pop", "pop"],
  ["minimal", "minimal"],
];
const CAPTION_POS_OPTIONS = [
  ["standard", "standard"],
  ["low", "low"],
];
const SPEED_OPTIONS = [
  [1, "1.0×"],
  [1.1, "1.1×"],
  [1.25, "1.25×"],
  [1.5, "1.5×"],
];
const STYLE_OPTIONS = [
  ["blur", "blur bg"],
  ["smart", "smart (motion tracked)"],
  ["crop", "center crop"],
  ["fill", "fill"],
  ["fit", "fit + bars"],
];
const QUALITY_OPTIONS = [
  ["fast", "720p · quick"],
  ["full", "1080p · sharp"],
  ["1440p", "1440p · slow on phone"],
];
const BRAND_OPTIONS = [
  ["none", "no brand style"],
  ["qyro-pop", "Qyro Pop"],
  ["qyro-minimal", "Qyro Minimal"],
  ["qyro-neon", "Qyro Neon"],
];
const LOGO_PRESETS = [
  ["", "no logo remover"],
  ["topleft", "top left"],
  ["topright", "top right"],
  ["bottomleft", "bottom left"],
  ["bottomright", "bottom right"],
  ["custom", "custom box"],
];
const AUDIO_MIX_OPTIONS = [
  ["replace", "replace audio"],
  ["duck", "duck voice under track"],
];
// v0.6.0: the server publishes its own catalog on /api/health (it knows
// which fonts are installed and whether the face model is available).
// These lists are only the fallback before the first health reply lands.
const FALLBACK_OPTIONS = {
  fonts: [["auto", "best for the language"]],
  anims: [["none", "none"], ["fade", "fade"], ["pop", "pop"],
          ["zoom", "zoom"], ["bounce", "bounce"], ["glow", "glow"],
          ["blurin", "blur in"], ["karaoke", "karaoke"], ["drop", "drop in"]],
  transitions: [["none", "none"], ["fade", "fade"], ["dip", "dip to black"],
                ["flash", "flash"], ["slide", "slide"]],
  track_modes: [["auto", "track the person"], ["vision", "vision only"],
                ["face", "face model"], ["off", "static crop"]],
  track_zooms: [["auto", "auto"], ["tight", "tight"], ["normal", "normal"],
                ["wide", "wide"]],
  languages: [["auto", "detect"], ["en", "English"], ["hi", "Hindi"],
              ["hinglish", "Hinglish"]],
};

// Fonts get a richer label than the other groups: the server knows which
// family each choice resolves to on THIS machine, and a font that resolves
// to nothing silently falls back. Say so up front instead of after a render.
function fontCatalog() {
  const items = (state.health?.options || {}).fonts;
  if (!Array.isArray(items) || !items.length) return FALLBACK_OPTIONS.fonts;
  return items.map((item) => {
    if (item.id === "auto") return [item.id, item.label || "best for the language"];
    if (item.installed === false) return [item.id, `${item.label} (not installed)`];
    if (item.resolved) return [item.id, `${item.label} — ${item.resolved}`];
    return [item.id, item.label || item.id];
  });
}

// [[value, label], ...] for a catalog group, from the server when available.
function catalog(group) {
  if (group === "fonts") return fontCatalog();
  const items = (state.health?.options || {})[group];
  if (!Array.isArray(items) || !items.length) {
    return FALLBACK_OPTIONS[group] || [];
  }
  return items.map((item) => [item.id, item.label || item.id]);
}

const SIGNAL_LABELS = {
  hook: "Hook",
  numbers: "Numbers",
  questions: "Questions",
  emotion: "Emotion",
  superlatives: "Superlatives",
  energy: "Energy",
  penalties: "Penalties",
};

const PRESET_KEY = "autoshorts.presets";
const CARDOPTS_KEY = "autoshorts.cardOpts";

let state = {
  data: null,
  health: null,
  previews: {},
  manualOpen: {},
  manualVals: {},
  detailsOpen: {},
  cutter: null,
};
let pollTimer = null;
let lastEpisodeSig = "";

function episodeSig(list) {
  return (list || []).map((e) => `${e.id}:${e.status || "new"}:${e.clip_count || 0}:${e.error || ""}`).join("|");
}
function patchEpisodeProgress() {
  const jobs = state.data?.jobs || [];
  const eps = state.data?.episodes || [];
  let needFull = false;
  eps.forEach((ep) => {
    const card = document.querySelector(`.episode[data-id="${ep.id}"]`);
    if (!card) { needFull = true; return; }
    const job = jobs.find((j) => j.episode_id === ep.id);
    const status = ep.status || "new";
    const badge = card.querySelector(".badge.status-new, .badge.status-processing, .badge.status-done, .badge.status-error, .badge[class*='status-']");
    // If error newly appeared or status changed from processing to done, need full
    if (ep.error && !card.querySelector(".stepmsg.error")) needFull = true;
    if (status === "processing" && job) {
      const pct = Math.round((job.progress || 0) * 100);
      const prog = card.querySelector(".progressbar .fill");
      if (prog) prog.style.width = `${pct}%`;
      else needFull = true;
      const step = card.querySelector(".stepmsg:not(.error)");
      if (step) step.textContent = job.message || job.step || "working…";
    } else if (!card.querySelector(".progressbar") === false && status !== "processing") {
      // was processing now not -> need full
      if (card.querySelector(".progressbar")) needFull = true;
    }
  });
  return !needFull;
}

// Server-provided defaults, a preset overlay the user applied, and per-card
// picks. Polling re-renders the cards but never touches the last two, so the
// user's chosen options survive every refresh.
let baseDefaults = {
  count: 5,
  min_dur: 25,
  max_dur: 90,
  profile: "viral",
  style: "blur",
  quality: "fast",
  format: "vertical",
  captions: "classic",
  captions_pos: "standard",
  captions_box: false,
  captions_enabled: true, // v6.2 toggle
  captions_brand: "none",
  speed: 1.0,
  progress: false,
  silence: false,
  loud: false,
  sync_beats: false,
  audio_track: "",
  audio_mix: "duck",
  silence_noise: -35,
  silence_min: 0.5,
  captions_font: "auto",
  captions_anim: "fade",
  transition: "fade",
  track_mode: "auto",
  track_zoom: "auto",
  language: "auto",
  quality_gate: true,
  logo_preset: "",
  logo_size: "M",
  logo_feather: 0,
  playlist_url: "",
};
let presetOverlay = {};
let cardOpts = loadCardOpts();

// ---------------------------------------------------------------- helpers
function loadCardOpts() {
  try { return JSON.parse(localStorage.getItem(CARDOPTS_KEY)) || {}; } catch (e) { return {}; }
}

function saveCardOpts() {
  try { localStorage.setItem(CARDOPTS_KEY, JSON.stringify(cardOpts)); } catch (e) {}
}

function fmtDur(s) {
  if (!s && s !== 0) return "—";
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  if (h) return `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
  return `${m}:${String(sec).padStart(2, "0")}`;
}

function fmtRange(a, b) {
  const f = (t) => `${Math.floor(t / 60)}:${String(Math.round(t % 60)).padStart(2, "0")}`;
  return `${f(a)}–${f(b)}`;
}

function fmtBytes(bytes) {
  const n = Number(bytes || 0);
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = n / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[unit]}`;
}

// ------------------------------------------------------------------ brand
/* Qyro v6.4 mark — the chrome "ion Q": a thick chrome crescent bowl lit by an
   electric-blue rim glow, crossed by a sharp blade tail, wrapped in a thin
   tilted orbit and finished with a four-point spark. Generated by
   tools/make_logo.py from the same geometry as web/logo.svg and the PNG icon
   set, then inlined here so the header needs zero requests. Its ids are
   prefixed "h" so they cannot collide with the intro's gradients. */
const LOGO_SVG = `
<svg viewBox="0 0 64 64" role="img" aria-label="Qyro" focusable="false">
  <defs>
    <linearGradient id="hqchrome" x1="14" y1="10" x2="50" y2="54" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#FFFFFF"/>
      <stop offset="0.38" stop-color="#E4EDFF"/>
      <stop offset="0.72" stop-color="#A8C6F2"/>
      <stop offset="1" stop-color="#5F8DD6"/>
    </linearGradient>
    <linearGradient id="hqblade" x1="26" y1="28" x2="53" y2="50" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#FFFFFF"/>
      <stop offset="0.55" stop-color="#E4EDFF"/>
      <stop offset="1" stop-color="#A8C6F2"/>
    </linearGradient>
    <linearGradient id="hqorbit" x1="10" y1="40" x2="56" y2="26" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#1E5BFF"/>
      <stop offset="0.5" stop-color="#9FC9FF"/>
      <stop offset="1" stop-color="#4D8CFF"/>
    </linearGradient>
    <filter id="hqbloom" x="-45%" y="-45%" width="190%" height="190%">
      <feGaussianBlur stdDeviation="2.9"/>
    </filter>
    <filter id="hqrim" x="-30%" y="-30%" width="160%" height="160%">
      <feGaussianBlur stdDeviation="0.9"/>
    </filter>
    <path id="hqring" d="M20.51 42.98 L19.56 42.23 L18.67 41.42 L17.85 40.53 L17.09 39.59 L16.41 38.59 L15.81 37.54 L15.29 36.44 L14.85 35.31 L14.50 34.16 L14.25 32.97 L14.08 31.78 L14.00 30.57 L14.02 29.36 L14.13 28.15 L14.34 26.96 L14.63 25.79 L15.01 24.64 L15.48 23.52 L16.03 22.45 L16.67 21.42 L17.38 20.44 L18.16 19.51 L19.01 18.66 L19.93 17.86 L20.90 17.14 L21.93 16.50 L23.00 15.94 L24.11 15.46 L25.25 15.07 L26.43 14.76 L27.62 14.55 L28.82 14.43 L30.03 14.40 L31.24 14.47 L32.44 14.62 L33.62 14.87 L34.78 15.21 L35.92 15.63 L37.01 16.14 L38.07 16.74 L39.07 17.41 L40.03 18.16 L40.92 18.97 L41.74 19.86 L42.50 20.80 L43.18 21.80 L43.79 22.85 L44.31 23.94 L44.74 25.07 L45.09 26.23 L45.35 27.41 L45.52 28.61 L45.60 29.82 L45.58 31.03 L45.47 32.23 L45.27 33.43 L44.98 34.60 L44.59 35.75 L44.13 36.86 L43.57 37.94 L42.94 38.97 L42.23 39.95 L41.45 40.87 L40.60 41.73 L39.68 42.53 L38.71 43.25 L37.69 43.89 L36.62 44.45 L35.51 44.93 L34.36 45.33 L33.19 45.63 L32.00 45.85 L32.00 45.85 L32.90 44.31 L33.76 43.33 L34.54 42.43 L35.23 41.56 L35.85 40.70 L36.38 39.84 L36.84 38.99 L37.25 38.16 L37.84 37.56 L38.38 36.93 L38.87 36.25 L39.30 35.54 L39.68 34.80 L40.01 34.03 L40.27 33.23 L40.47 32.42 L40.61 31.60 L40.69 30.77 L40.70 29.94 L40.64 29.10 L40.53 28.28 L40.35 27.46 L40.11 26.66 L39.81 25.88 L39.45 25.13 L39.03 24.41 L38.56 23.72 L38.04 23.06 L37.47 22.46 L36.85 21.89 L36.20 21.38 L35.50 20.91 L34.78 20.50 L34.02 20.15 L33.24 19.86 L32.44 19.62 L31.62 19.45 L30.79 19.35 L29.96 19.30 L29.12 19.32 L28.29 19.40 L27.47 19.55 L26.66 19.76 L25.87 20.03 L25.11 20.36 L24.37 20.75 L23.66 21.19 L22.99 21.69 L22.36 22.24 L21.77 22.83 L21.23 23.46 L20.74 24.14 L20.30 24.85 L19.92 25.59 L19.60 26.36 L19.33 27.15 L19.13 27.96 L18.99 28.79 L18.92 29.62 L18.90 30.45 L18.95 31.29 L19.07 32.11 L19.25 32.93 L19.49 33.73 L19.46 34.65 L19.46 35.62 L19.53 36.63 L19.67 37.68 L19.88 38.78 L20.14 39.93 L20.44 41.20 L20.51 42.98 Z"/>
    <path id="hqtail" d="M24.60 26.40 L25.59 28.14 L26.66 29.82 L27.79 31.45 L28.99 33.04 L30.26 34.57 L31.59 36.07 L32.99 37.52 L34.45 38.93 L35.96 40.31 L37.54 41.64 L39.17 42.95 L40.86 44.23 L42.61 45.47 L44.40 46.70 L46.25 47.89 L48.15 49.07 L50.09 50.23 L52.08 51.36 L54.12 52.49 L56.20 53.60 L56.20 53.60 L54.72 51.80 L53.21 50.05 L51.69 48.37 L50.16 46.73 L48.61 45.15 L47.05 43.63 L45.47 42.15 L43.89 40.71 L42.30 39.32 L40.70 37.98 L39.09 36.67 L37.48 35.41 L35.87 34.18 L34.25 32.98 L32.64 31.81 L31.02 30.68 L29.41 29.57 L27.80 28.49 L26.20 27.44 L24.60 26.40 Z"/>
    <path id="hqorb" fill-rule="evenodd" d="M55.36 26.39 L55.49 27.26 L55.45 28.16 L55.24 29.10 L54.84 30.06 L54.28 31.05 L53.55 32.04 L52.65 33.04 L51.60 34.04 L50.40 35.03 L49.06 35.99 L47.60 36.93 L46.01 37.84 L44.33 38.71 L42.55 39.53 L40.69 40.30 L38.76 41.01 L36.79 41.66 L34.78 42.24 L32.75 42.74 L30.72 43.17 L28.70 43.51 L26.71 43.78 L24.75 43.95 L22.86 44.05 L21.03 44.05 L19.29 43.97 L17.65 43.80 L16.12 43.55 L14.71 43.21 L13.44 42.79 L12.30 42.30 L11.32 41.73 L10.50 41.09 L9.84 40.38 L9.36 39.62 L9.04 38.81 L8.91 37.94 L8.95 37.04 L9.16 36.10 L9.56 35.14 L10.12 34.15 L10.85 33.16 L11.75 32.16 L12.80 31.16 L14.00 30.17 L15.34 29.21 L16.80 28.27 L18.39 27.36 L20.07 26.49 L21.85 25.67 L23.71 24.90 L25.64 24.19 L27.61 23.54 L29.62 22.96 L31.65 22.46 L33.68 22.03 L35.70 21.69 L37.69 21.42 L39.65 21.25 L41.54 21.15 L43.37 21.15 L45.11 21.23 L46.75 21.40 L48.28 21.65 L49.69 21.99 L50.96 22.41 L52.10 22.90 L53.08 23.47 L53.90 24.11 L54.56 24.82 L55.04 25.58 L55.36 26.39 Z M54.25 26.69 L54.36 27.46 L54.31 28.26 L54.09 29.10 L53.70 29.96 L53.15 30.85 L52.44 31.75 L51.57 32.65 L50.56 33.55 L49.40 34.45 L48.12 35.33 L46.72 36.19 L45.20 37.03 L43.59 37.83 L41.89 38.59 L40.11 39.30 L38.28 39.97 L36.40 40.58 L34.48 41.12 L32.55 41.61 L30.62 42.02 L28.70 42.36 L26.81 42.63 L24.95 42.82 L23.15 42.94 L21.43 42.97 L19.78 42.93 L18.23 42.80 L16.78 42.60 L15.45 42.33 L14.25 41.98 L13.18 41.56 L12.26 41.07 L11.50 40.51 L10.88 39.90 L10.44 39.23 L10.15 38.51 L10.04 37.74 L10.09 36.94 L10.31 36.10 L10.70 35.24 L11.25 34.35 L11.96 33.45 L12.83 32.55 L13.84 31.65 L15.00 30.75 L16.28 29.87 L17.68 29.01 L19.20 28.17 L20.81 27.37 L22.51 26.61 L24.29 25.90 L26.12 25.23 L28.00 24.62 L29.92 24.08 L31.85 23.59 L33.78 23.18 L35.70 22.84 L37.59 22.57 L39.45 22.38 L41.25 22.26 L42.97 22.23 L44.62 22.27 L46.17 22.40 L47.62 22.60 L48.95 22.87 L50.15 23.22 L51.22 23.64 L52.14 24.13 L52.90 24.69 L53.52 25.30 L53.96 25.97 L54.25 26.69 Z"/>
    <path id="hqspk" d="M48.00 11.00 L48.82 15.78 L51.40 16.60 L48.82 17.42 L48.00 22.20 L47.18 17.42 L44.60 16.60 L47.18 15.78 Z"/>
    <mask id="hqbladecut" maskUnits="userSpaceOnUse">
      <rect width="64" height="64" fill="#fff"/>
      <path d="M23.73 25.65 L24.61 27.51 L25.58 29.31 L26.64 31.04 L27.77 32.72 L28.99 34.34 L30.30 35.91 L31.68 37.43 L33.15 38.90 L34.69 40.34 L36.32 41.74 L38.04 43.10 L39.83 44.43 L41.70 45.73 L43.66 47.01 L45.69 48.27 L47.81 49.51 L50.00 50.73 L52.28 51.94 L54.64 53.15 L57.07 54.35 L57.07 54.35 L55.45 52.21 L53.81 50.16 L52.18 48.20 L50.54 46.33 L48.89 44.55 L47.24 42.84 L45.59 41.21 L43.93 39.66 L42.27 38.18 L40.60 36.76 L38.93 35.41 L37.26 34.12 L35.58 32.89 L33.90 31.72 L32.21 30.60 L30.52 29.52 L28.83 28.49 L27.13 27.51 L25.43 26.56 L23.73 25.65 Z" fill="#000"/>
    </mask>
    <g id="hqback" mask="url(#hqbladecut)">
      <use href="#hqorb"/><use href="#hqring"/>
    </g>
    <g id="hqall">
      <use href="#hqback"/><use href="#hqtail"/><use href="#hqspk"/>
    </g>
  </defs>
  <rect width="64" height="64" fill="#01030B" rx="14"/>
  <g>
    <use href="#hqall" fill="#1E5BFF" filter="url(#hqbloom)" opacity="0.9"/>
    <use href="#hqall" fill="#4D8CFF" filter="url(#hqrim)" opacity="0.9"/>
    <g mask="url(#hqbladecut)">
      <use href="#hqorb" fill="url(#hqorbit)"/>
      <use href="#hqring" fill="url(#hqchrome)"/>
    </g>
    <use href="#hqtail" fill="url(#hqblade)"/>
    <use href="#hqspk" fill="#FFFFFF"/>
  </g>
</svg>`

/* Icon set: every glyph in the UI is inline SVG — no emoji, no font, no
   network. 24x24, stroke uses currentColor so icons inherit button text. */
const ICON_PATHS = {
  play: `<path d="M8 5.2v13.6L19 12z"/>`,
  search: `<circle cx="11" cy="11" r="7"/><path d="M16.4 16.4L21 21"/>`,
  download: `<path d="M12 3.5v11.5m0 0l-4.5-4.5M12 15l4.5-4.5M4 20h16"/>`,
  upload: `<path d="M12 16.5V4.5m0 0l-4.5 4.5M12 4.5l4.5 4.5M4 15v3.2A1.8 1.8 0 0 0 5.8 20h12.4A1.8 1.8 0 0 0 20 18.2V15"/>`,
  sliders: `<path d="M4 7.5h8M17.5 7.5H20M4 16.5h4M13.5 16.5H20"/><circle cx="14.6" cy="7.5" r="2.4"/><circle cx="10.6" cy="16.5" r="2.4"/>`,
  trash: `<path d="M4.5 7h15M9.5 7V4.6h5V7M6.8 7l.9 13h8.6l.9-13M10.5 11v5.5M13.5 11v5.5"/>`,
  pencil: `<path d="M4.5 19.5h4L20 8a2.1 2.1 0 0 0-3-3L5.5 16.5z"/>`,
  x: `<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>`,
  retry: `<path d="M20 12a8 8 0 1 1-2.4-5.7M20 4v4.2h-4.2"/>`,
  sparkle: `<path d="M11.5 3.5l1.7 4.6 4.6 1.7-4.6 1.7-1.7 4.6-1.7-4.6L5.2 9.8l4.6-1.7z"/><path d="M18 15l.8 2.2 2.2.8-2.2.8L18 21l-.8-2.2-2.2-.8 2.2-.8z"/>`,
  music: `<path d="M9 17.5V5l11-2v12"/><circle cx="6.5" cy="17.5" r="2.5"/><circle cx="17.5" cy="15" r="2.5"/>`,
  captions: `<rect x="3" y="5" width="18" height="14" rx="3"/><path d="M7.5 13.5l2.5-2.5M7.5 16h5M14 13.5l2.5-2.5M14 16h2.5"/>`,
  crop: `<path d="M6.5 2.5v15h15M2.5 6.5h15v15"/>`,
  wand: `<path d="M4.5 19.5L15 9M13 4l.9 2.1L16 7l-2.1.9L13 10l-.9-2.1L10 7l2.1-.9zM19 12l.6 1.6 1.6.6-1.6.6-.6 1.6-.6-1.6-1.6-.6 1.6-.6zM16.5 7.5L19 5"/>`,
  grid: `<rect x="3.5" y="3.5" width="7" height="7" rx="2"/><rect x="13.5" y="3.5" width="7" height="7" rx="2"/><rect x="3.5" y="13.5" width="7" height="7" rx="2"/><rect x="13.5" y="13.5" width="7" height="7" rx="2"/>`,
  link: `<path d="M9.5 14.5l5-5M8 12l-2 2a3.4 3.4 0 0 0 4.8 4.8l2-2M16 12l2-2a3.4 3.4 0 0 0-4.8-4.8l-2 2"/>`,
  film: `<rect x="3" y="4.5" width="18" height="15" rx="3"/><path d="M8 4.5v15M16 4.5v15M3 12h18"/>`,
  clock: `<circle cx="12" cy="12" r="8.5"/><path d="M12 7v5.2l3.4 2"/>`,
  box: `<path d="M12 3.2l8 4.4v8.8L12 20.8 4 16.4V7.6z"/><path d="M4 7.6l8 4.4 8-4.4M12 12v8.8"/>`,
  eye: `<path d="M2.6 12S6.2 5.8 12 5.8 21.4 12 21.4 12 17.8 18.2 12 18.2 2.6 12 2.6 12z"/><circle cx="12" cy="12" r="3"/>`,
  chart: `<path d="M4 20V11M10 20V4.5M16 20v-6.5M2.5 20h19"/>`,
  book: `<path d="M4 5h5.5A2.5 2.5 0 0 1 12 7.5V20a2.2 2.2 0 0 0-2.2-2.2H4zM20 5h-5.5A2.5 2.5 0 0 0 12 7.5V20a2.2 2.2 0 0 1 2.2-2.2H20z"/>`,
  zap: `<path d="M13.5 2.5L5 14h5.5L10 21.5 19 10h-5.5z"/>`,
  scissors: `<circle cx="6.5" cy="6.5" r="2.6"/><circle cx="6.5" cy="17.5" r="2.6"/><path d="M8.7 8.2L20 17M8.7 15.8L20 7"/>`,
  copy: `<rect x="9" y="9" width="11.5" height="11.5" rx="2.5"/><path d="M15 5.5A2 2 0 0 0 13 3.5H5.5a2 2 0 0 0-2 2V13a2 2 0 0 0 2 2"/>`,
  share: `<path d="M12 16.5V4.2m0 0L8 8.4m4-4.2l4 4.2M4.5 14v4.2A1.8 1.8 0 0 0 6.3 20h11.4a1.8 1.8 0 0 0 1.8-1.8V14"/>`,
  check: `<path d="M4.5 12.5l5 5 10-10.5"/>`,
  alert: `<path d="M12 3.8l9.2 16H2.8z"/><path d="M12 9.6v4.6M12 17.2v.1"/>`,
  wave: `<path d="M2.5 12h2.2l2-6.5 3 13 3-9.5 2 5 2-2h4.8"/>`,
  image: `<rect x="3.2" y="4.5" width="17.6" height="15" rx="3"/><circle cx="9" cy="10" r="1.8"/><path d="M4 17l4.8-4.4 3.4 3 3-2.6 4.6 4"/>`,
  external: `<path d="M13.5 4.5H20v6.5M20 4.5l-8.5 8.5M18 14v4.2A1.8 1.8 0 0 1 16.2 20H5.8A1.8 1.8 0 0 1 4 18.2V7.8A1.8 1.8 0 0 1 5.8 6H10"/>`,
  text: `<path d="M4.5 6.5h15M4.5 11.5h15M4.5 16.5h9"/>`,
  folder: `<path d="M3.5 7.5A2 2 0 0 1 5.5 5.5H10l2 2.5h6.5a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/>`,
  key: `<circle cx="8" cy="8" r="4"/><path d="M11 11l9 9M17 17l-2 2M14 14l-2 2"/>`,
};

function icon(name) {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"
    stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">
    ${ICON_PATHS[name] || ""}</svg>`;
}
const ic = (name) => `<span class="ic">${icon(name)}</span>`;

/* Static markup carries <span class="ic" data-icon="play">; this fills them
   once at boot so the HTML stays readable and the icons stay scriptable. */
function hydrateIcons(root) {
  (root || document).querySelectorAll("[data-icon]").forEach((el) => {
    el.innerHTML = icon(el.dataset.icon);
    if (!el.classList.contains("ic")) el.classList.add("ic");
  });
  (root || document).querySelectorAll("[data-logo]").forEach((el) => {
    el.innerHTML = LOGO_SVG;
  });
}

function toast(msg, isError = false) {
  const el = document.createElement("div");
  el.className = "toast" + (isError ? " error" : "");
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), isError ? 6500 : 3500);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) {}
    if (Array.isArray(detail)) detail = detail[0]?.msg || "Invalid request";
    throw new Error(detail);
  }
  return res.json();
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
const trim = (s, n) => (s && s.length > n ? s.slice(0, n - 1) + "…" : s);

function chosen(choices, current) {
  return choices
    .map(([value, label]) =>
      `<option value="${value}"${String(value) === String(current) ? " selected" : ""}>${label}</option>`)
    .join("");
}
const checkedAttr = (value) => (value ? " checked" : "");

// ---------------------------------------------------------------- card opts
function optsFor(epId) {
  return { ...baseDefaults, ...presetOverlay, ...(cardOpts[epId] || {}) };
}

function lengthKey(minDur, maxDur) {
  const hit = Object.entries(LENGTH_PRESETS).find(([, [a, b]]) => a === minDur && b === maxDur);
  return hit ? hit[0] : "any";
}

function readCardOpts(card) {
  if (!card) return { ...baseDefaults, ...presetOverlay };
  const group = card.querySelector(".opt-length");
  const range = LENGTH_PRESETS[group?.value] || LENGTH_PRESETS.any;
  const pick = (selector, fallback) => card.querySelector(selector)?.value ?? fallback;
  const flag = (selector) => !!card.querySelector(selector)?.checked;
  const rawSpeed = card.querySelector(".opt-speed")?.value;
  let speed = parseFloat(rawSpeed);
  if (!Number.isFinite(speed)) speed = baseDefaults.speed;
  speed = Math.min(2, Math.max(0.5, speed));
  return {
    count: +(card.querySelector(".opt-count")?.value ?? baseDefaults.count),
    profile: pick(".opt-profile", baseDefaults.profile),
    min_dur: range[0],
    max_dur: range[1],
    style: pick(".opt-style", baseDefaults.style),
    quality: pick(".opt-quality", baseDefaults.quality),
    format: pick(".opt-format", baseDefaults.format),
    captions: pick(".opt-captions", baseDefaults.captions),
    captions_pos: pick(".opt-cap-pos", baseDefaults.captions_pos),
    captions_box: flag(".opt-cap-box"),
    captions_enabled: !card.querySelector(".opt-captions-enabled") || flag(".opt-captions-enabled"),
    speed,
    progress: flag(".opt-progress"),
    silence: flag(".opt-silence"),
    loud: flag(".opt-loud"),
    captions_brand: pick(".opt-brand", baseDefaults.captions_brand),
    sync_beats: flag(".opt-beats"),
    audio_track: pick(".opt-track", ""),
    audio_mix: pick(".opt-mix", baseDefaults.audio_mix || "duck"),
    logo_preset: pick(".opt-logo", ""),
    logo_size: pick(".opt-logo-size", "M"),
    logo_feather: +pick(".opt-logo-feather", 0) || 0,
    logoCustom: {
      x: +pick(".opt-logo-x", 0.03), y: +pick(".opt-logo-y", 0.03),
      w: +pick(".opt-logo-w", 0.3), h: +pick(".opt-logo-h", 0.1),
    },
    silence_noise: +pick(".opt-silence-noise", -35),
    silence_min: +pick(".opt-silence-min", 0.5),
    captions_font: pick(".opt-cap-font", baseDefaults.captions_font),
    captions_anim: pick(".opt-cap-anim", baseDefaults.captions_anim),
    transition: pick(".opt-transition", baseDefaults.transition),
    track_mode: pick(".opt-track-mode", baseDefaults.track_mode),
    track_zoom: pick(".opt-track-zoom", baseDefaults.track_zoom),
    language: pick(".opt-language", baseDefaults.language),
    quality_gate: !card.querySelector(".opt-quality-gate")
      || card.querySelector(".opt-quality-gate").checked,
  };
}

function rememberCardOpts(card) {
  if (!card || !card.dataset.id) return;
  cardOpts[card.dataset.id] = readCardOpts(card);
  if ($("#rememberOpts")?.checked !== false) saveCardOpts();
}

// ---------------------------------------------------------------- renderers
function renderHealth(h) {
  state.health = h;
  const yt = h.youtube_reachable;
  const llm = h.llm_available;
  const disk = h.disk_free ? `${fmtBytes(h.disk_free)} free` : "disk ?";
  const engine = h.engine || {};
  const ytInfo = h.youtube || {};
  /* A standing subtitle block is why "Generate" keeps failing, so say it up
     front instead of burying it in a job error the user has to trigger. */
  const block = ytInfo.rate_limit || {};
  const session = ytInfo.cookies || {};
  $("#health").innerHTML = `
    <span class="chip ${yt ? "ok" : "bad"}"><span class="dot"></span>YouTube ${yt ? "reachable" : "unreachable — demo mode"}</span>
    <span class="chip ${h.ffmpeg ? "ok" : "bad"}"><span class="dot"></span>ffmpeg ${h.ffmpeg ? "ready" : "missing"}</span>
    <span class="chip"><span class="dot"></span>${esc(disk)}</span>
    <span class="chip ${llm ? "ok" : ""}"><span class="dot"></span>${
      llm ? `AI: ${esc(engine.provider || "custom")}` : "AI off — offline titles"
    }</span>
    <span class="chip ${block.blocked ? "bad" : session.present ? "ok" : ""}"><span class="dot"></span>${
      block.blocked
        ? `YouTube 429 — transcripts paused ${block.minutes} min`
        : session.present ? "YouTube session on" : "no YouTube session"
    }</span>`;
}

function renderStats() {
  const stats = state.data?.stats || { episodes: 0, clips: 0, clip_seconds: 0, avg_score: 0 };
  $("#statstrip").innerHTML = `
    <div class="stat"><b>${stats.episodes}</b><span>episodes</span></div>
    <div class="stat"><b>${stats.clips}</b><span>clips made</span></div>
    <div class="stat"><b>${fmtDur(stats.clip_seconds)}</b><span>clip time</span></div>
    <div class="stat"><b>${Number(stats.avg_score || 0).toFixed(1)}</b><span>avg score</span></div>
    <div class="stat free">100% local · no API key</div>`;
}

function signalBars(signals) {
  if (!signals) return "";
  const keys = Object.keys(SIGNAL_LABELS);
  const values = keys.map((key) => Number(signals[key]) || 0);
  if (!values.some((value) => Math.abs(value) > 0.001)) return "";
  const max = Math.max(1, ...values.map((value) => Math.abs(value)));
  return `<div class="sigbars">${keys.map((key) => {
    const value = Number(signals[key]) || 0;
    const width = Math.round((Math.abs(value) / max) * 100);
    const neg = key === "penalties" ? " neg" : "";
    return `<div class="sigbar${neg}" title="${esc(SIGNAL_LABELS[key])}: weighted contribution ${value}">
      <span class="sname">${esc(SIGNAL_LABELS[key])}</span>
      <span class="strack"><span class="sfill" style="width:${width}%"></span></span>
      <span class="sval">${value.toFixed(1)}</span>
    </div>`;
  }).join("")}</div>`;
}

function waveformStrip(waveform) {
  if (!Array.isArray(waveform) || !waveform.length) return "";
  const bars = waveform
    .map((value) => `<span style="height:${Math.round(12 + Number(value || 0) * 88)}%"></span>`)
    .join("");
  return `<div class="wave" title="loudness waveform">${bars}</div>`;
}

function intelLine(stats) {
  if (!stats) return "";
  return `<div class="intel">${ic("chart")} ${stats.words} words · ${stats.sentences} sentences ·
    ${stats.questions} questions · ${stats.numbers} numbers · ${stats.wpm} wpm</div>`;
}

function renderMoments(epId) {
  const preview = state.previews[epId];
  if (!preview) return "";
  const moments = preview.moments || [];
  if (!moments.length) {
    return `<div class="moments"><div class="stepmsg">No picks found for this length. Try “Any”.</div></div>`;
  }
  return `<div class="moments">
    <div class="minihead">Preview picks — nothing downloaded or rendered</div>
    ${intelLine(preview.stats)}
    ${moments.map((moment) => `
      <div class="moment">
        <div class="moment-main">
          <div class="moment-title">${esc(moment.title)}</div>
          <div class="reasons">${(moment.reasons || []).map((reason) => `<span class="reason">${esc(reason)}</span>`).join("")}</div>
          ${signalBars(moment.breakdown || moment.signals)}
        </div>
        <span class="moment-time">${fmtRange(moment.start, moment.end)}</span>
        <span class="moment-score">${Number(moment.score).toFixed(1)}</span>
      </div>`).join("")}
  </div>`;
}

function renderManualBox(ep) {
  if (!state.manualOpen[ep.id]) return "";
  const vals = state.manualVals[ep.id] || { start: 10, end: 40, title: "" };
  return `<div class="manualbox">
    <div class="minihead">Cut an exact range (5–180 seconds)</div>
    <div class="manual-row">
      <label>Start <input class="manual-start" type="number" min="0" step="0.1" value="${vals.start}"></label>
      <label>End <input class="manual-end" type="number" min="5" step="0.1" value="${vals.end}"></label>
      <input class="manual-title" type="text" maxlength="120" placeholder="Optional title" value="${esc(vals.title)}">
      <button class="btn primary small" onclick="cutManual('${ep.id}', this)">Cut this range</button>
    </div>
  </div>`;
}

function captionLanguageNote(opts) {
  const options = state.health?.options || {};
  const hindiWanted = opts.language === "hi" || opts.language === "hinglish"
    || opts.captions_font === "devanagari";
  if (!hindiWanted || options.devanagari_ready !== false) return "";
  return `<label class="full warn" title="No Devanagari font is installed yet">
    ${ic("alert")} Hindi needs a Devanagari font — install one first
    <button class="btn small" onclick="installFonts(this)">Install fonts</button>
  </label>`;
}

async function installFonts(btn) {
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Installing…";
  try {
    const body = await api("/api/fonts/install", { method: "POST" });
    toast(body.ok ? `Fonts ready: ${body.installed}`
                  : "Fonts unavailable offline", !body.ok);
    await refreshHealth();
    renderEpisodes();
  } catch (err) {
    toast(`Font install failed: ${err.message}`, true);
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

async function refreshHealth() {
  try { state.health = await api("/api/health"); } catch (e) { /* keep old */ }
}

function renderAdvOpts(ep, opts) {
  const open = state.detailsOpen[`adv-${ep.id}`] ? " open" : "";
  return `<details class="advopts"${open}>
    <summary>${ic("sliders")} Fine-tune</summary>
    <div class="advgrid">
      <label class="toggle" title="Turn captions on or off"><span class="ic">${icon("captions")}</span><input class="opt-captions-enabled" type="checkbox"${checkedAttr(opts.captions_enabled !== false)}> captions on</label>
      <label>Captions <select class="opt-captions"${opts.captions_enabled === false ? " disabled" : ""}>${chosen(CAPTION_OPTIONS, opts.captions)}</select></label>
      <label>Cap position <select class="opt-cap-pos"${opts.captions_enabled === false ? " disabled" : ""}>${chosen(CAPTION_POS_OPTIONS, opts.captions_pos)}</select></label>
      <label class="toggle" title="Opaque box behind the words">${ic("box")}<input class="opt-cap-box" type="checkbox"${checkedAttr(opts.captions_box)}${opts.captions_enabled === false ? " disabled" : ""}> caption box</label>
      <label title="Follow the speaker instead of cropping the middle of a wide shot">Follow person <select class="opt-track-mode">${chosen(catalog("track_modes"), opts.track_mode)}</select></label>
      <label title="How much headroom to leave around the person">Framing <select class="opt-track-zoom"${opts.track_mode === "off" ? " disabled" : ""}>${chosen(catalog("track_zooms"), opts.track_zoom)}</select></label>
      <label>Caption font <select class="opt-cap-font">${chosen(catalog("fonts"), opts.captions_font)}</select></label>
      <label>Caption motion <select class="opt-cap-anim">${chosen(catalog("anims"), opts.captions_anim)}</select></label>
      <label>Clip transition <select class="opt-transition">${chosen(catalog("transitions"), opts.transition)}</select></label>
      <label>Language <select class="opt-language">${chosen(catalog("languages"), opts.language)}</select></label>
      <label class="toggle" title="Skip the thin, wordy, rambling windows and keep the tight ones">${ic("wave")}<input class="opt-quality-gate" type="checkbox"${opts.quality_gate === false ? "" : " checked"}> best parts only</label>
      ${captionLanguageNote(opts)}
      <label>Format <select class="opt-format">${chosen(FORMAT_OPTIONS, opts.format)}</select></label>
      <label>Speed <input class="opt-speed" type="number" min="0.5" max="2" step="0.05" value="${Number(opts.speed) || 1}"></label>
      <label class="toggle"><input class="opt-progress" type="checkbox"${checkedAttr(opts.progress)}> progress bar</label>
      <label class="toggle"><input class="opt-silence" type="checkbox"${checkedAttr(opts.silence)}> jump-cut silence</label>
      <label class="toggle"><input class="opt-loud" type="checkbox"${checkedAttr(opts.loud)}> loudness</label>
      <label>Caption brand <select class="opt-brand">${chosen(BRAND_OPTIONS, opts.captions_brand)}</select></label>
      <label class="toggle" title="Snap the cut points to the loudest beats measured offline">${ic("wave")}<input class="opt-beats" type="checkbox"${checkedAttr(opts.sync_beats)}> sync to beats</label>
      <label>Music bed <select class="opt-track">${trackOptions(opts.audio_track)}</select></label>
      <label>Bed mode <select class="opt-mix">${chosen(AUDIO_MIX_OPTIONS, opts.audio_mix)}</select></label>
      <label>Logo remover <select class="opt-logo">${chosen(LOGO_PRESETS, opts.logo_preset)}</select></label>
      ${opts.logo_preset === "custom" ? `<label>X <input class="opt-logo-x" type="number" min="0" max="0.98" step="0.01" value="${(opts.logoCustom || {}).x ?? 0.03}"></label>
      <label>Y <input class="opt-logo-y" type="number" min="0" max="0.98" step="0.01" value="${(opts.logoCustom || {}).y ?? 0.03}"></label>
      <label>Width <input class="opt-logo-w" type="number" min="0.01" max="0.98" step="0.01" value="${(opts.logoCustom || {}).w ?? 0.3}"></label>
      <label>Height <input class="opt-logo-h" type="number" min="0.01" max="0.98" step="0.01" value="${(opts.logoCustom || {}).h ?? 0.1}"></label>` : ""}
      <label>Box size <select class="opt-logo-size">${chosen([["S", "small"], ["M", "medium"], ["L", "large"]], opts.logo_size)}</select></label>
      <label class="full">Feather <span class="rangerow">
          <input class="opt-logo-feather" type="range" min="0" max="24" step="1" value="${Number(opts.logo_feather) || 0}">
          <output class="out-logo-feather">${Number(opts.logo_feather) || 0}px</output>
        </span></label>
      ${logoPreview(opts) + brandPreview(opts.captions_brand)}
      <label class="full">Silence tuner — cut quiet gaps
        <span class="rangerow">
          <input class="opt-silence-noise" type="range" min="-70" max="-18" step="1" value="${Number(opts.silence_noise)}">
          <output class="out-silence-noise">${Number(opts.silence_noise).toFixed(0)} dB</output>
        </span>
        <span class="rangerow">
          <input class="opt-silence-min" type="range" min="0.1" max="2" step="0.1" value="${Number(opts.silence_min)}">
          <output class="out-silence-min">${Number(opts.silence_min).toFixed(1)} s</output>
        </span>
      </label>
    </div>
  </details>`;
}

function renderEpisodes() {
  const allEpisodes = state.data?.episodes || [];
  const query = ($("#episodeSearch")?.value || "").trim().toLowerCase();
  const episodes = query
    ? allEpisodes.filter((episode) => `${episode.title} ${episode.id}`.toLowerCase().includes(query))
    : allEpisodes;
  $("#episodeCount").textContent = allEpisodes.length
    ? (query ? `${episodes.length}/${allEpisodes.length}` : `${allEpisodes.length} loaded`)
    : "";
  const list = $("#episodeList");

  if (!allEpisodes.length) {
    lastEpisodeSig = "";
    list.innerHTML = `<div class="empty" style="padding:34px 16px">
      <p>Load a YouTube playlist or single video above, or <a href="#" onclick="loadDemo();return false">try the demo</a>.</p>
    </div>`;
    return;
  }
  if (!episodes.length) {
    list.innerHTML = `<div class="empty" style="padding:34px 16px"><p>No matching episodes.</p></div>`;
    return;
  }

  const sig = episodeSig(allEpisodes) + "|q:" + query;
  const activeInside = document.activeElement && list.contains(document.activeElement);
  if (sig === lastEpisodeSig && (activeInside || document.querySelector("#episodeList .progressbar"))) {
    if (patchEpisodeProgress()) return;
  }
  lastEpisodeSig = sig;
  const prevIds = new Set(Array.from(list.querySelectorAll(".episode")).map((el) => el.dataset.id));

  list.innerHTML = episodes.map((ep) => {
    const status = ep.status || "new";
    const job = (state.data.jobs || []).find((item) => item.episode_id === ep.id);
    const busy = status === "processing";
    const pct = job ? Math.round(job.progress * 100) : 0;
    const opts = optsFor(ep.id);
    const disabled = busy ? "disabled" : "";
    return `
    <div class="episode${prevIds.has(ep.id) ? "" : " is-new"}" data-id="${ep.id}">
      <div class="title">${esc(ep.title)}</div>
      <div class="meta">
        <span class="badge ${ep.source === "demo" ? "demo" : "yt"}">${ep.source === "demo" ? "demo" : "youtube"}</span>
        <span class="badge status-${status}">${status}</span>
        <span>${ic("clock")} ${fmtDur(ep.duration)}</span>
        <span>${ic("film")} ${ep.clip_count || 0}</span>
      </div>
      ${busy ? `
        <div class="progressbar"><div class="fill" style="width:${pct}%"></div></div>
        <div class="stepmsg">${esc(job?.message || job?.step || "working…")}</div>` : ""}
      ${ep.error ? `<div class="stepmsg error">${ic("alert")} ${esc(ep.error)}</div>` : ""}
      ${isRateLimitError(ep.error) ? `
        <div class="stepmsg">
          <button class="btn small" onclick="openTools();switchToolTab('session')">${ic("key")} Fix this — add a YouTube session</button>
        </div>` : ""}
      <div class="controls">
        <select class="opt-count" ${disabled} aria-label="Number of shorts">
          ${[1, 2, 3, 5, 8, 12].map((n) => `<option value="${String(n)}"${String(n) === String(opts.count) ? " selected" : ""}>${n} shorts</option>`).join("")}
        </select>
        <select class="opt-profile" ${disabled} aria-label="Highlight profile">
          <option value="viral"${opts.profile === "viral" ? " selected" : ""}>Viral picks</option>
          <option value="story"${opts.profile === "story" ? " selected" : ""}>Story arc</option>
          <option value="facts"${opts.profile === "facts" ? " selected" : ""}>Numbers &amp; facts</option>
          <option value="energy"${opts.profile === "energy" ? " selected" : ""}>High energy</option>
        </select>
        <select class="opt-length" ${disabled} aria-label="Clip length">
          <option value="short"${lengthKey(opts.min_dur, opts.max_dur) === "short" ? " selected" : ""}>Short · 25–40s</option>
          <option value="medium"${lengthKey(opts.min_dur, opts.max_dur) === "medium" ? " selected" : ""}>Medium · 40–65s</option>
          <option value="long"${lengthKey(opts.min_dur, opts.max_dur) === "long" ? " selected" : ""}>Long · 60–90s</option>
          <option value="any"${lengthKey(opts.min_dur, opts.max_dur) === "any" ? " selected" : ""}>Any · 25–90s</option>
        </select>
        <select class="opt-style" ${disabled} aria-label="Framing style">
          ${chosen(STYLE_OPTIONS, opts.style)}
        </select>
        <select class="opt-quality" ${disabled} aria-label="Quality">
          ${chosen(QUALITY_OPTIONS, opts.quality)}
        </select>
        <select class="opt-format" ${disabled} aria-label="Format">
          ${chosen(FORMAT_OPTIONS, opts.format)}
        </select>
        <select class="opt-captions-main" ${disabled} aria-label="Caption style" onchange="syncCardSelect(this, '.opt-captions')">
          ${chosen(CAPTION_OPTIONS, opts.captions)}
        </select>
        <button class="btn primary small" onclick="generate('${ep.id}', this)" ${disabled}>
          ${busy ? "Working\u2026" : "Generate"}
        </button>
        <button class="btn small" onclick="previewPicks('${ep.id}', this)" ${disabled}>${ic("eye")} Preview</button>
      </div>
      ${opts.quality === "1440p" ? `<div class="hint">${ic("clock")} 1440p is a big render — expect it to be slow on a phone.</div>` : ""}
      ${renderAdvOpts(ep, opts)}
      ${renderMoments(ep.id)}
      ${renderManualBox(ep)}
      <div class="subrow">
        <button class="btn small" onclick="openCutter('${ep.id}')" ${disabled}>${ic("text")} Transcript</button>
        <button class="btn small" onclick="openChapters('${ep.id}')" ${disabled}>${ic("book")} Chapters</button>
        <button class="btn small" onclick="openBeats('${ep.id}')" ${disabled}>${ic("wave")} Beats</button>
        <button class="btn small" onclick="exportCsv('${ep.id}')" ${disabled}>${ic("download")} CSV</button>
        <button class="btn small" onclick="toggleManual('${ep.id}')" ${disabled}>${ic("scissors")} Exact range</button>
        <span class="spacer"></span>
        <a class="btn small" href="https://www.youtube.com/watch?v=${esc(ep.id)}" target="_blank" rel="noopener">${ic("external")} YouTube</a>
        <button class="btn small iconbtn" title="Delete episode and its clips" aria-label="Delete episode" onclick="deleteEpisode('${ep.id}', this)" ${disabled}>${ic("trash")}</button>
      </div>
    </div>`;
  }).join("");

  // keep the main caption select in sync with the fine-tune one
  document.querySelectorAll(".episode").forEach((card) => {
    const main = card.querySelector(".opt-captions-main");
    const fine = card.querySelector(".opt-captions");
    if (main && fine) main.value = fine.value;
  });
}

function syncCardSelect(source, targetSel) {
  const card = source?.closest(".episode");
  const target = card?.querySelector(targetSel);
  if (target) {
    target.value = source.value;
    rememberCardOpts(card);
  }
}

function renderShorts() {
  const allClips = state.data?.clips || [];
  const query = ($("#clipSearch")?.value || "").trim().toLowerCase();
  const sort = $("#clipSort")?.value || "score";
  const clips = allClips.filter((clip) => (
    !query || `${clip.title} ${clip.episode_title} ${(clip.reasons || []).join(" ")}`.toLowerCase().includes(query)
  ));
  clips.sort((a, b) => {
    if (sort === "newest") return Number(b.created || 0) - Number(a.created || 0);
    if (sort === "longest") return Number(b.duration || 0) - Number(a.duration || 0);
    return Number(b.score || 0) - Number(a.score || 0);
  });

  $("#clipCount").textContent = allClips.length
    ? (query ? `${clips.length}/${allClips.length}` : `${allClips.length} clips`)
    : "";
  $("#emptyShorts").style.display = allClips.length ? "none" : "";
  $("#zipBtn").disabled = !allClips.length;
  const maxScore = Math.max(...allClips.map((clip) => Number(clip.score || 0)), 1);
  const aiOn = !!state.health?.llm_available;

  $("#shortsGrid").innerHTML = clips.map((clip) => {
    const pack = clip.pack || null;
    const open = state.detailsOpen[`pack-${clip.id}`] ? " open" : "";
    // Synthetic demo footage is labelled where it matters: on the short, not
    // only on the episode card it came from (v0.6.6).
    const demo = clip.demo_media || clip.source === "demo";
    return `
    <div class="clip" data-id="${clip.id}">
      <video controls preload="metadata" playsinline
             ${clip.thumb ? `poster="/api/clips/${clip.id}/thumb"` : ""}
             src="/api/clips/${clip.id}/file"></video>
      ${demo ? `<div class="demoflag">${ic("alert")} demo media — test card, not a real video</div>` : ""}
      <div class="body">
        <div class="clip-title">${esc(clip.title)}</div>
        <div class="sub">From: <a href="https://www.youtube.com/watch?v=${esc(clip.episode_id)}&t=${Math.floor(clip.start || 0)}s" target="_blank" rel="noopener" title="${esc(clip.episode_title)}">${esc(trim(clip.episode_title, 42))}</a></div>
        <div class="sub timechip">${ic("clock")} ${fmtRange(clip.start, clip.end)} · ${Number(clip.duration).toFixed(1)}s · ${clip.width || 720}×${clip.height || 1280}${clip.format && clip.format !== "vertical" ? ` · ${esc(clip.format)}` : ""} · ${esc(clip.style || "blur")}${clip.captions_box ? " · box" : ""}${clip.captions_pos === "low" ? " · low" : ""}${clip.sync_beats ? " · beats" : ""}${clip.audio_track ? ` · ${esc(clip.audio_mix === "duck" ? "ducked track" : "new track")}` : ""}${clip.logo && clip.logo.enabled ? ` · ${esc(clip.logo.method)}` : ""}</div>
        ${waveformStrip(clip.waveform)}
        <div class="scorebox">
          <div class="scorebar"><div class="fill" style="width:${Math.round((Number(clip.score || 0) / maxScore) * 100)}%"></div></div>
          <div class="scorenum">${Number(clip.score || 0).toFixed(1)}</div>
        </div>
        ${signalBars(clip.breakdown)}
        ${clip.engine_notice ? `<div class="noticeline">${ic("alert")} ${esc(clip.engine_notice)}</div>` : ""}
        ${demo ? `<div class="noticeline demo">${ic("alert")} This short was cut from Qyro's offline <b>demo</b> footage (a colour-bar test card with a tone). It is how the pipeline looks end to end — to make real shorts, paste a YouTube link above and press Generate.</div>` : ""}
        <div class="reasons">${(clip.reasons || []).map((reason) => `<span class="reason">${esc(reason)}</span>`).join("")}</div>
        ${pack ? `<details class="pack"${open}>
          <summary>${ic("text")} Upload pack${pack.polished_by ? ` · ${ic("sparkle")} ${esc(pack.polished_by)}` : ""}</summary>
          <div class="packbody">
            ${(pack.titles || []).map((title) => `<div class="packtitle">${esc(title)}</div>`).join("")}
            <div class="tags">${(pack.hashtags || []).map((tag) => `<span class="tag">${esc(tag)}</span>`).join("")}</div>
            <pre class="packdesc">${esc(pack.description || "")}</pre>
          </div>
        </details>` : ""}
        <div class="actions">
          <a class="btn small" href="/api/clips/${clip.id}/file?dl=1" download="qyro-${clip.id}.mp4">${ic("play")} Video</a>
          <a class="btn small" href="/api/clips/${clip.id}/srt">${ic("captions")} SRT</a>
          <a class="btn small" href="/api/clips/${clip.id}/audio.mp3">${ic("music")} MP3</a>
          <button class="btn small" onclick="openThumbPicker('${clip.id}')">${ic("image")} Thumb</button>
          <button class="btn small" onclick="openTitleLab('${clip.id}')">${ic("wand")} Titles</button>
          <button class="btn small" onclick="openInspector('${clip.id}')">${ic("crop")} Inspect</button>
          <button class="btn small" onclick="shareClip('${clip.id}')">${ic("share")} Share</button>
          <button class="btn small iconbtn" aria-label="Rename clip" title="Rename clip" onclick="renameClip('${clip.id}')">${ic("pencil")}</button>
          <button class="btn small iconbtn" aria-label="Delete clip" title="Delete clip" onclick="deleteClip('${clip.id}')">${ic("trash")}</button>
        </div>
        <div class="actions">
          <button class="btn small" onclick="copyPack('${clip.id}')" ${pack ? "" : "disabled"}>${ic("copy")} Copy pack</button>
          <button class="btn small" onclick="rerenderClip('${clip.id}')">${ic("retry")} Re-render</button>
          ${aiOn ? `<button class="btn small" onclick="polishClip('${clip.id}')">${ic("sparkle")} Polish</button>` : ""}
        </div>
      </div>
    </div>`;
  }).join("");
}

// ---------------------------------------------------------------- actions
async function refresh() {
  try {
    const [st, health] = await Promise.all([api("/api/state"), api("/api/health")]);
    state.data = st;
    state.health = health;
    baseDefaults = { ...baseDefaults, ...(st.defaults || {}) };
    renderHealth(health);
    renderStats();
    renderEpisodes();
    renderShorts();
    const autopilot = $("#autopilot");
    if (autopilot && document.activeElement !== autopilot) {
      autopilot.checked = !!(st.settings || {}).autopilot;
    }
    renderPresets();
    renderEngineHint();
  } catch (error) {
    console.error(error);
  }
}

async function loadPlaylist() {
  const url = $("#playlistUrl").value.trim();
  if (!url) return toast("Paste a YouTube playlist or video URL first", true);
  const btn = $("#loadPlaylist");
  btn.disabled = true;
  btn.textContent = "Loading…";
  try {
    const result = await api("/api/playlist", {
      method: "POST",
      body: JSON.stringify({ url, limit: 25 }),
    });
    const kind = result.kind === "video" ? "video" : "playlist";
    toast(result.auto_queued
      ? `Added ${result.added} ${kind === "video" ? "video" : "episodes"} · auto-pilot queued ${result.auto_queued}`
      : `Added ${result.added} ${kind === "video" ? "video" : "episodes"} from ${kind}`);
    if (result.auto_queued) fastPoll();
  } catch (error) {
    toast(error.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = "Load";
    refresh();
  }
}

async function loadDemo() {
  try {
    const result = await api("/api/demo/load", { method: "POST" });
    // Say up front what the footage is: demo shorts are cut from a synthetic
    // test card, and nobody should meet that for the first time in a
    // downloaded file.
    toast(`Demo loaded (${result.added} episodes) — the pipeline runs on a labelled test card, not real video. Paste a YouTube link for real shorts.`, true);
  } catch (error) {
    toast(error.message, true);
  }
  refresh();
}

function renderOptsPayload(opts) {
  const payload = {
    count: opts.count,
    min_dur: opts.min_dur,
    max_dur: opts.max_dur,
    profile: opts.profile,
    style: opts.style,
    quality: opts.quality,
    format: opts.format,
    captions: opts.captions,
    captions_pos: opts.captions_pos || "standard",
    captions_box: !!opts.captions_box,
    captions_enabled: opts.captions_enabled !== false,
    speed: Number(opts.speed) || 1,
    progress: !!opts.progress,
    silence: !!opts.silence,
    loud: !!opts.loud,
    captions_brand: opts.captions_brand || "none",
    captions_font: opts.captions_font || "auto",
    captions_anim: opts.captions_anim || "fade",
    transition: opts.transition || "fade",
    track_mode: opts.track_mode || "auto",
    track_zoom: opts.track_zoom || "auto",
    language: opts.language || "auto",
    quality_gate: opts.quality_gate !== false,
    sync_beats: !!opts.sync_beats,
    audio_mix: opts.audio_mix || "duck",
    logo_box: logoBoxFrom(opts),
    silence_noise: Number(opts.silence_noise),
    silence_min: Number(opts.silence_min),
  };
  if (opts.audio_track) payload.audio_track = opts.audio_track;
  return payload;
}

async function generate(epId, btn) {
  const card = btn.closest(".episode");
  rememberCardOpts(card);
  const opts = readCardOpts(card);
  try {
    await api(`/api/episodes/${epId}/shorts`, {
      method: "POST",
      body: JSON.stringify(renderOptsPayload(opts)),
    });
    toast("Queued — jobs render one at a time");
    fastPoll();
  } catch (error) {
    toast(error.message, true);
  }
  refresh();
}

async function previewPicks(epId, btn) {
  const card = btn.closest(".episode");
  const opts = readCardOpts(card);
  btn.disabled = true;
  btn.textContent = "Finding…";
  try {
    const result = await api(`/api/episodes/${epId}/preview`, {
      method: "POST",
      body: JSON.stringify(renderOptsPayload(opts)),
    });
    state.previews[epId] = { moments: result.moments || [], stats: result.stats || null };
    renderEpisodes();
  } catch (error) {
    toast(error.message, true);
    btn.disabled = false;
    btn.innerHTML = `${ic("eye")} Preview`;
  }
}

function toggleManual(epId) {
  state.manualOpen[epId] = !state.manualOpen[epId];
  renderEpisodes();
}

function rememberManual(card) {
  if (!card?.dataset.id) return;
  state.manualVals[card.dataset.id] = {
    start: card.querySelector(".manual-start")?.value ?? "",
    end: card.querySelector(".manual-end")?.value ?? "",
    title: card.querySelector(".manual-title")?.value ?? "",
  };
}

async function cutManual(epId, btn) {
  const card = btn.closest(".episode");
  rememberManual(card);
  const start = +card.querySelector(".manual-start").value;
  const end = +card.querySelector(".manual-end").value;
  if (!Number.isFinite(start) || !Number.isFinite(end) || end - start < 5) {
    return toast("Manual clips must be at least 5 seconds", true);
  }
  const opts = readCardOpts(card);
  btn.disabled = true;
  try {
    await api(`/api/episodes/${epId}/manual`, {
      method: "POST",
      body: JSON.stringify({
        start,
        end,
        title: card.querySelector(".manual-title").value.trim(),
        ...renderOptsPayload(opts),
      }),
    });
    toast("Manual clip queued");
    state.manualOpen[epId] = false;
    fastPoll();
    refresh();
  } catch (error) {
    toast(error.message, true);
    btn.disabled = false;
  }
}

async function deleteClip(id) {
  try {
    await api(`/api/clips/${id}`, { method: "DELETE" });
    toast("Clip deleted");
  } catch (error) {
    toast(error.message, true);
  }
  refresh();
}

async function deleteEpisode(id, btn) {
  const ep = (state.data?.episodes || []).find((item) => item.id === id);
  if (!confirm(`Delete “${trim(ep?.title || id, 60)}” and every clip made from it? This cannot be undone.`)) return;
  if (btn) btn.disabled = true;
  try {
    const result = await api(`/api/episodes/${id}`, { method: "DELETE" });
    toast(`Episode deleted · ${result.clips_removed} clip(s) removed`);
    delete cardOpts[id];
    saveCardOpts();
  } catch (error) {
    toast(error.message, true);
    if (btn) btn.disabled = false;
  }
  refresh();
}

function exportCsv(epId) {
  const card = document.querySelector(`.episode[data-id="${epId}"]`);
  const opts = readCardOpts(card);
  const url = `/api/episodes/${epId}/export?count=${encodeURIComponent(opts.count)}&profile=${encodeURIComponent(opts.profile)}`;
  const link = document.createElement("a");
  link.href = url;
  link.download = `qyro-${epId}-moments.csv`;
  document.body.appendChild(link);
  link.click();
  link.remove();
}

async function copyText(text) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const input = document.createElement("textarea");
  input.value = text;
  input.style.position = "fixed";
  input.style.opacity = "0";
  document.body.appendChild(input);
  input.select();
  document.execCommand("copy");
  input.remove();
}

async function shareClip(id) {
  const clip = (state.data?.clips || []).find((item) => item.id === id);
  const title = clip?.title || "Qyro clip";
  const url = `${location.origin}/api/clips/${id}/file`;
  try {
    if (navigator.share && navigator.canShare) {
      const response = await fetch(`/api/clips/${id}/file`);
      if (!response.ok) throw new Error("Could not load clip");
      const blob = await response.blob();
      const file = new File([blob], `qyro-${id}.mp4`, { type: "video/mp4" });
      if (navigator.canShare({ files: [file] })) {
        await navigator.share({ title, text: title, files: [file] });
        return;
      }
    }
    await copyText(url);
    toast("Clip link copied");
  } catch (error) {
    if (error.name !== "AbortError") toast(error.message, true);
  }
}

async function renameClip(id) {
  const clip = (state.data?.clips || []).find((item) => item.id === id);
  if (!clip) return;
  const title = prompt("New clip title (max 120 chars)", clip.title || "");
  if (title === null) return;
  try {
    const result = await api(`/api/clips/${id}/rename`, {
      method: "POST",
      body: JSON.stringify({ title }),
    });
    toast(`Renamed to “${trim(result.title, 40)}”`);
  } catch (error) {
    toast(error.message, true);
  }
  refresh();
}

async function copyPack(id) {
  const clip = (state.data?.clips || []).find((item) => item.id === id);
  const pack = clip?.pack;
  if (!pack) return toast("No upload pack for this clip yet", true);
  const titles = (pack.titles || []).map((title, index) => `${index + 1}. ${title}`).join("\n");
  const text = [titles, "", (pack.hashtags || []).join(" "), "", pack.description || ""]
    .join("\n").trim();
  try {
    await copyText(text);
    toast(pack.polished_by ? `Pack copied (polished by ${pack.polished_by})` : "Upload pack copied");
  } catch (error) {
    toast(error.message, true);
  }
}

async function polishClip(id) {
  toast("Asking the engine for a rewrite\u2026");
  try {
    const result = await api(`/api/clips/${id}/polish`, { method: "POST" });
    if (result.notice) {
      toast(`${result.notice} — offline pack kept`, true);
    } else {
      toast(`Pack rewritten by ${result.engine || result.pack?.polished_by || "the engine"}`);
    }
  } catch (error) {
    toast(error.message, true);
  }
  refresh();
}

function rerenderClip(id) {
  const clip = (state.data?.clips || []).find((item) => item.id === id);
  if (!clip) return;
  const opts = { ...baseDefaults, ...presetOverlay, ...(clip.render || {}) };
  openModal("Re-render clip", `
    <div class="stepmsg">${esc(clip.title)} · ${fmtRange(clip.start, clip.end)}<br>
    Unchanged options keep the clip's stored settings (position, box, …).</div>
    <div class="advgrid">
      <label>Style <select id="rr-style">${chosen(STYLE_OPTIONS, opts.style)}</select></label>
      <label>Quality <select id="rr-quality">${chosen(QUALITY_OPTIONS, opts.quality)}</select></label>
      <label>Format <select id="rr-format">${chosen(FORMAT_OPTIONS, opts.format)}</select></label>
      <label class="toggle"><input id="rr-cap-enabled" type="checkbox"${checkedAttr(opts.captions_enabled !== false)}> captions on</label>
      <label>Captions <select id="rr-captions">${chosen(CAPTION_OPTIONS, opts.captions)}</select></label>
      <label>Cap position <select id="rr-cappos">${chosen(CAPTION_POS_OPTIONS, opts.captions_pos || "standard")}</select></label>
      <label class="toggle"><input id="rr-capbox" type="checkbox"${checkedAttr(opts.captions_box)}> caption box</label>
      <label>Speed <input id="rr-speed" type="number" min="0.5" max="2" step="0.05" value="${Number(opts.speed) || 1}"></label>
      <label class="toggle"><input id="rr-progress" type="checkbox"${checkedAttr(opts.progress)}> progress bar</label>
      <label class="toggle"><input id="rr-silence" type="checkbox"${checkedAttr(opts.silence)}> jump-cut silence</label>
      <label class="toggle"><input id="rr-loud" type="checkbox"${checkedAttr(opts.loud)}> loudness</label>
      <label>Title <input id="rr-title" type="text" maxlength="120" value="${esc(clip.title || "")}"></label>
      <label>Brand <select id="rr-brand">${chosen(BRAND_OPTIONS, opts.captions_brand || "none")}</select></label>
      <label>Music bed <select id="rr-track">${trackOptions(opts.audio_track)}</select></label>
      <label>Track mode <select id="rr-mix">${chosen(AUDIO_MIX_OPTIONS, opts.audio_mix || "duck")}</select></label>
      <label class="toggle"><input id="rr-beats" type="checkbox"${checkedAttr(opts.sync_beats)}> sync cuts to beats</label>
      <label>Follow person <select id="rr-track-mode">${chosen(catalog("track_modes"), opts.track_mode || baseDefaults.track_mode)}</select></label>
      <label>Framing <select id="rr-track-zoom">${chosen(catalog("track_zooms"), opts.track_zoom || baseDefaults.track_zoom)}</select></label>
      <label>Caption font <select id="rr-cap-font">${chosen(catalog("fonts"), opts.captions_font || baseDefaults.captions_font)}</select></label>
      <label>Caption motion <select id="rr-cap-anim">${chosen(catalog("anims"), opts.captions_anim || baseDefaults.captions_anim)}</select></label>
      <label>Clip transition <select id="rr-transition">${chosen(catalog("transitions"), opts.transition || baseDefaults.transition)}</select></label>
      <label>Language <select id="rr-language">${chosen(catalog("languages"), opts.language || baseDefaults.language)}</select></label>
      <label class="toggle"><input id="rr-gate" type="checkbox"${opts.quality_gate === false ? "" : " checked"}> best parts only</label>
    </div>
    <div class="cutbar"><button class="btn primary small" id="rr-go">${ic("retry")} Re-render</button></div>`);
  $("#rr-go")?.addEventListener("click", async (event) => {
    const btn = event.target;
    btn.disabled = true;
    try {
      await api(`/api/clips/${id}/rerender`, {
        method: "POST",
        body: JSON.stringify({
          style: $("#rr-style").value,
          quality: $("#rr-quality").value,
          format: $("#rr-format").value,
          captions_enabled: $("#rr-cap-enabled").checked,
          captions: $("#rr-captions").value,
          captions_pos: $("#rr-cappos").value,
          captions_box: $("#rr-capbox").checked,
          speed: +$("#rr-speed").value,
          progress: $("#rr-progress").checked,
          silence: $("#rr-silence").checked,
          loud: $("#rr-loud").checked,
          title: $("#rr-title").value,
          captions_brand: $("#rr-brand").value,
          captions_font: $("#rr-cap-font").value,
          captions_anim: $("#rr-cap-anim").value,
          transition: $("#rr-transition").value,
          track_mode: $("#rr-track-mode").value,
          track_zoom: $("#rr-track-zoom").value,
          language: $("#rr-language").value,
          quality_gate: $("#rr-gate").checked,
        }),
      });
      toast("Re-render queued");
      closeModal();
      fastPoll();
    } catch (error) {
      toast(error.message, true);
      btn.disabled = false;
    }
    refresh();
  });
}

async function downloadZip() {
  const btn = $("#zipBtn");
  btn.disabled = true;
  try {
    const response = await fetch("/api/clips/zip");
    if (!response.ok) {
      let detail = "No clips available";
      try { detail = (await response.json()).detail || detail; } catch (e) {}
      throw new Error(detail);
    }
    const blob = await response.blob();
    const disposition = response.headers.get("content-disposition") || "";
    const match = disposition.match(/filename\*?=(?:UTF-8''|\")?([^\";]+)/i);
    const filename = match ? decodeURIComponent(match[1].replace(/"/g, "")) : "qyro.zip";
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(link.href);
  } catch (error) {
    toast(error.message, true);
  } finally {
    btn.disabled = !(state.data?.clips || []).length;
  }
}

// ---------------------------------------------------------------- batches
function loadPresets() {
  try { return JSON.parse(localStorage.getItem(PRESET_KEY)) || {}; } catch (e) { return {}; }
}

function savePresets(presets) {
  try { localStorage.setItem(PRESET_KEY, JSON.stringify(presets)); } catch (e) {}
  renderPresets();
}

function renderPresets() {
  const sel = $("#presetSel");
  if (!sel) return;
  const current = sel.value;
  const presets = loadPresets();
  sel.innerHTML = `<option value="">— presets —</option>`
    + Object.keys(presets).sort().map((name) => `<option value="${esc(name)}">${esc(name)}</option>`).join("");
  if (presets[current]) sel.value = current;
}

function savePreset() {
  const firstCard = document.querySelector(".episode");
  const opts = firstCard ? readCardOpts(firstCard) : { ...baseDefaults, ...presetOverlay };
  const name = (prompt("Preset name", `Preset ${Object.keys(loadPresets()).length + 1}`) || "").trim();
  if (!name) return;
  const presets = loadPresets();
  presets[name] = opts;
  savePresets(presets);
  $("#presetSel").value = name;
  toast(`Preset “${name}” saved`);
}

function applyPreset(name) {
  const preset = loadPresets()[name];
  if (!preset) return;
  presetOverlay = { ...preset };
  cardOpts = {};
  saveCardOpts();
  renderEpisodes();
  toast(`Preset “${name}” applied`);
}

function deletePreset() {
  const name = $("#presetSel").value;
  if (!name) return toast("Pick a preset to delete", true);
  if (!confirm(`Delete preset “${name}”?`)) return;
  const presets = loadPresets();
  delete presets[name];
  savePresets(presets);
  $("#presetSel").value = "";
  toast(`Preset “${name}” deleted`);
}

async function processAll() {
  const name = $("#presetSel").value;
  const opts = { ...baseDefaults, ...presetOverlay, ...(loadPresets()[name] || {}) };
  const btn = $("#processAll");
  btn.disabled = true;
  btn.textContent = "Queueing…";
  try {
    const result = await api("/api/batch", {
      method: "POST",
      body: JSON.stringify(renderOptsPayload(opts)),
    });
    const skipped = result.skipped ? ` · skipped ${result.skipped}` : "";
    toast(`Queued ${result.queued} episode(s)${skipped}`);
    if (result.queued) fastPoll();
  } catch (error) {
    toast(error.message, true);
  } finally {
    btn.disabled = false;
    btn.innerHTML = `${ic("wand")} Process all`;
  }
  refresh();
}

async function setAutopilot(enabled) {
  try {
    await api("/api/settings", {
      method: "POST",
      body: JSON.stringify({ autopilot: !!enabled }),
    });
    toast(enabled ? "Auto-pilot on — new episodes queue themselves" : "Auto-pilot off");
  } catch (error) {
    toast(error.message, true);
  }
  refresh();
}

// ---------------------------------------------------------------- modals
function openModal(title, body, wide = false) {
  const root = $("#modalRoot");
  root.hidden = false;
  root.innerHTML = `
    <div class="modal-backdrop" data-close="1"></div>
    <div class="modal${wide ? " wide" : ""}" role="dialog" aria-modal="true" aria-label="${esc(title)}">
      <div class="modal-head">
        <h3>${esc(title)}</h3>
        <button class="btn small iconbtn" data-close="1" aria-label="Close">${ic("x")}</button>
      </div>
      <div class="modal-body">${body}</div>
    </div>`;
  root.querySelectorAll("[data-close]").forEach((el) => el.addEventListener("click", closeModal));
}

function closeModal() {
  const root = $("#modalRoot");
  if (!root || root.hidden) return;
  root.hidden = true;
  root.innerHTML = "";
}

function modalBody() {
  return $("#modalRoot .modal-body");
}

// ---------------------------------------------------------------- search
function openSearch() {
  openModal("Search transcripts", `
    <div class="stepmsg">Offline search over demo + downloaded transcripts — nothing hits the network.</div>
    <div class="searchbar">
      <input id="searchQ" type="search" placeholder="e.g. fear, 40 percent, CBI…" autocomplete="off">
      <button class="btn primary small" id="searchGo">Search</button>
    </div>
    <div id="searchResults" class="tlines"><div class="stepmsg">Type at least 2 characters.</div></div>`, true);
  const input = $("#searchQ");
  $("#searchGo").addEventListener("click", runSearch);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") runSearch();
  });
  input.focus();
}

async function runSearch() {
  const q = ($("#searchQ")?.value || "").trim();
  const box = $("#searchResults");
  if (!box) return;
  if (q.length < 2) {
    box.innerHTML = `<div class="stepmsg error">Query must be at least 2 characters.</div>`;
    return;
  }
  box.innerHTML = `<div class="stepmsg">Searching…</div>`;
  try {
    const data = await api(`/api/search?q=${encodeURIComponent(q)}`);
    const rows = data.results || [];
    box.innerHTML = rows.length
      ? rows.map((row) => `
        <div class="tline clickable" onclick="jumpToHit('${row.episode_id}', ${row.start})">
          <span class="tstamp">${fmtDur(row.start)}</span>
          <span class="ttext"><b>${esc(row.episode_title)}</b><br>${esc(row.text)}</span>
        </div>`).join("")
      : `<div class="stepmsg">No hits. Transcripts are cached only after an episode is processed (demo episodes always search).</div>`;
  } catch (error) {
    box.innerHTML = `<div class="stepmsg error">${ic("alert")} ${esc(error.message)}</div>`;
  }
}

function jumpToHit(epId, start) {
  closeModal();
  state.cutter = { epId, segments: [], start: null, end: null };
  openCutter(epId, Math.max(0, start - 10));
}

// ---------------------------------------------------------------- chapters
async function openChapters(epId) {
  openModal("Chapters", `<div class="stepmsg"><span class="spinner"></span>Scoring story moments…</div>`, true);
  try {
    const data = await api(`/api/episodes/${epId}/chapters`);
    modalBody().innerHTML = `
      <div class="stepmsg">${esc(trim(data.episode_title || "", 90))}</div>
      <div class="tlines">${(data.chapters || []).map((chapter) => `
        <div class="tline"><span class="tstamp">${fmtDur(chapter.start ?? chapter.time)}</span>
        <span class="ttext">${esc(chapter.title)}</span></div>`).join("")}</div>
      <pre class="chapterpre">${esc(data.text || "")}</pre>
      <div class="cutbar">
        <span class="count-pill">${(data.chapters || []).length} chapters</span>
        <button class="btn primary small" id="chapterCopy">${ic("copy")} Copy chapters</button>
      </div>`;
    $("#chapterCopy").addEventListener("click", async () => {
      await copyText(data.text || "");
      toast("Chapters copied");
    });
  } catch (error) {
    modalBody().innerHTML = `<div class="stepmsg error">${ic("alert")} ${esc(error.message)}</div>`;
  }
}

// ---------------------------------------------------------------- cutter
async function openCutter(epId, scrollStart = null) {
  state.cutter = { epId, segments: [], start: null, end: null };
  openModal("Transcript cutter", `<div class="stepmsg"><span class="spinner"></span>Loading transcript…</div>`, true);
  try {
    const data = await api(`/api/episodes/${epId}/transcript`);
    state.cutter.segments = data.segments || [];
    renderCutter(scrollStart);
  } catch (error) {
    modalBody().innerHTML = `<div class="stepmsg error">${ic("alert")} ${esc(error.message)}</div>`;
  }
}

function cutterLineClass(segment) {
  const cutter = state.cutter;
  if (!cutter) return "";
  if (cutter.start !== null && segment.start === cutter.start) return " pick";
  if (cutter.end !== null && segment.end === cutter.end) return " pick";
  if (cutter.start !== null && cutter.end !== null
      && segment.start >= cutter.start && segment.end <= cutter.end) return " in";
  return "";
}

function renderCutter(scrollStart = null) {
  const cutter = state.cutter;
  if (!cutter || !modalBody()) return;
  modalBody().innerHTML = `
    <div class="stepmsg">Tap a line for <b>START</b>, tap another for <b>END</b> — then cut it.</div>
    <div class="tlines" id="tlines">
      ${cutter.segments.map((segment, index) => `
        <div class="tline${cutterLineClass(segment)}" onclick="tapLine(${index})">
          <span class="tstamp">${fmtDur(segment.start)}</span>
          <span class="ttext">${esc(segment.text)}</span>
        </div>`).join("")}
    </div>
    <div class="cutbar">
      <span id="cutRange" class="count-pill"></span>
      <button class="btn primary small" onclick="cutFromCutter()">${ic("scissors")} Cut range</button>
    </div>`;
  updateCutLabel();
  if (scrollStart !== null) {
    const lines = document.querySelectorAll("#tlines .tline");
    const idx = cutter.segments.findIndex((segment) => segment.start >= scrollStart);
    if (idx >= 0 && lines[idx]) lines[idx].scrollIntoView({ block: "center" });
  }
}

function updateCutLabel() {
  const label = $("#cutRange");
  const cutter = state.cutter;
  if (!label || !cutter) return;
  label.textContent = (cutter.start === null)
    ? "no range picked"
    : (cutter.end === null
      ? `start ${fmtDur(cutter.start)} — pick the end`
      : `${fmtDur(cutter.start)} → ${fmtDur(cutter.end)} (${Math.round(cutter.end - cutter.start)}s)`);
}

function tapLine(index) {
  const cutter = state.cutter;
  if (!cutter || !cutter.segments[index]) return;
  const segment = cutter.segments[index];
  if (cutter.start === null || cutter.end !== null) {
    cutter.start = segment.start;
    cutter.end = null;
  } else if (segment.end <= cutter.start) {
    cutter.end = cutter.start;
    cutter.start = segment.start;
  } else {
    cutter.end = segment.end;
  }
  renderCutter();
}

async function cutFromCutter() {
  const cutter = state.cutter;
  if (!cutter) return;
  if (cutter.start === null || cutter.end === null || cutter.end - cutter.start < 5) {
    return toast("Pick a range of at least 5 seconds", true);
  }
  const card = document.querySelector(`.episode[data-id="${cutter.epId}"]`);
  const opts = readCardOpts(card);
  const title = (card?.querySelector(".manual-title")?.value || "").trim();
  const button = modalBody()?.querySelector(".btn.primary");
  if (button) button.disabled = true;
  try {
    await api(`/api/episodes/${cutter.epId}/manual`, {
      method: "POST",
      body: JSON.stringify({
        start: cutter.start,
        end: cutter.end,
        title,
        ...renderOptsPayload(opts),
      }),
    });
    toast("Manual clip queued");
    closeModal();
    fastPoll();
  } catch (error) {
    toast(error.message, true);
    if (button) button.disabled = false;
  }
  refresh();
}

// ---------------------------------------------------------------- dashboard
async function openDashboard() {
  openModal("Dashboard", `<div class="stepmsg"><span class="spinner"></span>Loading storage &amp; jobs…</div>`, true);
  try {
    const [storage, jobs, health] = await Promise.all([
      api("/api/storage"), api("/api/jobs?limit=200"), api("/api/health"),
    ]);
    state.storage = storage;
    state.jobs = jobs.jobs || [];
    state.health = health;
    modalBody().innerHTML = dashboardHtml();
  } catch (error) {
    modalBody().innerHTML = `<div class="stepmsg error">${ic("alert")} ${esc(error.message)}</div>`;
  }
}

function dashboardHtml() {
  const storage = state.storage || { dirs: {}, disk: {}, counts: {} };
  const health = state.health || {};
  const versions = health.versions || {};
  const dirs = storage.dirs || {};
  const jobs = state.jobs || [];
  const done = jobs.filter((job) => job.status === "done").length;
  const failed = jobs.filter((job) => job.status === "error").length;
  const active = jobs.filter((job) => job.status === "queued" || job.status === "running").length;
  const cancelled = jobs.filter((job) => job.status === "cancelled").length;
  const cleanTargets = ["subs", "thumbs", "clips"];

  return `
    <div class="jobstatline">
      <span class="chip ok">${ic("check")} ${done} done</span>
      <span class="chip ${failed ? "bad" : ""}">${ic("x")} ${failed} failed</span>
      <span class="chip">${ic("clock")} ${active} active</span>
      ${cancelled ? `<span class="chip">${ic("alert")} ${cancelled} cancelled</span>` : ""}
    </div>

    <div class="dashgrid">
      <div class="kv"><span>Version</span><b>${esc(health.version || "?")}</b></div>
      <div class="kv"><span>Python</span><b>${esc(versions.python || "?")}</b></div>
      <div class="kv"><span>ffmpeg</span><b title="${esc(versions.ffmpeg || "")}">${esc(trim(versions.ffmpeg || "?", 34))}</b></div>
      <div class="kv"><span>yt-dlp</span><b>${esc(versions.yt_dlp || "?")}</b></div>
      <div class="kv"><span>Disk free</span><b>${fmtBytes(storage.disk?.free)} / ${fmtBytes(storage.disk?.total)}</b></div>
      <div class="kv"><span>AI polish</span><b>${health.llm_available ? "on" : "off"}</b></div>
    </div>

    <div class="minihead">Storage — ${fmtBytes(storage.total_bytes)} in ${storage.total_files || 0} files</div>
    <div class="storagerows">
      ${["media", "clips", "thumbs", "subs"].map((name) => `
        <div class="storagerow">
          <span class="sname">${ic("folder")} ${name}</span>
          <span class="smeta">${dirs[name]?.files ?? 0} files · ${fmtBytes(dirs[name]?.bytes)}</span>
          ${cleanTargets.includes(name)
            ? `<button class="btn small danger" onclick="cleanStorage('${name}', this)">${ic("trash")} Clean</button>`
            : `<span class="count-pill">kept for resumes</span>`}
        </div>`).join("")}
    </div>
    <div class="cutbar">
      <span class="count-pill">clips: ${storage.counts?.clips ?? 0} · episodes: ${storage.counts?.episodes ?? 0}</span>
    </div>

    <div class="minihead">Backup &amp; restore</div>
    <div class="cutbar">
      <a class="btn small" href="/api/backup" download>${ic("download")} Download backup</a>
      <input type="file" id="restoreFile" accept="application/json,.json" onchange="restoreFromFile(this)">
      <span class="count-pill">restoring replaces everything</span>
    </div>

    <div class="minihead">Job history</div>
    <div class="jobs">
      ${jobs.length ? jobs.map((job) => `
        <div class="job">
          <span class="badge status-${esc(job.status)}">${esc(job.status)}</span>
          <span class="jstep">${esc(job.step || "")}</span>
          <span class="jmsg" title="${esc(job.message || job.error || "")}">${esc(trim(job.message || job.error || "", 54))}</span>
          <span class="jmode">${esc((job.params || {}).kind === "manual" ? "manual" : `${(job.params || {}).count || ""} auto`)}</span>
          ${job.status === "queued"
            ? `<button class="btn small iconbtn" title="Cancel before it starts" aria-label="Cancel job" onclick="cancelJob('${job.id}')">${ic("x")}</button>`
            : (job.status === "done" || job.status === "error")
              ? `<button class="btn small" title="Retry this job" onclick="retryJob('${job.id}')">${ic("retry")} Retry</button>`
              : ""}
        </div>`).join("") : `<div class="stepmsg">No jobs yet.</div>`}
    </div>`;
}

async function cleanStorage(target, button) {
  if (!confirm(`Delete every ${target} file? This cannot be undone.`)) return;
  if (button) button.disabled = true;
  try {
    const result = await api("/api/storage/clean", {
      method: "POST",
      body: JSON.stringify({ target }),
    });
    toast(`Removed ${result.removed ?? result.removed_files ?? 0} file(s) · freed ${fmtBytes(result.freed_bytes)}`);
  } catch (error) {
    toast(error.message, true);
  }
  openDashboard();
  refresh();
}

async function restoreFromFile(input) {
  const file = input?.files?.[0];
  if (!file) return;
  let data;
  try {
    data = JSON.parse(await file.text());
  } catch (error) {
    return toast("That file is not valid JSON", true);
  }
  if (!confirm("Replace ALL episodes, clips and jobs with this backup?")) {
    input.value = "";
    return;
  }
  try {
    const result = await api("/api/restore", { method: "POST", body: JSON.stringify(data) });
    toast(`Restored ${result.episodes} episodes · ${result.clips} clips`);
    closeModal();
  } catch (error) {
    toast(error.message, true);
  }
  refresh();
}

async function retryJob(jobId) {
  try {
    const result = await api(`/api/jobs/${jobId}/retry`, { method: "POST" });
    toast(`Job re-queued (${result.job_id})`);
    fastPoll();
  } catch (error) {
    toast(error.message, true);
  }
  openDashboard();
  refresh();
}

async function cancelJob(jobId) {
  try {
    await api(`/api/jobs/${jobId}/cancel`, { method: "POST" });
    toast("Job cancelled before it started");
    fastPoll();
  } catch (error) {
    toast(error.message, true);
  }
  openDashboard();
  refresh();
}


// ------------------------------------------------- v0.5.0 option helpers
const BRAND_SWATCH = {
  "qyro-pop": { fg: "#FFFFFF", edge: "#7C3AED", hi: "#22D3EE", label: "Qyro Pop" },
  "qyro-minimal": { fg: "#F0F4FF", edge: "transparent", hi: "#7C3AED", label: "Qyro Minimal" },
  "qyro-neon": { fg: "#22D3EE", edge: "#7C3AED", hi: "#FFFFFF", label: "Qyro Neon" },
};
const QUALITY_NOTE = {
  fast: "720p — quickest render, fine for talking heads.",
  full: "1080p — the everyday default.",
  "1440p": "1440p — crisp, but slow on a phone and a bigger file.",
};

/* Uploaded music beds come from /api/state; the select always offers "none" so
   a track can be dropped without opening the tools modal. */
function trackOptions(current) {
  const tracks = (state.data && state.data.audio_tracks) || [];
  return `<option value=""${current ? "" : " selected"}>original audio</option>`
    + tracks.map((t) => `<option value="${esc(t.id)}"${String(current) === String(t.id) ? " selected" : ""}>${esc(t.name)} · ${fmtDur(t.duration)}</option>`).join("");
}

/* Live mini-preview of the caption look, drawn with the same colours the ASS
   writer uses. Cheap, and it makes the brand choice obvious on a phone. */
function brandPreview(brand) {
  const swatch = BRAND_SWATCH[brand];
  if (!swatch) return "";
  return `<div class="full">
    <div class="brandpreview" style="--fg:${swatch.fg};--edge:${swatch.edge};--hi:${swatch.hi}">
      <span>${esc(swatch.label)}</span>
    </div>
  </div>`;
}

/* The logo box is defined as fractions of the frame, so a 9:16 placeholder is
   an honest preview of what delogo will be handed. */
function logoPreview(opts) {
  if (!opts.logo_preset || opts.logo_preset === "") return "";
  const frac = { S: 0.11, M: 0.16, L: 0.22 }[opts.logo_size] || 0.16;
  const margin = 1.2;
  const style = (pos) => {
    const base = `width:${(frac * 100).toFixed(1)}%;aspect-ratio:100/30;`;
    if (pos === "topleft") return `${base}top:${margin}%;left:${margin}%;`;
    if (pos === "topright") return `${base}top:${margin}%;right:${margin}%;`;
    if (pos === "bottomleft") return `${base}bottom:${margin}%;left:${margin}%;`;
    return `${base}bottom:${margin}%;right:${margin}%;`;
  };
  return `<div class="full">
    <div class="logopreview"><span class="logobox" style="${style(opts.logo_preset)}"><span>${esc(opts.logo_preset)}</span></span></div>
  </div>`;
}

function logoBoxFrom(opts) {
  if (!opts.logo_preset) return null;
  if (opts.logo_preset === "custom") {
    const c = opts.logoCustom || {};
    return {
      preset: "custom",
      x: c.x ?? 0.03, y: c.y ?? 0.03, w: c.w ?? 0.3, h: c.h ?? 0.1,
      size: opts.logo_size || "M", feather: Number(opts.logo_feather) || 0,
    };
  }
  return {
    preset: opts.logo_preset,
    size: opts.logo_size || "M",
    feather: Number(opts.logo_feather) || 0,
  };
}


// ------------------------------------------------- v0.5.0 tool modals
async function openBeats(epId) {
  openModal("Beat map", `<div class="stepmsg"><span class="spinner"></span>Measuring loudness peaks offline…</div>`, true);
  try {
    const data = await api(`/api/episodes/${epId}/beats`);
    const beats = data.beats || [];
    modalBody().innerHTML = `
      <div class="stepmsg">${data.count} beat markers across ${fmtDur(data.duration)}
        ${data.cached ? "· cached from the last scan" : "· scanned now"}</div>
      ${data.reason ? `<div class="noticeline">${ic("alert")} ${esc(data.reason)}</div>` : ""}
      <div class="beats">${beats.slice(0, 160).map((b) => `<span class="beat" title="${Number(b).toFixed(2)}s"></span>`).join("")}</div>
      <div class="probegrid">
        <div class="kv"><span>First beat</span><b>${beats.length ? fmtDur(beats[0]) : "—"}</b></div>
        <div class="kv"><span>Spacing</span><b>${beats.length > 1 ? (beats[1] - beats[0]).toFixed(2) + "s apart" : "—"}</b></div>
        <div class="kv"><span>Media</span><b title="${esc(data.media || "")}">${esc(trim(data.media || "none", 26))}</b></div>
      </div>
      <div class="cutbar">
        <span class="count-pill">turn on “sync to beats” in Fine-tune to snap cut points</span>
        <button class="btn small" id="beatsRescan">${ic("retry")} Rescan</button>
      </div>`;
    document.querySelector("#beatsRescan")?.addEventListener("click", () => {
      api(`/api/episodes/${epId}/beats?refresh=1`).then(() => openBeats(epId)).catch((e) => toast(e.message, true));
    });
  } catch (error) {
    modalBody().innerHTML = `<div class="stepmsg error">${ic("alert")} ${esc(error.message)}</div>`;
  }
}

async function openInspector(clipId) {
  openModal("Clip inspector", `<div class="stepmsg"><span class="spinner"></span>Probing the rendered file…</div>`, true);
  try {
    const info = await api(`/api/clips/${clipId}/probe`);
    const kv = (label, value, title) => `<div class="kv"><span>${label}</span><b${title ? ` title="${esc(title)}"` : ""}>${esc(value)}</b></div>`;
    modalBody().innerHTML = `
      <div class="probegrid">
        ${kv("Resolution", info.width && info.height ? `${info.width}\u00d7${info.height}` : "—")}
        ${kv("Aspect", info.aspect || "—", info.orientation || "")}
        ${kv("Frame rate", info.fps ? `${info.fps.toFixed(2)} fps` : "—")}
        ${kv("Duration", info.duration ? `${info.duration.toFixed(2)} s` : "—")}
        ${kv("Video codec", info.video_codec || "—")}
        ${kv("Audio", info.audio_codec ? `${info.audio_codec}${info.sample_rate ? ` \u00b7 ${Math.round(info.sample_rate / 1000)} kHz` : ""}` : "none")}
        ${kv("File size", info.size_bytes ? fmtBytes(info.size_bytes) : "—")}
        ${kv("Quality preset", info.stored?.quality || "—")}
        ${kv("Captions", info.stored?.captions_brand && info.stored.captions_brand !== "none" ? info.stored.captions_brand : "plain")}
        ${kv("Source media", info.stored?.source === "demo" ? "demo test card" : (info.source || "youtube"),
             info.stored?.source === "demo" ? "Synthetic media Qyro generates for offline runs — not footage from YouTube." : "")}
      </div>
      ${info.stored?.source === "demo" ? `<div class="noticeline demo">${ic("alert")} This short was cut from Qyro's synthetic demo media. The colour bars and the tone are the placeholder itself — load a YouTube link to render real footage.</div>` : ""}
      ${info.missing ? `<div class="noticeline">${ic("alert")} The file is not on disk — the card is stale; re-render or delete it.</div>` : ""}
      <div class="cutbar">
        <a class="btn small" href="/api/clips/${clipId}/audio.mp3">${ic("music")} Audio (MP3)</a>
        <button class="btn small" onclick="openThumbPicker('${clipId}')">${ic("image")} Thumbnail</button>
        <a class="btn small" href="/api/clips/${clipId}/file?dl=1" download="qyro-${clipId}.mp4">${ic("download")} Video</a>
      </div>`;
  } catch (error) {
    modalBody().innerHTML = `<div class="stepmsg error">${ic("alert")} ${esc(error.message)}</div>`;
  }
}

async function openThumbPicker(clipId) {
  openModal("Choose a thumbnail", `<div class="stepmsg"><span class="spinner"></span>Pulling frames from the clip…</div>`, true);
  try {
    const data = await api(`/api/clips/${clipId}/thumb-candidates?n=${configN()}`);
    const list = data.candidates || [];
    modalBody().innerHTML = `
      <div class="stepmsg">${list.length} frames spread across the clip. Tap one to make it the card poster.</div>
      <div class="candgrid">
        ${list.map((c) => `
          <button class="cand" data-index="${c.index}">
            <img src="${esc(c.url)}" alt="frame at ${Number(c.time).toFixed(1)}s" loading="lazy">
            <span class="tagrow"><span>${fmtDur(c.time)}</span><span>${ic("check")}</span></span>
          </button>`).join("")}
      </div>`;
    modalBody().querySelectorAll(".cand").forEach((btn) => {
      btn.addEventListener("click", async () => {
        btn.classList.add("on");
        try {
          await api(`/api/clips/${clipId}/thumb-pick`, {
            method: "POST", body: JSON.stringify({ index: Number(btn.dataset.index) }),
          });
          toast("Thumbnail set");
          closeModal();
          refresh();
        } catch (error) {
          toast(error.message, true);
          btn.classList.remove("on");
        }
      });
    });
  } catch (error) {
    modalBody().innerHTML = `<div class="stepmsg error">${ic("alert")} ${esc(error.message)}</div>`;
  }
}

function configN() { return 6; }

async function openTitleLab(clipId) {
  const clip = (state.data?.clips || []).find((item) => item.id === clipId);
  const seed = clip ? `${clip.title}. ${(clip.reasons || []).join(". ")}` : "";
  titleLabModal(clipId, seed, clip ? clip.profile || "viral" : "viral");
}

function titleLabModal(clipId, seed, profile) {
  openModal(clipId ? "Title lab" : "Title lab — free text", `
    <div class="stepmsg">Ten ways to say it, scored for ${profile === "viral" ? "the feed" : "this profile"}. Offline templates by default; a free AI key rewrites them.</div>
    <textarea id="tlText" rows="4" placeholder="Paste the line, or the whole transcript slice…">${esc(seed)}</textarea>
    <div class="cutbar">
      <select id="tlProfile" class="sortsel">
        ${[["viral", "Viral picks"], ["story", "Story arc"], ["facts", "Numbers & facts"], ["energy", "High energy"]]
          .map(([v, l]) => `<option value="${v}"${v === profile ? " selected" : ""}>${l}</option>`).join("")}
      </select>
      <button class="btn primary small" id="tlGo">${ic("wand")} Make titles</button>
      <span class="spacer"></span>
      <span class="count-pill" id="tlEngine"></span>
    </div>
    <div id="tlOut"></div>`, true);
  const go = () => runTitleLab(clipId);
  document.querySelector("#tlGo").addEventListener("click", go);
  document.querySelector("#tlProfile").addEventListener("change", go);
  if (seed) go();
}

async function runTitleLab(clipId) {
  const text = document.querySelector("#tlText").value.trim();
  const profile = document.querySelector("#tlProfile").value;
  const out = document.querySelector("#tlOut");
  const engine = document.querySelector("#tlEngine");
  if (!text) { out.innerHTML = `<div class="stepmsg error">${ic("alert")} Add a line of text first.</div>`; return; }
  out.innerHTML = `<div class="stepmsg"><span class="spinner"></span>Writing…</div>`;
  try {
    const data = await api("/api/titles", {
      method: "POST",
      body: JSON.stringify({ text, profile, episode_id: clipId || undefined }),
    });
    engine.textContent = data.engine === "offline" ? "offline engine" : `${data.engine} engine`;
    out.innerHTML = `
      ${data.notice ? `<div class="noticeline">${ic("alert")} ${esc(data.notice)}</div>` : ""}
      <div class="titlelab">
        ${(data.titles || []).map((t) => `<div class="packtitle" data-copy="${esc(t)}">${esc(t)}</div>`).join("")}
      </div>
      <div class="tags">${(data.hashtags || []).map((t) => `<span class="tag">${esc(t)}</span>`).join("")}</div>
      <div class="cutbar"><button class="btn small" id="tlCopyAll">${ic("copy")} Copy titles + tags</button></div>`;
    out.querySelectorAll("[data-copy]").forEach((el) => el.addEventListener("click", async () => {
      await copyText(el.dataset.copy);
      toast("Title copied");
    }));
    document.querySelector("#tlCopyAll")?.addEventListener("click", async () => {
      await copyText(`${(data.titles || []).join("\n")}\n\n${(data.hashtags || []).join(" ")}`);
      toast("Copied");
    });
  } catch (error) {
    out.innerHTML = `<div class="stepmsg error">${ic("alert")} ${esc(error.message)}</div>`;
  }
}

function fileToB64(file) {
  return new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => resolve(String(fr.result).split(",")[1] || "");
    fr.onerror = () => reject(new Error("Could not read that file"));
    fr.readAsDataURL(file);
  });
}

async function openTools() {
  const tracks = (state.data && state.data.audio_tracks) || [];
  openModal("Audio beds & offline tools", `
    <div class="tabs">
      <button class="tab on" data-tab="tracks">${ic("music")} Music beds</button>
      <button class="tab" data-tab="titles">${ic("wand")} Title lab</button>
      <button class="tab" data-tab="engine">${ic("sparkle")} Free AI engine</button>
      <button class="tab" data-tab="session">${ic("key")} YouTube session</button>
    </div>
    <div class="tabpanel" data-panel="tracks">
      <div class="stepmsg">Any MP3, M4A, WAV or OGG file on this device. Qyro loops it under the short — no cloud, no sample list.</div>
      <label class="drop" id="trackDrop"><input type="file" id="trackFile" accept="audio/*">
        ${ic("upload")} Tap to pick an audio file (up to 40 MB)</label>
      <div class="tracklist" style="margin-top:12px">
        ${tracks.length ? tracks.map((t) => `
          <div class="trackrow">
            ${ic("music")}
            <span class="tname">${esc(t.name)}</span>
            <span class="count-pill">${fmtDur(t.duration)} · ${fmtBytes(t.bytes)}</span>
            <audio controls preload="none" src="/api/audio/${esc(t.id)}/file"></audio>
          </div>`).join("") : `<div class="stepmsg">No beds yet — shorts keep their own audio.</div>`}
      </div>
    </div>
    <div class="tabpanel" data-panel="titles" hidden>
      <div class="stepmsg">Write a caption pack for any text, no clip needed.</div>
      <div id="tlFree"></div>
    </div>
    <div class="tabpanel" data-panel="engine" hidden>
      <div id="engNote"></div>
      <div class="cutbar"><button class="btn small" id="engOpen">${ic("sliders")} Open settings</button></div>
    </div>
    <div class="tabpanel" data-panel="session" hidden>
      <div id="sessNote"></div>
    </div>`, true);
  modalBody().querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
    modalBody().querySelectorAll(".tab").forEach((t) => t.classList.toggle("on", t === tab));
    modalBody().querySelectorAll(".tabpanel").forEach((p) => {
      p.hidden = p.dataset.panel !== tab.dataset.tab;
    });
  }));
  const file = document.querySelector("#trackFile");
  file.addEventListener("change", async () => {
    const picked = file.files && file.files[0];
    if (!picked) return;
    try {
      const b64 = await fileToB64(picked);
      const result = await api("/api/audio", {
        method: "POST", body: JSON.stringify({ name: picked.name, data_b64: b64 }),
      });
      toast(`“${result.name}” is ready — pick it in Fine-tune`);
      refresh();
      openTools();
    } catch (error) {
      toast(error.message, true);
    }
  });
  const drop = document.querySelector("#trackDrop");
  ["dragover", "dragenter"].forEach((evt) => drop.addEventListener(evt, (e) => {
    e.preventDefault(); drop.classList.add("on");
  }));
  ["dragleave", "drop"].forEach((evt) => drop.addEventListener(evt, (e) => {
    e.preventDefault(); drop.classList.remove("on");
    if (evt === "drop" && e.dataTransfer?.files?.[0]) {
      file.files = e.dataTransfer.files;
      file.dispatchEvent(new Event("change"));
    }
  }));
  document.querySelector("#engOpen").addEventListener("click", openSettings);
  titleLabFree();
  engineNote();
  sessionNote();
}

/* A 429 is the one failure the user can actually cure, so the card that shows
   the error also offers the cure instead of just "wait and retry". */
function isRateLimitError(message) {
  return /rate-limit|rate limit|too many requests|http 429/i.test(String(message || ""));
}

function switchToolTab(name) {
  const tab = document.querySelector(`#modalRoot .tab[data-tab="${name}"]`);
  if (tab) tab.click();
}

/* YouTube 429s on anonymous caption downloads are the one failure Qyro cannot
   fix on its own — it needs a signed-in session. This is the only place a
   phone user can supply one, so it doubles as the explanation of the block. */
function sessionNote() {
  const host = document.querySelector("#sessNote");
  if (!host) return;
  const yt = (state.health && state.health.youtube) || {};
  const cookies = yt.cookies || {};
  const block = yt.rate_limit || {};
  host.innerHTML = `
    <div class="probegrid">
      <div class="kv"><span>YouTube session</span><b>${
        cookies.present
          ? `installed · ${cookies.lines} cookie(s)`
          : "not installed — requests are anonymous"
      }</b></div>
      <div class="kv"><span>Subtitle block</span><b>${
        block.blocked ? `${block.minutes} min cooldown` : "none"
      }</b></div>
    </div>
    ${block.blocked
      ? `<div class="noticeline">${ic("alert")} YouTube answered HTTP 429, so transcript downloads are paused for about ${block.minutes} more minute(s). Qyro will not hammer it while the block stands — and anything already fetched is cached.</div>`
      : ""}
    <div class="stepmsg">Export a Netscape <code>cookies.txt</code> from a browser signed in to YouTube (the “Get cookies.txt LOCALLY” extension, in a private window) and pick it below. Qyro then downloads captions and video as your account, which is what actually ends the 429s. The file never leaves this device and is never shown back to you.</div>
    <label class="drop" id="cookieDrop"><input type="file" id="cookieFile" accept=".txt,text/plain">
      ${ic("upload")} Tap to pick cookies.txt</label>
    <div class="cutbar">
      <button class="btn small" id="sessRemove"${cookies.present ? "" : " disabled"}>${ic("trash")} Remove session</button>
      <span class="spacer"></span>
      <button class="btn small" id="sessHelp">${ic("external")} How to export</button>
    </div>
    <div id="sessOut"></div>`;

  document.querySelector("#cookieFile").addEventListener("change", async (event) => {
    const picked = event.target.files && event.target.files[0];
    if (!picked) return;
    try {
      const b64 = await fileToB64(picked);
      const result = await api("/api/cookies", {
        method: "POST", body: JSON.stringify({ name: picked.name, data_b64: b64 }),
      });
      toast(result.present
        ? `YouTube session installed (${result.lines} cookies) — the block is cleared, generate again`
        : "Session saved");
      await refreshHealth();
      renderHealth(state.health);
      sessionNote();
    } catch (error) {
      toast(error.message, true);
    }
  });
  document.querySelector("#sessRemove").addEventListener("click", async () => {
    try {
      await api("/api/cookies", { method: "DELETE" });
      toast("YouTube session removed — requests are anonymous again");
      await refreshHealth();
      renderHealth(state.health);
      sessionNote();
    } catch (error) {
      toast(error.message, true);
    }
  });
  document.querySelector("#sessHelp").addEventListener("click", () => {
    document.querySelector("#sessOut").innerHTML = `
      <div class="tlines">
        <div class="stepmsg"><b>Chrome / Edge / Brave</b> — install “Get cookies.txt LOCALLY”, open YouTube in a private window and sign in, click the extension, <i>Export</i> → <i>Export as cookies.txt</i>.</div>
        <div class="stepmsg"><b>Firefox</b> — the same extension exists; export from the YouTube tab while signed in.</div>
        <div class="stepmsg">Save the file anywhere on this device, then pick it above. That is the whole fix — no restart needed.</div>
        <div class="stepmsg">No browser handy? Run <code>pip install -U yt-dlp</code>: YouTube rotates which clients it challenges and a newer extractor often clears the block on its own.</div>
      </div>`;
  });
}

function titleLabFree() {
  const host = document.querySelector("#tlFree");
  if (!host) return;
  host.innerHTML = `
    <textarea id="tlText" rows="4" placeholder="Paste a transcript slice or a one-line thought…"></textarea>
    <div class="cutbar">
      <select id="tlProfile" class="sortsel">
        <option value="viral">Viral picks</option><option value="story">Story arc</option>
        <option value="facts">Numbers &amp; facts</option><option value="energy">High energy</option>
      </select>
      <button class="btn primary small" id="tlGo">${ic("wand")} Make titles</button>
      <span class="spacer"></span><span class="count-pill" id="tlEngine"></span>
    </div>
    <div id="tlOut"></div>`;
  document.querySelector("#tlGo").addEventListener("click", () => runTitleLab(null));
}

function engineNote() {
  const host = document.querySelector("#engNote");
  if (!host) return;
  const eng = (state.data && state.data.engine) || { provider: "offline" };
  host.innerHTML = `<div class="probegrid">
      <div class="kv"><span>Engine</span><b>${esc(eng.provider || "offline")}</b></div>
      <div class="kv"><span>Model</span><b>${esc(eng.model || "offline templates")}</b></div>
      <div class="kv"><span>Keys</span><b>${eng.key_set ? "stored on this device" : "none set"}</b></div>
    </div>
    <div class="noticeline info">${ic("check")} Titles, hashtags and polish fall back to the offline engine on any failure — Qyro never needs an account.</div>`;
}

// ------------------------------------------------------------- settings
const PROVIDER_OPTIONS = [
  ["offline", "Offline templates (free, no key)"],
  ["gemini", "Google AI Studio — free key"],
  ["groq", "Groq — free key"],
  ["custom", "Custom OpenAI-compatible URL"],
];

async function openSettings() {
  const s = (state.data && state.data.settings) || {};
  const eng = (state.data && state.data.engine) || {};
  openModal("Qyro settings", `
    <div class="minihead">Free AI engine (optional)</div>
    <div class="stepmsg">Qyro writes titles offline and needs no account. If you add a free key, titles, hashtags and the upload pack get an AI rewrite — and any failure quietly falls back to offline text.</div>
    <div class="advgrid" style="margin-top:10px">
      <label class="full">Provider <select id="setProvider">${chosen(PROVIDER_OPTIONS, eng.provider || "offline")}</select></label>
      <label>Model <input id="setModel" type="text" maxlength="120" placeholder="gemini-3.6-flash / llama-3.3-70b" value="${esc(eng.model_effective || eng.model || "")}"></label>
      <label>Base URL (custom only) <input id="setBase" type="text" maxlength="300" placeholder="http://127.0.0.1:11434/v1" value="${esc(eng.base_url || "")}"></label>
      <label>Google AI Studio key <input id="setGemini" type="password" maxlength="400" placeholder="${s.gemini_key_set ? "saved \u2014 type to replace" : "AQ\u2026 or AIza\u2026"}" autocomplete="off"></label>
      <label class="full"><span class="hint">New Google keys start with <code>AQ.</code> (older ones with <code>AIza</code>) — both work: Qyro calls Google's own endpoint, the only route the new keys accept. Leave Model empty to follow the free-tier default.</span></label>
      ${eng.model_note ? `<label class="full"><span class="noticeline">${ic("alert")} ${esc(eng.model_note)} <button class="btn small" id="setModelDefault" type="button">Follow the current default instead</button></span></label>` : ""}
      <label>Groq key <input id="setGroq" type="password" maxlength="400" placeholder="${s.groq_key_set ? "saved \u2014 type to replace" : "gsk_\u2026"}" autocomplete="off"></label>
      <label>OpenAI-compatible key <input id="setAi" type="password" maxlength="400" placeholder="${s.ai_key_set ? "saved \u2014 type to replace" : "optional"}" autocomplete="off"></label>
    </div>
    <div class="noticeline info">${ic("check")} Keys live only in <code>data/state.json</code> on this device. They are never echoed back by the API, never written to a log, and never leave the machine except to the provider you chose.</div>
    <div class="minihead">Defaults for new episodes</div>
    <div class="advgrid">
      <label>Quality <select id="setQuality">${chosen(QUALITY_OPTIONS, baseDefaults.quality)}</select></label>
      <label>Caption brand <select id="setBrand">${chosen(BRAND_OPTIONS, baseDefaults.captions_brand)}</select></label>
      <label class="full"><span class="hint" id="qualHint"></span></label>
      <label class="toggle" title="Play the synthesised ta-dum when the ident runs">${ic("music")}<input type="checkbox" id="setIdentSound"> ident sound on</label>
      <label class="full"><span class="hint">The intro ident is a local effect: the glowing Q, the spectrum beams and the ta-dum are all generated in the browser by <code>web/intro.js</code>, so nothing is fetched and nothing is stored off this device. Turning the sound off is remembered in this browser.</span></label>
    </div>
    <div class="cutbar">
      <button class="btn primary small" id="setSave">${ic("check")} Save settings</button>
      <span class="spacer"></span>
      <span class="count-pill">${esc((state.health || {}).version || "")}</span>
    </div>`, true);
  const sync = () => {
    const el = document.querySelector("#setQuality");
    const note = document.querySelector("#qualHint");
    if (el && note) note.textContent = QUALITY_NOTE[el.value] || "";
  };
  document.querySelector("#setQuality").addEventListener("change", sync);
  sync();
  const identSound = document.querySelector("#setIdentSound");
  if (identSound) {
    identSound.checked = !window.QyroIdent || window.QyroIdent.soundEnabled();
    identSound.addEventListener("change", (event) => setIdentSound(event.target.checked));
  }
  // A retired id in the Model box: one click empties it, and Qyro always calls
  // the provider's current free-tier model from then on.
  document.querySelector("#setModelDefault")?.addEventListener("click", () => {
    const field = document.querySelector("#setModel");
    if (field) field.value = "";
    toast("Model cleared — save settings and Qyro follows the current default");
  });
  document.querySelector("#setSave").addEventListener("click", async (event) => {
    const btn = event.currentTarget;
    const body = {
      ai_provider: document.querySelector("#setProvider").value,
      ai_model: document.querySelector("#setModel").value.trim(),
      ai_base_url: document.querySelector("#setBase").value.trim(),
    };
    const secret = (id, key) => {
      const value = document.querySelector(id).value.trim();
      if (value) body[key] = value;      // blank means "leave the stored key alone"
    };
    secret("#setGemini", "gemini_key");
    secret("#setGroq", "groq_key");
    secret("#setAi", "ai_key");
    btn.disabled = true;
    try {
      await api("/api/settings", { method: "POST", body: JSON.stringify(body) });
      const quality = document.querySelector("#setQuality").value;
      const captions_brand = document.querySelector("#setBrand").value;
      baseDefaults = { ...baseDefaults, quality, captions_brand };
      presetOverlay = { ...presetOverlay, quality, captions_brand };
      toast("Settings saved");
      closeModal();
      refresh();
    } catch (error) {
      toast(error.message, true);
      btn.disabled = false;
    }
  });
}

function renderEngineHint() {
  const el = document.querySelector("#engineHint");
  if (!el) return;
  const eng = (state.data && state.data.engine) || {};
  if (eng.provider && eng.provider !== "offline") {
    el.className = "hint ok";
    el.innerHTML = `${ic("sparkle")} free AI engine: ${esc(eng.provider)}`;
  } else {
    el.className = "hint";
    el.textContent = "offline titles · add a free key in settings for AI rewrites";
  }
}

// ---------------------------------------------------------------- polling
function fastPoll() {
  clearInterval(pollTimer);
  pollTimer = setInterval(() => {
    const busy = (state.data?.jobs || []).length > 0;
    refresh();
    if (!busy) {
      clearInterval(pollTimer);
      pollTimer = setInterval(refresh, 8000);
    }
  }, 1500);
}

// ---------------------------------------------------------------- events
document.addEventListener("change", (event) => {
  const card = event.target?.closest?.(".episode");
  if (card) rememberCardOpts(card);
  // Selects that change what the card *shows* (brand swatch, logo box,
  // quality warning) re-render the list; the open/closed state survives.
  if (card && /^(opt-brand|opt-logo|opt-logo-size|opt-quality|opt-captions-enabled)$/.test(
      (event.target.className || "").split(/\s+/).join("|"))) renderEpisodes();
});

document.addEventListener("input", (event) => {
  const card = event.target?.closest?.(".episode");
  if (!card) return;
  if (event.target.classList.contains("manual-start")
      || event.target.classList.contains("manual-end")
      || event.target.classList.contains("manual-title")) {
    rememberManual(card);
  } else {
    rememberCardOpts(card);
  }
  // range sliders show their value without a full re-render (no focus loss)
  const target = event.target;
  if (target.classList.contains("opt-silence-noise")) {
    const out = card?.querySelector(".out-silence-noise");
    if (out) out.textContent = `${Number(target.value).toFixed(0)} dB`;
  } else if (target.classList.contains("opt-silence-min")) {
    const out = card?.querySelector(".out-silence-min");
    if (out) out.textContent = `${Number(target.value).toFixed(1)} s`;
  } else if (target.classList.contains("opt-logo-feather")) {
    const out = card?.querySelector(".out-logo-feather");
    if (out) out.textContent = `${target.value}px`;
  }
});

document.addEventListener("toggle", (event) => {
  const element = event.target;
  if (!element?.classList) return;
  const card = element.closest?.(".episode");
  const clip = element.closest?.(".clip");
  if (element.classList.contains("advopts") && card) {
    state.detailsOpen[`adv-${card.dataset.id}`] = element.open;
  } else if (element.classList.contains("pack") && clip) {
    state.detailsOpen[`pack-${clip.dataset.id}`] = element.open;
  }
}, true);

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeModal();
});

/* --------------------------------------------------------------------- *
 * The v6.3 "spectrum" ident.
 *
 * web/intro.js owns the timeline, the canvas beams and the synthesised
 * ta-dum; app.js only decides when to run it and wires the two controls
 * the UI offers: replay the ident, and switch its sound off permanently.
 * Keeping the gate here means the overlay still hides itself (CSS
 * fail-safe) if the ident script never loads.
 * --------------------------------------------------------------------- */
function identAvailable() {
  return !!window.QyroIdent;
}

/* The ident is a 6-second full-screen animation. It must never stand between
 * someone and the render they are waiting for, so the auto-play is decided
 * after the first /api/state: while a job is queued or running — which is when
 * a phone is busiest and a reload is most likely — the app opens straight to
 * the work. The header sparkle button and ?intro=1 always still play it. */
function appIsBusy() {
  const jobs = (state.data && state.data.jobs) || [];
  return jobs.some((job) => job.status === "queued" || job.status === "running");
}

function initIntro() {
  if (!identAvailable()) return;
  if (appIsBusy() && !/[?&]intro=1(&|$)/.test(location.search)) return;
  window.QyroIdent.boot();
}

function replayIntro() {
  if (!identAvailable()) {
    toast("Ident unavailable - reload the page (web/intro.js did not load).", true);
    return;
  }
  window.QyroIdent.replay();
}

function setIdentSound(on) {
  if (!identAvailable()) return;
  window.QyroIdent.setSoundEnabled(!!on);
  const btn = document.getElementById("identBtn");
  if (btn) btn.classList.toggle("muted", !on);
}

window.addEventListener("DOMContentLoaded", async () => {
  $("#loadPlaylist").addEventListener("click", loadPlaylist);
  $("#loadDemo").addEventListener("click", loadDemo);
  $("#dashBtn").addEventListener("click", openDashboard);
  $("#processAll").addEventListener("click", processAll);
  $("#searchBtn").addEventListener("click", openSearch);
  $("#presetSave").addEventListener("click", savePreset);
  $("#presetDelete").addEventListener("click", deletePreset);
  $("#presetSel").addEventListener("change", (event) => applyPreset(event.target.value));
  $("#autopilot").addEventListener("change", (event) => setAutopilot(event.target.checked));
  $("#demoLink").addEventListener("click", (event) => {
    event.preventDefault();
    loadDemo();
  });
  $("#episodeSearch").addEventListener("input", renderEpisodes);
  $("#clipSearch").addEventListener("input", renderShorts);
  $("#clipSort").addEventListener("change", renderShorts);
  $("#zipBtn").addEventListener("click", downloadZip);
  $("#toolsBtn").addEventListener("click", openTools);
  $("#settingsBtn").addEventListener("click", openSettings);
  $("#identBtn").addEventListener("click", replayIntro);
  setIdentSound(!window.QyroIdent || window.QyroIdent.soundEnabled());
  hydrateIcons();
  if ("serviceWorker" in navigator && location.protocol.startsWith("http")) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }

  $("#playlistUrl").addEventListener("keydown", (event) => {
    if (event.key === "Enter") loadPlaylist();
  });

  await refresh();
  if (!$("#playlistUrl").value && baseDefaults.playlist_url) {
    $("#playlistUrl").value = baseDefaults.playlist_url;
  }
  initIntro();
  if ((state.data?.jobs || []).length) fastPoll();
  else pollTimer = setInterval(refresh, 8000);
});

window.installFonts = installFonts;
window.generate = generate;
window.previewPicks = previewPicks;
window.toggleManual = toggleManual;
window.cutManual = cutManual;
window.deleteClip = deleteClip;
window.deleteEpisode = deleteEpisode;
window.exportCsv = exportCsv;
window.shareClip = shareClip;
window.renameClip = renameClip;
window.loadDemo = loadDemo;
window.loadPlaylist = loadPlaylist;
window.copyPack = copyPack;
window.polishClip = polishClip;
window.rerenderClip = rerenderClip;
window.openChapters = openChapters;
window.openCutter = openCutter;
window.openSearch = openSearch;
window.jumpToHit = jumpToHit;
window.tapLine = tapLine;
window.cutFromCutter = cutFromCutter;
window.openDashboard = openDashboard;
window.closeModal = closeModal;
window.cleanStorage = cleanStorage;
window.restoreFromFile = restoreFromFile;
window.retryJob = retryJob;
window.cancelJob = cancelJob;
window.syncCardSelect = syncCardSelect;
window.openBeats = openBeats;
window.openInspector = openInspector;
window.openThumbPicker = openThumbPicker;
window.openTitleLab = openTitleLab;
window.openTools = openTools;
window.switchToolTab = switchToolTab;
window.openSettings = openSettings;
