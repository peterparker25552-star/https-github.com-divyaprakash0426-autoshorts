"""Qyro v0.5.0 unit tests — the pure, offline parts of the new tools.

Nothing here touches the network or a browser: logo-graph maths, beat
peak-picking on synthetic curves, audio filter graphs, caption-brand bundles
and their validation, the free AI engine's fallback chain, the 1440p size map
and the media prober. Every function under test is deterministic.
"""
from __future__ import annotations

import json
import math
import subprocess
import unittest
from pathlib import Path

from autoshorts import (
    audioswap, beats, config, engine, ffmpeg, logofx, maintenance, titles,
)
from autoshorts.transcripts import Segment

from . import util


# --------------------------------------------------------------------------
# T4 — logo remover
# --------------------------------------------------------------------------
class LogoSpecTests(unittest.TestCase):
    def test_clean_spec_normalises_corner_preset(self):
        spec = logofx.clean_spec({"preset": "TopRight", "size": "m", "feather": 9})
        self.assertEqual(spec["preset"], "topright")
        self.assertEqual(spec["size"], "M")
        self.assertEqual(spec["feather"], 9)
        self.assertNotIn("x", spec)               # corners derive their own box

    def test_clean_spec_rejects_bad_values(self):
        for bad in (
            {"preset": "middle"},
            {"preset": "topleft", "size": "XL"},
            {"preset": "topleft", "feather": 99},
            {"preset": "topleft", "feather": "soft"},
            {"preset": "topleft", "feather": True},
            "topleft",
            [1, 2],
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    logofx.clean_spec(bad)

    def test_none_and_empty_mean_off(self):
        self.assertIsNone(logofx.clean_spec(None))
        self.assertIsNone(logofx.clean_spec({}))
        self.assertIsNone(logofx.clean_spec({"enabled": False}))

    def test_custom_requires_frame_fractions(self):
        spec = logofx.clean_spec(
            {"preset": "custom", "x": 0.1, "y": 0.2, "w": 0.5, "h": 0.15}
        )
        self.assertEqual(spec["preset"], "custom")
        self.assertEqual(spec["w"], 0.5)
        for bad in (
            {"preset": "custom", "x": 1.4, "y": 0, "w": 0.2, "h": 0.1},
            {"preset": "custom", "x": 0, "y": 0, "w": 0.001, "h": 0.1},
            {"preset": "custom", "x": "left", "y": 0, "w": 0.2, "h": 0.1},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    logofx.clean_spec(bad)

    def test_rect_for_corners_is_inside_the_frame(self):
        w, h = 1080, 1920
        for preset in config.LOGO_PRESETS:
            if preset == "custom":
                continue
            spec = logofx.clean_spec({"preset": preset, "size": "L", "feather": 0})
            x, y, bw, bh = logofx.rect_for(spec, w, h)
            self.assertGreaterEqual(x, 0, preset)
            self.assertGreaterEqual(y, 0, preset)
            self.assertLessEqual(x + bw, w, preset)
            self.assertLessEqual(y + bh, h, preset)
            if "left" in preset:
                self.assertLess(x, w * 0.2, preset)
            if "right" in preset:
                self.assertGreater(x + bw, w * 0.8, preset)
            if preset.startswith("top"):
                self.assertLess(y, h * 0.2, preset)
            if preset.startswith("bottom"):
                self.assertGreater(y + bh, h * 0.8, preset)

    def test_rect_scales_with_frame_size_not_pixels(self):
        small = logofx.rect_for({"preset": "topleft", "size": "M", "feather": 0},
                               720, 1280)
        big = logofx.rect_for({"preset": "topleft", "size": "M", "feather": 0},
                              1440, 2560)
        self.assertAlmostEqual(small[2] * 2, big[2], delta=2)
        self.assertAlmostEqual(small[0] * 2, big[0], delta=2)

    def test_custom_rect_uses_fractions(self):
        x, y, w, h = logofx.rect_for(
            {"preset": "custom", "x": 0.25, "y": 0.5, "w": 0.4, "h": 0.1},
            1000, 2000,
        )
        self.assertEqual((x, y, w, h), (250, 1000, 400, 200))

    def test_feather_grows_and_clamps(self):
        x, y, w, h = logofx.apply_feather((10, 10, 100, 40), 1000, 1000, 8)
        self.assertEqual((x, y, w, h), (2, 2, 116, 56))
        # at the edge it clamps instead of going negative
        x2, y2, w2, h2 = logofx.apply_feather((1, 1, 20, 10), 100, 100, 6)
        self.assertEqual((x2, y2), (0, 0))
        self.assertLessEqual(x2 + w2, 100)

    def test_delogo_only_when_strictly_inside(self):
        self.assertTrue(logofx.delogo_usable((10, 10, 100, 40), 1080, 1920))
        self.assertFalse(logofx.delogo_usable((0, 10, 100, 40), 1080, 1920))
        self.assertFalse(logofx.delogo_usable((10, 10, 100, 40), 100, 90))

    def test_filter_graphs(self):
        rect = (12, 20, 160, 48)
        self.assertEqual(logofx.delogo_filter(rect), "delogo=x=12:y=20:w=160:h=48")
        # no :band= — ffmpeg 7 has no such option, and adding one fails the run
        self.assertNotIn("band", logofx.delogo_filter(rect))
        blur = logofx.blur_filter(rect, 4)
        self.assertIn("boxblur=4", blur)
        self.assertIn("overlay", blur)

    def test_logo_stage_off_and_shapes(self):
        self.assertIsNone(logofx.logo_stage(None, 1080, 1920))
        self.assertIsNone(logofx.logo_stage({"preset": "topleft"}, 0, 0))
        graph, label = logofx.logo_stage(
            logofx.clean_spec({"preset": "topleft", "size": "M"}), 1080, 1920
        )
        self.assertEqual(label, "vlogo")
        self.assertTrue(graph.startswith("[0:v]delogo="), graph)
        self.assertTrue(graph.endswith("[vlogo]"), graph)
        # a renamed input label is honoured (the concat path needs it)
        graph2, _ = logofx.logo_stage(
            logofx.clean_spec({"preset": "topleft", "size": "M"}), 1080, 1920,
            vin="vin",
        )
        self.assertTrue(graph2.startswith("[vin]"), graph2)

    def test_logo_stage_falls_back_to_blur_at_the_edge(self):
        # feather pushes the box onto x=0, where delogo refuses to work
        spec = logofx.clean_spec({"preset": "topleft", "size": "S", "feather": 20})
        graph, label = logofx.logo_stage(spec, 640, 360)
        self.assertEqual(label, "vlogo")
        self.assertIn("boxblur", graph)
        self.assertIn("overlay", graph)
        self.assertTrue(graph.startswith("[0:v]"), graph)

    def test_describe_is_ui_safe(self):
        self.assertEqual(logofx.describe(None), {"enabled": False})
        info = logofx.describe(
            logofx.clean_spec({"preset": "bottomright", "size": "M", "feather": 3}),
            1080, 1920,
        )
        self.assertTrue(info["enabled"])
        self.assertEqual(info["preset"], "bottomright")
        self.assertIn(info["method"], ("delogo", "boxblur"))
        self.assertEqual(set(info["rect"]), {"x", "y", "w", "h"})
        box = info["box"]
        self.assertTrue(0.0 <= box["x"] <= 1.0 and 0.0 < box["w"] <= 1.0)


# --------------------------------------------------------------------------
# T3 — beats
# --------------------------------------------------------------------------
class BeatTests(unittest.TestCase):
    def test_accented_curve_finds_the_accents(self):
        grid = beats.GRID
        times = [i * grid for i in range(1000)]            # 100 s
        # a loudness bump every 2.0 s on a steady bed
        curve = [
            0.30 + (0.45 if abs(t - round(t / 2.0) * 2.0) < 0.12 else 0.0)
            for t in times
        ]
        found = beats.beats_from_curve(curve)
        self.assertGreaterEqual(len(found), 20, "one beat per accent, roughly")
        for beat in found[:15]:
            # placement is quantised to the 0.1 s loudness grid, and the
            # 3-sample smoothing may pull it one step early: 0.35 s tolerance
            self.assertLessEqual(
                abs(beat - round(beat / 2.0) * 2.0), 0.35,
                f"beat {beat} is off the accent grid",
            )
        # spacing is respected: never closer than BEAT_MIN_GAP
        for a, b in zip(found, found[1:]):
            self.assertGreaterEqual(b - a, config.BEAT_MIN_GAP - 1e-6)

    def test_flat_curve_has_no_beats(self):
        self.assertEqual(beats.beats_from_curve([0.5] * 600), [])
        self.assertEqual(beats.beats_from_curve([]), [])
        self.assertEqual(beats.find_peaks([1.0] * 50), [])

    def test_top_peaks_ranks_and_spaces(self):
        values = [0.0, 0.9, 0.1, 0.0, 0.7, 0.1, 0.0, 0.95, 0.2]
        picked = beats.top_peaks(values, min_gap=2, neighbours=1)
        self.assertIn(1, picked)
        self.assertIn(7, picked)
        self.assertEqual(picked, sorted(picked))
        # a plateau of identical values is not a beat
        self.assertEqual(beats.top_peaks([0.4] * 30), [])

    def test_find_peaks_respects_min_gap(self):
        values = [0.0, 0.8, 0.75, 0.85, 0.0, 0.9, 0.0]
        picked = beats.find_peaks(values, min_gap=4, neighbours=1, k=0.1)
        self.assertLessEqual(len(picked), 2)
        for a, b in zip(picked, picked[1:]):
            self.assertGreaterEqual(b - a, 4)

    def test_snap_to_beats(self):
        markers = [10.0, 20.0, 30.0, 40.0]
        start, end, moved = beats.snap_to_beats(9.4, 40.6, markers, window=1.2)
        self.assertEqual((start, end), (10.0, 40.0))
        self.assertTrue(moved)
        # outside the window nothing moves
        start, end, moved = beats.snap_to_beats(8.0, 42.0, markers, window=1.2)
        self.assertEqual((start, end), (8.0, 42.0))
        self.assertFalse(moved)
        # no markers, or an empty list, is a no-op
        self.assertEqual(beats.snap_to_beats(1.0, 2.0, []), (1.0, 2.0, False))

    def test_snap_refuses_to_shorten_a_cut_below_five_seconds(self):
        markers = [11.0, 20.0]
        start, end, moved = beats.snap_to_beats(11.4, 12.0, markers, window=1.2)
        self.assertEqual((start, end), (11.4, 12.0))
        self.assertFalse(moved)

    def test_resample_bucketing(self):
        curve = beats.resample_curve([(0.0, -20.0), (0.05, -10.0), (0.25, -30.0)])
        self.assertEqual(len(curve), 3)
        self.assertAlmostEqual(max(curve), 1.0)
        self.assertAlmostEqual(min(curve), 0.0)
        self.assertEqual(beats.resample_curve([]), [])


# --------------------------------------------------------------------------
# T3 — audio swap graphs
# --------------------------------------------------------------------------
class AudioGraphTests(unittest.TestCase):
    def test_replace_graph_never_touches_the_source(self):
        graph = audioswap.replace_graph(30.0)
        self.assertIn("[1:a]atrim=0:30.000", graph)
        self.assertIn("asetpts=PTS-STARTPTS", graph)
        self.assertIn("loudnorm", graph)
        self.assertIn("afade=t=in", graph)
        self.assertIn("afade=t=out", graph)
        self.assertIn("[aout]", graph)
        self.assertNotIn("[0:a]", graph)
        self.assertNotIn("amix", graph)

    def test_duck_graph_mixes_quiet_source_with_the_track(self):
        graph = audioswap.duck_graph(20.0, src_label="0:a", gain=0.2)
        self.assertIn("[0:a]volume=0.2", graph)
        self.assertIn("[1:a]atrim=0:20.000", graph)
        self.assertIn("amix=inputs=2", graph)
        self.assertIn("normalize=0", graph)
        self.assertIn("[aout]", graph)

    def test_duck_graph_applies_atempo_only_when_needed(self):
        self.assertNotIn("atempo", audioswap.duck_graph(10.0, speed=1.0))
        self.assertIn("atempo=1.25", audioswap.duck_graph(10.0, speed=1.25))

    def test_fades_are_capped_and_omitted_when_pointless(self):
        graph = audioswap.fade_chain(0.3, 0.35)      # capped to a third
        self.assertIn("afade=t=in:st=0:d=0.100", graph)
        self.assertNotIn("d=0.350", graph)
        self.assertEqual(audioswap.fade_chain(10, 0), "")
        self.assertEqual(audioswap.fade_chain("x", "y"), "")

    def test_mix_plan_modes(self):
        plan = audioswap.mix_plan("replace", 12.0)
        self.assertEqual(plan["mode"], "replace")
        self.assertFalse(plan["use_source_audio"])
        self.assertEqual(plan["label"], "[aout]")
        plan = audioswap.mix_plan("duck", 12.0, source_label="0:a")
        self.assertEqual(plan["mode"], "duck")
        self.assertTrue(plan["use_source_audio"])
        self.assertIn("amix", plan["graph"])
        # unknown mode falls back to the default, never raises
        self.assertEqual(audioswap.mix_plan("both", 12.0)["mode"],
                         config.DEFAULT_AUDIO_MIX)
        # duck with no source audio degrades to replace
        plan = audioswap.mix_plan("duck", 12.0, source_label=None)
        self.assertEqual(plan["mode"], "replace")

    def test_graphs_are_valid_filter_segments(self):
        # each segment must be "[label]chain[label]" — no stray semicolons
        for graph in (audioswap.replace_graph(9.0), audioswap.duck_graph(9.0)):
            for part in graph.split(";"):
                self.assertTrue(part.strip().startswith("["), part)
                self.assertTrue(part.strip().endswith("]"), part)

    def test_save_and_delete_track_roundtrip(self):
        # a real (tiny) MP3 written by ffmpeg, so validation is exercised
        src = Path(config.DATA_DIR) / "unit-bed.mp3"
        src.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [config.FFMPEG_BIN, "-v", "error", "-y", "-f", "lavfi",
             "-i", "sine=frequency=200:duration=1", "-c:a", "libmp3lame", str(src)],
            check=True, capture_output=True,
        )
        entry = audioswap.save_track("bed.mp3", src.read_bytes())
        self.assertTrue(entry["id"])
        self.assertEqual(entry["name"], "bed.mp3")
        self.assertGreater(entry["bytes"], 1000)
        self.assertGreater(entry["duration"], 0.5)
        self.assertIn(entry, audioswap.list_tracks())
        self.assertTrue(audioswap.track_file(entry["id"]).is_file())
        self.assertTrue(audioswap.delete_track(entry["id"]))
        self.assertFalse(audioswap.delete_track(entry["id"]))
        self.assertIsNone(audioswap.track_file("nope"))

    def test_save_track_validates(self):
        with self.assertRaises(ValueError):
            audioswap.save_track("bed.mp3", b"")
        with self.assertRaises(ValueError):
            audioswap.save_track("", b"\xff\xfb" + b"\x00" * 4096)
        with self.assertRaises(ValueError):
            audioswap.save_track("song.txt", b"not audio at all " * 400)
        # a name is sanitised, never used as a path
        self.assertNotIn("/", audioswap._SAFE_NAME.sub("", "../../etc/passwd"))


# --------------------------------------------------------------------------
# T2 — caption brands
# --------------------------------------------------------------------------
class CaptionBrandTests(unittest.TestCase):
    def setUp(self):
        self.segments = [
            Segment(start=0.0, end=2.0, text="the body gives up first"),
            Segment(start=2.0, end=4.0, text="and nobody warns you"),
        ]
        self.out = Path(config.DATA_DIR) / "brand-ass"
        self.out.mkdir(parents=True, exist_ok=True)

    def _ass(self, **kwargs) -> str:
        path = self.out / f"{kwargs.pop('name', 'brand')}.ass"
        ffmpeg.make_ass(self.segments, 0.0, 4.0, path,
                        play_w=720, play_h=1280, **kwargs)
        return path.read_text(encoding="utf-8")

    def test_none_is_the_v040_bytes(self):
        plain = self._ass(name="plain")
        brandless = self._ass(name="none", brand="none")
        self.assertEqual(plain, brandless)

    def test_bundle_lookup_is_safe(self):
        self.assertIsNone(ffmpeg.brand_bundle("qyro-glow"))
        self.assertIsNone(ffmpeg.brand_bundle(None))
        self.assertIsNotNone(ffmpeg.brand_bundle("qyro-neon"))

    def test_brand_sets_style_name_size_and_colours(self):
        for brand, style_name in (("qyro-pop", "QPop"), ("qyro-minimal", "QMin"),
                                  ("qyro-neon", "QNeon")):
            with self.subTest(brand=brand):
                text = self._ass(name=brand, brand=brand)
                line = next(l for l in text.splitlines() if l.startswith("Style:"))
                fields = [f.strip() for f in line.split(":", 1)[1].split(",")]
                self.assertEqual(fields[0], style_name)          # Style name
                preset = config.CAPTION_BRAND_PRESETS[brand]
                scale = preset["font_size_scale"]
                base = self._ass(name="none", brand="none")
                base_line = next(l for l in base.splitlines() if l.startswith("Style:"))
                base_size = float(base_line.split(",")[2])
                self.assertAlmostEqual(
                    float(fields[2]) / base_size, scale, delta=0.06
                )
                # PrimaryColour is field 3, OutlineColour field 5
                self.assertEqual(fields[3], preset["primary"])
                self.assertEqual(fields[5], preset["outline_colour"])

    @staticmethod
    def _fields(text):
        line = next(l for l in text.splitlines() if l.startswith("Style:"))
        return [f.strip() for f in line.split(":", 1)[1].split(",")]

    def test_bundle_owns_box_and_place_when_unasked(self):
        # nobody asked: the Neon bundle brings its opaque box …
        neon = self._ass(name="neon", brand="qyro-neon")
        self.assertEqual(self._fields(neon)[15], "3", "BorderStyle must be box")
        # … and an explicit request wins over the bundle
        plain = self._ass(name="neon-plain", brand="qyro-neon", box=False)
        self.assertEqual(self._fields(plain)[15], "1")
        # Minimal prefers low placement (smaller MarginV than standard)
        minimal = self._ass(name="min", brand="qyro-minimal")
        standard = self._ass(name="min-std", brand="qyro-minimal", pos="standard")
        self.assertLess(int(self._fields(minimal)[21]),
                        int(self._fields(standard)[21]))

    def test_word_pop_is_burned_into_events(self):
        chunked = self._ass(name="pop", brand="qyro-pop")
        events = [l for l in chunked.splitlines() if l.startswith("Dialogue:")]
        self.assertGreater(len(events), 2, "per-word events should multiply")
        self.assertTrue(any(r"\fs" in e and r"\b1" in e for e in events),
                        "word pop needs size/bold overrides")
        preset = config.CAPTION_BRAND_PRESETS["qyro-pop"]
        self.assertIn(preset["emphasis"], chunked)
        # plain classic without a brand keeps whole chunks (no per-word rows)
        plain = self._ass(name="plain2", brand="none")
        events = [l for l in plain.splitlines() if l.startswith("Dialogue:")]
        self.assertLess(len(events), 6)
        self.assertNotIn(r"\fs", plain)

    def test_pop_style_still_pops_without_a_brand(self):
        text = self._ass(name="popstyle", caption_style="pop")
        self.assertIn(r"\fs", text)            # per-word size punch
        self.assertIn(r"\b1", text)            # and the bold flash
        self.assertIn(config._POP_EMPHASIS if hasattr(config, "_POP_EMPHASIS")
                      else "&H0000FFFF", text)

    def test_ass_header_colours_follow_the_bundle(self):
        text = self._ass(name="colours", brand="qyro-neon")
        head = text.split("[Events]")[0]
        self.assertIn("PlayResX: 720", head)
        self.assertIn("PlayResY: 1280", head)
        self.assertIn("ScaledBorderAndShadow: yes", head)


# --------------------------------------------------------------------------
# render-option validation (v0.5.0 keys)
# --------------------------------------------------------------------------
class RenderOptionTests(unittest.TestCase):
    def test_new_keys_are_reachable_and_default(self):
        for key in ("captions_brand", "logo_box", "sync_beats", "audio_track",
                    "audio_mix", "silence_noise", "silence_min"):
            self.assertIn(key, maintenance.RENDER_DEFAULTS)
        opts = maintenance.render_opts_from({})
        self.assertEqual(opts["captions_brand"], "none")
        self.assertIsNone(opts["logo_box"])
        self.assertFalse(opts["sync_beats"])
        self.assertEqual(opts["audio_mix"], config.DEFAULT_AUDIO_MIX)

    def test_1440p_quality_is_accepted_everywhere(self):
        for fmt, (w, h) in (("vertical", (1440, 2560)), ("square", (1440, 1440)),
                            ("wide", (2560, 1440))):
            self.assertEqual(config.OUTPUT_SIZES[fmt]["1440p"], (w, h))
        opts = maintenance.render_opts_from({"quality": "1440p", "format": "wide"})
        self.assertEqual(opts["quality"], "1440p")
        self.assertEqual(config.OUTPUT_SIZES["wide"]["1440p"], (2560, 1440))
        self.assertIn("1440p", config.QUALITIES)
        self.assertIn("1440p", config.RENDER_HEIGHTS)

    def test_brand_and_logo_are_normalised(self):
        opts = maintenance.render_opts_from({
            "captions_brand": "qyro-neon",
            "logo_box": {"preset": "topleft", "size": "M", "feather": 4},
        })
        self.assertEqual(opts["captions_brand"], "qyro-neon")
        self.assertEqual(opts["logo_box"]["preset"], "topleft")
        self.assertEqual(opts["logo_box"]["feather"], 4)

    def test_bad_values_are_422(self):
        for bad in (
            {"captions_brand": "qyro-glow"},
            {"quality": "8k"},
            {"logo_box": {"preset": "middle"}},
            {"logo_box": 5},
            {"audio_mix": "both"},
            {"sync_beats": "yes"},
            {"silence_min": 9},
            {"silence_noise": 3},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(maintenance.ServiceError) as ctx:
                    maintenance.render_opts_from(bad)
                self.assertEqual(ctx.exception.status, 422)

    def test_brand_box_and_position_are_defaults_not_orders(self):
        opts = maintenance.render_opts_from({"captions_brand": "qyro-neon"})
        self.assertTrue(opts["captions_box"])          # bundle forces the box
        opts = maintenance.render_opts_from(
            {"captions_brand": "qyro-neon", "captions_box": False}
        )
        self.assertFalse(opts["captions_box"])         # explicit request wins
        opts = maintenance.render_opts_from(
            {"captions_brand": "qyro-minimal", "captions_pos": "standard"}
        )
        self.assertEqual(opts["captions_pos"], "standard")

    def test_unknown_audio_track_is_a_422_not_a_silent_drop(self):
        with self.assertRaises(maintenance.ServiceError) as ctx:
            maintenance.render_opts_from({"audio_track": "does-not-exist"})
        self.assertEqual(ctx.exception.status, 422)

    def test_pipeline_drops_a_track_that_vanished_after_queueing(self):
        """The id is resolved at render time, so a deleted bed cannot fail a
        job that was queued while it still existed."""
        settings = _render_settings({
            "quality": "fast", "audio_track": "gone", "audio_mix": "duck",
        })
        self.assertIsNone(settings["audio_track"])
        self.assertIsNone(settings["audio_track_path"])
        self.assertEqual(settings["audio_mix"], config.DEFAULT_AUDIO_MIX)

    def test_silence_tuner_bounds(self):
        opts = maintenance.render_opts_from({"silence_noise": -50, "silence_min": 1.2})
        self.assertEqual(opts["silence_noise"], -50.0)
        self.assertEqual(opts["silence_min"], 1.2)
        self.assertEqual(maintenance.render_opts_from({})["silence_noise"],
                         config.DEFAULT_SILENCE_NOISE)

    def test_rerender_accepts_the_new_keys(self):
        params = maintenance.rerender_params_from(
            {"captions_brand": "qyro-pop", "logo_box": {"preset": "bottomleft"}},
            base={},
        )
        self.assertEqual(params["captions_brand"], "qyro-pop")
        self.assertEqual(params["logo_box"]["preset"], "bottomleft")
        with self.assertRaises(maintenance.ServiceError):
            maintenance.rerender_params_from({"count": 3}, base={})

    def test_brand_placement_is_a_default_not_an_order(self):
        # the bundle chooses when the request does not …
        self.assertEqual(
            maintenance.render_opts_from(
                {"captions_brand": "qyro-minimal"}
            )["captions_pos"],
            "low",
        )
        # … and never overrules an explicit pick
        opts = maintenance.render_opts_from(
            {"captions_brand": "qyro-minimal", "captions_pos": "standard"}
        )
        self.assertEqual(opts["captions_pos"], "standard")
        opts = maintenance.render_opts_from(
            {"captions_brand": "qyro-neon", "captions_box": False}
        )
        self.assertFalse(opts["captions_box"])
        # a stored base counts as "already chosen" too (re-render fidelity)
        opts = maintenance.render_opts_from(
            {"captions_brand": "qyro-minimal"}, base={"captions_pos": "standard"}
        )
        self.assertEqual(opts["captions_pos"], "standard")


# --------------------------------------------------------------------------
# T5 — free AI engine
# --------------------------------------------------------------------------
class EngineTests(unittest.TestCase):
    def test_read_defaults_to_offline(self):
        cfg = engine.read({})
        self.assertEqual(cfg["provider"], "offline")
        self.assertEqual(engine.chain(cfg), [])
        self.assertFalse(engine.available({}))

    def test_chain_follows_the_provider_choice(self):
        cfg = engine.read({"ai_provider": "groq", "groq_key": "gsk_x"})
        self.assertEqual(engine.chain(cfg), ["groq"])
        self.assertEqual(engine.key_for(cfg, "groq"), "gsk_x")
        auto = engine.read({"ai_provider": "auto", "groq_key": "g", "gemini_key": ""})
        self.assertEqual(engine.chain(auto), ["groq"])
        self.assertTrue(engine.available({"ai_provider": "auto", "groq_key": "g"}))
        self.assertFalse(engine.available({"ai_provider": "offline"}))

    def test_mask_settings_never_leaks_keys(self):
        settings = {"playlist_url": "u", "gemini_key": "AIzaSECRET",
                    "groq_key": "gsk_SECRET", "ai_key": "sk-SECRET"}
        masked = engine.mask_settings(settings)
        dumped = json.dumps(masked)
        for secret in ("AIzaSECRET", "gsk_SECRET", "sk-SECRET"):
            self.assertNotIn(secret, dumped)
        self.assertTrue(masked["gemini_key_set"])
        self.assertTrue(masked["groq_key_set"])
        self.assertTrue(masked["ai_key_set"])
        self.assertEqual(masked["playlist_url"], "u")

    def test_public_state_shape(self):
        state = engine.public_state({"ai_provider": "gemini", "gemini_key": "kk",
                                     "ai_model": "gemini-2.0-flash"})
        self.assertEqual(state["provider"], "gemini")
        self.assertEqual(state["model"], "gemini-2.0-flash")
        self.assertTrue(state["key_set"])
        self.assertTrue(state["active"])
        self.assertEqual([p["id"] for p in state["providers"]],
                         ["offline", "gemini", "groq", "custom"])
        gemini = next(p for p in state["providers"] if p["id"] == "gemini")
        self.assertTrue(gemini["has_key"])
        self.assertNotIn("gemini_key", json.dumps(state))
        off = engine.public_state({})
        self.assertEqual(off["provider"], "offline")
        self.assertFalse(off["active"])
        self.assertFalse(off["key_set"])

    def test_error_scrubber_hides_keys(self):
        messy = "HTTP 401 for key AIzaSyABCDEFGHIJKLMNOPQRSTUV"
        scrubbed = engine._scrub(messy)
        self.assertNotIn("AIzaSyABCDEFGHIJKLMNOPQRSTUV", scrubbed)
        self.assertIn("401", scrubbed)

    def test_call_without_provider_returns_notice(self):
        pack = {"titles": ["A"], "hashtags": ["#shorts"], "description": "d"}
        polished, provider, notice = engine.polish_pack(pack, "ctx", {}, timeout=1)
        self.assertIsNone(polished)
        self.assertEqual(provider, "offline")
        self.assertTrue(notice)

    def test_bad_key_falls_back_without_raising(self):
        """A configured-but-wrong key must behave like no key at all."""
        import urllib.error
        import urllib.request

        def boom(request, *args, **kwargs):
            raise urllib.error.HTTPError(
                request.full_url, 401, "Unauthorized", {}, None
            )

        original = urllib.request.urlopen
        urllib.request.urlopen = boom
        try:
            settings = {"ai_provider": "gemini", "gemini_key": "AIzaBADKEY123456",
                        "ai_model": "gemini-2.0-flash"}
            answer, provider, notice = engine.ask_json("sys", "user", settings,
                                                        timeout=1)
            self.assertIsNone(answer)
            self.assertEqual(provider, "offline")
            self.assertIn("401", notice)
            self.assertNotIn("AIzaBADKEY123456", notice)
            polished, used, notice2 = engine.polish_pack(
                {"titles": ["A"], "hashtags": [], "description": "d"},
                "ctx", settings, timeout=1,
            )
            self.assertIsNone(polished)
            self.assertEqual(used, "offline")
            self.assertTrue(notice2)
            refined, name, note = engine.refine_pack(
                {"titles": ["A"], "hashtags": ["#shorts"], "description": "d"},
                "ctx", settings,
            )
            self.assertEqual(refined["titles"], ["A"])
            self.assertEqual(name, "offline")
        finally:
            urllib.request.urlopen = original

    def test_unreachable_network_is_a_notice_not_a_crash(self):
        import urllib.request
        original = urllib.request.urlopen

        def down(*args, **kwargs):
            raise OSError("network down")

        urllib.request.urlopen = down
        try:
            answer, provider, notice = engine.ask_json(
                "s", "u", {"ai_provider": "groq", "groq_key": "gsk_x"}, timeout=1
            )
        finally:
            urllib.request.urlopen = original
        self.assertIsNone(answer)
        self.assertEqual(provider, "offline")
        self.assertTrue(notice)

    def test_junk_reply_is_rejected(self):
        settings = {"ai_provider": "groq", "groq_key": "gsk_x",
                    "ai_model": "llama-3.3-70b-versatile"}
        original = engine._post_json
        engine._post_json = lambda *a, **k: {
            "choices": [{"message": {"content": "sorry, no json here"}}]
        }
        try:
            answer, provider, notice = engine.ask_json("s", "u", settings, timeout=1)
        finally:
            engine._post_json = original
        self.assertIsNone(answer)
        self.assertEqual(provider, "offline")
        self.assertIn("JSON", notice)

    def test_good_reply_is_used(self):
        settings = {"ai_provider": "groq", "groq_key": "gsk_x"}
        reply = {"titles": ["One: the real reason coaches lie",
                            "Two things nobody says out loud"],
                 "hashtags": ["#shorts", "#coaching"]}
        original = engine._post_json
        engine._post_json = lambda *a, **k: {
            "choices": [{"message": {"content": json.dumps(reply)}}]
        }
        try:
            answer, provider, notice = engine.ask_json("s", "u", settings, timeout=1)
        finally:
            engine._post_json = original
        self.assertEqual(provider, "groq")
        self.assertEqual(notice, "")
        self.assertEqual(answer["hashtags"][0], "#shorts")

    def test_refine_pack_keeps_offline_words(self):
        pack = titles.generate_pack("Hook", "full transcript here", "Ep", "viral", 30)
        refined, name, notice = engine.refine_pack(pack, "ctx", {})
        self.assertEqual(refined, pack)
        self.assertEqual(name, "offline")

    def test_title_variations_endpoint_logic(self):
        result = engine.title_variations(
            "the body gives up before the mind does", "viral", 10, {}
        )
        self.assertEqual(len(result["titles"]), 10)
        self.assertTrue(all(t.strip() for t in result["titles"]))
        self.assertIn("#shorts", result["hashtags"][0])
        self.assertEqual(result["engine"], "offline")
        # an empty transcript still answers with usable copy
        empty = engine.title_variations("", "story", 10, {})
        self.assertEqual(len(empty["titles"]), 10)

    def test_gemini_key_is_never_in_the_url(self):
        seen = {}

        def spy(url, payload, headers, timeout):
            seen["url"] = url
            seen["headers"] = dict(headers or {})
            return None                      # stop right after the call shape

        original = engine._post_json
        engine._post_json = spy
        try:
            engine.ask_json("s", "u", {"ai_provider": "gemini",
                                       "gemini_key": "AIzaTOPSECRET",
                                       "ai_model": "m"}, timeout=1)
        finally:
            engine._post_json = original
        self.assertNotIn("AIzaTOPSECRET", seen["url"])
        lowered = {k.lower(): v for k, v in seen["headers"].items()}
        self.assertEqual(lowered.get("x-goog-api-key"), "AIzaTOPSECRET")


# --------------------------------------------------------------------------
# T6 — prober + thumbnail candidates
# --------------------------------------------------------------------------
class ProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(config.DATA_DIR) / "probe"
        cls.dir.mkdir(parents=True, exist_ok=True)
        cls.clip = cls.dir / "tiny.mp4"
        subprocess.run(
            [config.FFMPEG_BIN, "-v", "error", "-y", "-f", "lavfi",
             "-i", "testsrc=size=320x480:rate=10:duration=2",
             "-f", "lavfi", "-i", "sine=frequency=300:duration=2",
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-shortest", str(cls.clip)],
            check=True, capture_output=True,
        )

    def test_probe_media_reads_real_facts(self):
        info = ffmpeg.probe_media(self.clip)
        self.assertEqual((info["width"], info["height"]), (320, 480))
        self.assertAlmostEqual(info["duration"], 2.0, delta=0.3)
        self.assertAlmostEqual(info["fps"], 10.0, delta=0.5)
        self.assertEqual(info["orientation"], "vertical")
        self.assertEqual(info["video_codec"], "h264")
        self.assertGreater(info["size_bytes"], 1000)
        self.assertTrue(info["audio_codec"])

    def test_probe_media_is_none_safe(self):
        info = ffmpeg.probe_media(self.dir / "missing.mp4")
        self.assertFalse(info["exists"])
        self.assertIsNone(info["width"])
        self.assertIsNone(info["duration"])
        for key in ("fps", "video_codec", "audio_codec", "size_bytes",
                    "sample_rate", "aspect", "orientation"):
            self.assertIn(key, info)

    def test_frame_candidates_are_spread_and_numbered(self):
        out = self.dir / "cands"
        found = ffmpeg.frame_candidates(self.clip, 4, out, "tiny")
        self.assertEqual(len(found), 4)
        self.assertEqual([c["index"] for c in found], [0, 1, 2, 3])
        times = [c["time"] for c in found]
        self.assertEqual(times, sorted(times))
        self.assertGreater(times[-1] - times[0], 0.5)
        for cand in found:
            self.assertTrue((out / cand["file"]).is_file())
            self.assertGreater((out / cand["file"]).stat().st_size, 400)

    def test_frame_candidates_survive_a_bad_source(self):
        self.assertEqual(
            ffmpeg.frame_candidates(self.dir / "nope.mp4", 6, out_dir := self.dir,
                                    "nope"),
            [],
        )


class ConfigContractTests(unittest.TestCase):
    def test_version_and_names(self):
        from autoshorts import APP_NAME, __version__
        self.assertEqual(__version__, "0.5.0")
        self.assertEqual(APP_NAME, "Qyro")
        self.assertEqual(config.APP_NAME, "Qyro")

    def test_brand_preset_colours_are_valid_ass(self):
        for name, preset in config.CAPTION_BRAND_PRESETS.items():
            for key in ("primary", "outline_colour", "emphasis"):
                value = preset[key]
                self.assertTrue(value.startswith("&H") and len(value) == 10, name)
                int(value[2:], 16)      # must parse as hex

    def test_brands_are_named_for_the_ui(self):
        labels = [p["label"] for p in config.CAPTION_BRAND_PRESETS.values()]
        self.assertEqual(labels, ["Qyro Pop", "Qyro Minimal", "Qyro Neon"])
        self.assertEqual(config.CAPTION_BRANDS,
                         ("none",) + tuple(config.CAPTION_BRAND_PRESETS))

    def test_pack_footer_is_branded(self):
        pack = titles.generate_pack("Title", "text", "Episode", "viral", 30)
        self.assertIn("Made with Qyro", pack["description"])

    def test_no_new_runtime_dependencies(self):
        """v0.5.0 must stay stdlib + ffmpeg + yt-dlp, even on Termux."""
        import importlib
        for module in (logofx, beats, audioswap, engine, ffmpeg, maintenance):
            source = Path(module.__file__).read_text(encoding="utf-8")
            for line in source.splitlines():
                stripped = line.strip()
                if not stripped.startswith(("import ", "from ")):
                    continue
                body = stripped[5:] if stripped.startswith("from ") else stripped[7:]
                if body.startswith("."):
                    continue                     # intra-package import
                top = body.split()[0].split(".")[0]
                if top in ("autoshorts", "yt_dlp"):
                    continue          # the one external dep this project allows
                self.assertIn(
                    top,
                    {"__future__", "json", "math", "re", "subprocess", "time",
                     "pathlib", "uuid", "shutil", "os", "sys", "hashlib", "base64",
                     "tempfile", "typing", "csv", "io", "platform", "threading",
                     "queue", "urllib", "collections", "dataclasses", "functools",
                     "datetime", "random", "string", "itertools", "bisect",
                     "unicodedata", "textwrap", "statistics"},
                    f"{module.__name__} imports {top}",
                )


def _render_settings(params: dict) -> dict:
    """``Pipeline._render_settings`` is a staticmethod — call it directly."""
    from autoshorts.pipeline import Pipeline

    return Pipeline._render_settings(params)
