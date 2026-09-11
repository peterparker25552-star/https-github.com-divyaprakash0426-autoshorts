# Qyro v6.5 — Upgrade Guide (search bar, AQ. keys, clean ta-dum)

The install/verify path for **v0.6.5**. Three user-reported fixes: the search
icon overlapping the word "Search", Google's new **`AQ.`** API keys, and the
random **glitch/zap** in the intro sound. No render pipeline, API route or
option changed.

## What changed in v6.5

1. **Search bar overlap (`web/style.css`)**
   - The shared control rule `input[type="search"] { padding: 9px 11px }` sits
     *after* `.inputwrap input { padding-left: 34px }` at equal specificity, so
     it won and the placeholder slid under the prefix icon.
   - Fix: `.inputwrap input, .inputwrap input[type] { padding-left: 34px; }` —
     the attribute selector outranks it. Applies to the episode search, the
     clip search and the playlist field. The icon is now also vertically
     centred with `top: 50%; transform: translateY(-50%)`.
2. **Google "AQ." auth keys (`autoshorts/config.py`, `engine.py`, `maintenance.py`, `web/app.js`)**
   - `GEMINI_KEY_PATTERN` + `config.is_gemini_key()` accept `AQ.` (new auth
     keys) and `AIza` (legacy); `POST /api/settings` answers `422` with a
     readable hint for anything else.
   - A Google key pasted into the *OpenAI-compatible* box is moved to the
     Google provider automatically (AQ. keys cannot work on Bearer routes).
   - Default free-tier model is `gemini-3.6-flash`; Gemini 3.x gets
     `thinkingConfig: {thinkingLevel: "low"}`, 2.5-class keeps
     `thinkingBudget: 0`.
   - Settings UI: the Google key field now shows `AQ… or AIza…` and explains
     the new format; the model placeholder names `gemini-3.6-flash`.
3. **Intro glitch (`web/intro.js`)**
   - Envelope automation can no longer land at/behind `ctx.currentTime` (the
     click source): gains start pinned to silence and late cues shift ~12 ms.
   - The score gets 60 ms of lead room (was 20 ms).
   - Reverb impulse 2.6 s → 1.7 s and sub-bass cues skip the reverb send — the
     crackle source on phones.
   - `dispose()` fades the old context over 30 ms before closing — no more pop
     on instant replays.
4. **Upgrade-safe.** Shell cache moves to `qyro-v0.6.5-shell`, `APP_VERSION`
   and `__version__` are `0.6.5`, the ident reports `6.5-spectrum`.

## Install (desktop / laptop)

```bash
git pull                      # get v0.6.5
python -m pip install -r requirements.txt
./run.sh                      # Windows: run.bat
```

Open http://127.0.0.1:8000 — the intro plays once per session as usual.

## Install (Android / Termux)

```bash
pkg update && pkg install python ffmpeg git
git pull
python -m pip install -r requirements.txt
bash android-install.sh       # one-time PWA bootstrap
python run.py
```

Then open the local URL in Chrome → *Add to Home screen*. Because the shell
cache is versioned, the installed icon picks up the new `style.css`/`intro.js`
on the first launch after the server comes back.

## Verify in 60 seconds

1. **Search bars** — look at the *Episodes* box: the magnifier sits alone in
   its lane and the word "Search" starts to the right of it. Same for the
   *Shorts* box. Type into both; the text never slides under the icon.
2. **AQ. key** — ⚙ Settings → *Google AI Studio key* → paste the key you copied
   from aistudio.google.com/apikey (starts with `AQ.`) → Save settings. The
   header hint turns into `free AI engine: gemini`. Generate any short; the
   upload pack comes back AI-polished. (Paste a Groq key there on purpose and
   the app refuses with a clear message instead of failing later.)
3. **Intro sound** — tap the ✦ button in the header a few times in a row
   (instant replays are the case that used to pop). The ta-dum lands clean
   every time, with no zap at the burst and no crackle under the beams. On a
   phone, reload with sound blocked, tap once, and the beat still lands on the
   burst — just quieter leads, no click.
4. **Tests** — `python -m tests.run_all` → `ALL GREEN ✅ — 442 tests`.

## Rollback

`git checkout v0.6.4` (or the previous commit) and restart. Keys stored in
`data/state.json` are untouched by the upgrade.
