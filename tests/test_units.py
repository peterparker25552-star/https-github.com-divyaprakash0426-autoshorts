"""Pure-logic tests: motion math, time warp/remap, crop expressions, ASS
features, render-option validation, storage accounting and title packs."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from autoshorts import config, ffmpeg, maintenance, titles
from autoshorts.store import Store
from autoshorts.transcripts import Segment

from . import util


class MotionMathTests(unittest.TestCase):
    """SMART crop decisions: active side wins / calm→center / empty→center."""

    def test_active_side_wins(self):
        self.assertEqual(
            ffmpeg.pick_third({"left": 5.0, "center": 1.0, "right": 1.0}, "center"),
            "left",
        )
        self.assertEqual(
            ffmpeg.pick_third({"left": 1.0, "center": 1.0, "right": 9.0}, "center"),
            "right",
        )

    def test_hysteresis_keeps_incumbent(self):
        # Challenger is higher but NOT clearly higher (< 1.25x) → hold.
        self.assertEqual(
            ffmpeg.pick_third({"left": 1.2, "center": 1.0, "right": 0.0}, "center"),
            "center",
        )
        # 1.3x is clearly higher than 1.25x threshold → switch
        self.assertEqual(
            ffmpeg.pick_third({"left": 1.3, "center": 1.0, "right": 0.0}, "center"),
            "left",
        )

    def test_calm_goes_center(self):
        self.assertEqual(
            ffmpeg.pick_third({"left": 0.30, "center": 0.20, "right": 0.10}, "left"),
            "center",
        )
        # just above the calm line → normal hysteresis applies
        self.assertEqual(
            ffmpeg.pick_third({"left": 0.36, "center": 0.0, "right": 0.0}, "center"),
            "left",
        )

    def test_empty_goes_center(self):
        self.assertEqual(ffmpeg.pick_third({}, "left"), "center")
        self.assertEqual(
            ffmpeg.pick_third({"left": 0, "center": 0, "right": 0}, "right"),
            "center",
        )

    def test_chunk_winners_remap_to_even_offsets(self):
        positions = [(0.0, 2), (2.0, 2), (4.0, 0)]  # right, right, left
        expr = ffmpeg.smart_x_expression(positions, [], 1.0, 202, 640)
        span = 640 - 202
        right_x = int(round(span / 2) * 2)          # third index 2 → full span
        self.assertEqual(right_x % 2, 0)
        # right wins for [0,4), left (x=0) afterwards
        self.assertEqual(expr, f"if(lt(t,4.00),{right_x},0)")
        for num in re.findall(r",(-?\d+)[,)]", expr):
            self.assertEqual(int(num) % 2, 0, f"odd offset {num} in {expr}")


class WarpTests(unittest.TestCase):
    """remap identity / speed / silence-cut remapping."""

    def test_identity(self):
        identity = [(0.0, float("inf"))]
        self.assertAlmostEqual(ffmpeg.remap_time(0.0, identity), 0.0)
        self.assertAlmostEqual(ffmpeg.remap_time(12.34, identity), 12.34)

    def test_cut_remap(self):
        keeps = [(0.0, 2.0), (3.0, 8.0)]
        self.assertAlmostEqual(ffmpeg.remap_time(1.0, keeps), 1.0)
        self.assertAlmostEqual(ffmpeg.remap_time(2.5, keeps), 2.0)  # inside a cut
        self.assertAlmostEqual(ffmpeg.remap_time(3.0, keeps), 2.0)
        self.assertAlmostEqual(ffmpeg.remap_time(5.0, keeps), 4.0)
        self.assertAlmostEqual(ffmpeg.remap_time(9.0, keeps), 7.0)

    def test_speed_divides_output_time(self):
        positions = [(0.0, 0), (4.0, 1)]
        expr = ffmpeg.smart_x_expression(positions, [], 2.0, 200, 640)
        # source 4s on a 2x render → boundary at 2.00 output seconds
        self.assertIn("lt(t,2.00)", expr)

    def test_remap_ass_through_cuts_and_speed(self):
        src = Path("/tmp/as-test-remap.ass")
        out = Path("/tmp/as-test-remap-out.ass")
        header = ffmpeg._ass_header(720, 1280, ffmpeg._style_params("classic", 1280))
        # event at source 2.5-4.5s, written speed-scaled (rate 1 → same)
        src.write_text(
            header
            + "Dialogue: 0,0:00:02.50,0:00:04.50,Cap,,0,0,0,,HELLO\n"
            + "Dialogue: 0,0:00:05.00,0:00:06.00,Cap,,0,0,0,,WORLD\n"
        )
        keeps = [(0.0, 2.0), (3.0, 8.0)]
        ffmpeg.remap_ass_file(src, keeps, out, speed=1.0)
        text = out.read_text()
        # 2.5s starts inside the removed (2,3) silence → clamps to 2.00;
        # 4.5s → 2 + (4.5-3) = 3.50
        self.assertIn("0,0:00:02.00,0:00:03.50,", text)
        # second event 5-6s maps to 4-5s
        self.assertIn("0:00:04.00", text)
        self.assertIn("0:00:05.00", text)

    def test_invert_ranges(self):
        self.assertEqual(ffmpeg.invert_ranges([], 0, 10), [(0.0, 10.0)])
        self.assertEqual(
            ffmpeg.invert_ranges([(2, 3), (5, 6)], 0, 10),
            [(0.0, 2.0), (3.0, 5.0), (6.0, 10.0)],
        )

    def test_build_concat_with_speed(self):
        graph = ffmpeg.build_concat_filter([(0, 2), (3, 8)], speed=1.25)
        self.assertIn("setpts=PTS/1.25", graph)
        self.assertIn("atempo=1.25", graph)
        self.assertIn("concat=n=2", graph)


class CropExprTests(unittest.TestCase):
    """stepped numeric x(t): nesting, even offsets, no ow/iw."""

    def test_nesting_shape(self):
        expr = ffmpeg._stepped_x_expr([(0.0, 0), (4.0, 218), (8.0, 438)])
        self.assertEqual(expr, "if(lt(t,4.00),0,if(lt(t,8.00),218,438))")
        self.assertEqual(expr.count("if(lt(t,"), 2)

    def test_even_offsets(self):
        for expr in (
            ffmpeg.smart_x_expression([(0, 0), (2, 2), (4, 1)], [], 1.0, 202, 640),
            ffmpeg.smart_x_expression([(0, 1), (3, 0)], [], 1.1, 180, 1280),
        ):
            bare = re.sub(r"lt\(t,[\d.]+\)", "", expr)  # drop time boundaries
            for num in re.findall(r",(-?\d+)[,)]", bare):
                self.assertEqual(int(num) % 2, 0, f"odd offset {num} in {expr}")
            self.assertNotIn("ow", expr)
            self.assertNotIn("iw", expr)

    def test_single_position_constant(self):
        self.assertEqual(ffmpeg.smart_x_expression([(0, 1)], [], 1.0, 202, 640), "220")

    def test_crop_dims_even_inside_source(self):
        for src in ((640, 360), (1920, 1080), (360, 640), (1080, 1080)):
            for out in ((720, 1280), (1080, 1920), (720, 720), (1280, 720)):
                cw, ch = ffmpeg.crop_dims(*src, *out)
                self.assertLessEqual(cw, src[0])
                self.assertLessEqual(ch, src[1])
                self.assertEqual(cw % 2, 0)
                self.assertEqual(ch % 2, 0)

    def test_escape_expr(self):
        self.assertEqual(ffmpeg._escape_expr("if(lt(t,2.00),0,4)"), "if(lt(t\\,2.00)\\,0\\,4)")


class AssTests(unittest.TestCase):
    def _style_line(self, **kwargs) -> str:
        from autoshorts.transcripts import Segment

        path = Path("/tmp/as-test-caption.ass")
        ffmpeg.make_ass(
            [Segment(0, 4, "hello world this is captions")],
            0, 4, path, 720, 1280, **kwargs,
        )
        return [line for line in path.read_text().splitlines() if line.startswith("Style:")][0]

    @staticmethod
    def _field(style_line: str, index: int) -> str:
        return style_line.split(",")[index]

    def test_borderstyle_1_default(self):
        self.assertEqual(self._field(self._style_line(), 15), "1")

    def test_borderstyle_3_with_box(self):
        line = self._style_line(box=True)
        self.assertEqual(self._field(line, 15), "3")

    def test_margin_v_low(self):
        standard = int(self._field(self._style_line(), 21))
        low = int(self._field(self._style_line(pos="low"), 21))
        self.assertLess(low, standard)

    def test_autofit_shrinks_and_floors(self):
        self.assertEqual(ffmpeg.autofit_fontsize(67, 60, 720), 24)  # floor
        self.assertEqual(ffmpeg.autofit_fontsize(67, 5, 720), 67)   # no shrink
        # long words in a real clip shrink the header font to the floor
        from autoshorts.transcripts import Segment

        path = Path("/tmp/as-test-autofit.ass")
        ffmpeg.make_ass(
            [Segment(0, 4, "hello " + "x" * 60 + " world")],
            0, 4, path, 720, 1280,
        )
        line = [l for l in path.read_text().splitlines() if l.startswith("Style:")][0]
        self.assertEqual(int(self._field(line, 2)), config.CAPTION_MIN_FONT)

    def test_speed_scales_event_times(self):
        from autoshorts.transcripts import Segment

        path = Path("/tmp/as-test-speed.ass")
        ffmpeg.make_ass(
            [Segment(0, 4, "one two three four")], 0, 4, path, 720, 1280, speed=2.0
        )
        text = path.read_text()
        self.assertIn("0:00:00.00", text)
        self.assertNotIn("0:00:03.", text)  # 4s window at 2x ends at 2s

    def test_word_groups_of_four(self):
        from autoshorts.transcripts import Segment

        path = Path("/tmp/as-test-groups.ass")
        ffmpeg.make_ass(
            [Segment(0, 8, "one two three four five six seven eight nine ten")],
            0, 8, path, 720, 1280,
        )
        events = [l for l in path.read_text().splitlines() if l.startswith("Dialogue:")]
        words_per_event = [len(e.split(",,")[-1].split()) for e in events]
        self.assertTrue(all(w <= 4 for w in words_per_event), words_per_event)


class RenderOptsTests(unittest.TestCase):
    def test_bool_speed_rejected_422(self):
        with self.assertRaises(maintenance.ServiceError) as ctx:
            maintenance.render_opts_from({"speed": True})
        self.assertEqual(ctx.exception.status, 422)
        with self.assertRaises(maintenance.ServiceError):
            maintenance.render_opts_from({"speed": False})

    def test_speed_number_range(self):
        self.assertEqual(maintenance.render_opts_from({"speed": 1.5})["speed"], 1.5)
        with self.assertRaises(maintenance.ServiceError):
            maintenance.render_opts_from({"speed": "fast"})
        with self.assertRaises(maintenance.ServiceError):
            maintenance.render_opts_from({"speed": 5.0})

    def test_all_styles_and_positions(self):
        for style in config.STYLES:
            self.assertEqual(
                maintenance.render_opts_from({"style": style})["style"], style
            )
        for pos in config.CAPTION_POSITIONS:
            self.assertEqual(
                maintenance.render_opts_from({"captions_pos": pos})["captions_pos"],
                pos,
            )
        with self.assertRaises(maintenance.ServiceError):
            maintenance.render_opts_from({"style": "diagonal"})
        with self.assertRaises(maintenance.ServiceError):
            maintenance.render_opts_from({"captions_pos": "top"})

    def test_base_survives(self):
        base = {"style": "smart", "captions_pos": "low", "captions_box": True, "speed": 1.25}
        opts = maintenance.render_opts_from({}, base=base)
        self.assertEqual(opts["style"], "smart")
        self.assertEqual(opts["captions_pos"], "low")
        self.assertTrue(opts["captions_box"])
        self.assertEqual(opts["speed"], 1.25)

    def test_unknown_keys_rejected(self):
        for body in ({"oops": 1}, {"filters": "x"}):
            with self.assertRaises(maintenance.ServiceError) as ctx:
                maintenance.shorts_params_from({"count": 2, **body})
            self.assertEqual(ctx.exception.status, 422)
            self.assertEqual(ctx.exception.detail, "Unknown options")

    def test_count_bounds(self):
        for bad in (0, 13, True, 2.5, "8"):
            with self.assertRaises(maintenance.ServiceError):
                maintenance.shorts_params_from({"count": bad})
        self.assertEqual(maintenance.shorts_params_from({"count": 12})["count"], 12)

    def test_rerender_allowlist(self):
        opts = maintenance.rerender_params_from(
            {"style": "smart", "title": "New title"},
            base={"captions_pos": "low"},
        )
        self.assertEqual(opts["style"], "smart")
        with self.assertRaises(maintenance.ServiceError) as ctx:
            maintenance.rerender_params_from({"count": 3}, base={})
        self.assertEqual(ctx.exception.detail, "Unknown options")

    def test_manual_validation(self):
        params = maintenance.manual_params_from({"start": 10, "end": 30})
        self.assertEqual(params["kind"], "manual")
        for bad in (
            {"start": 10, "end": 12},                # < 5 s
            {"start": True, "end": 30},              # bool start
            {"start": 10, "end": 30, "title": "x" * 121},
        ):
            with self.assertRaises(maintenance.ServiceError) as ctx:
                maintenance.manual_params_from(bad)
            self.assertEqual(ctx.exception.status, 422)


class YtdlpVersionTests(unittest.TestCase):
    def test_version_from_module_not_cli(self):
        version = maintenance._ytdlp_version()
        self.assertRegex(version, r"^\d{4}\.\d{2}\.\d{2}")

    def test_versions_info_shape(self):
        info = maintenance.versions_info()
        self.assertIn("python", info)
        self.assertIn("ffmpeg", info)
        self.assertIn("yt_dlp", info)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        util.fresh_state(self.store)

    def test_clean_measures_bytes_before_delete(self):
        from autoshorts import config

        pre_existing = {
            p.name: p.stat().st_size
            for p in config.THUMBS_DIR.rglob("*.jpg") if p.is_file()
        }
        files = []
        for i in range(3):
            path = config.THUMBS_DIR / f"unit-test-{i}.jpg"
            path.write_bytes(b"x" * (1000 + i))
            files.append(path)
        mine = {p.name: p.stat().st_size for p in files}
        result = maintenance.clean_storage(self.store, "thumbs")
        self.assertEqual(
            result["removed"], len(mine) + len(pre_existing)
        )
        self.assertGreaterEqual(result["freed_bytes"], sum(mine.values()))
        for path in files:
            self.assertFalse(path.exists())

    def test_clean_bad_target_422(self):
        with self.assertRaises(maintenance.ServiceError) as ctx:
            maintenance.clean_storage(self.store, "everything")
        self.assertEqual(ctx.exception.status, 422)

    def test_clean_targets(self):
        self.assertEqual(maintenance.CLEAN_TARGETS, ("subs", "thumbs", "clips"))


class CancelAndEpisodeTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        util.fresh_state(self.store)

    def test_cancel_only_queued(self):
        self.store.upsert_episode(
            {"id": "ep-x", "title": "X", "url": "u", "status": "new", "clips": []}
        )
        job = self.store.create_job("ep-x", {})
        self.assertEqual(self.store.job(job["id"])["status"], "queued")
        self.assertIsNotNone(self.store.cancel_job(job["id"]))
        self.assertEqual(self.store.job(job["id"])["status"], "cancelled")
        self.assertIsNone(self.store.cancel_job(job["id"]))  # not queued anymore

    def test_reset_episode_when_idle(self):
        self.store.upsert_episode(
            {"id": "ep-y", "title": "Y", "url": "u", "status": "processing", "clips": []}
        )
        job = self.store.create_job("ep-y", {})
        self.store.cancel_job(job["id"])
        maintenance.reset_episode_if_idle(self.store, "ep-y")
        self.assertEqual(self.store.get_episode("ep-y")["status"], "new")

    def test_worker_skips_cancelled_jobs(self):
        """A cancelled job is dequeued and skipped by the worker, never run."""
        from autoshorts.pipeline import Pipeline

        self.store.upsert_episode(
            {"id": "ep-s", "title": "S", "url": "u", "status": "new", "clips": []}
        )
        pipeline = Pipeline(self.store)
        job = self.store.create_job("ep-s", {})
        self.store.cancel_job(job["id"])
        pipeline._run_job(job["id"])  # the real worker path
        self.assertEqual(self.store.job(job["id"])["status"], "cancelled")
        self.assertEqual(self.store.get_episode("ep-s")["status"], "new")

    def test_rename_rules(self):
        from autoshorts import config

        self.store.upsert_episode(
            {"id": "ep-r", "title": "R", "url": "u", "status": "new", "clips": []}
        )
        clip_id = util.make_fake_clip(self.store, config, episode_id="ep-r")
        result = maintenance.rename_clip(self.store, clip_id, {"title": "  New name  "})
        self.assertEqual(result, {"id": clip_id, "title": "New name"})
        for bad in ({}, {"title": ""}, {"title": "   "}, {"title": "x" * 121}, {"title": 5}):
            with self.assertRaises(maintenance.ServiceError) as ctx:
                maintenance.rename_clip(self.store, clip_id, bad)
            self.assertEqual(ctx.exception.status, 422)
        with self.assertRaises(maintenance.ServiceError) as ctx:
            maintenance.rename_clip(self.store, "nope", {"title": "fine"})
        self.assertEqual(ctx.exception.status, 404)

    def test_delete_episode_removes_files(self):
        from autoshorts import config

        self.store.upsert_episode(
            {"id": "ep-d", "title": "D", "url": "u", "status": "new", "clips": []}
        )
        clip_id = util.make_fake_clip(self.store, config, episode_id="ep-d")
        clip = self.store.get_clip(clip_id)
        clip_path = config.CLIPS_DIR / clip["file"]
        thumb_path = config.THUMBS_DIR / clip["thumb"]
        self.assertTrue(clip_path.exists())
        self.assertTrue(thumb_path.exists())
        result = maintenance.delete_episode(self.store, "ep-d")
        self.assertEqual(result, {"deleted": "ep-d", "clips_removed": 1})
        self.assertFalse(clip_path.exists())
        self.assertFalse(thumb_path.exists())
        self.assertIsNone(self.store.get_episode("ep-d"))

    def test_delete_unknown_404(self):
        with self.assertRaises(maintenance.ServiceError) as ctx:
            maintenance.delete_episode(self.store, "ghost")
        self.assertEqual(ctx.exception.status, 404)

    def test_delete_busy_409(self):
        self.store.upsert_episode(
            {"id": "ep-b", "title": "B", "url": "u", "status": "processing", "clips": []}
        )
        with self.assertRaises(maintenance.ServiceError) as ctx:
            maintenance.delete_episode(self.store, "ep-b")
        self.assertEqual(ctx.exception.status, 409)


class SearchUnitTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        util.fresh_state(self.store)
        for episode in (
            {"id": "demo-chhetri-223", "title": "Chhetri demo", "source": "demo",
             "status": "new", "clips": []},
        ):
            self.store.upsert_episode(episode)

    def test_hits_have_contract_shape(self):
        result = maintenance.search_transcripts(self.store, "fear")
        self.assertIn("results", result)
        for hit in result["results"]:
            self.assertEqual(
                set(hit.keys()),
                {"episode_id", "episode_title", "start", "end", "text"},
            )
        self.assertTrue(any("fear" in hit["text"].lower() for hit in result["results"]))

    def test_short_query_422(self):
        for q in ("", "a", None, 5):
            with self.assertRaises(maintenance.ServiceError) as ctx:
                maintenance.search_transcripts(self.store, q)
            self.assertEqual(ctx.exception.status, 422)

    def test_no_hits_empty(self):
        result = maintenance.search_transcripts(self.store, "zzzunfindable")
        self.assertEqual(result["results"], [])


class CsvUnitTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        util.fresh_state(self.store)
        self.store.upsert_episode(
            {"id": "demo-chhetri-223", "title": "Chhetri demo", "source": "demo",
             "status": "new", "clips": []}
        )

    def test_header_and_rows(self):
        filename, text = maintenance.export_csv(self.store, "demo-chhetri-223", 3, "viral")
        self.assertEqual(filename, "qyro-demo-chhetri-223-moments.csv")
        lines = text.strip().splitlines()
        self.assertEqual(lines[0], "start,end,duration,title,score,reasons")
        self.assertGreater(len(lines) - 1, 0)
        self.assertLessEqual(len(lines) - 1, 3)
        first = lines[1].split(",")
        self.assertEqual(len(first), 6)

    def test_validation(self):
        with self.assertRaises(maintenance.ServiceError) as ctx:
            maintenance.export_csv(self.store, "demo-chhetri-223", 0, "viral")
        self.assertEqual(ctx.exception.status, 422)
        with self.assertRaises(maintenance.ServiceError):
            maintenance.export_csv(self.store, "demo-chhetri-223", 13, "viral")
        with self.assertRaises(maintenance.ServiceError):
            maintenance.export_csv(self.store, "demo-chhetri-223", 3, "drama")
        with self.assertRaises(maintenance.ServiceError) as ctx:
            maintenance.export_csv(self.store, "ghost", 3, "viral")
        self.assertEqual(ctx.exception.status, 404)


class PackTests(unittest.TestCase):
    def test_pack_contract(self):
        pack = titles.generate_pack(
            "The biggest mistake of my career",
            "I lost 40 percent of my savings. Why did nobody tell me the truth?",
            "Sunil Chhetri On Football | FO 223",
            "viral",
            42.0,
        )
        self.assertEqual(len(pack["titles"]), 3)
        self.assertLessEqual(len(pack["hashtags"]), 12)
        self.assertIn("#shorts", [tag.lower() for tag in pack["hashtags"]])
        self.assertIn("🎙", pack["description"])
        self.assertIn("⏱", pack["description"])


class SettingsUnitTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        util.fresh_state(self.store)

    def test_persist_playlist_and_autopilot(self):
        update = maintenance.settings_update_from(
            {"playlist_url": "https://example.com/playlist?list=abc", "autopilot": True}
        )
        self.store.update_settings(**update)
        settings = self.store.settings()
        self.assertTrue(settings["autopilot"])
        self.assertEqual(settings["playlist_url"], "https://example.com/playlist?list=abc")

    def test_bad_settings_422(self):
        for bad in ({"autopilot": "yes"}, {"playlist_url": 5}, {"playlist_url": "short"}, {}):
            with self.assertRaises(maintenance.ServiceError) as ctx:
                maintenance.settings_update_from(bad)
            self.assertEqual(ctx.exception.status, 422)


if __name__ == "__main__":
    unittest.main()
