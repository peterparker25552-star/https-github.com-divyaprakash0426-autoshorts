/* AutoShorts UI — vanilla JS, polls /api/state. */
const $ = (sel) => document.querySelector(sel);

const state = { data: null, health: null };
let pollTimer = null;

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

// ---------------------------------------------------------------- render
function renderHealth(h) {
  state.health = h;
  const yt = h.youtube_reachable;
  $("#health").innerHTML = `
    <span class="chip ${yt ? "ok" : "bad"}"><span class="dot"></span>YouTube ${yt ? "reachable" : "unreachable — demo mode"}</span>
    <span class="chip"><span class="dot" style="background:var(--ok)"></span>ffmpeg ${h.ffmpeg ? "ready" : "missing"}</span>`;
}

function renderEpisodes() {
  const eps = state.data?.episodes || [];
  $("#episodeCount").textContent = eps.length ? `${eps.length} loaded` : "";
  const list = $("#episodeList");

  if (!eps.length) {
    list.innerHTML = `<div class="empty" style="padding:34px 16px">
      <p>Load a YouTube playlist above, or <a href="#" onclick="loadDemo();return false">try the demo</a>.</p>
    </div>`;
    return;
  }

  list.innerHTML = eps.map((ep) => {
    const status = ep.status || "new";
    const job = (state.data.jobs || []).find((j) => j.episode_id === ep.id);
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
        <select class="opt-count" ${busy ? "disabled" : ""}>
          ${[3, 5, 8].map((n) => `<option value="${n}" ${n === defaults.count ? "selected" : ""}>${n} shorts</option>`).join("")}
        </select>
        <select class="opt-style" ${busy ? "disabled" : ""}>
          <option value="blur" ${defaults.style === "blur" ? "selected" : ""}>blur bg</option>
          <option value="crop" ${defaults.style === "crop" ? "selected" : ""}>center crop</option>
        </select>
        <select class="opt-quality" ${busy ? "disabled" : ""}>
          <option value="fast" ${defaults.quality === "fast" ? "selected" : ""}>720×1280</option>
          <option value="full" ${defaults.quality === "full" ? "selected" : ""}>1080×1920</option>
        </select>
        <button class="btn primary small" onclick="generate('${ep.id}', this)" ${busy ? "disabled" : ""}>
          ${busy ? "Working…" : "Generate"}
        </button>
      </div>
    </div>`;
  }).join("");
}

function renderShorts() {
  const clips = state.data?.clips || [];
  $("#clipCount").textContent = clips.length ? `${clips.length} clips` : "";
  $("#emptyShorts").style.display = clips.length ? "none" : "";
  const maxScore = Math.max(...clips.map((c) => c.score), 1);

  $("#shortsGrid").innerHTML = clips.map((c) => `
    <div class="clip" data-id="${c.id}">
      <video controls preload="metadata" playsinline
             ${c.thumb ? `poster="/api/clips/${c.id}/thumb"` : ""}
             src="/api/clips/${c.id}/file"></video>
      <div class="body">
        <div class="clip-title">${esc(c.title)}</div>
        <div class="sub">From: <span title="${esc(c.episode_title)}">${esc(trim(c.episode_title, 42))}</span></div>
        <div class="sub timechip">⏱ ${fmtRange(c.start, c.end)} · ${Math.round(c.duration)}s · ${c.height || 1280}p</div>
        <div class="scorebox">
          <div class="scorebar"><div class="fill" style="width:${Math.round((c.score / maxScore) * 100)}%"></div></div>
          <div class="scorenum">${c.score.toFixed(1)}</div>
        </div>
        <div class="reasons">${(c.reasons || []).map((r) => `<span class="reason">${esc(r)}</span>`).join("")}</div>
        <div class="actions">
          <a class="btn small" href="/api/clips/${c.id}/file" download="autoshort-${c.id}.mp4">⬇ Download</a>
          <button class="btn small" onclick="deleteClip('${c.id}')">🗑</button>
        </div>
      </div>
    </div>`).join("");
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
const trim = (s, n) => (s && s.length > n ? s.slice(0, n - 1) + "…" : s);

// ---------------------------------------------------------------- actions
async function refresh() {
  try {
    const [st, h] = await Promise.all([api("/api/state"), api("/api/health")]);
    state.data = st;
    defaults = { ...defaults, ...(st.defaults || {}) };
    renderHealth(h);
    renderEpisodes();
    renderShorts();
  } catch (e) {
    console.error(e);
  }
}

async function loadPlaylist() {
  const url = $("#playlistUrl").value.trim();
  if (!url) return toast("Paste a YouTube playlist URL first", true);
  const btn = $("#loadPlaylist");
  btn.disabled = true;
  btn.textContent = "Loading…";
  try {
    const r = await api("/api/playlist", {
      method: "POST",
      body: JSON.stringify({ url, limit: 25 }),
    });
    toast(`Added ${r.added} episodes`);
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = "Load playlist";
    refresh();
  }
}

async function loadDemo() {
  try {
    const r = await api("/api/demo/load", { method: "POST" });
    toast(`Demo playlist loaded (${r.added} episodes) — hit Generate!`);
  } catch (e) {
    toast(e.message, true);
  }
  refresh();
}

async function generate(epId, btn) {
  const card = btn.closest(".episode");
  const params = {
    count: +card.querySelector(".opt-count").value,
    min_dur: defaults.min_dur,
    max_dur: defaults.max_dur,
    style: card.querySelector(".opt-style").value,
    quality: card.querySelector(".opt-quality").value,
  };
  try {
    await api(`/api/episodes/${epId}/shorts`, {
      method: "POST",
      body: JSON.stringify(params),
    });
    toast("Generating — watch the progress bar");
    fastPoll();
  } catch (e) {
    toast(e.message, true);
  }
  refresh();
}

async function deleteClip(id) {
  try {
    await api(`/api/clips/${id}`, { method: "DELETE" });
  } catch (e) {
    toast(e.message, true);
  }
  refresh();
}

// ---------------------------------------------------------------- polling
let defaults = { count: 5, min_dur: 20, max_dur: 60, style: "blur", quality: "fast", playlist_url: "" };

function fastPoll() {
  clearInterval(pollTimer);
  pollTimer = setInterval(() => {
    const busy = (state.data?.jobs || []).length > 0;
    refresh();
    if (!busy) { clearInterval(pollTimer); pollTimer = setInterval(refresh, 8000); }
  }, 1500);
}

// ---------------------------------------------------------------- init
window.addEventListener("DOMContentLoaded", async () => {
  $("#loadPlaylist").addEventListener("click", loadPlaylist);
  $("#loadDemo").addEventListener("click", loadDemo);
  $("#demoLink").addEventListener("click", (e) => { e.preventDefault(); loadDemo(); });

  await refresh();
  // prefill the playlist box with the seed playlist
  if (!$("#playlistUrl").value && defaults.playlist_url) {
    $("#playlistUrl").value = defaults.playlist_url;
  }
  if ((state.data?.jobs || []).length) fastPoll();
  else pollTimer = setInterval(refresh, 8000);
});

window.generate = generate;
window.deleteClip = deleteClip;
window.loadDemo = loadDemo;
