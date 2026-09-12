# Qyro v6.6 — Upgrade Guide (the stuck rainbow screen, and the retired Gemini model)

The install/verify path for **v0.6.6**. Two user reports, both of which made a
working app look broken:

* *"When I am downloading any video in the app it's only a VIBGYOR screen at
  which a beep sound is coming."*
* *`AI unavailable during render (HTTP 404 … This model models/gemini-2.5-flash
  is no longer available to new users …)`* after pasting a Gemini key.

No render geometry, option or route contract changed.

## What changed in v6.6

1. **The ident can no longer strand the app (`web/intro.js`, `web/style.css`,
   `web/app.js`).** The spectrum ident is a full-screen animation, and it used
   to advance its timeline one clamped step per `requestAnimationFrame`
   callback. A phone encoding a short has no idle main thread, so six seconds
   of animation stretched into a minute of stuck rainbow beams with the
   synthesised ta-dum still playing on the audio thread — the "VIBGYOR screen
   with a beep". Four fixes, all pinned by `tests/ident_harness.js`:
   - the timeline now **catches up with the wall clock** instead of replaying
     dropped frames (`CATCHUP_SLACK`), so a 4 fps device sees a short ident, not
     a long one;
   - a plain **`setTimeout` deadline** dismisses the stage even if rAF never
     fires again (backgrounded tab, frozen tab), and the timer is cleared on the
     normal path;
   - **a hidden tab is put away** immediately, and a canvas whose context is
     lost ends the ident on the spot instead of throwing out of the frame loop;
   - the CSS is **opt-in**: `.intro-overlay` is `visibility: hidden` until
     `QyroIdent.play()` adds `.live`, with a second pure-CSS `introHardHide`
     lock behind it. A stale offline shell, a blocked script or a reload that
     dies mid-parse now shows the app, never a sheet over it.
   Plus: `app.js` decides *after* the first `/api/state`, and skips the auto-play
   entirely while a job is queued or running (the ident never waits between you
   and a render); the "seen" stamp is also kept in `localStorage` for five
   minutes, so a reload storm cannot replay it. The header sparkle button and
   `?intro=1` still play it on demand.
2. **Demo media is labelled (`autoshorts/ffmpeg.py`, `pipeline.py`,
   `maintenance.py`, `web/app.js`).** The offline demo "episode" is a
   `testsrc2` colour-bar card with a sine tone — which is exactly what the
   report describes, and it was indistinguishable from a broken render.
   - the placeholder is now burned with the line
     **"DEMO - SYNTHETIC TEST MEDIA, NOT A REAL VIDEO"** through libass (the
     same filter the captions use), with a `drawtext` and then a white-bar
     fallback so a build without either still gets a marked card and never a
     failed render;
   - `data/media/<id>.mp4.qyro-demo.json` marks the file on disk, so
     "a cached file exists" can never be confused with "the real footage was
     downloaded" — `youtube.download_video` re-downloads over a placeholder, and
     the pipeline refuses to cut a short out of one for a YouTube episode;
   - every clip stores `source` / `demo_media`, `/api/state` fills both in for
     old clips too (from the episode), and the short's card, the preview flag,
     the inspector and the Demo button all say what the footage is.
3. **A download downloads (`autoshorts/server.py`, `server_stdlib.py`).**
   `GET /api/clips/{id}/file?dl=1` answers `Content-Disposition: attachment`;
   the plain URL stays `inline` for the `<video>` element. Android WebViews
   ignore a link's `download` attribute, so the header is what makes the tap
   save a file. The FastAPI backend also stops sending `attachment` for the
   inline case (Starlette's default), which is what the stdlib server already
   did — both backends now behave identically.
4. **Retired AI model ids rescue themselves (`autoshorts/config.py`,
   `engine.py`, `maintenance.py`).** A `models/gemini-2.5-flash` left in
   `data/state.json` (older default, or the Settings form re-posting what it was
   prefilled with) pinned every call to a 404 and demoted every render to the
   offline pack.
   - `config.is_retired_model()` knows the retired Gemini 1.x/2.x and Groq
     ids; `normalise_model()` strips the `models/` prefix Google writes into its
     error text (a pasted id built a double path segment and 404'd);
   - `engine.model_candidates()` orders the ids to try: a retired id is demoted
     behind the current free-tier default, a live-but-unknown id gets its honest
     first attempt with the default behind it. A refused *key* or an exhausted
     quota is not retried across models — that would only waste the render;
   - failures read like sentences now (`_humanise`):
     `Google AI Studio (Gemini) does not serve gemini-2.5-flash (HTTP 404) —
     leave Model blank in Settings to follow the current free-tier default`
     instead of a JSON dump;
   - `/api/state` adds `engine.model_effective` and `engine.model_note`, the
     Settings field is prefilled with the model that is actually called, and a
     **Follow the current default instead** button clears the stale value. The
     upload pack's `polished_by` names the model that answered.
5. **An empty render can no longer be filed as a finished short.** A source file
   shorter than the moment being cut from it (a truncated download, a stale
   placeholder of another length) used to produce a few hundred bytes of mp4, a
   stored clip, a black player and a job marked `done`. `pipeline._render_one`
   now checks the output exists, is big enough and reports a playable duration,
   and raises a message naming the file to delete; `synth_demo_video`
   re-synthesises when the cached card's recorded length disagrees with the
   transcript it is about to serve.
6. **Upgrade-safe.** Shell cache `qyro-v0.6.5-shell` → `qyro-v0.6.6-shell`,
   `APP_VERSION`/`__version__` `0.6.6`, the ident reports `6.6-spectrum`.

## Install (desktop / laptop)

```bash
git pull                      # get v0.6.6
python -m pip install -r requirements.txt
./run.sh                      # Windows: run.bat
```

Open http://127.0.0.1:8000 — the intro plays once, then hands the page back.

## Install (Android / Termux)

```bash
pkg update && pkg install python ffmpeg git
cd ~/autoshorts && git pull
bash install-android.sh
python run.py
```

Then open the local URL in Chrome. Because the shell cache is versioned, an
installed home-screen icon picks up the new `intro.js`/`style.css`/`app.js` on
the first launch after the server comes back — which matters here, since the
broken behaviour lived in the cached shell.

## Verify in 60 seconds

1. **The ident lets go.** Reload the page while a render is running: the app
   opens on the job, not on the intro. Tap the sparkle button in the header to
   play the ident deliberately, then switch to another app mid-ident and back —
   the stage is gone, not frozen. (`tests/ident_harness.js` drives all of this
   headlessly, including a 4 fps device and a run where rAF never returns.)
2. **Demo is obviously demo.** Press **Demo**, generate a short, download it:
   the file says `DEMO - SYNTHETIC TEST MEDIA, NOT A REAL VIDEO` in the middle
   of the picture, the card carries a red *demo media* flag, and the inspector's
   *Source media* row says `demo test card`. `data/media/` also gains
   `demo-*.mp4.qyro-demo.json`.
3. **The key works.** ⚙ Settings → *Google AI Studio key* → paste an `AQ…` key →
   Save. If your `data/state.json` still names a retired model, the Model box now
   shows `gemini-3.6-flash` with a one-line note and a *Follow the current
   default* button. Generate any short: the pack comes back polished, or the
   notice is a sentence you can act on — not a JSON blob.
4. **A dead render reports itself.** Truncate any file in `data/media/` (or use
   an old short demo card), generate, and the job ends with an error naming
   `data/media/<id>.mp4` instead of filing an empty clip.
5. **Tests** — `python -m tests.run_all` → `ALL GREEN ✅ — 498 tests` (56 new in
   `tests/test_units_v066.py`).

## Rollback

`git checkout v0.6.5` (or the previous commit) and restart. Keys and settings in
`data/state.json` are untouched by the upgrade; the only new files on disk are
the `*.qyro-demo.json` markers, which can be deleted freely (a demo card is
simply re-synthesised with its label).
