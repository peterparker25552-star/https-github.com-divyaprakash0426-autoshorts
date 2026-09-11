/* Qyro v6.3 — the "Spectrum" ident.
 *
 * A glowing Q draws itself on black, charges, then bursts outward into a full
 * spectrum of vertical light beams that dance like an equalizer. Everything you
 * hear is synthesised at runtime by WebAudio (ta, dum, sub boom, riser, a
 * left-to-right arpeggio as the beams launch, a resolving chord under the
 * wordmark) — no audio file ships with the app, so the ident works offline,
 * inside the installed PWA and in a Termux WebView exactly like on a desktop.
 *
 * Three rules keep it reliable:
 *   1. ONE clock. Visuals and audio both read `state.clock`; if the browser
 *      blocks autoplay the frame is held at the gate, the tap that unlocks
 *      audio resumes it, and the hit still lands on the burst.
 *   2. A tap never swallows the sound. While the ident is waiting for audio the
 *      first gesture unlocks and starts the score; only a later tap (or Esc, or
 *      the Skip button) dismisses it. The v6.2 build cancelled the cue from its
 *      own overlay click handler, which is why it looked silent.
 *   3. Only compositor-cheap work per frame: no animated CSS blur or
 *      backdrop-filter, devicePixelRatio clamped to 2, and each beam is three
 *      additive gradient passes rather than a `shadowBlur` sweep.
 */
(function () {
  "use strict";

  /* ------------------------------------------------------------------ *
   * Timeline, in seconds. `holdGate` is where the ident waits if audio is
   * still blocked; `holdMax` is how long it will make you wait at all.
   * ------------------------------------------------------------------ */
  const T = {
    ring: 0.30,          // the Q starts tracing itself
    ringDone: 1.28,      // ring closed
    tail: 1.24,          // the Q's tail slashes in
    charge: 1.46,        // glow + rumble build
    holdGate: 1.80,      // hold here until audio is live
    holdMax: 0.90,
    ta: 2.02,            // TA
    burst: 2.14,         // Q blows apart, beams fire
    dum: 2.38,           // DUM
    launch: 0.95,        // one beam's rise duration
    sweep: 0.62,         // centre-outwards stagger
    settle: 3.52,        // equaliser relaxes
    word: 3.58,          // QYRO lands
    tag: 4.14,           // tagline + rule
    fade: 5.06,          // dim towards the app
    end: 5.88,
  };

  const STORE_SEEN = "qyro.introSeen";
  const STORE_SOUND = "qyro.identSound";

  /* ------------------------------------------------------------------ *
   * Maths helpers
   * ------------------------------------------------------------------ */
  const clamp = (x, lo, hi) => (x < lo ? lo : x > hi ? hi : x);
  const clamp01 = (x) => clamp(x, 0, 1);
  const lerp = (a, b, u) => a + (b - a) * u;
  const inv = (t, a, b) => clamp01((t - a) / (b - a));
  const easeOutCubic = (u) => 1 - Math.pow(1 - u, 3);
  const easeInQuad = (u) => u * u;
  const easeInOutSine = (u) => -(Math.cos(Math.PI * u) - 1) / 2;
  /* Fast rise with a hint of overshoot, so the beams look fired, not faded. */
  const grow = (u) => {
    if (u <= 0) return 0;
    if (u >= 1) return 1;
    return easeOutCubic(u) * (1 + 0.16 * Math.sin(u * Math.PI * 2.1) * (1 - u));
  };

  /* Red → violet across the screen, the same ramp as the wordmark rule. */
  const hueAt = (frac) => (345 + 300 * frac) % 360;
  const beamColor = (frac, light, alpha) =>
    `hsla(${hueAt(frac).toFixed(1)}, 96%, ${light}%, ${Math.max(0, alpha).toFixed(3)})`;

  /* ------------------------------------------------------------------ *
   * Sound — all procedural, through a generated convolution reverb so the
   * hits read as a studio ident instead of synthesiser beeps.
   * ------------------------------------------------------------------ */
  function createSound() {
    let ctx = null;
    let out = null;
    let verbSend = null;
    let analyser = null;
    let bins = null;
    let noise = null;
    let started = false;
    let halted = false;
    let enabled = true;

    try {
      enabled = localStorage.getItem(STORE_SOUND) !== "0";
    } catch (error) { /* private mode */ }

    function makeNoise(c) {
      const len = Math.floor(c.sampleRate * 2);
      const buf = c.createBuffer(1, len, c.sampleRate);
      const data = buf.getChannelData(0);
      for (let i = 0; i < len; i += 1) data[i] = Math.random() * 2 - 1;
      return buf;
    }

    /* Decaying stereo noise = a cheap, convincing hall impulse. */
    function makeImpulse(c, seconds, decay) {
      const rate = c.sampleRate;
      const len = Math.max(1, Math.floor(rate * seconds));
      const buf = c.createBuffer(2, len, rate);
      for (let ch = 0; ch < 2; ch += 1) {
        const data = buf.getChannelData(ch);
        for (let i = 0; i < len; i += 1) {
          const t = i / len;
          const onset = t < 0.004 ? t / 0.004 : 1;
          data[i] = (Math.random() * 2 - 1) * Math.pow(1 - t, decay) * onset;
        }
      }
      return buf;
    }

    function build() {
      if (ctx) return true;
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return false;
      try {
        ctx = new AC({ latencyHint: "interactive" });
      } catch (error) {
        try { ctx = new AC(); } catch (inner) { ctx = null; }
      }
      if (!ctx) return false;

      out = ctx.createGain();
      out.gain.value = 0.95;

      const comp = ctx.createDynamicsCompressor();
      comp.threshold.value = -13;
      comp.knee.value = 18;
      comp.ratio.value = 7;
      comp.attack.value = 0.003;
      comp.release.value = 0.28;

      try {
        analyser = ctx.createAnalyser();
        analyser.fftSize = 256;
        analyser.smoothingTimeConstant = 0.74;
        bins = new Uint8Array(analyser.frequencyBinCount);
      } catch (error) { analyser = null; }

      const verb = ctx.createConvolver();
      verb.buffer = makeImpulse(ctx, 2.6, 3.1);
      verbSend = ctx.createGain();
      verbSend.gain.value = 0.62;
      const verbOut = ctx.createGain();
      verbOut.gain.value = 0.44;
      verbSend.connect(verb);
      verb.connect(verbOut);
      verbOut.connect(comp);

      out.connect(comp);
      if (analyser) comp.connect(analyser);   // taps the bus, does not feed it
      comp.connect(ctx.destination);
      noise = makeNoise(ctx);
      return true;
    }

    function connectTail(node, pan, sendToVerb) {
      let tail = node;
      if (typeof pan === "number" && ctx.createStereoPanner) {
        const panner = ctx.createStereoPanner();
        panner.pan.value = clamp(pan, -1, 1);
        node.connect(panner);
        tail = panner;
      }
      tail.connect(out);
      if (sendToVerb && verbSend) node.connect(verbSend);
    }

    function shape(gain, at, peak, attack, dur, hold) {
      const g = gain.gain;
      const top = Math.max(0.0002, peak);
      const end = Math.max(at + attack + 0.02, at + dur);
      g.setValueAtTime(0.0001, at);
      g.exponentialRampToValueAtTime(top, at + Math.max(0.002, attack));
      if (hold && hold < dur) g.setValueAtTime(top, at + hold);
      g.exponentialRampToValueAtTime(0.0001, end);
      return end;
    }

    /* A pitched voice with an optional glide: drums, sub, chords, plucks. */
    function tone(at, dur, opts) {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = opts.type || "sine";
      const from = Math.max(20, opts.from);
      const to = Math.max(20, opts.to || from);
      osc.frequency.setValueAtTime(from, at);
      if (to !== from) {
        osc.frequency.exponentialRampToValueAtTime(to, at + dur * (opts.glide || 0.85));
      }
      if (opts.detune) osc.detune.setValueAtTime(opts.detune, at);
      const end = shape(gain, at, opts.peak, opts.attack || 0.004, dur, opts.hold);
      let tail = gain;
      if (opts.lowpass) {
        const lp = ctx.createBiquadFilter();
        lp.type = "lowpass";
        lp.frequency.setValueAtTime(opts.lowpass, at);
        if (opts.lowpassTo) {
          lp.frequency.exponentialRampToValueAtTime(opts.lowpassTo, at + dur);
        }
        gain.connect(lp);
        tail = lp;
      }
      osc.connect(gain);
      connectTail(tail, opts.pan, opts.verb);
      osc.start(at);
      osc.stop(end + 0.05);
    }

    /* Noise through a swept filter: whooshes, snaps, air. */
    function noiseHit(at, dur, opts) {
      const src = ctx.createBufferSource();
      src.buffer = noise;
      if (opts.rate) src.playbackRate.value = opts.rate;
      const filter = ctx.createBiquadFilter();
      filter.type = opts.type || "bandpass";
      filter.Q.value = opts.q === undefined ? 1.1 : opts.q;
      filter.frequency.setValueAtTime(opts.from || 1000, at);
      if (opts.to) {
        filter.frequency.exponentialRampToValueAtTime(Math.max(40, opts.to), at + dur * (opts.sweep || 0.9));
      }
      const gain = ctx.createGain();
      const end = shape(gain, at, opts.peak, opts.attack || 0.006, dur, opts.hold);
      src.connect(filter);
      filter.connect(gain);
      connectTail(gain, opts.pan, opts.verb);
      src.start(at, Math.random() * 0.5);
      src.stop(end + 0.05);
    }

    /* ---------------------------------------------------------------- *
     * The score. Every cue is scheduled at (cueTime - elapsed), so an
     * ident that started late simply skips whatever the visuals showed.
     * ---------------------------------------------------------------- */
    const PENTA = [0, 2, 4, 7, 9, 12, 14, 16, 19, 21, 24, 26, 28];

    function score(elapsed) {
      const now = ctx.currentTime + 0.02;
      const play = (when, fn) => {
        if (when < elapsed - 0.01) return;
        fn(now + (when - elapsed));
      };

      // 1. Charge — a low rumble gathers under the closing ring.
      play(T.charge, (a) => {
        const dur = T.ta - T.charge + 0.5;
        tone(a, dur, { type: "sine", from: 34, to: 58, peak: 0.2, attack: 0.35, hold: dur * 0.5 });
        noiseHit(a, dur, { type: "lowpass", from: 130, to: 520, peak: 0.1, attack: 0.3, q: 0.7 });
      });

      // 2. Riser — the sweep that throws the mark into the burst.
      play(T.charge - 0.35, (a) => {
        const dur = T.ta - (T.charge - 0.35) + 0.08;
        noiseHit(a, dur, { type: "bandpass", from: 260, to: 8200, peak: 0.15, attack: dur * 0.8, q: 0.9 });
        tone(a, dur, {
          type: "sawtooth", from: 82, to: 246, peak: 0.06, attack: dur * 0.7,
          lowpass: 900, lowpassTo: 5200,
        });
      });

      // 3. TA — the sharp one: a snap, a tom, a fleck of metal.
      play(T.ta, (a) => {
        noiseHit(a, 0.1, { type: "highpass", from: 1500, peak: 0.44, attack: 0.002, verb: true });
        tone(a, 0.28, { type: "sine", from: 244, to: 128, peak: 0.55, attack: 0.003, verb: true });
        tone(a, 0.34, { type: "triangle", from: 1760, to: 1480, peak: 0.11, attack: 0.003, verb: true });
      });

      // 4. DUM — the big one: body, sub, and a chest-thumping noise layer.
      play(T.dum, (a) => {
        tone(a, 1.2, { type: "sine", from: 162, to: 44, peak: 0.82, attack: 0.004, verb: true });
        tone(a, 2.2, { type: "sine", from: 41, to: 36, peak: 0.52, attack: 0.02, hold: 0.5 });
        noiseHit(a, 0.32, { type: "lowpass", from: 420, to: 150, peak: 0.42, attack: 0.004, verb: true });
        tone(a, 0.5, { type: "triangle", from: 320, to: 120, peak: 0.15, attack: 0.004, verb: true });
      });

      // 5. The spectrum — every beam plucks one note as it fires, panned to
      //    its place on screen and rising through a pentatonic run.
      const notes = PENTA.length;
      for (let i = 0; i < notes; i += 1) {
        const frac = i / (notes - 1);
        play(T.burst + 0.02 + frac * T.sweep, (a) => {
          const freq = 440 * Math.pow(2, (57 + PENTA[i] - 69) / 12);
          tone(a, 1.2 - 0.5 * frac, {
            type: "triangle", from: freq, peak: 0.085 + 0.05 * (1 - frac),
            attack: 0.004, pan: lerp(-0.85, 0.85, frac), verb: true,
          });
          tone(a, 0.4, {
            type: "sine", from: freq * 2, peak: 0.03, attack: 0.003,
            pan: lerp(-0.8, 0.8, frac), verb: true,
          });
        });
      }

      // 6. Air and sparkle over the beams as they reach full height.
      play(T.burst + 0.1, (a) => {
        noiseHit(a, 1.5, { type: "highpass", from: 4200, peak: 0.06, attack: 0.12, verb: true });
        [1320, 1760, 2640, 3520].forEach((f, i) => {
          tone(a + i * 0.05, 1.9, {
            type: "sine", from: f, peak: 0.035, attack: 0.09,
            pan: i % 2 ? 0.4 : -0.4, verb: true,
          });
        });
      });

      // 7. Resolve — a warm chord lands with the wordmark.
      play(T.word - 0.1, (a) => {
        [110, 220, 261.63, 329.63, 440].forEach((f, i) => {
          tone(a, 2.6, {
            type: "sawtooth", from: f, peak: i === 0 ? 0.08 : 0.035,
            attack: 0.4, detune: i * 4 - 8, lowpass: 1100,
            verb: true, pan: lerp(-0.5, 0.5, i / 4),
          });
        });
      });

      // 8. Goodbye — one last low breath before the overlay goes away.
      play(T.fade, (a) => {
        tone(a, 1.1, { type: "sine", from: 96, to: 48, peak: 0.22, attack: 0.02, verb: true });
        noiseHit(a, 0.9, { type: "bandpass", from: 2400, to: 300, peak: 0.05, attack: 0.05, verb: true });
      });
    }

    function fadeOut(seconds) {
      if (!ctx || !out) return;
      const now = ctx.currentTime;
      const left = Math.max(0.25, seconds);
      out.gain.cancelScheduledValues(now);
      out.gain.setValueAtTime(out.gain.value, now);
      out.gain.exponentialRampToValueAtTime(0.0001, now + left);
    }

    return {
      get ready() { return !!ctx && ctx.state === "running" && enabled; },
      get playing() { return started; },
      enabled() { return enabled; },
      muted() { return !enabled; },
      setEnabled(value) {
        enabled = !!value;
        try { localStorage.setItem(STORE_SOUND, enabled ? "1" : "0"); } catch (error) {}
        if (!enabled) fadeOut(0.2);
      },
      /* Get a running AudioContext. Resolves true when audio is truly live. */
      arm() {
        if (!enabled) return Promise.resolve(false);
        if (!build()) return Promise.resolve(false);
        if (ctx.state === "running") return Promise.resolve(true);
        if (!ctx.resume) return Promise.resolve(false);
        return ctx.resume()
          .then(() => ctx.state === "running")
          .catch(() => false);
      },
      start(elapsed) {
        if (!ctx || started || halted || !enabled || ctx.state !== "running") return false;
        started = true;
        score(clamp(elapsed || 0, 0, T.end - 0.2));
        return true;
      },
      /* Fade and refuse to restart — used by Skip and by tab-hide. */
      halt() {
        if (ctx && started) fadeOut(0.18);
        started = false;
        halted = true;
      },
      /* 0..1 spectrum energy per beam, read off the master bus. */
      levels(count, sink) {
        if (!analyser || !started || ctx.state !== "running") return false;
        analyser.getByteFrequencyData(bins);
        const usable = Math.max(4, Math.floor(bins.length * 0.72));
        for (let i = 0; i < count; i += 1) {
          const lo = Math.floor((i / count) * usable);
          const hi = Math.max(lo + 1, Math.floor(((i + 1) / count) * usable));
          let sum = 0;
          for (let j = lo; j < hi; j += 1) sum += bins[j];
          sink[i] = clamp01(sum / (hi - lo) / 190);
        }
        return true;
      },
      dispose() {
        const dying = ctx;
        ctx = null;
        out = null;
        verbSend = null;
        analyser = null;
        bins = null;
        noise = null;
        started = false;
        halted = false;
        if (dying && dying.close) {
          try { dying.close(); } catch (error) {}
        }
      },
    };
  }

  /* ------------------------------------------------------------------ *
   * Stage geometry
   * ------------------------------------------------------------------ */
  let sound = null;
  let state = null;

  function beamCount(width, reduced) {
    return clamp(Math.round(width / 24), reduced ? 16 : 26, 58);
  }

  function makeBeams(width, height, count) {
    const beams = [];
    for (let i = 0; i < count; i += 1) {
      const frac = count === 1 ? 0.5 : i / (count - 1);
      beams.push({
        frac,
        x: width * 0.02 + frac * width * 0.96,
        phase: (i * 1.73) % (Math.PI * 2),
        speed: 1.35 + ((i * 37) % 11) / 14,
        shape: 0.6 + 0.5 * Math.sin(Math.PI * (0.08 + 0.86 * frac)),
        delay: Math.abs(frac - 0.5) * 2,      // centre outwards
      });
    }
    return beams;
  }

  function makeMotes(count, width, height) {
    const motes = [];
    for (let i = 0; i < count; i += 1) {
      motes.push({
        x: Math.random() * width,
        y: Math.random() * height,
        r: 0.4 + Math.random() * 1.5,
        drift: 6 + Math.random() * 26,
        phase: Math.random() * Math.PI * 2,
        hue: Math.random(),
      });
    }
    return motes;
  }

  function sizeStage(canvas, host) {
    const dpr = clamp(window.devicePixelRatio || 1, 1, 2);
    const rect = host.getBoundingClientRect();
    const w = Math.max(2, Math.round(rect.width || window.innerWidth || 2));
    const h = Math.max(2, Math.round(rect.height || window.innerHeight || 2));
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    canvas.style.width = w + "px";
    canvas.style.height = h + "px";
    const g = canvas.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { g, w, h };
  }

  function drawGlow(g, x, y, radius, css, alpha) {
    if (alpha <= 0.003 || radius <= 0.5) return;
    const grad = g.createRadialGradient(x, y, 0, x, y, radius);
    grad.addColorStop(0, css.replace("@", alpha.toFixed(3)));
    grad.addColorStop(0.45, css.replace("@", (alpha * 0.32).toFixed(3)));
    grad.addColorStop(1, css.replace("@", "0"));
    g.fillStyle = grad;
    g.fillRect(x - radius, y - radius, radius * 2, radius * 2);
  }

  /* ------------------------------------------------------------------ *
   * One frame of the ident
   * ------------------------------------------------------------------ */
  function render(c, wall) {
    const s = state;
    if (!s || !s.stage) return;
    const { g, w, h } = s.stage;
    const reduced = s.reduced;

    g.setTransform(s.dpr, 0, 0, s.dpr, 0, 0);
    g.clearRect(0, 0, w, h);

    const horizon = h * 0.735;
    const qy = h * (s.lockup ? 0.4 : 0.45);
    const qs = Math.max(110, Math.min(220, Math.min(w, h) * 0.3));

    const ringIn = reduced ? 1 : easeInOutSine(inv(c, T.ring, T.ringDone));
    const tailIn = reduced ? 1 : easeOutCubic(inv(c, T.tail, T.tail + 0.42));
    const charge = inv(c, T.charge, T.ta);
    const burst = inv(c, T.burst, T.burst + (reduced ? 0.3 : 0.5));
    const qAlive = 1 - easeInQuad(inv(c, T.burst, T.burst + 0.45));

    // --- the Q: JS drives the trace so the clock can be held or resumed -
    if (s.mark) {
      const breathe = s.held ? 1 + 0.03 * Math.sin(wall * 5.4) : 1;
      const pop = 1 + 0.95 * easeInQuad(burst);
      const scale = lerp(0.72, 1, easeOutCubic(inv(c, 0, T.ringDone))) * breathe * pop;
      s.mark.style.transform = "scale(" + scale.toFixed(4) + ")";
      s.mark.style.opacity = (qAlive * (0.2 + 0.8 * easeOutCubic(inv(c, 0, 0.5)))).toFixed(3);
      trace(s.ring, 1 - ringIn);
      trace(s.tail, 1 - tailIn);
      if (s.core) {
        // the core ignites as the ring closes and blows out on the TA
        const ignite = easeOutCubic(inv(c, T.charge, T.ta));
        const kick = 1 + 2.4 * easeInQuad(burst);
        s.core.style.opacity = (qAlive * (0.12 + 0.88 * Math.max(ignite, inv(c, T.ring, T.charge) * 0.35))).toFixed(3);
        s.core.style.transform = "scale(" + (lerp(0.4, 1.15, ignite) * kick).toFixed(3) + ")";
      }
    }

    g.globalCompositeOperation = "lighter";

    // --- light behind the Q, which becomes the light show ---------------
    const pulse = s.held ? 0.62 + 0.3 * Math.sin(wall * 5.6) : 1;
    // gather, don't fog: the halo tightens over the last third of the charge so
    // the eye is on the mark itself when it blows apart
    const gather = 1 - 0.42 * inv(c, T.charge + 0.22, T.ta);
    drawGlow(g, w / 2, qy, qs * (0.9 + 0.95 * charge) * pulse,
      "hsla(276, 92%, 72%, @)",
      (0.4 * Math.min(1, ringIn * 1.6) + 0.5 * charge) * qAlive * gather);
    drawGlow(g, w / 2, qy, qs * (0.45 + 0.55 * charge),
      "hsla(188, 96%, 70%, @)", 0.22 * ringIn * qAlive * gather);

    // --- shockwave off the burst ---------------------------------------
    if (burst > 0 && burst < 1) {
      const radius = lerp(qs * 0.35, Math.max(w, h) * 0.85, easeOutCubic(burst));
      g.strokeStyle = "hsla(196, 100%, 86%, " + (0.5 * (1 - burst)).toFixed(3) + ")";
      g.lineWidth = lerp(7, 1, burst);
      g.beginPath();
      g.arc(w / 2, qy, radius, 0, Math.PI * 2);
      g.stroke();
    }

    // --- the vertical spectrum -----------------------------------------
    const driven = sound ? sound.levels(s.beams.length, s.levels) : false;
    for (let i = 0; i < s.beams.length; i += 1) {
      const beam = s.beams[i];
      const launch = T.burst + beam.delay * T.sweep;
      const u = reduced ? 1 : inv(c, launch, launch + T.launch);
      if (u <= 0) continue;
      const rise = grow(u);

      // real FFT energy while the ident is audible, a synthetic wiggle when not
      let level;
      if (driven) {
        level = 0.44 + 0.9 * s.levels[i];
      } else {
        const wob = 0.5
          + 0.5 * Math.sin(c * beam.speed * 2.2 + beam.phase)
          + 0.22 * Math.sin(c * beam.speed * 5.1 + beam.phase * 1.7);
        level = 0.6 + 0.42 * wob;
      }
      const relax = 1 - 0.22 * inv(c, T.settle, T.fade);
      const len = (horizon - h * 0.05) * beam.shape * (0.5 + 0.62 * level) * rise * relax;
      if (len <= 1) continue;

      const top = horizon - len;
      const bw = Math.max(3.2, (w / s.beams.length) * 0.62);
      const frac = beam.frac;

      // 1. halo — the "bloom", faked with a wide cheap rect; adjacent halos
      //      overlap on purpose so the spectrum reads as one wall of light
      const halo = g.createLinearGradient(0, horizon, 0, top);
      halo.addColorStop(0, beamColor(frac, 62, 0.21 * rise));
      halo.addColorStop(0.45, beamColor(frac, 66, 0.09 * rise));
      halo.addColorStop(1, beamColor(frac, 70, 0));
      g.fillStyle = halo;
      g.fillRect(beam.x - bw * 2.2, top, bw * 5.4, len);

      // 2. the beam
      const core = g.createLinearGradient(0, horizon, 0, top);
      core.addColorStop(0, beamColor(frac, 92, 0.95 * rise));
      core.addColorStop(0.12, beamColor(frac, 80, 0.86 * rise));
      core.addColorStop(0.58, beamColor(frac, 66, 0.6 * rise));
      core.addColorStop(1, beamColor(frac, 62, 0.04 * rise));
      g.fillStyle = core;
      g.fillRect(beam.x - bw / 2, top, bw, len);

      // 3. a white-hot foot at the base, and a faint glint travelling upwards
      const spine = Math.max(0.8, bw * 0.2);
      const foot = len * 0.4;
      const hot = g.createLinearGradient(0, horizon, 0, horizon - foot);
      hot.addColorStop(0, "hsla(0, 0%, 100%, " + (0.5 * rise).toFixed(3) + ")");
      hot.addColorStop(0.55, "hsla(0, 0%, 100%, " + (0.16 * rise).toFixed(3) + ")");
      hot.addColorStop(1, "hsla(0, 0%, 100%, 0)");
      g.fillStyle = hot;
      g.fillRect(beam.x - spine / 2, horizon - foot, spine, foot);
      if (!reduced) {
        const run = (c * 0.55 + beam.phase) % 1;
        const glint = (1 - Math.abs(0.5 - run) * 2) * rise;
        if (glint > 0.02) {
          g.fillStyle = beamColor(frac, 97, 0.16 * glint);
          g.fillRect(beam.x - bw * 0.36, horizon - run * len - len * 0.08, bw * 0.72, len * 0.16);
        }
      }

      // caps, the pool of light on the floor, and the mirror below it
      drawGlow(g, beam.x, top, bw * 1.7, "hsla(" + hueAt(frac).toFixed(1) + ", 100%, 88%, @)", 0.34 * rise);
      drawGlow(g, beam.x, horizon, bw * 5.4, "hsla(" + hueAt(frac).toFixed(1) + ", 100%, 72%, @)", 0.42 * rise);
      const mirror = g.createLinearGradient(0, horizon, 0, horizon + len * 0.26);
      mirror.addColorStop(0, beamColor(frac, 70, 0.24 * rise));
      mirror.addColorStop(1, beamColor(frac, 60, 0));
      g.fillStyle = mirror;
      g.fillRect(beam.x - bw / 2, horizon, bw, len * 0.26);
    }

    // --- the floor line the spectrum stands on -------------------------
    const floorIn = inv(c, T.burst + 0.1, T.burst + 0.7);
    if (floorIn > 0) {
      const spread = easeOutCubic(floorIn);
      const half = (w / 2) * spread;
      const line = g.createLinearGradient(w / 2 - half, 0, w / 2 + half, 0);
      line.addColorStop(0, "hsla(276, 96%, 70%, 0)");
      line.addColorStop(0.5, "hsla(190, 100%, 88%, " + (0.5 * (1 - 0.45 * inv(c, T.settle, T.fade))).toFixed(3) + ")");
      line.addColorStop(1, "hsla(48, 96%, 70%, 0)");
      g.fillStyle = line;
      g.fillRect(w / 2 - half, horizon - 1, half * 2, 1.8);
    }

    // --- dust kicked up by the burst -----------------------------------
    if (!reduced && s.motes.length) {
      const dust = inv(c, T.burst, T.end);
      if (dust > 0) {
        for (let i = 0; i < s.motes.length; i += 1) {
          const m = s.motes[i];
          const life = (dust * 1.6 + m.phase) % 1;
          const alpha = 0.45 * Math.sin(Math.PI * life) * (1 - dust * 0.5);
          if (alpha <= 0.01) continue;
          g.fillStyle = beamColor(m.hue, 86, alpha);
          g.beginPath();
          g.arc(m.x + Math.sin(life * 3 + m.phase) * 9, m.y - life * m.drift * 3.4, m.r, 0, Math.PI * 2);
          g.fill();
        }
      }
    }

    // --- the TA flash ---------------------------------------------------
    const flash = 1 - inv(c, T.ta, T.ta + 0.26);
    if (flash > 0) {
      const power = 0.4 * Math.pow(flash, 2.8);
      const bloom = g.createRadialGradient(w / 2, qy, 0, w / 2, qy, Math.max(w, h) * 0.62);
      bloom.addColorStop(0, "hsla(200, 100%, 98%, " + power.toFixed(3) + ")");
      bloom.addColorStop(0.45, "hsla(268, 100%, 92%, " + (power * 0.45).toFixed(3) + ")");
      bloom.addColorStop(1, "hsla(268, 100%, 90%, 0)");
      g.fillStyle = bloom;
      g.fillRect(0, 0, w, h);
    }

    g.globalCompositeOperation = "source-over";
  }

  /* ------------------------------------------------------------------ *
   * Lifecycle, unlock dance, replay switch
   * ------------------------------------------------------------------ */
  function prefersReduced() {
    return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }

  function store(key, value) {
    try {
      if (value === null) sessionStorage.removeItem(key);
      else sessionStorage.setItem(key, value);
    } catch (error) { /* private mode */ }
  }

  function read(key) {
    try { return sessionStorage.getItem(key); } catch (error) { return null; }
  }

  function mark(el, cls, on) {
    if (el && el.classList) el.classList.toggle(cls, !!on);
  }

  /* Set the same dash offset on a node list (the sharp stroke and its halo). */
  function trace(list, offset) {
    if (!list) return;
    const value = offset.toFixed(4);
    for (let i = 0; i < list.length; i += 1) list[i].style.strokeDashoffset = value;
  }

  function applyClasses(c) {
    const s = state;
    if (!s) return;
    mark(s.lockup, "in", c >= T.word);
    mark(s.lockup, "tag", c >= T.tag);
    mark(s.overlay, "dim", c >= T.fade);
  }

  function frame(ts) {
    const s = state;
    if (!s || s.done || !sound) return;
    const dt = Math.min(0.05, s.last ? (ts - s.last) / 1000 : 0.016);
    s.last = ts;
    s.wall += dt;

    if (s.held) {
      // waiting for a tap: leave as soon as audio is live, never longer
      // than holdMax so the app is never held hostage by a muted tab
      if (sound.ready || s.wall - s.heldAt >= T.holdMax) {
        s.held = false;
        s.gated = true;
        mark(s.hint, "show", false);
        sound.start(s.clock);
      }
    } else if (!s.gated && !sound.ready && !sound.playing && !sound.muted()
               && s.clock >= T.holdGate) {
      s.held = true;
      s.heldAt = s.wall;
      mark(s.hint, "show", true);
    } else if (sound.ready && !sound.playing && s.clock < T.end - 0.5) {
      // unlocked late: bring the score in on the beat it is still on
      sound.start(s.clock);
    }

    // The clock stops while the ident waits for audio, so the TA still lands
    // on the burst after a mobile unlock instead of running away from it.
    if (!s.held) s.clock += dt;

    render(s.clock, s.wall);
    applyClasses(s.clock);

    if (s.clock >= s.endAt) {
      finish(false);
      return;
    }
    s.raf = window.requestAnimationFrame(frame);
  }

  function finish(skipped) {
    const s = state;
    if (!s || s.done) return;
    s.done = true;
    if (s.raf) window.cancelAnimationFrame(s.raf);
    s.raf = 0;
    if (skipped && sound) sound.halt();
    mark(s.overlay, "dismissed", true);
    if (s.persist) store(STORE_SEEN, "1");
    detach(s);
    // The overlay stays in the DOM (hidden by `visibility`, so it costs no
    // paint) which is what makes an instant, asset-free replay possible.
    state = null;
    if (typeof s.onDone === "function") s.onDone(skipped);
  }

  function detach(s) {
    document.removeEventListener("pointerdown", onGesture);
    document.removeEventListener("touchstart", onGesture);
    document.removeEventListener("keydown", onKey);
    if (s && s.overlay) s.overlay.removeEventListener("click", onPointer);
    if (s && s.onResize) window.removeEventListener("resize", s.onResize);
  }

  function unlock() {
    const s = state;
    if (!s || !sound) return;
    sound.arm().then((live) => {
      const now = state;
      if (!live || !now || now.done || sound.playing) return;
      now.held = false;
      now.gated = true;
      mark(now.hint, "show", false);
      sound.start(now.clock);
    });
  }

  function onGesture() {
    const s = state;
    if (!s || s.done) return;
    if (!sound || !sound.playing) unlock();
  }

  function onKey(event) {
    const s = state;
    if (!s || s.done) return;
    if (event.key === "Escape") finish(true);
    else if (!sound.playing) unlock();
  }

  /* A tap only skips once the sound is on its way or already gone, so the
   * gesture that unlocks audio is never also the gesture that cancels it. */
  function onPointer() {
    const s = state;
    if (!s || s.done) return;
    if (s.held || !sound.playing) {
      unlock();
      return;              // never trade the ta-dum for a skip
    }
    finish(true);
  }

  function play(options) {
    const opts = options || {};
    const overlay = document.getElementById("introOverlay");
    if (!overlay) return null;

    if (state) {
      if (state.raf) window.cancelAnimationFrame(state.raf);
      detach(state);
      state.done = true;
      state = null;
    }
    if (sound) {
      sound.dispose();
      sound = null;
    }
    sound = createSound();
    if (opts.muted === false) sound.setEnabled(true);
    if (opts.muted === true) sound.setEnabled(false);

    const canvas = overlay.querySelector(".intro-stage");
    const reduced = prefersReduced() || opts.reduced === true;
    let stage = null;
    if (canvas) {
      overlay.classList.add("live");
      stage = sizeStage(canvas, overlay);
    }
    const count = stage ? beamCount(stage.w, reduced) : 0;

    state = {
      overlay,
      canvas,
      stage,
      dpr: stage ? clamp(window.devicePixelRatio || 1, 1, 2) : 1,
      hint: overlay.querySelector(".intro-hint"),
      lockup: overlay.querySelector(".intro-lockup"),
      mark: overlay.querySelector(".intro-q-mark"),
      ring: overlay.querySelectorAll(".intro-q-ring, .intro-q-halo"),
      tail: overlay.querySelectorAll(".intro-q-tail"),
      core: overlay.querySelector(".intro-q-core"),
      clock: 0,
      wall: 0,
      last: 0,
      raf: 0,
      held: false,
      heldAt: 0,
      gated: false,
      done: false,
      reduced,
      persist: opts.persist !== false,
      onDone: typeof opts.onDone === "function" ? opts.onDone : null,
      beams: stage ? makeBeams(stage.w, stage.h, count) : [],
      motes: stage ? makeMotes(reduced ? 0 : 64, stage.w, stage.h) : [],
      levels: new Float32Array(Math.max(1, count)),
    };

    overlay.classList.remove("dismissed", "dim");
    mark(state.lockup, "in", false);
    mark(state.lockup, "tag", false);

    state.onResize = () => {
      const s = state;
      if (!s || !s.canvas || s.done) return;
      s.stage = sizeStage(s.canvas, s.overlay);
      s.beams = makeBeams(s.stage.w, s.stage.h, s.beams.length);
      s.motes = makeMotes(s.reduced ? 0 : 64, s.stage.w, s.stage.h);
    };
    window.addEventListener("resize", state.onResize);

    document.addEventListener("pointerdown", onGesture, { passive: true });
    document.addEventListener("touchstart", onGesture, { passive: true });
    document.addEventListener("keydown", onKey);
    overlay.addEventListener("click", onPointer);

    // Motion-sensitive users skip the slow trace and the dust, but still get
    // the burst, the beams and the ta-dum — the ident is mostly a sound cue.
    state.endAt = reduced ? T.word + 0.9 : T.end;
    if (reduced) {
      state.clock = T.ta - 0.06;
      state.gated = true;
      render(state.clock, 0);
      applyClasses(state.clock);
    }

    unlock();
    state.raf = window.requestAnimationFrame(frame);
    return state;
  }

  document.addEventListener("visibilitychange", () => {
    if (state && document.hidden && sound && sound.playing) sound.halt();
  });

  window.QyroIdent = {
    T,
    version: "6.3-spectrum",
    isPlaying() { return !!state && !state.done; },
    soundEnabled() {
      if (sound) return sound.enabled();
      // No context yet (the ident has not run): read the stored preference.
      try { return localStorage.getItem(STORE_SOUND) !== "0"; } catch (error) { return true; }
    },
    setSoundEnabled(value) {
      if (!sound) sound = createSound();
      sound.setEnabled(value);
    },
    play,
    skip() { finish(true); },
    /* Header button / ?intro=1: forget the session flag and run it again. */
    replay() {
      store(STORE_SEEN, null);
      if (state) {
        if (state.raf) window.cancelAnimationFrame(state.raf);
        detach(state);
        state.done = true;
      }
      state = null;
      if (!document.getElementById("introOverlay")) return null;
      return play({ persist: false });
    },
    /* Called once from app.js: shows the ident at most per browser session. */
    boot() {
      const overlay = document.getElementById("introOverlay");
      const forced = new URLSearchParams(window.location.search).get("intro") === "1";
      if (!forced && read(STORE_SEEN) === "1") {
        // Returning visitor: the stage is still in the markup, so it has to be
        // put away here — otherwise every reload shows a black sheet for the
        // whole fail-safe timeout instead of the app.
        if (overlay) {
          overlay.classList.add("dismissed", "live");
        }
        return null;
      }
      return play(forced ? { persist: false } : {});
    },
  };
})();
