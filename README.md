# ✂️ AutoShorts

Turn long YouTube podcast episodes into **short, captioned, vertical (9:16) clips** — automatically.

Built around the seed playlist [**Figuring Out With Raj Shamani**](https://www.youtube.com/playlist?list=PLE0Jo6NF_JYO5-phess8GKafKMtPv3tfZ) (562 episodes), but works with **any** YouTube playlist or video.

```text
playlist URL ──▶ yt-dlp ──▶ transcripts ──▶ highlight engine ──▶ ffmpeg ──▶ 9:16 shorts
                (media +     (auto-captions)  (hooks · numbers ·    (crop/blur +
                 captions)                     questions · energy)   burned-in captions)
```

## What's new in v0.3.0

- **God-mode render options** — every job now accepts `format` (vertical / square / wide), `captions` (classic / pop / minimal), `speed` (1.0× / 1.1× / 1.25×), a burned-in progress bar, silence jump-cuts and loudness normalisation. Find them under **⚙ Fine-tune** on each episode.
- **Signal breakdown** — every preview pick and rendered clip shows the weighted hook / numbers / questions / emotion / superlatives / energy / penalties signals behind its score.
- **Upload packs** — each clip ships with three titles (punchy · curiosity · SEO), up to 12 hashtags (starting `#shorts`) and a description, all generated offline. Hit **📋 Copy pack** and paste straight into YouTube; **✨ Polish** optionally rewrites it with any OpenAI-compatible endpoint.
- **Transcript cutter** — tap one transcript line for the start, another for the end, and cut exactly that range.
- **Chapters** — turn an episode's top story moments into paste-ready YouTube chapters (`0:00 Intro`, `12:04 The truth about…`).
- **SRT sidecars + re-render** — download a clip's captions as `.srt`, or re-render it from the same stored range with new options.
- **Batch & auto-pilot** — save option presets, queue every new/errored episode with **⚡ All**, or let auto-pilot queue episodes the moment a playlist loads.
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

```bash
pkg update -y ; pkg install -y curl ; curl -sSL https://raw.githubusercontent.com/peterparker25552-star/https-github.com-divyaprakash0426-autoshorts/main/android-install.sh | bash
```

It installs everything, then start the app any time with
`bash ~/start-autoshorts.sh`.

Or do it by hand:

```bash
pkg update -y && pkg install -y python ffmpeg git
git clone https://github.com/peterparker25552-star/https-github.com-divyaprakash0426-autoshorts.git autoshorts
cd autoshorts
bash install-android.sh   # once
bash run-android.sh       # whenever you want to use the app
```

3. Open **http://localhost:8000** in Chrome. In Chrome's ⋮ menu choose
   **"Add to Home screen"** to install it as an app with its own icon.
4. Rendered shorts land in the `data/clips/` folder inside Termux. To copy
   them to your phone's Downloads:

```bash
termux-setup-storage    # once — allow the permission
cp data/clips/*.mp4 ~/storage/downloads/
```

AutoShorts automatically uses Termux's native (ARM) ffmpeg and binds to
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

1. **Paste a playlist URL** (pre-filled with the Raj Shamani playlist) and press **Load playlist**, or
2. Press **⚡ Try demo** — generates 3 episodes of synthetic media with crafted transcripts and runs the *entire real pipeline* (highlight scoring → clipping → captioning → thumbnails). Useful when YouTube is unreachable (e.g. restricted networks) or for a quick tour.

Per episode, pick **how many shorts**, a scoring profile (**Viral**, **Story**, **Facts**, or **Energy**), a length, the framing (**blurred background** or **center crop**) and the quality (**720p-class** or **1080p-class**), then hit **Generate**. Under **⚙ Fine-tune** you can also set the **format** (vertical 9:16 · square 1:1 · wide 16:9), **captions** (classic · pop · minimal), **speed** (1.0× / 1.1× / 1.25×), a **progress bar**, **silence jump-cuts** and **loudness** normalisation. Use **Preview picks** to inspect the proposed moments (with their signal breakdown) without a media download, **✂ Transcript cutter** to tap out an exact range, **✂ Manual clip** to type one, or **🔖 Chapters** to get paste-ready description chapters. Each finished short shows its score, its weighted signals, why it was picked (hook, stats, emotion…), an **upload pack**, an inline player, sharing, an `.srt` download and a one-click **Re-render**.

## Rate-limit (HTTP 429) protection

AutoShorts v0.2.0 deliberately trades a little speed for reliable caption fetching:

- A thread-safe global pacer keeps every yt-dlp call at least **4 seconds** apart.
- Caption languages are tried **one per request** (`en`, `hi`, `en-orig`, `en.*`, then `hi.*`) rather than in a burst.
- yt-dlp also waits between subtitle and HTTP requests.
- HTTP 429 / “Too Many Requests” responses trigger **20-second and 40-second backoffs** before the final attempt.
- Successfully normalized transcripts are cached in `data/subs/`, so retries and later renders do not repeat completed caption work.

If YouTube still reports a 429, wait **10–15 minutes** before retrying. Avoid repeatedly restarting jobs during that window; queued jobs run one at a time automatically.

## How the highlight engine works (no API keys needed)

1. Captions are fetched one language at a time and split into sentence-like utterances.
2. The transcript is scanned with sliding **sentence-aligned windows** (20–60 s by default). Each window is scored with the selected Viral, Story, Facts, or Energy weight profile using signals that correlate with engaging short-form moments:
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

Every `shorts`, `manual`, `rerender` and `batch` request accepts the same eight
options. Unknown values are rejected with `422`, and the options are stored on
each clip so **Re-render** and **Retry** reproduce exactly what produced it.

| Option | Values | Default | Notes |
| --- | --- | --- | --- |
| `style` | `blur`, `crop` | `blur` | framing; ignored when `format=wide` (letterbox wins) |
| `quality` | `fast`, `full` | `fast` | 720p-class or 1080p-class output |
| `format` | `vertical`, `square`, `wide` | `vertical` | 9:16 · 1:1 · 16:9 |
| `captions` | `classic`, `pop`, `minimal` | `classic` | uppercase chunks · per-word pop · small lower-third |
| `speed` | `1.0`, `1.1`, `1.25` | `1.0` | audio + video; captions stay in sync |
| `progress` | `true` / `false` | `false` | burned-in bottom progress bar |
| `silence` | `true` / `false` | `false` | detect pauses ≥0.4 s and jump-cut them out |
| `loud` | `true` / `false` | `false` | `loudnorm` (≈ -16 LUFS) |

## Configuration

| Env var | Purpose |
| --- | --- |
| `AUTOSHORTS_DATA` | Data directory (default `./data`) |
| `AUTOSHORTS_FFMPEG` | Explicit path to an ffmpeg binary |
| `AUTOSHORTS_FONT` | Caption font family (default `DejaVu Sans`) |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` | Optional LLM endpoint |

## API

The web UI is a thin client over a small JSON API:

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/api/health` | version, YouTube reachability, ffmpeg path, tool versions, disk space, LLM availability |
| `GET` | `/api/state` | episodes, clips, active jobs, library stats, render defaults |
| `POST` | `/api/playlist` | `{url, limit}` — ingest playlist metadata (auto-queues when auto-pilot is on) |
| `POST` | `/api/demo/load` | load the demo episodes |
| `POST` | `/api/settings` | `{autopilot}` — toggle auto-pilot |
| `POST` | `/api/episodes/{id}/shorts` | `{count, min_dur, max_dur, profile}` + render options — queue automatic clips |
| `POST` | `/api/episodes/{id}/preview` | score and return moments (with `signals` and transcript `stats`) without downloading/rendering |
| `POST` | `/api/episodes/{id}/manual` | `{start, end, title}` + render options — queue an exact range |
| `GET` | `/api/episodes/{id}/transcript` | parsed transcript segments |
| `GET` | `/api/episodes/{id}/chapters` | YouTube-style chapters from the top story moments |
| `POST` | `/api/batch` | queue shorts for every new/errored episode (same body as `/shorts`) |
| `GET` | `/api/jobs?limit=` | recent job history |
| `POST` | `/api/jobs/{id}/retry` | re-queue a finished job with the same episode + parameters |
| `GET` | `/api/clips/zip?episode_id=…` | download all or per-episode clips as a ZIP |
| `GET` | `/api/clips/{id}/file` · `/thumb` | media files |
| `GET` | `/api/clips/{id}/srt` | download the clip's captions as SubRip |
| `POST` | `/api/clips/{id}/rerender` | re-render the stored range, optionally overriding render options |
| `POST` | `/api/clips/{id}/polish` | rewrite the upload pack with the configured LLM (503 without a key, 502 on failure) |
| `DELETE` | `/api/clips/{id}` | delete a clip |
| `GET` | `/api/storage` | per-folder sizes, file counts, disk usage |
| `POST` | `/api/storage/clean` | `{target}` — `media` · `subs` · `thumbs` · `clips` |
| `GET` | `/api/backup` | download a full state backup (JSON attachment) |
| `POST` | `/api/restore` | restore a backup; interrupted jobs come back as retryable errors |

## Notes & limits

- **Captions dependency**: automatic highlight picking and previews need a transcript. Manual ranges can still render without captions; local Whisper transcription is a natural future extension.
- **Sandbox/network**: some hosted environments block YouTube. AutoShorts detects this and points you at demo mode; run it on your own machine for real downloads.
- **Responsibility**: downloading and re-publishing creators' content may be restricted by copyright and platform terms. Use for personal study or with permission.
