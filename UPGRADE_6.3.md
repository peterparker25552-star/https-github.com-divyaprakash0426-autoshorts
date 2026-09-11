# Qyro v6.3 — Upgrade Guide (the spectrum ident)

This is the install/verify path for **v0.6.3**, which rebuilds the app intro: a
glowing **Q** on black that expands outward into a colourful **spectrum of vertical
light beams**, with a **ta-dum** you can actually hear. v6.2 had the animation and
*silently dropped the sound* — this release fixes the cause and replaces the reveal.

## What changed in v6.3

1. **New ident — `web/intro.js` (new file, ~900 lines, zero dependencies)**
   - One `requestAnimationFrame` clock drives everything: the Q's `stroke-dashoffset`
     trace, the canvas beams and the audio scheduling. Because one clock drives both,
     a delayed audio unlock cannot desync the hit from the burst.
   - Canvas stage: additive ( `globalCompositeOperation = "lighter"` ) vertical beams,
     hue `345 → 285` across the screen (red → violet), centre-outwards stagger,
     overshoot easing on the rise, mirror + floor pool + cap bloom, travelling highlight,
     dust motes, shockwave ring, white flash on the *ta*.
   - Beam height is driven by an `AnalyserNode` tapped off the master bus, so the
     spectrum is genuinely reacting to the sound; with no audio it falls back to a
     synthetic equaliser so it never freezes.
   - Perf guards for phones: DPR clamped to `1..2`, no animated `filter`/`backdrop-filter`,
     `contain: strict`, 26–58 beams chosen from viewport width.
2. **Sound design — synthesised, no asset shipped**
   - *ta*: noise snap (high-pass 1.5 kHz) + tom `244→128 Hz` + a metal fleck at 1.76 kHz.
   - *dum*: body `162→44 Hz`, `41 Hz` sub with a 2.2 s tail, low-passed noise thump.
   - Plus: charge rumble, a riser into the burst, **one pentatonic pluck per beam**
     (13 notes, stereo-panned in beam order, so the spectrum is also a scale), sparkle
     air, a resolving A-minor chord under the wordmark and a low breath on the fade.
   - Bus: `DynamicsCompressor` (-13 dB, 7:1) → destination, with a procedurally
     generated 2.6 s stereo noise impulse through a `ConvolverNode` for the hall tail.
3. **The "no sound" fix (root cause)**
   - v6.2's `initIntro()` registered `document.addEventListener("pointerdown", unlock, { once: true })`
     *and* `overlay.addEventListener("click", () => dismiss(true))`, where `dismiss(true)`
     set `introAudioCancelled = true`. The overlay covers the whole screen, so the first
     tap both unlocked the context and cancelled the cue → silence, every time on mobile.
   - v6.3: while audio is blocked the first gesture **only** unlocks (it can never skip);
     skipping needs a later tap, the **Skip** button, or `Esc`. If audio is still blocked at
     `t = 1.80 s` the frame is **held for up to 0.9 s** and then released, so the intro
     always finishes even with no gesture and no sound. A deliberate mute (see 4) skips the
     hold entirely — the gate is only for browsers that refuse autoplay.
   - `visibilitychange` halts a half-played score instead of letting it resume mid-hit.
4. **New UI controls**
   - `#identBtn` in the header — replay the ident (also shows the muted state).
   - Settings → **ident sound on/off**, persisted in `localStorage["qyro.identSound"]`.
   - "Tap anywhere to hear the ident" hint, only while the browser is waiting for a gesture.
   - `?intro=1` preview switch is kept, and the seen-flag is not written for a forced replay.
5. **Housekeeping**
   - Version `0.6.2 → 0.6.3` (`autoshorts/__init__.py`, `autoshorts/config.py`).
   - Service worker shell `qyro-v0.6.2-cinematic-shell → qyro-v0.6.3-spectrum-shell`, and
     `/static/intro.js` added to the precached assets so an installed PWA cannot keep the
     silent v6.2 shell.
   - CSS fail-safe `.intro-overlay:not(.live)` clears the black stage 6.5 s after load if
     `intro.js` never runs — the app can never be covered by a dead overlay.
   - `tests/ident_harness.js` (new) + `tests/test_units_v063.py` (new, wired into
     `tests/run_all.py`); `tests/test_units_v061.py` version floor updated.
   - `web/index.html` and `web/style.css` lost the v6.2 ribbon/petal intro entirely.

## Design note: original, not copied

The pacing idea — a black stage, a mark that blooms, and a short two-hit audio
signature — is what was asked for. Everything shipped here is **generated from
scratch**: the Q is Qyro's own geometry (a traced ring plus a slash tail on a
red-to-violet ramp), the beams are Qyro's own renderer, and the ta-dum is
synthesised here from oscillators and noise with the frequencies listed above.
It is not a reproduction of any streamer's logo animation, and it does not use
any of their audio: that sound is a registered trademark, and a recorded copy of
it would put the repo at risk. Re-running `python3 tools/make_logo.py` can
regenerate the icon set from the same original vectors if you ever want to tweak
the silhouette.

## 1 — Pull it (do this after the PR is merged)

```bash
git fetch origin
git checkout main
git pull origin main
```

On a fork of the repo instead:

```bash
git pull upstream main --ff-only
```

Before the merge exists, you can already run the branch this came from:

```bash
git fetch origin arena/01a090ae-https-github-com-divyaprakash0
git checkout arena/01a090ae-https-github-com-divyaprakash0
```

## 2 — Check you really got 6.3

```bash
python3 -c "from autoshorts import __version__; print(__version__)"   # -> 0.6.3
grep -c "intro.js" web/index.html web/sw.js                           # -> 1 and 1
test -f web/intro.js && echo "ident present"
```

No new Python dependency and no new binary asset: `requirements.txt` is unchanged, and
the ident is HTML canvas + CSS + WebAudio only.

## 3 — Desktop / laptop install

```bash
./install.sh          # macOS & Linux  (creates ./.venv, installs deps)
./run.sh              # then open http://localhost:8000
```

Windows: double-click `install.bat`, then `run.bat`.

If you already installed an earlier version, nothing needs reinstalling for v6.3 —
the change is frontend + version string. `./run.sh` is enough.

## 4 — Android (Termux) install

In **Termux from F-Droid** (`https://f-droid.org/packages/com.termux/`), one line:

```bash
pkg update -y && pkg install -y curl && curl -fsSL https://raw.githubusercontent.com/peterparker25552-star/https-github.com-divyaprakash0426-autoshorts/main/android-install.sh -o "$HOME/autoshorts-android-install.sh" && bash "$HOME/autoshorts-android-install.sh"
```

It re-clones the repo, so it always lands on the merged version, and the banner prints
whatever `autoshorts.__version__` says (should be `v0.6.3`). Afterwards:

```bash
bash ~/start-autoshorts.sh
```

then open `http://localhost:8000` in Chrome. To run the branch before the merge, export
the ref first:

```bash
AUTOSHORTS_REF=arena/01a090ae-https-github-com-divyaprakash0 bash "$HOME/autoshorts-android-install.sh"
```

## 5 — Clear the old shell cache (important for the intro!)

The PWA caches the app shell. If you used 6.2 in this browser, **force the new shell**,
otherwise you may still see the old ribbon intro:

- Easiest: open `http://localhost:8000/?intro=1` — a hard reload (`Ctrl+Shift+R` /
  pull-to-refresh on Android) so `sw.js` re-registers under the new cache name.
- Or DevTools → **Application** → **Storage** → *Clear site data*, then reload.
- Or DevTools → Application → Service Workers → **Unregister**, reload.
- No DevTools on Android? Chrome → `⋮` → History → Clear browsing data → *Cached images
  and files*, then reload once.

The cache name change normally handles this by itself; the steps above are the manual
version of the same thing.

## 6 — Verify the ident by hand

1. Load the app in a **fresh tab** (or add `?intro=1`): black stage, the Q traces
   itself, charges, then bursts into the spectrum; `QYRO` + tagline land underneath.
2. You should hear the ta-dum *without* tapping. On desktop Chrome/Safari and on
   Firefox the first play usually needs one click/keypress anywhere — the "Tap anywhere
   to hear the ident" pill appears while it waits; the tap starts the sound and the
   intro **keeps going** rather than skipping.
3. Tap the **Skip** button (or `Esc`) mid-roll → the sound fades out immediately, the app
   is revealed.
4. Press the **sparkle button** in the header → the ident replays with sound in the same
   session (this is the fastest way to A/B the audio).
5. Settings → untick **ident sound on** → replay → visuals only, and it stays silent on
   reload (remembered in `localStorage`).
6. `chrome://flags` / Safari "Block all auto-play" or a browser tab opened in the
   background: the ident must still finish on its own after ~0.9 s of waiting.
7. Phone: rotate mid-roll — the canvas re-sizes and the beam count follows the new width.
8. Reduced motion (`prefers-reduced-motion`): short reveal, beams at rest, ta-dum intact.

## 7 — Verify with the tests

```bash
python3 -m tests.run_all          # everything, incl. the ident harness (needs node)
python3 -m unittest tests.test_units_v063 -v
node tests/ident_harness.js       # the headless ident run: 11 scenarios + verdict
```

The harness prints one JSON record per scenario and exits non-zero if: no cues were
scheduled, `resume()` refusal left the app covered, the unlock tap skipped the intro,
the ta→dum spacing drifted, or the beams were never painted. `node` is optional for the
Python suite (those tests skip without it).

## If you still hear nothing

- **System/media volume and the tab's own mute**: Chrome can mute a single tab
  (right-click the tab → *Unmute sound tab*).
- **Settings → ident sound on** must be ticked; the sparkle button goes dim when off.
- **`http://`, not `file://`** — opening `web/index.html` from disk gives no origin, and
  some browsers refuse audio there. Always use `http://localhost:8000`.
- **Old shell** → redo step 5.
- **Android**: some WebView builds refuse `AudioContext` entirely until a gesture; the
  pill tells you to tap, and the first tap starts the score. If a ROM still swallows it,
  `node tests/ident_harness.js` proves the schedule is correct, which localises the
  problem to the browser's policy rather than Qyro.
- Quick console check: `QyroIdent.soundEnabled()` → `true`, and
  `QyroIdent.replay()` should replay it.

## API / behaviour changes

None. No endpoint, payload or stored key changed; `captions_enabled` and every v6.2
render option behave identically. This release is frontend + version string + docs.

## Files changed in v6.3

| File | Change |
| --- | --- |
| `web/intro.js` | **new** — the spectrum ident: timeline, canvas beams, WebAudio score, unlock gate |
| `web/index.html` | intro markup replaced (canvas stage + Q + lockup + hint + skip); `intro.js` script tag; `#identBtn` in the header |
| `web/style.css` | v6.2 intro CSS replaced by the v6.3 ident block, incl. the `.live` fail-safe |
| `web/app.js` | old hand-rolled intro synth removed; thin `QyroIdent` wiring + replay/sound controls |
| `web/sw.js` | shell cache renamed, `/static/intro.js` precached |
| `autoshorts/__init__.py`, `autoshorts/config.py` | `0.6.2 → 0.6.3` + release notes |
| `tests/ident_harness.js`, `tests/test_units_v063.py` | **new** — headless ident tests + file-contract tests |
| `tests/run_all.py`, `tests/test_units_v061.py` | register the new suite, check `intro.js` with node, version floor |
| `README.md`, `ANDROID-GUIDE.md`, `install.sh` | version wording and the new feature section |
| `UPGRADE_6.3.md` | **new** — this file |
