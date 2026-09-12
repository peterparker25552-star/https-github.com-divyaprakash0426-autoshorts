"""v0.6.7 HTTP surface: the YouTube session endpoints and the health slice
that tells the UI a subtitle block is standing.

Both servers are exercised — FastAPI in-process and the stdlib server as a
real subprocess — because the Termux path is the one a phone user hits, and a
session that can only be installed through a shell is no fix at all.
"""
from __future__ import annotations

import base64
import shutil
import tempfile
import unittest
from pathlib import Path

from autoshorts import config, maintenance, youtube
from autoshorts.server import app, pipeline, store
from fastapi.testclient import TestClient

from . import util

COOKIES = (
    "# Netscape HTTP Cookie File\n"
    ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tabc123\n"
    ".youtube.com\tTRUE\t/\tTRUE\t0\tLOGIN_INFO\tdef456\n"
)


class _CookiesSandbox(unittest.TestCase):
    """Point COOKIES_FILE / RATE_LIMIT_STATE at a temp dir for each test."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        for name in ("COOKIES_FILE", "RATE_LIMIT_STATE"):
            self.addCleanup(setattr, config, name, getattr(config, name))
        config.COOKIES_FILE = self.dir / "cookies.txt"
        config.RATE_LIMIT_STATE = self.dir / ".rate-limit.json"

    def b64(self, text: str = COOKIES) -> str:
        return base64.b64encode(text.encode("utf-8")).decode("ascii")


class FastApiCookiesTests(_CookiesSandbox):
    def setUp(self):
        super().setUp()
        util.fresh_state(store)
        self.addCleanup(util.fresh_state, store)
        self.original_run = pipeline._run_job
        pipeline._run_job = lambda job_id: store.update_job(
            job_id, status="done", progress=1.0, step="done"
        )
        self.addCleanup(setattr, pipeline, "_run_job", self.original_run)
        self.client = TestClient(app, raise_server_exceptions=False)

    def test_get_then_post_then_delete(self):
        body = self.client.get("/api/cookies").json()
        self.assertFalse(body["present"])
        self.assertEqual(body["lines"], 0)

        posted = self.client.post(
            "/api/cookies", json={"name": "cookies.txt", "data_b64": self.b64()}
        )
        self.assertEqual(posted.status_code, 200, posted.text)
        self.assertTrue(posted.json()["present"])
        self.assertEqual(posted.json()["lines"], 2)

        body = self.client.get("/api/cookies").json()
        self.assertTrue(body["present"])
        self.assertNotIn("abc123", posted.text,
                         "cookie values must never be echoed back")

        removed = self.client.delete("/api/cookies")
        self.assertEqual(removed.status_code, 200)
        self.assertFalse(removed.json()["present"])

    def test_a_junk_file_is_rejected_with_422(self):
        response = self.client.post(
            "/api/cookies", json={"data_b64": self.b64("just some text")}
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("cookies.txt", response.json()["detail"])
        self.assertFalse(config.COOKIES_FILE.exists())

    def test_health_reports_the_session_and_any_block(self):
        body = self.client.get("/api/health").json()
        self.assertIn("youtube", body)
        self.assertFalse(body["youtube"]["cookies"]["present"])
        self.assertFalse(body["youtube"]["rate_limit"]["blocked"])

        youtube._mark_rate_limited(600)
        body = self.client.get("/api/health").json()
        self.assertTrue(body["youtube"]["rate_limit"]["blocked"])
        self.assertGreater(body["youtube"]["rate_limit"]["minutes"], 0)

    def test_installing_a_session_lifts_the_block_it_cures(self):
        youtube._mark_rate_limited(600)
        self.assertTrue(
            self.client.get("/api/health").json()["youtube"]["rate_limit"]["blocked"]
        )
        response = self.client.post(
            "/api/cookies", json={"data_b64": self.b64()}
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            self.client.get("/api/health").json()["youtube"]["rate_limit"]["blocked"]
        )


class StdlibCookiesTests(_CookiesSandbox):
    """The Termux path: a real HTTP server, a real uploaded file."""

    httpd = None
    port = None
    data_dir = None

    @classmethod
    def setUpClass(cls):
        cls.data_dir = Path(tempfile.mkdtemp(prefix="as-test-v067-"))
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
        super().setUp()
        self.base = f"http://127.0.0.1:{self.port}"
        # The server is a separate process with its own config: it writes to
        # the data dir it was started with, not to this test's patched path.
        self.server_cookies = Path(self.data_dir) / "cookies.txt"

    def tearDown(self):
        self.server_cookies.unlink(missing_ok=True)

    def test_the_session_round_trip_over_real_http(self):
        status, body, _ = util.http_request("GET", f"{self.base}/api/cookies")
        self.assertEqual(status, 200)
        self.assertFalse(body["present"])

        status, body, _ = util.http_request(
            "POST", f"{self.base}/api/cookies",
            {"name": "cookies.txt", "data_b64": self.b64()},
        )
        self.assertEqual(status, 200, body)
        self.assertTrue(body["present"])
        self.assertEqual(self.server_cookies.read_text(encoding="utf-8"), COOKIES)

        status, body, _ = util.http_request("DELETE", f"{self.base}/api/cookies")
        self.assertEqual(status, 200)
        self.assertFalse(body["present"])
        self.assertFalse(self.server_cookies.exists())

    def test_health_carries_the_youtube_slice(self):
        status, body, _ = util.http_request("GET", f"{self.base}/api/health")
        self.assertEqual(status, 200)
        self.assertIn("youtube", body)
        self.assertIn("cookies", body["youtube"])
        self.assertIn("rate_limit", body["youtube"])

    def test_a_junk_upload_is_rejected_over_real_http(self):
        status, body, _ = util.http_request(
            "POST", f"{self.base}/api/cookies",
            {"data_b64": self.b64("nope")},
        )
        self.assertEqual(status, 422)
        self.assertIn("detail", body)


class WebUiContractTests(unittest.TestCase):
    """The Tools modal must offer the cure the server endpoint provides."""

    def setUp(self):
        self.js = (Path(__file__).resolve().parent.parent / "web" / "app.js").read_text(
            encoding="utf-8"
        )

    def test_the_tools_modal_has_a_session_tab(self):
        self.assertIn('data-tab="session"', self.js)
        self.assertIn('data-panel="session"', self.js)

    def test_the_session_tab_uploads_to_the_endpoint(self):
        self.assertIn("/api/cookies", self.js)

    def test_a_rate_limit_error_offers_the_cure(self):
        self.assertIn("isRateLimitError", self.js)
        self.assertIn("switchToolTab('session')", self.js)

    def test_the_health_strip_shows_a_standing_block(self):
        self.assertIn("block.blocked", self.js)
        self.assertIn("transcripts paused", self.js)


if __name__ == "__main__":
    unittest.main()
