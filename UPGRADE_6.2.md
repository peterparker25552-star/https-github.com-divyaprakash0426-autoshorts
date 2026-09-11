# Qyro v6.2 — Upgrade Guide

This guide covers what changed in **v6.2** and how to install it after the PR is merged.

## What's new in v6.2

1. **New logo — Claude + Grok inspired**
   - Soft 6-point rounded star (Claude's friendly organic shape) forming the Q ring
   - Sharp play triangle tail (Grok's angular cut) + cyan sparkle
   - Violet `#7C3AED` → `#9D5CF5` → cyan `#22D3EE` gradient on near-black `#0A0A0F`
   - Assets: `web/logo.svg`, `web/icons/icon-*.png`, `favicon.svg`, `apple-touch-icon.png`
   - The header uses inline SVG, so no extra request.

2. **Captions on/off toggle**
   - New boolean `captions_enabled` (default **True**)
   - Backend: `config.DEFAULT_CAPTIONS_ENABLED`, `maintenance.RENDER_DEFAULTS` + `_FLAG_KEYS`, `pipeline._render_settings` + `_render_one` skips `make_ass` when disabled (ffmpeg already handles `ass_path=None`)
   - Frontend: checkbox `.opt-captions-enabled` in Fine-tune, main payload `renderOptsPayload` includes `captions_enabled`, rerender modal also has toggle `#rr-cap-enabled`
   - When off, you get clean video with no burned captions, but SRT still downloadable from cached transcript.

3. **Flicker fix — Generate buttons no longer flicker**
   - Root cause: `renderEpisodes()` did `innerHTML` replace every 1.5s `fastPoll`, and CSS `animation: rise` retriggered.
   - Fix CSS: `web/style.css` `.episode,.clip` now `animation:none` + `transition`, new `.is-new` class only animates first paint.
   - Fix JS: `web/app.js` keeps `lastEpisodeSig` (id:status:clip_count:error), `patchEpisodeProgress()` updates only progressbar fill + badge + stepmsg when episode IDs unchanged and user is interacting (select focused or progressbar present). Full re-render only when list changes.

4. **Netflix-style intro animation + sound**
   - Markup: `#introOverlay` in `web/index.html` with SVG logo + `QYRO` text + line
   - CSS: `@keyframes introStar/introTail/introSpark/introShine/introText/introFade/introLine` + `.intro-overlay.dismissed`
   - JS: WebAudio `playIntroSound()` creates a ta-dum (110Hz→52Hz boom + 220Hz triangle + 880Hz shimmer), `initIntro()` shows overlay once per session via `sessionStorage`, auto-dismiss 2.4s, click to dismiss early, sound on first user interaction.

## Install / upgrade steps after PR merge

### 1. Pull latest
```bash
git checkout main
git pull origin main
# or if you are on your fork:
git pull upstream main
```

### 2. Check version
```bash
python3 -c "from autoshorts import __version__; print(__version__)"
# should print 0.6.2
```

### 3. Update Python deps (if needed)
```bash
pip install -U yt-dlp
# ffmpeg must be present
ffmpeg -version
# if missing: Termux -> pkg install ffmpeg, Ubuntu -> apt install ffmpeg
```

### 4. (Optional) Regenerate logo PNGs
Only if you edit the geometry. Requires Pillow (dev-only):
```bash
pip install pillow
python3 tools/make_logo.py
```

### 5. Run Qyro
```bash
python3 run.py
# or
uvicorn autoshorts.server:app --host 0.0.0.0 --port 8000
# stdlib fallback:
python3 -m autoshorts.server_stdlib
```
Open http://localhost:8000 — you should see the intro animation + sound, new logo in header, captions toggle in Fine-tune, and no flicker when Generate is working.

### 6. Verify features
- **Logo**: header shows violet→cyan Q with sparkle
- **Captions toggle**: Episode card → Fine-tune → first control "captions on" — uncheck, Generate, clip has no burned captions
- **Flicker**: Start a Generate job, try opening Fine-tune selects — they keep focus, buttons don't flash every 1.5s
- **Intro**: Refresh with cleared sessionStorage → intro plays once, click to skip

### 7. If you deployed before
- Delete old `data/state.json` only if you want fresh defaults (not required)
- Clear browser cache / service worker: DevTools → Application → Clear storage
- PWA icons will update automatically from `manifest.webmanifest`

## API change
`POST /api/episodes/{id}/shorts`, `/preview`, `/manual`, `/clips/{id}/rerender`, `/batch` now accept:
```json
{ "captions_enabled": true }
```
boolean, defaults to true. Validation in `maintenance.py`.

## Files changed
- `autoshorts/__init__.py`, `config.py`, `maintenance.py`, `pipeline.py`
- `web/index.html`, `web/app.js`, `web/style.css`, `web/logo.svg`, `web/icons/*`
- `tools/make_logo.py` (dev tool)
- `tests/test_units_v061.py` version bump
- New: `UPGRADE_6.2.md` (this file)
