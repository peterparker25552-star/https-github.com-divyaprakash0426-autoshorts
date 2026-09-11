<p align="center">
  <img src="web/logo.svg" alt="Qyro" width="88" height="88">
</p>

# Qyro

*formerly AutoShorts — same repo, same installers, new name.*

Turn long YouTube podcast episodes into **short, captioned, vertical (9:16) clips** — automatically.

Built around the seed playlist [**Figuring Out With Raj Shamani**](https://www.youtube.com/playlist?list=PLE0Jo6NF_JYO5-phess8GKafKMtPv3tfZ) (562 episodes), but works with **any** YouTube playlist or video.

```text
playlist URL ──▶ yt-dlp ──▶ transcripts ──▶ highlight engine ──▶ ffmpeg ──▶ 9:16 shorts
                (media +     (auto-captions)  (hooks · numbers ·    (crop/blur +
                 captions)                     questions · energy)   burned-in captions)
```

## What's new in v0.6.2 — a cinematic Qyro identity

- **Original orbit logo.** A soft six-petal form, open Q-shaped orbit, curved cyan tail and cross-spark replace the old star/play mark. The same vector geometry is used by the header, intro, favicon and regenerated PWA icons.
- **Premium studio intro.** A black ribbon sweep reveals the mark, QYRO wordmark and descriptor in a paced 5.6-second sequence, then fades cleanly into the app. It is Netflix-inspired in pacing, but uses original Qyro artwork and sound.
- **Longer, synchronized sound.** A single WebAudio context now builds the whoosh, two-note hit and a 5.5-second bass/resonance tail. Mobile autoplay unlocks the same scheduled sound on the first gesture, so repeated clicks cannot create overlapping or prematurely cut-off effects.
- **Smoother mobile transition.** The intro avoids animated blur and backdrop filters; its reveal uses compositor-friendly opacity, transforms and stroke offsets. The service-worker shell name is bumped so installed PWAs fetch the new intro assets.

## What's new in v0.6.1 — the camera follows the *speaker*

- **Speaker-aware tracking.** v0.6.0 followed *a* person: the biggest face in
  frame (which jumps whenever someone leans closer to the camera) or the middle
  of the skin+motion blob (which frames nobody in a two-person shot). The
  tracker now answers "who is talking?" entirely offline: the ffmpeg heat pass
  emits separate skin and motion maps, connected components on the skin map
  find up to three distinct people, one `ebur128` pass over the clip's own
  audio yields voice-active/idle flags, and each candidate is scored by the
  motion it makes *while the voice is active* — the speaker's head and hands
  move on their words, a listener sits still. The camera follows the winner and
  hands over (a smooth pan, never a snap) when the other person clearly takes
  the turn; with no speech in the audio it follows whoever is most persistently
  in frame. The optional OpenCV Haar backend now feeds its *face detections*
  through the same speaker scoring instead of blindly picking the biggest face
  per frame. On a synthetic two-person clip where the turn changes at 12 s, the
  tracked camera sits at x=0.20 through the first speaker's turn and glides to
  x=0.66 within 0.4 s of the hand-off (`switches: 1`, confidence 0.94).
- **"AI unavailable during render" fixed at the root.** The two free-tier
  defaults had been retired upstream — Groq shut down `llama-3.1-8b-instant`
  on 2026-08-16 and Google deprecated the 2.0 Flash family on 2026-06-01 — so
  every polished-metadata call failed with a 404 and fell back. The defaults
  now point at the providers' current free models (`openai/gpt-oss-20b`,
  `gemini-2.5-flash`); the Gemini payload pins `thinkingBudget: 0` so
  2.5-class models cannot spend the whole output budget thinking and reply
  empty; transient failures (timeouts, 429/5xx) are retried once; the
  render-path polish budget rose from a hard-coded 12 s to the full 25 s; and
  when the engine really cannot answer, the notice now says *why* ("HTTP 404
  …", "needs an API key") instead of a generic "unavailable".
- **Shorts end where the speaker stops.** Cuts no longer stop the instant the
  score window stops (the "unfinished line" failure): a window that ends
  mid-flow is scored down, and its end is extended to the next natural stop —
  terminal punctuation, a real pause in the transcript, or the end of the
  source — within a 3 s slack. Every cut also gets a breath of air after the
  last spoken word (up to 0.9 s, placed inside the following silence), and
  beat-synced or manual boundaries that land mid-word are repaired: forward to
  finish the word, or back to the previous pause when the rest of the line is
  too long.
- **Longer shorts.** Default clip length is now 25–90 s (was 20–60 s) — the
  highlight engine's duration sweet spot scales with the budget — with UI
  presets Short · 25–40s, Medium · 40–65s, Long · 60–90s, Any · 25–90s.
  YouTube Shorts accepts up to 3 minutes, so there is headroom.

## What's new in v0.6.0 — the camera follows the person

- **Subject tracking instead of a static crop.** The old behaviour cropped the
  middle of a wide shot and kept whatever happened to fit, which put people out
  of frame. Qyro now analyses the video, finds the speaker, and pans and zooms
  a 9:16 window to keep them in shot. All the pixel work happens inside ffmpeg
  (a downscaled heat map of skin-tone plus motion), so a 30 s clip is analysed
  in about 1.5 s and the result is cached. On a test clip where the subject
  sweeps across the frame, the tracked render holds the subject at
  0.47–0.53 of the frame width while a static centre crop loses them entirely.
  Optional OpenCV face detection sharpens it further when installed; it is not
  required.
- **A quality gate on the cuts.** After the highlight engine proposes windows,
  each one is graded on speech density, the longest pause inside it, and filler
  at the edges, then re-ranked and trimmed. Thin, rambling or dead-air-heavy
  stretches drop out, so only the good parts become clips.
- **Per-word caption timing.** Captions now pop word by word on the actual
  subtitle timings instead of being spread evenly across a line.
- **Nine caption animations** — fade, pop, zoom, bounce, glow, blur in,
  karaoke (words light up as they are spoken) and drop — plus **four clip
  transitions** (fade, dip, flash, slide) that preserve duration, so burned-in
  captions never drift out of sync.
- **Ten caption fonts and real Hindi support.** Hindi captions are no longer
  forced into CAPITAL LETTERS, get more words per line, and render in a
  Devanagari font. Qyro installs the OFL-licensed Shobhika font into
  `~/.fonts` on demand (one button in the UI, no root) because libass resolves
  families through fontconfig — without that step every Hindi word renders as
  a box.
- **Hindi and Hinglish subtitle selection** when downloading from YouTube.
- **Android** — a full beginner guide: [ANDROID-GUIDE.md](ANDROID-GUIDE.md).

## What's new in v0.5.0 — **Qyro**

The project is now called **Qyro** (same repo, same installers, same `autoshorts`
package name — nothing about how you install or run it changed).

- **A real brand** — the mark is a **Q** whose tail is a play triangle cut out of
  the ring, drawn in the violet → cyan gradient (`#7C3AED` → `#22D3EE`) on
  near-black `#0A0A0F`. One geometry, shipped as crisp inline SVG (header,
  favicon, README), the same shape rasterised for the PWA and Apple touch icons.
- **New look** — near-black glass UI, 12–16 px radii, gradient primary buttons,
  page/modal/toast transitions and a **zero-emoji interface**: every control uses
  a hand-drawn inline SVG icon (play, search, download, sliders, trash, pencil,
  close, retry, sparkle, music, captions, crop, wand). Mobile-first — 480 px
  wide phones get full-size touch targets and thumb-reachable modals.
- **1440p quality** — `quality` now takes `fast` · `full` · `1440p`
  (vertical 1440×2560, square 1440×1440, wide 2560×1440). The UI labels it
  *"slow on phone"*; `/file` and `/zip` serve whatever was rendered.
- **T1 Smart framing (unchanged, still the default for talking heads)** — region
  motion tracking, hysteresis, a numeric `x(t)` crop expression and a static
  fallback that always renders.
- **T2 Caption brands** — **Qyro Pop**, **Qyro Minimal** and **Qyro Neon** are
  bundles (size, colours, outline, weight, position, box, word-pop timing)
  applied on top of the base style, with the pop timing burned into the ASS file.
  `captions_brand: "none"` reproduces v0.4.0 captions byte-for-byte.
- **T3 Beat sync + audio beds** — beat markers are measured *offline* from
  ffmpeg's `ebur128` loudness curve (onset peak-picking, no new dependency);
  **"sync cuts to beats"** snaps moment boundaries onto the nearest marker.
  Upload your own music (`POST /api/audio`), then render with `audio_track` +
  `audio_mix: replace` (original muted, track looped, trimmed and loudness
  matched) or `duck` (voice at 20 % under the track).
- **T4 Logo remover** — `logo_box` erases a channel bug before framing with
  `delogo`, or a `boxblur` patch when the box touches a frame edge. Corners are a
  single preset; `custom` takes frame fractions. Optional: it never fails a short.
- **T5 Free AI engine** — titles, hashtags and the upload pack run through an
  engine chain: **offline templates by default**, then optional **Google AI
  Studio**, **Groq** or any **OpenAI-compatible** endpoint (base URL + key) you
  put in Settings. Keys are stored server-side, masked in every API response and
  never logged. Any failure — bad key, offline, junk reply — silently falls back
  to the offline pack and tells the UI why. Qyro never needs money or an account.
- **T6 Tool suite** — Audio Extract (`/api/clips/{id}/audio.mp3`, MP3 download),
  Thumbnail Picker (six frames → tap one to set the poster), Title Lab
  (`POST /api/titles`, 10 variations + hashtags), Silence Tuner (`silence_noise`,
  `silence_min` sliders), Clip Inspector (`/api/clips/{id}/probe`) and a Beat map
  per episode (`/api/episodes/{id}/beats`).
- **PWA** — manifest, theme colour, maskable icons, `apple-touch-icon` and a
  root-scoped service worker (network-first, shell-only cache), so *Add to Home
  screen* gives Qyro its own icon and window on Android and iOS.

## What's new in v0.4.0 — "Super God Mode"

- **Two servers, one API** — `autoshorts/server_stdlib.py` (pure Python, zero third-party imports — the Termux default: `python -m autoshorts.server_stdlib --port 8000`) and `autoshorts/server.py` (a FastAPI mirror). Same routes, same status codes, same `{"detail": …}` errors.
- **Smart framing** — portrait-from-landscape motion tracking: ffmpeg `signalstats` YDIF picks the active left/center/right third every 2 s, hysteresis + calm→center bias keep the crop steady, and the winner positions are baked into a stepped numeric `if(lt(t,…))` crop expression, remapped through silence cuts and speed onto the output timeline. Any analysis failure falls back to a static center crop — renders never break.
- **Caption upgrades** — `captions_pos` (standard / low), `captions_box` (opaque-box `BorderStyle=3`), and auto-fit that shrinks the font for long words (never below 24px) so 9:16 frames never overflow.
- **Styles** — `blur` · `crop` · `fill` · `fit` · `smart`; wide output always plain-scales.
- **Waveform strips** — 24 ebur128 loudness bars stored per clip and drawn on every card.
- **Single-video ingest** — the header input now takes playlists *and* single videos (`/api/playlist` returns `kind: video|playlist`); batch flows probe YouTube exactly once.
- **Job control** — cancel queued jobs (the close button in the dashboard, episodes reset when idle), retry done/failed jobs, CSV export (`/api/episodes/{id}/export`), offline transcript search (`/api/search`), clip rename, episode delete with file cleanup, storage clean for `subs|thumbs|clips` with honest `freed_bytes`.
- **Speed is a number** — any 0.5–2.0× (booleans are rejected with 422); unknown option keys are rejected with `Unknown options`; yt-dlp's version is read from `yt_dlp.version`, never CLI text.

## What's new in v0.3.0

- **God-mode render options** — every job now accepts `format` (vertical / square / wide), `captions` (classic / pop / minimal), `speed` (1.0× / 1.1× / 1.25×), a burned-in progress bar, silence jump-cuts and loudness normalisation. Find them under **Fine-tune** on each episode.
- **Signal breakdown** — every preview pick and rendered clip shows the weighted hook / numbers / questions / emotion / superlatives / energy / penalties signals behind its score.
- **Upload packs** — each clip ships with three titles (punchy · curiosity · SEO), up to 12 hashtags (starting `#shorts`) and a description, all generated offline. Hit **Copy pack** and paste straight into YouTube; **Polish** optionally rewrites it with the free engine (v0.5.0: any failure keeps the offline pack instead of erroring).
- **Transcript cutter** — tap one transcript line for the start, another for the end, and cut exactly that range.
- **Chapters** — turn an episode's top story moments into paste-ready YouTube chapters (`0:00 Intro`, `12:04 The truth about…`).
- **SRT sidecars + re-render** — download a clip's captions as `.srt`, or re-render it from the same stored range with new options.
- **Batch & auto-pilot** — save option presets, queue every new/errored episode with **Process all**, or let auto-pilot queue episodes the moment a playlist loads.
- **Dashboard** — per-folder storage with one-click cleanup, state backup/restore, job history with retry, and python/ffmpeg/yt-dlp version info.

## What's new in v0.2.0

- **429-safe YouTube ingestion** — all yt-dlp calls are serialized and paced, subtitle languages are requested one at a time, and HTTP 429 responses get progressive backoff.
- **Four highlight profiles** — choose **Viral**, **Story**, **Facts**, or **Energy** to change how moments are ranked.
- **Preview picks** — inspect titles, ranges, scores, and reasons before downloading or rendering media.
- **Manual clips** — cut any 5–180 second range; cached captions are used when present, but captions are optional.
- **Serial job queue** — one background worker handles all jobs, reducing load on YouTube and the host machine.
- **Clip library tools** — episode/clip search, score/date/duration sorting, native sharing, statistics, and one-click ZIP download.

## Quick start

**Easiest (Windows):** download the ZIP, extract it, then double-click
**`install.bat`** once, and **`run.bat`** whenever you want to use the app.
It opens at <http://localhost:8000> — keep the black window open while using it.

**Easiest (macOS / Linux):**

```bash
./install.sh   # once
./run.sh       # whenever you want to use the app
```

**Android (via Termux):**

1. Install [Termux from F-Droid](https://f-droid.org/packages/com.termux/) (the Play Store build is outdated — don't use it)
2. Easiest — paste this one line into Termux and wait:

> **Use the repository URL below exactly.** This Qyro build is published at
> `peterparker25552-star/https-github.com-divyaprakash0426-autoshorts`. The
> separate `divyaprakash0426/autoshorts` repository does not contain
> `android-install.sh`, so its raw URL returns a 404.

```bash
pkg update -y && pkg install -y curl && curl -fsSL https://raw.githubusercontent.com/peterparker25552-star/https-github.com-divyaprakash0426-autoshorts/main/android-install.sh -o "$HOME/autoshorts-android-install.sh" && bash "$HOME/autoshorts-android-install.sh"
```

The download uses `curl --fail` and saves the file before running it. If the
URL is ever wrong, it stops with a curl error instead of passing GitHub's
`404: Not Found` response to Bash. It installs everything, then start the app
any time with `bash ~/start-autoshorts.sh`.

Or do it by hand:

```bash
pkg update -y && pkg install -y python ffmpeg git
git clone https://github.com/peterparker25552-star/https-github.com-divyaprakash0426-autoshorts.git autoshorts
cd autoshorts
bash install-android.sh   # once
bash run-android.sh       # whenever you want to use the app
```

3. Open **http://localhost:8000** in Chrome, then in Chrome's menu (the three
   dots) choose **"Add to Home screen"** — Qyro installs as its own app with the
   Q mark icon, a near-black splash and its own window (no browser bar). It is a
   real PWA: manifest + theme colour + maskable icons + `apple-touch-icon`, and
   the service worker keeps the shell readable when the server is stopped.
   On iPhone: open the same URL in Safari and use **Share → Add to Home Screen**.
4. Rendered shorts land in the `data/clips/` folder inside Termux. To copy
   them to your phone's Downloads:

```bash
termux-setup-storage    # once — allow the permission
cp data/clips/*.mp4 ~/storage/downloads/
```

Qyro automatically uses Termux's native (ARM) ffmpeg and binds to
`0.0.0.0`, so any device on the same Wi-Fi can open the app too.

> **Android uses a built-in pure-Python server** — no FastAPI / pydantic, so
> the install works on every Termux Python (including 3.14+, where pip cannot
> resolve FastAPI at all). If you previously hit
> `ERROR: Cannot install fastapi==...`, just re-run the one-liner above; it
> replaces the old install completely.

**Manual (any OS):**

```bash
pip install -r requirements.txt   # ffmpeg ships via imageio-ffmpeg
python run.py                     # open http://localhost:8000
```

No FastAPI? No problem — `run.py` automatically falls back to the built-in
pure-Python server (same UI, same API). Strictly speaking only `yt-dlp` is
required for YouTube downloads, and demo mode needs nothing at all.

Then either:

1. **Paste a playlist URL** (pre-filled with the Raj Shamani playlist) and press **Load**, or
2. Press **Demo** — generates 3 episodes of synthetic media with crafted transcripts and runs the *entire real pipeline* (highlight scoring → clipping → captioning → thumbnails). Useful when YouTube is unreachable (e.g. restricted networks) or for a quick tour.

Per episode, pick **how many shorts**, a scoring profile (**Viral**, **Story**, **Facts**, or **Energy**), a length, the framing (**blurred background** or **center crop**) and the quality (**720p** · **1080p** · **1440p** — the last one is marked
"slow on phone"), then hit **Generate**. Under **Fine-tune** you can also set the **format** (vertical 9:16 · square 1:1 · wide 16:9), **captions** (classic · pop · minimal), a **caption brand** (Qyro Pop · Qyro Minimal · Qyro Neon), **speed** (0.5–2.0×), a **progress bar**, **silence jump-cuts** with the **silence tuner** sliders, **loudness**, the **logo remover** box (live preview, `delogo` with a `boxblur` fallback), a **music bed** with `replace`/`duck`, and **sync cuts to beats**. Use **Preview** to inspect the proposed moments (with their signal breakdown) without a media download, **Transcript** to tap out an exact range, **Exact range** to type one, **Beats** for the offline beat map, or **Chapters** to get paste-ready description chapters. Each finished short shows its score, its weighted signals, why it was picked (hook, stats, emotion…), an **upload pack** ("Made with Qyro" in its footer), an inline player, sharing, an `.srt` download, a one-click **Re-render**, plus **MP3**, **Thumb**, **Titles** and **Inspect** tools.

## Rate-limit (HTTP 429) protection

Qyro deliberately trades a little speed for reliable caption fetching:

- A thread-safe global pacer keeps every yt-dlp call at least **4 seconds** apart.
- Caption languages are tried **one per request** (`en`, `hi`, `en-orig`, `en.*`, then `hi.*`) rather than in a burst.
- yt-dlp also waits between subtitle and HTTP requests.
- HTTP 429 / “Too Many Requests” responses trigger **20-second and 40-second backoffs** before the final attempt.
- Successfully normalized transcripts are cached in `data/subs/`, so retries and later renders do not repeat completed caption work.

If YouTube still reports a 429, wait **10–15 minutes** before retrying. Avoid repeatedly restarting jobs during that window; queued jobs run one at a time automatically.

## How the highlight engine works (no API keys needed)

1. Captions are fetched one language at a time and split into sentence-like utterances.
2. The transcript is scanned with sliding **sentence-aligned windows** (25–90 s by default). Each window is scored with the selected Viral, Story, Facts, or Energy weight profile using signals that correlate with engaging short-form moments:
   - **hook phrases** — "the truth is", "nobody tells you", "sach bataun", …
   - **hard numbers / stats** — `40 crore`, `100 million`, percentages
   - **curiosity questions** — direct questions to the guest
   - **emotional & superlative language** — "shocking", "biggest", "changed my life"
   - **delivery energy** — words per second
   - **penalties** — intros, sponsor reads, greetings, filler
3. The top **non-overlapping** windows are kept; the punchiest sentence in each becomes the clip title, and contributing signals become the reason chips you see in the UI.
4. ffmpeg cuts each moment, reframes it to 9:16 (blur-background or crop) and burns in **word-by-word captions** styled like popular shorts.

Optional: set `OPENAI_API_KEY` (+ `OPENAI_BASE_URL`, `OPENAI_MODEL`) in the environment to enable LLM-assisted refinements. The heuristic engine is the default and runs fully offline.

## Render options

Every `shorts`, `manual`, `rerender` and `batch` request accepts the same set of
options (v0.5.0 added the middle block, v0.6.0 the last seven rows). Unknown
values are rejected with `422`, and the options are stored on each clip so
**Re-render** and **Retry** reproduce exactly what produced it. `/api/health`
returns the same catalog as JSON, which is what the web UI builds its pickers
from, so the two can never disagree.

| Option | Values | Default | Notes |
| --- | --- | --- | --- |
| `style` | `blur`, `crop` | `blur` | framing; ignored when `format=wide` (letterbox wins) |
| `quality` | `fast`, `full`, `1440p` | `fast` | 720p · 1080p · 1440p (2560-class long edge) |
| `captions_brand` | `none`, `qyro-pop`, `qyro-minimal`, `qyro-neon` | `none` | caption bundle: size, colours, outline, box, position, word-pop |
| `logo_box` | `null` or `{preset, size, feather}` / `{preset:"custom", x, y, w, h}` | `null` | erase a channel logo before framing (`delogo`, `boxblur` fallback) |
| `sync_beats` | `true` / `false` | `false` | move cut boundaries onto the nearest offline beat marker |
| `audio_track` | id from `POST /api/audio` | `null` | replace the short's soundtrack |
| `audio_mix` | `replace`, `duck` | `duck` | mute the original, or keep the voice at 20 % under the track |
| `silence_noise` | `-70` … `-18` (dB) | `-35` | where "silent" starts for jump-cuts |
| `silence_min` | `0.1` … `2.0` (s) | `0.5` | how long a pause must last to be cut |
| `format` | `vertical`, `square`, `wide` | `vertical` | 9:16 · 1:1 · 16:9 |
| `captions` | `classic`, `pop`, `minimal` | `classic` | uppercase chunks · per-word pop · small lower-third |
| `speed` | `1.0`, `1.1`, `1.25` | `1.0` | audio + video; captions stay in sync |
| `progress` | `true` / `false` | `false` | burned-in bottom progress bar |
| `silence` | `true` / `false` | `false` | detect pauses ≥0.4 s and jump-cut them out |
| `loud` | `true` / `false` | `false` | `loudnorm` (≈ -16 LUFS) |
| `track_mode` | `auto`, `vision`, `face`, `off` | `auto` | follow the speaker instead of a static crop; `face` needs OpenCV, `off` is the old thirds crop |
| `track_zoom` | `auto`, `tight`, `normal`, `wide` | `auto` | headroom left around the tracked subject |
| `captions_font` | `auto`, `bold`, `rounded`, `condensed`, `serif`, `mono`, `impact`, `hand`, `devanagari`, `devanagari-serif` | `auto` | resolved against the fonts actually installed |
| `captions_anim` | `none`, `fade`, `pop`, `zoom`, `bounce`, `glow`, `blurin`, `karaoke`, `drop` | `fade` | libass entrance tags; `karaoke` re-times the words |
| `transition` | `none`, `fade`, `dip`, `flash`, `slide` | `fade` | duration-preserving, so captions stay in sync |
| `language` | `auto`, `en`, `hi`, `hinglish` | `auto` | Hindi skips upper-casing, uses a Devanagari font and more words per line |
| `quality_gate` | `true` / `false` | `true` | grade each candidate window on speech density and dead air, then re-rank and trim |

## Configuration

| Env var | Purpose |
| --- | --- |
| `AUTOSHORTS_DATA` | Data directory (default `./data`) |
| `AUTOSHORTS_FFMPEG` | Explicit path to an ffmpeg binary |
| `AUTOSHORTS_PORT` | Port for `run.py` (default `8000`) |
| `AUTOSHORTS_FONT` | Caption font family (default `DejaVu Sans`) |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` | Optional OpenAI-compatible endpoint (the engine's "custom" provider) |

Engine keys are usually set in **Settings** in the UI instead of the environment;
they are written to `data/state.json` only, masked as `*_set` booleans in every
API response, and never included in a log line or an error message.

## API

The web UI is a thin client over a small JSON API:

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/api/health` | version, YouTube reachability, ffmpeg path, tool versions, disk space, LLM availability |
| `GET` | `/api/state` | episodes, clips, active jobs, library stats, render defaults |
| `POST` | `/api/playlist` | `{url, limit}` — ingest playlist metadata (auto-queues when auto-pilot is on) |
| `POST` | `/api/demo/load` | load the demo episodes |
| `POST` | `/api/settings` | `{autopilot, ai_provider, ai_model, ai_base_url, gemini_key, groq_key, ai_key}` — defaults and the free AI engine (keys are write-only) |
| `GET` | `/api/state` extras | `engine` (masked provider/model/key status) and `audio_tracks` |
| `POST` | `/api/audio` | `{name, data_b64}` → `{track_id}` — upload a music bed (base64, ≤40 MB) |
| `GET` | `/api/audio` · `GET /api/audio/{id}/file` · `DELETE /api/audio/{id}` | list, preview, forget beds |
| `GET` | `/api/episodes/{id}/beats` | offline beat markers (`?refresh=1` rescans) |
| `GET` | `/api/episodes/{id}/audio.mp3` | extract an episode's audio as MP3 |
| `GET` | `/api/clips/{id}/audio.mp3` | extract a clip's audio as MP3 |
| `GET` | `/api/clips/{id}/probe` | real width/height/fps/codecs/duration/size |
| `GET` | `/api/clips/{id}/thumb-candidates?n=6` | frames to choose a poster from |
| `POST` | `/api/clips/{id}/thumb-pick` | `{index}` — make a candidate the clip's thumbnail |
| `POST` | `/api/titles` | Title Lab: `{text, profile, count}` → 10 titles + hashtags via the engine |
| `POST` | `/api/episodes/{id}/shorts` | `{count, min_dur, max_dur, profile}` + render options — queue automatic clips |
| `POST` | `/api/episodes/{id}/preview` | score and return moments (with `signals` and transcript `stats`) without downloading/rendering |
| `POST` | `/api/episodes/{id}/manual` | `{start, end, title}` + render options — queue an exact range |
| `GET` | `/api/episodes/{id}/transcript` | parsed transcript segments |
| `GET` | `/api/episodes/{id}/chapters` | YouTube-style chapters from the top story moments |
| `POST` | `/api/batch` | queue shorts for every new/errored episode (same body as `/shorts`) |
| `GET` | `/api/jobs?limit=` | recent job history |
| `POST` | `/api/jobs/{id}/retry` | re-queue a finished job with the same episode + parameters |
| `GET` | `/api/clips/zip?episode_id=…` | download all or per-episode clips as a ZIP |
| `GET` | `/api/clips/{id}/file` · `/thumb` | media files (`/thumb?index=N` serves candidate N) |
| `GET` | `/api/clips/{id}/srt` | download the clip's captions as SubRip |
| `POST` | `/api/clips/{id}/rerender` | re-render the stored range, optionally overriding render options |
| `POST` | `/api/clips/{id}/polish` | rewrite the upload pack through the engine — 503 only when nothing is configured; any provider failure returns 200 with the offline pack plus a `notice` |
| `DELETE` | `/api/clips/{id}` | delete a clip |
| `GET` | `/api/storage` | per-folder sizes, file counts, disk usage |
| `POST` | `/api/storage/clean` | `{target}` — `media` · `subs` · `thumbs` · `clips` |
| `GET` | `/api/backup` | download a full state backup (JSON attachment) |
| `POST` | `/api/restore` | restore a backup; interrupted jobs come back as retryable errors |

## Notes & limits

- **Captions dependency**: automatic highlight picking and previews need a transcript. Manual ranges can still render without captions; local Whisper transcription is a natural future extension.
- **Sandbox/network**: some hosted environments block YouTube. Qyro detects this and points you at demo mode; run it on your own machine for real downloads.
- **Big renders on a phone**: 1440p is 4× the pixels of 720p. On mid-range ARM it is several times slower per clip; the app warns about it, and quality is per-episode so you can draft at 720p and re-render the keeper at 1440p.
- **Upload size**: `POST /api/audio` takes base64, so its body limit is ~57 MB (≈ 40 MB of audio). Everything else keeps the 5 MB JSON cap.
- **Responsibility**: downloading and re-publishing creators' content may be restricted by copyright and platform terms. Use for personal study or with permission.
