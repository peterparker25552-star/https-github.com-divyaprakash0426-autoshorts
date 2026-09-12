"""Qyro v0.6.7 — the YouTube 429 path stops being a dead end.

The bug this file exists for: a user picks a quality, presses *Generate*, and
gets ``Transcript unavailable: YouTube is still rate-limiting subtitle
downloads (HTTP 429). Retry in about 8 minute(s)`` — wait eight minutes,
generate again, and get the same thing. The old code could not get out of its
own way:

* it retried the **same blocked player client** three times with 30 s/60 s
  backoffs, which fed the block instead of escaping it;
* it read the 429 off the **last stderr line only**, so real blocks were
  reported as "No captions available for this episode";
* it threw away a caption file **already on disk** from an interrupted run and
  went back to YouTube for it — while claiming "transcripts are cached";
* it checked the cooldown **before** looking on disk, so a transcript Qyro
  already held was gated behind a wait;
* it recorded a cooldown only when *every* pass 429'd, and never cleared it,
  so one unlucky block became an open-ended 10-minute loop;
* ``download_video`` had no 429 handling at all;
* and the only lasting cure — a signed-in ``cookies.txt`` — could not be
  installed without shell access to ``data/``.

Every test below pins one of those. None of them touch the network.
"""
from __future__ import annotations

import base64
import json
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from autoshorts import config, maintenance, youtube
from autoshorts.maintenance import ServiceError
from autoshorts.transcripts import Segment

# A minimal but real WebVTT caption track, so the parser does actual work.
VTT = """WEBVTT

00:00:00.000 --> 00:00:02.500
The truth is, I had cried in the dressing room.

00:00:02.500 --> 00:00:05.000
Nobody tells you this about sport.

00:00:05.000 --> 00:00:08.000
The body gives up before the mind does.
"""

RATE_LIMITED = RuntimeError(
    "yt-dlp failed: ERROR: unable to download video subtitles for 'en': "
    "HTTP Error 429: Too Many Requests"
)


class _Sandbox(unittest.TestCase):
    """Redirect every path the 429 code touches into a temp data dir."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        for name in ("COOKIES_FILE", "RATE_LIMIT_STATE", "SUBS_DIR", "MEDIA_DIR"):
            self.addCleanup(setattr, config, name, getattr(config, name))
        config.COOKIES_FILE = self.dir / "cookies.txt"
        config.RATE_LIMIT_STATE = self.dir / ".rate-limit.json"
        config.SUBS_DIR = self.dir / "subs"
        config.MEDIA_DIR = self.dir / "media"
        config.SUBS_DIR.mkdir(parents=True, exist_ok=True)
        config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        self.addCleanup(setattr, config, "YTDLP_PLAYER_CLIENT",
                        config.YTDLP_PLAYER_CLIENT)
        config.YTDLP_PLAYER_CLIENT = ""

    # -- helpers ---------------------------------------------------------
    def fake_ytdlp(self, results):
        """Return a ``run_ytdlp`` stub yielding ``results`` in order.

        Each entry is an exception to raise, ``"write"`` for a call that
        succeeds *and* leaves a caption file behind (what yt-dlp really does),
        or ``None`` for a call that succeeds and produces nothing. The stub
        records the player client each call was made with, which is how the
        rotation tests prove a retry went somewhere new.
        """
        calls: list[dict] = []
        pending = list(results)

        def stub(args, timeout=600, retries=3):
            args = list(args)
            client = ""
            for index, flag in enumerate(args):
                if flag == "--extractor-args" and index + 1 < len(args):
                    client = str(args[index + 1]).split("=", 1)[-1]
            calls.append({"client": client, "args": args, "retries": retries})
            outcome = pending.pop(0) if pending else None
            if isinstance(outcome, BaseException):
                raise outcome
            if outcome == "write":
                # yt-dlp writes to the -o prefix; mimic that naming
                prefix = "vid1"
                for index, flag in enumerate(args):
                    if flag == "-o" and index + 1 < len(args):
                        prefix = Path(str(args[index + 1])).name
                (config.SUBS_DIR / f"{prefix}.en.vtt").write_text(
                    VTT, encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, "", "")

        self.calls = calls
        original = youtube.run_ytdlp
        youtube.run_ytdlp = stub
        self.addCleanup(setattr, youtube, "run_ytdlp", original)
        # never actually sleep in a test suite
        self.addCleanup(setattr, youtube, "_pace", youtube._pace)
        youtube._pace = lambda: None
        self.addCleanup(setattr, youtube, "_RATE_LIMIT_BACKOFF",
                        youtube._RATE_LIMIT_BACKOFF)
        youtube._RATE_LIMIT_BACKOFF = 0.0
        return calls

    def write_subtitle(self, video_id: str = "vid1", text: str = VTT) -> Path:
        path = config.SUBS_DIR / f"{video_id}.en.vtt"
        path.write_text(text, encoding="utf-8")
        return path

    def cache_path(self, video_id: str = "vid1") -> Path:
        return config.SUBS_DIR / f"{video_id}.segments.json"


# --------------------------------------------------------------------------
# 1. Recognizing a 429 at all
# --------------------------------------------------------------------------
class RateLimitDetectionTests(unittest.TestCase):
    def test_the_standard_youtube_message_is_a_block(self):
        self.assertTrue(youtube.is_rate_limit_error(
            "yt-dlp failed: ERROR: HTTP Error 429: Too Many Requests"))

    def test_a_bare_too_many_requests_is_a_block(self):
        self.assertTrue(youtube.is_rate_limit_error("Too Many Requests"))

    def test_an_unrelated_failure_is_not_a_block(self):
        self.assertFalse(youtube.is_rate_limit_error(
            "yt-dlp failed: ERROR: Video unavailable"))
        self.assertFalse(youtube.is_rate_limit_error(""))

    def test_a_duration_that_mentions_429_is_not_a_block(self):
        # "429" as a standalone number inside prose must not read as HTTP 429
        self.assertFalse(youtube.is_rate_limit_error(
            "yt-dlp failed: the episode runs 429 seconds"))

    def test_run_ytdlp_raises_a_subclass_callers_can_branch_on(self):
        with unittest.mock.patch.object(
            youtube.subprocess, "run",
            return_value=subprocess.CompletedProcess(
                [], 1, "ERROR: HTTP Error 429: Too Many Requests", ""),
        ):
            with self.assertRaises(youtube.RateLimited):
                youtube.run_ytdlp(["-J", "https://x"])
        # still a RuntimeError, so every existing handler keeps working
        self.assertTrue(issubclass(youtube.RateLimited, RuntimeError))

    def test_a_429_only_in_stdout_is_still_caught(self):
        # --no-warnings can leave the useful copy on stdout
        with unittest.mock.patch.object(
            youtube.subprocess, "run",
            return_value=subprocess.CompletedProcess(
                [], 1, "WARNING: falling back\n",
                "ERROR: unable to download subtitles: HTTP Error 429"),
        ):
            with self.assertRaises(youtube.RateLimited):
                youtube.run_ytdlp(["-J", "https://x"])


# --------------------------------------------------------------------------
# 2. Player-client rotation — the part that actually escapes a block
# --------------------------------------------------------------------------
class PlayerClientTests(_Sandbox):
    def test_the_default_chain_walks_several_clients(self):
        chain = youtube.player_client_chain()
        # v0.6.9 moved the first pass off a pinned client: an empty entry
        # means "no --extractor-args at all", so the *installed* yt-dlp picks
        # with a list maintained against a YouTube that changes weekly. Pinning
        # ``web`` first — what v0.6.7 did — overrode that judgement and chose
        # the client YouTube most often refuses anonymous captions on.
        self.assertEqual(chain[0], "")
        self.assertGreater(len(chain), 1)
        self.assertIn("web", chain, "the pinned clients still run, as fallbacks")

    def test_a_configured_client_is_tried_first_without_duplicates(self):
        config.YTDLP_PLAYER_CLIENT = "tv,web_safari"
        chain = youtube.player_client_chain()
        self.assertEqual(chain[:2], ["tv", "web_safari"])
        self.assertEqual(len(chain), len(set(chain)))
        self.assertIn("web", chain)

    def test_a_block_on_one_client_moves_to_the_next(self):
        calls = self.fake_ytdlp([RATE_LIMITED, "write"])
        segments, source = youtube.get_transcript(
            "vid1", "https://youtube.com/watch?v=vid1")
        self.assertTrue(segments)
        # The retry must land somewhere *different* — that is the whole point
        # of the rotation. v0.6.7 asserted the concrete pair ("web", "mweb");
        # v0.6.9 leads with yt-dlp's own default, so assert the movement.
        clients = [call["client"] for call in calls]
        self.assertEqual(len(clients), 2)
        self.assertNotEqual(clients[0], clients[1])
        self.assertEqual(clients[0], "", "the first pass pins no client")
        self.assertIn("vtt", source)
        self.assertIsNone(youtube._rate_limit_remaining(),
                          "a client that worked means there is no block")

    def test_every_client_blocked_records_a_cooldown(self):
        self.fake_ytdlp([RATE_LIMITED] * len(youtube.player_client_chain()))
        with self.assertRaises(youtube.TranscriptUnavailable) as caught:
            youtube.get_transcript("vid1", "https://youtube.com/watch?v=vid1")
        self.assertIn("rate-limiting", str(caught.exception))
        self.assertIn("cookies.txt", str(caught.exception))
        self.assertIsNotNone(youtube._rate_limit_remaining(),
                             "a real block must be remembered for the next run")

    def test_a_pass_without_a_429_does_not_burn_the_whole_chain(self):
        # No captions anywhere: one clean pass proves a second client would
        # only repeat the same answer.
        calls = self.fake_ytdlp([None])
        with self.assertRaises(youtube.TranscriptUnavailable) as caught:
            youtube.get_transcript("vid1", "https://youtube.com/watch?v=vid1")
        self.assertIn("No captions available", str(caught.exception))
        # One language per request (that is deliberate), but every one of them
        # on the *same* client: a clean pass proves a second client would only
        # repeat the same "no captions" answer. v0.6.9 does check that answer
        # against the episode's own metadata (one extra ``-J`` request), so
        # only the *subtitle* passes are pinned to a single client here.
        passes = [call for call in calls if "--write-subs" in call["args"]]
        self.assertTrue(passes)
        self.assertEqual({call["client"] for call in passes}, {""})
        self.assertIsNone(youtube._rate_limit_remaining(),
                          "no 429 means no cooldown")

    def test_subtitle_passes_do_not_let_ytdlp_retry_the_block_away(self):
        self.fake_ytdlp([None])
        with self.assertRaises(youtube.TranscriptUnavailable):
            youtube.get_transcript("vid1", "https://youtube.com/watch?v=vid1")
        self.assertEqual(self.calls[0]["retries"], youtube._SUBTITLE_RETRIES)
        self.assertLess(youtube._SUBTITLE_RETRIES, 3)


# --------------------------------------------------------------------------
# 3. Work already on disk is never thrown away
# --------------------------------------------------------------------------
class DiskRecoveryTests(_Sandbox):
    def test_a_leftover_caption_file_is_used_without_a_request(self):
        self.write_subtitle()
        self.fake_ytdlp([])          # any call at all is a failure here
        segments, source = youtube.get_transcript(
            "vid1", "https://youtube.com/watch?v=vid1")
        self.assertEqual(len(segments), 3)
        self.assertIn("dressing room", segments[0].text)
        self.assertEqual(self.calls, [],
                         "a transcript on disk must not cost a request")
        self.assertIn("vtt", source)

    def test_recovery_normalizes_the_cache_for_later_runs(self):
        self.write_subtitle()
        self.fake_ytdlp([])
        youtube.get_transcript("vid1", "https://youtube.com/watch?v=vid1")
        cached = youtube.load_cached_transcript("vid1")
        self.assertEqual(len(cached), 3)
        self.assertTrue(self.cache_path().is_file())
        self.assertEqual(list(config.SUBS_DIR.glob("vid1*.vtt")), [],
                         "raw caption files are dropped once normalized")

    def test_recovery_wins_over_an_active_cooldown(self):
        # The promise in the error message — "no progress is lost" — only
        # holds if a transcript we already hold is not gated behind a wait.
        youtube._mark_rate_limited(600)
        self.write_subtitle()
        self.fake_ytdlp([])
        segments, _source = youtube.get_transcript(
            "vid1", "https://youtube.com/watch?v=vid1")
        self.assertTrue(segments)

    def test_an_unparseable_leftover_file_is_ignored_not_fatal(self):
        (config.SUBS_DIR / "vid1.en.vtt").write_text("not a caption file at all\n",
                                                     encoding="utf-8")
        self.fake_ytdlp([None])
        with self.assertRaises(youtube.TranscriptUnavailable):
            youtube.get_transcript("vid1", "https://youtube.com/watch?v=vid1")
        self.assertFalse(self.cache_path().exists())


# --------------------------------------------------------------------------
# 4. The cooldown: escalates, clears, and never blocks work we already have
# --------------------------------------------------------------------------
class CooldownTests(_Sandbox):
    def test_an_active_cooldown_short_circuits_without_a_request(self):
        self.fake_ytdlp([])
        youtube._mark_rate_limited(60)
        with self.assertRaises(youtube.TranscriptUnavailable) as caught:
            youtube.get_transcript("vid1", "https://youtube.com/watch?v=vid1")
        self.assertIn("still rate-limiting", str(caught.exception))
        self.assertIn("minute", str(caught.exception))
        self.assertEqual(self.calls, [], "a blocked retry must not touch YouTube")

    def test_a_second_block_waits_longer_than_the_first(self):
        first = youtube._mark_rate_limited()
        second = youtube._mark_rate_limited()
        self.assertGreater(second, first)

    def test_the_cooldown_is_capped(self):
        for _ in range(len(config.RATE_LIMIT_COOLDOWNS) + 3):
            seconds = youtube._mark_rate_limited()
        self.assertLessEqual(seconds, config.RATE_LIMIT_MAX_COOLDOWN)

    def test_a_successful_fetch_clears_an_expired_block(self):
        # The realistic case: the block has run out (so a fetch is allowed
        # again) but its note is still on disk. Once YouTube answers, the note
        # must go — otherwise the next block would step straight to 20 minutes.
        youtube._mark_rate_limited(-1)
        self.assertIsNone(youtube._rate_limit_remaining(),
                          "an expired note must not block a new attempt")
        self.assertTrue(config.RATE_LIMIT_STATE.exists())
        self.fake_ytdlp(["write"])
        youtube.get_transcript("vid1", "https://youtube.com/watch?v=vid1")
        self.assertFalse(config.RATE_LIMIT_STATE.exists(),
                         "a working request must forget the old block")
        self.assertEqual(youtube._rate_limit_hits(), 0)

    def test_recovering_from_disk_does_not_claim_the_block_is_over(self):
        # No request was made, so nothing proved YouTube is talking again.
        youtube._mark_rate_limited(600)
        self.write_subtitle()
        self.fake_ytdlp([])
        youtube.get_transcript("vid1", "https://youtube.com/watch?v=vid1")
        self.assertIsNotNone(youtube._rate_limit_remaining())

    def test_the_status_dict_is_ui_safe(self):
        self.assertEqual(youtube.rate_limit_status(),
                         {"blocked": False, "minutes": 0})
        youtube._mark_rate_limited(600)
        status = youtube.rate_limit_status()
        self.assertTrue(status["blocked"])
        self.assertGreater(status["minutes"], 0)


# --------------------------------------------------------------------------
# 5. Media downloads: try, don't pre-emptively block
# --------------------------------------------------------------------------
class MediaDownloadTests(_Sandbox):
    def test_a_subtitle_block_does_not_pre_block_a_download(self):
        # Media comes off a different CDN: a caption block must not refuse a
        # download that would have worked.
        youtube._mark_rate_limited(600)
        calls = self.fake_ytdlp([None])
        with self.assertRaises(RuntimeError):
            # no file appears, so download_video reports that — the point is
            # that it tried instead of hiding behind the cooldown
            youtube.download_video("vid1", "https://youtube.com/watch?v=vid1")
        self.assertEqual(len(calls), 1)

    def test_a_media_429_falls_back_to_another_client_then_notes_the_block(self):
        self.fake_ytdlp([RATE_LIMITED, RATE_LIMITED])
        with self.assertRaises(RuntimeError) as caught:
            youtube.download_video("vid1", "https://youtube.com/watch?v=vid1")
        self.assertIn("429", str(caught.exception))
        self.assertIn("cookies.txt", str(caught.exception))
        self.assertEqual(len({call["client"] for call in self.calls}), 2)
        self.assertIsNotNone(youtube._rate_limit_remaining())

    def test_a_non_429_download_failure_is_raised_as_is(self):
        boom = RuntimeError("yt-dlp failed: ERROR: Video unavailable")
        self.fake_ytdlp([boom])
        with self.assertRaises(RuntimeError) as caught:
            youtube.download_video("vid1", "https://youtube.com/watch?v=vid1")
        self.assertEqual(str(caught.exception), str(boom))
        self.assertIsNone(youtube._rate_limit_remaining(),
                          "an unrelated failure must not start a cooldown")


# --------------------------------------------------------------------------
# 6. cookies.txt — the cure, installable from the browser
# --------------------------------------------------------------------------
COOKIES = (
    "# Netscape HTTP Cookie File\n"
    ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tabc123\n"
    ".youtube.com\tTRUE\t/\tTRUE\t0\tLOGIN_INFO\tdef456\n"
)


class CookiesTests(_Sandbox):
    def test_status_reports_absent_and_present_without_the_contents(self):
        self.assertFalse(maintenance.cookies_status()["present"])
        config.COOKIES_FILE.write_text(COOKIES, encoding="utf-8")
        status = maintenance.cookies_status()
        self.assertTrue(status["present"])
        self.assertEqual(status["lines"], 2)
        self.assertNotIn("abc123", json.dumps(status),
                         "cookie values must never leave the server")

    def test_a_browser_export_is_accepted(self):
        result = maintenance.save_cookies(
            {"data_b64": base64.b64encode(COOKIES.encode()).decode()})
        self.assertTrue(result["present"])
        self.assertEqual(result["lines"], 2)
        self.assertEqual(config.COOKIES_FILE.read_text(encoding="utf-8"), COOKIES)

    def test_installing_a_session_clears_a_standing_block(self):
        youtube._mark_rate_limited(600)
        maintenance.save_cookies(
            {"data_b64": base64.b64encode(COOKIES.encode()).decode()})
        self.assertIsNone(youtube._rate_limit_remaining(),
                          "a signed-in session is the cure — stop honouring "
                          "the block recorded for an anonymous client")

    def test_a_non_cookie_file_is_rejected(self):
        with self.assertRaises(ServiceError) as caught:
            maintenance.save_cookies(
                {"data_b64": base64.b64encode(b"hello, not cookies").decode()})
        self.assertEqual(caught.exception.status, 422)
        self.assertFalse(config.COOKIES_FILE.exists())

    def test_bad_base64_and_a_missing_field_are_rejected(self):
        for body in ({}, {"data_b64": "not base64!!"},
                     {"data_b64": base64.b64encode(b"").decode()}):
            with self.assertRaises(ServiceError) as caught:
                maintenance.save_cookies(body)
            self.assertEqual(caught.exception.status, 422)

    def test_clearing_removes_the_session(self):
        maintenance.save_cookies(
            {"data_b64": base64.b64encode(COOKIES.encode()).decode()})
        result = maintenance.clear_cookies()
        self.assertFalse(result["present"])
        self.assertFalse(config.COOKIES_FILE.exists())

    def test_the_status_slice_shapes_for_the_ui(self):
        status = maintenance.youtube_status()
        self.assertIn("cookies", status)
        self.assertIn("rate_limit", status)
        self.assertIn("blocked", status["rate_limit"])


# --------------------------------------------------------------------------
# 7. The message a blocked user actually reads
# --------------------------------------------------------------------------
class MessageTests(_Sandbox):
    def test_the_block_message_says_how_to_fix_it(self):
        self.fake_ytdlp([RATE_LIMITED] * len(youtube.player_client_chain()))
        with self.assertRaises(youtube.TranscriptUnavailable) as caught:
            youtube.get_transcript("vid1", "https://youtube.com/watch?v=vid1")
        text = str(caught.exception)
        self.assertIn("429", text)
        self.assertIn("cookies.txt", text)
        self.assertIn("yt-dlp", text)
        self.assertIn("minute", text)


if __name__ == "__main__":
    unittest.main()
