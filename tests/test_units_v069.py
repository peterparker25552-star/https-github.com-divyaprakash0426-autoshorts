"""Qyro v0.6.9 — "No captions available" stops being a guess.

The bug this file exists for: a user presses *Generate* and the job dies with
``Transcript unavailable: No captions available for this episode`` on every
episode, including ones that plainly have captions.

``get_transcript`` had two outcomes: an HTTP 429, and that sentence. Every
other way a caption fetch can fail was folded into the second one —

* YouTube **withholding** caption tracks from an anonymous client (a "PO
  Token" skip): yt-dlp discards them with a *warning*, prints
  ``[info] There are no subtitles for the requested languages`` and exits
  **0**, so a clean run with nothing on disk read as "the video has no
  captions";
* a **bot check** ("Sign in to confirm you're not a bot"), which is an error
  but not a 429, so it ended the walk after one player client;
* a **TLS reset / dead DNS / blocked network**, likewise;
* an **extractor too old** to parse YouTube's current player, likewise;
* a **private or region-locked** video, which was blamed on its captions.

And because only a 429 continued the player-client walk, the chain that exists
to escape a refusal never ran past its first entry — which v0.6.8 had pinned to
``web``, the client YouTube refuses anonymous captions on most often, and the
one whose PO-token warning ``--no-warnings`` then deleted.

Every test below pins one piece of the diagnosis. None of them touch the
network: a stub yt-dlp acts out each failure with the output the real one
produces.
"""
from __future__ import annotations

import datetime
import json
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from autoshorts import config, maintenance, youtube

URL = "https://youtube.com/watch?v=vid1"

# A minimal but real WebVTT caption track, so the parser does actual work.
VTT = """WEBVTT

00:00:00.000 --> 00:00:02.500
The truth is, I had cried in the dressing room.

00:00:02.500 --> 00:00:05.000
Nobody tells you this about sport.

00:00:05.000 --> 00:00:08.000
The body gives up before the mind does.
"""

# What yt-dlp really prints when YouTube withholds caption tracks from an
# anonymous client: a warning, an "nothing to write" info line, and exit 0.
PO_TOKEN_OUTPUT = (
    "[youtube] Extracting URL: https://youtube.com/watch?v=vid1\n"
    "WARNING: [youtube] Some web client subtitles require a PO Token which was "
    "not provided. They will be discarded since they are not downloadable "
    "as-is. For more information, refer to https://github.com/yt-dlp/yt-dlp/"
    "wiki/PO-Token-Guide\n"
    "[info] There are no subtitles for the requested languages\n"
)
PO_TOKEN = (PO_TOKEN_OUTPUT, "")

NO_SUBTITLES = ("[info] There are no subtitles for the requested languages\n", "")

BOT_CHECK = RuntimeError(
    "yt-dlp failed: ERROR: [youtube] vid1: Sign in to confirm you're not a bot. "
    "Use --cookies-from-browser or --cookies to provide credentials"
)
TLS_RESET = RuntimeError(
    "yt-dlp failed: ERROR: [youtube] vid1: Unable to download API page: "
    "TLS/SSL connection has been closed (EOF) (_ssl.c:992)"
)
STALE_EXTRACTOR = RuntimeError(
    "yt-dlp failed: ERROR: [youtube] vid1: Unable to extract initial data; "
    "please report this issue on https://github.com/yt-dlp/yt-dlp/issues"
)
PRIVATE_VIDEO = RuntimeError(
    "yt-dlp failed: ERROR: [youtube] vid1: Private video. Sign in if you've "
    "been granted access to this video"
)
RATE_LIMITED = RuntimeError(
    "yt-dlp failed: ERROR: unable to download video subtitles for 'en': "
    "HTTP Error 429: Too Many Requests"
)

# How many languages one full ``auto`` walk asks for, used to size scenarios.
CHAIN = youtube.sub_lang_chain("auto")


class FakeYtDlp:
    """A stub yt-dlp that can act out any caption failure YouTube produces.

    ``subs`` is one outcome per subtitle request, ``meta`` one per metadata
    probe (``-J``). An outcome is an exception to raise, a ``(stdout, stderr)``
    tuple for a clean run, ``"write"``/``("write", "hi")`` for a run that leaves
    a caption file behind, ``"empty"`` for a clean run that does not, or a dict
    for a probe's caption inventory. The last outcome repeats, so a scenario
    only has to spell out the interesting part.
    """

    def __init__(self, subs=None, meta=None):
        self.subs = list(subs or [])
        self.meta = list(meta or [])
        self.calls: list[dict] = []

    # -- plumbing --------------------------------------------------------
    def install(self, case: unittest.TestCase) -> "FakeYtDlp":
        original = youtube.run_ytdlp
        youtube.run_ytdlp = self
        case.addCleanup(setattr, youtube, "run_ytdlp", original)
        # Never sleep: the pacer and the rotation pauses would add minutes.
        case.addCleanup(setattr, youtube, "_pace", youtube._pace)
        youtube._pace = lambda: None
        case.addCleanup(setattr, youtube, "_RATE_LIMIT_BACKOFF",
                        youtube._RATE_LIMIT_BACKOFF)
        youtube._RATE_LIMIT_BACKOFF = 0.0
        case.addCleanup(setattr, config, "CLIENT_ROTATION_PAUSE",
                        config.CLIENT_ROTATION_PAUSE)
        config.CLIENT_ROTATION_PAUSE = 0.0
        return self

    def __call__(self, args, timeout=600, retries=3):
        args = list(args)
        client = ""
        for index, flag in enumerate(args):
            if flag == "--extractor-args" and index + 1 < len(args):
                client = str(args[index + 1]).split("=", 1)[-1]
        probe = "-J" in args
        lang = ""
        for index, flag in enumerate(args):
            if flag == "--sub-langs" and index + 1 < len(args):
                lang = str(args[index + 1])
        self.calls.append({"client": client, "args": args, "retries": retries,
                           "probe": probe, "lang": lang})

        queue = self.meta if probe else self.subs
        if queue:
            outcome = queue.pop(0)
            if not queue:
                queue.append(outcome)          # the last outcome repeats
        else:
            outcome = {} if probe else "empty"

        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, dict):          # a probe's caption inventory
            return subprocess.CompletedProcess(args, 0, json.dumps(outcome), "")

        write = outcome == "write" or (
            isinstance(outcome, tuple) and outcome and outcome[0] == "write")
        if write:
            code = outcome[1] if isinstance(outcome, tuple) else (lang or "en")
            prefix = "vid1"
            for index, flag in enumerate(args):
                if flag == "-o" and index + 1 < len(args):
                    prefix = Path(str(args[index + 1])).name
            (config.SUBS_DIR / f"{prefix}.{code}.vtt").write_text(
                VTT, encoding="utf-8")

        if isinstance(outcome, tuple) and outcome and isinstance(outcome[0], str):
            stdout, stderr = outcome[0], (outcome[1] if len(outcome) > 1 else "")
        else:
            stdout, stderr = "", ""
        return subprocess.CompletedProcess(args, 0, stdout, stderr)

    # -- what the tests read ---------------------------------------------
    @property
    def clients(self) -> list[str]:
        return [call["client"] for call in self.calls if not call["probe"]]

    @property
    def languages(self) -> list[str]:
        return [call["lang"] for call in self.calls
                if not call["probe"] and call["lang"]]

    @property
    def probes(self) -> int:
        return sum(1 for call in self.calls if call["probe"])


class _Sandbox(unittest.TestCase):
    """Redirect every path the caption code touches into a temp data dir."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        for name in ("COOKIES_FILE", "RATE_LIMIT_STATE", "SUBS_DIR",
                     "MEDIA_DIR", "YTDLP_PLAYER_CLIENT"):
            self.addCleanup(setattr, config, name, getattr(config, name))
        config.COOKIES_FILE = self.dir / "cookies.txt"
        config.RATE_LIMIT_STATE = self.dir / ".rate-limit.json"
        config.SUBS_DIR = self.dir / "subs"
        config.MEDIA_DIR = self.dir / "media"
        config.SUBS_DIR.mkdir(parents=True, exist_ok=True)
        config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        config.YTDLP_PLAYER_CLIENT = ""

    def fake(self, subs=None, meta=None) -> FakeYtDlp:
        return FakeYtDlp(subs, meta).install(self)

    @staticmethod
    def inventory(*languages: str, manual: bool = False) -> dict:
        """A ``-J`` payload listing exactly these caption languages."""
        tracks = {lang: [{"ext": "json3", "url": f"https://x/{lang}"}]
                  for lang in languages}
        return {
            "id": "vid1",
            "title": "Episode 1",
            "subtitles": dict(tracks) if manual else {},
            "automatic_captions": {} if manual else dict(tracks),
        }

    def fetch(self, language: str = "auto"):
        return youtube.get_transcript("vid1", URL, language)

    def message(self, language: str = "auto") -> str:
        with self.assertRaises(youtube.TranscriptUnavailable) as caught:
            self.fetch(language)
        return str(caught.exception)


# --------------------------------------------------------------------------
# 1. Naming the failure — the classifier behind every message
# --------------------------------------------------------------------------
class ClassificationTests(unittest.TestCase):
    def test_a_429_is_still_a_429(self):
        self.assertEqual(youtube.classify_failure(str(RATE_LIMITED)),
                         youtube.KIND_RATE_LIMIT)

    def test_a_bot_check_is_not_read_as_a_rate_limit(self):
        self.assertEqual(youtube.classify_failure(str(BOT_CHECK)),
                         youtube.KIND_BOT_CHECK)

    def test_a_tls_reset_is_a_connection_problem(self):
        self.assertEqual(youtube.classify_failure(str(TLS_RESET)),
                         youtube.KIND_NETWORK)

    def test_a_broken_extractor_is_named_as_one(self):
        self.assertEqual(youtube.classify_failure(str(STALE_EXTRACTOR)),
                         youtube.KIND_EXTRACTOR)

    def test_a_private_video_is_not_blamed_on_its_captions(self):
        self.assertEqual(youtube.classify_failure(str(PRIVATE_VIDEO)),
                         youtube.KIND_VIDEO)

    def test_an_age_gate_reads_as_the_video_not_a_bot_check(self):
        # Both say "sign in"; only one of them is fixed by a session.
        self.assertEqual(
            youtube.classify_failure(
                "ERROR: Sign in to confirm your age. This video is age-restricted"
            ),
            youtube.KIND_VIDEO,
        )

    def test_a_region_block_reads_as_the_video(self):
        self.assertEqual(
            youtube.classify_failure(
                "ERROR: The uploader has not made this video available "
                "in your country"
            ),
            youtube.KIND_VIDEO,
        )

    def test_a_429_in_prose_is_still_not_a_block(self):
        self.assertEqual(
            youtube.classify_failure("yt-dlp failed: the episode runs 429 seconds"),
            youtube.KIND_UNKNOWN,
        )

    def test_a_po_token_skip_is_recognised_in_a_clean_run(self):
        # The v0.6.8 killer: exit 0, nothing written, and the only evidence in
        # a warning that --no-warnings used to delete before it was read.
        self.assertEqual(youtube.classify_caption_output(PO_TOKEN_OUTPUT),
                         youtube.KIND_PO_TOKEN)

    def test_a_clean_run_with_nothing_to_write_reads_as_no_captions(self):
        self.assertEqual(youtube.classify_caption_output(NO_SUBTITLES[0]),
                         youtube.KIND_NO_CAPTIONS)

    def test_silence_reads_as_no_captions_not_as_a_crash(self):
        self.assertEqual(youtube.classify_caption_output(""),
                         youtube.KIND_NO_CAPTIONS)

    def test_the_most_specific_failure_wins(self):
        self.assertEqual(
            youtube.worse_kind(youtube.KIND_NO_CAPTIONS, youtube.KIND_PO_TOKEN),
            youtube.KIND_PO_TOKEN,
        )
        self.assertEqual(
            youtube.worse_kind(youtube.KIND_BOT_CHECK, youtube.KIND_NETWORK),
            youtube.KIND_BOT_CHECK,
        )
        self.assertEqual(youtube.worse_kind(youtube.KIND_OK, youtube.KIND_OK),
                         youtube.KIND_OK)

    def test_the_detail_shown_to_a_user_is_the_error_line_trimmed(self):
        detail = youtube.detail_line(
            "[youtube] Extracting URL: https://x\n"
            "WARNING: something minor\n"
            "ERROR: [youtube] vid1: Private video. Sign in if you've been "
            "granted access to this video\n"
        )
        self.assertTrue(detail.startswith("ERROR"))
        self.assertIn("Private video", detail)
        self.assertLessEqual(len(youtube.detail_line("x" * 900)), 220)

    def test_caption_calls_keep_the_warnings_that_diagnose_them(self):
        # --no-warnings is what hid the PO-token refusal, so a subtitle call
        # must not carry it — while a -J metadata call must, so its stdout
        # stays parseable as JSON.
        self.assertTrue(youtube.is_caption_call(
            ["--skip-download", "--write-subs", "--sub-langs", "en", "url"]))
        self.assertTrue(youtube.is_caption_call(
            ["--skip-download", "--write-auto-subs", "url"]))
        self.assertFalse(youtube.is_caption_call(["-J", "--skip-download", "url"]))
        try:
            youtube._ytdlp()
        except RuntimeError:
            self.skipTest("yt-dlp is not installed")
        with unittest.mock.patch.object(
            youtube.subprocess, "run",
            return_value=subprocess.CompletedProcess([], 0, "{}", ""),
        ) as run:
            youtube.run_ytdlp(["--skip-download", "--write-subs", "url"])
            caption_cmd = list(run.call_args[0][0])
            youtube.run_ytdlp(["-J", "--skip-download", "url"])
            probe_cmd = list(run.call_args[0][0])
        self.assertNotIn("--no-warnings", caption_cmd)
        self.assertIn("--no-warnings", probe_cmd)


# --------------------------------------------------------------------------
# 2. The player-client walk actually walks
# --------------------------------------------------------------------------
class RotationTests(_Sandbox):
    def test_the_first_pass_leaves_the_choice_to_the_installed_ytdlp(self):
        yt = self.fake([NO_SUBTITLES])
        self.message()
        self.assertTrue(yt.calls)
        self.assertNotIn("--extractor-args", yt.calls[0]["args"],
                         "pinning web first is what v0.6.8 did, and web is the "
                         "client YouTube refuses anonymous captions on")
        chain = youtube.player_client_chain()
        self.assertEqual(chain[0], "")
        self.assertIn("web", chain, "pinned clients still run, as fallbacks")

    def test_a_configured_client_still_leads_the_walk(self):
        config.YTDLP_PLAYER_CLIENT = "tv"
        yt = self.fake([NO_SUBTITLES])
        self.message()
        self.assertEqual(yt.calls[0]["client"], "tv")

    def test_a_po_token_refusal_moves_to_another_client(self):
        # The exact v0.6.8 report: refused on the first client, fine on the
        # next — and reported instead as "No captions available".
        yt = self.fake([*([PO_TOKEN] * len(CHAIN)), "write"])
        segments, source = self.fetch()
        self.assertEqual(len(segments), 3)
        self.assertIn("vtt", source)
        self.assertGreaterEqual(len(set(yt.clients)), 2,
                                "a refusal must rotate, not repeat")

    def test_a_bot_check_rotates_and_says_what_really_happened(self):
        yt = self.fake([BOT_CHECK])
        message = self.message()
        self.assertNotIn("No captions available", message)
        self.assertIn("bot check", message.lower())
        self.assertIn("Sign in to confirm you're not a bot", message)
        self.assertIn("cookies.txt", message)
        self.assertGreaterEqual(len(set(yt.clients)), 2)

    def test_a_bot_check_does_not_ask_the_same_client_five_times(self):
        # A refusal is about the client, not the language: one request per
        # client, not one per language, is the whole budget argument.
        yt = self.fake([BOT_CHECK])
        self.message()
        self.assertEqual(len(yt.clients), len(set(yt.clients)))

    def test_a_dead_connection_is_reported_as_a_dead_connection(self):
        yt = self.fake([TLS_RESET])
        message = self.message()
        self.assertNotIn("No captions available", message)
        self.assertIn("TLS/SSL connection has been closed", message)
        self.assertIn("connection", message.lower())
        self.assertTrue(yt.clients)
        self.assertLessEqual(len(yt.clients), config.TRANSCRIPT_MAX_PASSES,
                             "another player client cannot fix a dead network")

    def test_a_stale_extractor_says_to_update_ytdlp(self):
        self.fake([STALE_EXTRACTOR])
        message = self.message()
        self.assertNotIn("No captions available", message)
        self.assertIn("pip install -U yt-dlp", message)

    def test_a_private_video_stops_at_once_instead_of_rotating(self):
        yt = self.fake([PRIVATE_VIDEO])
        message = self.message()
        self.assertNotIn("No captions available", message)
        self.assertIn("Private video", message)
        self.assertEqual(len(yt.clients), 1,
                         "no client and no session makes a private video public")

    def test_a_429_still_walks_the_whole_chain_and_cools_down(self):
        # The v0.6.7 escape must survive the v0.6.9 caps.
        yt = self.fake([RATE_LIMITED])
        message = self.message()
        self.assertIn("rate-limiting", message)
        self.assertIn("cookies.txt", message)
        self.assertIsNotNone(youtube._rate_limit_remaining())
        self.assertEqual(len(set(yt.clients)),
                         len(youtube.player_client_chain()))

    def test_a_refusal_walk_is_capped_but_wider_than_one_client(self):
        yt = self.fake([PO_TOKEN])
        self.message()
        used = set(yt.clients)
        self.assertGreater(len(used), 1)
        self.assertLessEqual(len(used), config.TRANSCRIPT_MAX_PASSES)

    def test_a_rotation_pass_asks_fewer_languages_than_the_first(self):
        # Rotating must not cost another full five-language walk per client:
        # once the episode's tracks are known, ask only for those.
        yt = self.fake([PO_TOKEN] * 20,
                       meta=[self.inventory("en", "hi", "en-orig")])
        self.message()
        self.assertEqual(yt.languages[:len(CHAIN)], CHAIN)
        later = yt.languages[len(CHAIN):]
        self.assertTrue(later)
        self.assertLessEqual(
            len(later),
            config.TRANSCRIPT_ROTATION_LANGUAGES * (config.TRANSCRIPT_MAX_PASSES - 1))
        known = {"en", "hi", "en-orig"}
        self.assertTrue(set(later) <= known,
                        f"asked for languages the episode does not have: {later}")


# --------------------------------------------------------------------------
# 3. "No captions available" now has to be earned
# --------------------------------------------------------------------------
class HonestyTests(_Sandbox):
    def test_the_episode_is_asked_before_its_captions_are_denied(self):
        yt = self.fake([NO_SUBTITLES], meta=[self.inventory()])
        message = self.message()
        self.assertEqual(yt.probes, 1,
                         "one metadata request turns a guess into a fact")
        self.assertIn("No captions available", message)
        self.assertIn("metadata", message)

    def test_a_video_with_captions_is_never_told_it_has_none(self):
        self.fake([PO_TOKEN], meta=[self.inventory("en", "hi", "en-orig")])
        message = self.message()
        self.assertNotIn("No captions available", message)
        self.assertIn("PO Token", message)
        self.assertIn("cookies.txt", message)

    def test_an_empty_pass_the_probe_contradicts_is_not_a_verdict(self):
        # yt-dlp said "there are no subtitles for the requested languages" and
        # the episode's own metadata says otherwise: that is a refusal.
        yt = self.fake([NO_SUBTITLES], meta=[self.inventory("en", "hi")])
        message = self.message()
        self.assertNotIn("No captions available", message)
        self.assertIn("does have", message)
        self.assertEqual(yt.probes, 1)

    def test_an_unanswerable_probe_falls_back_to_the_clean_pass(self):
        # The metadata request came back with nothing parseable: there is no
        # evidence of a refusal, so the clean pass stands. This is the v0.6.7
        # behaviour, kept on purpose.
        self.fake([NO_SUBTITLES], meta=[("not json at all", "")])
        self.assertIn("No captions available", self.message())

    def test_a_refusal_earlier_in_the_run_outweighs_an_empty_listing(self):
        # One client was bot-checked, another came back clean and empty, and
        # the metadata probe lists nothing. The same refusal that empties a
        # caption pass empties that listing, so the refusal is the better story
        # — and "no captions" would send the user away from the real fix.
        self.fake([BOT_CHECK, NO_SUBTITLES], meta=[self.inventory()])
        message = self.message()
        self.assertNotIn("No captions available", message)
        self.assertIn("bot check", message.lower())
        self.assertIn("cookies.txt", message)

    def test_a_probe_that_finds_the_video_private_says_so(self):
        self.fake([NO_SUBTITLES], meta=[PRIVATE_VIDEO])
        message = self.message()
        self.assertIn("Private video", message)
        self.assertNotIn("No captions available", message)

    def test_a_probe_that_fails_for_no_named_reason_does_not_blame_captions(self):
        self.fake([NO_SUBTITLES],
                  meta=[RuntimeError("yt-dlp failed: ERROR: something entirely new")])
        message = self.message()
        self.assertNotIn("No captions available", message)
        self.assertIn("could not tell why", message)

    def test_a_refused_probe_names_the_refusal_not_the_captions(self):
        self.fake([NO_SUBTITLES], meta=[BOT_CHECK])
        message = self.message()
        self.assertNotIn("No captions available", message)
        self.assertIn("bot check", message.lower())

    def test_a_transcript_on_disk_is_still_free_and_still_wins(self):
        (config.SUBS_DIR / "vid1.en.vtt").write_text(VTT, encoding="utf-8")
        yt = self.fake([], meta=[])
        segments, source = self.fetch()
        self.assertEqual(len(segments), 3)
        self.assertIn("vtt", source)
        self.assertEqual(yt.calls, [],
                         "a transcript we already hold costs no request")

    def test_a_client_that_works_leaves_no_block_behind(self):
        # A 429 on the first client, captions on the next: YouTube is talking
        # again, so nothing may be remembered against the next job. (A block
        # recorded *before* the walk is a different case — that one refuses
        # without a request, and v0.6.6/v0.6.7 pin it.)
        self.fake([RATE_LIMITED, "write"])
        segments, _source = self.fetch()
        self.assertTrue(segments)
        self.assertIsNone(youtube._rate_limit_remaining(),
                          "a client that worked means YouTube is talking again")
        self.assertFalse(youtube.rate_limit_status()["blocked"])


# --------------------------------------------------------------------------
# 4. The language the episode actually has
# --------------------------------------------------------------------------
class LanguageTests(_Sandbox):
    def test_auto_takes_the_track_the_episode_really_has(self):
        # A Tamil-only episode used to fail with "no captions" even though a
        # transcript existed: the chain only ever asked for English and Hindi.
        yt = self.fake([*([NO_SUBTITLES] * len(CHAIN)), "write"],
                       meta=[self.inventory("ta", "ta-orig")])
        segments, _source = self.fetch("auto")
        self.assertTrue(segments)
        self.assertTrue(any(lang.startswith("ta") for lang in yt.languages),
                        f"auto-detect must detect: asked {yt.languages}")

    def test_an_explicit_language_that_does_not_exist_says_so(self):
        chain = youtube.sub_lang_chain("hi")
        yt = self.fake([NO_SUBTITLES],
                       meta=[self.inventory("ta", "ta-orig")])
        message = self.message("hi")
        self.assertNotIn("No captions available", message)
        self.assertIn("Auto-detect", message)
        self.assertIn("ta", message)
        self.assertEqual(len(yt.clients), len(chain),
                         "one clean pass, then an answer — not a full rotation")

    def test_the_language_pattern_matcher_understands_wildcards(self):
        self.assertTrue(youtube._matches_language("en.*", "en-orig"))
        self.assertTrue(youtube._matches_language("en", "en"))
        self.assertTrue(youtube._matches_language("all", "ta"))
        self.assertFalse(youtube._matches_language("hi.*", "en-GB"))
        self.assertFalse(youtube._matches_language("", "en"))

    def test_the_original_audio_language_is_preferred_for_auto(self):
        keys = youtube._language_keys(
            {"en": [], "ta": [], "ta-orig": [], "hi": []})
        self.assertEqual(keys[0], "ta-orig")


# --------------------------------------------------------------------------
# 5. The extractor's age is part of every diagnosis
# --------------------------------------------------------------------------
class ExtractorAgeTests(_Sandbox):
    def test_a_calendar_version_has_an_age(self):
        recent = datetime.date.today().strftime("%Y.%m.%d")
        self.assertEqual(youtube.ytdlp_age_days(recent), 0)
        self.assertGreater(youtube.ytdlp_age_days("2024.04.09"), 365)

    def test_an_unparseable_version_has_no_age(self):
        self.assertIsNone(youtube.ytdlp_age_days("missing"))
        self.assertIsNone(youtube.ytdlp_age_days(""))
        self.assertIsNone(youtube.ytdlp_age_days("2026.13.45"))

    def _pretend_version(self, version: str) -> None:
        original = youtube.ytdlp_version
        youtube.ytdlp_version = lambda: version
        self.addCleanup(setattr, youtube, "ytdlp_version", original)
        maintenance._ytdlp_health_cache = (0.0, {})
        self.addCleanup(setattr, maintenance, "_ytdlp_health_cache", (0.0, {}))

    def test_health_reports_a_stale_extractor(self):
        self._pretend_version("2024.04.09")
        status = maintenance.youtube_status()
        self.assertIn("yt_dlp", status)
        self.assertTrue(status["yt_dlp"]["stale"])
        self.assertEqual(status["yt_dlp"]["version"], "2024.04.09")

    def test_health_reports_a_current_extractor_as_trusted(self):
        self._pretend_version(datetime.date.today().strftime("%Y.%m.%d"))
        status = maintenance.youtube_status()["yt_dlp"]
        self.assertFalse(status["stale"])
        self.assertEqual(status["age_days"], 0)

    def test_a_missing_ytdlp_is_said_plainly(self):
        self._pretend_version("")
        status = maintenance.youtube_status()["yt_dlp"]
        self.assertFalse(status["present"])
        self.assertEqual(status["version"], "missing")
        self.assertIsNone(status["age_days"])

    def test_the_failure_message_names_the_installed_extractor(self):
        self._pretend_version("2024.04.09")
        self.fake([STALE_EXTRACTOR])
        message = self.message()
        self.assertIn("2024.04.09", message)
        self.assertIn("days old", message)


# --------------------------------------------------------------------------
# 6. The media download learns the same lesson
# --------------------------------------------------------------------------
class DownloadTests(_Sandbox):
    def test_a_bot_checked_download_points_at_the_session_fix(self):
        yt = self.fake([BOT_CHECK])
        with self.assertRaises(RuntimeError) as caught:
            youtube.download_video("vid1", URL)
        self.assertIn("bot check", str(caught.exception).lower())
        self.assertIn("cookies.txt", str(caught.exception))
        self.assertTrue(yt.calls)

    def test_a_private_video_download_says_what_ytdlp_said(self):
        self.fake([PRIVATE_VIDEO])
        with self.assertRaises(RuntimeError) as caught:
            youtube.download_video("vid1", URL)
        self.assertIn("Private video", str(caught.exception))

    def test_a_rate_limited_download_still_rotates_then_cools_down(self):
        yt = self.fake([RATE_LIMITED])
        with self.assertRaises(RuntimeError) as caught:
            youtube.download_video("vid1", URL)
        self.assertIn("rate-limiting", str(caught.exception))
        self.assertEqual(len(yt.calls), 2, "media tries two clients, as before")
        self.assertIsNotNone(youtube._rate_limit_remaining())


# --------------------------------------------------------------------------
# 7. The pipeline says "Transcript unavailable" with the reason attached
# --------------------------------------------------------------------------
class PipelineSurfaceTests(_Sandbox):
    def test_the_job_error_carries_the_diagnosis(self):
        """What the dashboard shows must be the reason, not the shrug."""
        from autoshorts import pipeline
        from autoshorts.store import Store

        self.fake([BOT_CHECK])
        state = self.dir / "state.json"
        store = Store(state)
        store.upsert_episode({"id": "vid1", "title": "Episode 1",
                              "duration": 600, "url": URL, "channel": "x"})
        job = store.create_job("vid1", {"kind": "auto"})
        worker = pipeline.Pipeline(store)
        worker._queue.put(job["id"])
        worker._queue.join()
        finished = store.job(job["id"])
        self.assertEqual(finished["status"], "error")
        self.assertTrue(finished["error"].startswith("Transcript unavailable:"),
                        finished["error"])
        self.assertIn("bot check", finished["error"].lower())
        self.assertNotIn("No captions available", finished["error"])


if __name__ == "__main__":
    unittest.main()
