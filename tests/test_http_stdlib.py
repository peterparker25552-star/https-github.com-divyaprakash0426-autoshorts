"""End-to-end tests against the stdlib server, run as a REAL subprocess
(``python -m autoshorts.server_stdlib``) on a real port.

This is where the whole stack is exercised over actual HTTP, including the
full demo render (transcript → highlights → ffmpeg → clip + waveform) and the
download endpoints.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
import zipfile
from io import BytesIO
from pathlib import Path

from autoshorts import __version__ as autoshorts_version

from . import util


class StdlibServerBase(unittest.TestCase):
    """One server for the whole class (state managed via /api/restore)."""

    httpd = None
    port = None
    data_dir = None

    @classmethod
    def setUpClass(cls):
        cls.data_dir = Path(tempfile.mkdtemp(prefix="as-test-stdlib-"))
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
        # Reset the library through the public restore API so tests stay
        # independent even though they share one server process.
        empty = {
            "settings": {"playlist_url": "", "autopilot": False},
            "episodes": {},
            "clips": {},
            "jobs": {},
        }
        status, body, _ = util.http_request("POST", f"{self.base}/api/restore", empty)
        assert status == 200, body
        self.load_demo()

    def load_demo(self):
        status, body, _ = util.http_request("POST", f"{self.base}/api/demo/load")
        assert status == 200, body
        return body

    def post(self, path, payload=None, timeout=60):
        return util.http_request("POST", f"{self.base}{path}", payload, timeout=timeout)

    def get(self, path, timeout=60):
        return util.http_request("GET", f"{self.base}{path}", timeout=timeout)

    def delete(self, path, timeout=60):
        return util.http_request("DELETE", f"{self.base}{path}", timeout=timeout)

    def wait_job(self, job_id, timeout=420.0):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            status, body, _ = self.get(f"/api/jobs?limit=200")
            for job in body.get("jobs", []):
                if job["id"] == job_id:
                    last = job
                    if job["status"] in ("done", "error", "cancelled"):
                        return job
            time.sleep(1.0)
        raise AssertionError(f"job {job_id} never finished: {last}")


class HealthTests(StdlibServerBase):
    def test_health_version_and_shape(self):
        status, body, headers = self.get("/api/health")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers["Content-Type"])
        self.assertEqual(body["version"], autoshorts_version)
        self.assertEqual(body["app"], "Qyro")
        self.assertEqual(body["brand"], "Qyro")
        self.assertIsInstance(body["youtube_reachable"], bool)
        self.assertTrue(body["demo_available"])
        self.assertIn("yt_dlp", body["versions"])
        self.assertRegex(body["versions"]["yt_dlp"], r"^\d{4}\.\d{2}\.\d{2}")

    def test_state_shape(self):
        status, body, _ = self.get("/api/state")
        self.assertEqual(status, 200)
        for key in ("episodes", "clips", "jobs", "settings"):
            self.assertIn(key, body)


class ErrorShapeTests(StdlibServerBase):
    def test_errors_are_json_detail(self):
        status, body, headers = self.get("/api/episodes/ghost/transcript")
        self.assertEqual(status, 404)
        self.assertIn("application/json", headers["Content-Type"])
        self.assertIn("detail", body)

    def test_wrong_methods_405(self):
        for method, path in (
            ("GET", "/api/playlist"),
            ("POST", "/api/state"),
            ("DELETE", "/api/health"),
            ("POST", "/api/search"),
            ("GET", "/api/storage/clean"),
            ("PUT", "/api/settings"),
            ("POST", f"/api/clips/anything/file"),
        ):
            status, body, _ = util.http_request(method, f"{self.base}{path}", {} if method in ("POST", "PUT") else None)
            self.assertEqual(status, 405, f"{method} {path}")
            self.assertIn("detail", body)

    def test_unknown_path_404(self):
        self.assertEqual(self.get("/api/whatever")[0], 404)

    def test_invalid_json_422(self):
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            f"{self.base}/api/settings",
            data=b"{not json",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as resp:
                status = resp.status
                body = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            status = exc.code
            body = json.loads(exc.read())
        self.assertEqual(status, 422)
        self.assertIn("detail", body)


class IngestTests(StdlibServerBase):
    def test_playlist_validation(self):
        self.assertEqual(self.post("/api/playlist", {"url": "x"})[0], 422)
        self.assertEqual(self.post("/api/playlist", {})[0], 422)

    def test_demo_load_adds_three(self):
        state = self.get("/api/state")[1]
        self.assertEqual(len(state["episodes"]), 3)
        self.assertTrue(all(ep["source"] == "demo" for ep in state["episodes"]))
        self.assertEqual(state["stats"]["episodes"], 3)

    def test_settings_persist(self):
        status, body, _ = self.post(
            "/api/settings",
            {"autopilot": True, "playlist_url": "https://example.com/playlist?list=pq"},
        )
        self.assertEqual(status, 200)
        settings = self.get("/api/state")[1]["settings"]
        self.assertTrue(settings["autopilot"])
        self.assertEqual(settings["playlist_url"], "https://example.com/playlist?list=pq")
        # reset for other tests
        self.post("/api/settings", {"autopilot": False})

    def test_settings_validation(self):
        self.assertEqual(
            self.post("/api/settings", {"autopilot": "nope"})[0], 422
        )
        self.assertEqual(self.post("/api/settings", {})[0], 422)


class ShortsFlowTests(StdlibServerBase):
    EP = "demo-chhetri-223"

    def test_full_demo_render_with_waveform(self):
        """THE end-to-end: transcript → highlights → ffmpeg → clip + waveform."""
        status, body, _ = self.post(
            f"/api/episodes/{self.EP}/shorts",
            {
                "count": 1, "min_dur": 20, "max_dur": 32,
                "profile": "viral",
                "style": "crop", "quality": "fast", "format": "vertical",
                "captions": "classic", "captions_pos": "standard",
                "captions_box": False, "speed": 1.0,
                "progress": False, "silence": False, "loud": False,
            },
        )
        self.assertEqual(status, 200, body)
        job_id = body["job_id"]

        job = self.wait_job(job_id, timeout=420)
        self.assertEqual(job["status"], "done", job)

        state = self.get("/api/state")[1]
        clips = [c for c in state["clips"] if c["episode_id"] == self.EP]
        self.assertEqual(len(clips), 1)
        clip = clips[0]

        # waveform: 24 loudness bars stored on the clip
        self.assertIsInstance(clip.get("waveform"), list)
        self.assertEqual(len(clip["waveform"]), 24)
        for value in clip["waveform"]:
            self.assertIsInstance(value, (int, float))
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)

        # upload pack contract
        pack = clip["pack"]
        self.assertEqual(len(pack["titles"]), 3)
        self.assertLessEqual(len(pack["hashtags"]), 12)
        self.assertIn("#shorts", [tag.lower() for tag in pack["hashtags"]])
        self.assertIn("🎙", pack["description"])
        self.assertIn("⏱", pack["description"])

        # render stored for rerender base
        self.assertIn("captions_pos", clip["render"])
        self.assertIn("captions_box", clip["render"])

        # download endpoints
        status, raw, headers = self.get(f"/api/clips/{clip['id']}/file")
        self.assertEqual(status, 200)
        self.assertIn("video/mp4", headers["Content-Type"])
        self.assertGreater(len(raw), 10_000)
        # v0.6.6: the card's download link asks for an attachment (Android
        # WebViews ignore a link's `download` attribute), while the URL the
        # <video> element uses stays inline and playable.
        inline = headers.get("Content-Disposition", "")
        status, raw, headers = self.get(f"/api/clips/{clip['id']}/file?dl=1")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers.get("Content-Disposition", ""))
        self.assertNotIn("attachment", inline)
        # …and a short cut from synthetic demo media is labelled as such
        self.assertEqual(clip.get("source"), "demo")
        self.assertTrue(clip.get("demo_media"),
                        "a demo render must be flagged on the clip itself")
        marker = self.data_dir / "media" / "demo-chhetri-223.mp4.qyro-demo.json"
        self.assertTrue(marker.is_file(),
                        "the placeholder must be recognisable on disk")
        status, raw, headers = self.get(f"/api/clips/{clip['id']}/thumb")
        self.assertEqual(status, 200)
        self.assertIn("image/jpeg", headers["Content-Type"])
        status, raw, headers = self.get(f"/api/clips/{clip['id']}/srt")
        self.assertEqual(status, 200)
        self.assertIn("--> ", raw.decode("utf-8"))

        # episode marked done
        eps = {ep["id"]: ep for ep in self.get("/api/state")[1]["episodes"]}
        self.assertEqual(eps[self.EP]["status"], "done")

    def test_double_shorts_409_while_processing(self):
        # occupy the single worker with a real render
        status, body, _ = self.post(
            f"/api/episodes/{self.EP}/shorts",
            {"count": 1, "min_dur": 20, "max_dur": 30},
        )
        self.assertEqual(status, 200)
        job1 = body["job_id"]
        # second job on the SAME episode → 409
        status, body, _ = self.post(
            f"/api/episodes/{self.EP}/shorts", {"count": 1}
        )
        self.assertEqual(status, 409)
        job = self.wait_job(job1)
        self.assertEqual(job["status"], "done")

    def test_error_codes(self):
        self.assertEqual(
            self.post("/api/episodes/ghost/shorts", {"count": 1})[0], 404
        )
        self.assertEqual(
            self.post(f"/api/episodes/{self.EP}/shorts", {"speed": True})[0], 422
        )
        self.assertEqual(
            self.post(f"/api/episodes/{self.EP}/shorts", {"count": 13})[0], 422
        )
        self.assertEqual(
            self.post(f"/api/episodes/{self.EP}/shorts", {"filters": 1})[0], 422
        )
        body = self.post(f"/api/episodes/{self.EP}/shorts", {"filters": 1})[1]
        self.assertEqual(body["detail"], "Unknown options")
        self.assertEqual(
            self.post(f"/api/episodes/{self.EP}/manual", {"start": 0, "end": 2})[0], 422
        )

    def test_preview_shape(self):
        status, body, _ = self.post(
            f"/api/episodes/{self.EP}/preview",
            {"count": 2, "min_dur": 20, "max_dur": 60, "profile": "viral"},
        )
        self.assertEqual(status, 200)
        self.assertIn("stats", body)
        for moment in body["moments"]:
            for key in ("start", "end", "duration", "title", "score", "reasons", "breakdown"):
                self.assertIn(key, moment)

    def test_transcript_and_chapters(self):
        status, body, _ = self.get(f"/api/episodes/{self.EP}/transcript")
        self.assertEqual(status, 200)
        self.assertGreater(len(body["segments"]), 0)
        status, body, _ = self.get(f"/api/episodes/{self.EP}/chapters")
        self.assertEqual(status, 200)
        for chapter in body["chapters"]:
            self.assertIn("start", chapter)
            self.assertIn("title", chapter)

    def test_export_csv(self):
        status, text, headers = self.get(
            f"/api/episodes/{self.EP}/export?count=8&profile=viral"
        )
        self.assertEqual(status, 200)
        self.assertIn("text/csv", headers["Content-Type"])
        self.assertIn("qyro-demo-chhetri-223-moments.csv",
                      headers["Content-Disposition"])
        lines = text.decode("utf-8").strip().splitlines()
        self.assertEqual(lines[0], "start,end,duration,title,score,reasons")
        self.assertGreater(len(lines) - 1, 0)
        self.assertEqual(
            self.get(f"/api/episodes/{self.EP}/export?count=0")[0], 422
        )
        self.assertEqual(
            self.get(f"/api/episodes/{self.EP}/export?profile=nope")[0], 422
        )
        self.assertEqual(self.get("/api/episodes/ghost/export")[0], 404)

    def test_search(self):
        status, body, _ = self.get("/api/search?q=fear")
        self.assertEqual(status, 200)
        hits = body["results"]
        self.assertGreater(len(hits), 0)
        for hit in hits:
            self.assertEqual(
                set(hit.keys()),
                {"episode_id", "episode_title", "start", "end", "text"},
            )
        self.assertEqual(self.get("/api/search?q=f")[0], 422)
        status, body, _ = self.get("/api/search?q=zzzznothing")
        self.assertEqual((status, body["results"]), (200, []))

    def test_batch_queues_all_new(self):
        status, body, _ = self.post("/api/batch", {
            "count": 1, "min_dur": 20, "max_dur": 25,
        })
        self.assertEqual(status, 200)
        self.assertEqual(body["queued"], 3)  # every demo episode is new
        # wait for all jobs (real renders)
        for entry in body["jobs"]:
            job = self.wait_job(entry["job_id"], timeout=600)
            self.assertIn(job["status"], ("done", "error"))

    def test_cancel_flow(self):
        # occupy the worker with a real render, then queue a second
        job1 = self.post(
            f"/api/episodes/{self.EP}/shorts", {"count": 1, "min_dur": 20, "max_dur": 25}
        )[1]["job_id"]
        other = "demo-diljit-215"
        job2 = self.post(f"/api/episodes/{other}/shorts", {"count": 1})[1]["job_id"]

        # job1 running (wait for it), job2 queued
        deadline = time.time() + 60
        while time.time() < deadline:
            jobs = {job["id"]: job for job in self.get("/api/jobs?limit=50")[1]["jobs"]}
            if jobs.get(job1, {}).get("status") == "running":
                break
            time.sleep(0.3)
        status, body, _ = self.post(f"/api/jobs/{job2}/cancel")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"cancelled": job2})
        status, body, _ = self.post(f"/api/jobs/{job2}/cancel")
        self.assertEqual(status, 409)          # already cancelled → not queued
        self.assertEqual(self.post("/api/jobs/ghostjob/cancel")[0], 404)

        job = self.wait_job(job1)
        self.assertEqual(job["status"], "done")
        # job2 was cancelled and never ran
        jobs = {job["id"]: job for job in self.get("/api/jobs?limit=50")[1]["jobs"]}
        self.assertEqual(jobs[job2]["status"], "cancelled")
        state = self.get("/api/state")[1]
        ep = {e["id"]: e for e in state["episodes"]}[other]
        self.assertEqual(ep["status"], "new")  # reset after cancel
        self.assertEqual([c for c in state["clips"] if c["episode_id"] == other], [])

    def test_rename_and_rerender(self):
        # make one clip first
        job = self.post(
            f"/api/episodes/{self.EP}/shorts",
            {"count": 1, "min_dur": 20, "max_dur": 30},
        )[1]["job_id"]
        self.wait_job(job)
        clip = [c for c in self.get("/api/state")[1]["clips"]
                if c["episode_id"] == self.EP][0]
        cid = clip["id"]

        # rename
        status, body, _ = self.post(f"/api/clips/{cid}/rename", {"title": "  Renamed clip "})
        self.assertEqual(status, 200)
        self.assertEqual(body, {"id": cid, "title": "Renamed clip"})
        self.assertEqual(self.post(f"/api/clips/{cid}/rename", {"title": ""})[0], 422)
        self.assertEqual(self.post(f"/api/clips/{cid}/rename", {})[0], 422)
        self.assertEqual(self.post(f"/api/clips/{cid}/rename", {"title": "x" * 121})[0], 422)
        self.assertEqual(self.post("/api/clips/ghost/rename", {"title": "ok"})[0], 404)

        # rerender: 409 while the episode would be busy — queue one first
        status, body, _ = self.post(
            f"/api/clips/{cid}/rerender", {"style": "smart", "captions": "pop"}
        )
        self.assertEqual(status, 200)
        rerender_job = body["job_id"]
        self.assertEqual(body["options"]["style"], "smart")
        job = self.wait_job(rerender_job)
        self.assertEqual(job["status"], "done")
        state = self.get("/api/state")[1]
        clips = [c for c in state["clips"] if c["episode_id"] == self.EP]
        self.assertEqual(len(clips), 2)  # original + rerender
        rerendered = [c for c in clips if c["id"] != cid][0]
        self.assertEqual(rerendered["style"], "smart")
        # unknown options
        self.assertEqual(
            self.post(f"/api/clips/{cid}/rerender", {"count": 3})[0], 422
        )
        self.assertEqual(
            self.post("/api/clips/ghost/rerender", {})[0], 404
        )

    def test_polish_503(self):
        job = self.post(
            f"/api/episodes/{self.EP}/shorts",
            {"count": 1, "min_dur": 20, "max_dur": 30},
        )[1]["job_id"]
        self.wait_job(job)
        clip = [c for c in self.get("/api/state")[1]["clips"]
                if c["episode_id"] == self.EP][0]
        status, body, _ = self.post(f"/api/clips/{clip['id']}/polish")
        if body.get("detail") and "No LLM" in body["detail"]:
            self.assertEqual(status, 503)
        else:
            self.assertEqual(status, 200)  # an LLM was configured

    def test_delete_episode(self):
        ep = "demo-dig-221"
        job = self.post(
            f"/api/episodes/{ep}/shorts", {"count": 1, "min_dur": 20, "max_dur": 25}
        )[1]["job_id"]
        self.wait_job(job)
        clip = [c for c in self.get("/api/state")[1]["clips"] if c["episode_id"] == ep][0]
        file_url = f"/api/clips/{clip['id']}/file"
        self.assertEqual(self.get(file_url)[0], 200)

        status, body, _ = self.delete(f"/api/episodes/{ep}")
        self.assertEqual(status, 200)
        self.assertEqual(body["deleted"], ep)
        self.assertEqual(body["clips_removed"], 1)
        self.assertEqual(self.get(file_url)[0], 404)          # file gone
        self.assertEqual(self.delete(f"/api/episodes/{ep}")[0], 404)
        self.assertEqual(self.get(f"/api/episodes/{ep}/transcript")[0], 404)

    def test_delete_clip(self):
        job = self.post(
            f"/api/episodes/{self.EP}/shorts",
            {"count": 1, "min_dur": 20, "max_dur": 25},
        )[1]["job_id"]
        self.wait_job(job)
        clip = [c for c in self.get("/api/state")[1]["clips"]
                if c["episode_id"] == self.EP][0]
        status, body, _ = self.delete(f"/api/clips/{clip['id']}")
        self.assertEqual((status, body), (200, {"deleted": clip["id"]}))
        self.assertEqual(self.delete(f"/api/clips/{clip['id']}")[0], 404)

    def test_zip_download(self):
        status, raw, headers = self.get("/api/clips/zip?episode_id=demo-chhetri-223")
        if status == 404:
            job = self.post(
                f"/api/episodes/{self.EP}/shorts",
                {"count": 1, "min_dur": 20, "max_dur": 25},
            )[1]["job_id"]
            self.wait_job(job)
            status, raw, headers = self.get("/api/clips/zip?episode_id=demo-chhetri-223")
        self.assertEqual(status, 200)
        self.assertIn("application/zip", headers["Content-Type"])
        self.assertIsInstance(raw, bytes)
        archive = zipfile.ZipFile(BytesIO(raw))
        self.assertTrue(archive.namelist())
        self.assertTrue(all(name.endswith(".mp4") for name in archive.namelist()))

    def test_storage_and_clean(self):
        status, body, _ = self.get("/api/storage")
        self.assertEqual(status, 200)
        self.assertIn("dirs", body)
        self.assertEqual(
            self.post("/api/storage/clean", {"target": "galaxy"})[0], 422
        )
        status, body, _ = self.post("/api/storage/clean", {"target": "thumbs"})
        self.assertEqual(status, 200)
        self.assertIn("removed", body)
        self.assertIn("freed_bytes", body)

    def test_backup_restore(self):
        status, state, _ = self.get("/api/backup")
        self.assertEqual(status, 200)
        for key in ("settings", "episodes", "clips", "jobs"):
            self.assertIn(key, state)
        status, body, _ = self.post("/api/restore", state)
        self.assertEqual(status, 200)
        self.assertEqual(body["episodes"], 3)
        self.assertEqual(self.post("/api/restore", [1])[0], 422)


if __name__ == "__main__":
    unittest.main()
