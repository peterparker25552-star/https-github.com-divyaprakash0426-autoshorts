"""v0.6.6 unit tests — the stuck ident, the demo test card, and retired models.

Two bug reports own this file.

1. **"Downloading a video shows only a rainbow screen with a beep."** Two
   separate defects stacked into one experience. The intro ident is a
   full-screen, requestAnimationFrame-driven animation: on a phone whose CPU is
   busy encoding, frames stop arriving, so six seconds of animation stretched
   into minutes of stuck spectrum with the WebAudio ta-dum still playing on its
   own thread. And Qyro's synthetic demo media *is* a rainbow test card with a
   sine tone, so a short cut from it looked exactly like a broken download.
   The ident now answers to the wall clock, carries a dismissal timer that does
   not need frames, and the demo placeholder is labelled in the footage, on the
   card and in the inspector. A placeholder can also no longer masquerade as a
   cached YouTube download.

2. **"AI unavailable during render (HTTP 404 … gemini-2.5-flash is no longer
   available to new users …)"** — the model id stored in an older
   ``data/state.json`` kept being replayed by the Settings form long after the
   default moved on. A retired id is now never called first: the current
   free-tier model answers instead, the user's own id follows when *that* is
   what failed, and the reply is a sentence rather than a JSON dump.

Everything here is file text, temp files or monkeypatched calls: no network,
and the only ffmpeg use is guarded.
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

from autoshorts import config, engine, ffmpeg, maintenance, pipeline as pipeline_mod
from autoshorts import youtube
from autoshorts.store import Store

WEB = Path(config.BASE_DIR) / "web"
TESTS = Path(config.BASE_DIR) / "tests"
HAVE_FFMPEG = Path(config.FFMPEG_BIN).is_file() or shutil.which("ffmpeg") is not None


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


INTRO_JS = read(WEB / "intro.js")
APP_JS = read(WEB / "app.js")
STYLE_CSS = read(WEB / "style.css")
SW_JS = read(WEB / "sw.js")


def block(text: str, start: str, end: str = "\n}") -> str:
    """The slice of a JS/CSS source between two markers (bodies end in "}")."""
    head = text[text.index(start):]
    return head[: head.index(end) + len(end)]


def py_block(text: str, start: str) -> str:
    """One top-level Python def, by dedent-to-next-definition."""
    head = text[text.index(start):]
    tail = head[len(start):]
    stop = re.search(r"\n(?=def |@|class )", tail)
    return head[: len(start) + (stop.start() if stop else len(tail))]


# --------------------------------------------------------------------------
# 1 — the ident must give the page back
# --------------------------------------------------------------------------
class IdentLifecycleTests(unittest.TestCase):
    def test_the_timeline_answers_to_the_wall_clock(self):
        """Dropped frames are skipped, not replayed — the stuck-screen fix."""
        self.assertIn("CATCHUP_SLACK = 0.35", INTRO_JS)
        self.assertIn("const raw = Math.max(0, s.last ? (ts - s.last) / 1000 : 0.016);",
                      INTRO_JS)
        self.assertIn("const dt = Math.min(0.05, raw);", INTRO_JS)
        self.assertIn("s.wall += raw;", INTRO_JS)
        self.assertIn("if (!s.held && onTime > s.clock + CATCHUP_SLACK) "
                      "s.clock = onTime - 0.05;", INTRO_JS)
        # the smooth path (and the audio gate) still uses the clamped step
        self.assertIn("if (!s.held) s.clock += dt;", INTRO_JS)

    def test_a_timer_dismisses_even_when_raf_never_returns(self):
        frame = block(INTRO_JS, "function armDeadline(s) {")
        self.assertIn("window.setTimeout", frame)
        self.assertIn("if (state === s) finish(false);", frame)
        self.assertIn("armDeadline(state);", INTRO_JS)
        # slack enough for the audio gate to be allowed to wait, and cleared on
        # the way out so a normal finish is never doubled
        self.assertIn("s.endAt + T.holdMax + DEADLINE_SLACK", frame)
        slack = float(re.search(r"DEADLINE_SLACK = ([\d.]+);", INTRO_JS).group(1))
        timeline = INTRO_JS[INTRO_JS.index("const T = {"):]
        end = float(re.search(r"end: ([\d.]+),", timeline[:timeline.index("};")]).group(1))
        hold = float(re.search(r"holdMax: ([\d.]+),",
                               timeline[:timeline.index("};")]).group(1))
        self.assertLess(end + hold, end + hold + slack)
        self.assertLessEqual(end + hold + slack, 12.0,
                             "the hard deadline must not be a long wait")
        self.assertIn("window.clearTimeout(s.deadline)", INTRO_JS)

    def test_a_canvas_that_throws_cannot_strand_the_stage(self):
        frame = block(INTRO_JS, "  function frame(ts) {")
        self.assertIn("try {", frame)
        self.assertIn("render(s.clock, s.wall);", frame)
        self.assertIn("s.clock = s.endAt;", frame)

    def test_a_hidden_tab_is_put_away_instead_of_frozen(self):
        """v0.6.8: the same handler now also *greets* a return to the
        foreground, so the hidden half is asserted inside its own branch."""
        handler = block(INTRO_JS, 'document.addEventListener("visibilitychange"',
                        "\n  });")
        self.assertIn("if (document.hidden) {", handler)
        self.assertIn("if (!state) return;", handler)
        self.assertIn("sound.halt();", handler)
        self.assertIn("finish(false);", handler)

    def test_a_reload_storm_does_not_replay_the_ident(self):
        self.assertIn("RELOAD_COOLDOWN = 300", INTRO_JS)
        self.assertIn("function shownRecently()", INTRO_JS)
        # v0.6.8: boot() grew a launch/reload distinction, and the cool-down
        # now only guards the reload half of it (see test_units_v068).
        boot = block(INTRO_JS, "function bootIdent(options) {", "\n  }")
        self.assertIn('read(STORE_SEEN) === "1" || shownRecently()', boot)
        self.assertIn("if (!forced && !launched &&", boot)
        # the stamp is written for the browser, not just for the session
        self.assertIn("storeLocal(STORE_SEEN_AT, Date.now())", INTRO_JS)
        helpers = block(INTRO_JS, "function storeLocal(key, value) {")
        self.assertIn("localStorage", helpers)

    def test_the_stage_is_opt_in_in_css(self):
        """Hidden unless .live: a page whose JS never runs shows the app."""
        overlay = block(STYLE_CSS, ".intro-overlay {")
        self.assertIn("visibility: hidden;", overlay)
        self.assertIn("opacity: 0;", overlay)
        self.assertIn(".intro-overlay.live {", STYLE_CSS)
        live = block(STYLE_CSS, ".intro-overlay.live {")
        self.assertIn("visibility: visible;", live)
        # and a second, JS-independent lock for a .live run that stalls
        self.assertIn(".intro-overlay.live:not(.dismissed)", STYLE_CSS)
        self.assertIn("introHardHide", STYLE_CSS)
        self.assertIn("@keyframes introHardHide", STYLE_CSS)
        self.assertNotIn("blur", block(STYLE_CSS, "@keyframes introHardHide"))

    def test_the_original_fail_safe_is_still_there(self):
        self.assertIn(".intro-overlay:not(.live)", STYLE_CSS)
        self.assertIn("introFailSafe 0.9s ease 6.5s both", STYLE_CSS)

    def test_the_app_knows_whether_it_is_busy(self):
        """v0.6.8 moved the *decision*: busy shortens the ident rather than
        cancelling it, and the greeting no longer waits for /api/state (see
        test_units_v068 for both). The busy test itself is unchanged."""
        self.assertIn("function appIsBusy()", APP_JS)
        self.assertIn('job.status === "queued" || job.status === "running"',
                      APP_JS)
        gate = block(APP_JS, "function initIntro() {", "\n}")
        self.assertIn("window.QyroIdent.setBusyProvider(appIsBusy);", gate)
        self.assertIn("window.QyroIdent.boot({ busy: appIsBusy() });", gate)

    def test_the_shell_cache_moved_so_an_installed_pwa_gets_the_fix(self):
        import re

        self.assertRegex(SW_JS, r'const SHELL = "qyro-v0\.6\.[6-9][^"]*-shell"')

    def test_version_bumped_for_the_lifecycle_fix(self):
        # A floor, not an exact pin: the fixes below shipped in 0.6.6 and
        # every release since must keep them.
        self.assertGreaterEqual(config.APP_VERSION, "0.6.6")
        import autoshorts

        self.assertGreaterEqual(autoshorts.__version__, "0.6.6")


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class IdentStarvationHarnessTests(unittest.TestCase):
    """The behaviour the text assertions above only describe: run the module."""

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

    def test_harness_passes_with_the_new_scenarios(self):
        self.assertEqual(self.proc.returncode, 0,
                         f"harness failed:\n{self.proc.stderr[-2500:]}")

    def test_each_starvation_path_hands_the_page_back(self):
        for key in ("starved", "noFrames", "hiddenTab", "thrown", "cooldown"):
            with self.subTest(scenario=key):
                result = self.results.get(key) or {}
                self.assertTrue(result.get("classes", {}).get("dismissed"),
                                f"{key}: the overlay left covering the app")
                self.assertFalse(result.get("error"), f"{key}: {result.get('error')}")

    def test_a_starved_frame_loop_does_not_stretch_the_ident(self):
        starved = self.results.get("starved") or {}
        # 4 fps used to mean ~30 seconds of stuck spectrum; the wall clock says
        # it is over with in about one timeline length.
        self.assertLessEqual(starved.get("endedWall", 99), 7.5)
        self.assertLess(starved.get("frames", 999), 40)


# --------------------------------------------------------------------------
# 2 — synthetic demo media can never pass for a real download
# --------------------------------------------------------------------------
class PlaceholderMarkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_a_plain_file_is_not_a_placeholder(self):
        real = self.dir / "abc123.mp4"
        real.write_bytes(b"x" * 5000)
        self.assertFalse(ffmpeg.is_placeholder_media(real))
        self.assertFalse(ffmpeg.is_placeholder_media(None))
        self.assertFalse(ffmpeg.is_placeholder_media(self.dir / "missing.mp4"))

    def test_the_marker_is_named_after_the_media_file(self):
        media = self.dir / "abc123.mp4"
        self.assertEqual(ffmpeg.demo_marker(media).name,
                         "abc123.mp4" + ffmpeg.DEMO_MARKER_SUFFIX)
        # and it is never mistaken for a video by the media-dir scans
        self.assertNotIn(media.with_name(media.name + ffmpeg.DEMO_MARKER_SUFFIX).suffix,
                         (".mp4", ".mkv", ".webm", ".m4a", ".mov"))

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg is not installed")
    def test_synth_demo_video_labels_and_marks_itself(self):
        out = self.dir / "demo-x.mp4"
        ffmpeg.synth_demo_video(out, duration=6, hue=30)
        self.assertTrue(out.is_file(), "no placeholder was produced")
        self.assertTrue(ffmpeg.is_placeholder_media(out),
                        "a synthetic card must be recognisable from disk alone")
        marker = json.loads(ffmpeg.demo_marker(out).read_text(encoding="utf-8"))
        self.assertTrue(marker["qyro_placeholder"])
        self.assertIn("NOT A REAL VIDEO", marker["note"])

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg is not installed")
    def test_an_existing_placeholder_is_not_resynthesised(self):
        out = self.dir / "demo-y.mp4"
        ffmpeg.synth_demo_video(out, duration=6, hue=0)
        before = out.stat().st_mtime_ns
        again = ffmpeg.synth_demo_video(out, duration=6, hue=0)
        self.assertEqual(Path(again), out)
        self.assertEqual(before, out.stat().st_mtime_ns,
                         "the cached card should be reused, not re-encoded")

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg is not installed")
    def test_a_card_of_the_wrong_length_is_made_again(self):
        """A 12-second card cannot serve a 96-second transcript — the cut window
        would land past the end and render an empty file."""
        out = self.dir / "demo-w.mp4"
        ffmpeg.synth_demo_video(out, duration=6, hue=0)
        stale = out.stat().st_mtime_ns
        ffmpeg.synth_demo_video(out, duration=20, hue=0)
        self.assertNotEqual(stale, out.stat().st_mtime_ns,
                            "a length mismatch must re-synthesise, not reuse")
        marker = json.loads(ffmpeg.demo_marker(out).read_text(encoding="utf-8"))
        self.assertEqual(marker["duration"], 20)

    def test_the_label_is_narrow_enough_to_survive_a_crop(self):
        """A 9:16 centre crop keeps about a third of the source width, so the
        words have to be stacked rather than spelled across the frame."""
        lines = ffmpeg.DEMO_OVERLAY.replace("\\N", "\n").splitlines()
        widths = [len(re.sub(r"\{[^}]*\}", "", line).strip()) for line in lines]
        self.assertGreaterEqual(len(lines), 2, "the label should stack two lines")
        self.assertLessEqual(max(widths), 18, f"label line too wide: {widths}")

    def test_the_label_file_is_a_valid_one_line_ass(self):
        """The overlay is written in the shape libass expects."""
        text = ffmpeg.demo_label_ass(self.dir, 12).read_text(encoding="utf-8")
        self.assertIn("DEMO", text)
        self.assertIn("NOT REAL FOOTAGE", text)
        self.assertIn(ffmpeg.DEMO_OVERLAY.replace("\\N", "").split("{")[0], text)
        self.assertIn("BorderStyle", text)          # the plate behind the words
        self.assertIn("[Events]", text)
        # the Style line must carry one value per Format field, or libass
        # silently misplaces the colour and the words vanish
        fmt = text[text.index("Format: Name,") + len("Format: Name,"):]
        fmt = fmt[: fmt.index("\n")]
        style = text[text.index("Style: Demo,") + len("Style: Demo,"):]
        style = style[: style.index("\n")]
        self.assertEqual(len(fmt.split(",")), len(style.split(",")),
                         "the ASS Style line has the wrong number of fields")

    def test_the_scratch_label_is_cleaned_up(self):
        """Nothing is left in data/media but the card and its marker."""
        target = ffmpeg.demo_label_ass(self.dir, 6)
        self.assertTrue(target.is_file())
        target.unlink()
        self.assertFalse(list(self.dir.glob("*.ass")))

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg is not installed")
    def test_the_label_is_in_the_picture_not_only_in_the_filename(self):
        """A downloaded demo file says what it is, without the app around."""
        out = self.dir / "demo-z.mp4"
        ffmpeg.synth_demo_video(out, duration=6, hue=0)
        raw = subprocess.run(
            [config.FFMPEG_BIN, "-v", "error", "-ss", "2", "-i", str(out),
             "-frames:v", "1", "-vf", "crop=iw:ih/3:0:ih/3",
             "-f", "rawvideo", "-pix_fmt", "gray", "-"],
            capture_output=True, timeout=180,
        ).stdout
        self.assertGreater(len(raw), 0, "the middle third could not be read")
        # testsrc2's colour bars never go darker than ~76; the plate behind the
        # words (or the white bar fallback) is unmistakable in the band
        darkest = min(raw)
        self.assertLess(darkest, 40,
                        "no label was burned into the placeholder")

    def test_the_fallback_chain_starts_at_the_card_itself(self):
        """A build without libass/drawtext still gets a marked card, never a
        failed render."""
        source = read(Path(config.BASE_DIR) / "autoshorts" / "ffmpeg.py")
        body = py_block(source, "def synth_demo_video(out: Path, duration: int = 90,")
        self.assertIn("for chain in chains:", body)
        self.assertIn("chains.append(bare)", body)
        self.assertIn("if label:", body)


class DownloadCacheTests(unittest.TestCase):
    """``download_video`` must never hand a placeholder to a real render."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self._media = config.MEDIA_DIR
        config.MEDIA_DIR = self.dir
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, config, "MEDIA_DIR", self._media)
        # the yt-dlp pacer would make each test wait four seconds
        self.addCleanup(setattr, youtube, "_pace", youtube._pace)
        youtube._pace = lambda: None

    def _placeholder(self, video_id: str) -> Path:
        path = config.MEDIA_DIR / f"{video_id}.mp4"
        path.write_bytes(b"t" * 20_000)
        ffmpeg.demo_marker(path).write_text('{"qyro_placeholder": true}',
                                            encoding="utf-8")
        return path

    def test_a_real_cached_file_short_circuits(self):
        path = config.MEDIA_DIR / "good123.mp4"
        path.write_bytes(b"t" * 20_000)
        called = []
        original = youtube.run_ytdlp
        youtube.run_ytdlp = lambda *a, **k: called.append(a)
        self.addCleanup(setattr, youtube, "run_ytdlp", original)
        self.assertEqual(youtube.download_video("good123", "u"), path)
        self.assertEqual(called, [], "the cache should have been used")

    def test_a_placeholder_is_replaced_by_the_real_download(self):
        path = self._placeholder("fake456")
        calls = []

        class Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(args, timeout=600):
            calls.append(args)
            # two more bytes than the placeholder, so the assertion below
            # cannot pass by leaving the old file in place
            path.write_bytes(b"r" * 20_001)
            return Proc()

        original = youtube.run_ytdlp
        youtube.run_ytdlp = fake_run
        self.addCleanup(setattr, youtube, "run_ytdlp", original)
        self.assertEqual(youtube.download_video("fake456", "u"), path)
        self.assertEqual(len(calls), 1, "the placeholder must be re-downloaded")
        self.reads = path.read_bytes()
        self.assertTrue(all(byte == ord("r") for byte in self.reads[:20_001]),
                        "the download must actually overwrite the placeholder")
        self.assertFalse(ffmpeg.is_placeholder_media(path),
                         "a real file must not stay marked as a placeholder")

    def test_the_pipeline_refuses_to_cut_a_short_from_a_card(self):
        path = self._placeholder("fake789")
        original = youtube.download_video
        youtube.download_video = lambda video_id, url: path
        self.addCleanup(setattr, youtube, "download_video", original)
        episode = {"id": "fake789", "url": "u", "source": "youtube", "title": "t"}
        with self.assertRaises(RuntimeError) as caught:
            pipeline_mod.Pipeline._prepare_media(episode)
        self.assertIn("demo", str(caught.exception).lower())
        self.assertIn("data/media", str(caught.exception))

    def test_a_demo_episode_still_gets_its_card(self):
        """The gate is on real episodes: demo mode must keep working offline."""
        from autoshorts import demo as demo_mod

        calls = []
        original = demo_mod.prepare_demo_media
        demo_mod.prepare_demo_media = lambda ep: calls.append(ep) or Path("x.mp4")
        self.addCleanup(setattr, demo_mod, "prepare_demo_media", original)
        episode = {"id": "demo-one", "source": "demo", "url": "u", "title": "t"}
        self.assertEqual(pipeline_mod.Pipeline._prepare_media(episode), Path("x.mp4"))
        self.assertEqual(len(calls), 1)


class ClipProvenanceTests(unittest.TestCase):
    def test_demo_source_is_detected_from_the_marker(self):
        clip = {"id": "c1", "episode_id": "vid1", "demo_media": True}
        self.assertEqual(maintenance.clip_source(clip, {"id": "vid1",
                                                        "source": "youtube"}),
                         "demo")

    def test_a_legacy_clip_is_answered_by_its_episode(self):
        # rendered before v0.6.6: no source field at all on the clip
        clip = {"id": "c2", "episode_id": "demo-chhetri-223"}
        episode = {"id": "demo-chhetri-223", "source": "demo"}
        self.assertEqual(maintenance.clip_source(clip, episode), "demo")

    def test_a_real_clip_stays_real(self):
        clip = {"id": "c3", "episode_id": "vid3", "source": "youtube"}
        self.assertEqual(maintenance.clip_source(clip, {"id": "vid3",
                                                       "source": "youtube"}),
                         "youtube")
        self.assertEqual(maintenance.clip_source({"id": "c4", "episode_id": "x"}),
                         "youtube")

    def test_mark_clip_sources_labels_the_whole_grid(self):
        clips = [
            {"id": "a", "episode_id": "demo-one"},
            {"id": "b", "episode_id": "vid-two"},
            {"id": "c", "episode_id": "vid-three", "demo_media": True},
        ]
        episodes = [
            {"id": "demo-one", "source": "demo"},
            {"id": "vid-two", "source": "youtube"},
            {"id": "vid-three", "source": "youtube"},
        ]
        out = maintenance.mark_clip_sources(clips, episodes)
        self.assertEqual([clip["source"] for clip in out],
                         ["demo", "youtube", "demo"])
        self.assertEqual([clip["demo_media"] for clip in out],
                         [True, False, True])


class DemoLabelUiTests(unittest.TestCase):
    def test_the_short_itself_is_badged(self):
        self.assertIn('class="demoflag"', APP_JS)
        self.assertIn("demo media — test card, not a real video", APP_JS)
        self.assertIn("Qyro's offline <b>demo</b> footage", APP_JS)
        self.assertIn("clip.demo_media || clip.source === \"demo\"", APP_JS)

    def test_the_badge_is_styled(self):
        self.assertIn(".demoflag {", STYLE_CSS)
        self.assertIn(".noticeline.demo {", STYLE_CSS)

    def test_the_inspector_reports_the_source(self):
        self.assertIn('kv("Source media"', APP_JS)
        self.assertIn("demo test card", APP_JS)

    def test_the_demo_button_says_what_demo_media_is(self):
        self.assertIn("labelled test card, not real video", APP_JS)

    def test_the_probe_carries_the_provenance(self):
        source = py_block(read(Path(config.BASE_DIR) / "autoshorts" / "maintenance.py"),
                          "def clip_probe(store: Store, clip_id: str) -> dict:")
        self.assertIn('"source": source', source)
        self.assertIn('"demo_media": source == "demo"', source)


# --------------------------------------------------------------------------
# 3 — a download has to arrive as a file
# --------------------------------------------------------------------------
class EmptyRenderGuardTests(unittest.TestCase):
    """A render that produced no frames used to be filed as a finished short."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self._dirs = {}
        for name, folder in (("MEDIA_DIR", "media"), ("CLIPS_DIR", "clips"),
                             ("SUBS_DIR", "subs"), ("THUMBS_DIR", "thumbs"),
                             ("CACHE_DIR", "cache")):
            (self.dir / folder).mkdir(parents=True, exist_ok=True)
            if hasattr(config, name):
                self._dirs[name] = getattr(config, name)
                setattr(config, name, self.dir / folder)
        self.addCleanup(self.restore)
        self.store = Store(self.dir / "state.json")

    def restore(self):
        for name, value in self._dirs.items():
            setattr(config, name, value)
        self.tmp.cleanup()

    def _render_with(self, payload: bytes, duration: float | None):
        from autoshorts import highlights
        from autoshorts.transcripts import Segment

        original_render = pipeline_mod.render_clip
        original_probe = ffmpeg.probe_duration

        def fake_render(media, start, end, out, *args, **kwargs):
            Path(out).write_bytes(payload)

        pipeline_mod.render_clip = fake_render
        ffmpeg.probe_duration = lambda path: duration
        self.addCleanup(setattr, pipeline_mod, "render_clip", original_render)
        self.addCleanup(setattr, ffmpeg, "probe_duration", original_probe)

        self.store.upsert_episode({"id": "vid1", "title": "Ep", "url": "u",
                                   "duration": 96, "source": "youtube",
                                   "status": "new", "clips": [], "added_at": 1})
        moment = highlights.Highlight(start=14.0, end=69.0, score=5.0,
                                      title="A moment worth keeping",
                                      reasons=["hook"], signals={})
        settings = dict(maintenance.RENDER_DEFAULTS)
        settings.update({
            "width": 720, "height": 1280, "speed": 1.0, "progress": False,
            "silence": False, "loud": False, "format": "vertical",
            "logo_box": None, "audio_track": None,
            "audio_track_path": None, "audio_mix": "duck",
            "silence_noise": -35.0, "silence_min": 0.4,
            "sync_beats": False, "quality_gate": False, "language": "auto",
        })
        fake_self = type("S", (), {"store": self.store})()
        segments = [Segment(14.0, 20.0, "He cried in the dressing room twice.")]
        return pipeline_mod.Pipeline._render_one(
            fake_self, {"id": "vid1", "title": "Ep", "url": "u", "duration": 96},
            config.MEDIA_DIR / "vid1.mp4", segments, moment, settings)

    def test_a_card_with_no_bytes_fails_loudly(self):
        with self.assertRaises(RuntimeError) as caught:
            self._render_with(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 40, None)
        message = str(caught.exception)
        self.assertIn("no video", message.lower())
        self.assertIn("data/media/vid1.mp4", message,
                      "the message has to name the file to delete")
        self.assertEqual(self.store.clips(), [], "an empty clip must not be stored")

    def test_a_truncated_file_with_a_header_fails_loudly(self):
        big = b"\x00" * 8192
        with self.assertRaises(RuntimeError) as caught:
            self._render_with(big, 0.0)
        self.assertIn("unplayable", str(caught.exception).lower())
        self.assertEqual(self.store.clips(), [])

    def test_a_real_render_still_passes(self):
        clip = self._render_with(b"\x00" * 8192, 42.0)
        self.assertEqual(clip["episode_id"], "vid1")
        self.assertEqual(clip["source"], "youtube")
        self.assertFalse(clip["demo_media"])
        self.assertEqual(len(self.store.clips()), 1)


class DownloadDispositionTests(unittest.TestCase):
    def test_the_ui_links_ask_for_an_attachment(self):
        self.assertIn('href="/api/clips/${clip.id}/file?dl=1"', APP_JS)
        self.assertIn('href="/api/clips/${clipId}/file?dl=1"', APP_JS)
        # the inline <video> element must keep the plain URL
        self.assertIn('src="/api/clips/${clip.id}/file"', APP_JS)

    def test_fastapi_server_honours_dl(self):
        source = read(Path(config.BASE_DIR) / "autoshorts" / "server.py")
        route = py_block(source, "def clip_file(clip_id: str, dl: int = 0):")
        self.assertIn("attachment; filename=", route)
        self.assertIn("if dl:", route)

    def test_stdlib_server_honours_dl(self):
        source = read(Path(config.BASE_DIR) / "autoshorts" / "server_stdlib.py")
        route = source[source.index('if action == "file":'):source.index('return\n                    ', source.index('if action == "file":'))]
        self.assertIn('query.get("dl")', route)
        self.assertIn("attachment=want in", route)


# --------------------------------------------------------------------------
# 4 — retired model ids
# --------------------------------------------------------------------------
class RetiredModelTests(unittest.TestCase):
    def test_the_google_resource_prefix_is_stripped(self):
        self.assertEqual(config.normalise_model("models/gemini-3.6-flash"),
                         "gemini-3.6-flash")
        self.assertEqual(engine.read({"ai_model": "  models/gemini-2.5-flash "})["model"],
                         "gemini-2.5-flash")
        self.assertEqual(config.normalise_model(None), "")

    def test_qyros_own_default_is_never_retired(self):
        for name in ("GEMINI_MODEL", "GROQ_MODEL"):
            with self.subTest(model=name):
                self.assertFalse(config.is_retired_model(getattr(config, name)),
                                 f"config.{name} points at a retired model")

    def test_the_retired_ids_are_the_ones_that_404(self):
        for model in ("gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash",
                      "gemini-1.5-flash", "llama-3.1-8b-instant",
                      "models/gemini-2.5-flash-preview-05-20"):
            with self.subTest(model=model):
                self.assertTrue(config.is_retired_model(model))
        for model in ("gemini-3.6-flash", "gpt-4o-mini", "openai/gpt-oss-20b", ""):
            with self.subTest(model=model):
                self.assertFalse(config.is_retired_model(model))

    def test_a_retired_id_is_demoted_behind_the_current_default(self):
        cfg = engine.read({"ai_provider": "gemini", "ai_model": "gemini-2.5-flash",
                           "gemini_key": "AQ.Ab8RqXdEXAMPLEKEY123456"})
        self.assertEqual(engine.model_candidates(cfg, "gemini"),
                         [config.GEMINI_MODEL, "gemini-2.5-flash"])
        self.assertEqual(engine.model_for(cfg, "gemini"), config.GEMINI_MODEL)

    def test_a_live_id_is_called_first_and_the_default_follows_it(self):
        cfg = engine.read({"ai_provider": "gemini", "ai_model": "gemini-3.5-flash"})
        self.assertEqual(engine.model_candidates(cfg, "gemini"),
                         ["gemini-3.5-flash", config.GEMINI_MODEL])
        empty = engine.read({"ai_provider": "gemini"})
        self.assertEqual(engine.model_candidates(empty, "gemini"),
                         [config.GEMINI_MODEL])

    def test_the_state_reports_the_model_that_will_actually_be_called(self):
        state = engine.public_state({
            "ai_provider": "gemini", "gemini_key": "AIzaSUPERSECRET123",
            "ai_model": "gemini-2.5-flash",
        })
        self.assertEqual(state["model"], "gemini-2.5-flash")     # not rewritten
        self.assertEqual(state["model_effective"], config.GEMINI_MODEL)
        self.assertIn("retired", state["model_note"])
        gemini = next(p for p in state["providers"] if p["id"] == "gemini")
        self.assertEqual(gemini["model_effective"], config.GEMINI_MODEL)
        clean = engine.public_state({"ai_provider": "gemini", "gemini_key": "k",
                                    "ai_model": "gemini-3.6-flash"})
        self.assertEqual(clean["model_note"], "")

    def test_settings_save_normalises_the_id_but_keeps_it(self):
        update = maintenance.settings_update_from(
            {"ai_model": " models/gemini-2.5-flash "})
        self.assertEqual(update["ai_model"], "gemini-2.5-flash")

    def test_a_reply_without_a_leading_path_is_accepted(self):
        update = maintenance.settings_update_from({"ai_model": "gemini-3.6-flash"})
        self.assertEqual(update["ai_model"], "gemini-3.6-flash")


class EngineCallTests(unittest.TestCase):
    """What actually goes over the wire when a model id is dead."""

    SETTINGS = {
        "ai_provider": "gemini",
        "gemini_key": "AQ.Ab8RqXdEXAMPLEKEY123456",
        "ai_model": "models/gemini-2.5-flash",
    }
    DEAD = {
        "__error__": 'HTTP 404 { "error": { "code": 404, "message": "This model '
                     'models/gemini-2.5-flash is no longer available to new users. '
                     'Please update your code to use models/gemini-3.6-flash for '
                     'the latest features." } }',
        "__status__": 404,
    }
    REFUSED_KEY = {
        "__error__": 'HTTP 401 { "error": { "code": 401, "message": '
                     '"API key not valid. Please pass a valid API key." } }',
        "__status__": 401,
    }

    def setUp(self):
        self._original = engine._post_json
        self.calls: list[tuple[str, dict]] = []
        self.addCleanup(setattr, engine, "_post_json", self._original)

    def _reply(self, model: str) -> dict:
        payload = {
            "titles": ["He cried in the dressing room",
                       "The 94th goal that felt like nothing",
                       "Why 150 caps felt like 150 failures",
                       "What Virat told him once"],
            "hashtags": ["#shorts", "#football"],
            "description": "Sunil Chhetri on fear and the number 150.",
        }
        return {"candidates": [{"content": {"parts": [
            {"text": json.dumps(payload)}]}}]}

    def _serve(self, handler):
        def fake(url, payload, headers, timeout, retries=None):
            model = url.rsplit("/models/", 1)[-1].split(":", 1)[0]
            self.calls.append((model, payload))
            return handler(model)
        engine._post_json = fake
        return fake

    def test_a_retired_id_is_never_called_when_the_default_answers(self):
        self._serve(lambda model: self._reply(model))
        report: dict = {}
        data, provider, notice = engine.ask_json("s", "u", self.SETTINGS,
                                                 timeout=1, report=report)
        self.assertIsNotNone(data)
        self.assertEqual([model for model, _ in self.calls], [config.GEMINI_MODEL])
        self.assertEqual(notice, "")
        self.assertEqual(report["model"], config.GEMINI_MODEL)
        self.assertEqual(report["requested_model"], "gemini-2.5-flash")

    def test_the_thinking_control_follows_the_model_being_called(self):
        self._serve(lambda model: self._reply(model))
        engine.ask_json("s", "u", self.SETTINGS, timeout=1)
        config_block = self.calls[0][1]["generationConfig"]
        self.assertEqual(config_block["thinkingConfig"], {"thinkingLevel": "low"})

    def test_an_unknown_model_falls_back_and_the_pack_says_so(self):
        # gemini-3.5-flash is a live-shaped id that no longer resolves
        settings = dict(self.SETTINGS, ai_model="gemini-3.5-flash")

        def handler(model):
            return self.DEAD if model == "gemini-3.5-flash" else self._reply(model)

        self._serve(handler)
        pack = {"titles": ["a", "b"], "hashtags": ["#shorts"], "description": "d"}
        polished, provider, notice = engine.polish_pack(pack, "ctx", settings, 1)
        self.assertEqual([model for model, _ in self.calls],
                         ["gemini-3.5-flash", config.GEMINI_MODEL])
        self.assertEqual(provider, "gemini")
        self.assertEqual(notice, "")
        self.assertIn(f"gemini:{config.GEMINI_MODEL}", polished["polished_by"])
        self.assertIn("settings said gemini-3.5-flash", polished["polished_by"])

    def test_a_refused_key_is_not_tried_again_under_a_different_model(self):
        self._serve(lambda model: self.REFUSED_KEY)
        data, provider, notice = engine.ask_json("s", "u", self.SETTINGS, timeout=1)
        self.assertIsNone(data)
        self.assertEqual(provider, "offline")
        self.assertEqual(len(self.calls), 1, "a bad key must not loop over models")
        self.assertIn("401", notice)
        self.assertIn("refused the API key", notice)

    def test_the_notice_is_a_sentence_with_the_status_not_a_json_dump(self):
        self._serve(lambda model: self.DEAD)
        _, _, notice = engine.ask_json("s", "u", self.SETTINGS, timeout=1)
        self.assertIn("404", notice, "the number is what people search for")
        self.assertNotIn('"error"', notice)
        self.assertNotIn("{", notice)
        self.assertIn("does not serve", notice)
        # and the render path wraps it into its own readable line
        pack = {"titles": ["a"], "hashtags": ["#shorts"], "description": "d"}
        kept, name, note = engine.refine_pack(pack, "ctx", self.SETTINGS, timeout=1)
        self.assertEqual(kept, pack, "the offline pack must be kept")
        self.assertIn("offline pack used", note)
        self.assertIn("404", note)

    def test_a_quota_answer_reads_as_a_quota(self):
        self._serve(lambda model: {
            "__error__": 'HTTP 429 { "error": { "message": "Quota exceeded for '
                         'quota metric" } }',
            "__status__": 429,
        })
        _, _, notice = engine.ask_json("s", "u", self.SETTINGS, timeout=1)
        self.assertIn("free-tier limit", notice)
        self.assertIn("429", notice)


# --------------------------------------------------------------------------
# 5 — YouTube 429 handling: cookies, player client, and an honoured cooldown
# --------------------------------------------------------------------------
class YoutubeRateLimitTests(unittest.TestCase):
    """A 429 on subtitle downloads must not hammer YouTube on an instant Retry.

    YouTube's block outlives a single job; the old code failed after ~60 s and
    a user who pressed Retry a minute later just re-triggered it. These tests
    cover the two levers that actually get past the block (a logged-in
    ``cookies.txt`` and a ``player_client`` override) and the cooldown note
    that makes the "wait 10-15 minutes" message honest.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self._cookies = config.COOKIES_FILE
        self._client = config.YTDLP_PLAYER_CLIENT
        self._state = config.RATE_LIMIT_STATE
        self._subs = config.SUBS_DIR
        self.addCleanup(setattr, config, "COOKIES_FILE", self._cookies)
        self.addCleanup(setattr, config, "YTDLP_PLAYER_CLIENT", self._client)
        self.addCleanup(setattr, config, "RATE_LIMIT_STATE", self._state)
        self.addCleanup(setattr, config, "SUBS_DIR", self._subs)

    def test_no_cookies_and_no_client_adds_nothing(self):
        config.COOKIES_FILE = self.dir / "missing.txt"
        config.YTDLP_PLAYER_CLIENT = ""
        self.assertEqual(youtube._youtube_extractor_args(), [])

    def test_a_cookies_file_is_passed_through(self):
        cookies = self.dir / "cookies.txt"
        cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
        config.COOKIES_FILE = cookies
        config.YTDLP_PLAYER_CLIENT = ""
        self.assertEqual(youtube._youtube_extractor_args(),
                         ["--cookies", str(cookies)])

    def test_a_player_client_override_is_passed_through(self):
        config.COOKIES_FILE = self.dir / "missing.txt"
        config.YTDLP_PLAYER_CLIENT = "tv,web_safari"
        self.assertEqual(youtube._youtube_extractor_args(),
                         ["--extractor-args",
                          "youtube:player_client=tv,web_safari"])

    def test_the_cooldown_note_round_trips(self):
        config.RATE_LIMIT_STATE = self.dir / ".rate-limit.json"
        self.assertIsNone(youtube._rate_limit_remaining())
        youtube._mark_rate_limited(30)
        left = youtube._rate_limit_remaining()
        self.assertIsNotNone(left)
        self.assertGreater(left, 0)
        self.assertLessEqual(left, 30)
        # an expired note reads as clear, never a negative wait
        youtube._mark_rate_limited(-1)
        self.assertIsNone(youtube._rate_limit_remaining())

    def test_an_active_cooldown_short_circuits_without_a_request(self):
        config.RATE_LIMIT_STATE = self.dir / ".rate-limit.json"
        config.SUBS_DIR = self.dir / "subs"
        config.SUBS_DIR.mkdir(parents=True, exist_ok=True)
        youtube._mark_rate_limited(60)
        calls = []
        original = youtube.run_ytdlp
        youtube.run_ytdlp = lambda *a, **k: calls.append(a)
        self.addCleanup(setattr, youtube, "run_ytdlp", original)
        with self.assertRaises(youtube.TranscriptUnavailable) as caught:
            youtube.get_transcript("vid1", "https://youtube.com/watch?v=vid1")
        self.assertIn("still rate-limiting", str(caught.exception))
        self.assertIn("minute", str(caught.exception))
        self.assertEqual(calls, [], "a blocked retry must not touch YouTube")

    def test_a_fresh_block_is_remembered_for_the_next_retry(self):
        config.RATE_LIMIT_STATE = self.dir / ".rate-limit.json"
        config.SUBS_DIR = self.dir / "subs"
        config.SUBS_DIR.mkdir(parents=True, exist_ok=True)
        original = youtube.run_ytdlp
        youtube.run_ytdlp = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("yt-dlp failed: HTTP Error 429: Too Many Requests"))
        self.addCleanup(setattr, youtube, "run_ytdlp", original)
        # the pacer and the 30/60 s backoffs would make the test sleep ~90 s
        self.addCleanup(setattr, youtube, "_pace", youtube._pace)
        youtube._pace = lambda: None
        self.addCleanup(setattr, youtube, "_RATE_LIMIT_BACKOFF",
                        youtube._RATE_LIMIT_BACKOFF)
        youtube._RATE_LIMIT_BACKOFF = 0.0
        with self.assertRaises(youtube.TranscriptUnavailable):
            youtube.get_transcript("vid1", "https://youtube.com/watch?v=vid1")
        self.assertIsNotNone(youtube._rate_limit_remaining(),
                             "a 429 block must be remembered for the next run")


if __name__ == "__main__":
    unittest.main()
