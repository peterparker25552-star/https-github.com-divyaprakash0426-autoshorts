"""Full HTTP-surface tests against the FastAPI mirror (in-process ASGI).

Covers every endpoint contract and error code, including bool-speed 422,
"Unknown options", search shape, CSV, cancel/retry/delete rules and the
ONE-probe batch rule.

The pipeline worker is replaced by a controllable fake for the whole module
(no test ever triggers a real render); the real worker + render path is
covered end-to-end by the stdlib server test.
"""
from __future__ import annotations

import threading
import time
import unittest

from fastapi.testclient import TestClient

from autoshorts import config
from autoshorts.server import app, pipeline, store

from . import util


class WorkerFixture(unittest.TestCase):
    """Fresh store + a fake worker: jobs run→wait→done only when released."""

    def setUp(self):
        util.fresh_state(store)
        self.blocker = threading.Event()  # unset by default → jobs block
        self.original_run = pipeline._run_job
        pipeline._run_job = self._fake_run
        self.client = TestClient(app, raise_server_exceptions=False)

    def _fake_run(self, job_id):
        job = store.job(job_id)
        if not job or job.get("status") == "cancelled":
            return  # skip without running (mirrors the real worker rule)
        store.update_job(job_id, status="running", step="render", progress=0.5)
        self.blocker.wait(timeout=30)
        store.update_job(job_id, status="done", progress=1.0, step="done")
        if store.has_active_jobs(job["episode_id"]):
            store.update_episode(job["episode_id"], status="processing")
        else:
            store.update_episode(job["episode_id"], status="done", error=None)

    def release(self):
        self.blocker.set()

    def wait_episode_idle(self, ep_id, timeout=10.0, want="done"):
        deadline = time.time() + timeout
        while time.time() < deadline:
            episode = store.get_episode(ep_id)
            job_statuses = [job["status"] for job in store.jobs_for_episode(ep_id)]
            if episode and episode.get("status") == want and "running" not in job_statuses:
                return
            time.sleep(0.05)
        raise AssertionError(f"episode {ep_id} never settled to {want}")

    def tearDown(self):
        self.release()
        deadline = time.time() + 10
        while time.time() < deadline and store.active_jobs():
            time.sleep(0.05)
        pipeline._run_job = self.original_run
        util.fresh_state(store)


class HealthAndStateTests(WorkerFixture):
    def test_health_version_050(self):
        resp = self.client.get("/api/health")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["version"], "0.5.0")
        self.assertEqual(body["app"], "Qyro")
        self.assertEqual(body["brand"], "Qyro")
        self.assertIn("ffmpeg", body)
        self.assertIn("versions", body)
        self.assertIn("yt_dlp", body["versions"])

    def test_state_shape(self):
        resp = self.client.get("/api/state")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        for key in ("episodes", "clips", "jobs", "settings", "stats", "defaults"):
            self.assertIn(key, body)
        self.assertIn("captions_pos", body["defaults"])
        self.assertIn("captions_box", body["defaults"])

    def test_unknown_path_404(self):
        self.assertEqual(self.client.get("/api/nope").status_code, 404)

    def test_wrong_method_405(self):
        resp = self.client.get("/api/playlist")
        self.assertEqual(resp.status_code, 405)
        self.assertIn("detail", resp.json())
        resp = self.client.post("/api/state")
        self.assertEqual(resp.status_code, 405)
        resp = self.client.delete("/api/health")
        self.assertEqual(resp.status_code, 405)


class IngestTests(WorkerFixture):
    def test_playlist_url_required(self):
        self.assertEqual(
            self.client.post("/api/playlist", json={"url": "short"}).status_code, 422
        )
        self.assertEqual(
            self.client.post("/api/playlist", json={}).status_code, 422
        )

    def test_unreachable_503_single_probe(self):
        calls = {"n": 0}

        def fake_reachable(ttl=60.0):
            calls["n"] += 1
            return False

        import autoshorts.server as server_module

        original = server_module.youtube_reachable
        server_module.youtube_reachable = fake_reachable
        try:
            resp = self.client.post(
                "/api/playlist", json={"url": "https://www.youtube.com/watch?v=abc"}
            )
            self.assertEqual(resp.status_code, 503)
            self.assertEqual(calls["n"], 1)  # exactly ONE probe per request
        finally:
            server_module.youtube_reachable = original

    def test_demo_load(self):
        resp = self.client.post("/api/demo/load")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["added"], 3)
        state = self.client.get("/api/state").json()
        self.assertEqual(len(state["episodes"]), 3)
        self.assertTrue(all(ep["source"] == "demo" for ep in state["episodes"]))

    def test_batch_one_probe_total(self):
        calls = {"n": 0}

        def fake_reachable(ttl=60.0):
            calls["n"] += 1
            return False

        import autoshorts.server as server_module

        original = server_module.youtube_reachable
        server_module.youtube_reachable = fake_reachable
        try:
            resp = self.client.post(
                "/api/batch",
                json={"urls": ["https://example.com/v1", "https://example.com/v2",
                               "https://example.com/v3"]},
            )
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(body["queued"], 0)
            self.assertEqual(body["skipped"], 3)
            self.assertEqual(calls["n"], 1)  # ONE probe for the whole batch
        finally:
            server_module.youtube_reachable = original

    def test_batch_bad_body_422(self):
        self.assertEqual(
            self.client.post("/api/batch", json={"urls": "notalist"}).status_code, 422
        )
        self.assertEqual(
            self.client.post("/api/batch", json={"episodes": [1, 2]}).status_code, 422
        )


class ShortsAndPreviewTests(WorkerFixture):
    def setUp(self):
        super().setUp()
        self.client.post("/api/demo/load")
        self.ep = "demo-chhetri-223"

    def test_shorts_queues_job(self):
        self.release()  # let queued jobs finish immediately
        resp = self.client.post(
            f"/api/episodes/{self.ep}/shorts",
            json={"count": 1, "min_dur": 20, "max_dur": 30},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("job_id", resp.json())

    def test_shorts_unknown_episode_404(self):
        resp = self.client.post("/api/episodes/ghost/shorts", json={})
        self.assertEqual(resp.status_code, 404)

    def test_bool_speed_422(self):
        resp = self.client.post(
            f"/api/episodes/{self.ep}/shorts", json={"speed": True}
        )
        self.assertEqual(resp.status_code, 422)
        resp = self.client.post(
            f"/api/episodes/{self.ep}/shorts", json={"speed": False}
        )
        self.assertEqual(resp.status_code, 422)

    def test_unknown_options_422(self):
        resp = self.client.post(
            f"/api/episodes/{self.ep}/shorts", json={"filters": "cinematic"}
        )
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["detail"], "Unknown options")

    def test_bad_count_and_profile_422(self):
        self.assertEqual(
            self.client.post(f"/api/episodes/{self.ep}/shorts", json={"count": 0}).status_code,
            422,
        )
        self.assertEqual(
            self.client.post(f"/api/episodes/{self.ep}/shorts", json={"count": 13}).status_code,
            422,
        )
        self.assertEqual(
            self.client.post(f"/api/episodes/{self.ep}/shorts", json={"profile": "drama"}).status_code,
            422,
        )

    def test_bad_style_and_captions_422(self):
        self.assertEqual(
            self.client.post(f"/api/episodes/{self.ep}/shorts", json={"style": "diagonal"}).status_code,
            422,
        )
        self.assertEqual(
            self.client.post(f"/api/episodes/{self.ep}/shorts", json={"captions_pos": "top"}).status_code,
            422,
        )
        self.assertEqual(
            self.client.post(f"/api/episodes/{self.ep}/shorts", json={"captions_box": "yes"}).status_code,
            422,
        )

    def test_preview_contract(self):
        resp = self.client.post(
            f"/api/episodes/{self.ep}/preview",
            json={"count": 2, "min_dur": 20, "max_dur": 60, "profile": "viral"},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("stats", body)
        self.assertLessEqual(len(body["moments"]), 2)
        for moment in body["moments"]:
            for key in ("start", "end", "duration", "title", "score", "reasons", "breakdown"):
                self.assertIn(key, moment)
            self.assertIsInstance(moment["reasons"], list)
            self.assertIsInstance(moment["breakdown"], dict)

    def test_preview_validation(self):
        self.assertEqual(
            self.client.post(
                f"/api/episodes/{self.ep}/preview", json={"count": 99}
            ).status_code,
            422,
        )
        self.assertEqual(
            self.client.post(
                f"/api/episodes/{self.ep}/preview",
                json={"min_dur": 60, "max_dur": 20},
            ).status_code,
            422,
        )
        self.assertEqual(
            self.client.post("/api/episodes/ghost/preview", json={}).status_code,
            404,
        )

    def test_manual_min_duration(self):
        self.release()
        resp = self.client.post(
            f"/api/episodes/{self.ep}/manual",
            json={"start": 10, "end": 40, "title": "Manual test"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("job_id", resp.json())
        resp = self.client.post(
            f"/api/episodes/{self.ep}/manual", json={"start": 10, "end": 12}
        )
        self.assertEqual(resp.status_code, 422)
        resp = self.client.post(
            f"/api/episodes/{self.ep}/manual", json={"start": "ten", "end": 40}
        )
        self.assertEqual(resp.status_code, 422)


class TranscriptChaptersExportTests(WorkerFixture):
    def setUp(self):
        super().setUp()
        self.client.post("/api/demo/load")
        self.ep = "demo-chhetri-223"

    def test_transcript_shape(self):
        resp = self.client.get(f"/api/episodes/{self.ep}/transcript")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertGreater(len(body["segments"]), 0)
        for segment in body["segments"]:
            self.assertEqual(set(segment.keys()), {"start", "end", "text"})

    def test_chapters_have_start_title(self):
        resp = self.client.get(f"/api/episodes/{self.ep}/chapters")
        self.assertEqual(resp.status_code, 200)
        chapters = resp.json()["chapters"]
        self.assertGreater(len(chapters), 0)
        for chapter in chapters:
            self.assertIn("start", chapter)
            self.assertIn("title", chapter)
        self.assertEqual(chapters[0]["start"], 0.0)  # intro prepended

    def test_chapters_unknown_404(self):
        self.assertEqual(
            self.client.get("/api/episodes/ghost/chapters").status_code, 404
        )

    def test_export_csv(self):
        resp = self.client.get(f"/api/episodes/{self.ep}/export?count=8&profile=viral")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/csv", resp.headers["content-type"])
        self.assertIn(
            "qyro-demo-chhetri-223-moments.csv",
            resp.headers["content-disposition"],
        )
        lines = resp.text.strip().splitlines()
        self.assertEqual(lines[0], "start,end,duration,title,score,reasons")
        self.assertGreater(len(lines) - 1, 0)

    def test_export_validation(self):
        self.assertEqual(
            self.client.get(f"/api/episodes/{self.ep}/export?count=0").status_code, 422
        )
        self.assertEqual(
            self.client.get(f"/api/episodes/{self.ep}/export?count=99").status_code, 422
        )
        self.assertEqual(
            self.client.get(f"/api/episodes/{self.ep}/export?count=abc").status_code, 422
        )
        self.assertEqual(
            self.client.get(f"/api/episodes/{self.ep}/export?profile=bogus").status_code, 422
        )
        self.assertEqual(
            self.client.get("/api/episodes/ghost/export?count=8").status_code, 404
        )


class SearchTests(WorkerFixture):
    def setUp(self):
        super().setUp()
        self.client.post("/api/demo/load")

    def test_hit_shape(self):
        resp = self.client.get("/api/search?q=fear")
        self.assertEqual(resp.status_code, 200)
        results = resp.json()["results"]
        self.assertGreater(len(results), 0)
        for hit in results:
            self.assertEqual(
                set(hit.keys()),
                {"episode_id", "episode_title", "start", "end", "text"},
            )

    def test_short_q_422(self):
        self.assertEqual(self.client.get("/api/search?q=f").status_code, 422)
        self.assertEqual(self.client.get("/api/search?q=").status_code, 422)
        self.assertEqual(self.client.get("/api/search").status_code, 422)

    def test_empty_results(self):
        resp = self.client.get("/api/search?q=zzzzunfindable")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["results"], [])


class SettingsTests(WorkerFixture):
    def test_persist_settings(self):
        resp = self.client.post(
            "/api/settings",
            json={"autopilot": True, "playlist_url": "https://example.com/playlist?list=xyz"},
        )
        self.assertEqual(resp.status_code, 200)
        settings = self.client.get("/api/state").json()["settings"]
        self.assertTrue(settings["autopilot"])
        self.assertEqual(settings["playlist_url"], "https://example.com/playlist?list=xyz")

    def test_validation_422(self):
        self.assertEqual(
            self.client.post("/api/settings", json={"autopilot": "yes"}).status_code, 422
        )
        self.assertEqual(
            self.client.post("/api/settings", json={}).status_code, 422
        )
        self.assertEqual(
            self.client.post("/api/settings", json={"playlist_url": "short"}).status_code, 422
        )


class RenameRerenderPolishTests(WorkerFixture):
    def setUp(self):
        super().setUp()
        self.client.post("/api/demo/load")
        self.release()
        self.clip_id = util.make_fake_clip(store, config, with_files=False)

    def test_rename_ok(self):
        resp = self.client.post(
            f"/api/clips/{self.clip_id}/rename", json={"title": "  Trimmed  "}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"id": self.clip_id, "title": "Trimmed"})

    def test_rename_422s(self):
        for body in ({}, {"title": ""}, {"title": "   "}, {"title": "x" * 121}):
            resp = self.client.post(f"/api/clips/{self.clip_id}/rename", json=body)
            self.assertEqual(resp.status_code, 422, body)

    def test_rename_unknown_404(self):
        self.assertEqual(
            self.client.post(
                "/api/clips/ghost/rename", json={"title": "fine"}
            ).status_code,
            404,
        )

    def test_rerender_unknown_options_422(self):
        resp = self.client.post(
            f"/api/clips/{self.clip_id}/rerender", json={"count": 5}
        )
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["detail"], "Unknown options")

    def test_rerender_unknown_clip_404(self):
        self.assertEqual(
            self.client.post("/api/clips/ghost/rerender", json={}).status_code, 404
        )

    def test_rerender_queues_job_with_base(self):
        resp = self.client.post(
            f"/api/clips/{self.clip_id}/rerender", json={"style": "smart"}
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("job_id", body)
        self.assertEqual(body["options"]["captions_pos"], "standard")  # from clip.render
        self.assertEqual(body["options"]["style"], "smart")

    def test_polish_503_without_llm(self):
        if config.LLM_API_KEY:
            self.skipTest("LLM configured")
        resp = self.client.post(f"/api/clips/{self.clip_id}/polish")
        self.assertEqual(resp.status_code, 503)


class CancelRetryDeleteTests(WorkerFixture):
    def setUp(self):
        super().setUp()
        self.client.post("/api/demo/load")

    def _wait_running(self, job_id, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = store.job(job_id)
            if job and job["status"] == "running":
                return
            time.sleep(0.05)
        raise AssertionError(f"job {job_id} never started running")

    def test_cancel_rules(self):
        # job1 occupies the worker (running); job2 stays queued.
        job1 = self.client.post(
            "/api/episodes/demo-chhetri-223/shorts", json={"count": 1}
        ).json()["job_id"]
        job2 = self.client.post(
            "/api/episodes/demo-dig-221/shorts", json={"count": 1}
        ).json()["job_id"]
        self._wait_running(job1)

        # cancel queued → ok
        resp = self.client.post(f"/api/jobs/{job2}/cancel")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"cancelled": job2})
        self.assertEqual(store.job(job2)["status"], "cancelled")

        # cancel running → 409
        resp = self.client.post(f"/api/jobs/{job1}/cancel")
        self.assertEqual(resp.status_code, 409)

        # cancel unknown → 404
        self.assertEqual(
            self.client.post("/api/jobs/ghost/cancel").status_code, 404
        )

    def test_cancel_resets_episode_when_idle(self):
        # Occupy the worker with a job on ANOTHER episode so job2 stays queued.
        holder = self.client.post(
            "/api/episodes/demo-chhetri-223/shorts", json={"count": 1}
        ).json()["job_id"]
        self._wait_running(holder)

        ep = "demo-diljit-215"
        job2 = self.client.post(
            f"/api/episodes/{ep}/shorts", json={"count": 1}
        ).json()["job_id"]  # queued behind the holder
        self.assertEqual(store.job(job2)["status"], "queued")
        self.assertEqual(store.get_episode(ep)["status"], "processing")

        resp = self.client.post(f"/api/jobs/{job2}/cancel")
        self.assertEqual(resp.status_code, 200)
        # episode resets to new: the cancel left no active jobs for it
        self.assertEqual(store.get_episode(ep)["status"], "new")
        self.assertEqual(store.job(job2)["status"], "cancelled")

    def test_retry_rules(self):
        job1 = self.client.post(
            "/api/episodes/demo-chhetri-223/shorts", json={"count": 1}
        ).json()["job_id"]
        self._wait_running(job1)
        # running → 409
        self.assertEqual(
            self.client.post(f"/api/jobs/{job1}/retry").status_code, 409
        )
        # unknown → 404
        self.assertEqual(
            self.client.post("/api/jobs/ghost/retry").status_code, 404
        )
        # done → fresh job
        self.release()
        self.wait_episode_idle("demo-chhetri-223", want="done")
        resp = self.client.post(f"/api/jobs/{job1}/retry")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("job_id", body)
        self.assertNotEqual(body["job_id"], job1)

    def test_delete_episode_busy_then_ok(self):
        ep = "demo-dig-221"
        job = self.client.post(
            f"/api/episodes/{ep}/shorts", json={"count": 1}
        ).json()["job_id"]
        self._wait_running(job)
        # busy → 409
        self.assertEqual(
            self.client.delete(f"/api/episodes/{ep}").status_code, 409
        )
        # finish, add a clip with files, delete for real
        self.release()
        self.wait_episode_idle(ep, want="done")
        clip_id = util.make_fake_clip(store, config, episode_id=ep)
        clip = store.get_clip(clip_id)
        clip_path = config.CLIPS_DIR / clip["file"]
        thumb_path = config.THUMBS_DIR / clip["thumb"]
        self.assertTrue(clip_path.exists())
        resp = self.client.delete(f"/api/episodes/{ep}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"deleted": ep, "clips_removed": 1})
        self.assertFalse(clip_path.exists())
        self.assertFalse(thumb_path.exists())
        # unknown → 404
        self.assertEqual(self.client.delete("/api/episodes/ghost").status_code, 404)


class StorageBackupTests(WorkerFixture):
    def test_storage_info(self):
        resp = self.client.get("/api/storage")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        for name in ("media", "clips", "thumbs", "subs"):
            self.assertIn(name, body["dirs"])
        self.assertIn("free", body["disk"])
        self.assertIn("counts", body)

    def test_clean_bad_target_422(self):
        resp = self.client.post("/api/storage/clean", json={"target": "everything"})
        self.assertEqual(resp.status_code, 422)
        resp = self.client.post("/api/storage/clean", json={"target": "media"})
        self.assertEqual(resp.status_code, 422)  # media is not a clean target

    def test_clean_thumbs(self):
        path = config.THUMBS_DIR / "http-test-thumb.jpg"
        path.write_bytes(b"z" * 512)
        resp = self.client.post("/api/storage/clean", json={"target": "thumbs"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("removed", body)
        self.assertIn("freed_bytes", body)
        self.assertGreaterEqual(body["freed_bytes"], 512)
        self.assertFalse(path.exists())

    def test_backup_restore_roundtrip(self):
        self.client.post("/api/demo/load")
        resp = self.client.get("/api/backup")
        self.assertEqual(resp.status_code, 200)
        state = resp.json()
        for key in ("settings", "episodes", "clips", "jobs"):
            self.assertIn(key, state)
        util.fresh_state(store)
        resp = self.client.post("/api/restore", json=state)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["episodes"], 3)
        # restore validation
        self.assertEqual(self.client.post("/api/restore", json=[1, 2]).status_code, 422)
        self.assertEqual(
            self.client.post("/api/restore", json={"settings": {}}).status_code, 422
        )


class ClipServingTests(WorkerFixture):
    def setUp(self):
        super().setUp()
        self.client.post("/api/demo/load")
        self.clip_id = util.make_fake_clip(store, config)

    def test_file_and_thumb(self):
        resp = self.client.get(f"/api/clips/{self.clip_id}/file")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("video/mp4", resp.headers["content-type"])
        resp = self.client.get(f"/api/clips/{self.clip_id}/thumb")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("image/jpeg", resp.headers["content-type"])

    def test_missing_files_410(self):
        clip = store.get_clip(self.clip_id)
        (config.CLIPS_DIR / clip["file"]).unlink()
        self.assertEqual(
            self.client.get(f"/api/clips/{self.clip_id}/file").status_code, 410
        )

    def test_unknown_clip_404(self):
        self.assertEqual(
            self.client.get("/api/clips/ghost/file").status_code, 404
        )
        self.assertEqual(
            self.client.delete("/api/clips/ghost").status_code, 404
        )

    def test_srt(self):
        resp = self.client.get(f"/api/clips/{self.clip_id}/srt")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("--> ", resp.text)

    def test_delete_clip_removes_files(self):
        clip = store.get_clip(self.clip_id)
        clip_path = config.CLIPS_DIR / clip["file"]
        thumb_path = config.THUMBS_DIR / clip["thumb"]
        resp = self.client.delete(f"/api/clips/{self.clip_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"deleted": self.clip_id})
        self.assertFalse(clip_path.exists())
        self.assertFalse(thumb_path.exists())

    def test_zip_empty_404(self):
        util.fresh_state(store)
        resp = self.client.get("/api/clips/zip")
        self.assertEqual(resp.status_code, 404)

    def test_zip_with_clips(self):
        resp = self.client.get("/api/clips/zip")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("application/zip", resp.headers["content-type"])


class StaticUITests(WorkerFixture):
    def test_index_served(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Qyro", resp.text)
        self.assertNotIn("AutoShorts", resp.text)

    def test_static_served(self):
        self.assertEqual(self.client.get("/static/app.js").status_code, 200)
        self.assertEqual(self.client.get("/static/style.css").status_code, 200)

    def test_index_counts_via_string(self):
        # rule 3: episode-card defaults render count via String(...)
        app_js = (config.BASE_DIR / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("String(n) === String(opts.count)", app_js)
        self.assertIn('placeholder="playlist or video URL"',
                      (config.BASE_DIR / "web" / "index.html").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
