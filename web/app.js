/* AutoShorts UI — vanilla JS, polls /api/state. */
const $ = (sel) => document.querySelector(sel);

const LENGTH_PRESETS = {
  short: [20, 30],
  medium: [30, 45],
  long: [45, 60],
  any: [20, 60],
};

const state = {
  data: null,
  health: null,
  previews: {},
  manualOpen: {},
};
let pollTimer = null;
let defaults = {
  count: 5,
  min_dur: 20,
  max_dur: 60,
  profile: "viral",
  style: "blur",
  quality: "fast",
  playlist_url: "",
};

// ---------------------------------------------------------------- helpers
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

function selectedRange(card) {
  return LENGTH_PRESETS[card.querySelector(".opt-length").value] || LENGTH_PRESETS.any;
}

// ---------------------------------------------------------------- render
function renderHealth(h) {
  state.health = h;
  const yt = h.youtube_reachable;
  $("#health").innerHTML = `
    <span class="chip ${yt ? "ok" : "bad"}"><span class="dot"></span>YouTube ${yt ? "reachable" : "unreachable — demo mode"}</span>
    <span class="chip"><span class="dot" style="background:var(--ok)"></span>ffmpeg ${h.ffmpeg ? "ready" : "missing"}</span>`;
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

function renderMoments(epId) {
  const moments = state.previews[epId];
  if (!moments) return "";
  if (!moments.length) {
    return `<div class="moments"><div class="stepmsg">No picks found for this length. Try “Any”.</div></div>`;
  }
  return `<div class="moments">
    <div class="minihead">Preview picks — nothing downloaded or rendered</div>
    ${moments.map((moment) => `
      <div class="moment">
        <div class="moment-main">
          <div class="moment-title">${esc(moment.title)}</div>
          <div class="reasons">${(moment.reasons || []).map((reason) => `<span class="reason">${esc(reason)}</span>`).join("")}</div>
        </div>
        <span class="moment-time">${fmtRange(moment.start, moment.end)}</span>
        <span class="moment-score">${Number(moment.score).toFixed(1)}</span>
      </div>`).join("")}
  </div>`;
}

function renderManualBox(ep) {
  if (!state.manualOpen[ep.id]) return "";
  return `<div class="manualbox">
    <div class="minihead">Cut an exact range (5–180 seconds)</div>
    <div class="manual-row">
      <label>Start <input class="manual-start" type="number" min="0" step="0.1" value="10"></label>
      <label>End <input class="manual-end" type="number" min="5" step="0.1" value="40"></label>
      <input class="manual-title" type="text" maxlength="120" placeholder="Optional title">
      <button class="btn primary small" onclick="cutManual('${ep.id}', this)">Cut this range</button>
    </div>
  </div>`;
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
      <p>Load a YouTube playlist above, or <a href="#" onclick="loadDemo();return false">try the demo</a>.</p>
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
        <select class="opt-count" ${busy ? "disabled" : ""} aria-label="Number of shorts">
          ${[3, 5, 8].map((n) => `<option value="${n}" ${n === defaults.count ? "selected" : ""}>${n} shorts</option>`).join("")}
        </select>
        <select class="opt-profile" ${busy ? "disabled" : ""} aria-label="Highlight profile">
          <option value="viral" ${defaults.profile === "viral" ? "selected" : ""}>🔥 Viral</option>
          <option value="story" ${defaults.profile === "story" ? "selected" : ""}>📖 Story</option>
          <option value="facts" ${defaults.profile === "facts" ? "selected" : ""}>📊 Facts</option>
          <option value="energy" ${defaults.profile === "energy" ? "selected" : ""}>⚡ Energy</option>
        </select>
        <select class="opt-length" ${busy ? "disabled" : ""} aria-label="Clip length">
          <option value="short">Short · 20–30s</option>
          <option value="medium">Medium · 30–45s</option>
          <option value="long">Long · 45–60s</option>
          <option value="any" selected>Any · 20–60s</option>
        </select>
        <select class="opt-style" ${busy ? "disabled" : ""} aria-label="Framing style">
          <option value="blur" ${defaults.style === "blur" ? "selected" : ""}>blur bg</option>
          <option value="crop" ${defaults.style === "crop" ? "selected" : ""}>center crop</option>
        </select>
        <select class="opt-quality" ${busy ? "disabled" : ""} aria-label="Quality">
          <option value="fast" ${defaults.quality === "fast" ? "selected" : ""}>720×1280</option>
          <option value="full" ${defaults.quality === "full" ? "selected" : ""}>1080×1920</option>
        </select>
        <button class="btn primary small" onclick="generate('${ep.id}', this)" ${busy ? "disabled" : ""}>
          ${busy ? "Working…" : "Generate"}
        </button>
        <button class="btn small" onclick="previewPicks('${ep.id}', this)" ${busy ? "disabled" : ""}>👁 Preview picks</button>
        <button class="btn small" onclick="toggleManual('${ep.id}')" ${busy ? "disabled" : ""}>✂ Manual clip</button>
      </div>
      ${renderMoments(ep.id)}
      ${renderManualBox(ep)}
    </div>`;
  }).join("");
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

  $("#shortsGrid").innerHTML = clips.map((clip) => `
    <div class="clip" data-id="${clip.id}">
      <video controls preload="metadata" playsinline
             ${clip.thumb ? `poster="/api/clips/${clip.id}/thumb"` : ""}
             src="/api/clips/${clip.id}/file"></video>
      <div class="body">
        <div class="clip-title">${esc(clip.title)}</div>
        <div class="sub">From: <span title="${esc(clip.episode_title)}">${esc(trim(clip.episode_title, 42))}</span></div>
        <div class="sub timechip">⏱ ${fmtRange(clip.start, clip.end)} · ${Number(clip.duration).toFixed(1)}s · ${clip.height || 1280}p</div>
        <div class="scorebox">
          <div class="scorebar"><div class="fill" style="width:${Math.round((Number(clip.score || 0) / maxScore) * 100)}%"></div></div>
          <div class="scorenum">${Number(clip.score || 0).toFixed(1)}</div>
        </div>
        <div class="reasons">${(clip.reasons || []).map((reason) => `<span class="reason">${esc(reason)}</span>`).join("")}</div>
        <div class="actions">
          <a class="btn small" href="/api/clips/${clip.id}/file" download="autoshort-${clip.id}.mp4">⬇ Download</a>
          <button class="btn small" onclick="shareClip('${clip.id}')">📤 Share</button>
          <button class="btn small iconbtn" aria-label="Delete clip" onclick="deleteClip('${clip.id}')">🗑</button>
        </div>
      </div>
    </div>`).join("");
}

// ---------------------------------------------------------------- actions
async function refresh() {
  try {
    const [st, health] = await Promise.all([api("/api/state"), api("/api/health")]);
    state.data = st;
    defaults = { ...defaults, ...(st.defaults || {}) };
    renderHealth(health);
    renderStats();
    renderEpisodes();
    renderShorts();
  } catch (error) {
    console.error(error);
  }
}

async function loadPlaylist() {
  const url = $("#playlistUrl").value.trim();
  if (!url) return toast("Paste a YouTube playlist URL first", true);
  const btn = $("#loadPlaylist");
  btn.disabled = true;
  btn.textContent = "Loading…";
  try {
    const result = await api("/api/playlist", {
      method: "POST",
      body: JSON.stringify({ url, limit: 25 }),
    });
    toast(`Added ${result.added} episodes`);
  } catch (error) {
    toast(error.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = "Load playlist";
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

async function generate(epId, btn) {
  const card = btn.closest(".episode");
  const [minDur, maxDur] = selectedRange(card);
  const params = {
    count: +card.querySelector(".opt-count").value,
    min_dur: minDur,
    max_dur: maxDur,
    profile: card.querySelector(".opt-profile").value,
    style: card.querySelector(".opt-style").value,
    quality: card.querySelector(".opt-quality").value,
  };
  try {
    await api(`/api/episodes/${epId}/shorts`, {
      method: "POST",
      body: JSON.stringify(params),
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
  const [minDur, maxDur] = selectedRange(card);
  btn.disabled = true;
  btn.textContent = "Finding…";
  try {
    const result = await api(`/api/episodes/${epId}/preview`, {
      method: "POST",
      body: JSON.stringify({
        count: +card.querySelector(".opt-count").value,
        min_dur: minDur,
        max_dur: maxDur,
        profile: card.querySelector(".opt-profile").value,
      }),
    });
    state.previews[epId] = result.moments;
    renderEpisodes();
  } catch (error) {
    toast(error.message, true);
    btn.disabled = false;
    btn.textContent = "👁 Preview picks";
  }
}

function toggleManual(epId) {
  state.manualOpen[epId] = !state.manualOpen[epId];
  renderEpisodes();
}

async function cutManual(epId, btn) {
  const card = btn.closest(".episode");
  const start = +card.querySelector(".manual-start").value;
  const end = +card.querySelector(".manual-end").value;
  if (!Number.isFinite(start) || !Number.isFinite(end) || end - start < 5) {
    return toast("Manual clips must be at least 5 seconds", true);
  }
  btn.disabled = true;
  try {
    await api(`/api/episodes/${epId}/manual`, {
      method: "POST",
      body: JSON.stringify({
        start,
        end,
        title: card.querySelector(".manual-title").value.trim(),
        style: card.querySelector(".opt-style").value,
        quality: card.querySelector(".opt-quality").value,
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
  } catch (error) {
    toast(error.message, true);
  }
  refresh();
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
    const filename = match ? decodeURIComponent(match[1].replace(/\"/g, "")) : "autoshorts.zip";
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

// ---------------------------------------------------------------- init
window.addEventListener("DOMContentLoaded", async () => {
  $("#loadPlaylist").addEventListener("click", loadPlaylist);
  $("#loadDemo").addEventListener("click", loadDemo);
  $("#demoLink").addEventListener("click", (event) => {
    event.preventDefault();
    loadDemo();
  });
  $("#episodeSearch").addEventListener("input", renderEpisodes);
  $("#clipSearch").addEventListener("input", renderShorts);
  $("#clipSort").addEventListener("change", renderShorts);
  $("#zipBtn").addEventListener("click", downloadZip);

  await refresh();
  if (!$("#playlistUrl").value && defaults.playlist_url) {
    $("#playlistUrl").value = defaults.playlist_url;
  }
  if ((state.data?.jobs || []).length) fastPoll();
  else pollTimer = setInterval(refresh, 8000);
});

window.generate = generate;
window.previewPicks = previewPicks;
window.toggleManual = toggleManual;
window.cutManual = cutManual;
window.deleteClip = deleteClip;
window.shareClip = shareClip;
window.loadDemo = loadDemo;
