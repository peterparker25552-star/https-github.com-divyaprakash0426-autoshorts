/* AutoShorts UI — vanilla JS, polls /api/state. */
const $ = (sel) => document.querySelector(sel);

const LENGTH_PRESETS = {
  short: [20, 30],
  medium: [30, 45],
  long: [45, 60],
  any: [20, 60],
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
  ["smart", "🎥 Smart"],
  ["crop", "center crop"],
  ["fill", "fill"],
  ["fit", "fit + bars"],
];
const QUALITY_OPTIONS = [
  ["fast", "720p class"],
  ["full", "1080p class"],
];

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

// Server-provided defaults, a preset overlay the user applied, and per-card
// picks. Polling re-renders the cards but never touches the last two, so the
// user's chosen options survive every refresh.
let baseDefaults = {
  count: 5,
  min_dur: 20,
  max_dur: 60,
  profile: "viral",
  style: "blur",
  quality: "fast",
  format: "vertical",
  captions: "classic",
  captions_pos: "standard",
  captions_box: false,
  speed: 1.0,
  progress: false,
  silence: false,
  loud: false,
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
    speed,
    progress: flag(".opt-progress"),
    silence: flag(".opt-silence"),
    loud: flag(".opt-loud"),
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
  $("#health").innerHTML = `
    <span class="chip ${yt ? "ok" : "bad"}"><span class="dot"></span>YouTube ${yt ? "reachable" : "unreachable — demo mode"}</span>
    <span class="chip ${h.ffmpeg ? "ok" : "bad"}"><span class="dot"></span>ffmpeg ${h.ffmpeg ? "ready" : "missing"}</span>
    <span class="chip"><span class="dot"></span>${esc(disk)}</span>
    <span class="chip ${llm ? "ok" : ""}"><span class="dot"></span>${llm ? "AI polish on" : "AI off"}</span>`;
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
  return `<div class="intel">📊 ${stats.words} words · ${stats.sentences} sentences ·
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

function renderAdvOpts(ep, opts) {
  const open = state.detailsOpen[`adv-${ep.id}`] ? " open" : "";
  return `<details class="advopts"${open}>
    <summary>⚙ Fine-tune</summary>
    <div class="advgrid">
      <label>Captions <select class="opt-captions">${chosen(CAPTION_OPTIONS, opts.captions)}</select></label>
      <label>Cap position <select class="opt-cap-pos">${chosen(CAPTION_POS_OPTIONS, opts.captions_pos)}</select></label>
      <label class="toggle"><input class="opt-cap-box" type="checkbox"${checkedAttr(opts.captions_box)}> 📦 caption box</label>
      <label>Format <select class="opt-format">${chosen(FORMAT_OPTIONS, opts.format)}</select></label>
      <label>Speed <input class="opt-speed" type="number" min="0.5" max="2" step="0.05" value="${Number(opts.speed) || 1}"></label>
      <label class="toggle"><input class="opt-progress" type="checkbox"${checkedAttr(opts.progress)}> progress bar</label>
      <label class="toggle"><input class="opt-silence" type="checkbox"${checkedAttr(opts.silence)}> jump-cut silence</label>
      <label class="toggle"><input class="opt-loud" type="checkbox"${checkedAttr(opts.loud)}> loudness</label>
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
    list.innerHTML = `<div class="empty" style="padding:34px 16px">
      <p>Load a YouTube playlist or single video above, or <a href="#" onclick="loadDemo();return false">try the demo</a>.</p>
    </div>`;
    return;
  }
  if (!episodes.length) {
    list.innerHTML = `<div class="empty" style="padding:34px 16px"><p>No matching episodes.</p></div>`;
    return;
  }

  list.innerHTML = episodes.map((ep) => {
    const status = ep.status || "new";
    const job = (state.data.jobs || []).find((item) => item.episode_id === ep.id);
    const busy = status === "processing";
    const pct = job ? Math.round(job.progress * 100) : 0;
    const opts = optsFor(ep.id);
    const disabled = busy ? "disabled" : "";
    return `
    <div class="episode" data-id="${ep.id}">
      <div class="title">${esc(ep.title)}</div>
      <div class="meta">
        <span class="badge ${ep.source === "demo" ? "demo" : "yt"}">${ep.source === "demo" ? "demo" : "youtube"}</span>
        <span class="badge status-${status}">${status}</span>
        <span>⏱ ${fmtDur(ep.duration)}</span>
        <span>🎬 ${ep.clip_count || 0}</span>
      </div>
      ${busy ? `
        <div class="progressbar"><div class="fill" style="width:${pct}%"></div></div>
        <div class="stepmsg">${esc(job?.message || job?.step || "working…")}</div>` : ""}
      ${ep.error ? `<div class="stepmsg error">⚠ ${esc(ep.error)}</div>` : ""}
      <div class="controls">
        <select class="opt-count" ${disabled} aria-label="Number of shorts">
          ${[1, 2, 3, 5, 8, 12].map((n) => `<option value="${String(n)}"${String(n) === String(opts.count) ? " selected" : ""}>${n} shorts</option>`).join("")}
        </select>
        <select class="opt-profile" ${disabled} aria-label="Highlight profile">
          <option value="viral"${opts.profile === "viral" ? " selected" : ""}>🔥 Viral</option>
          <option value="story"${opts.profile === "story" ? " selected" : ""}>📖 Story</option>
          <option value="facts"${opts.profile === "facts" ? " selected" : ""}>📊 Facts</option>
          <option value="energy"${opts.profile === "energy" ? " selected" : ""}>⚡ Energy</option>
        </select>
        <select class="opt-length" ${disabled} aria-label="Clip length">
          <option value="short"${lengthKey(opts.min_dur, opts.max_dur) === "short" ? " selected" : ""}>Short · 20–30s</option>
          <option value="medium"${lengthKey(opts.min_dur, opts.max_dur) === "medium" ? " selected" : ""}>Medium · 30–45s</option>
          <option value="long"${lengthKey(opts.min_dur, opts.max_dur) === "long" ? " selected" : ""}>Long · 45–60s</option>
          <option value="any"${lengthKey(opts.min_dur, opts.max_dur) === "any" ? " selected" : ""}>Any · 20–60s</option>
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
          ${busy ? "Working…" : "Generate"}
        </button>
        <button class="btn small" onclick="previewPicks('${ep.id}', this)" ${disabled}>👁 Preview</button>
      </div>
      ${renderAdvOpts(ep, opts)}
      ${renderMoments(ep.id)}
      ${renderManualBox(ep)}
      <div class="subrow">
        <button class="btn small" onclick="openCutter('${ep.id}')" ${disabled}>📜 Transcript cutter</button>
        <button class="btn small" onclick="openChapters('${ep.id}')" ${disabled}>📑 Chapters</button>
        <button class="btn small" onclick="exportCsv('${ep.id}')" ${disabled}>📊 CSV</button>
        <button class="btn small" onclick="toggleManual('${ep.id}')" ${disabled}>✂ Exact range</button>
        <span class="spacer"></span>
        <a class="btn small" href="https://www.youtube.com/watch?v=${esc(ep.id)}" target="_blank" rel="noopener">↗ YouTube</a>
        <button class="btn small iconbtn" title="Delete episode and its clips" onclick="deleteEpisode('${ep.id}', this)" ${disabled}>🗑</button>
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
    return `
    <div class="clip" data-id="${clip.id}">
      <video controls preload="metadata" playsinline
             ${clip.thumb ? `poster="/api/clips/${clip.id}/thumb"` : ""}
             src="/api/clips/${clip.id}/file"></video>
      <div class="body">
        <div class="clip-title">${esc(clip.title)}</div>
        <div class="sub">From: <a href="https://www.youtube.com/watch?v=${esc(clip.episode_id)}&t=${Math.floor(clip.start || 0)}s" target="_blank" rel="noopener" title="${esc(clip.episode_title)}">${esc(trim(clip.episode_title, 42))}</a></div>
        <div class="sub timechip">⏱ ${fmtRange(clip.start, clip.end)} · ${Number(clip.duration).toFixed(1)}s · ${clip.width || 720}×${clip.height || 1280}${clip.format && clip.format !== "vertical" ? ` · ${esc(clip.format)}` : ""} · ${esc(clip.style || "blur")}${clip.captions_box ? " 📦" : ""}${clip.captions_pos === "low" ? " ⬇" : ""}</div>
        ${waveformStrip(clip.waveform)}
        <div class="scorebox">
          <div class="scorebar"><div class="fill" style="width:${Math.round((Number(clip.score || 0) / maxScore) * 100)}%"></div></div>
          <div class="scorenum">${Number(clip.score || 0).toFixed(1)}</div>
        </div>
        ${signalBars(clip.breakdown)}
        <div class="reasons">${(clip.reasons || []).map((reason) => `<span class="reason">${esc(reason)}</span>`).join("")}</div>
        ${pack ? `<details class="pack"${open}>
          <summary>📝 Upload pack${pack.polished_by ? ` · ✨ ${esc(pack.polished_by)}` : ""}</summary>
          <div class="packbody">
            ${(pack.titles || []).map((title) => `<div class="packtitle">${esc(title)}</div>`).join("")}
            <div class="tags">${(pack.hashtags || []).map((tag) => `<span class="tag">${esc(tag)}</span>`).join("")}</div>
            <pre class="packdesc">${esc(pack.description || "")}</pre>
          </div>
        </details>` : ""}
        <div class="actions">
          <a class="btn small" href="/api/clips/${clip.id}/file" download="autoshort-${clip.id}.mp4">🎬 Video</a>
          <a class="btn small" href="/api/clips/${clip.id}/srt">💬 SRT</a>
          ${clip.thumb ? `<a class="btn small" href="/api/clips/${clip.id}/thumb" target="_blank" rel="noopener">🖼 Thumb</a>` : ""}
          <button class="btn small" onclick="shareClip('${clip.id}')">📤 Share</button>
          <button class="btn small" onclick="renameClip('${clip.id}')">✏️ Rename</button>
          <button class="btn small iconbtn" aria-label="Delete clip" onclick="deleteClip('${clip.id}')">🗑</button>
        </div>
        <div class="actions">
          <button class="btn small" onclick="copyPack('${clip.id}')" ${pack ? "" : "disabled"}>📋 Copy pack</button>
          <button class="btn small" onclick="rerenderClip('${clip.id}')">♻ Re-render</button>
          ${aiOn ? `<button class="btn small" onclick="polishClip('${clip.id}')">✨ Polish</button>` : ""}
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
    toast(`Demo playlist loaded (${result.added} episodes) — hit Generate!`);
  } catch (error) {
    toast(error.message, true);
  }
  refresh();
}

function renderOptsPayload(opts) {
  return {
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
    speed: Number(opts.speed) || 1,
    progress: !!opts.progress,
    silence: !!opts.silence,
    loud: !!opts.loud,
  };
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
    btn.textContent = "👁 Preview";
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
  link.download = `autoshorts-${epId}-moments.csv`;
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
  const title = clip?.title || "AutoShorts clip";
  const url = `${location.origin}/api/clips/${id}/file`;
  try {
    if (navigator.share && navigator.canShare) {
      const response = await fetch(`/api/clips/${id}/file`);
      if (!response.ok) throw new Error("Could not load clip");
      const blob = await response.blob();
      const file = new File([blob], `autoshort-${id}.mp4`, { type: "video/mp4" });
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
  try {
    const result = await api(`/api/clips/${id}/polish`, { method: "POST" });
    toast(`Pack polished by ${result.pack?.polished_by || "the model"}`);
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
      <label>Captions <select id="rr-captions">${chosen(CAPTION_OPTIONS, opts.captions)}</select></label>
      <label>Cap position <select id="rr-cappos">${chosen(CAPTION_POS_OPTIONS, opts.captions_pos || "standard")}</select></label>
      <label class="toggle"><input id="rr-capbox" type="checkbox"${checkedAttr(opts.captions_box)}> 📦 caption box</label>
      <label>Speed <input id="rr-speed" type="number" min="0.5" max="2" step="0.05" value="${Number(opts.speed) || 1}"></label>
      <label class="toggle"><input id="rr-progress" type="checkbox"${checkedAttr(opts.progress)}> progress bar</label>
      <label class="toggle"><input id="rr-silence" type="checkbox"${checkedAttr(opts.silence)}> jump-cut silence</label>
      <label class="toggle"><input id="rr-loud" type="checkbox"${checkedAttr(opts.loud)}> loudness</label>
      <label>Title <input id="rr-title" type="text" maxlength="120" value="${esc(clip.title || "")}"></label>
    </div>
    <div class="cutbar"><button class="btn primary small" id="rr-go">♻ Re-render</button></div>`);
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
          captions: $("#rr-captions").value,
          captions_pos: $("#rr-cappos").value,
          captions_box: $("#rr-capbox").checked,
          speed: +$("#rr-speed").value,
          progress: $("#rr-progress").checked,
          silence: $("#rr-silence").checked,
          loud: $("#rr-loud").checked,
          title: $("#rr-title").value,
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
    const filename = match ? decodeURIComponent(match[1].replace(/"/g, "")) : "autoshorts.zip";
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
    btn.textContent = "⚡ Process-all";
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
        <button class="btn small iconbtn" data-close="1" aria-label="Close">✕</button>
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
  openModal("🔍 Search transcripts", `
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
    box.innerHTML = `<div class="stepmsg error">⚠ ${esc(error.message)}</div>`;
  }
}

function jumpToHit(epId, start) {
  closeModal();
  state.cutter = { epId, segments: [], start: null, end: null };
  openCutter(epId, Math.max(0, start - 10));
}

// ---------------------------------------------------------------- chapters
async function openChapters(epId) {
  openModal("📑 Chapters", `<div class="stepmsg"><span class="spinner"></span>Scoring story moments…</div>`, true);
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
        <button class="btn primary small" id="chapterCopy">📋 Copy chapters</button>
      </div>`;
    $("#chapterCopy").addEventListener("click", async () => {
      await copyText(data.text || "");
      toast("Chapters copied");
    });
  } catch (error) {
    modalBody().innerHTML = `<div class="stepmsg error">⚠ ${esc(error.message)}</div>`;
  }
}

// ---------------------------------------------------------------- cutter
async function openCutter(epId, scrollStart = null) {
  state.cutter = { epId, segments: [], start: null, end: null };
  openModal("📜 Transcript cutter", `<div class="stepmsg"><span class="spinner"></span>Loading transcript…</div>`, true);
  try {
    const data = await api(`/api/episodes/${epId}/transcript`);
    state.cutter.segments = data.segments || [];
    renderCutter(scrollStart);
  } catch (error) {
    modalBody().innerHTML = `<div class="stepmsg error">⚠ ${esc(error.message)}</div>`;
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
      <button class="btn primary small" onclick="cutFromCutter()">✂ Cut range</button>
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
  openModal("🛠 Dashboard", `<div class="stepmsg"><span class="spinner"></span>Loading storage &amp; jobs…</div>`, true);
  try {
    const [storage, jobs, health] = await Promise.all([
      api("/api/storage"), api("/api/jobs?limit=200"), api("/api/health"),
    ]);
    state.storage = storage;
    state.jobs = jobs.jobs || [];
    state.health = health;
    modalBody().innerHTML = dashboardHtml();
  } catch (error) {
    modalBody().innerHTML = `<div class="stepmsg error">⚠ ${esc(error.message)}</div>`;
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
      <span>✅ ${done} done</span> · <span>❌ ${failed} failed</span> ·
      <span>⏳ ${active} active</span>${cancelled ? ` · <span>🚫 ${cancelled} cancelled</span>` : ""}
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
          <span class="sname">${name}</span>
          <span class="smeta">${dirs[name]?.files ?? 0} files · ${fmtBytes(dirs[name]?.bytes)}</span>
          ${cleanTargets.includes(name)
            ? `<button class="btn small" onclick="cleanStorage('${name}', this)">🧹 Clean</button>`
            : `<span class="count-pill">kept for resumes</span>`}
        </div>`).join("")}
    </div>
    <div class="cutbar">
      <span class="count-pill">clips: ${storage.counts?.clips ?? 0} · episodes: ${storage.counts?.episodes ?? 0}</span>
    </div>

    <div class="minihead">Backup &amp; restore</div>
    <div class="cutbar">
      <a class="btn small" href="/api/backup" download>⬇ Download backup</a>
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
            ? `<button class="btn small" title="Cancel before it starts" onclick="cancelJob('${job.id}')">✕</button>`
            : (job.status === "done" || job.status === "error")
              ? `<button class="btn small" onclick="retryJob('${job.id}')">↻ Retry</button>`
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

  $("#playlistUrl").addEventListener("keydown", (event) => {
    if (event.key === "Enter") loadPlaylist();
  });

  await refresh();
  if (!$("#playlistUrl").value && baseDefaults.playlist_url) {
    $("#playlistUrl").value = baseDefaults.playlist_url;
  }
  if ((state.data?.jobs || []).length) fastPoll();
  else pollTimer = setInterval(refresh, 8000);
});

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
