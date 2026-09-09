# ✂️ AutoShorts

Turn long YouTube podcast episodes into **short, captioned, vertical (9:16) clips** — automatically.

Built around the seed playlist [**Figuring Out With Raj Shamani**](https://www.youtube.com/playlist?list=PLE0Jo6NF_JYO5-phess8GKafKMtPv3tfZ) (562 episodes), but works with **any** YouTube playlist or video.

```text
playlist URL ──▶ yt-dlp ──▶ transcripts ──▶ highlight engine ──▶ ffmpeg ──▶ 9:16 shorts
                (media +     (auto-captions)  (hooks · numbers ·    (crop/blur +
                 captions)                     questions · energy)   burned-in captions)
```

## Quick start

**Easiest (Windows):** download the ZIP, extract it, then double-click
**`install.bat`** once, and **`run.bat`** whenever you want to use the app.
It opens at <http://localhost:8000> — keep the black window open while using it.

**Easiest (macOS / Linux):**

```bash
./install.sh   # once
./run.sh       # whenever you want to use the app
```

**Manual (any OS):**

```bash
pip install -r requirements.txt   # ffmpeg ships via imageio-ffmpeg
python run.py                     # open http://localhost:8000
```

Then either:

1. **Paste a playlist URL** (pre-filled with the Raj Shamani playlist) and press **Load playlist**, or
2. Press **⚡ Try demo** — generates 3 episodes of synthetic media with crafted transcripts and runs the *entire real pipeline* (highlight scoring → clipping → captioning → thumbnails). Useful when YouTube is unreachable (e.g. restricted networks) or for a quick tour.

Per episode, pick **how many shorts**, the framing (**blurred background** or **center crop**) and the quality (**720×1280** or **1080×1920**), then hit **Generate**. Each finished short shows its score, why it was picked (hook, stats, emotion…), an inline player and a download button.

## How the highlight engine works (no API keys needed)

1. Captions are fetched (`en`/`hi` first) and split into sentence-like utterances.
2. The transcript is scanned with sliding **sentence-aligned windows** (20–60 s by default). Each window is scored on signals that correlate with engaging short-form moments:
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
| `GET` | `/api/state` | episodes, clips, active jobs |
| `POST` | `/api/playlist` | `{url, limit}` — ingest playlist metadata |
| `POST` | `/api/demo/load` | load the demo episodes |
| `POST` | `/api/episodes/{id}/shorts` | `{count, min_dur, max_dur, style, quality}` — start job |
| `GET` | `/api/episodes/{id}/transcript` | parsed transcript segments |
| `GET` | `/api/clips/{id}/file` · `/thumb` | media files |
| `DELETE` | `/api/clips/{id}` | delete a clip |

## Notes & limits

- **Captions dependency**: highlight picking needs a transcript. Videos without any caption track can't be processed yet (local Whisper transcription is a natural next step — see `autoshorts/highlights.py` for where scoring hooks in).
- **Sandbox/network**: some hosted environments block YouTube. AutoShorts detects this and points you at demo mode; run it on your own machine for real downloads.
- **Responsibility**: downloading and re-publishing creators' content may be restricted by copyright and platform terms. Use for personal study or with permission.
