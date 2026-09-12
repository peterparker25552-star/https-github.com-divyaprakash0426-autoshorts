# Qyro v6.8 — Upgrade Guide (the intro that only ever played once)

The install/verify path for **v0.6.8**. One user report:

* *"The app intro is not coming while I am opening it. I can access it from
  inside the app, but it should come when I open the app."*

"I can access it from inside the app" is the header sparkle button, which calls
`QyroIdent.replay()` — a path that clears both seen-flags and bypasses every
gate. That it worked while the launch did not *is* the diagnosis: the ident was
never broken, the decision to play it was. No render geometry, option or route
contract changed.

## What changed in v6.8

1. **`boot()` now tells a launch from a reload (`web/intro.js`).** The
   seen-once flags were written for the *reload* v6.6 fixed — a pull-to-refresh
   while a render hogs the CPU, a tab Chrome discarded and restored — and were
   applied to every load. An installed app keeps its `sessionStorage` for as
   long as its WebView process lives, so every open after the very first was
   silent. `navKind()` reads the navigation type (`performance.getEntriesByType
   ("navigation")`, with the legacy `performance.navigation` fallback an old
   Android WebView needs): a `navigate` — a launcher tap, a fresh tab, a cold
   WebView — is greeted even with both flags set, and a `reload` keeps the v6.6
   rule exactly as it was. An engine that will not say counts as reload-like, so
   nothing that used to be suppressed starts playing by accident.
2. **Coming back to the foreground is an open too (`web/intro.js`).** An
   installed PWA or WebView often never reloads at all — tapping the icon just
   brings the existing document back, so no load event ever fired and nothing
   ran. `visibilitychange` (and `pageshow` with `persisted`, for a back/forward
   cache restore) now measures the absence: hidden for longer than
   `RESUME_AFTER` (30 s) and the ident greets you again, because that *was* an
   open. A four-second glance at another app is not, and stays silent. Hiding
   still puts the stage away at once, exactly as v6.6 required.
3. **A page created hidden holds its greeting (`web/intro.js`).** An Android
   WebView is routinely built a beat before its activity is visible, and
   `requestAnimationFrame` does not run in a hidden page — so the ident used to
   be dismissed by its own deadline before anybody could see it, and the app
   then looked like it had no intro at all. `boot()` now notices
   `document.hidden`, keeps the stage away, and owes the greeting: it is given
   on the first `visibilitychange`/`focus`/`pageshow` where the page really is
   on screen, without having to clear the 30 s absence threshold first.
4. **Busy shortens the greeting instead of cancelling it (`web/app.js`,
   `web/intro.js`).** `initIntro()` used to `return` when a job was queued or
   running. Now it hands `appIsBusy` to the ident with `setBusyProvider()` and
   plays the **short form**: it starts at the burst and curtains just after the
   wordmark, about two seconds instead of six, with the ta-dum intact. The ident
   still never stands between you and a render — it just no longer disappears.
   `?intro=1` and the sparkle button still play all six seconds.
5. **A server that died mid-render stops reporting work nobody is doing
   (`autoshorts/pipeline.py`).** The worker's queue lives in memory; the state
   file does not. A job left `queued`/`running` by a server that was killed —
   Termux swiped away, a laptop lid, Ctrl-C during a render — was never picked
   up again by anything, so `/api/state` said the app was busy *forever*: an
   episode stuck on "processing", a phantom job in the dashboard, and (because
   of item 4 as it used to work) no intro on any open, ever.
   `recover_interrupted_jobs()` runs before the worker starts and parks them as
   ordinary `error` jobs with `error="Interrupted"` — the same shape a backup
   restore uses — so **Retry** re-queues them, and an episode left mid-flight
   with no job at all comes back to `new`.
6. **The greeting no longer waits for the server (`web/app.js`).** `initIntro()`
   was called *after* the first `await refresh()`, which fetches `/api/state`
   **and** `/api/health`. `/api/health` shells out to `ffmpeg -version` and
   `yt-dlp --version` (20 s timeouts) and asks YouTube whether it is reachable
   (`HEAD https://www.youtube.com/robots.txt`, 6 s timeout). On a phone — and on
   any network that blocks YouTube — that is seconds of app on screen before the
   intro appeared, which reads as "no intro", and then lands on top of whatever
   you had started doing. `initIntro()` is now the first statement of
   `DOMContentLoaded`: the ident needs nothing from the server, so it no longer
   waits for one. The busy provider it installs starts answering for real once
   the state arrives, which is what a later resume greeting uses.
7. **The shell cache moved (`web/sw.js`, `autoshorts/config.py`,
   `autoshorts/__init__.py`).** `SHELL = "qyro-v0.6.8-shell"` and version
   `0.6.8`, so an installed home-screen icon cannot keep serving the previous
   `app.js`/`intro.js` — the one fix a PWA user would otherwise never see.
   `initIntro()` also asks whether `setBusyProvider`/`boot` exist before calling
   them, so a half-updated cache (one file fresh, one still cached) cannot throw
   out of `DOMContentLoaded` and take the whole UI with it: a missing greeting
   is a much smaller bug than a dead app, and the CSS fail-safe hides the stage
   when the ident script is the older one.

## Install / upgrade

```bash
git pull                      # get v0.6.8
python -m pip install -r requirements.txt
./run.sh                      # Windows: run.bat
```

Open http://127.0.0.1:8000 — the intro plays on the open, then hands the page
back.

## Install (Android / Termux)

```bash
pkg update && pkg install python ffmpeg git
cd ~/autoshorts && git pull
bash install-android.sh
python run.py
```

Then open the local URL (or the home-screen icon). The versioned shell cache
means the icon picks up the new `intro.js`/`app.js` on its first launch after
the server comes back; if it looks unchanged, close the app fully and open it
again so the WebView process actually restarts.

## Verify in 60 seconds

1. **Open the app — the intro is there.** Cold-start it (swipe the app away
   first). The glowing Q traces, bursts into the spectrum, TA-DUM, QYRO, and the
   app is handed back in under six seconds. It also arrives when the app was
   opened from a cold WebView start, where the page exists before it is visible.
2. **Open it again a minute later.** Put the app in the background for more than
   30 s and come back: the ident greets you again. Glance away for five seconds
   and come back: it does not — you were never really gone.
3. **A reload still opens straight on the app.** Refresh the page inside five
   minutes of an ident: no intro, no black sheet, just Qyro. That is the v6.6
   rule, still standing.
4. **A dead render no longer hides the intro.** If a previous session was killed
   mid-render, the first start now prints
   `[qyro] parked N job(s) the previous server was killed in the middle of`,
   `data/state.json` shows those jobs as `error` / `Interrupted`, the episode is
   back to `new`, the dashboard offers **Retry**, and the next open is greeted
   normally. Start a render and open a second tab while it runs: you get the
   ~2-second ident, not six seconds and not nothing.
5. **The intro does not wait for the network.** Pull the network (or point
   Qyro at a YouTube-blocked connection) and open the app: the ident plays
   immediately rather than after the health probe times out.
6. **Tests** — `python -m tests.run_all` → `ALL GREEN` (new module
   `tests/test_units_v068.py`, and six new scenarios in
   `tests/ident_harness.js`: `coldOpen`, `reloadStorm`, `resume`, `shortAway`,
   `busy`, `busyProvider`, `startHidden`).

## Rollback

`git checkout v0.6.7` (or the previous commit) and restart. Nothing in
`data/state.json` is rewritten by the upgrade except the jobs a *dead* server
had left open, which become retryable `error` jobs — the same thing the
dashboard's Retry button was already for.
