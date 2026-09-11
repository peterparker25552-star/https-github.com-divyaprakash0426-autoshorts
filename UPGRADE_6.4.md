# Qyro v6.4 — Upgrade Guide (the ion logo)

This is the install/verify path for **v0.6.4**, which replaces the app logo with the
**chrome "ion Q"** and retunes the entire UI to that mark's colours. It is a
**cosmetic release**: no pipeline, API, render option or intro behaviour changed.

## What changed in v6.4

1. **New mark — the chrome "ion Q"**
   - A thick chrome crescent **bowl** (the Q) whose tips taper to points at the
     lower-left opening, lit by an electric-blue rim glow.
   - A sharp double-pointed **blade tail** crossing the bowl to the lower right. The
     blade is punched out of the bowl and the orbit with a slightly fatter copy of
     itself, which is what makes it read as passing *in front* of them.
   - A thin tilted **orbit ellipse** running behind the whole mark.
   - A four-point **spark** at the upper right.
   - Palette: chrome `#FFFFFF → #E4EDFF → #A8C6F2 → #5F8DD6` lit from the upper left,
     ion blue `#1E5BFF` for the bloom, ice `#9FC9FF` in the orbit, on `#01030B`.
2. **One geometry, every asset — `tools/make_logo.py` rewritten**
   - The arcs, the blade beziers, the orbit rims and the spark are sampled **once** and
     emitted as both SVG path data and Pillow polygons, so `web/logo.svg`,
     `web/icons/favicon.svg`, the inline copy in `web/app.js` and all six PNGs are the
     same curves. A test asserts the three vector copies are byte-identical to the
     generator's output.
   - The header copy's gradient/filter ids are prefixed `h` (`hqchrome`, `hqbloom`, …)
     so they cannot collide with the intro's `qspec`/`qhot` defs in the same document.
   - Still dev-only: Pillow is **not** a runtime dependency, the PNGs are checked in.
3. **The UI now matches the logo (`web/style.css`)**
   - `--violet → #1E5BFF`, `--violet-2 → #4D8CFF`, `--cyan → #9FC9FF`,
     `--bg → #01030B`, `--bg-2 → #04081A`.
   - Panels, lines and hovers moved from white alpha to **blue-tinted** alpha
     (`rgba(120,170,255,…)`) — that tint is what gives the app its cold depth instead of
     looking like grey glass on black.
   - `--grad` (primary buttons, checkboxes, score bars, progress fills) is now
     `#1E5BFF → #4D8CFF → #D6E6FF`; `--shadow-2`, the two ambient `.glow` radials, focus
     rings, `::selection`, sliders, badges, drop zones and the scrollbar all follow.
   - Ink on gradient buttons is `#01030B` so it stays legible on the lighter ramp end.
4. **One source of truth for the brand**
   - `config.BRAND_VIOLET/BRAND_CYAN/BRAND_BG` carry the new values (names kept for
     backwards compatibility), and `tests/test_http_v050.py` now asserts `web/logo.svg`
     contains **those constants** rather than hard-coded hexes — so the logo and the
     theme cannot silently diverge again.
   - `theme_color`/`background_color` in the manifest and the `theme-color` meta move to
     `#01030B`, so the PWA splash and the Android status bar match.
5. **Deliberately left alone**
   - The **v6.3 spectrum ident** keeps its rainbow ramp — the full spectrum is the point
     of that animation, and it is the one place the old colours still belong.
   - The **caption brand previews** (`.brandpreview`) keep violet/cyan because they
     simulate the *rendered caption*, whose ASS colours live in
     `config.CAPTION_BRAND_PRESETS` and were not touched.
6. **Housekeeping**
   - Version `0.6.3 → 0.6.4` (`autoshorts/__init__.py`, `autoshorts/config.py`).
   - Service worker shell `qyro-v0.6.3-spectrum-shell → qyro-v0.6.4-ion-shell` so an
     installed PWA cannot keep the old purple icon and splash.
   - `tests/test_units_v061.py` and `tests/test_units_v063.py` version pins relaxed to
     floors, so future bumps don't break suites that only meant to defend a minimum.

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
git fetch origin arena/01a09184-https-github-com-divyaprakash0
git checkout arena/01a09184-https-github-com-divyaprakash0
```

## 2 — Check you really got 6.4

```bash
python3 -c "from autoshorts import __version__; print(__version__)"   # -> 0.6.4
grep -c "1E5BFF" web/logo.svg                                         # -> 1 or more
grep "ion-shell" web/sw.js                                            # -> qyro-v0.6.4-ion-shell
```

No new Python dependency and no new binary asset beyond the regenerated icons:
`requirements.txt` is unchanged.

## 3 — Desktop / laptop install

```bash
./install.sh          # macOS & Linux  (creates ./.venv, installs deps)
./run.sh              # then open http://localhost:8000
```

Windows: double-click `install.bat`, then `run.bat`.

If you already installed an earlier version, nothing needs reinstalling for v6.4 —
the change is frontend + version string. `./run.sh` is enough.

## 4 — Android (Termux) install

In **Termux from F-Droid** (`https://f-droid.org/packages/com.termux/`), one line:

```bash
pkg update -y && pkg install -y curl && curl -fsSL https://raw.githubusercontent.com/peterparker25552-star/https-github.com-divyaprakash0426-autoshorts/main/android-install.sh -o "$HOME/autoshorts-android-install.sh" && bash "$HOME/autoshorts-android-install.sh"
```

It re-clones the repo, so it always lands on the merged version, and the banner prints
whatever `autoshorts.__version__` says (should be `v0.6.4`). Afterwards:

```bash
bash ~/start-autoshorts.sh
```

then open `http://localhost:8000` in Chrome. To run the branch before the merge, export
the ref first:

```bash
AUTOSHORTS_REF=arena/01a09184-https-github-com-divyaprakash0 bash "$HOME/autoshorts-android-install.sh"
```

## 5 — Clear the old shell cache (important for the icon!)

The PWA caches the app shell **and the icons**. If you installed Qyro to your home
screen before, Android may keep showing the old purple flower until the new shell wins:

- Easiest: hard reload (`Ctrl+Shift+R` / pull-to-refresh on Android) so `sw.js`
  re-registers under the new cache name.
- Or DevTools → **Application** → **Storage** → *Clear site data*, then reload.
- Or DevTools → Application → Service Workers → **Unregister**, reload.
- No DevTools on Android? Chrome → `⋮` → History → Clear browsing data → *Cached images
  and files*, then reload once.
- **Home-screen icon still old?** Android caches the launcher icon aggressively —
  remove the shortcut and re-add it via Chrome → `⋮` → *Add to Home screen*.

## 6 — Verify the look by hand

1. Header, top left: the chrome Q with the blue glow, blade tail and spark — not the
   six-petal flower.
2. Browser tab favicon and, if installed, the home-screen icon show the same mark.
3. **Load** (the primary button) is a blue→ice gradient with dark ink, not purple.
4. Focus a text field: the ring is ion blue.
5. The two ambient glows behind the page are blue, and panels read blue-tinted rather
   than grey.
6. Sliders, score bars, the queued badge and the demo badge all sit in the blue family.
7. Tools → captions: the **brand preview** swatches are *intentionally* still
   violet/cyan — they preview the burned-in caption, not the UI.
8. Reload with `?intro=1`: the spectrum ident is *unchanged*, still a full rainbow.

## 7 — Verify with the tests

```bash
python3 -m tests.run_all                       # 417 tests, all green
python3 -m unittest tests.test_http_v050 -v    # includes the logo/manifest/theme asserts
python3 tools/make_logo.py                     # regenerate the icons (needs Pillow)
```

`tools/make_logo.py` is reproducible: re-running it must leave `git status` clean.

## API / behaviour changes

None. No endpoint, payload, render option or stored key changed. This release is
colour + logo + version string + docs.

## Files changed in v6.4

| File | Change |
| --- | --- |
| `tools/make_logo.py` | rewritten — the ion-Q geometry, shared by the SVG and PNG renderers |
| `web/logo.svg`, `web/icons/favicon.svg` | regenerated from the new geometry |
| `web/icons/*.png` | all six icons regenerated (192/512/maskable/apple-touch/favicon-32) |
| `web/app.js` | inline `LOGO_SVG` replaced with the new mark, ids prefixed `h` |
| `web/style.css` | palette re-derived from the logo; ident block and caption previews untouched |
| `web/index.html`, `web/manifest.webmanifest` | `theme-color` / `theme_color` / `background_color` → `#01030B` |
| `web/sw.js` | shell cache renamed to `qyro-v0.6.4-ion-shell` |
| `autoshorts/config.py` | `BRAND_*` → the new palette, `APP_VERSION 0.6.3 → 0.6.4` |
| `autoshorts/__init__.py` | `0.6.3 → 0.6.4` + release notes |
| `tests/test_http_v050.py` | logo/theme asserts now read `config.BRAND_*` instead of literals |
| `tests/test_units_v061.py`, `tests/test_units_v063.py` | version pins relaxed to floors |
| `README.md`, `ANDROID-GUIDE.md`, `install.sh` | version wording and the new feature section |
| `UPGRADE_6.4.md` | **new** — this file |
