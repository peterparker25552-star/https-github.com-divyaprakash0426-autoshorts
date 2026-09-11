"""Qyro v0.5.0 HTTP surface: every new endpoint on both servers, plus the
one real 1440p end-to-end render (smart framing + logo remover + brand +
beat sync) verified by probing the file that came out of ffmpeg.

Routes are exercised through the FastAPI TestClient (in-process, fast) and
against a real stdlib server (the Termux path), because v0.5.0 added parsing
rules (a bigger body cap for ``POST /api/audio``) that only the real server
proves.
"""
from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB = REPO_ROOT / "web"

from autoshorts import (__version__ as autoshorts_version, audioswap, config,
                        maintenance, pipeline as pipeline_mod)
from autoshorts.server import app, pipeline, store
from autoshorts.store import Store
from fastapi.testclient import TestClient

from . import util

try:
    import imageio_ffmpeg  # noqa: F401
    HAVE_FFMPEG = shutil.which(config.FFMPEG_BIN) is not None or Path(
        config.FFMPEG_BIN
    ).is_file()
except Exception:
    HAVE_FFMPEG = shutil.which("ffmpeg") is not None


def make_toy_video(path: Path, seconds: int = 3, size: str = "320x480") -> bool:
    """A tiny real MP4 (video + audio) so probers and extractors have work."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [config.FFMPEG_BIN, "-v", "error", "-y",
             "-f", "lavfi", "-i", f"testsrc=size={size}:rate=12:duration={seconds}",
             "-f", "lavfi", "-i", f"sine=frequency=320:duration={seconds}",
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-shortest", str(path)],
            check=True, capture_output=True, timeout=120,
        )
        return path.is_file()
    except Exception:
        return False


def make_toy_mp3(path: Path, seconds: int = 2) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [config.FFMPEG_BIN, "-v", "error", "-y", "-f", "lavfi",
             "-i", f"sine=frequency=180:duration={seconds}",
             "-c:a", "libmp3lame", str(path)],
            check=True, capture_output=True, timeout=120,
        )
        return path.is_file()
    except Exception:
        return False


class V050Fixture(unittest.TestCase):
    """Fresh store + fake worker (no real render) — same rules as v0.4.0.

    The fake worker finishes a job immediately, which is exactly what these
    tests need: they assert on what the *API* accepted, normalised and stored,
    not on ffmpeg output (that is EndToEndRenderTests below).
    """

    @classmethod
    def setUpClass(cls):
        cls.toy_dir = Path(config.DATA_DIR) / "v050"
        cls.toy_dir.mkdir(parents=True, exist_ok=True)
        cls.video = cls.toy_dir / "toy.mp4"
        cls.mp3 = cls.toy_dir / "toy.mp3"
        cls.have_video = cls.video.is_file() or make_toy_video(cls.video)
        cls.have_mp3 = cls.mp3.is_file() or make_toy_mp3(cls.mp3)

    def setUp(self):
        util.fresh_state(store)
        self.original_run = pipeline._run_job
        pipeline._run_job = lambda job_id: store.update_job(
            job_id, status="done", progress=1.0, step="done"
        )
        self.client = TestClient(app, raise_server_exceptions=False)
        self.client.post("/api/demo/load")

    def tearDown(self):
        pipeline._run_job = self.original_run
        util.fresh_state(store)

    # helpers ------------------------------------------------------------
    def one_clip(self, with_files=True):
        return util.make_fake_clip(store, config, with_files=with_files)

    def upload_bed(self, name="toy.mp3") -> str:
        raw = self.mp3.read_bytes()
        resp = self.client.post("/api/audio",
                                json={"name": name, "data_b64": base64.b64encode(raw).decode()})
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["track_id"]


def clear_tracks() -> None:
    """The audio library lives on disk, so tests must reset it themselves."""
    config.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    for path in config.AUDIO_DIR.glob("track-*"):
        path.unlink(missing_ok=True)
    (config.AUDIO_DIR / "tracks.json").unlink(missing_ok=True)


class AudioTrackTests(V050Fixture):
    def setUp(self):
        super().setUp()
        clear_tracks()

    def tearDown(self):
        super().tearDown()
        clear_tracks()

    def test_upload_list_use_delete(self):
        if not self.have_mp3:
            self.skipTest("no ffmpeg")
        track_id = self.upload_bed()
        listed = self.client.get("/api/audio").json()["tracks"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["id"], track_id)
        self.assertEqual(listed[0]["name"], "toy.mp3")
        self.assertGreater(listed[0]["duration"], 0.5)

        stream = self.client.get(f"/api/audio/{track_id}/file")
        self.assertEqual(stream.status_code, 200)
        self.assertIn("audio", stream.headers["content-type"])

        state = self.client.get("/api/state").json()
        self.assertEqual(state["audio_tracks"][0]["id"], track_id)

        deleted = self.client.delete(f"/api/audio/{track_id}")
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json()["deleted"], track_id)
        self.assertEqual(self.client.get("/api/audio").json()["tracks"], [])

    def test_upload_validation(self):
        bad = [
            {"name": "x.mp3", "data_b64": "not base64 at all!!"},
            {"name": "x.mp3", "data_b64": ""},
            {"name": "x.mp3"},
            {"data_b64": base64.b64encode(b"tiny").decode()},
            {"name": "x.mp3", "data_b64": base64.b64encode(b"tiny").decode(),
             "surprise": 1},
        ]
        for body in bad:
            with self.subTest(body=body):
                resp = self.client.post("/api/audio", json=body)
                self.assertEqual(resp.status_code, 422, resp.text)
                self.assertIn("detail", resp.json())

    def test_upload_rejects_oversize(self):
        # 1 MB of audio with the cap dropped to 1 KB proves the size guard
        original = config.MAX_AUDIO_BYTES
        config.MAX_AUDIO_BYTES = 1024
        try:
            resp = self.client.post("/api/audio", json={
                "name": "big.mp3",
                "data_b64": base64.b64encode(b"\x00" * (200 * 1024)).decode(),
            })
            self.assertEqual(resp.status_code, 422)
            self.assertIn("too large", resp.json()["detail"])
        finally:
            config.MAX_AUDIO_BYTES = original

    def test_wrong_methods_and_unknown_track(self):
        self.assertIn(self.client.get("/api/titles").status_code, (405, 422, 400))
        self.assertEqual(self.client.get("/api/audio/does-not-exist/file").status_code,
                         404)
        self.assertEqual(self.client.delete("/api/audio/does-not-exist").status_code, 404)

    def test_render_rejects_unknown_track_id(self):
        resp = self.client.post(
            "/api/episodes/demo-chhetri-223/shorts",
            json={"count": 1, "audio_track": "ghost"},
        )
        self.assertEqual(resp.status_code, 422)
        self.assertIn("audio_track", resp.json()["detail"])

    def test_render_accepts_a_real_track(self):
        if not self.have_mp3:
            self.skipTest("no ffmpeg")
        track_id = self.upload_bed()
        resp = self.client.post(
            "/api/episodes/demo-chhetri-223/shorts",
            json={"count": 1, "audio_track": track_id, "audio_mix": "duck"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        job_id = resp.json()["job_id"]
        job = store.job(job_id)
        self.assertEqual(job["params"]["audio_track"], track_id)
        self.assertEqual(job["params"]["audio_mix"], "duck")


class NewResourceTests(V050Fixture):
    def test_beats_endpoint(self):
        resp = self.client.get("/api/episodes/demo-chhetri-223/beats")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        for key in ("episode_id", "beats", "count", "duration", "cached", "reason"):
            self.assertIn(key, body)
        self.assertEqual(body["episode_id"], "demo-chhetri-223")
        self.assertIsInstance(body["beats"], list)
        # an unknown episode keeps the {"detail": ...} shape
        missing = self.client.get("/api/episodes/nope/beats")
        self.assertEqual(missing.status_code, 404)
        self.assertIn("detail", missing.json())

    def test_probe_endpoint_reports_missing_files_politely(self):
        clip_id = self.one_clip(with_files=False)
        body = self.client.get(f"/api/clips/{clip_id}/probe").json()
        self.assertTrue(body["missing"])
        self.assertIsNone(body["width"])
        self.assertEqual(body["clip_id"], clip_id)
        self.assertIn("stored", body)
        self.assertEqual(
            self.client.get("/api/clips/ghost/probe").status_code, 404
        )

    def test_probe_endpoint_on_a_real_file(self):
        if not self.have_video:
            self.skipTest("no ffmpeg")
        clip_id = self.one_clip(with_files=False)
        shutil.copyfile(self.video, config.CLIPS_DIR / f"{clip_id}.mp4")
        info = self.client.get(f"/api/clips/{clip_id}/probe").json()
        self.assertFalse(info["missing"])
        self.assertEqual((info["width"], info["height"]), (320, 480))
        self.assertEqual(info["orientation"], "vertical")
        self.assertGreater(info["size_bytes"], 1000)

    def test_thumb_candidates_and_pick(self):
        if not self.have_video:
            self.skipTest("no ffmpeg")
        clip_id = self.one_clip(with_files=False)
        shutil.copyfile(self.video, config.CLIPS_DIR / f"{clip_id}.mp4")
        body = self.client.get(f"/api/clips/{clip_id}/thumb-candidates?n=4").json()
        self.assertEqual(body["count"], 4)
        self.assertEqual(len(body["candidates"]), 4)
        urls = [c["url"] for c in body["candidates"]]
        self.assertTrue(all(u.startswith(f"/api/clips/{clip_id}/thumb?index=")
                            for u in urls))
        served = self.client.get(urls[1])
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served.headers["content-type"], "image/jpeg")

        picked = self.client.post(f"/api/clips/{clip_id}/thumb-pick",
                                  json={"index": 1})
        self.assertEqual(picked.status_code, 200, picked.text)
        self.assertEqual(store.get_clip(clip_id)["thumb"], picked.json()["thumb"])
        # the candidates are cleaned up after a pick
        self.assertEqual(
            list(config.THUMBS_DIR.glob(f"{clip_id}-cand-*.jpg")), []
        )

    def test_thumb_candidate_validation(self):
        clip_id = self.one_clip(with_files=False)
        self.assertEqual(
            self.client.get(f"/api/clips/{clip_id}/thumb-candidates?n=99").status_code,
            422,
        )
        self.assertEqual(
            self.client.get(f"/api/clips/{clip_id}/thumb-candidates?n=two").status_code,
            422,
        )
        self.assertEqual(
            self.client.get(f"/api/clips/{clip_id}/thumb-candidates").status_code, 410,
            "a fake clip has no real video on disk"
        )
        self.assertEqual(
            self.client.post(f"/api/clips/{clip_id}/thumb-pick",
                             json={"index": "one"}).status_code, 422,
        )
        self.assertEqual(
            self.client.post(f"/api/clips/{clip_id}/thumb-pick",
                             json={"index": 3}).status_code, 404,
        )
        self.assertEqual(
            self.client.get("/api/clips/ghost/thumb-candidates").status_code, 404
        )

    def test_titles_lab_endpoint(self):
        resp = self.client.post(
            "/api/titles",
            json={"text": "the body gives up before the mind does",
                  "profile": "viral"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(len(body["titles"]), config.TITLE_VARIATIONS)
        self.assertEqual(body["engine"], "offline")
        self.assertTrue(body["hashtags"][0].startswith("#shorts"))
        for bad in (
            {"text": ""},
            {"text": 5},
            {"text": "ok", "profile": "gossip"},
            {"text": "ok", "count": 2},
            {"text": "ok", "extra": 1},
        ):
            with self.subTest(bad=bad):
                self.assertEqual(
                    self.client.post("/api/titles", json=bad).status_code, 422
                )

    def test_audio_extract_endpoints(self):
        if not self.have_video:
            self.skipTest("no ffmpeg")
        clip_id = self.one_clip(with_files=False)
        shutil.copyfile(self.video, config.CLIPS_DIR / f"{clip_id}.mp4")
        resp = self.client.get(f"/api/clips/{clip_id}/audio.mp3")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("audio/mpeg", resp.headers["content-type"])
        self.assertIn("attachment", resp.headers["content-disposition"])
        self.assertGreater(len(resp.content), 1000)
        self.assertEqual(self.client.get("/api/clips/ghost/audio.mp3").status_code, 404)


class RenderOptionSurfaceTests(V050Fixture):
    def test_new_options_survive_a_job_and_a_rerender(self):
        payload = {
            "count": 1, "quality": "1440p", "style": "smart",
            "captions_brand": "qyro-neon", "sync_beats": True,
            "logo_box": {"preset": "topleft", "size": "L", "feather": 6},
            "silence": True, "silence_noise": -50, "silence_min": 0.9,
        }
        resp = self.client.post("/api/episodes/demo-chhetri-223/shorts", json=payload)
        self.assertEqual(resp.status_code, 200, resp.text)
        store.update_episode("demo-chhetri-223", status="done")
        params = store.job(resp.json()["job_id"])["params"]
        self.assertEqual(params["quality"], "1440p")
        self.assertEqual(params["captions_brand"], "qyro-neon")
        self.assertTrue(params["sync_beats"])
        self.assertEqual(params["logo_box"]["preset"], "topleft")
        self.assertEqual(params["silence_noise"], -50.0)

        clip_id = self.one_clip()
        rerender = self.client.post(
            f"/api/clips/{clip_id}/rerender",
            json={"quality": "1440p", "captions_brand": "qyro-pop",
                  "logo_box": {"preset": "custom", "x": 0.1, "y": 0.1,
                               "w": 0.3, "h": 0.1}},
        )
        self.assertEqual(rerender.status_code, 200, rerender.text)
        options = rerender.json()["options"]
        self.assertEqual(options["quality"], "1440p")
        self.assertEqual(options["captions_brand"], "qyro-pop")
        self.assertEqual(options["logo_box"]["preset"], "custom")

    def test_bad_new_options_are_422_on_both_routes(self):
        for body in (
            {"quality": "4k"},
            {"captions_brand": "qyro-cool"},
            {"logo_box": {"preset": "centre"}},
            {"logo_box": {"preset": "custom", "x": 5}},
            {"audio_mix": "blend"},
            {"sync_beats": "sure"},
            {"silence_noise": 0},
            {"silence_min": 0},
        ):
            with self.subTest(body=body):
                payload = dict(body)
                payload.setdefault("count", 1)
                short = self.client.post("/api/episodes/demo-chhetri-223/shorts",
                                         json=payload)
                self.assertEqual(short.status_code, 422, short.text)
                self.assertIn("detail", short.json())
                clip_id = self.one_clip()
                rerender = self.client.post(f"/api/clips/{clip_id}/rerender",
                                            json=body)
                self.assertEqual(rerender.status_code, 422, rerender.text)

    def test_manual_clip_accepts_the_new_options(self):
        resp = self.client.post(
            "/api/episodes/demo-chhetri-223/manual",
            json={"start": 5, "end": 25, "title": "Branded",
                  "quality": "1440p", "captions_brand": "qyro-minimal",
                  "sync_beats": True},
        )
        self.assertEqual(resp.status_code, 200, resp.text)


class SettingsMaskingTests(V050Fixture):
    def test_keys_are_write_only(self):
        state = self.client.get("/api/state").json()
        self.assertNotIn("gemini_key", state["settings"])
        self.assertFalse(state["settings"]["gemini_key_set"])
        self.assertEqual(state["engine"]["provider"], "offline")
        self.assertFalse(state["engine"]["active"])

        resp = self.client.post("/api/settings", json={
            "ai_provider": "gemini", "gemini_key": "AIzaSUPERSECRET123",
            "ai_model": "gemini-2.0-flash",
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        dumped = json.dumps(resp.json())
        self.assertNotIn("AIzaSUPERSECRET123", dumped)
        self.assertTrue(resp.json()["settings"]["gemini_key_set"])

        state = self.client.get("/api/state").json()
        self.assertNotIn("AIzaSUPERSECRET123", json.dumps(state))
        self.assertEqual(state["engine"]["provider"], "gemini")
        self.assertEqual(state["engine"]["model"], "gemini-2.0-flash")
        self.assertTrue(state["engine"]["active"])
        # persisted where it belongs
        self.assertEqual(store.settings()["gemini_key"], "AIzaSUPERSECRET123")

        # an empty string clears it, and no response ever shows the value
        self.client.post("/api/settings", json={"gemini_key": ""})
        after = self.client.get("/api/state").json()
        self.assertFalse(after["settings"]["gemini_key_set"])
        self.assertEqual(after["engine"]["provider"], "gemini")  # choice survives

    def test_engine_validation(self):
        for body in (
            {"ai_provider": "openai"},
            {"ai_model": 5},
            {"ai_base_url": "ftp://nope"},
            {"gemini_key": 12},
            {"gemini_key": "x" * 500},
        ):
            with self.subTest(body=body):
                resp = self.client.post("/api/settings", json=body)
                self.assertEqual(resp.status_code, 422, resp.text)

    def test_health_reports_the_brand(self):
        body = self.client.get("/api/health").json()
        self.assertEqual(body["app"], "Qyro")
        self.assertEqual(body["version"], autoshorts_version)
        self.assertIn("engine", body)
        self.assertIn("llm_available", body)


class PolishFallbackTests(V050Fixture):
    def setUp(self):
        super().setUp()
        self.clip_id = util.make_fake_clip(store, config, with_files=False)

    def test_polish_without_any_provider_is_503(self):
        if config.LLM_API_KEY:
            self.skipTest("LLM configured in env")
        resp = self.client.post(f"/api/clips/{self.clip_id}/polish")
        self.assertEqual(resp.status_code, 503)
        self.assertIn("No LLM", resp.json()["detail"])

    def test_polish_keeps_the_pack_when_the_provider_fails(self):
        from autoshorts import engine

        self.client.post("/api/settings",
                         json={"ai_provider": "groq", "groq_key": "gsk_bad"})
        original = engine._post_json
        engine._post_json = lambda *a, **k: {"__error__": "HTTP 401 bad key"}
        try:
            resp = self.client.post(f"/api/clips/{self.clip_id}/polish")
        finally:
            engine._post_json = original
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["engine"], "offline")
        self.assertIn("detail" if False else "notice", body)
        self.assertTrue(body["notice"])
        self.assertIn("titles", body["pack"])

    def test_polish_uses_a_working_provider(self):
        from autoshorts import engine

        self.client.post("/api/settings",
                         json={"ai_provider": "groq", "groq_key": "gsk_good"})
        reply = {"titles": ["Rewritten: the number that changed it",
                            "Rewritten: what the coach never says"],
                 "hashtags": ["#shorts", "#rewritten"],
                 "description": "Rewritten description for the feed."}
        original = engine._post_json
        engine._post_json = lambda *a, **k: {
            "choices": [{"message": {"content": json.dumps(reply)}}]
        }
        try:
            resp = self.client.post(f"/api/clips/{self.clip_id}/polish")
        finally:
            engine._post_json = original
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["engine"], "groq")
        self.assertEqual(body["notice"], "")
        self.assertIn("Rewritten", body["pack"]["description"])
        self.assertEqual(store.get_clip(self.clip_id)["pack"], body["pack"])


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg is required for a real render")
class EndToEndRenderTests(unittest.TestCase):
    """One demo episode, the whole v0.5.0 stack, and a probe to prove it."""

    def test_smart_logo_brand_beats_1440p_render(self):
        data = Path(config.DATA_DIR) / "e2e"
        for sub in ("media", "clips", "thumbs", "subs"):
            (data / sub).mkdir(parents=True, exist_ok=True)
        # a private state file: this test writes real clips into the shared
        # data dir, but it must never disturb the in-process store other
        # classes use
        local_store = Store(data / "state.json")
        util.fresh_state(local_store)
        local_store.upsert_episode({
            "id": "e2e-qyro", "title": "Qyro end to end", "url": "",
            "duration": 120.0, "source": "manual", "status": "new",
            "clips": [], "added_at": time.time(),
        })
        media = data / "media" / "e2e-qyro.mp4"
        self.assertTrue(make_toy_video(media, seconds=12, size="640x360"))
        bed = data / "bed.mp3"
        self.assertTrue(make_toy_mp3(bed, seconds=4))
        clear_tracks()
        track = audioswap.save_track("bed.mp3", bed.read_bytes())

        # a cached transcript is what a manual clip uses for captions: seed it
        # with one line per second so the ASS file has real word timings
        lines = [
            {"start": float(i), "end": float(i) + 1.0,
             "text": f"the body gives up before the mind does line {i}"}
            for i in range(12)
        ]
        config.SUBS_DIR.mkdir(parents=True, exist_ok=True)
        (config.SUBS_DIR / "e2e-qyro.segments.json").write_text(
            json.dumps(lines), encoding="utf-8"
        )

        original_prepare = pipeline_mod.Pipeline._prepare_media
        pipeline_mod.Pipeline._prepare_media = staticmethod(lambda ep: media)
        worker = pipeline_mod.Pipeline(local_store)
        try:
            job = worker.start_job("e2e-qyro", {
                "kind": "manual", "start": 1.0, "end": 9.0, "title": "All of it",
                "style": "smart", "quality": "1440p", "format": "vertical",
                "captions": "classic", "captions_brand": "qyro-neon",
                "captions_box": False,
                "logo_box": {"preset": "topright", "size": "M", "feather": 4},
                "sync_beats": True, "silence": True,
                "silence_noise": -45.0, "silence_min": 0.2,
                "audio_track": track["id"], "audio_mix": "replace",
                "loud": True, "progress": True, "speed": 1.0,
            })
            deadline = time.time() + 420
            status = local_store.job(job["id"])
            while time.time() < deadline:
                status = local_store.job(job["id"])
                if status["status"] in ("done", "error", "cancelled"):
                    break
                time.sleep(0.5)
            self.assertEqual(status["status"], "done", status.get("error"))
            clips = local_store.clips()
            self.assertEqual(len(clips), 1)
            clip = clips[0]

            # --- the file itself, measured with ffmpeg, not with hope ------
            out = config.CLIPS_DIR / clip["file"]
            self.assertTrue(out.is_file())
            info = maintenance.clip_probe(local_store, clip["id"])
            self.assertEqual((info["width"], info["height"]), (1440, 2560))
            self.assertEqual(info["orientation"], "vertical")
            self.assertEqual(info["video_codec"], "h264")
            self.assertTrue(info["audio_codec"], "a swapped bed must still be muxed")
            self.assertGreater(info["size_bytes"], 10_000)
            self.assertGreater(info["duration"], 4.0)
            self.assertLessEqual(info["duration"], 12.0)

            # --- what got recorded on the clip ----------------------------
            self.assertEqual(clip["quality"], "1440p")
            self.assertEqual(clip["captions_brand"], "qyro-neon")
            self.assertEqual(clip["audio_track"], track["id"])
            self.assertTrue(clip["logo"]["enabled"])
            self.assertIn(clip["logo"]["method"], ("delogo", "boxblur"))
            self.assertEqual(clip["engine"], "offline")

            # --- the captions file proves the brand was burned in ---------
            ass = config.SUBS_DIR / f"{clip['id']}.ass"
            self.assertTrue(ass.is_file())
            text = ass.read_text(encoding="utf-8")
            style_line = next(l for l in text.splitlines() if l.startswith("Style:"))
            self.assertIn("QNeon", style_line)
            events = [l for l in text.splitlines() if l.startswith("Dialogue:")]
            self.assertTrue(events, "the transcript must produce caption events")
            self.assertTrue(any(r"\fs" in l for l in events),
                            "word-pop timing must be in the events")
            # "Dialogue: Layer,Start,End,Style,..." — field 3 is the style
            styles = {l.split(",")[3].strip() for l in events}
            self.assertEqual(styles, {"QNeon"}, "every event uses the brand style")

            # --- the v0.5.0 tool endpoints work on this real clip ---------
            probed = json.dumps(info)
            self.assertIn("1440x2560", probed)
            self.assertEqual(
                [c["index"] for c in maintenance.thumb_candidates(
                    local_store, clip["id"], 6)["candidates"]],
                [0, 1, 2, 3, 4, 5],
            )
            picked = maintenance.thumb_pick(local_store, clip["id"], {"index": 3})
            self.assertEqual(local_store.get_clip(clip["id"])["thumb"],
                             picked["thumb"])
            mp3, name = maintenance.extract_mp3(local_store, clip["id"])
            self.assertTrue(Path(mp3).is_file() and Path(mp3).stat().st_size > 500)
            self.assertTrue(name.endswith(".mp3"))
            beats = maintenance.episode_beats(local_store, "e2e-qyro")
            self.assertIsInstance(beats["beats"], list)
        finally:
            pipeline_mod.Pipeline._prepare_media = original_prepare
            worker.shutdown() if hasattr(worker, "shutdown") else None


class PwaAndAssetTests(unittest.TestCase):
    """Requirement 5: installable, branded, and free of emoji in the chrome."""

    EMOJI_RANGES = (
        (0x1F000, 0x1FAFF), (0x2600, 0x26FF), (0x2700, 0x27BF),
        (0xFE00, 0xFE0F), (0x1F1E6, 0x1F1FF),
    )

    def web_files(self):
        root = WEB
        for path in sorted(root.rglob("*")):
            if path.suffix in (".html", ".css", ".js", ".webmanifest", ".svg"):
                yield path

    def test_no_emoji_in_ui_chrome(self):
        hits = []
        for path in self.web_files():
            text = path.read_text(encoding="utf-8", errors="replace")
            for ch in text:
                if any(lo <= ord(ch) <= hi for lo, hi in self.EMOJI_RANGES):
                    hits.append(f"{path.name}: U+{ord(ch):04X}")
        self.assertEqual(hits, [], f"emoji in the UI: {sorted(set(hits))}")

    def test_index_is_branded(self):
        html = (WEB / "index.html").read_text(encoding="utf-8")
        self.assertIn("<title>Qyro", html)
        self.assertIn('name="theme-color" content="#0A0A0F"', html)
        self.assertIn("manifest.webmanifest", html)
        self.assertIn("apple-touch-icon", html)
        self.assertIn('data-logo="1"', html)
        self.assertNotIn("AutoShorts", html)

    def test_manifest_is_installable(self):
        manifest = json.loads((WEB / "manifest.webmanifest").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "Qyro")
        self.assertEqual(manifest["short_name"], "Qyro")
        self.assertEqual(manifest["theme_color"].upper(), "#0A0A0F")
        self.assertEqual(manifest["display"], "standalone")
        sizes = {tuple(sorted(i["sizes"].split("x"))) for i in manifest["icons"]}
        self.assertIn(("192", "192"), sizes)
        self.assertIn(("512", "512"), sizes)
        purposes = {i["purpose"] for i in manifest["icons"]}
        self.assertEqual(purposes, {"any", "maskable"})
        for icon in manifest["icons"]:
            self.assertTrue((WEB / icon["src"].replace("/static/", "")).is_file(),
                            icon["src"])

    def test_service_worker_is_scope_rooted_and_bounded(self):
        sw = (WEB / "sw.js").read_text(encoding="utf-8")
        self.assertIn("self.addEventListener(\"fetch\"", sw)
        self.assertIn("url.pathname.startsWith(\"/api/\")", sw)
        self.assertIn("QYRO", sw.upper())

    def test_every_icon_the_ui_asks_for_exists(self):
        """A typo in ic(\"name\") would render an empty square, so pin it."""
        app_js = (WEB / "app.js").read_text(encoding="utf-8")
        html = (WEB / "index.html").read_text(encoding="utf-8")
        start = app_js.index("const ICON_PATHS = {")
        block = app_js[start:app_js.index("\n};", start)]
        defined = set(re.findall(r"^  ([a-z]+):", block, re.MULTILINE))
        used = set(re.findall(r'ic\("([a-z]+)"\)', app_js))
        used |= set(re.findall(r'icon\("([a-z]+)"\)', app_js))
        used |= set(re.findall(r'data-icon="([a-z]+)"', html))
        self.assertTrue(used, "the icon set must be in use")
        self.assertEqual(sorted(used - defined), [], "unknown icon names")
        for required in ("play", "search", "download", "sliders", "trash",
                         "pencil", "x", "retry", "sparkle", "music", "captions",
                         "crop", "wand"):
            self.assertIn(required, defined, f"the brief asked for {required}")

    def test_logo_assets_exist_and_are_crisp(self):
        svg = (WEB / "logo.svg").read_text(encoding="utf-8")
        self.assertIn("viewBox=\"0 0 64 64\"", svg)
        for colour in ("#7C3AED", "#22D3EE", "#0A0A0F"):
            self.assertIn(colour, svg)
        icons = WEB / "icons"
        for name in ("icon-192.png", "icon-512.png", "icon-maskable-192.png",
                     "icon-maskable-512.png", "apple-touch-icon.png",
                     "favicon-32.png", "favicon.svg"):
            self.assertTrue((icons / name).is_file(), name)
        with (icons / "icon-512.png").open("rb") as handle:
            head = handle.read(24)
        self.assertEqual(head[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(int.from_bytes(head[16:20], "big"), 512)


class StdlibV050Tests(unittest.TestCase):
    """The same v0.5.0 routes over real HTTP on the pure-Python server.

    This is the Termux path: no FastAPI, no pip packages, and it is where the
    larger ``POST /api/audio`` body cap and the ``/sw.js`` root route live.
    """

    @classmethod
    def setUpClass(cls):
        cls.data_dir = Path(tempfile.mkdtemp(prefix="as-test-v050-"))
        cls.video = cls.data_dir / "toy.mp4"
        cls.mp3 = cls.data_dir / "toy.mp3"
        cls.have_video = make_toy_video(cls.video, seconds=4)
        cls.have_mp3 = make_toy_mp3(cls.mp3, seconds=2)
        cls.port = util.free_port()
        cls.httpd = util.spawn_server(cls.port, cls.data_dir)
        util.wait_http_ready(cls.port)

    @classmethod
    def tearDownClass(cls):
        if cls.httpd:
            cls.httpd.terminate()
            try:
                cls.httpd.wait(timeout=10)
            except Exception:
                cls.httpd.kill()
        shutil.rmtree(cls.data_dir, ignore_errors=True)

    def setUp(self):
        self.base = f"http://127.0.0.1:{self.port}"
        empty = {
            "settings": {"playlist_url": "", "autopilot": False},
            "episodes": {}, "clips": {}, "jobs": {},
        }
        status, body, _ = util.http_request("POST", f"{self.base}/api/restore", empty)
        assert status == 200, body
        status, body, _ = self.post("/api/demo/load")
        assert status == 200, body

    def post(self, path, payload=None, timeout=120):
        return util.http_request("POST", f"{self.base}{path}", payload, timeout=timeout)

    def get(self, path, timeout=120):
        return util.http_request("GET", f"{self.base}{path}", timeout=timeout)

    def delete(self, path):
        return util.http_request("DELETE", f"{self.base}{path}")

    def wait_job(self, job_id, timeout=420.0):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            status, body, _ = self.get("/api/jobs?limit=200")
            for job in body.get("jobs", []):
                if job["id"] == job_id:
                    last = job
                    if job["status"] in ("done", "error", "cancelled"):
                        return job
            time.sleep(1.0)
        raise AssertionError(f"job {job_id} never finished: {last}")

    # ------------------------------------------------------------------
    def test_health_and_state_branding(self):
        status, body, _ = self.get("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["app"], "Qyro")
        self.assertEqual(body["version"], autoshorts_version)
        self.assertEqual(body["brand"], "Qyro")
        self.assertEqual(body["engine"]["provider"], "offline")

        status, state, _ = self.get("/api/state")
        self.assertEqual(status, 200)
        for key in ("engine", "audio_tracks", "defaults", "settings"):
            self.assertIn(key, state)
        self.assertEqual(state["defaults"]["captions_brand"], "none")
        self.assertEqual(state["defaults"]["quality"], "fast")
        # the v0.5.0 render options are all advertised as defaults
        for key in ("logo_box", "sync_beats", "audio_track", "audio_mix",
                    "silence_noise", "silence_min"):
            self.assertIn(key, state["defaults"])
        self.assertNotIn("gemini_key", state["settings"])

    def test_settings_keys_are_masked_over_http(self):
        status, body, _ = self.post(
            "/api/settings", {"ai_provider": "groq", "groq_key": "gsk_real"}
        )
        self.assertEqual(status, 200, body)
        self.assertNotIn("gsk_real", json.dumps(body))
        self.assertTrue(body["settings"]["groq_key_set"])
        status, state, _ = self.get("/api/state")
        self.assertNotIn("gsk_real", json.dumps(state))
        self.assertEqual(state["engine"]["provider"], "groq")

    def test_audio_routes_and_titles_lab(self):
        if not self.have_mp3:
            self.skipTest("no ffmpeg")
        payload = {"name": "toy.mp3",
                   "data_b64": base64.b64encode(self.mp3.read_bytes()).decode()}
        status, body, _ = self.post("/api/audio", payload)
        self.assertEqual(status, 200, body)
        track_id = body["track_id"]
        self.assertTrue(track_id)
        self.assertEqual(body["name"], "toy.mp3")

        status, listed, _ = self.get("/api/audio")
        self.assertEqual(status, 200)
        self.assertEqual([t["id"] for t in listed["tracks"]], [track_id])

        status, raw, headers = self.get(f"/api/audio/{track_id}/file")
        self.assertEqual(status, 200)
        self.assertIn("audio", headers["Content-Type"])
        self.assertGreater(len(raw), 500)

        status, body, _ = self.post(
            "/api/episodes/demo-chhetri-223/shorts",
            {"count": 1, "min_dur": 20, "max_dur": 26, "audio_track": track_id,
             "audio_mix": "replace", "captions_brand": "qyro-pop",
             "logo_box": {"preset": "topleft", "size": "M"}, "quality": "fast"},
        )
        self.assertEqual(status, 200, body)
        job = self.wait_job(body["job_id"])
        self.assertEqual(job["status"], "done", job.get("error"))

        status, state, _ = self.get("/api/state")
        clips = [c for c in state["clips"] if c["episode_id"] == "demo-chhetri-223"]
        self.assertEqual(len(clips), 1)
        clip = clips[0]
        self.assertEqual(clip["captions_brand"], "qyro-pop")
        self.assertEqual(clip["audio_track"], track_id)
        self.assertTrue(clip["logo"]["enabled"])

        status, info, _ = self.get(f"/api/clips/{clip['id']}/probe")
        self.assertEqual(status, 200)
        self.assertFalse(info["missing"])
        self.assertEqual((info["width"], info["height"]), (720, 1280))
        self.assertEqual(info["orientation"], "vertical")
        self.assertTrue(info["audio_codec"])

        status, raw, headers = self.get(f"/api/clips/{clip['id']}/audio.mp3")
        self.assertEqual(status, 200)
        self.assertIn("audio/mpeg", headers["Content-Type"])
        self.assertGreater(len(raw), 1000)

        status, cand, _ = self.get(f"/api/clips/{clip['id']}/thumb-candidates?n=5")
        self.assertEqual(status, 200)
        self.assertEqual(cand["count"], 5)
        status, raw, headers = self.get(
            f"/api/clips/{clip['id']}/thumb?index={cand['candidates'][2]['index']}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/jpeg")
        status, body, _ = self.post(
            f"/api/clips/{clip['id']}/thumb-pick", {"index": 2}
        )
        self.assertEqual(status, 200, body)
        status, state, _ = self.get("/api/state")
        self.assertEqual(
            [c["thumb"] for c in state["clips"] if c["id"] == clip["id"]][0],
            body["thumb"],
        )

        status, body, _ = self.post(
            "/api/titles", {"text": "the body gives up before the mind does",
                            "profile": "energy"}
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["titles"]), 10)
        self.assertEqual(body["engine"], "offline")

        status, beats, _ = self.get("/api/episodes/demo-chhetri-223/beats")
        self.assertEqual(status, 200)
        self.assertIsInstance(beats["beats"], list)
        self.assertGreater(beats["count"], 0)

        status, body, _ = self.delete(f"/api/audio/{track_id}")
        self.assertEqual(status, 200)
        self.assertEqual(status, 200)

    def test_stdlib_error_shapes(self):
        cases = [
            ("GET", "/api/clips/ghost/probe", 404),
            ("GET", "/api/clips/ghost/thumb-candidates", 404),
            ("GET", "/api/episodes/ghost/beats", 404),
            ("GET", "/api/audio/ghost/file", 404),
            ("POST", "/api/titles", 422),
        ]
        for method, path, want in cases:
            with self.subTest(path=path):
                if method == "GET":
                    status, body, _ = self.get(path)
                else:
                    status, body, _ = self.post(path, {})
                self.assertEqual(status, want, body)
                self.assertIn("detail", body)

        status, body, _ = self.get("/api/titles")
        self.assertEqual(status, 405)
        self.assertIn("detail", body)
        status, body, _ = self.get("/api/nonsense")
        self.assertEqual(status, 404)

    def test_pwa_files_are_served(self):
        for path, needle in (
            ("/sw.js", "addEventListener"),
            ("/static/manifest.webmanifest", "Qyro"),
            ("/static/icons/icon-192.png", None),
            ("/static/icons/favicon.svg", "Qyro"),
            ("/static/logo.svg", "svg"),
        ):
            status, raw, headers = self.get(path)
            self.assertEqual(status, 200, path)
            if needle:
                # the harness parses anything served as JSON, so normalise
                text = raw if isinstance(raw, str) else json.dumps(
                    raw, ensure_ascii=False
                ) if isinstance(raw, (dict, list)) else raw.decode(
                    "utf-8", "replace"
                )
                self.assertIn(needle, text, path)

    def test_rejected_body_shape_is_a_json_error(self):
        status, body, _ = self.post("/api/audio", {"name": 1, "data_b64": "AAAA"})
        self.assertEqual(status, 422)
        self.assertIn("detail", body)
