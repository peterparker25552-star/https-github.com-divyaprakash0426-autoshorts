"""v0.6.5 unit tests — the search bar, the AQ. keys, and the clean ta-dum.

Three user-visible bugs own this file:

1. **The search icon and the word "Search" overlapped.** The shared control
   rule ``input[type="search"] { padding: 9px 11px }`` appears *after*
   ``.inputwrap input { padding-left: 34px }`` in style.css at equal
   specificity, so every prefix-icon input lost its left padding and the
   placeholder slid under the icon. The fix is a higher-specificity selector
   (``.inputwrap input[type]``); the test pins both sides of that contract.

2. **Google's new ``AQ.`` auth keys.** AI Studio now issues keys starting
   ``AQ.`` instead of ``AIza``. They work on Google's own endpoint via
   ``x-goog-api-key`` (which is how engine.py always called it) but are
   rejected on OpenAI-compatible Bearer routes. The tests pin the validator,
   the 422 on wrong-field pastes, the mispaste rescue (a Google key dropped in
   the OpenAI-compatible box is re-routed to the Google provider) and the
   Gemini 3.x ``thinkingLevel`` payload (3.x dropped ``thinkingBudget``).

3. **The intro's random glitch/zap.** Three causes fixed in web/intro.js:
   envelopes whose automation landed at/behind ``ctx.currentTime`` (clicking
   oscillators on a late unlock), the 2.6 s convolution impulse starving the
   audio thread on phones (crackle), and a hard ``close()`` that cut sound
   mid-sample on instant replays (pop). The tests read the shipped file —
   ``tests/ident_harness.js`` drives the behaviour.

Everything here is file text or monkeypatched calls: no network, no ffmpeg.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from autoshorts import config, engine, maintenance
from autoshorts.maintenance import ServiceError

WEB = Path(config.BASE_DIR) / "web"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


STYLE_CSS = read(WEB / "style.css")
INTRO_JS = read(WEB / "intro.js")
SW_JS = read(WEB / "sw.js")
APP_JS = read(WEB / "app.js")


# --------------------------------------------------------------------------
# 1 — the search inputs keep their icon lane
# --------------------------------------------------------------------------
class SearchBarPaddingTests(unittest.TestCase):
    def test_prefix_rule_keeps_a_higher_specificity_padding(self):
        # the fix itself: an attribute selector that outranks the plain
        # input[type=…] control rule further down the file
        self.assertIn(".inputwrap input,", STYLE_CSS)
        self.assertIn(".inputwrap input[type] { padding-left: 34px; }", STYLE_CSS)

    def test_the_control_rule_that_used_to_win_is_still_there(self):
        # the general rule must keep existing (every plain input relies on it)
        # — the fix works *because* it is outranked, not because it was removed
        self.assertIn('input[type="search"]', STYLE_CSS)
        self.assertIn("padding: 9px 11px", STYLE_CSS)

    def test_icon_is_absolutely_positioned_off_the_padding(self):
        self.assertIn(".inputwrap .prefix {", STYLE_CSS)
        self.assertIn("position: absolute;", STYLE_CSS)

    def test_both_search_fields_use_the_prefix_markup(self):
        from html.parser import HTMLParser

        class Icons(HTMLParser):
            def __init__(self):
                super().__init__()
                self.prefixes = 0
                self.searches = 0

            def handle_starttag(self, tag, attrs):
                data = dict(attrs)
                if data.get("data-icon") == "search":
                    self.prefixes += 1
                if data.get("id") in ("episodeSearch", "clipSearch", "searchQ"):
                    self.searches += 1

        parser = Icons()
        parser.feed(read(WEB / "index.html"))
        # index.html carries the episode and clip searches (the transcript
        # search dialog is built in app.js); the third search icon is the
        # header button
        self.assertEqual(parser.searches, 2)
        self.assertEqual(parser.prefixes, 3)


# --------------------------------------------------------------------------
# 2 — Google "AQ." auth keys
# --------------------------------------------------------------------------
class AqKeyTests(unittest.TestCase):
    def test_new_auth_keys_are_accepted(self):
        for key in (
            "AQ.Ab8RqXdEXAMPLEKEY123456",          # the documented AQ. shape
            "AQ.AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
            "AQAb8RqXdExampleKey12345",            # tolerate a lost dot
        ):
            self.assertTrue(config.is_gemini_key(key), key)

    def test_legacy_keys_are_still_accepted(self):
        self.assertTrue(config.is_gemini_key("AIzaSyD-abc1234567890xyz"))
        self.assertTrue(config.is_gemini_key("AIzaSUPERSECRET123"))

    def test_other_providers_and_junk_are_rejected(self):
        for bad in ("", "AQ.", "AQ. short", "gsk_1234567890abc", "sk-1234567890",
                    "paste your key here", "AIza"):
            self.assertFalse(config.is_gemini_key(bad), bad)

    def test_settings_api_accepts_both_key_shapes(self):
        update = maintenance.settings_update_from(
            {"gemini_key": "AQ.Ab8RqXdEXAMPLEKEY123456"}
        )
        self.assertTrue(update["gemini_key"].startswith("AQ."))
        update = maintenance.settings_update_from({"gemini_key": "AIzaSyD-abc123456789"})
        self.assertTrue(update["gemini_key"].startswith("AIza"))

    def test_settings_api_422s_a_wrong_field_paste(self):
        for bad in ("gsk_1234567890abc", "sk-1234567890abc", "not-a-key"):
            with self.assertRaises(ServiceError) as caught:
                maintenance.settings_update_from({"gemini_key": bad})
            self.assertEqual(caught.exception.status, 422)
            self.assertIn("AQ", caught.exception.detail)

    def test_default_model_is_the_current_flash_generation(self):
        self.assertEqual(config.GEMINI_MODEL, "gemini-3.6-flash")


class AqRoutingTests(unittest.TestCase):
    """An AQ. key can never work on an OpenAI-compatible Bearer route."""

    def test_mispasted_google_key_is_rerouted(self):
        cfg = engine.read({
            "ai_provider": "custom",
            "ai_key": "AQ.Ab8RqXdEXAMPLEKEY123456",
        })
        self.assertEqual(cfg["keys"]["gemini"], "AQ.Ab8RqXdEXAMPLEKEY123456")
        self.assertEqual(cfg["keys"]["custom"], "")
        self.assertEqual(engine.chain(cfg), ["gemini"])
        self.assertTrue(engine.available({
            "ai_provider": "custom", "ai_key": "AQ.Ab8RqXdEXAMPLEKEY123456",
        }))

    def test_legacy_aiza_key_stays_in_the_custom_box(self):
        cfg = engine.read({
            "ai_provider": "custom",
            "ai_key": "AIzaSyD-abc1234567890xyz",
        })
        self.assertEqual(cfg["keys"]["custom"], "AIzaSyD-abc1234567890xyz")
        self.assertEqual(cfg["keys"]["gemini"], "")
        self.assertEqual(engine.chain(cfg), ["custom"])

    def test_an_explicit_google_key_is_never_moved(self):
        cfg = engine.read({
            "ai_provider": "custom",
            "ai_key": "AQ.Ab8RqXdEXAMPLEKEY123456",
            "gemini_key": "AQ.OwnGoogleKey12345678",
        })
        self.assertEqual(cfg["keys"]["custom"], "AQ.Ab8RqXdEXAMPLEKEY123456")
        self.assertEqual(engine.chain(cfg), ["custom"])


class GeminiPayloadTests(unittest.TestCase):
    """The request engine.py actually builds for the new models."""

    def setUp(self):
        self.seen = {}

        def spy(url, payload, headers, timeout):
            self.seen = {"url": url, "payload": payload,
                         "headers": dict(headers or {})}
            return {
                "candidates": [{
                    "content": {"parts": [
                        {"text": json.dumps({
                            "titles": ["a correct title", "another title",
                                       "third title here"],
                            "hashtags": ["#shorts", "#podcast"],
                            "description": "two lines\\nhashtags",
                        })}
                    ]},
                }],
            }

        self._original = engine._post_json
        engine._post_json = spy

    def tearDown(self):
        engine._post_json = self._original

    def test_aq_key_calls_google_with_x_goog_api_key(self):
        settings = {
            "ai_provider": "gemini",
            "gemini_key": "AQ.Ab8RqXdEXAMPLEKEY123456",
            "ai_model": "gemini-3.6-flash",
        }
        data, provider, notice = engine.ask_json("sys", "user", settings, timeout=1)
        self.assertEqual(notice, "")
        self.assertEqual(provider, "gemini")
        self.assertIn("/models/gemini-3.6-flash:generateContent", self.seen["url"])
        self.assertEqual(self.seen["headers"].get("x-goog-api-key"),
                         "AQ.Ab8RqXdEXAMPLEKEY123456")
        self.assertNotIn("Authorization", self.seen["headers"],
                         "AQ. keys are rejected on Bearer routes")
        self.assertIsNotNone(data)

    def test_gemini_3_gets_thinking_level_not_budget(self):
        engine.ask_json("s", "u", {
            "ai_provider": "gemini",
            "gemini_key": "AQ.Ab8RqXdEXAMPLEKEY123456",
            "ai_model": "gemini-3.6-flash",
        }, timeout=1)
        gen = self.seen["payload"]["generationConfig"]
        self.assertEqual(gen["thinkingConfig"], {"thinkingLevel": "low"})
        self.assertNotIn("thinkingBudget", json.dumps(gen))

    def test_gemini_2_5_still_gets_the_zero_budget(self):
        """The 2.5 payload rule belongs to the model, not to the release.

        v0.6.6 stopped *calling* a retired id first (Google closed
        ``gemini-2.5-flash`` to new keys and every render fell back to the
        offline pack), so this asserts both halves: the builder still pins
        ``thinkingBudget: 0`` for a 2.5-class id, and the request that goes out
        in its place carries the Gemini 3 shape.
        """
        self.assertEqual(engine._gemini_thinking("gemini-2.5-flash"),
                         {"thinkingConfig": {"thinkingBudget": 0}})
        engine.ask_json("s", "u", {
            "ai_provider": "gemini",
            "gemini_key": "AIzaSyD-abc1234567890xyz",
            "ai_model": "gemini-2.5-flash",
        }, timeout=1)
        self.assertIn("/models/gemini-3.6-flash:generateContent", self.seen["url"])
        gen = self.seen["payload"]["generationConfig"]
        self.assertEqual(gen["thinkingConfig"], {"thinkingLevel": "low"})


# --------------------------------------------------------------------------
# 3 — the intro glitch: read the shipped guards
# --------------------------------------------------------------------------
class IntroGlitchGuardTests(unittest.TestCase):
    def test_envelopes_start_from_silence_not_the_default_gain(self):
        # a GainNode defaults to 1.0 — an unpinned gain clicks on its first
        # samples if automation ever lands late
        self.assertIn("g.value = 0.0001;", INTRO_JS)

    def test_no_event_is_scheduled_behind_the_audio_clock(self):
        # the +12 ms shift: a late cue starts a beat later instead of clamping
        # its attack ramp to zero length (the "zap")
        self.assertIn("ctx.currentTime + 0.012", INTRO_JS)

    def test_score_gets_real_lead_room(self):
        self.assertIn("ctx.currentTime + 0.06", INTRO_JS)

    def test_reverb_impulse_is_the_short_one(self):
        self.assertIn("makeImpulse(ctx, 1.7, 3.1)", INTRO_JS)
        self.assertNotIn("makeImpulse(ctx, 2.6", INTRO_JS)

    def test_sub_bass_skips_the_convolution_send(self):
        chunk = INTRO_JS[INTRO_JS.index("// 4. DUM"):]
        chunk = chunk[:chunk.index("// 5.")]
        sub_line = next(line for line in chunk.splitlines() if "from: 41" in line)
        self.assertIn("peak: 0.52", sub_line)
        self.assertNotIn("verb", sub_line,
                         "the pure sub must not add a convolution tap")
        # the DUM body keeps its hall
        self.assertIn("verb: true", chunk)

    def test_dispose_fades_before_closing(self):
        chunk = INTRO_JS[INTRO_JS.index("dispose() {"):]
        chunk = chunk[:chunk.index("beamCount")]
        self.assertIn("exponentialRampToValueAtTime(0.0001, now + 0.03)", chunk)
        self.assertIn("setTimeout", chunk, "close must wait for the fade")

    def test_ident_version_bumped(self):
        # v0.6.6 re-cut the ident's lifecycle; the 6.5 spectrum name survives
        self.assertRegex(INTRO_JS, r'version: "6\.[5-9]-spectrum"')

    def test_shell_cache_bumped_with_the_assets(self):
        # the floor is the point: the shell must never name a pre-6.5 cache
        self.assertRegex(SW_JS, r'const SHELL = "qyro-v0\.6\.[5-9][^"]*-shell"')
        self.assertNotIn("qyro-v0.6.4", SW_JS)

    def test_settings_ui_names_the_new_key_shape(self):
        self.assertIn("AQ\\u2026 or AIza\\u2026", APP_JS)
        self.assertIn("gemini-3.6-flash / llama-3.3-70b", APP_JS)


if __name__ == "__main__":
    unittest.main()
