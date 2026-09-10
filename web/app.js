/* AutoShorts UI v0.4.0 — vanilla JS, polls /api/state. */
const $ = (sel) => document.querySelector(sel);

const state = {
  data: null,
  health: null,
  previews: {},
  cardOpts: {},   // per-card memory: epId -> opts
  rerenderOpen: {},
};
let pollTimer = null;
let defaults = {
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

function cardOpts(epId) {
  if (!state.cardOpts[epId]) {
    state.cardOpts[epId] = {
      count: defaults.count,
      min_dur: defaults.min_dur,
      max_dur: defaults.max_dur,
      profile: defaults.profile,
      style: defaults.style,
      quality: defaults.quality,
      format: defaults.format,
      captions: defaults.captions,
      captions_pos: defaults.captions_pos,
      captions_box: defaults.captions_box,
      speed: defaults.speed,
      progress: defaults.progress,
      silence: defaults.silence,
      loud: defaults.loud,
    };
  }
  return state.cardOpts[epId];
}

function readCard(card) {
  const num = (sel, fb) => {
    const v = parseFloat(card.querySelector(sel)?.value);
    return Number.isFinite(v) ? v : fb;
  };
  const chk = (sel) => !!card.querySelector(sel)?.checked;
  return {
    count: Math.max(1, Math.min(12, parseInt(card.querySelector(".opt-count")?.value, 10) || defaults.count)),
    min_dur: num(".opt-min", defaults.min_dur),
    max_dur: num(".opt-max", defaults.max_dur),
    profile: card.querySelector(".opt-profile")?.value || "viral",
    style: card.querySelector(".opt-style")?.value || "blur",
    quality: card.querySelector(".opt-quality")?.value || "fast",
    format: card.querySelector(".opt-format")?.value || "vertical",
    captions: card.querySelector(".opt-captions")?.value || "classic",
    captions_pos: card.querySelector(".opt-cappos")?.value || "standard",
    captions_box: chk(".opt-box"),
    speed: num(".opt-speed", 1.0),
    progress: chk(".opt-progress"),
    silence: chk(".opt-silence"),
    loud: chk(".opt-loud"),
  };
}

function saveCard(epId, card) {
  state.cardOpts[epId] = readCard(card);
}

// ---------------------------------------------------------------- render
function renderHealth(h) {
  state.health = h;
  const yt = h.youtube_reachable;
  $("#health").innerHTML = `
    <span class="chip ${yt ? "ok" : "bad"}"><span class="dot"></span>YouTube ${yt ? "reachable" : "unreachable — demo mode"}</span>
    <span class="chip"><span class="dot" style="background:var(--ok)"></span>ffmpeg ${h.ffmpeg ? "ready" : "missing"}</span>
    <span class="chip">v${esc(h.version || "")}</span>`;
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
    return `<div class="moments"><div class="stepmsg">No picks found. Try wider durations.</div></div>`;
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

function countOptions(selected) {
  // Episode-card defaults: count renders via String(defaults.count).
  const sel = String(selected ?? String(defaults.count));
  let out = "";
  for (let n = 1; n <= 12; n++) {
    out += `<option value="${n}" ${String(n) === sel ? "selected" : ""}>${n} short${n > 1 ? "s" : ""}</option>`;
  }
  return out;
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
    const job = (state.data.jobs || []).find((item) => item.episode_id === ep.id && (item.status === "queued" || item.status === "running"));
    const busy = status === "processing" || !!job;
    const pct = job ? Math.round((job.progress || 0) * 100) : 0;
    const o = cardOpts(ep.id);
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
        <div class="stepmsg"><span class="spinner"></span>${esc(job?.message || job?.step || "working…")}</div>` : ""}
      ${ep.error ? `<div class="stepmsg error">⚠ ${esc(ep.error)}</div>` : ""}
      <div class="finetune" data-ep="${ep.id}">
        <label>Count<select class="opt-count" ${busy ? "disabled" : ""}>${countOptions(o.count)}</select></label>
        <label>Min(s)<input class="opt-min" type="number" min="5" max="180" step="1" value="${esc(o.min_dur)}" ${busy ? "disabled" : ""}></label>
        <label>Max(s)<input class="opt-max" type="number" min="5" max="180" step="1" value="${esc(o.max_dur)}" ${busy ? "disabled" : ""}></label>
        <label>Profile<select class="opt-profile" ${busy ? "disabled" : ""}>
          <option value="viral" ${o.profile === "viral" ? "selected" : ""}>🔥 Viral</option>
          <option value="story" ${o.profile === "story" ? "selected" : ""}>📖 Story</option>
          <option value="facts" ${o.profile === "facts" ? "selected" : ""}>📊 Facts</option>
          <option value="energy" ${o.profile === "energy" ? "selected" : ""}>⚡ Energy</option>
        </select></label>
        <label>Style<select class="opt-style" ${busy ? "disabled" : ""}>
          <option value="blur" ${o.style === "blur" ? "selected" : ""}>blur bg</option>
          <option value="crop" ${o.style === "crop" ? "selected" : ""}>center crop</option>
          <option value="fill" ${o.style === "fill" ? "selected" : ""}>fill</option>
          <option value="fit" ${o.style === "fit" ? "selected" : ""}>fit</option>
          <option value="smart" ${o.style === "smart" ? "selected" : ""}>🎥 Smart</option>
        </select></label>
        <label>Quality<select class="opt-quality" ${busy ? "disabled" : ""}>
          <option value="fast" ${o.quality === "fast" ? "selected" : ""}>fast</option>
          <option value="full" ${o.quality === "full" ? "selected" : ""}>full</option>
        </select></label>
        <label>Format<select class="opt-format" ${busy ? "disabled" : ""}>
          <option value="vertical" ${o.format === "vertical" ? "selected" : ""}>9:16</option>
          <option value="square" ${o.format === "square" ? "selected" : ""}>1:1</option>
          <option value="wide" ${o.format === "wide" ? "selected" : ""}>16:9</option>
        </select></label>
        <label>Captions<select class="opt-captions" ${busy ? "disabled" : ""}>
          <option value="classic" ${o.captions === "classic" ? "selected" : ""}>classic</option>
          <option value="pop" ${o.captions === "pop" ? "selected" : ""}>pop</option>
          <option value="minimal" ${o.captions === "minimal" ? "selected" : ""}>minimal</option>
        </select></label>
        <label>Cap-pos<select class="opt-cappos" ${busy ? "disabled" : ""}>
          <option value="standard" ${o.captions_pos === "standard" ? "selected" : ""}>standard</option>
          <option value="low" ${o.captions_pos === "low" ? "selected" : ""}>low</option>
        </select></label>
        <label>Speed<input class="opt-speed" type="number" min="0.5" max="2" step="0.1" value="${esc(o.speed)}" ${busy ? "disabled" : ""}></label>
        <label class="toggle"><input class="opt-box" type="checkbox" ${o.captions_box ? "checked" : ""} ${busy ? "disabled" : ""}>📦 box</label>
        <label class="toggle"><input class="opt-loud" type="checkbox" ${o.loud ? "checked" : ""} ${busy ? "disabled" : ""}>🔊 loud</label>
        <label class="toggle"><input class="opt-progress" type="checkbox" ${o.progress ? "checked" : ""} ${busy ? "disabled" : ""}>📈 progress</label>
        <label class="toggle"><input class="opt-silence" type="checkbox" ${o.silence ? "checked" : ""} ${busy ? "disabled" : ""}>✂ silence</label>
      </div>
      <div class="controls">
        <button class="btn primary small" onclick="generate('${ep.id}', this)" ${busy ? "disabled" : ""}>
          ${busy ? "Working…" : "Generate"}
        </button>
        <button class="btn small" onclick="previewPicks('${ep.id}', this)" ${busy ? "disabled" : ""}>👁 Preview</button>
      </div>
      <div class="subrow">
        <button class="btn small ghostbtn" onclick="openTranscript('${ep.id}')">📜 Transcript cutter</button>
        <button class="btn small ghostbtn" onclick="openChapters('${ep.id}')">📑 Chapters</button>
        <button class="btn small ghostbtn" onclick="exportCSV('${ep.id}')">📊 CSV</button>
        <button class="btn small ghostbtn danger" onclick="deleteEpisode('${ep.id}')">🗑</button>
      </div>
      ${renderMoments(ep.id)}
    </div>`;
  }).join("");
  // per-card memory: persist on change
  list.querySelectorAll(".finetune").forEach((grid) => {
    grid.addEventListener("change", () => {
      const card = grid.closest(".episode");
      saveCard(grid.dataset.ep, card);
    });
  });
}

function breakdownBars(breakdown) {
  if (!breakdown || typeof breakdown !== "object") return "";
  const keys = ["hook", "numbers", "questions", "emotion", "superlative", "wps"];
  const max = Math.max(...keys.map((k) => Number(breakdown[k] || 0)), 0.001);
  return `<div class="breakdown">${keys.map((k) => {
    const v = Number(breakdown[k] || 0);
    const pct = Math.round((v / max) * 100);
    return `<div class="bd-row"><span class="bd-key">${esc(k)}</span><div class="bd-bar"><div class="fill" style="width:${pct}%"></div></div><span class="bd-val">${Number.isInteger(v) ? v : v.toFixed(1)}</span></div>`;
  }).join("")}</div>`;
}

function waveformStrip(waveform) {
  if (!Array.isArray(waveform) || !waveform.length) return "";
  return `<div class="waveform" aria-hidden="true">${waveform.map((v) => {
    const h = Math.max(2, Math.round(Number(v || 0) * 18));
    return `<span style="height:${h}px"></span>`;
  }).join("")}</div>`;
}

function rerenderBox(clip) {
  if (!state.rerenderOpen[clip.id]) return "";
  const r = clip.render || {};
  const sel = (key, opts, labels) => `<select class="rr-${key}">${opts.map((v, i) => `<option value="${v}" ${String(r[key] ?? defaults[key]) === v ? "selected" : ""}>${labels[i]}</option>`).join("")}</select>`;
  return `<div class="rerender">
    <div class="minihead">Rerender overrides</div>
    <div class="rr-grid">
      <label>Style${sel("style", ["blur", "crop", "fill", "fit", "smart"], ["blur", "crop", "fill", "fit", "🎥 Smart"])}</label>
      <label>Quality${sel("quality", ["fast", "full"], ["fast", "full"])}</label>
      <label>Format${sel("format", ["vertical", "square", "wide"], ["9:16", "1:1", "16:9"])}</label>
      <label>Caps${sel("captions", ["classic", "pop", "minimal"], ["classic", "pop", "minimal"])}</label>
      <label>Pos${sel("captions_pos", ["standard", "low"], ["standard", "low"])}</label>
      <label>Speed<input class="rr-speed" type="number" min="0.5" max="2" step="0.1" value="${esc(r.speed ?? 1.0)}"></label>
      <label class="toggle"><input class="rr-box" type="checkbox" ${r.captions_box ? "checked" : ""}>📦</label>
      <label class="toggle"><input class="rr-progress" type="checkbox" ${r.progress ? "checked" : ""}>📈</label>
      <label class="toggle"><input class="rr-silence" type="checkbox" ${r.silence ? "checked" : ""}>✂</label>
      <label class="toggle"><input class="rr-loud" type="checkbox" ${r.loud ? "checked" : ""}>🔊</label>
    </div>
    <input class="rr-title" type="text" maxlength="120" placeholder="Optional new title" value="">
    <button class="btn primary small" onclick="submitRerender('${clip.id}', this)">Rerender</button>
  </div>`;
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
        ${breakdownBars(clip.breakdown)}
        ${waveformStrip(clip.waveform)}
        <div class="actions">
          <a class="btn small" href="/api/clips/${clip.id}/file" download="autoshort-${clip.id}.mp4">⬇ Download</a>
          <a class="btn small" href="/api/clips/${clip.id}/srt" download="autoshort-${clip.id}.srt">SRT</a>
          ${clip.thumb ? `<a class="btn small" href="/api/clips/${clip.id}/thumb" download="autoshort-${clip.id}.jpg">🖼 Thumb</a>` : ""}
        </div>
        <div class="actions">
          <button class="btn small" onclick="shareClip('${clip.id}')">📤 Share</button>
          <button class="btn small" onclick="renameClip('${clip.id}')">✏️ Rename</button>
          <button class="btn small" onclick="toggleRerender('${clip.id}')">🎛 Rerender</button>
          <button class="btn small iconbtn" aria-label="Delete clip" onclick="deleteClip('${clip.id}')">🗑</button>
        </div>
        ${rerenderBox(clip)}
      </div>
    </div>`).join("");
}

// ---------------------------------------------------------------- modal
function openModal(title, html) {
  $("#modalTitle").textContent = title;
  $("#modalBody").innerHTML = html;
  $("#modalOverlay").classList.remove("hidden");
}
function closeModal() {
  $("#modalOverlay").classList.add("hidden");
  $("#modalBody").innerHTML = "";
}

async function openTranscript(epId) {
  openModal("📜 Transcript cutter", `<div class="stepmsg"><span class="spinner"></span>Loading transcript…</div>`);
  try {
    const data = await api(`/api/episodes/${epId}/transcript`);
    const segs = data.segments || [];
    if (!segs.length) {
      $("#modalBody").innerHTML = `<div class="stepmsg">No transcript available for this episode.</div>`;
      return;
    }
    $("#modalBody").innerHTML = `
      <div class="manual-row" style="margin-bottom:10px">
        <label>Start <input id="cutStart" type="number" min="0" step="0.5" value="${segs[0].start.toFixed(1)}"></label>
        <label>End <input id="cutEnd" type="number" min="5" step="0.5" value="${Math.min(segs[0].start + 30, segs[segs.length - 1].end).toFixed(1)}"></label>
        <input id="cutTitle" type="text" maxlength="120" placeholder="Optional title" style="flex:1;min-width:120px">
        <button class="btn primary small" onclick="cutFromModal('${epId}')">Cut this range</button>
      </div>
      <div class="stepmsg">Click a line to set start → click another to set end.</div>
      <div class="seglist">${segs.map((s, i) => `
        <div class="seg" data-i="${i}" data-start="${s.start}" data-end="${s.end}" onclick="segClick(this)">
          <span class="seg-t">${fmtRange(s.start, s.end)}</span><span>${esc(s.text)}</span>
        </div>`).join("")}</div>`;
  } catch (error) {
    $("#modalBody").innerHTML = `<div class="stepmsg error">⚠ ${esc(error.message)}</div>`;
  }
}

let segAnchor = null;
function segClick(el) {
  const s = parseFloat(el.dataset.start), e = parseFloat(el.dataset.end);
  const startInput = $("#cutStart"), endInput = $("#cutEnd");
  if (segAnchor === null || segAnchor === undefined) {
    segAnchor = s;
    if (startInput) startInput.value = s.toFixed(1);
    el.classList.add("picked");
  } else {
    const a = Math.min(segAnchor, e), b = Math.max(segAnchor, e);
    if (startInput) startInput.value = a.toFixed(1);
    if (endInput) endInput.value = b.toFixed(1);
    segAnchor = null;
    document.querySelectorAll(".seg.picked").forEach((n) => n.classList.remove("picked"));
  }
}

async function cutFromModal(epId) {
  const start = parseFloat($("#cutStart")?.value), end = parseFloat($("#cutEnd")?.value);
  const title = ($("#cutTitle")?.value || "").trim();
  if (!Number.isFinite(start) || !Number.isFinite(end) || end - start < 5) {
    return toast("Manual clips must be at least 5 seconds", true);
  }
  try {
    const o = cardOpts(epId);
    await api(`/api/episodes/${epId}/manual`, {
      method: "POST",
      body: JSON.stringify({ start, end, title, ...o }),
    });
    toast("Manual clip queued");
    closeModal();
    fastPoll();
    refresh();
  } catch (error) {
    toast(error.message, true);
  }
}

async function openChapters(epId) {
  openModal("📑 Chapters", `<div class="stepmsg"><span class="spinner"></span>Loading chapters…</div>`);
  try {
    const data = await api(`/api/episodes/${epId}/chapters`);
    const chapters = data.chapters || [];
    $("#modalBody").innerHTML = chapters.length
      ? `<div class="chaplist">${chapters.map((c) => `
        <div class="chap"><span class="chap-t">${fmtDur(c.start)}</span><span>${esc(c.title)}</span></div>`).join("")}</div>`
      : `<div class="stepmsg">No chapters found.</div>`;
  } catch (error) {
    $("#modalBody").innerHTML = `<div class="stepmsg error">⚠ ${esc(error.message)}</div>`;
  }
}

async function exportCSV(epId) {
  try {
    const o = cardOpts(epId);
    const url = `/api/episodes/${epId}/export?count=${encodeURIComponent(o.count)}&profile=${encodeURIComponent(o.profile)}`;
    const res = await fetch(url);
    if (!res.ok) {
      let detail = "Export failed";
      try { detail = (await res.json()).detail || detail; } catch (e) {}
      throw new Error(detail);
    }
    const blob = await res.blob();
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    const disp = res.headers.get("content-disposition") || "";
    const m = disp.match(/filename="?([^";]+)"?/i);
    link.download = m ? m[1] : `autoshorts-${epId}-moments.csv`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(link.href);
  } catch (error) {
    toast(error.message, true);
  }
}

async function deleteEpisode(epId) {
  if (!confirm("Delete this episode and all its clips?")) return;
  try {
    const r = await api(`/api/episodes/${epId}`, { method: "DELETE" });
    toast(`Deleted episode (${r.clips_removed} clips removed)`);
  } catch (error) {
    toast(error.message, true);
  }
  refresh();
}

function openSearch() {
  openModal("🔍 Search transcripts", `
    <div class="stepmsg">Offline search over demo + cached transcripts.</div>
    <div class="manual-row" style="margin:10px 0">
      <input id="searchQ" type="search" placeholder="Type at least 2 characters…" style="flex:1;min-width:200px">
      <button class="btn primary small" onclick="runSearch()">Search</button>
    </div>
    <div id="searchResults"></div>`);
  const input = $("#searchQ");
  input?.addEventListener("keydown", (e) => { if (e.key === "Enter") runSearch(); });
  setTimeout(() => input?.focus(), 50);
}

async function runSearch() {
  const q = ($("#searchQ")?.value || "").trim();
  const box = $("#searchResults");
  if (q.length < 2) {
    box.innerHTML = `<div class="stepmsg error">Type at least 2 characters.</div>`;
    return;
  }
  box.innerHTML = `<div class="stepmsg"><span class="spinner"></span>Searching…</div>`;
  try {
    const data = await api(`/api/search?q=${encodeURIComponent(q)}`);
    const results = data.results || [];
    box.innerHTML = results.length
      ? `<div class="seglist">${results.map((r) => `
        <div class="seg" onclick="jumpToEpisode('${esc(r.episode_id)}')">
          <span class="seg-t">${fmtRange(r.start, r.end)}</span>
          <span><b>${esc(trim(r.episode_title, 48))}</b><br>${esc(r.text)}</span>
        </div>`).join("")}</div>`
      : `<div class="stepmsg">No matches.</div>`;
  } catch (error) {
    box.innerHTML = `<div class="stepmsg error">⚠ ${esc(error.message)}</div>`;
  }
}

function jumpToEpisode(epId) {
  closeModal();
  const card = document.querySelector(`.episode[data-id="${CSS.escape(epId)}"]`);
  if (card) {
    card.scrollIntoView({ behavior: "smooth", block: "center" });
    card.classList.add("flash");
    setTimeout(() => card.classList.remove("flash"), 1600);
  } else {
    toast("Episode is filtered out — clear the search", true);
  }
}

function openDashboard() {
  const jobs = state.data?.jobs || [];
  const done = jobs.filter((j) => j.status === "done").length;
  const failed = jobs.filter((j) => j.status === "error").length;
  const active = jobs.filter((j) => j.status === "queued" || j.status === "running").length;
  const cancelled = jobs.filter((j) => j.status === "cancelled").length;
  openModal("📊 Dashboard", `
    <div class="dashstats">✅ ${done} done · ❌ ${failed} failed · ⏳ ${active} active${cancelled ? ` · 🚫 ${cancelled} cancelled` : ""}</div>
    <div class="joblist">${jobs.length ? jobs.slice(0, 30).map((j) => `
      <div class="job">
        <div class="job-main">
          <div class="job-title">${esc(j.episode_id)} <span class="badge status-${j.status}">${esc(j.status)}</span></div>
          <div class="sub">${esc(j.message || j.step || "")}${j.error ? ` — ⚠ ${esc(j.error)}` : ""}</div>
        </div>
        ${(j.status === "done" || j.status === "error") ? `<button class="btn small" onclick="retryJob('${j.id}')">↻ Retry</button>` : ""}
        ${j.status === "queued" ? `<button class="btn small" onclick="cancelJob('${j.id}')">✕ Cancel</button>` : ""}
      </div>`).join("") : `<div class="stepmsg">No jobs yet.</div>`}
    </div>`);
}

async function retryJob(jobId) {
  try {
    await api(`/api/jobs/${jobId}/retry`, { method: "POST" });
    toast("Job retried");
    fastPoll();
  } catch (error) {
    toast(error.message, true);
  }
  refresh();
  openDashboard();
}

async function cancelJob(jobId) {
  try {
    await api(`/api/jobs/${jobId}/cancel`, { method: "POST" });
    toast("Job cancelled");
  } catch (error) {
    toast(error.message, true);
  }
  refresh();
  openDashboard();
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
  if (!url) return toast("Paste a YouTube playlist or video URL first", true);
  const btn = $("#loadPlaylist");
  btn.disabled = true;
  btn.textContent = "Loading…";
  try {
    const result = await api("/api/playlist", {
      method: "POST",
      body: JSON.stringify({ url, limit: 25 }),
    });
    toast(`Added ${result.added} episode(s)${result.auto_queued ? ` · auto-queued ${result.auto_queued}` : ""}`);
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

async function processAll() {
  const eps = (state.data?.episodes || []).filter((e) => e.status !== "processing");
  if (!eps.length) return toast("No idle episodes to process", true);
  try {
    const result = await api("/api/batch", {
      method: "POST",
      body: JSON.stringify({ episodes: eps.map((e) => e.id) }),
    });
    toast(`Queued ${result.queued} job(s)`);
    fastPoll();
  } catch (error) {
    toast(error.message, true);
  }
  refresh();
}

async function generate(epId, btn) {
  const card = btn.closest(".episode");
  saveCard(epId, card);
  const params = readCard(card);
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
  saveCard(epId, card);
  const params = readCard(card);
  btn.disabled = true;
  const orig = btn.textContent;
  btn.textContent = "Finding…";
  try {
    const result = await api(`/api/episodes/${epId}/preview`, {
      method: "POST",
      body: JSON.stringify(params),
    });
    state.previews[epId] = result.moments;
    renderEpisodes();
  } catch (error) {
    toast(error.message, true);
    btn.disabled = false;
    btn.textContent = orig;
  }
}

function toggleRerender(clipId) {
  state.rerenderOpen[clipId] = !state.rerenderOpen[clipId];
  renderShorts();
}

async function submitRerender(clipId, btn) {
  const box = btn.closest(".clip");
  const overrides = {
    style: box.querySelector(".rr-style")?.value,
    quality: box.querySelector(".rr-quality")?.value,
    format: box.querySelector(".rr-format")?.value,
    captions: box.querySelector(".rr-captions")?.value,
    captions_pos: box.querySelector(".rr-captions_pos")?.value,
    captions_box: box.querySelector(".rr-box")?.checked,
    speed: parseFloat(box.querySelector(".rr-speed")?.value),
    progress: box.querySelector(".rr-progress")?.checked,
    silence: box.querySelector(".rr-silence")?.checked,
    loud: box.querySelector(".rr-loud")?.checked,
  };
  const title = (box.querySelector(".rr-title")?.value || "").trim();
  if (title) overrides.title = title;
  Object.keys(overrides).forEach((k) => overrides[k] === undefined && delete overrides[k]);
  btn.disabled = true;
  try {
    await api(`/api/clips/${clipId}/rerender`, {
      method: "POST",
      body: JSON.stringify(overrides),
    });
    toast("Rerender queued");
    state.rerenderOpen[clipId] = false;
    fastPoll();
    refresh();
  } catch (error) {
    toast(error.message, true);
    btn.disabled = false;
  }
}

async function renameClip(id) {
  const clip = (state.data?.clips || []).find((item) => item.id === id);
  const next = prompt("Rename clip:", clip?.title || "");
  if (next === null) return;
  try {
    await api(`/api/clips/${id}/rename`, {
      method: "POST",
      body: JSON.stringify({ title: next }),
    });
    toast("Renamed");
  } catch (error) {
    toast(error.message, true);
  }
  refresh();
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
    const match = disposition.match(/filename\*?=(?:UTF-8''|")?([^";]+)/i);
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

// ---------------------------------------------------------------- polling
function fastPoll() {
  clearInterval(pollTimer);
  pollTimer = setInterval(() => {
    const busy = (state.data?.jobs || []).some((j) => j.status === "queued" || j.status === "running");
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
  $("#processAll").addEventListener("click", processAll);
  $("#searchBtn").addEventListener("click", openSearch);
  $("#dashBtn").addEventListener("click", openDashboard);
  $("#demoLink").addEventListener("click", (event) => {
    event.preventDefault();
    loadDemo();
  });
  $("#modalClose").addEventListener("click", closeModal);
  $("#modalOverlay").addEventListener("click", (event) => {
    if (event.target.id === "modalOverlay") closeModal();
  });
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeModal();
  });
  $("#episodeSearch").addEventListener("input", renderEpisodes);
  $("#clipSearch").addEventListener("input", renderShorts);
  $("#clipSort").addEventListener("change", renderShorts);
  $("#zipBtn").addEventListener("click", downloadZip);

  await refresh();
  if (!$("#playlistUrl").value && defaults.playlist_url) {
    $("#playlistUrl").value = defaults.playlist_url;
  }
  if ((state.data?.jobs || []).some((j) => j.status === "queued" || j.status === "running")) fastPoll();
  else pollTimer = setInterval(refresh, 8000);
});

window.generate = generate;
window.previewPicks = previewPicks;
window.toggleRerender = toggleRerender;
window.submitRerender = submitRerender;
window.deleteClip = deleteClip;
window.renameClip = renameClip;
window.shareClip = shareClip;
window.loadDemo = loadDemo;
window.openTranscript = openTranscript;
window.openChapters = openChapters;
window.exportCSV = exportCSV;
window.deleteEpisode = deleteEpisode;
window.openSearch = openSearch;
window.runSearch = runSearch;
window.jumpToEpisode = jumpToEpisode;
window.openDashboard = openDashboard;
window.retryJob = retryJob;
window.cancelJob = cancelJob;
window.segClick = segClick;
window.cutFromModal = cutFromModal;
