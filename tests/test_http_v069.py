"""v0.6.9 HTTP surface: the diagnosis reaches the UI, on both servers.

The unit tests in ``test_units_v069`` pin *what* a caption failure is now
called. These pin that the name survives the trip to the screen: the health
slice both servers answer must carry the extractor's version and age (a stale
yt-dlp is behind most caption failures Qyro cannot fix itself), and a job that
dies on a transcript must put the reason *and* the remedy in the job error the
dashboard renders — not a shrug that says the episode has no captions.

FastAPI runs in-process; the stdlib server runs as a real subprocess, because
that is the one a Termux phone user hits.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from autoshorts import config, maintenance, youtube
from autoshorts import pipeline as pipeline_module
from autoshorts import server as server_module
from autoshorts.server import app, pipeline, store
from fastapi.testclient import TestClient

from . import util

BOT_CHECK = youtube.TranscriptUnavailable(
    "YouTube refused the caption download with a bot check — “ERROR: "
    "[youtube] vid1: Sign in to confirm you're not a bot”. Fix: add a "
    "signed-in cookies.txt in Tools ▸ YouTube session (or save it as "
    "data/cookies.txt), then press Retry."
)
NO_CAPTIONS = "No captions available for this episode"


def _settle(job_id: str, timeout: float = 30.0) -> dict:
    """Wait for the worker to finish a job and return its final record."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = store.job(job_id) or {}
        if job.get("status") in ("done", "error", "cancelled"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never settled")


class _FastApi(unittest.TestCase):
    def setUp(self):
        util.fresh_state(store)
        self.addCleanup(util.fresh_state, store)
        self.addCleanup(setattr, maintenance, "_ytdlp_health_cache",
                        maintenance._ytdlp_health_cache)
        maintenance._ytdlp_health_cache = (0.0, {})
        # Generate refuses a YouTube episode when the reachability probe says
        # YouTube is down; these tests are about what happens *after* that gate.
        self.addCleanup(setattr, server_module, "youtube_reachable",
                        server_module.youtube_reachable)
        server_module.youtube_reachable = lambda ttl=60.0: True
        self.client = TestClient(app, raise_server_exceptions=False)

    def _episode(self, ep_id: str = "vid1") -> str:
        store.upsert_episode({
            "id": ep_id, "title": "Episode 1", "duration": 600,
            "url": f"https://www.youtube.com/watch?v={ep_id}",
            "channel": "Test", "status": "new",
        })
        return ep_id

    # -- health ----------------------------------------------------------
    def test_health_carries_the_installed_extractor(self):
        body = self.client.get("/api/health").json()
        yt_dlp = body["youtube"]["yt_dlp"]
        self.assertIn("version", yt_dlp)
        self.assertIn("age_days", yt_dlp)
        self.assertIn("stale", yt_dlp)
        self.assertEqual(yt_dlp["stale_days"], config.YTDLP_STALE_DAYS)

    def test_health_flags_an_extractor_youtube_has_outrun(self):
        original = youtube.ytdlp_version
        youtube.ytdlp_version = lambda: "2024.04.09"
        self.addCleanup(setattr, youtube, "ytdlp_version", original)
        maintenance._ytdlp_health_cache = (0.0, {})
        yt_dlp = self.client.get("/api/health").json()["youtube"]["yt_dlp"]
        self.assertTrue(yt_dlp["stale"])
        self.assertGreater(yt_dlp["age_days"], config.YTDLP_STALE_DAYS)

    def test_the_session_and_block_slice_is_untouched(self):
        body = self.client.get("/api/health").json()["youtube"]
        self.assertIn("cookies", body)
        self.assertIn("rate_limit", body)

    # -- the job error the dashboard renders ------------------------------
    def test_a_refused_transcript_arrives_with_its_reason_and_cure(self):
        ep_id = self._episode()
        original = youtube.get_transcript

        def refuse(*args, **kwargs):
            raise BOT_CHECK

        youtube.get_transcript = refuse
        self.addCleanup(setattr, youtube, "get_transcript", original)

        response = self.client.post(f"/api/episodes/{ep_id}/shorts",
                                    json={"count": 1})
        self.assertEqual(response.status_code, 200, response.text)
        job = _settle(response.json()["job_id"])

        self.assertEqual(job["status"], "error")
        self.assertTrue(job["error"].startswith("Transcript unavailable:"),
                        job["error"])
        self.assertIn("bot check", job["error"].lower())
        self.assertIn("cookies.txt", job["error"],
                      "the remedy must survive into the job error")
        self.assertNotIn(NO_CAPTIONS, job["error"])
        # The card shows the episode error too, and that is what offers the
        # "Fix this — add a YouTube session" button.
        episode = store.get_episode(ep_id)
        self.assertEqual(episode["status"], "error")
        self.assertIn("cookies.txt", episode["error"])
        self.assertLessEqual(len(job["error"]),
                             pipeline_module.ERROR_MESSAGE_LIMIT)

    def test_the_whole_diagnosis_fits_the_job_error(self):
        """A message truncated before its remedy is no remedy at all."""
        ep_id = self._episode()
        original = youtube.get_transcript
        long_refusal = youtube.TranscriptUnavailable(
            youtube._refused_message(
                "WARNING: [youtube] Some web client subtitles require a PO "
                "Token which was not provided. They will be discarded since "
                "they are not downloadable as-is.",
                {"languages": ["en", "hi", "en-orig"], "manual": []},
                clients=config.TRANSCRIPT_MAX_PASSES,
            )
        )

        def refuse(*args, **kwargs):
            raise long_refusal

        youtube.get_transcript = refuse
        self.addCleanup(setattr, youtube, "get_transcript", original)

        response = self.client.post(f"/api/episodes/{ep_id}/shorts",
                                    json={"count": 1})
        job = _settle(response.json()["job_id"])
        self.assertNotIn(NO_CAPTIONS, job["error"])
        self.assertIn("cookies.txt", job["error"])
        self.assertIn("does have", job["error"])
        self.assertFalse(job["error"].endswith("…"),
                         f"the message was cut short: {job['error'][-60:]}")

    def test_the_transcript_tool_reports_the_same_diagnosis(self):
        """The Transcript cutter hits the same wall and must say the same thing."""
        ep_id = self._episode()
        original = youtube.get_transcript

        def refuse(*args, **kwargs):
            raise BOT_CHECK

        youtube.get_transcript = refuse
        self.addCleanup(setattr, youtube, "get_transcript", original)
        response = self.client.get(f"/api/episodes/{ep_id}/transcript")
        self.assertEqual(response.status_code, 502)
        detail = response.json()["detail"]
        self.assertIn("bot check", detail.lower())
        self.assertIn("cookies.txt", detail)
        self.assertNotIn(NO_CAPTIONS, detail)


class StdlibHealthTests(unittest.TestCase):
    """The Termux path: a real subprocess, same answers."""

    def test_the_stdlib_server_reports_the_extractor_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            port = util.free_port()
            proc = util.spawn_server(port, Path(tmp))
            try:
                util.wait_http_ready(port)
                status, body, _headers = util.http_request(
                    "GET", f"http://127.0.0.1:{port}/api/health")
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except Exception:                    # noqa: BLE001
                    proc.kill()
        self.assertEqual(status, 200)
        yt_dlp = body["youtube"]["yt_dlp"]
        self.assertIn("version", yt_dlp)
        self.assertIn("stale", yt_dlp)
        self.assertIn("rate_limit", body["youtube"])


if __name__ == "__main__":
    unittest.main()
