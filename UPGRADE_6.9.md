# Qyro v6.9 — Upgrade Guide (the transcript that was never missing)

The install/verify path for **v0.6.9**. One user report:

* *"I can't generate shorts from the app version 6.8 … when I am clicking the
  generate option it is saying — Transcript unavailable: No captions available
  for this episode."*

That sentence is a claim about the video, and it was almost always wrong. The
episodes in question have captions; what happened is that YouTube declined to
hand them over, and Qyro had no word for "declined". No render geometry, option
or route contract changed — this is the transcript path only.

## What was actually broken

`get_transcript()` had exactly two outcomes: an HTTP 429, and *"No captions
available for this episode"*. Everything else was folded into the second one:

| What really happened | What v0.6.8 said |
| --- | --- |
| YouTube **withheld** the caption tracks from an anonymous client — its *PO Token* skip. yt-dlp prints a **warning**, then `[info] There are no subtitles for the requested languages`, and exits **0** with nothing on disk | "No captions available for this episode" |
| **Bot check** — `Sign in to confirm you're not a bot` | "No captions available for this episode" |
| **TLS reset / dead DNS / a network that blocks YouTube's API** | "No captions available for this episode" |
| **yt-dlp too old** to parse YouTube's current player | "No captions available for this episode" |
| **Private, removed, age- or region-locked video** | "No captions available for this episode" |

Three details made it worse:

1. **The player-client walk only walked on a 429.** v0.6.7 built a chain
   (`web → mweb → tv → web_safari → ios`) precisely because a refusal on one
   client usually leaves another working — but any *other* failure ended the
   loop after the first client. The escape hatch never opened.
2. **The first client was pinned to `web`**, the one YouTube challenges
   hardest, and pinning it with `--extractor-args youtube:player_client=web`
   *overrides* the installed yt-dlp's own default chain — a list maintained
   against a YouTube that changes weekly (it now leads with the JavaScript-less
   `visionos` client for exactly this reason).
3. **`--no-warnings` deleted the evidence.** The PO-token refusal — the single
   most common cause of a caption-less run in 2025–26 — is a *warning*. Qyro
   suppressed warnings on every call, so the one line that explained the empty
   result was thrown away before anything could read it, and yt-dlp's exit
   status 0 made the pass look clean.

The real error text was carried all the way up to `get_transcript` and then
assigned to `_error` and dropped on the floor.

## What changed in v6.9

1. **Every empty caption pass now says *why* it was empty
   (`autoshorts/youtube.py`).** A `KIND_*` taxonomy replaces the 429/everything-else
   split: `rate_limit`, `bot_check`, `po_token` (captions withheld), `video_unavailable`,
   `network`, `extractor` (yt-dlp could not parse YouTube), `no_captions`,
   `unknown`. `classify_failure()` reads a failed run, `classify_caption_output()`
   reads a run that *succeeded* — which is where the silent failures live.
2. **Caption calls keep yt-dlp's warnings (`autoshorts/youtube.py`).**
   `--no-warnings` is now applied to everything *except* a subtitle call, so the
   PO-token and bot-check notices survive to be read. Metadata (`-J`) calls keep
   it, so their stdout stays parseable JSON.
3. **The player-client walk escapes every YouTube-side refusal, not just a 429.**
   A bot check, a withheld track or an unparsed response moves to the next
   client; a private video stops at once (nothing rotates that); a dead
   connection and a broken extractor are capped, because another client cannot
   fix either and the user should not stare at "Fetching transcript…" for the
   whole chain. A 429 still walks everything, exactly as v0.6.7 required.
4. **The first pass pins no client at all.** `PLAYER_CLIENT_CHAIN` now starts
   with an empty entry — "whatever the installed yt-dlp picks" — followed by
   `visionos, tv_simply, mweb, web_safari, tv, web`. `AUTOSHORTS_PLAYER_CLIENT`
   still leads the walk when it is set, and yt-dlp's own default becomes one of
   the fallbacks.
5. **"No captions available" has to be earned.** A clean-but-empty pass is now
   *evidence*, not a verdict: `caption_inventory()` asks yt-dlp for the
   episode's own metadata (one request, only on that path) and the words are
   only used when the metadata agrees there is no subtitle or auto-caption
   track. When it lists tracks, the message says the episode **does** have
   captions and that they were withheld — with the cure.
6. **Rotation is targeted, so it stays quick.** Once the inventory is known, a
   later client is asked only for languages the episode really has
   (`TRANSCRIPT_ROTATION_LANGUAGES`, default 3) instead of another full
   five-language walk, and refusals are capped at `TRANSCRIPT_MAX_PASSES`
   (default 4) clients.
7. **Auto-detect now detects.** An episode whose captions exist only in a
   language nobody asked for — Tamil, Telugu, Marathi — used to fail as
   "no captions". With the inventory known, an `auto` fetch takes the episode's
   own track (the `-orig` language first). An *explicit* choice is not quietly
   widened: it is told what the episode has and pointed at Auto-detect.
8. **Every message ends in the thing to do next.** A bot check or withheld
   track names **Tools ▸ YouTube session** (a signed-in `cookies.txt`); a stale
   extractor names `pip install -U yt-dlp` *and the installed version with its
   age*; a network failure says it is a connection problem and that cached
   transcripts are safe; a private video says no client or session will help.
   Messages are kept inside the job-error budget (`ERROR_MESSAGE_LIMIT`, now
   700 chars) so the remedy is never truncated away.
9. **The app says so before you press Generate (`web/app.js`,
   `autoshorts/maintenance.py`).** `/api/health`'s YouTube slice now carries
   `yt_dlp: {version, age_days, stale, stale_days, present}`; the health strip
   shows a chip (`yt-dlp 2026.08.19`, amber `yt-dlp 886d old — update`, red
   `yt-dlp missing`), and **Tools ▸ YouTube session** lists the extractor's age
   next to the session it cures. The card's *"Fix this — add a YouTube session"*
   button, which used to appear only for a 429, now appears for a bot check and
   a withheld track too — the same cure.
10. **One command diagnoses an install (`tools/check_transcript.py`).** Offline
    by default: a stub yt-dlp acts out all eight failures with YouTube's real
    output and shows what Qyro now says and does. With `--live <url>` it
    diagnoses *this* machine against a real episode — extractor version and age,
    session installed or not, a standing block, the caption tracks the episode
    really has, and then a real fetch with the verdict.

## Install / upgrade

```bash
git pull                      # get v0.6.9
python -m pip install -r requirements.txt
python -m pip install -U yt-dlp     # do this one even if it "worked before"
./run.sh                      # Windows: run.bat
```

Open http://127.0.0.1:8000 — the health strip now names your yt-dlp version.

## Install (Android / Termux)

```bash
pkg update && pkg install python ffmpeg git
cd ~/autoshorts && git pull
pip install -U yt-dlp
bash install-android.sh
python run.py
```

The versioned shell cache (`qyro-v0.6.9-shell`) means the home-screen icon
picks up the new `app.js` on its first launch after the server comes back.

## Verify in 60 seconds

1. **The offline proof** — `python3 tools/check_transcript.py` → all eight
   scenarios `PASS`, exit 0. Nothing there touches the network.
2. **Your machine, a real episode** —
   `python3 tools/check_transcript.py --live "<episode url>"`. It prints your
   yt-dlp version and age, whether a session is installed, which caption tracks
   the episode really has, and then fetches one. If it fails, the message names
   the cause; the four lines under it map each cause to its fix.
3. **Press Generate on the episode that used to fail.** Either it works (a
   different player client served the captions), or the job error now tells you
   what YouTube actually said — with the cure — instead of "No captions
   available for this episode".
4. **If the message names a bot check or withheld captions** — open
   **Tools ▸ YouTube session**, export a Netscape `cookies.txt` from a browser
   signed in to YouTube, pick it, and press **Retry**. The card offers that
   button directly now.
5. **If the health chip says `yt-dlp …d old — update`** — run
   `pip install -U yt-dlp` and restart. YouTube outruns an old extractor
   weekly; this is the other half of most caption failures.
6. **Tests** — `python -m tests.run_all` → `ALL GREEN` (new modules
   `tests/test_units_v069.py` and `tests/test_http_v069.py`; three assertions
   in `tests/test_units_v067.py` updated to the new chain order and to what a
   clean-but-empty pass is now allowed to mean).

## What a *real* "no captions" looks like now

The verdict survives — it is just no longer a guess:

> No captions available for this episode — YouTube's own metadata for it lists
> no subtitle or auto-caption track, so there is nothing to transcribe. Pick
> another episode, or cut one yourself with Exact range on the episode card — a
> manual clip renders fine without a transcript.

## Rollback

`git checkout v0.6.8` (or the previous commit) and restart. Nothing in
`data/state.json` is rewritten by this upgrade, and every transcript already
cached in `data/subs/` is used exactly as before — a cached transcript is still
read with no request at all, so the upgrade cannot lose finished work.
