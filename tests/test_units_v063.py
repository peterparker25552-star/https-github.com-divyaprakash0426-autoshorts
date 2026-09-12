"""v0.6.3 unit tests — the "spectrum" ident.

The brief for this release: a glowing Q on black that expands outward into a
colourful spectrum of vertical light beams, with a ta-dum you can actually
hear. v6.2 shipped the first attempt and the sound never came, so these tests
are mostly about the two things that are easy to get wrong:

1. **the audio has to survive a browser that blocks autoplay.** A tap that
   exists only to satisfy the autoplay policy must start the score, not cancel
   it, and the timeline has to wait for it so the hit lands on the burst.
2. **the animation must stay cheap.** Everything per-frame is a canvas paint
   or a transform; no animated ``filter``/``backdrop-filter``, because that is
   what jitters on a phone.

Most assertions read the shipped files (they are the product), and when node
is available the real module is executed head-first against a stubbed DOM and
WebAudio by ``tests/ident_harness.js`` — which is what actually proves the
sync between the beams and the ta-dum.

Everything here is file text or node: no ffmpeg, no network.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from autoshorts import config

WEB = Path(config.BASE_DIR) / "web"
TESTS = Path(config.BASE_DIR) / "tests"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


INTRO_JS = read(WEB / "intro.js")
APP_JS = read(WEB / "app.js")
INDEX_HTML = read(WEB / "index.html")
STYLE_CSS = read(WEB / "style.css")
SW_JS = read(WEB / "sw.js")


def timeline() -> dict[str, float]:
    """The T = {...} table from intro.js, parsed rather than duplicated."""
    block = INTRO_JS[INTRO_JS.index("const T = {"):]
    block = block[: block.index("};")]
    values: dict[str, float] = {}
    for name, value in re.findall(r"(\w+):\s*([-\d.]+),", block):
        values[name] = float(value)
    return values


class VersionTests(unittest.TestCase):
    def test_version_bumped_to_063(self):
        # 0.6.4 owns the file now; this suite defends the floor it shipped with
        self.assertGreaterEqual(config.APP_VERSION, "0.6.3")

    def test_package_and_config_agree(self):
        import autoshorts

        self.assertEqual(autoshorts.__version__, config.APP_VERSION)
        # the release notes for this suite's version must survive later bumps
        self.assertIn("v0.6.3", autoshorts.__doc__ or "")

    def test_service_worker_cache_moved_so_the_old_intro_cannot_persist(self):
        """A PWA that kept the v6.2 shell would keep the silent intro too."""
        self.assertRegex(SW_JS, r'const SHELL = "qyro-v0\.6\.[3-9]\S*-shell"')
        self.assertNotIn("0.6.2", SW_JS)
        self.assertIn('"/static/intro.js"', SW_JS)

    def test_a_seen_session_hides_the_stage_without_playing(self):
        """v0.6.8 moved this into bootIdent() and taught it the difference
        between a launch and a reload; the seen-away half is unchanged."""
        boot = INTRO_JS[INTRO_JS.index("function bootIdent(options) {"):]
        boot = boot[: boot.index("\n  }")]
        self.assertIn('overlay.classList.add("dismissed", "live")', boot,
                      "returning visitors must never wait for the fail-safe fade")
        self.assertIn("if (!forced && !launched &&", boot,
                      "…and a genuine open of the app must still be greeted")

    def test_ident_script_loads_before_the_app(self):
        html = INDEX_HTML
        self.assertIn('<script src="/static/intro.js"></script>', html)
        self.assertLess(
            html.index('src="/static/intro.js"'),
            html.index('src="/static/app.js"'),
            "intro.js must be parsed before app.js calls QyroIdent.boot()",
        )


class IdentMarkupTests(unittest.TestCase):
    def test_stage_mark_q_and_hints_exist(self):
        for needle in (
            'class="intro-stage"',          # canvas: the beams
            'class="intro-q-mark"',         # the glowing Q
            'class="intro-q-ring"',
            'class="intro-q-tail"',
            'class="intro-lockup"',
            'class="intro-wordmark"',
            'class="intro-skip"',
            'class="intro-hint"',
        ):
            self.assertIn(needle, INDEX_HTML, needle)

    def test_the_q_is_drawn_with_the_spectrum_ramp(self):
        """The mark carries the same ramp the beams expand into."""
        gradient = INDEX_HTML[INDEX_HTML.index('id="qspec"'):]
        gradient = gradient[: gradient.index("</linearGradient>")]
        stops = re.findall(r'stop-color="(#\w+)"', gradient)
        self.assertGreaterEqual(len(stops), 5)
        for colour in ("#FF3D5A", "#FF9A2E", "#FFE066", "#4ADE80", "#22D3EE", "#8B5CF6"):
            self.assertIn(colour, stops, f"{colour} missing from the spectrum ramp")

    def test_traced_strokes_use_path_length(self):
        """intro.js animates stroke-dashoffset 1 -> 0, which needs pathLength."""
        self.assertGreaterEqual(INDEX_HTML.count('pathLength="1"'), 4)
        self.assertIn(".intro-q-ring {\n  stroke-dasharray: 1;", STYLE_CSS)

    def test_the_overlay_is_not_marked_aria_hidden(self):
        """It holds a real Skip button, so it must stay reachable."""
        overlay = INDEX_HTML[INDEX_HTML.index('id="introOverlay"'):]
        overlay = overlay[: overlay.index(">")]
        self.assertNotIn("aria-hidden", overlay)
        self.assertIn('role="dialog"', overlay)

    def test_ribbon_and_petal_intro_is_fully_gone(self):
        """The replaced v6.2 markup must not linger in any layer."""
        for needle in ("intro-ribbon", "intro-petal", "intro-petal-layer",
                       "introRibbonGradient", "intro-mark", "intro-halo",
                       "intro-logo-wrap", "intro-ambient"):
            for name, text in (("index.html", INDEX_HTML), ("style.css", STYLE_CSS),
                               ("app.js", APP_JS)):
                self.assertNotIn(needle, text, f"{needle} still in {name}")


class IdentCssTests(unittest.TestCase):
    def css_block(self) -> str:
        return STYLE_CSS[STYLE_CSS.index("v6.3 \"spectrum\" ident"):]

    def test_stage_is_a_full_screen_layer_above_the_app(self):
        css = self.css_block()
        block = css[css.index(".intro-overlay {"):]
        block = block[: block.index("}")]
        for prop in ("position: fixed", "inset: 0", "z-index: 120", "contain: strict",
                     "overflow: hidden"):
            self.assertIn(prop, block, prop)
        self.assertGreater(120, 60, "the ident must cover the modal root")

    def test_the_Q_sits_where_the_canvas_paints_its_glow(self):
        """CSS 30vmin and intro.js min(w,h)*0.30 must be the same number."""
        css = self.css_block()
        self.assertIn("width: max(110px, min(220px, 30vmin))", css)
        self.assertIn("Math.max(110, Math.min(220, Math.min(w, h) * 0.3))", INTRO_JS)
        # ...and the vertical centre both of them assume
        self.assertIn("padding-bottom: 20vh", css)
        self.assertIn("h * (s.lockup ? 0.4 : 0.45)", INTRO_JS)

    def test_fail_safe_clears_the_stage_if_intro_js_never_runs(self):
        """A stale offline cache must not leave a black sheet over the app."""
        css = self.css_block()
        self.assertIn(".intro-overlay:not(.live)", css)
        self.assertIn("introFailSafe", css)
        self.assertIn('overlay.classList.add("live")', INTRO_JS)
        # and it must never pre-empt a normal run
        match = re.search(r"introFailSafe\s+[\d.]+s\s+\w+[\w-]*\s+([\d.]+)s", css)
        self.assertIsNotNone(match, "no delay on the fail-safe animation")
        self.assertGreater(float(match.group(1)), timeline()["end"],
                           "the fail-safe must not pre-empt a normal ident run")

    def test_no_animated_blur_in_the_ident(self):
        """Per-frame filters are the Termux jitter; only static drops are ok."""
        css = self.css_block()
        self.assertNotIn("backdrop-filter", css)
        for keyframes in re.findall(r"@keyframes (\w+)[^{]*\{(.*?)\n\}", css, re.S):
            self.assertNotIn("blur", keyframes[1],
                             f"{keyframes[0]} animates a blur")

    def test_reduced_motion_path_is_declared(self):
        css = self.css_block()
        self.assertIn("@media (prefers-reduced-motion: reduce)", css)
        self.assertIn(".intro-grain { animation: none; }", css)

    def test_mobile_breakpoint_exists(self):
        self.assertIn("@media (max-width: 520px)", self.css_block())


class IdentModuleTests(unittest.TestCase):
    def test_module_is_self_contained_and_global(self):
        self.assertTrue(INTRO_JS.lstrip().startswith("/*"))
        self.assertIn("(function () {", INTRO_JS)
        self.assertIn("window.QyroIdent", INTRO_JS)
        for api in ("play", "replay", "boot", "skip", "soundEnabled", "setSoundEnabled"):
            self.assertRegex(INTRO_JS, rf"\b{api}\b")

    def test_no_shipped_audio_or_video_asset(self):
        """The sound is synthesised, so the PWA still has nothing to download."""
        self.assertNotIn("new Audio(", INTRO_JS)
        self.assertFalse(list(WEB.glob("*.mp3")) or list(WEB.glob("*.wav"))
                         or list(WEB.glob("*.ogg")))

    def test_the_score_contains_a_real_ta_dum(self):
        for cue in ("TA — the sharp one", "DUM — the big one", "Riser", "Charge",
                    "Resolve", "Goodbye"):
            self.assertIn(cue, INTRO_JS, cue)
        # the two hits are a third of a second apart, the way the real thing is
        t = timeline()
        self.assertLess(t["ta"], t["burst"])
        self.assertLess(t["burst"], t["dum"])
        self.assertGreater(0.5, t["dum"] - t["ta"])

    def test_sub_and_reverb_exist_so_it_does_not_sound_like_a_beep(self):
        self.assertIn("createConvolver", INTRO_JS)
        self.assertIn("makeImpulse", INTRO_JS)
        self.assertIn("createDynamicsCompressor", INTRO_JS)
        self.assertIn('"sine", from: 41, to: 36', INTRO_JS)   # the sub tail

    def test_beams_are_a_centre_outwards_spectrum(self):
        self.assertIn("delay: Math.abs(frac - 0.5) * 2,", INTRO_JS)   # centre first
        self.assertIn("const hueAt = (frac) => (345 + 300 * frac) % 360;", INTRO_JS)
        self.assertIn('g.globalCompositeOperation = "lighter"', INTRO_JS)
        self.assertIn("sound.levels(s.beams.length, s.levels)", INTRO_JS)

    def test_pixel_ratio_is_clamped_and_the_beat_count_bounded(self):
        self.assertIn("clamp(window.devicePixelRatio || 1, 1, 2)", INTRO_JS)
        self.assertIn("clamp(Math.round(width / 24)", INTRO_JS)

    def test_timeline_fits_the_fail_safe(self):
        t = timeline()
        self.assertLess(t["ring"], t["charge"])
        self.assertLess(t["charge"], t["holdGate"])
        self.assertLess(t["holdGate"], t["ta"])
        self.assertLess(t["word"], t["fade"])
        self.assertLessEqual(t["end"], 6.0)
        self.assertLessEqual(t["holdMax"], 1.0,
                             "the ident must never hold the app for long")

    def test_the_clock_freezes_instead_of_running_ahead_of_the_beat(self):
        self.assertIn("if (!s.held) s.clock += dt;", INTRO_JS)

    def test_app_wiring_is_thin_and_gone_from_app_js(self):
        """The old hand-rolled 6.2 synth must not survive alongside it."""
        for dead in ("introAudioContext", "scheduleIntroSound", "introTone",
                     "introWhoosh", "fadeIntroSound", "introAudioCancelled"):
            self.assertNotIn(dead, APP_JS, f"{dead} should live only in intro.js")
        self.assertIn("window.QyroIdent.boot(", APP_JS)
        self.assertIn('$("#identBtn").addEventListener("click", replayIntro)', APP_JS)


class AutoplayUnlockTests(unittest.TestCase):
    """The actual bug report: 'no sound coming in this one version'."""

    def test_first_gesture_unlocks_rather_than_skips(self):
        js = INTRO_JS
        pointer = js[js.index("function onPointer()"):]
        pointer = pointer[: pointer.index("\n  }")]
        self.assertIn("unlock();", pointer)
        self.assertIn("// never trade the ta-dum for a skip", js)
        self.assertLess(pointer.index("unlock()"), pointer.index("finish(true)"),
                        "a tap must try to unlock audio before it may dismiss")

    def test_listeners_are_not_once_so_a_second_chance_exists(self):
        self.assertIn('document.addEventListener("pointerdown", onGesture, { passive: true })',
                      INTRO_JS)
        self.assertIn('document.addEventListener("touchstart", onGesture, { passive: true })',
                      INTRO_JS)
        self.assertNotIn("{ once: true }", INTRO_JS)

    def test_resume_is_attempted_and_its_failure_tolerated(self):
        self.assertIn("ctx.resume()", INTRO_JS)
        self.assertIn("catch(() => false)", INTRO_JS)

    def test_a_deliberate_mute_never_holds_the_timeline(self):
        gate = INTRO_JS[INTRO_JS.index("} else if (!s.gated"):]
        gate = gate[: gate.index("{")]
        self.assertIn("!sound.muted()", gate)

    def test_a_refused_unlock_cannot_freeze_the_app(self):
        self.assertIn("s.wall - s.heldAt >= T.holdMax", INTRO_JS)
        self.assertIn("s.gated = true;", INTRO_JS)

    def test_backgrounding_the_tab_does_not_leave_a_half_played_hit(self):
        self.assertIn('document.addEventListener("visibilitychange"', INTRO_JS)
        self.assertIn("sound.halt()", INTRO_JS)


class HarnessTests(unittest.TestCase):
    """Run the module for real, with a fake DOM + fake WebAudio."""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")

    def run_harness(self) -> dict:
        proc = subprocess.run(
            [self.node, str(TESTS / "ident_harness.js")],
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(proc.returncode, 0, f"harness failed:\n{proc.stderr[-2500:]}")
        payload, _ = json.JSONDecoder().raw_decode(proc.stdout)
        return payload

    @unittest.skipIf(shutil.which("node") is None, "node not installed")
    def test_every_scenario_ends_cleanly(self):
        results = self.run_harness()
        for name, data in results.items():
            self.assertIsNone(data["error"], f"{name} threw {data['error']}")
            if name == "again":
                continue
            self.assertTrue(data["classes"]["dismissed"], f"{name} never finished")

    @unittest.skipIf(shutil.which("node") is None, "node not installed")
    def test_scheduled_audio_matches_the_visual_beat(self):
        results = self.run_harness()
        t = timeline()
        desktop = results["desktop"]
        self.assertGreater(desktop["cues"], 30, "the score was not scheduled")
        self.assertGreaterEqual(desktop["tones"], 25)
        self.assertGreaterEqual(desktop["noiseLayers"], 4)
        # v0.6.5 shortened the impulse to 1.7 s: the tail past that was
        # inaudible under the wordmark chord, but its convolution cost was the
        # main crackle source on phones. Still a real hall, not a sliver.
        self.assertGreaterEqual(desktop["impulseSeconds"], 1.2)
        # TA/DUM are read back off the stub as absolute ctx times
        self.assertAlmostEqual(desktop["dumAt"] - desktop["taAt"], t["dum"] - t["ta"], places=2)
        self.assertGreater(desktop["additivePasses"], 50, "beams not additively blended")
        self.assertGreater(desktop["drawRects"], 1000, "beams were not painted")
        self.assertGreater(desktop["fftReads"], 0, "beams are not analyser-driven")

    @unittest.skipIf(shutil.which("node") is None, "node not installed")
    def test_blocked_autoplay_is_silent_but_never_stuck(self):
        results = self.run_harness()
        blocked = results["blocked"]
        self.assertEqual(blocked["cues"], 0)
        self.assertTrue(blocked["classes"]["dismissed"])
        self.assertGreater(blocked["endAfterGate"], 0.4)
        self.assertLess(blocked["endAfterGate"], 1.4,
                        f"held too long ({blocked['endAfterGate']}s)")

    @unittest.skipIf(shutil.which("node") is None, "node not installed")
    def test_the_unlock_tap_starts_the_score_and_keeps_the_intro(self):
        results = self.run_harness()
        unlock = results["unlock"]
        self.assertGreater(unlock["cues"], 30, "the tap did not start the ident's audio")
        self.assertTrue(unlock["classes"]["dismissed"], "the tap must not skip")
        # a short freeze is expected: the frame waits for the gesture, then resumes
        self.assertGreater(unlock["endAfterGate"], 0.05)
        self.assertAlmostEqual(unlock["dumAt"] - unlock["taAt"],
                               results["desktop"]["dumAt"] - results["desktop"]["taAt"],
                               places=2, msg="ta-dum shape changed after resuming")

    @unittest.skipIf(shutil.which("node") is None, "node not installed")
    def test_muted_pref_skips_audio_without_skipping_the_visuals(self):
        results = self.run_harness()
        muted = results["muted"]
        self.assertEqual(muted["contexts"], 0, "no AudioContext may be built when muted")
        self.assertEqual(muted["cues"], 0)
        self.assertTrue(muted["classes"]["dismissed"])
        self.assertLess(muted["endAfterGate"], 0.1, "a mute must not hold the timeline")

    @unittest.skipIf(shutil.which("node") is None, "node not installed")
    def test_seen_session_shows_nothing(self):
        results = self.run_harness()
        again = results["again"]
        # armed (so the CSS fail-safe stands down) but never played
        self.assertTrue(again["classes"]["live"])
        self.assertTrue(again["classes"]["dismissed"])
        self.assertEqual(again["contexts"], 0)
        self.assertEqual(again["cues"], 0)
        # ...and a returning visit must not sit there covering the app
        self.assertTrue(again["classes"]["dismissed"],
                        "a skipped ident must hide the stage immediately")

    @unittest.skipIf(shutil.which("node") is None, "node not installed")
    def test_reduced_motion_and_hi_dpi_still_complete(self):
        results = self.run_harness()
        for name in ("reduced", "hiDpi", "skip", "esc", "early", "unlockLate"):
            self.assertTrue(results[name]["classes"]["dismissed"], f"{name} hung")
        self.assertGreater(results["early"]["cues"], 30,
                           "a tap at 0.4s must unlock, not dismiss")


if __name__ == "__main__":
    unittest.main()
