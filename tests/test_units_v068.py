"""v0.6.8 unit tests — the ident greets an *open*, and a dead server's jobs
stop telling the app it is busy.

The report, verbatim: *"The app intro is not coming while I am opening it.
I can access it from inside the app, but it should come when I open the app."*

"The inside the app" half is the header sparkle button, which calls
``QyroIdent.replay()`` — a path that clears both seen-flags and bypasses every
gate. That it worked while the launch did not is the whole diagnosis: the ident
was fine, the *decision to play it* was not. Five things stood in the way.

1. ``boot()`` could not tell a **launch** from a **reload**. The seen-once flags
   were written for the reload v0.6.6 fixed — a pull-to-refresh while a render
   hogs the CPU, a tab Chrome discarded and restored — and were applied to every
   load. An installed app keeps its ``sessionStorage`` for as long as its WebView
   process lives, so every open after the very first one was silent.
2. An installed app often never reloads at all: tapping the icon brings the
   existing document back to the foreground. No load event, no boot call,
   nothing to greet the user with.
3. ``appIsBusy()`` **cancelled** the ident, and a job left ``queued``/``running``
   in ``data/state.json`` by a server that was killed mid-render is never picked
   up again (the worker's queue is in memory) — so ``/api/state`` reported the
   app busy forever and the greeting was cancelled on every open, with an
   episode stuck on "processing" as the visible symptom.
4. A page can be created *while it is still hidden* — an Android WebView is
   routinely built a beat before its activity is visible — and
   ``requestAnimationFrame`` does not run in a hidden page, so the ident was
   dismissed by its own deadline before anybody could see it.
5. The greeting was queued *behind* the first ``/api/state`` + ``/api/health``,
   which on a phone means behind ffmpeg, yt-dlp and a YouTube reachability
   probe. An intro that lands seconds after the app is on screen reads as no
   intro at all — and then covers whatever the user started doing.

Everything here is file text, a temp state file, or the headless node harness:
no network, and ffmpeg is only needed by the shared config import.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from autoshorts import config, pipeline as pipeline_mod
from autoshorts.store import Store

from . import util

WEB = Path(config.BASE_DIR) / "web"
TESTS = Path(config.BASE_DIR) / "tests"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


INTRO_JS = read(WEB / "intro.js")
APP_JS = read(WEB / "app.js")
STYLE_CSS = read(WEB / "style.css")
SW_JS = read(WEB / "sw.js")
PIPELINE_PY = read(Path(config.BASE_DIR) / "autoshorts" / "pipeline.py")


def block(text: str, start: str, end: str = "\n}") -> str:
    """The slice of a JS/CSS source between two markers."""
    head = text[text.index(start):]
    return head[: head.index(end) + len(end)]


def timeline() -> dict:
    """The ``T = {...}`` table from intro.js, parsed rather than duplicated."""
    body = INTRO_JS[INTRO_JS.index("const T = {"):]
    body = body[: body.index("};")]
    return {key: float(value) for key, value in
            re.findall(r"(\w+):\s*([\d.]+),", body)}


def boot_block() -> str:
    return block(INTRO_JS, "function bootIdent(options) {", "\n  }")


def py_func(text: str, start: str) -> str:
    """One top-level Python def, by dedent-to-next-definition."""
    head = text[text.index(start):]
    tail = head[len(start):]
    stop = re.search(r"\n(?=def |class |@)", tail)
    return head[: len(start) + (stop.start() if stop else len(tail))]


# --------------------------------------------------------------------------
# 1 — a launch is not a reload
# --------------------------------------------------------------------------
class LaunchVsReloadTests(unittest.TestCase):
    def test_the_module_asks_the_browser_what_kind_of_load_this_is(self):
        nav = block(INTRO_JS, "function navKind() {", "\n  }")
        self.assertIn('perf.getEntriesByType("navigation")[0]', nav)
        self.assertIn("entry.type", nav)
        # an old Android WebView has only the legacy API
        self.assertIn("perf.navigation", nav)
        self.assertIn('["navigate", "reload", "back_forward"]', nav)
        # …and an engine that will not say must not be guessed at
        self.assertIn('return "unknown";', nav)
        self.assertIn("try {", nav)

    def test_an_unanswered_navigation_type_falls_back_to_the_v66_rule(self):
        """Unknown counts as reload-like: nothing suppressed starts playing."""
        boot = boot_block()
        self.assertIn('const launched = opts.launch === true || navKind() === "navigate";',
                      boot)
        self.assertIn("if (!forced && !launched && "
                      '(read(STORE_SEEN) === "1" || shownRecently())) {', boot)
        # the seen-away branch still puts the stage away on the first frame
        self.assertIn('overlay.classList.add("dismissed", "live");', boot)
        self.assertIn("return null;", boot)

    def test_a_launch_ignores_both_seen_flags(self):
        """The session flag outlives every open of an installed app."""
        boot = boot_block()
        self.assertNotIn("if (!forced && (read(STORE_SEEN)", boot)
        # and the cool-down is documented as a *reload* guard, not a launch one
        self.assertIn("RELOAD_COOLDOWN = 300", INTRO_JS)
        comment = INTRO_JS[: INTRO_JS.index("const RELOAD_COOLDOWN")]
        self.assertIn("does NOT replay the ident", comment)

    def test_the_reload_cool_down_is_still_written_to_local_storage(self):
        self.assertIn("storeLocal(STORE_SEEN_AT, Date.now())", INTRO_JS)
        self.assertIn("function shownRecently()", INTRO_JS)

    def test_a_running_ident_is_never_restarted(self):
        """A resume and a pageshow can land in the same tick."""
        boot = boot_block()
        self.assertIn("if (state && !state.done) return state;", boot)

    def test_the_header_button_and_the_query_string_still_win(self):
        boot = boot_block()
        self.assertIn('new URLSearchParams(window.location.search).get("intro") === "1"',
                      boot)
        self.assertIn("if (forced) return play({ persist: false });", boot)
        self.assertIn("window.QyroIdent.replay()", APP_JS)

    def test_the_module_announces_the_version_it_ships(self):
        self.assertIn('version: "6.8-spectrum"', INTRO_JS)


# --------------------------------------------------------------------------
# 2 — an installed app that never reloads still has to say hello
# --------------------------------------------------------------------------
class ResumeIsAnOpenTests(unittest.TestCase):
    def handler(self) -> str:
        return block(INTRO_JS, 'document.addEventListener("visibilitychange"',
                     "\n  });")

    def test_the_threshold_is_a_named_number_in_a_sane_range(self):
        self.assertIn("const RESUME_AFTER = 30;", INTRO_JS)
        seconds = int(re.search(r"RESUME_AFTER = (\d+);", INTRO_JS).group(1))
        self.assertGreaterEqual(seconds, 10, "a glance at another app is not an open")
        self.assertLessEqual(seconds, 120, "…and an open must still be greeted")

    def test_coming_back_to_the_foreground_plays_the_ident(self):
        handler = self.handler()
        self.assertIn("const awaySeconds = hiddenAt ? (Date.now() - hiddenAt) / 1000 : 0;",
                      handler)
        self.assertIn("if (!owed && awaySeconds < RESUME_AFTER) return;", handler)
        self.assertIn("bootIdent({ launch: true });", handler)

    def test_a_brief_glance_away_does_not_replay_it(self):
        """The same handler, the other branch: no absence, no greeting."""
        handler = self.handler()
        self.assertLess(handler.index("if (!owed && awaySeconds < RESUME_AFTER) return;"),
                        handler.index("bootIdent({ launch: true });"))
        self.assertIn("hiddenAt = 0;", handler)

    def test_hiding_still_puts_the_stage_away_at_once(self):
        """The v0.6.6 guarantee is untouched by the new resume half."""
        handler = self.handler()
        self.assertIn("if (document.hidden) {", handler)
        self.assertIn("hiddenAt = Date.now();", handler)
        self.assertIn("if (sound && sound.playing) sound.halt();", handler)
        self.assertIn("finish(false);", handler)
        self.assertLess(handler.index("sound.halt();"), handler.index("awaySeconds"))

    def test_a_page_created_hidden_holds_its_greeting(self):
        """An Android WebView is often built a beat before it is visible: rAF
        does not run while the page is hidden, so an ident started then would
        be dismissed by its own deadline before anybody saw it."""
        boot = boot_block()
        self.assertIn("if (document.hidden) {", boot)
        self.assertIn("pendingLaunch = true;", boot)
        # the stage stays away while the greeting is owed
        self.assertIn('overlay.classList.add("dismissed", "live");', boot)
        handler = block(INTRO_JS, 'document.addEventListener("visibilitychange"',
                        "\n  });")
        self.assertIn("const owed = pendingLaunch;", handler)
        self.assertIn("if (!owed && awaySeconds < RESUME_AFTER) return;", handler)

    def test_an_owed_greeting_is_collected_even_without_a_visibility_event(self):
        collector = block(INTRO_JS, "function collectPendingLaunch() {", "\n  }")
        self.assertIn("if (!pendingLaunch || document.hidden) return;", collector)
        self.assertIn("bootIdent({ launch: true });", collector)
        self.assertIn('window.addEventListener("focus", collectPendingLaunch);', INTRO_JS)
        self.assertIn('window.addEventListener("pageshow", collectPendingLaunch);',
                      INTRO_JS)

    def test_a_back_forward_cache_restore_is_also_an_open(self):
        pageshow = block(INTRO_JS, 'window.addEventListener("pageshow"', "\n  });")
        self.assertIn("if (!event || !event.persisted) return;", pageshow)
        self.assertIn("bootIdent({ launch: true });", pageshow)


# --------------------------------------------------------------------------
# 3 — busy shortens the greeting instead of cancelling it
# --------------------------------------------------------------------------
class BusyShortensInsteadOfCancellingTests(unittest.TestCase):
    def test_the_app_no_longer_cancels_the_ident_when_a_job_is_running(self):
        gate = block(APP_JS, "function initIntro() {", "\n}")
        self.assertNotIn("appIsBusy() &&", gate,
                         "a busy app must not skip the greeting any more")
        self.assertEqual(gate.count("return;"), 1,
                         "the only early return left is 'the ident script is missing'")
        self.assertIn("if (!identAvailable()) return;", gate)
        self.assertIn("window.QyroIdent.setBusyProvider(appIsBusy);", gate)
        self.assertIn("window.QyroIdent.boot({ busy: appIsBusy() });", gate)

    def test_the_busy_test_itself_is_unchanged(self):
        busy = block(APP_JS, "function appIsBusy() {", "\n}")
        self.assertIn('job.status === "queued" || job.status === "running"', busy)

    def test_a_busy_open_gets_the_short_form_of_the_ident(self):
        play = block(INTRO_JS, "  function play(options) {", "\n  }")
        self.assertIn("const quick = opts.quick === true && !reduced;", play)
        self.assertIn("state.endAt = reduced ? T.word + 0.9 : quick ? T.word + 0.45 : T.end;",
                      play)
        # it starts at the burst, so the ta-dum still lands where it should
        self.assertIn("if (reduced || quick) {", play)
        self.assertIn("state.clock = T.ta - 0.06;", play)

    def test_the_short_form_still_says_qyro_and_still_fits_the_deadline(self):
        t = timeline()
        quick_end = t["word"] + 0.45
        self.assertGreater(quick_end, t["word"], "the wordmark must still land")
        self.assertLess(quick_end, t["end"], "…and it must be shorter than six seconds")
        self.assertLess(quick_end - (t["ta"] - 0.06), 2.6,
                        "a busy open should be greeted in about two seconds")
        slack = float(re.search(r"DEADLINE_SLACK = ([\d.]+);", INTRO_JS).group(1))
        self.assertLessEqual(quick_end + t["holdMax"] + slack, 12.0,
                             "the short form must stay inside the CSS hard hide")

    def test_the_busy_answer_comes_from_app_js_and_cannot_throw(self):
        self.assertIn("setBusyProvider(fn) { busyProvider = "
                      'typeof fn === "function" ? fn : null; },', INTRO_JS)
        helper = block(INTRO_JS, "function isBusy() {", "\n  }")
        self.assertIn('if (typeof busyProvider !== "function") return false;', helper)
        self.assertIn("try { return !!busyProvider(); } catch (error) { return false; }",
                      helper)
        boot = boot_block()
        self.assertIn("const busy = opts.busy === undefined ? isBusy() : !!opts.busy;",
                      boot)
        self.assertIn("return play({ quick: busy });", boot)

    def test_an_explicit_forced_intro_is_never_shortened(self):
        boot = boot_block()
        self.assertLess(boot.index("if (forced) return play({ persist: false });"),
                        boot.index("const busy ="))


# --------------------------------------------------------------------------
# 4 — the greeting does not wait for the server
# --------------------------------------------------------------------------
class GreetingGoesFirstTests(unittest.TestCase):
    def test_the_ident_runs_before_any_fetch(self):
        boot = block(APP_JS, 'window.addEventListener("DOMContentLoaded"', "\n});")
        self.assertIn("initIntro();", boot)
        self.assertLess(boot.index("initIntro();"), boot.index("await refresh();"),
                        "the greeting must not wait for /api/state + /api/health")
        self.assertLess(boot.index("initIntro();"), boot.index('$("#loadPlaylist")'),
                        "…or for the listener wiring either")

    def test_a_half_updated_cache_cannot_kill_the_boot_handler(self):
        """A previous release's cached intro.js next to a fresh app.js must not
        throw out of DOMContentLoaded: a missing greeting beats a dead UI."""
        gate = block(APP_JS, "function initIntro() {", "\n}")
        self.assertIn('typeof window.QyroIdent.setBusyProvider === "function"', gate)
        self.assertIn('typeof window.QyroIdent.boot === "function"', gate)
        # …and the CSS fail-safe is what hides the stage when boot() is absent
        self.assertIn(".intro-overlay:not(.live)", STYLE_CSS)
        self.assertIn("introFailSafe", STYLE_CSS)

    def test_it_is_called_exactly_once(self):
        self.assertEqual(APP_JS.count("initIntro();"), 1)
        self.assertEqual(APP_JS.count("function initIntro() {"), 1)

    def test_the_reason_is_written_down_where_the_next_reader_will_look(self):
        comment = APP_JS[APP_JS.index("/* The greeting goes first"):
                         APP_JS.index("function initIntro() {")]
        self.assertIn("/api/health", comment)
        self.assertIn("ffmpeg", comment)
        self.assertIn("yt-dlp", comment)

    def test_what_the_greeting_used_to_wait_for_is_genuinely_slow(self):
        """/api/health shells out to ffmpeg and yt-dlp and probes YouTube with a
        six-second timeout — seconds on a phone, and every one of them used to
        be spent before the ident was allowed to start."""
        root = Path(config.BASE_DIR) / "autoshorts"
        youtube_src = read(root / "youtube.py")
        reach = py_func(youtube_src, "def check_reachable(")
        self.assertIn("urlopen", reach)
        self.assertGreaterEqual(
            float(re.search(r"timeout: float = ([\d.]+)", reach).group(1)), 3.0)
        maintenance_src = read(root / "maintenance.py")
        versions = py_func(maintenance_src, "def versions_info(")
        self.assertIn("_ffmpeg_version()", versions)
        self.assertIn("_ytdlp_version()", versions)
        self.assertIn("subprocess", py_func(maintenance_src, "def _ffmpeg_version("))
        # both servers put those two calls behind GET /api/health
        for name in ("server.py", "server_stdlib.py"):
            with self.subTest(server=name):
                source = read(root / name)
                self.assertIn("versions_info()", source)
                self.assertIn("youtube_reachable()", source)


# --------------------------------------------------------------------------
# 5 — a server that died mid-render must not report work nobody is doing
# --------------------------------------------------------------------------
class StaleJobRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "state.json")
        util.fresh_state(self.store)
        self.store.upsert_episode({
            "id": "ep-stale", "title": "Killed mid-render", "url": "u",
            "status": "processing", "clips": [], "added_at": time.time(),
        })

    def test_a_running_job_left_by_a_dead_server_is_parked(self):
        job = self.store.create_job("ep-stale", {"kind": "auto"})
        self.store.update_job(job["id"], status="running", step="media",
                              progress=0.4)
        self.assertTrue(self.store.active_jobs(), "the fixture must look busy")

        parked = pipeline_mod.recover_interrupted_jobs(self.store)

        self.assertEqual(parked, 1)
        after = self.store.job(job["id"])
        self.assertEqual(after["status"], "error")
        self.assertEqual(after["step"], "error")
        self.assertEqual(after["error"], "Interrupted")
        self.assertIn("Retry", after["message"])
        self.assertEqual(self.store.active_jobs(), [],
                         "/api/state must stop reporting the app as busy")

    def test_a_queued_job_nobody_will_ever_pick_up_is_parked_too(self):
        """The worker's queue is in memory, so 'queued' is just as dead."""
        job = self.store.create_job("ep-stale", {"kind": "auto"})
        self.assertEqual(pipeline_mod.recover_interrupted_jobs(self.store), 1)
        self.assertEqual(self.store.job(job["id"])["status"], "error")

    def test_the_episode_comes_off_processing(self):
        job = self.store.create_job("ep-stale", {"kind": "auto"})
        self.store.update_job(job["id"], status="running")
        pipeline_mod.recover_interrupted_jobs(self.store)
        episode = self.store.get_episode("ep-stale")
        self.assertEqual(episode["status"], "new")
        self.assertIsNone(episode["error"])

    def test_an_orphaned_processing_episode_is_reset_as_well(self):
        """The job record can be pruned; the spinner cannot be left running."""
        self.assertEqual(self.store.active_jobs(), [])
        pipeline_mod.recover_interrupted_jobs(self.store)
        self.assertEqual(self.store.get_episode("ep-stale")["status"], "new")

    def test_finished_cancelled_and_failed_jobs_are_left_alone(self):
        kept = []
        for status in ("done", "cancelled", "error"):
            job = self.store.create_job("ep-stale", {"kind": "auto"})
            self.store.update_job(job["id"], status=status, error=None)
            kept.append(job["id"])
        self.assertEqual(pipeline_mod.recover_interrupted_jobs(self.store), 0)
        for jid, status in zip(kept, ("done", "cancelled", "error")):
            self.assertEqual(self.store.job(jid)["status"], status)

    def test_parking_is_idempotent(self):
        self.store.create_job("ep-stale", {"kind": "auto"})
        self.assertEqual(pipeline_mod.recover_interrupted_jobs(self.store), 1)
        self.assertEqual(pipeline_mod.recover_interrupted_jobs(self.store), 0)

    def test_the_pipeline_does_it_before_the_worker_starts(self):
        self.store.create_job("ep-stale", {"kind": "auto"})
        worker = pipeline_mod.Pipeline(self.store)
        self.assertEqual(worker.recovered_jobs, 1)
        self.assertEqual(self.store.active_jobs(), [])
        self.assertEqual(self.store.get_episode("ep-stale")["status"], "new")

    def test_both_servers_build_their_pipeline_at_startup(self):
        """FastAPI and the stdlib twin must both get the recovery for free."""
        for name in ("server.py", "server_stdlib.py"):
            with self.subTest(server=name):
                source = read(Path(config.BASE_DIR) / "autoshorts" / name)
                self.assertIn("pipeline = Pipeline(store)", source)
                # …and both answer /api/state from the active jobs only
                self.assertIn("store.active_jobs()", source)


class RestartedServerReportsIdleTests(unittest.TestCase):
    """End to end, on the Termux path: a restart must look idle to the UI."""

    def test_api_state_has_no_jobs_after_a_restart(self):
        with tempfile.TemporaryDirectory() as raw:
            data = Path(raw)
            state = {
                "settings": {"playlist_url": "", "playlist_title": "", "channel": "",
                             "last_loaded": None, "autopilot": False},
                "episodes": {"ep-x": {"id": "ep-x", "title": "Killed mid-render",
                                      "url": "u", "status": "processing",
                                      "clips": [], "added_at": time.time()}},
                "clips": {},
                "jobs": {"job-x": {"id": "job-x", "episode_id": "ep-x", "params": {},
                                   "status": "running", "step": "render",
                                   "progress": 0.7, "message": "", "error": None,
                                   "created": time.time()}},
            }
            (data / "state.json").write_text(json.dumps(state), encoding="utf-8")

            port = util.free_port()
            proc = util.spawn_server(port, data)
            try:
                util.wait_http_ready(port, timeout=45)
                status, body, _ = util.http_request(
                    "GET", f"http://127.0.0.1:{port}/api/state")
                self.assertEqual(status, 200, body)
                # the app is idle, so the UI has no reason to skip the greeting
                self.assertEqual(body["jobs"], [],
                                 "a dead server's job was still reported as active")
                episodes = {ep["id"]: ep for ep in body["episodes"]}
                self.assertEqual(episodes["ep-x"]["status"], "new")

                status, jobs, _ = util.http_request(
                    "GET", f"http://127.0.0.1:{port}/api/jobs?limit=5")
                self.assertEqual(status, 200, jobs)
                parked = {job["id"]: job for job in jobs["jobs"]}["job-x"]
                self.assertEqual(parked["status"], "error")
                self.assertEqual(parked["error"], "Interrupted")
                # the state file on disk agrees, so the next start is quiet too
                on_disk = json.loads((data / "state.json").read_text(encoding="utf-8"))
                self.assertEqual(on_disk["jobs"]["job-x"]["status"], "error")
                self.assertEqual(on_disk["episodes"]["ep-x"]["status"], "new")
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()


# --------------------------------------------------------------------------
# 6 — the shipped artefacts an installed app actually receives
# --------------------------------------------------------------------------
class VersionAndShellCacheTests(unittest.TestCase):
    def test_version_bumped_for_the_launch_fix(self):
        import autoshorts

        self.assertGreaterEqual(config.APP_VERSION, "0.6.8")
        self.assertEqual(autoshorts.__version__, config.APP_VERSION)

    def test_the_shell_cache_moved_so_an_installed_pwa_gets_the_fix(self):
        """A cached app.js/intro.js is the one fix a PWA user would never see."""
        match = re.search(r'const SHELL = "qyro-v(\d+)\.(\d+)\.(\d+)-shell"', SW_JS)
        self.assertIsNotNone(match, SW_JS[:200])
        self.assertGreaterEqual(tuple(int(part) for part in match.groups()), (0, 6, 8))

    def test_the_release_notes_say_what_changed(self):
        notes = read(Path(config.BASE_DIR) / "UPGRADE_6.8.md")
        for needle in ("intro", "open", "reload", "Interrupted"):
            self.assertIn(needle, notes)

    def test_no_emoji_sneaked_into_the_ui_with_the_new_comments(self):
        ranges = ((0x1F000, 0x1FAFF), (0x2600, 0x26FF), (0x2700, 0x27BF),
                  (0xFE00, 0xFE0F))
        hits = []
        for path in sorted(WEB.rglob("*")):
            if path.suffix not in (".html", ".css", ".js", ".webmanifest", ".svg"):
                continue
            for ch in path.read_text(encoding="utf-8", errors="replace"):
                if any(lo <= ord(ch) <= hi for lo, hi in ranges):
                    hits.append(f"{path.name}: U+{ord(ch):04X}")
        self.assertEqual(hits, [], f"emoji in the UI: {sorted(set(hits))}")


# --------------------------------------------------------------------------
# 7 — run the module for real, in the harness that owns the rAF clock
# --------------------------------------------------------------------------
@unittest.skipUnless(shutil.which("node"), "node is not installed")
class LaunchHarnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        proc = subprocess.run(
            [shutil.which("node"), str(TESTS / "ident_harness.js")],
            capture_output=True, text=True, timeout=180,
        )
        cls.proc = proc
        cls.results = {}
        if proc.returncode == 0:
            cls.results, _ = json.JSONDecoder().raw_decode(proc.stdout)

    def test_harness_passes_with_the_launch_scenarios(self):
        self.assertEqual(self.proc.returncode, 0,
                         f"harness failed:\n{self.proc.stderr[-2500:]}")

    def result(self, key: str) -> dict:
        self.assertIn(key, self.results, f"the harness no longer runs '{key}'")
        return self.results[key]

    def test_a_cold_open_after_a_seen_session_still_plays(self):
        """The exact report: flags set, four seconds old, and the app opened."""
        cold = self.result("coldOpen")
        self.assertFalse(cold.get("error"), cold.get("error"))
        self.assertGreaterEqual(cold["contexts"], 1, "no audio graph was built")
        self.assertGreater(cold["cues"], 20, "the score was never scheduled")
        self.assertTrue(cold["classes"]["live"])
        self.assertTrue(cold["classes"]["dismissed"], "the stage never came down")
        self.assertTrue(cold["classes"]["lockupIn"], "the wordmark never landed")
        self.assertIsNotNone(cold["localSeenAt"], "the launch did not re-stamp")

    def test_the_same_flags_on_a_reload_still_show_the_app(self):
        """The v0.6.6 fix must survive the v0.6.8 one."""
        storm = self.result("reloadStorm")
        self.assertFalse(storm.get("error"), storm.get("error"))
        self.assertEqual(storm["contexts"], 0)
        self.assertEqual(storm["cues"], 0)
        self.assertTrue(storm["classes"]["dismissed"])
        self.assertTrue(storm["classes"]["live"])

    def test_coming_back_after_a_minute_greets_the_user_again(self):
        resume = self.result("resume")
        self.assertFalse(resume.get("error"), resume.get("error"))
        self.assertTrue(resume["resumed"], "the harness never brought it back")
        self.assertGreater(resume["resumeFrames"], 40,
                           "nothing was painted after the return")
        self.assertGreater(resume["resumeCues"], 20,
                           "the second greeting was silent")
        self.assertEqual(resume["contexts"], 2, "one audio context per greeting")
        self.assertTrue(resume["classes"]["dismissed"],
                        "the second greeting left the stage over the app")

    def test_a_short_glance_away_does_not_replay_it(self):
        short = self.result("shortAway")
        self.assertFalse(short.get("error"), short.get("error"))
        self.assertTrue(short["resumed"])
        self.assertEqual(short["resumeFrames"], 0)
        self.assertEqual(short["resumeCues"], 0)

    def test_a_busy_open_gets_about_two_seconds_not_six_and_not_nothing(self):
        for key in ("busy", "busyProvider"):
            with self.subTest(scenario=key):
                busy = self.result(key)
                self.assertFalse(busy.get("error"), busy.get("error"))
                self.assertTrue(busy["classes"]["dismissed"])
                self.assertGreater(busy["cues"], 0, "the busy greeting was silent")
                self.assertTrue(busy["classes"]["lockupIn"],
                                "the busy greeting never said QYRO")
                self.assertIsNotNone(busy["endedWall"])
                self.assertLessEqual(busy["endedWall"], 3.6,
                                     f"{key}: the short form ran {busy['endedWall']}s")
                full = self.result("desktop")["endedWall"]
                self.assertLess(busy["endedWall"], full - 2.0,
                                f"{key}: not actually shorter than the full ident")

    def test_a_page_created_hidden_gets_its_greeting_when_it_appears(self):
        """The platform case behind the report: a WebView built before its
        activity is visible used to spend the ident on nobody."""
        hidden = self.result("startHidden")
        self.assertFalse(hidden.get("error"), hidden.get("error"))
        self.assertEqual(hidden["contextsAtResume"], 0,
                         "an ident was built while the page was still hidden")
        self.assertTrue(hidden["resumed"])
        self.assertEqual(hidden["contexts"], 1, "the owed greeting never arrived")
        self.assertGreater(hidden["cues"], 20)
        self.assertGreater(hidden["resumeFrames"], 40,
                           "nothing was painted once the page became visible")
        self.assertTrue(hidden["classes"]["dismissed"],
                        "the late greeting left the stage over the app")

    def test_every_scenario_still_hands_the_page_back(self):
        """The v0.6.6 contract, re-checked across all 22 scenarios."""
        for key, result in self.results.items():
            with self.subTest(scenario=key):
                self.assertFalse(result.get("error"), f"{key}: {result.get('error')}")
                self.assertTrue(result.get("classes", {}).get("dismissed"),
                                f"{key}: the overlay left covering the app")


if __name__ == "__main__":
    unittest.main(verbosity=2)
