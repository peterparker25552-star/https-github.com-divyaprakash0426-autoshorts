"""v0.6.0 unit tests: subject tracking, caption fonts/animations/languages,
clip transitions, the quality gate and the option validation around them.

Every test here is pure maths or file text — no ffmpeg, no network — so the
suite stays fast. The real end-to-end render lives in ``test_http_v060``.
"""
from __future__ import annotations

import unittest
import tempfile
import shutil
import os
import io
from pathlib import Path

from autoshorts import config, ffmpeg, fonts, maintenance, quality, vision
from autoshorts import youtube
from autoshorts.highlights import Highlight
from autoshorts.transcripts import (Segment, parse_json3, parse_vtt,
                                    segments_for_window, strip_vtt_markup)


# --------------------------------------------------------------------------
# Tracking maths
# --------------------------------------------------------------------------
class HeatMapTests(unittest.TestCase):
    def test_filter_uses_full_resolution_chroma(self):
        graph = vision.heat_filter(160, 20, 11)
        # geq's cb()/cr() read the chroma planes at their native resolution,
        # so yuv420p puts the skin mask at half position. This is the fix.
        self.assertIn("format=yuv444p", graph)
        self.assertNotIn("format=yuv420p", graph)
        self.assertIn("tblend=all_mode=difference", graph)
        self.assertIn("scale=20:11:flags=area", graph)
        self.assertIn("%FPS%", graph)

    def test_filter_stacks_skin_over_motion(self):
        # v0.6.1: the speaker tracker needs skin and motion *separately*, so
        # the graph downscales each map and vstacks them instead of blending
        graph = vision.heat_filter(160, 20, 11)
        self.assertIn("vstack", graph)
        self.assertEqual(graph.count("scale=20:11:flags=area"), 2)

    def test_split_planes_and_legacy_frames(self):
        grid = (4, 2)
        skin = bytes([10, 20, 30, 40, 50, 60, 70, 80])
        motion = bytes([1, 2, 3, 4, 5, 6, 7, 8])
        pairs = vision.split_planes([skin + motion, skin], *grid)
        self.assertEqual(pairs[0], (skin, motion))
        # a legacy single-plane frame degrades to (skin, empty), never raises
        self.assertEqual(pairs[1], (skin, b""))
        self.assertEqual(vision.split_planes([], *grid), [])

    def test_grid_follows_aspect_and_is_bounded(self):
        self.assertEqual(vision.grid_for(1920, 1080), (20, 11))
        self.assertEqual(vision.grid_for(1080, 1920), (20, 14))  # clamped
        self.assertEqual(vision.grid_for(100, 100), (20, 14))    # clamped high
        self.assertEqual(vision.grid_for(1000, 100), (20, 6))    # clamped low

    def test_plan_fps_caps_long_windows(self):
        self.assertEqual(vision.plan_fps(30, 5), 5.0)
        self.assertLessEqual(vision.plan_fps(600, 5), 1.0)
        self.assertEqual(vision.plan_fps(0, 5), 5.0)   # degenerate -> default
        self.assertEqual(vision.plan_fps("junk", 5), vision.DEFAULT_FPS)

    def test_column_and_row_mass(self):
        frame = bytes([10, 20, 30, 40])
        self.assertEqual(vision.column_mass(frame, 2), [40.0, 60.0])
        self.assertEqual(vision.row_mass(frame, 2), [30.0, 70.0])

    def test_best_window_reports_the_centroid_not_the_middle(self):
        # a 4-cell window over a 1-cell subject: every covering position ties
        # on mass, so the centroid is the only tie-free answer
        mass = [0.0, 0.0, 100.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        centre, value, coverage = vision.best_window(mass, 4.0)
        self.assertAlmostEqual(centre, 2.5, places=2)
        self.assertAlmostEqual(value, 100.0, places=2)
        self.assertAlmostEqual(coverage, 1.0, places=4)

    def test_best_window_picks_the_heavier_of_two_blobs(self):
        mass = [50.0, 50.0, 0.0, 0.0, 0.0, 90.0, 90.0, 0.0]   # total 280
        centre, value, coverage = vision.best_window(mass, 2.0)
        self.assertGreater(centre, 5.0)
        self.assertAlmostEqual(value, 180.0, places=2)
        self.assertAlmostEqual(coverage, 180.0 / 280.0, places=3)

    def test_best_window_handles_degenerate_input(self):
        self.assertEqual(vision.best_window([], 3.0), (0.0, 0.0, 0.0))
        centre, value, coverage = vision.best_window([0.0, 0.0], 5.0)
        self.assertEqual(coverage, 0.0)
        self.assertGreaterEqual(centre, 0.0)

    def test_heat_targets_skips_empty_frames_and_measures_width(self):
        grid = (4, 2)
        lit = bytes([0, 200, 200, 0, 0, 150, 150, 0])
        dark = bytes(8)
        targets, confidence, width = vision.heat_targets(
            [lit, dark, lit], grid, 0.5, 1.0, 5.0
        )
        self.assertEqual(len(targets), 2)          # the dark frame is dropped
        self.assertAlmostEqual(targets[0][0], 0.0)
        # timestamps stay on the ORIGINAL frame clock: the dark frame at index
        # 1 is skipped, so the next target is frame 2 -> 2/5 s
        self.assertAlmostEqual(targets[1][0], 0.4)
        # columns 1 and 2 are lit -> centroid 2.0 cells of 4 -> 0.5
        self.assertAlmostEqual(targets[0][1], 0.5, places=3)
        self.assertGreater(width, 0.0)
        self.assertGreater(confidence, 0.0)

    def test_heat_targets_on_nothing(self):
        self.assertEqual(vision.heat_targets([], (4, 2), 0.5, 1.0, 5.0),
                         ([], 0.0, 0.0))


class SmoothingTests(unittest.TestCase):
    def test_velocity_is_capped(self):
        # subject teleports 0.8 of the frame in 0.2 s; the camera may only move
        targets = [(0.0, 0.1, 0.5), (0.2, 0.9, 0.5)]
        out = vision.smooth_targets(targets, max_speed=0.5, alpha=1.0,
                                    deadband=0.0, hold=0.0)
        self.assertEqual(len(out), 2)
        self.assertLessEqual(out[1][1] - out[0][1], 0.5 * 0.2 + 1e-6)

    def test_deadband_freezes_small_drift(self):
        targets = [(float(i), 0.5 + i * 0.001, 0.5) for i in range(6)]
        out = vision.smooth_targets(targets, deadband=0.05, alpha=1.0,
                                    max_speed=10.0, hold=0.0)
        self.assertAlmostEqual(max(x for _t, x, _y in out), 0.5, places=6)

    def test_opening_is_held_still(self):
        targets = [(0.0, 0.2, 0.5), (0.2, 0.5, 0.5), (0.4, 0.8, 0.5)]
        out = vision.smooth_targets(targets, hold=0.5, alpha=1.0,
                                    deadband=0.0, max_speed=10.0)
        self.assertAlmostEqual(out[1][1], 0.2, places=6)
        self.assertAlmostEqual(out[2][1], 0.2, places=6)

    def test_output_stays_inside_the_frame(self):
        targets = [(float(i) * 0.5, 1.7, -0.4) for i in range(5)]
        for _t, x, y in vision.smooth_targets(targets, alpha=1.0, deadband=0.0,
                                              hold=0.0, max_speed=10.0):
            self.assertGreaterEqual(x, 0.0)
            self.assertLessEqual(x, 1.0)
            self.assertGreaterEqual(y, 0.0)
            self.assertLessEqual(y, 1.0)

    def test_single_point_and_empty(self):
        self.assertEqual(vision.smooth_targets([]), [])
        self.assertEqual(vision.smooth_targets([(0.0, 0.3, 0.5)]),
                         [(0.0, 0.3, 0.5)])


class SimplifyTests(unittest.TestCase):
    def test_straight_line_collapses_to_two_points(self):
        pts = [(i * 0.1, i * 0.01, 0.5) for i in range(20)]
        out = vision.simplify_path(pts, 0.001)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0][0], 0.0)
        self.assertAlmostEqual(out[-1][0], 1.9, places=3)

    def test_a_kink_is_kept(self):
        pts = [(0.0, 0.10, 0.5), (0.5, 0.10, 0.5), (1.0, 0.90, 0.5)]
        out = vision.simplify_path(pts, 0.01)
        self.assertEqual(len(out), 3)

    def test_max_points_is_respected(self):
        import math
        pts = [(i * 0.05, 0.5 + 0.4 * math.sin(i * 0.9), 0.5) for i in range(120)]
        out = vision.simplify_path(pts, 0.0001, max_points=16)
        self.assertLessEqual(len(out), 16)
        self.assertEqual(out[0][0], 0.0)
        self.assertEqual(out[-1][0], pts[-1][0])

    def test_travel(self):
        self.assertEqual(vision.path_travel([]), 0.0)
        self.assertAlmostEqual(
            vision.path_travel([(0, 0.1, 0), (1, 0.4, 0), (2, 0.2, 0)]), 0.5, 4
        )


class ZoomTests(unittest.TestCase):
    def test_named_modes_are_exact(self):
        self.assertEqual(vision.choose_zoom("tight", 0.2, 0.3, 1920, 1080)[0],
                         vision.ZOOM_FRACTIONS["tight"])
        self.assertEqual(vision.choose_zoom("wide", 0.2, 0.3, 1920, 1080)[0], 1.0)

    def test_auto_refuses_to_upscale_into_mush(self):
        # a 720p source cropped to 9:16 is already a 2.6x upscale at full crop
        zoom, note = vision.choose_zoom("auto", 0.15, 0.316, 1280, 1080)
        self.assertEqual(zoom, 1.0)
        self.assertIn("upscale", note)

    def test_auto_punches_in_on_a_4k_source(self):
        zoom, _note = vision.choose_zoom("auto", 0.15, 0.316, 3840, 1080)
        self.assertLess(zoom, 1.0)
        self.assertGreaterEqual(zoom, vision.ZOOM_FLOOR)

    def test_no_subject_means_no_zoom(self):
        self.assertEqual(vision.choose_zoom("auto", 0.0, 0.3, 3840, 1080)[0], 1.0)

    def test_bad_input_is_safe(self):
        self.assertEqual(vision.choose_zoom("auto", 0.2, 0.0, 1920, 1080)[0], 1.0)
        self.assertEqual(vision.choose_zoom("auto", 0.2, 0.3, 0, 0)[0], 1.0)


class TrackPathTests(unittest.TestCase):
    def test_round_trip(self):
        plan = vision.TrackPath(
            keyframes=[(0.0, 0.5, 0.5), (1.0, 0.6, 0.5)],
            zoom=0.9, backend="heuristic", fps=5.0, frames=10,
            confidence=0.8, travel=0.1, subject_width=0.2, note="zoom=auto",
        )
        back = vision.TrackPath.from_dict(plan.as_dict())
        self.assertEqual(back.keyframes, plan.keyframes)
        self.assertEqual(back.backend, "heuristic")
        self.assertAlmostEqual(back.zoom, 0.9)

    def test_from_dict_rejects_junk(self):
        with self.assertRaises(ValueError):
            vision.TrackPath.from_dict({})
        with self.assertRaises(ValueError):
            vision.TrackPath.from_dict({"keyframes": ["nope"]})

    def test_cache_is_optional_and_never_raises(self):
        plan = vision.TrackPath(keyframes=[(0.0, 0.5, 0.5)])
        missing = Path("/nonexistent-dir-xyz/track.json")
        self.assertIsNone(vision.load_cache(missing))
        vision.save_cache(missing, plan)      # must not raise

    def test_off_mode_returns_none(self):
        self.assertIsNone(vision.track_window(
            Path("nope.mp4"), 0, 10, 1920, 1080, 720, 1280, 0.3, 1.0, mode="off"
        ))

    def test_full_frame_needs_no_tracking(self):
        self.assertIsNone(vision.track_window(
            Path("nope.mp4"), 0, 10, 1080, 1920, 1080, 1920, 1.0, 1.0,
            mode="vision",
        ))


# --------------------------------------------------------------------------
# ffmpeg expression builders
# --------------------------------------------------------------------------
class ExpressionTests(unittest.TestCase):
    def test_piecewise_interpolates(self):
        expr = ffmpeg._piecewise_expr([(0.0, 10.0), (2.0, 30.0)])
        self.assertIn("if(lt(t,2.000)", expr)
        self.assertIn("10.00+(20.00)*(t-0.000)/2.000", expr)

    def test_piecewise_single_and_empty(self):
        self.assertEqual(ffmpeg._piecewise_expr([]), "0")
        self.assertEqual(ffmpeg._piecewise_expr([(0.0, 42.0)]), "42.00")

    def test_anchor_drops_constant_runs(self):
        merged = ffmpeg._anchor([(3.0, 5), (1.0, 5), (2.0, 5), (4.0, 9)])
        self.assertEqual(merged[0], (0.0, 5))
        self.assertEqual(len(merged), 2)

    def test_clamped_expr_leaves_commas_for_the_escaper(self):
        expr = ffmpeg._clamped_even_expr("if(lt(t,1),0,10)", 100)
        self.assertIn(",0)", expr)         # NOT "\\,0)" — that would double-escape
        self.assertIn(",100)", expr)
        self.assertEqual(ffmpeg._clamped_even_expr("5", 0), "0")

    def test_path_expressions_maps_centre_to_offset(self):
        # subject at the far right of a 1000 px frame, 200 px crop -> x = 800
        expr, _y = ffmpeg.path_expressions(
            [(0.0, 1.0, 0.5)], [(0.0, 10.0)], 1.0, 200, 200, 1000, 1000
        )
        self.assertIn("800", expr)

    def test_path_expressions_respects_speed_and_keeps(self):
        expr, _y = ffmpeg.path_expressions(
            [(0.0, 0.5, 0.5), (4.0, 0.9, 0.5)], [(0.0, 2.0), (4.0, 6.0)],
            2.0, 200, 200, 1000, 1000,
        )
        # t=4 in the source is t=2 after the cut, then /2 for speed -> t=1
        self.assertIn("if(lt(t,1.000)", expr)

    def test_path_expressions_without_freedom(self):
        self.assertEqual(
            ffmpeg.path_expressions([(0.0, 0.5, 0.5)], None, 1.0,
                                    1000, 1000, 1000, 1000),
            ("0", "0"),
        )
        self.assertEqual(ffmpeg.path_expressions([], None, 1.0, 2, 2, 10, 10),
                         ("0", "0"))


class TransitionTests(unittest.TestCase):
    def test_none_and_unknown_are_empty(self):
        self.assertEqual(ffmpeg.transition_stage("none", 10.0, 720, 1280), "")
        self.assertEqual(ffmpeg.transition_stage("bogus", 10.0, 720, 1280), "")
        self.assertEqual(ffmpeg.transition_stage("", 10.0, 720, 1280), "")

    def test_fade_in_and_out_fit_inside_the_clip(self):
        stage = ffmpeg.transition_stage("fade", 10.0, 720, 1280, 0.28)
        self.assertIn("fade=t=in:st=0:d=0.280", stage)
        self.assertIn("fade=t=out:st=9.720:d=0.280", stage)

    def test_dip_uses_white(self):
        self.assertIn("color=white", ffmpeg.transition_stage("dip", 8.0, 720, 1280))

    def test_flash_is_opening_only(self):
        stage = ffmpeg.transition_stage("flash", 8.0, 720, 1280)
        self.assertIn("fade=t=in", stage)
        self.assertNotIn("fade=t=out", stage)

    def test_slide_pads_then_crops_back_to_size(self):
        stage = ffmpeg.transition_stage("slide", 8.0, 720, 1280)
        self.assertIn("pad=720:2560:0:1280", stage)
        self.assertIn("crop=720:1280:0:", stage)

    def test_too_short_a_clip_gets_no_transition(self):
        self.assertEqual(ffmpeg.transition_stage("fade", 0.2, 720, 1280), "")
        self.assertEqual(ffmpeg.transition_stage("fade", None, 720, 1280), "")


class VideoChainTests(unittest.TestCase):
    def test_track_plan_wins_over_smart(self):
        chain = ffmpeg.video_chain(
            "smart", 720, 1280, None, 10.0, False, vin="0:v", fmt="vertical",
            smart=(400, 720, "10"), speed=1.0,
            track_plan=(300, 540, "min(max(2*trunc((t*10)/2),0),980)", "0"),
        )
        self.assertIn("crop=300:540:", chain)
        self.assertNotIn("crop=400:720:", chain)
        self.assertTrue(chain.endswith("[v]"))

    def test_smart_fallback_still_works(self):
        chain = ffmpeg.video_chain("smart", 720, 1280, None, 10.0, False,
                                   smart=(400, 720, "if(lt(t,2),0,10)"))
        self.assertIn("crop=400:720:", chain)

    def test_transition_is_applied_before_captions(self):
        ass = Path("/tmp/x.ass")
        chain = ffmpeg.video_chain("crop", 720, 1280, ass, 10.0, False,
                                   transition="fade")
        self.assertLess(chain.index("fade=t=in"), chain.index("ass="))

    def test_fontsdir_is_attached_to_the_ass_filter(self):
        chain = ffmpeg.video_chain("fit", 720, 1280, Path("/tmp/x.ass"), 10.0,
                                   False, fontsdir="/data/fonts")
        self.assertIn("fontsdir=/data/fonts", chain)

    def test_wide_ignores_tracking(self):
        chain = ffmpeg.video_chain("smart", 1280, 720, None, 10.0, False,
                                   fmt="wide", track_plan=(300, 540, "5", "0"))
        self.assertIn("force_original_aspect_ratio=decrease", chain)
        self.assertNotIn("crop=300:540", chain)


# --------------------------------------------------------------------------
# Transcripts with word timings
# --------------------------------------------------------------------------
class WordTimingTests(unittest.TestCase):
    def test_json3_word_offsets(self):
        raw = ('{"events":[{"tStartMs":1000,"dDurationMs":2000,"segs":['
               '{"utf8":"hello "},{"utf8":"world","tOffsetMs":900}]}]}')
        seg = parse_json3(raw)[0]
        self.assertEqual(seg.words, [("hello", 1.0, 1.9), ("world", 1.9, 3.0)])

    def test_json3_without_offsets_yields_no_words(self):
        raw = ('{"events":[{"tStartMs":0,"dDurationMs":1000,"segs":['
               '{"utf8":"no timings here"}]}]}')
        self.assertEqual(parse_json3(raw)[0].words, [])

    def test_vtt_word_tags(self):
        raw = ("WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n"
               "<00:00:01.000><c>namaste</c> <00:00:01.800><c>dosto</c>\n")
        seg = parse_vtt(raw)[0]
        self.assertEqual(seg.text, "namaste dosto")
        self.assertEqual(seg.words, [("namaste", 1.0, 1.8), ("dosto", 1.8, 3.0)])

    def test_vtt_markup_is_stripped_from_the_text(self):
        raw = ("WEBVTT\n\n00:00:01.000 --> 00:00:04.000\n"
               "<00:00:01.000><v Speaker>hello there</v> <00:00:02.500><c>friend</c>\n")
        seg = parse_vtt(raw)[0]
        self.assertEqual(seg.text, "hello there friend")
        self.assertNotIn("<", seg.text)

    def test_untagged_vtt_still_parses(self):
        seg = parse_vtt("WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nplain text\n")[0]
        self.assertEqual(seg.text, "plain text")
        self.assertEqual(seg.words, [])

    def test_strip_vtt_markup(self):
        self.assertEqual(strip_vtt_markup("<00:00:01.000><c>hi</c> <b>you</b>"),
                         "hi you")
        self.assertEqual(strip_vtt_markup(""), "")

    def test_window_keeps_only_inside_words(self):
        seg = Segment(0.0, 4.0, "a b c",
                      [("a", 0.0, 1.0), ("b", 1.0, 2.0), ("c", 2.0, 4.0)])
        out = segments_for_window([seg], 1.5, 3.0)
        self.assertEqual([w[0] for w in out[0].words], ["b", "c"])
        self.assertAlmostEqual(out[0].words[0][1], 1.5)

    def test_plain_segments_are_still_supported(self):
        seg = Segment(0.0, 1.0, "hi")
        self.assertEqual(seg.words, [])
        self.assertEqual(segments_for_window([seg], 0.0, 1.0)[0].words, [])


# --------------------------------------------------------------------------
# Caption generation
# --------------------------------------------------------------------------
class CaptionTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(config.SUBS_DIR)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _make(self, segments, **kwargs):
        out = self.dir / f"v060-{abs(hash(repr(kwargs))) % 100000}.ass"
        ffmpeg.make_ass(segments, 0.0, 10.0, out, 720, 1280, **kwargs)
        return out.read_text(encoding="utf-8")

    def test_every_animation_emits_a_valid_override_block(self):
        segs = [Segment(0.0, 4.0, "hello there friend")]
        for anim in config.CAPTION_ANIMS:
            text = self._make(segs, caption_style="classic", anim=anim)
            self.assertIn("Dialogue:", text, anim)
            for line in text.splitlines():
                if line.startswith("Dialogue:"):
                    body = line.split(",,", 1)[1]
                    self.assertEqual(body.count("{"), body.count("}"), anim)
            if anim == "none":
                self.assertNotIn("\\fad", text)

    def test_karaoke_uses_kf_tags_and_a_distinct_secondary_colour(self):
        text = self._make([Segment(0.0, 4.0, "one two three four")],
                          caption_style="minimal", anim="karaoke")
        self.assertIn("\\kf", text)
        style_line = next(l for l in text.splitlines() if l.startswith("Style:"))
        fields = style_line.split(",")
        self.assertNotEqual(fields[3], fields[4])   # primary != secondary

    def test_word_timings_drive_the_chunks(self):
        segs = [Segment(0.0, 4.0, "alpha beta gamma delta",
                        [("alpha", 0.0, 0.4), ("beta", 0.4, 2.0),
                         ("gamma", 2.0, 2.3), ("delta", 2.3, 4.0)])]
        text = self._make(segs, caption_style="classic", words_per_caption=2)
        starts = [l.split(",")[1] for l in text.splitlines()
                  if l.startswith("Dialogue:")]
        # the second chunk starts when "gamma" is spoken (2.0 s), not at 2.0 s
        # of an even split (which would also be 2.0) — check the *first* one
        self.assertEqual(starts[0], "0:00:00.00")
        self.assertEqual(starts[1], "0:00:02.00")

    @staticmethod
    def _bodies(text):
        """Caption text of every Dialogue line (everything after Effect)."""
        return [
            line.split(",0,0,0,,", 1)[1]
            for line in text.splitlines()
            if line.startswith("Dialogue:")
        ]

    def test_hindi_is_never_upper_cased_and_gets_more_words(self):
        hindi = [Segment(0.0, 6.0, "नमस्ते दोस्तों आज हम बात करेंगे")]
        bodies = self._bodies(
            self._make(hindi, caption_style="classic", language="hi")
        )
        # all six words fit on one line at the Hindi chunk size, verbatim
        self.assertEqual(len(bodies), 1)
        self.assertEqual(bodies[0], "नमस्ते दोस्तों आज हम बात करेंगे")

    def test_english_classic_still_upper_cases(self):
        bodies = self._bodies(
            self._make([Segment(0.0, 4.0, "hello there")],
                       caption_style="classic")
        )
        self.assertEqual(bodies, ["HELLO THERE"])

    def test_english_gets_fewer_words_per_line_than_hindi(self):
        words = "one two three four five six"
        en = self._bodies(self._make([Segment(0.0, 6.0, words)],
                                     caption_style="classic", language="en"))
        hi = self._bodies(self._make([Segment(0.0, 6.0, words)],
                                     caption_style="classic", language="hi"))
        self.assertGreater(len(en), len(hi))

    def test_devanagari_text_forces_a_devanagari_font(self):
        text = self._make([Segment(0.0, 4.0, "नमस्ते")],
                          caption_style="classic", font="bold")
        style = next(l for l in text.splitlines() if l.startswith("Style:"))
        family = style.split("Style: ", 1)[1].split(",")[1]
        self.assertTrue(
            fonts.is_devanagari_font(family)
            or not fonts.installed_families(),
            f"{family} cannot draw Devanagari",
        )

    def test_unknown_animation_falls_back_to_none(self):
        text = self._make([Segment(0.0, 4.0, "hello")], anim="teleport")
        self.assertNotIn("\\fad", text)

    def test_anim_block_shapes(self):
        params = {"outline": 3}
        self.assertEqual(ffmpeg._anim_block("none", params), "")
        self.assertEqual(ffmpeg._anim_block("karaoke", params), "")
        self.assertIn("\\fad", ffmpeg._anim_block("fade", params))
        self.assertIn("\\fscx62", ffmpeg._anim_block("pop", params))
        self.assertIn("\\fscx138", ffmpeg._anim_block("zoom", params))
        self.assertIn("\\fscy42", ffmpeg._anim_block("bounce", params))
        self.assertIn("\\bord9", ffmpeg._anim_block("glow", params))
        self.assertIn("\\blur7", ffmpeg._anim_block("blurin", params))
        self.assertIn("\\frz-6", ffmpeg._anim_block("drop", params))

    def test_karaoke_text_distributes_the_duration(self):
        text = ffmpeg.karaoke_text(["a", "bb", "ccc"], 300)
        self.assertEqual(text.count("\\kf"), 3)
        total = sum(int(part.split("}")[0][3:]) for part in text.split("{")[1:])
        self.assertEqual(total, 300)
        self.assertEqual(ffmpeg.karaoke_text([], 100), "")


# --------------------------------------------------------------------------
# Fonts
# --------------------------------------------------------------------------
class FontTests(unittest.TestCase):
    def test_wants_devanagari(self):
        self.assertTrue(fonts.wants_devanagari("नमस्ते", "auto"))
        self.assertTrue(fonts.wants_devanagari("hello", "hi"))
        self.assertFalse(fonts.wants_devanagari("नमस्ते", "en"))
        self.assertFalse(fonts.wants_devanagari("hello", "auto"))

    def test_devanagari_share(self):
        self.assertEqual(fonts.devanagari_share(""), 0.0)
        # "नम hello" has 7 letters, 2 of them Devanagari
        self.assertAlmostEqual(fonts.devanagari_share("नम hello"), 2 / 7, places=3)

    def test_families_from_filename(self):
        names = fonts._families_from_filename("NotoSansDevanagari-Bold")
        self.assertIn("NotoSansDevanagari", names)
        self.assertIn("NotoSansDevanagari-Bold", names)
        names = fonts._families_from_filename("Shobhika-Regular")
        self.assertIn("Shobhika", names)

    def test_resolve_always_returns_a_usable_family(self):
        for font_id in config.CAPTION_FONTS:
            family, note = fonts.resolve(font_id)
            self.assertTrue(family, font_id)
            self.assertIn("font=", note)

    def test_resolve_unknown_id_falls_back(self):
        family, _note = fonts.resolve("not-a-real-font")
        self.assertTrue(family)

    def test_resolve_devanagari_rescues_a_latin_choice(self):
        family, _note = fonts.resolve("bold", "नमस्ते दोस्तों", "auto")
        pool = fonts.installed_families()
        if pool:
            self.assertTrue(fonts.is_devanagari_font(family)
                            or fonts.families_for_lang("hi") == set())

    def test_available_fonts_covers_every_id(self):
        listed = fonts.available_fonts()
        self.assertEqual([f["id"] for f in listed], list(config.CAPTION_FONTS))
        for entry in listed:
            self.assertIn("resolved", entry)
            self.assertIn("installed", entry)

    def test_user_font_roots_are_absolute(self):
        for root in fonts.user_font_roots():
            self.assertTrue(root.is_absolute())

    def test_ensure_installed_is_idempotent(self):
        first = fonts.ensure_installed()
        second = fonts.ensure_installed()
        self.assertIn("copied", first)
        self.assertEqual(first.get("root"), second.get("root"))


# --------------------------------------------------------------------------
# Quality gate
# --------------------------------------------------------------------------
class FreshInstallFontTests(unittest.TestCase):
    """A fresh clone ships no fonts (``data/`` is user state), so the Hindi
    path has to be able to obtain one. This is the whole Android story: no
    sudo, no apt, no GitHub raw fetch."""

    def _fake_sdist(self, tmp: Path) -> Path:
        import tarfile

        archive = tmp / "devanagari_fonts-0.2.0.tar.gz"
        root = "devanagari_fonts-0.2.0/src/devanagari_fonts/fonts/Shobhika-1.05"
        with tarfile.open(archive, "w:gz") as tar:
            for name in ("Shobhika-Regular.otf", "Shobhika-Bold.otf"):
                blob = f"fake font bytes for {name}".encode()
                info = tarfile.TarInfo(f"{root}/{name}")
                info.size = len(blob)
                tar.addfile(info, io.BytesIO(blob))
        return archive

    def test_download_extracts_the_otf_files_into_the_fonts_dir(self):
        import subprocess as sp

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            archive = self._fake_sdist(tmp_path)
            download_dir = tmp_path / "dl"
            download_dir.mkdir()

            original_run = sp.run
            original_dir = config.FONTS_DIR

            def fake_run(cmd, **kwargs):
                # pip download writes the sdist into the -d directory
                target = Path(cmd[cmd.index("-d") + 1])
                target.mkdir(parents=True, exist_ok=True)
                shutil.copy2(archive, target / archive.name)
                return original_run(["true"], **{k: v for k, v in kwargs.items()
                                                 if k in ("capture_output",
                                                          "timeout")})

            config.FONTS_DIR = download_dir
            sp.run = fake_run
            try:
                result = fonts.download_devanagari()
            finally:
                sp.run = original_run
                config.FONTS_DIR = original_dir

            self.assertTrue(result["ok"], result["detail"])
            self.assertEqual(sorted(result["installed"]),
                             ["Shobhika-Bold.otf", "Shobhika-Regular.otf"])
            self.assertTrue((download_dir / "Shobhika-Regular.otf").is_file())

    def test_download_failure_is_a_status_not_an_exception(self):
        import subprocess as sp

        original_run = sp.run

        def failing_run(cmd, **kwargs):
            raise OSError("no network here")

        sp.run = failing_run
        try:
            result = fonts.download_devanagari()
        finally:
            sp.run = original_run
        self.assertFalse(result["ok"])
        self.assertIn("pip", result["detail"])
        self.assertEqual(result["installed"], [])

    def test_ensure_installed_tries_the_download_when_nothing_is_bundled(self):
        """With an empty fonts dir and no Devanagari font installed, the
        install step must reach for the download path."""
        with tempfile.TemporaryDirectory() as tmp:
            original_dir = config.FONTS_DIR
            original_has = fonts.has_devanagari_font
            original_dl = fonts.download_devanagari
            calls = []
            config.FONTS_DIR = Path(tmp) / "fonts"
            (Path(tmp) / "fonts").mkdir()
            fonts._CACHE.clear()
            fonts.has_devanagari_font = lambda: False
            fonts.download_devanagari = lambda **kw: (
                calls.append(1) or {"ok": True, "installed": ["Shobhika-Regular.otf"],
                                    "detail": "stub"})
            try:
                status = fonts.ensure_installed(force=True)
            finally:
                fonts.has_devanagari_font = original_has
                fonts.download_devanagari = original_dl
                config.FONTS_DIR = original_dir
                fonts._CACHE.clear()
            self.assertEqual(len(calls), 1, "the download path must be tried")
            self.assertEqual(status["downloaded"], 1)

    def test_user_font_roots_survives_an_xdg_dir(self):
        """``user_font_roots`` builds a Path from $XDG_DATA_HOME at runtime."""
        original = os.environ.get("XDG_DATA_HOME")
        os.environ["XDG_DATA_HOME"] = "/tmp/qyro-xdg-test"
        try:
            roots = [str(r) for r in fonts.user_font_roots()]
        finally:
            if original is None:
                os.environ.pop("XDG_DATA_HOME", None)
            else:
                os.environ["XDG_DATA_HOME"] = original
        self.assertIn("/tmp/qyro-xdg-test/fonts", roots)


class QualityTests(unittest.TestCase):
    SEGMENTS = [
        Segment(0.0, 3.0, "hello there"),
        Segment(3.0, 6.0, "my friends"),
        Segment(12.0, 15.0, "after a long pause"),
    ]

    def test_speech_ratio(self):
        self.assertAlmostEqual(quality.speech_ratio(self.SEGMENTS, 0, 15), 0.6, 3)
        self.assertAlmostEqual(quality.speech_ratio(self.SEGMENTS, 0, 6), 1.0, 3)
        self.assertEqual(quality.speech_ratio([], 0, 10), 0.0)

    def test_longest_gap_includes_the_edges(self):
        self.assertAlmostEqual(quality.longest_gap(self.SEGMENTS, 0, 15), 6.0, 3)
        self.assertAlmostEqual(quality.longest_gap(self.SEGMENTS, 0, 6), 0.0, 3)
        self.assertAlmostEqual(quality.longest_gap([], 0, 4), 4.0, 3)

    def test_trim_edges_leaves_padding_and_a_floor(self):
        start, end = quality.trim_edges(self.SEGMENTS, 0.0, 20.0)
        self.assertLessEqual(start, 0.12 + 1e-6)
        self.assertLess(end, 20.0)
        self.assertGreaterEqual(end - start, 3.0)

    def test_trim_edges_never_collapses(self):
        self.assertEqual(quality.trim_edges([], 1.0, 2.0), (1.0, 2.0))
        start, end = quality.trim_edges(self.SEGMENTS, 0.0, 30.0, max_trim=100)
        self.assertGreaterEqual(end - start, 3.0)

    def test_grade_penalises_sparse_windows(self):
        multiplier, reasons = quality.grade(self.SEGMENTS, 0, 15)
        self.assertLess(multiplier, 1.0)
        self.assertTrue(any("pause" in r for r in reasons))
        self.assertEqual(quality.grade(self.SEGMENTS, 0, 6)[0], 1.0)

    def test_refine_reorders_by_adjusted_score(self):
        moments = [
            Highlight(start=0.0, end=15.0, score=10.0, title="sparse",
                      reasons=[], sentences=[]),
            Highlight(start=0.0, end=6.0, score=9.0, title="dense",
                      reasons=[], sentences=[]),
        ]
        out = quality.refine_moments(moments, self.SEGMENTS)
        self.assertEqual(out[0].title, "dense")
        self.assertLess(out[1].score, 10.0)
        self.assertTrue(any("quality:" in r for r in out[1].reasons))
        # the inputs are untouched
        self.assertEqual(moments[0].score, 10.0)
        self.assertEqual(moments[0].end, 15.0)

    def test_refine_disabled_and_empty(self):
        self.assertEqual(quality.refine_moments([], self.SEGMENTS), [])
        moments = [Highlight(start=0.0, end=15.0, score=10.0, title="x")]
        self.assertEqual(quality.refine_moments(moments, self.SEGMENTS,
                                                enabled=False)[0].score, 10.0)

    def test_window_report(self):
        report = quality.window_report(self.SEGMENTS, 0, 15)
        self.assertEqual(
            sorted(report), ["longest_gap", "notes", "quality",
                             "speech_ratio", "trimmed_end", "trimmed_start"]
        )
        self.assertLess(report["quality"], 1.0)


# --------------------------------------------------------------------------
# Option plumbing
# --------------------------------------------------------------------------
class OptionTests(unittest.TestCase):
    def test_sub_lang_chains(self):
        self.assertEqual(youtube.sub_lang_chain("hi")[0], "hi")
        self.assertEqual(youtube.sub_lang_chain("hinglish")[0], "hi")
        self.assertEqual(youtube.sub_lang_chain("en")[0], "en")
        self.assertEqual(youtube.sub_lang_chain("nonsense"),
                         list(config.SUB_LANG_CHAIN))

    def test_new_render_defaults(self):
        for key in ("captions_font", "captions_anim", "transition",
                    "track_mode", "track_zoom", "language", "quality_gate"):
            self.assertIn(key, maintenance.RENDER_DEFAULTS)
            self.assertIn(key, maintenance.RENDER_OPT_KEYS)
            self.assertIn(key, maintenance.SHORTS_KEYS)
            self.assertIn(key, maintenance.RERENDER_KEYS)

    def test_every_choice_key_has_a_choice_table(self):
        for key in ("captions_font", "captions_anim", "transition",
                    "track_mode", "track_zoom", "language"):
            self.assertIn(key, maintenance._CHOICES)

    def test_valid_options_pass_through(self):
        opts = maintenance.render_opts_from({
            "captions_font": "rounded", "captions_anim": "karaoke",
            "transition": "slide", "track_mode": "vision",
            "track_zoom": "tight", "language": "hi", "quality_gate": False,
        })
        self.assertEqual(opts["captions_anim"], "karaoke")
        self.assertEqual(opts["transition"], "slide")
        self.assertEqual(opts["track_mode"], "vision")
        self.assertEqual(opts["language"], "hi")
        self.assertIs(opts["quality_gate"], False)

    def test_bad_options_are_422(self):
        for body in ({"captions_font": "comic"}, {"captions_anim": "spin"},
                     {"transition": "wipe"}, {"track_mode": "magic"},
                     {"track_zoom": "huge"}, {"language": "klingon"}):
            with self.assertRaises(maintenance.ServiceError) as ctx:
                maintenance.render_opts_from(body)
            self.assertEqual(ctx.exception.status, 422)
            self.assertIn(list(body)[0], ctx.exception.detail)

    def test_quality_gate_must_be_a_bool(self):
        with self.assertRaises(maintenance.ServiceError):
            maintenance.render_opts_from({"quality_gate": "yes"})

    def test_stale_stored_values_are_normalised(self):
        opts = maintenance.render_opts_from(
            {}, base={"captions_anim": "gone", "track_mode": "gone",
                      "language": "gone", "transition": "gone"}
        )
        self.assertEqual(opts["captions_anim"], config.DEFAULT_CAPTION_ANIM)
        self.assertEqual(opts["track_mode"], config.DEFAULT_TRACK_MODE)
        self.assertEqual(opts["language"], config.DEFAULT_LANGUAGE)
        self.assertEqual(opts["transition"], config.DEFAULT_TRANSITION)

    def test_catalog_lists_every_choice(self):
        catalog = maintenance.render_options_catalog()
        self.assertEqual(
            [item["id"] for item in catalog["anims"]], list(config.CAPTION_ANIMS)
        )
        self.assertEqual(
            [item["id"] for item in catalog["transitions"]],
            list(config.TRANSITIONS),
        )
        self.assertEqual(
            [item["id"] for item in catalog["languages"]], list(config.LANGUAGES)
        )
        self.assertEqual(
            [item["id"] for item in catalog["fonts"]], list(config.CAPTION_FONTS)
        )
        self.assertIn("devanagari_ready", catalog)
        self.assertIn("face_model_ready", catalog)
        self.assertEqual(catalog["fonts_dir"], str(config.FONTS_DIR))


if __name__ == "__main__":
    unittest.main()
