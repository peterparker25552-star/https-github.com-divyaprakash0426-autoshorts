# ✂️ AutoShorts

Turn long YouTube podcast episodes into **short, captioned, vertical (9:16) clips** — automatically.

Built around the seed playlist [**Figuring Out With Raj Shamani**](https://www.youtube.com/playlist?list=PLE0Jo6NF_JYO5-phess8GKafKMtPv3tfZ) (562 episodes), but works with **any** YouTube playlist or video.

```text
playlist URL ──▶ yt-dlp ──▶ transcripts ──▶ highlight engine ──▶ ffmpeg ──▶ 9:16 shorts
                (media +     (auto-captions)  (hooks · numbers ·    (crop/blur +
                 captions)                     questions · energy)   burned-in captions)
```

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

Per episode, pick **how many shorts**, a scoring profile (**Viral**, **Story**, **Facts**, or **Energy**), a length, the framing (**blurred background** or **center crop**) and the quality (**720×1280** or **1080×1920**), then hit **Generate**. Use **Preview picks** to inspect the proposed moments without a media download, or **Manual clip** to cut an exact range. Each finished short shows its score, why it was picked (hook, stats, emotion…), an inline player, sharing, and download controls.

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
| `GET` | `/api/health` | version, YouTube reachability, ffmpeg path |
| `GET` | `/api/state` | episodes, clips, active jobs, and library stats |
| `POST` | `/api/playlist` | `{url, limit}` — ingest playlist metadata |
| `POST` | `/api/demo/load` | load the demo episodes |
| `POST` | `/api/episodes/{id}/shorts` | `{count, min_dur, max_dur, profile, style, quality}` — queue automatic clips |
| `POST` | `/api/episodes/{id}/preview` | score and return moments without downloading/rendering |
| `POST` | `/api/episodes/{id}/manual` | `{start, end, title, style, quality}` — queue an exact range |
| `GET` | `/api/episodes/{id}/transcript` | parsed transcript segments |
| `GET` | `/api/clips/zip?episode_id=…` | download all or per-episode clips as a ZIP |
| `GET` | `/api/clips/{id}/file` · `/thumb` | media files |
| `DELETE` | `/api/clips/{id}` | delete a clip |

## Notes & limits

- **Captions dependency**: automatic highlight picking and previews need a transcript. Manual ranges can still render without captions; local Whisper transcription is a natural future extension.
- **Sandbox/network**: some hosted environments block YouTube. AutoShorts detects this and points you at demo mode; run it on your own machine for real downloads.
- **Responsibility**: downloading and re-publishing creators' content may be restricted by copyright and platform terms. Use for personal study or with permission.
