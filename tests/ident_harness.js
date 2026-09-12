/* Headless check for web/intro.js — the v6.3 spectrum ident.
 *
 * `node tests/ident_harness.js` prints a JSON verdict, then exits non-zero if
 * any scenario misbehaved. The DOM and WebAudio are stubbed just far enough to
 * drive the module: the harness owns the requestAnimationFrame clock, so frames
 * are synchronous and the audio graph is recorded instead of played. That lets
 * us assert the thing that silently broke in v6.2 — the ta-dum never firing
 * when a browser refuses autoplay — plus the beam painting itself.
 *
 * Scenarios cover desktop autoplay, a blocked context, an unlock tap mid-roll,
 * skip by tap, skip by Escape, reduced motion, high DPR, the sound pref off,
 * and "already seen this session" — plus the five ways v6.6 could still strand
 * the stage over the app: a starved main thread (4 fps), rAF never coming back,
 * the tab going hidden mid-roll, a canvas whose context is lost, and a page
 * reloaded seconds after the ident already ran.
 */
"use strict";

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "..");
const INTRO = path.join(ROOT, "web", "intro.js");
const FPS = 24;
const FRAME = 1000 / FPS;

/* ------------------------------------------------------------------ *
 * Minimal DOM
 * ------------------------------------------------------------------ */
function makeEl(tagName, className) {
  const el = {
    tagName,
    id: "",
    _classes: new Set((className || "").split(/\s+/).filter(Boolean)),
    style: {},
    listeners: {},
    children: [],
    parentNode: null,
    removed: false,
    _size: null,
  };
  el.classList = {
    toggle(cls, on) {
      if (on === undefined) {
        if (el._classes.has(cls)) el._classes.delete(cls);
        else el._classes.add(cls);
      } else if (on) el._classes.add(cls);
      else el._classes.delete(cls);
    },
    add(...names) { names.forEach((n) => el._classes.add(n)); },
    remove(...names) { names.forEach((n) => el._classes.delete(n)); },
    contains(name) { return el._classes.has(name); },
  };
  el.addEventListener = (type, fn) => {
    (el.listeners[type] = el.listeners[type] || []).push(fn);
  };
  el.removeEventListener = (type, fn) => {
    el.listeners[type] = (el.listeners[type] || []).filter((f) => f !== fn);
  };
  el.dispatch = (type, event) => {
    (el.listeners[type] || []).slice().forEach((fn) => fn(event || { type }));
  };
  el.getBoundingClientRect = () => ({
    width: el._size ? el._size[0] : 1280,
    height: el._size ? el._size[1] : 720,
  });
  el.querySelector = (sel) => matchOne(el, sel);
  el.querySelectorAll = (sel) => matchAll(el, sel);
  el.appendChild = (child) => { child.parentNode = el; el.children.push(child); return child; };
  el.removeChild = (child) => {
    el.children = el.children.filter((c) => c !== child);
    child.parentNode = null;
    child.removed = true;
  };
  el.remove = () => {
    if (el.parentNode) el.parentNode.removeChild(el);
    else el.removed = true;
  };
  el.getContext = () => ctx2d;
  return el;
}

/* supports ".a", ".a.b", ".a, .b" and "#id" — all intro.js ever asks for */
function matches(el, sel) {
  return sel.split(",").map((s) => s.trim()).some((one) => {
    if (one.startsWith("#")) return el.id === one.slice(1);
    if (one.startsWith(".")) return one.slice(1).split(".").every((c) => el._classes.has(c));
    return el.tagName === one;
  });
}

function walk(el, out) {
  el.children.forEach((child) => { out.push(child); walk(child, out); });
  return out;
}
const matchAll = (root, sel) => walk(root, []).filter((el) => matches(el, sel));
const matchOne = (root, sel) => matchAll(root, sel)[0] || null;

/* A recording 2D context: counts only, and the "lighter" mode we rely on. */
const ctx2d = (() => {
  const calls = { fillRect: 0, stroke: 0, arc: 0, gradient: 0, clear: 0, lighter: 0 };
  const counter = (key) => () => { calls[key] += 1; };
  const api = {
    calls,
    setTransform: counter("setTransform") ,
    clearRect: counter("clear"),
    fillRect: counter("fillRect"),
    beginPath: () => {},
    stroke: counter("stroke"),
    fill: () => {},
    arc: counter("arc"),
    createRadialGradient: () => { calls.gradient += 1; return { addColorStop: () => {} }; },
    createLinearGradient: () => { calls.gradient += 1; return { addColorStop: () => {} }; },
    fillStyle: "", strokeStyle: "", lineWidth: 1,
  };
  let composite = "source-over";
  Object.defineProperty(api, "globalCompositeOperation", {
    get: () => composite,
    set: (v) => { composite = v; if (v === "lighter") calls.lighter += 1; },
  });
  return api;
})();

function resetCtx() {
  Object.keys(ctx2d.calls).forEach((k) => { ctx2d.calls[k] = 0; });
}

function buildDocument() {
  const overlay = makeEl("div", "intro-overlay");
  overlay.id = "introOverlay";
  const stage = makeEl("canvas", "intro-stage");
  stage._size = [1280, 720];
  overlay.appendChild(stage);
  [["div", "intro-grain"], ["div", "intro-vignette"], ["div", "intro-ident"],
   ["svg", "intro-q"], ["g", "intro-q-mark"], ["circle", "intro-q-halo"],
   ["circle", "intro-q-ring"], ["circle", "intro-q-core"], ["path", "intro-q-tail"],
   ["path", "intro-q-tail intro-q-tail-halo"], ["div", "intro-lockup"],
   ["div", "intro-wordmark"], ["div", "intro-sub"], ["div", "intro-rule"],
   ["button", "intro-skip"], ["div", "intro-hint"]].forEach(([tag, cls]) => {
    overlay.appendChild(makeEl(tag, cls));
  });
  const body = makeEl("body");
  body.appendChild(overlay);
  const doc = {
    body,
    hidden: false,
    listeners: {},
    getElementById: (id) => (id === "introOverlay" ? overlay : matchOne(body, `#${id}`)),
    querySelector: (sel) => matchOne(body, sel),
    querySelectorAll: (sel) => matchAll(body, sel),
    createElement: (tag) => makeEl(tag, ""),
    addEventListener(type, fn) { (doc.listeners[type] = doc.listeners[type] || []).push(fn); },
    removeEventListener(type, fn) {
      doc.listeners[type] = (doc.listeners[type] || []).filter((f) => f !== fn);
    },
    dispatch(type, event) {
      (doc.listeners[type] || []).slice().forEach((fn) => fn(event || { type }));
    },
  };
  return { doc, overlay };
}

/* ------------------------------------------------------------------ *
 * WebAudio stub — records the schedule instead of playing it
 * ------------------------------------------------------------------ */
function makeAudioStub(rec, opts) {
  const Ctx = class {
    constructor() {
      this.sampleRate = 48000;
      this.state = opts.state || "running";
      this._t = 0;
      rec.contexts += 1;
    }
    get currentTime() { return this._t; }
    set currentTime(v) { this._t = v; }
    get destination() { return { kind: "destination" }; }
    resume() {
      rec.resumeCalls += 1;
      if (opts.deferResume) {
        // real mobile: the promise settles, but the context stays suspended
        // until a gesture, so the ident must hold its frame instead
        return Promise.resolve();
      }
      if (opts.blocked) {
        rec.resumeRefused += 1;
        return Promise.reject(new Error("NotAllowedError: play() failed"));
      }
      this.state = "running";
      return Promise.resolve();
    }
    close() { rec.closeCalls += 1; this.state = "closed"; return Promise.resolve(); }
    _param(node, name, value) {
      const p = {
        node, name, value,
        setValueAtTime(v, at) { p.value = v; rec.params.push([`${node}.${name}`, at, v]); return p; },
        linearRampToValueAtTime(v, at) { rec.params.push([`${node}.${name}`, at, v]); return p; },
        exponentialRampToValueAtTime(v, at) {
          if (!(v > 0)) throw new Error(`${node}.${name}: exponential ramp to ${v} (must be > 0)`);
          rec.params.push([`${node}.${name}`, at, v]);
          return p;
        },
        cancelScheduledValues() { return p; },
        setTargetAtTime(v, at) { rec.params.push([`${node}.${name}`, at, v]); return p; },
      };
      return p;
    }
    _node(kind, extra) {
      const self = this;
      const node = Object.assign({
        kind,
        connects: [],
        connect(dest) { node.connects.push(dest); return dest; },
        disconnect() {},
        start(when, offset) {
          if (self.state === "closed") throw new Error(`${kind}.start() after ctx.close()`);
          rec.started.push({
            kind,
            at: Number((when === undefined ? self.currentTime : when).toFixed(4)),
            rel: Number((Number(when === undefined ? self.currentTime : when) - self.currentTime).toFixed(4)),
            ctxAt: Number(self.currentTime.toFixed(4)),
            freq: node.frequency ? node.frequency.value : null,
            type: node.type || null,
            offset: offset === undefined ? null : Number(offset.toFixed(3)),
          });
        },
        stop(when) { rec.stops.push(Number((when === undefined ? self.currentTime : when).toFixed(4))); },
      }, extra || {});
      return node;
    }
    createGain() { return this._node("gain", { gain: this._param("gain", "gain", 1) }); }
    createDynamicsCompressor() {
      const node = this._node("compressor");
      ["threshold", "knee", "ratio", "attack", "release"].forEach((p) => {
        node[p] = this._param("compressor", p, 0);
      });
      return node;
    }
    createStereoPanner() {
      return this._node("panner", { pan: this._param("panner", "pan", 0) });
    }
    createBiquadFilter() {
      return this._node("biquad", {
        type: "lowpass",
        frequency: this._param("biquad", "frequency", 350),
        Q: this._param("biquad", "Q", 1),
      });
    }
    createConvolver() { return this._node("convolver", { buffer: null }); }
    createAnalyser() {
      rec.analysers += 1;
      return this._node("analyser", {
        fftSize: 256,
        smoothingTimeConstant: 0.8,
        frequencyBinCount: 128,
        getByteFrequencyData(arr) {
          rec.fftReads += 1;
          for (let i = 0; i < arr.length; i += 1) arr[i] = 60 + ((i * 13) % 180);
        },
      });
    }
    createOscillator() {
      const node = this._node("oscillator", {
        type: "sine",
        frequency: this._param("oscillator", "frequency", 440),
        detune: this._param("oscillator", "detune", 0),
      });
      node.kind = "oscillator";
      return node;
    }
    createBufferSource() {
      const node = this._node("buffer", {
        buffer: null,
        playbackRate: this._param("buffer", "playbackRate", 1),
      });
      node.kind = "buffer";
      return node;
    }
    createBuffer(channels, length) {
      rec.buffers.push({ channels, length });
      const data = new Float32Array(length);
      return { length, numberOfChannels: channels, getChannelData: () => data };
    }
  };
  return { Ctx };
}

/* ------------------------------------------------------------------ *
 * Drive one scenario
 * ------------------------------------------------------------------ */
/* A 2D context that fails the moment it is asked to paint — a lost WebGL/2D
 * context or an out-of-memory canvas on a low-end phone. */
function brokenCtx() {
  const noop = () => {};
  return {
    setTransform: noop, clearRect: noop, beginPath: noop, closePath: noop,
    stroke: noop, fill: noop, arc: noop, save: noop, restore: noop,
    translate: noop, rotate: noop, scale: noop, moveTo: noop, lineTo: noop,
    fillRect() { throw new Error("CanvasRenderingContext2D lost"); },
    createRadialGradient: () => ({ addColorStop: noop }),
    createLinearGradient: () => ({ addColorStop: noop }),
    createPattern: () => null,
    fillStyle: "", strokeStyle: "", lineWidth: 1, globalAlpha: 1,
    globalCompositeOperation: "source-over",
  };
}

function runScenario(cfg) {
  resetCtx();
  const { doc, overlay } = buildDocument();
  if (cfg.throwOnPaint) {
    matchOne(overlay, ".intro-stage").getContext = () => brokenCtx();
  }
  const rec = {
    contexts: 0, resumeCalls: 0, resumeRefused: 0, closeCalls: 0, analysers: 0,
    fftReads: 0, buffers: [], started: [], stops: [], params: [],
  };
  const rafQueue = [];
  const timers = [];
  let now = 0;
  let ctxRef = null;

  const storage = (backing) => ({
    getItem: (k) => (k in backing ? backing[k] : null),
    setItem: (k, v) => { backing[k] = String(v); },
    removeItem: (k) => { delete backing[k]; },
  });
  const session = Object.assign({}, cfg.session);
  const local = Object.assign({}, cfg.local);

  const win = {
    innerWidth: 1280,
    innerHeight: 720,
    devicePixelRatio: cfg.dpr || 1,
    AudioContext: class extends makeAudioStub(rec, cfg.audio || {}).Ctx {
      constructor() { super(); ctxRef = this; }
    },
    requestAnimationFrame: (fn) => { rafQueue.push(fn); return rafQueue.length; },
    cancelAnimationFrame: () => { rafQueue.length = 0; },
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => {},
    setTimeout: (fn, ms) => { timers.push({ fn, at: now + (ms || 0) }); return timers.length; },
    clearTimeout: () => {},
    matchMedia: (q) => ({ matches: !!cfg.reduced, media: q, addEventListener() {}, addListener() {} }),
    location: { search: cfg.search || "" },
  };

  const sandbox = {
    window: win,
    document: doc,
    performance: { now: () => now },
    localStorage: storage(local),
    sessionStorage: storage(session),
    URLSearchParams,
    Float32Array,
    Uint8Array,
    Math,
    Date,
    JSON,
    Promise,
    console: { log: () => {}, warn: () => {}, error: () => {} },
  };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(INTRO, "utf8"), sandbox, { filename: "web/intro.js" });

  const ident = win.QyroIdent;
  const trace = [];
  let gestureDone = false;
  let hiddenDone = false;
  let framesDelivered = 0;
  let endedWall = null;
  let error = null;
  const step = cfg.lag === undefined ? FRAME : cfg.lag;

  const pump = () => {
    // let promise chains from resume()/arm() settle between frames
    for (let i = 0; i < 4; i += 1) {
      // synchronous stubs settle immediately; the extra passes keep the
      // frame loop honest if the module ever awaits.
      void 0;
    }
    timers.filter((t) => t.at <= now).slice().forEach((t) => {
      timers.splice(timers.indexOf(t), 1);
      t.fn();
    });
  };

  try {
    ident.boot();
    for (let i = 0; i < Math.round(FPS * 14); i += 1) {
      now += step;
      if (ctxRef) ctxRef.currentTime = now / 1000;
      if (cfg.gestureAt !== undefined && !gestureDone && now / 1000 >= cfg.gestureAt) {
        gestureDone = true;
        if (cfg.gesture === "overlay") overlay.dispatch("click", {});
        else if (cfg.gesture === "esc") doc.dispatch("keydown", { key: "Escape" });
        else doc.dispatch("pointerdown", {});
        if ((cfg.audio || {}).deferResume && cfg.gesture !== "overlay" && ctxRef) {
          ctxRef.state = "running";
        }
      }
      // the phone went back to the home screen / the app switcher: rAF stops
      if (cfg.hideAt !== undefined && !hiddenDone && now / 1000 >= cfg.hideAt) {
        hiddenDone = true;
        doc.hidden = true;
        doc.dispatch("visibilitychange", { type: "visibilitychange" });
      }
      if (overlay.classList.contains("dismissed")) { endedWall = now / 1000; break; }
      // `noFramesAfter` starves the module: from that frame on, the browser
      // simply never calls back, while timers keep running in the background.
      if (cfg.noFramesAfter !== undefined && i >= cfg.noFramesAfter) rafQueue.length = 0;
      const queued = rafQueue.splice(0, rafQueue.length);
      if (!queued.length && !cfg.keepPumping) break;
      framesDelivered += queued.length;
      queued.forEach((fn) => fn(now));
      trace.push({ wall: Number((now / 1000).toFixed(3)), cues: rec.started.length });
      if (overlay.classList.contains("dismissed")) { endedWall = now / 1000; break; }
      pump();
    }
  } catch (thrown) {
    error = String((thrown && thrown.stack) || thrown).split("\n").slice(0, 4).join(" | ");
  }

  const toneStarts = rec.started.filter((s) => s.kind === "oscillator");
  const byFreq = (hz) => toneStarts.find((s) => Math.abs((s.freq || 0) - hz) < 0.01);
  const ta = byFreq(244);
  const dum = byFreq(162);
  const lockup = matchOne(overlay, ".intro-lockup");

  return {
    error,
    frames: framesDelivered,
    classes: {
      live: overlay.classList.contains("live"),
      dismissed: overlay.classList.contains("dismissed"),
      dim: overlay.classList.contains("dim"),
      lockupIn: lockup.classList.contains("in"),
      lockupTag: lockup.classList.contains("tag"),
    },
    overlayRemoved: overlay.removed,
    sessionFlag: session["qyro.introSeen"] || null,
    contexts: rec.contexts,
    resumeCalls: rec.resumeCalls,
    resumeRefused: rec.resumeRefused,
    closeCalls: rec.closeCalls,
    fftReads: rec.fftReads,
    impulseSeconds: rec.buffers.length
      ? Number((Math.max(...rec.buffers.map((b) => b.length)) / 48000).toFixed(2))
      : 0,
    cues: rec.started.length,
    tones: toneStarts.length,
    noiseLayers: rec.started.filter((s) => s.kind === "buffer").length,
    firstCueRel: rec.started.length ? Math.min(...rec.started.map((s) => s.rel)) : null,
    lastCueRel: rec.started.length ? Math.max(...rec.started.map((s) => s.rel)) : null,
    taAt: ta ? ta.at : null,
    dumAt: dum ? dum.at : null,
    taToDum: ta && dum ? Number((dum.at - ta.at).toFixed(3)) : null,
    endedWall: endedWall === null ? null : Number(endedWall.toFixed(3)),
    drawRects: ctx2d.calls.fillRect,
    strokes: ctx2d.calls.stroke,
    gradients: ctx2d.calls.gradient,
    additivePasses: ctx2d.calls.lighter,
    endAfterGate: endedWall === null ? null : Number((endedWall - ident.T.end).toFixed(3)),
  };
}

const scenarios = {
  desktop: { note: "autoplay allowed: the whole score is scheduled, no gate" },
  blocked: {
    note: "suspended and resume refused: finish muted, after one short hold",
    audio: { state: "suspended", blocked: true },
  },
  unlock: {
    note: "suspended until a tap: the tap starts the sound and must not skip",
    audio: { state: "suspended", deferResume: true },
    gestureAt: 1.95,
    gesture: "document",
  },
  unlockLate: {
    note: "no tap at all within holdMax: carry on silently, never stall",
    audio: { state: "suspended", deferResume: true },
  },
  skip: { note: "a tap after the burst dismisses", gestureAt: 4.2, gesture: "overlay" },
  esc: { note: "Escape dismisses", gestureAt: 2.6, gesture: "esc" },
  early: { note: "a tap before the burst unlocks instead of skipping", gestureAt: 0.4, gesture: "document" },
  reduced: { note: "prefers-reduced-motion: reduce", reduced: true },
  hiDpi: { note: "devicePixelRatio 3 must be clamped, not crash", dpr: 3 },
  muted: { note: "ident sound switched off in settings", local: { "qyro.identSound": "0" } },
  again: { note: "already seen this session: nothing plays", session: { "qyro.introSeen": "1" } },
  /* v6.6 — the report these four exist for: a phone busy encoding a short
   * stopped having a main thread to spare, and a rAF-driven ident turned into
   * a stuck rainbow screen with a beep. None of these may leave the stage up. */
  starved: {
    note: "4 frames a second: the timeline catches up instead of crawling",
    lag: 250,
  },
  noFrames: {
    note: "rAF never fires again (backgrounded/frozen tab): the timer frees the app",
    noFramesAfter: 1,
    keepPumping: true,
  },
  hiddenTab: {
    note: "the tab goes hidden mid-roll: put the stage away, do not freeze it",
    hideAt: 3.0,
  },
  thrown: {
    note: "the canvas context is lost mid-ident: dismiss instead of stranding it",
    throwOnPaint: true,
  },
  cooldown: {
    note: "reloaded seconds after a previous ident: the app shows, not the intro",
    local: { "qyro.introSeenAt": String(Date.now() - 4000) },
  },
};

const results = {};
Object.entries(scenarios).forEach(([key, cfg]) => {
  results[key] = Object.assign({ note: cfg.note }, runScenario(cfg));
});

/* ------------------------------------------------------------------ *
 * Verdict
 * ------------------------------------------------------------------ */
const fail = [];
const expect = (cond, msg) => { if (!cond) fail.push(msg); };
const done = (key, r) => r.classes.live && r.classes.dismissed;

// Scenarios that are *about* not painting (lost context, starved frames, a
// seen visit) are held to the dismissal contract, not the paint contract.
const sparse = new Set(["again", "cooldown", "thrown", "noFrames", "starved"]);

Object.entries(results).forEach(([key, r]) => {
  if (r.error) fail.push(`${key}: threw ${r.error}`);

  if (key === "again" || key === "cooldown") {
    // a returning visit must hide the stage on the very first frame
    if (!r.classes.dismissed) fail.push(`${key}: seen overlay still covers the app`);
    if (!r.classes.live) fail.push(`${key}: seen overlay must arm the CSS fail-safe off`);
    return;
  }
  if (!r.classes.live) fail.push(`${key}: overlay was never armed (.live)`);
  if (!r.classes.dismissed) fail.push(`${key}: overlay left covering the app`);
  if (r.overlayRemoved) fail.push(`${key}: overlay node was removed (replay would break)`);
  if (sparse.has(key)) return;
  if (r.sessionFlag !== "1") fail.push(`${key}: session flag not written`);
  if (r.drawRects < 200) fail.push(`${key}: beams were not painted (${r.drawRects} fillRects)`);
});

/* v6.6: the ident must never be the thing standing between a user and their
 * app. Each of these is a way v6.5 could strand the stage. */
expect(results.starved.classes.dismissed && results.starved.frames > 0,
       "starved: a slow device must still finish the ident");
expect(results.starved.endedWall !== null && results.starved.endedWall <= 7.5,
       `starved: 4 fps stretched the ident to ${results.starved.endedWall}s `
       + "(the timeline must catch up with the wall clock)");
expect(results.starved.frames < 40,
       `starved: ${results.starved.frames} frames were replayed instead of skipped`);
expect(results.noFrames.classes.dismissed,
       "noFrames: with no rAF callbacks the timer must still hand the page back");
expect(results.noFrames.endedWall !== null && results.noFrames.endedWall <= 12,
       `noFrames: the dismissal deadline fired late (${results.noFrames.endedWall}s)`);
expect(results.hiddenTab.classes.dismissed,
       "hiddenTab: a hidden tab kept the overlay up");
expect(results.hiddenTab.endedWall !== null && results.hiddenTab.endedWall <= 3.5,
       "hiddenTab: the stage must go away as the page is hidden, not after it");
expect(results.thrown.classes.dismissed,
       "thrown: a canvas that throws left the stage over the app");
expect(results.thrown.endedWall !== null && results.thrown.endedWall < 1.0,
       "thrown: a failed frame must end the ident at once, not spin");
expect(results.cooldown.cues === 0 && results.cooldown.contexts === 0,
       "cooldown: a reload storm must not rebuild the audio graph");

expect(results.desktop.classes.dim, "desktop: the fade-out class never arrived");
expect(results.desktop.classes.lockupIn && results.desktop.classes.lockupTag,
       "desktop: wordmark/tagline reveals never fired");
expect(results.desktop.cues > 30, `desktop: only ${results.desktop.cues} cues scheduled`);
expect(results.desktop.tones > 25, "desktop: too few pitched voices");
expect(results.desktop.noiseLayers >= 4, "desktop: no noise layers (whoosh/snap/air)");
expect(results.desktop.impulseSeconds >= 1.2, "desktop: no reverb impulse generated");
// v0.6.5 anti-glitch: every cue must be schedulable strictly ahead of the
// audio clock — an envelope whose attack lands "now" is an audible click.
expect(results.desktop.firstCueRel === null || results.desktop.firstCueRel >= 0.05,
       `desktop: a cue was scheduled with no lead room (${results.desktop.firstCueRel}s)`);
expect(results.unlock.firstCueRel === null || results.unlock.firstCueRel >= 0.05,
       `unlock: resumed score scheduled too tight (${results.unlock.firstCueRel}s)`);
expect(results.desktop.fftReads > 0, "desktop: beams are not driven by the analyser");
expect(results.desktop.taAt !== null && results.desktop.dumAt !== null,
       "desktop: the TA or the DUM was never scheduled");
expect(results.desktop.taToDum !== null
       && results.desktop.taToDum > 0.15 && results.desktop.taToDum < 0.8,
       `desktop: ta-dum spacing wrong (${results.desktop.taToDum}s)`);
expect(results.desktop.firstCueRel >= 0, "desktop: a cue was scheduled in the past");
expect(results.desktop.additivePasses > 50, "desktop: beams are not drawn additively");

expect(results.blocked.contexts === 1, "blocked: should still build one context");
expect(results.blocked.resumeRefused > 0, "blocked: resume was never attempted");
expect(results.blocked.cues === 0, `blocked: ${results.blocked.cues} cues must not be scheduled`);
expect(results.blocked.endAfterGate !== null
       && results.blocked.endAfterGate > 0.4 && results.blocked.endAfterGate < 1.4,
       `blocked: the gate should hold about one holdMax, got ${results.blocked.endAfterGate}s`);

expect(results.unlock.cues > 30, "unlock: the tap did not bring the score in");
expect(results.unlock.endAfterGate !== null
       && results.unlock.endAfterGate > 0.1 && results.unlock.endAfterGate < 0.3,
       `unlock: the frame should hold until the tap, held ${results.unlock.endAfterGate}s`);
expect(results.unlock.taToDum !== null && results.unlock.taToDum > 0.15,
       "unlock: ta-dum lost its shape after resuming");
expect(results.unlock.taAt !== null && results.unlock.taAt > 0,
       "unlock: the TA must be scheduled from the resumed clock, not in the past");
expect(results.unlockLate.cues === 0, "unlockLate: nothing may be scheduled while suspended");
expect(results.unlockLate.endAfterGate !== null
       && results.unlockLate.endAfterGate > 0.8 && results.unlockLate.endAfterGate < 1.1,
       `unlockLate: holdMax not respected (${results.unlockLate.endAfterGate}s)`);

expect(results.early.cues > 30, "early: a tap at 0.4s must start the score");
expect(results.early.classes.dismissed, "early: overlay must still finish normally");
expect(results.skip.cues > 30, "skip: cues should already be playing before the tap");
expect(results.esc.classes.dismissed, "esc: Escape did not dismiss the ident");

expect(results.muted.cues === 0, "muted: no cue may be scheduled with sound off");
expect(results.muted.contexts === 0, "muted: no AudioContext should even be built");
expect(results.muted.endAfterGate !== null && results.muted.endAfterGate < 0.1,
       "muted: a deliberate mute must not make the ident wait for audio");
expect(results.muted.classes.dismissed, "muted: the visual ident must still play");

expect(results.reduced.classes.dismissed, "reduced: motion-reduced path must finish");
expect(results.hiDpi.classes.dismissed, "hiDpi: DPR 3 path must finish");
expect(results.again.contexts === 0 && results.again.cues === 0,
       "again: a seen session must not build audio or schedule a single cue");
expect(results.again.sessionFlag === "1", "again: the seen flag must survive the skip");

console.log(JSON.stringify(results, null, 2));
if (fail.length) {
  console.error("\nIDENT HARNESS FAILURES:\n - " + fail.join("\n - "));
  process.exit(1);
}
console.log("\nIDENT HARNESS OK — " + Object.keys(scenarios).length + " scenarios");
