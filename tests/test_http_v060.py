"""v0.6.0 HTTP surface + one real tracked render.

The render test is the point of this file: it builds a landscape video with a
subject that walks right across the frame, renders a 9:16 short with subject
tracking on and off, and measures where the subject actually lands in the
output. Tracking has to keep the person in frame; the static centre crop must
lose them. That is the whole feature, tested end to end through ffmpeg.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
import unittest
from pathlib import Path

from autoshorts import config, maintenance, pipeline as pipeline_mod, vision
from autoshorts.server import app, pipeline, store
from autoshorts.store import Store
from fastapi.testclient import TestClient

from . import util

HAVE_FFMPEG = Path(config.FFMPEG_BIN).is_file() or shutil.which("ffmpeg") is not None


def make_walking_subject(path: Path, seconds: int = 12) -> bool:
    """A 1280x720 clip whose skin-toned subject sweeps left -> right -> left."""
    path.parent.mkdir(parents=True, exist_ok=True)
    sweep = "80+900*(0.5+0.5*sin(2*PI*t/10))"
    try:
        subprocess.run(
            [config.FFMPEG_BIN, "-v", "error", "-y",
             "-f", "lavfi", "-i", f"color=c=0x223344:s=1280x720:d={seconds}:r=25",
             "-f", "lavfi", "-i", f"color=c=0xE0A080:s=170x250:d={seconds}:r=25",
             "-f", "lavfi", "-i", f"color=c=0x884422:s=88x88:d={seconds}:r=25",
             "-f", "lavfi", "-i", f"sine=frequency=320:duration={seconds}",
             "-filter_complex",
             f"[0:v][1:v]overlay=x='{sweep}':y=300:format=auto[b];"
             f"[b][2:v]overlay=x='{sweep}+40':y=205:format=auto[v]",
             "-map", "[v]", "-map", "3:a",
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-shortest", str(path)],
            check=True, capture_output=True, timeout=300,
        )
        return path.is_file()
    except Exception:
        return False


def subject_centre(clip: Path, at: float, width: int = 720) -> float | None:
    """Horizontal centre of the skin-toned subject in a rendered frame."""
    try:
        raw = subprocess.run(
            [config.FFMPEG_BIN, "-v", "error", "-ss", f"{at:.3f}",
             "-i", str(clip), "-frames:v", "1",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            capture_output=True, timeout=120,
        ).stdout
    except Exception:
        return None
    height = len(raw) // (width * 3)
    if height <= 0:
        return None
    columns = []
    for x in range(width):
        hits = 0
        for y in range(height // 8, height - height // 8, 4):
            r, g, b = raw[(y * width + x) * 3:(y * width + x) * 3 + 3]
            cb = 128 - 0.168736 * r - 0.331264 * g + 0.5 * b
            cr = 128 + 0.5 * r - 0.418688 * g - 0.081312 * b
            if 77 <= cb <= 127 and 133 <= cr <= 173:
                hits += 1
        columns.append(hits)
    peak = max(columns) if columns else 0
    if peak < 2:
        return None
    hot = [x for x, value in enumerate(columns) if value >= peak * 0.4]
    return (hot[0] + hot[-1]) / 2.0 / width


# --------------------------------------------------------------------------
# API surface
# --------------------------------------------------------------------------
class V060SurfaceTests(unittest.TestCase):
    """Option validation and the health catalog. Jobs are stubbed out so no
    rendering happens here — the real render is :class:`TrackedRenderTests`."""

    def setUp(self):
        util.fresh_state(store)
        self.original_run = pipeline._run_job
        pipeline._run_job = lambda job_id: store.update_job(
            job_id, status="done", progress=1.0, step="done"
        )
        self.client = TestClient(app, raise_server_exceptions=False)
        self.client.post("/api/demo/load")
        util.make_fake_clip(store, config)

    def tearDown(self):
        pipeline._run_job = self.original_run
        util.fresh_state(store)

    def clip_id(self) -> str:
        return store.clips()[0]["id"]

    def test_health_exposes_the_option_catalog(self):
        body = self.client.get("/api/health").json()
        options = body["options"]
        self.assertEqual(
            [item["id"] for item in options["anims"]], list(config.CAPTION_ANIMS)
        )
        self.assertEqual(
            [item["id"] for item in options["transitions"]],
            list(config.TRANSITIONS),
        )
        self.assertEqual(
            [item["id"] for item in options["fonts"]], list(config.CAPTION_FONTS)
        )
        self.assertEqual(
            [item["id"] for item in options["track_modes"]],
            list(config.TRACK_MODES),
        )
        self.assertEqual(
            [item["id"] for item in options["languages"]], list(config.LANGUAGES)
        )
        self.assertIsInstance(options["devanagari_ready"], bool)
        self.assertIsInstance(options["face_model_ready"], bool)
        self.assertEqual(options["fonts_dir"], str(config.FONTS_DIR))

    def test_state_defaults_carry_the_new_options(self):
        defaults = self.client.get("/api/state").json()["defaults"]
        self.assertEqual(defaults["captions_anim"], config.DEFAULT_CAPTION_ANIM)
        self.assertEqual(defaults["transition"], config.DEFAULT_TRANSITION)
        self.assertEqual(defaults["track_mode"], config.DEFAULT_TRACK_MODE)
        self.assertEqual(defaults["track_zoom"], config.DEFAULT_TRACK_ZOOM)
        self.assertEqual(defaults["language"], config.DEFAULT_LANGUAGE)
        self.assertIs(defaults["quality_gate"], True)

    def test_good_options_are_accepted_on_both_routes(self):
        body = {"count": 1, "captions_anim": "karaoke", "transition": "slide",
                "track_mode": "vision", "track_zoom": "tight",
                "language": "hinglish", "captions_font": "rounded",
                "quality_gate": False}
        short = self.client.post("/api/episodes/demo-chhetri-223/shorts",
                                 json=body)
        self.assertEqual(short.status_code, 200, short.text)
        params = store.job(short.json()["job_id"])["params"]
        # the stubbed worker never settles the episode; free it for the re-render
        store.update_episode("demo-chhetri-223", status="ready")
        self.assertEqual(params["captions_anim"], "karaoke")
        self.assertEqual(params["transition"], "slide")
        self.assertEqual(params["track_mode"], "vision")
        self.assertEqual(params["language"], "hinglish")
        self.assertIs(params["quality_gate"], False)

        rerender = self.client.post(f"/api/clips/{self.clip_id()}/rerender",
                                    json={"captions_anim": "bounce",
                                          "track_zoom": "wide"})
        self.assertEqual(rerender.status_code, 200, rerender.text)
        options = rerender.json()["options"]
        self.assertEqual(options["captions_anim"], "bounce")
        self.assertEqual(options["track_zoom"], "wide")

    def test_bad_options_are_422_on_both_routes(self):
        for body in (
            {"captions_font": "comic-sans-forever"},
            {"captions_anim": "spin"},
            {"transition": "wipe"},
            {"track_mode": "magic"},
            {"track_zoom": "enormous"},
            {"language": "klingon"},
            {"quality_gate": "yes"},
        ):
            with self.subTest(body=body):
                payload = dict(body)
                payload.setdefault("count", 1)
                short = self.client.post("/api/episodes/demo-chhetri-223/shorts",
                                         json=payload)
                self.assertEqual(short.status_code, 422, short.text)
                self.assertIn("detail", short.json())
                rerender = self.client.post(f"/api/clips/{self.clip_id()}/rerender",
                                            json=body)
                self.assertEqual(rerender.status_code, 422, rerender.text)

    def test_transcript_response_stays_lean(self):
        """Word timings live on the Segment but must not bloat the API."""
        config.SUBS_DIR.mkdir(parents=True, exist_ok=True)
        (config.SUBS_DIR / "lean-ep.segments.json").write_text(
            json.dumps([{"start": 0.0, "end": 1.0, "text": "hi",
                         "words": [["hi", 0.0, 1.0]]}]),
            encoding="utf-8",
        )
        store.upsert_episode({"id": "lean-ep", "title": "Lean", "url": "",
                              "duration": 10.0, "source": "manual",
                              "status": "ready", "clips": [],
                              "added_at": time.time()})
        body = self.client.get("/api/episodes/lean-ep/transcript").json()
        self.assertEqual(set(body["segments"][0]), {"start", "end", "text"})

    def test_font_install_endpoint_reports_the_real_state(self):
        """Hindi captions need a resolvable font; the button must tell the
        truth either way rather than claiming success."""
        body = self.client.post("/api/fonts/install", json={}).json()
        self.assertIn("ok", body)
        self.assertIn("devanagari_ready", body)
        self.assertIsInstance(body["installed"], list)
        self.assertTrue(body["detail"])
        # ok must agree with what the catalog will now report
        health = self.client.get("/api/health").json()["options"]
        self.assertEqual(body["ok"], health["devanagari_ready"])


    def test_word_timings_survive_the_transcript_cache(self):
        from autoshorts import youtube

        cache = config.SUBS_DIR / "roundtrip.segments.json"
        cache.write_text(json.dumps([
            {"start": 0.0, "end": 2.0, "text": "a b",
             "words": [["a", 0.0, 0.8], ["b", 0.8, 2.0]]},
            {"start": 2.0, "end": 3.0, "text": "legacy row"},
        ]), encoding="utf-8")
        segments = youtube.load_cached_transcript("roundtrip")
        self.assertEqual(segments[0].words, [("a", 0.0, 0.8), ("b", 0.8, 2.0)])
        self.assertEqual(segments[1].words, [])   # pre-v0.6.0 cache row


# --------------------------------------------------------------------------
# The real render
# --------------------------------------------------------------------------
@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg is required for the render test")
class TrackedRenderTests(unittest.TestCase):
    """Track the person, caption in Hindi, transition the edges — for real."""

    @classmethod
    def setUpClass(cls):
        cls.data = Path(config.DATA_DIR) / "e2e060"
        for sub in ("media", "clips", "thumbs", "subs"):
            (cls.data / sub).mkdir(parents=True, exist_ok=True)
        cls.media = cls.data / "media" / "e2e060.mp4"
        cls.ok = make_walking_subject(cls.media, seconds=12)

    def _render(self, tag: str, params: dict) -> Path:
        """Run one manual job through the real pipeline and return the file."""
        local = Store(self.data / f"state-{tag}.json")
        util.fresh_state(local)
        local.upsert_episode({
            "id": f"e2e060-{tag}", "title": "Tracked", "url": "",
            "duration": 12.0, "source": "manual", "status": "new",
            "clips": [], "added_at": time.time(),
        })
        config.SUBS_DIR.mkdir(parents=True, exist_ok=True)
        (config.SUBS_DIR / f"e2e060-{tag}.segments.json").write_text(
            json.dumps([
                {"start": 1.0, "end": 5.0, "text": "नमस्ते दोस्तों स्वागत है",
                 "words": [["नमस्ते", 1.0, 2.2], ["दोस्तों", 2.2, 3.2],
                           ["स्वागत", 3.2, 4.2], ["है", 4.2, 5.0]]},
                {"start": 5.0, "end": 9.0,
                 "text": "this short follows the speaker",
                 "words": [["this", 5.0, 5.6], ["short", 5.6, 6.2],
                           ["follows", 6.2, 7.0], ["the", 7.0, 7.4],
                           ["speaker", 7.4, 9.0]]},
            ]),
            encoding="utf-8",
        )
        original = pipeline_mod.Pipeline._prepare_media
        pipeline_mod.Pipeline._prepare_media = staticmethod(lambda ep: self.media)
        worker = pipeline_mod.Pipeline(local)
        try:
            job = worker.start_job(f"e2e060-{tag}", {
                "kind": "manual", "start": 1.0, "end": 10.0, "title": tag,
                "style": "smart", "quality": "fast", "format": "vertical",
                "captions": "classic", **params,
            })
            deadline = time.time() + 420
            status = local.job(job["id"])
            while time.time() < deadline:
                status = local.job(job["id"])
                if status["status"] in ("done", "error", "cancelled"):
                    break
                time.sleep(0.4)
            self.assertEqual(status["status"], "done", status.get("error"))
            clips = local.clips()
            self.assertEqual(len(clips), 1)
            self.clip = clips[0]
            return config.CLIPS_DIR / clips[0]["file"]
        finally:
            pipeline_mod.Pipeline._prepare_media = original

    def test_tracking_keeps_the_subject_in_frame(self):
        if not self.ok:
            self.skipTest("could not build the test video")
        tracked = self._render("track", {
            "track_mode": "vision", "track_zoom": "wide",
            "captions_anim": "fade", "transition": "fade",
            "captions_font": "auto", "language": "auto",
        })
        self.assertTrue(tracked.is_file())
        self.assertEqual(
            (self.clip["width"], self.clip["height"]), (720, 1280)
        )
        self.assertEqual(self.clip["track_mode"], "vision")
        self.assertEqual(self.clip["transition"], "fade")
        self.assertEqual(self.clip["captions_anim"], "fade")

        centres = [subject_centre(tracked, t) for t in (1.0, 3.0, 5.0, 7.0)]
        centres = [c for c in centres if c is not None]
        self.assertGreaterEqual(len(centres), 3, "subject must be visible")
        # a tracked subject stays near the middle of the 9:16 frame
        self.assertTrue(
            all(0.18 <= c <= 0.82 for c in centres),
            f"subject drifted out of frame: {centres}",
        )

        # the control: the same window with tracking off. A fixed centre crop
        # cannot follow a walking subject, so its frames must sit further from
        # the middle of the 9:16 output than the tracked ones do.
        static = self._render("static", {
            "track_mode": "off", "captions_anim": "none",
            "transition": "none", "captions_font": "auto", "language": "en",
        })
        same_times = [1.0, 3.0, 5.0, 7.0]
        static_centres = [
            c for c in (subject_centre(static, t) for t in same_times)
            if c is not None
        ]
        self.assertTrue(static_centres, "the control render produced no frames")
        off_centre = lambda values: sum(abs(c - 0.5) for c in values) / len(values)
        self.assertLess(
            off_centre(centres), off_centre(static_centres),
            f"tracking did not improve framing: tracked={centres} "
            f"static={static_centres}",
        )
        # and the static crop really does push the subject to the frame edge
        self.assertTrue(
            any(c < 0.15 or c > 0.85 for c in static_centres),
            f"the walking subject never left the middle: {static_centres}",
        )

    def test_hindi_captions_and_animation_are_burned_in(self):
        if not self.ok:
            self.skipTest("could not build the test video")
        self._render("hindi", {
            "track_mode": "vision", "captions_anim": "karaoke",
            "transition": "slide", "captions_font": "devanagari",
            "language": "hi",
        })
        ass = config.SUBS_DIR / f"{self.clip['id']}.ass"
        self.assertTrue(ass.is_file())
        text = ass.read_text(encoding="utf-8")
        events = [l for l in text.splitlines() if l.startswith("Dialogue:")]
        self.assertTrue(events, "the transcript must produce caption events")
        # karaoke timing is in the events
        self.assertTrue(any("\\kf" in line for line in events))
        # the Hindi line survived verbatim (no ALL-CAPS mangling)
        self.assertIn("नमस्ते", text)
        # every override block is balanced, so libass cannot choke on it
        for line in events:
            body = line.split(",0,0,0,,", 1)[1]
            self.assertEqual(body.count("{"), body.count("}"))

        info = maintenance.clip_probe(
            Store(self.data / "state-hindi.json"), self.clip["id"]
        )
        self.assertEqual(info["orientation"], "vertical")
        self.assertEqual(info["video_codec"], "h264")
        self.assertGreater(info["duration"], 4.0)

    def test_tracking_analysis_is_cached(self):
        if not self.ok:
            self.skipTest("could not build the test video")
        # a window no other test uses, so the cache entry is definitely new
        window = (1.5, 9.5)
        first = vision.track_window(self.media, *window, 1280, 720,
                                    720, 1280, 0.316, 1.0, mode="vision",
                                    use_cache=False)
        self.assertIsNotNone(first)
        before = len(list(config.TRACK_CACHE_DIR.glob("track-*.json")))
        vision.track_window(self.media, *window, 1280, 720,
                            720, 1280, 0.316, 1.0, mode="vision")
        after = len(list(config.TRACK_CACHE_DIR.glob("track-*.json")))
        self.assertEqual(after, before + 1, "the analysis must be cached")
        started = time.time()
        second = vision.track_window(self.media, *window, 1280, 720,
                                     720, 1280, 0.316, 1.0, mode="vision")
        elapsed = time.time() - started
        # the cache stores rounded coordinates, so compare at cache precision
        rounded = lambda plan: [(round(t, 4), round(x, 4), round(y, 4))
                                for t, x, y in plan.keyframes]
        self.assertEqual(rounded(second), rounded(first))
        self.assertEqual(second.backend, first.backend)
        self.assertLess(elapsed, 0.5, "a cached plan must not re-analyse")


if __name__ == "__main__":
    unittest.main()
