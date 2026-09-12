"""v0.6.1 unit tests: speaker-aware tracking, clean endings and the free
engine's reliability fixes.

Three user-visible bugs are locked down here:

1. the camera followed *a* person, not the one speaking — the tracker now
   scores candidates by motion during voice activity and hands off when the
   turn changes;
2. "AI unavailable during render" fired even when the call just needed a
   retry — transient failures are retried and the real reason is reported;
3. shorts ended mid-line — window endings are now scored on whether the
   speaker actually stops there, and every cut gets a tail of air.

Everything here is pure maths or file text: no ffmpeg, no network.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from autoshorts import config, engine, highlights, quality, vision
from autoshorts.transcripts import Segment, to_sentences


# --------------------------------------------------------------------------
# Fix 1 — the speaker tracker
# --------------------------------------------------------------------------
GRID = (20, 11)


def skin_for(bx: int | None, ax: int | None = 4) -> bytes:
    """A 20x11 skin map with up to two people (3x3 blobs each)."""
    skin = bytearray(220)
    for centre in (ax, bx):
        if centre is None:
            continue
        for x in range(centre - 1, centre + 2):
            for y in (2, 3, 4):
                skin[y * 20 + x] = 200
    return bytes(skin)


def motion_for(bx: int | None = None, ax: int | None = None) -> bytes:
    motion = bytearray(220)
    for centre in (ax, bx):
        if centre is None:
            continue
        for x in range(centre - 1, centre + 2):
            for y in (2, 3, 4):
                motion[y * 20 + x] = 180
    return bytes(motion)


class PersonBlobTests(unittest.TestCase):
    def test_two_people_are_separate_candidates(self):
        persons = vision.find_persons(skin_for(14), *GRID)
        self.assertEqual(len(persons), 2)
        xs = sorted(round(p.cx, 2) for p in persons)
        self.assertAlmostEqual(xs[0], 0.22, places=1)
        self.assertAlmostEqual(xs[1], 0.72, places=1)

    def test_one_person_is_one_candidate(self):
        persons = vision.find_persons(skin_for(None), *GRID)
        self.assertEqual(len(persons), 1)

    def test_motion_energy_lands_inside_the_right_box(self):
        frames = [skin_for(14) + motion_for(ax=4)]  # only person A moves
        persons = vision.person_frames_from_heat(frames, GRID)[0]
        left = next(p for p in persons if p.cx < 0.5)
        right = next(p for p in persons if p.cx > 0.5)
        self.assertGreater(left.energy, 0.0)
        self.assertEqual(right.energy, 0.0)

    def test_backdrop_is_not_a_person(self):
        whole = bytes([200]) * 220
        self.assertEqual(vision.find_persons(whole, *GRID), [])

    def test_heat_frames_are_two_planes_and_split_safe(self):
        self.assertEqual(vision.GRID_X, 20)
        pairs = vision.split_planes(
            [skin_for(14) + motion_for(ax=4), skin_for(None)], *GRID
        )
        # motion cells for person A live at (y=2..4, x=3..5) -> index 43+
        self.assertEqual(pairs[0][1][43:45], b"\xb4\xb4")   # 180 = motion
        self.assertEqual(pairs[0][0][:2], b"\x00\x00")      # skin is elsewhere
        self.assertEqual(pairs[1][1], b"")


class VoiceActivityTests(unittest.TestCase):
    def test_speech_bursts_are_flagged(self):
        env = [-70.0] * 30 + [-18.0] * 40 + [-70.0] * 30
        flags = vision.voice_activity(env)
        self.assertFalse(flags[10])
        self.assertTrue(flags[50])
        self.assertFalse(flags[-5])

    def test_constant_tone_counts_as_voice(self):
        self.assertTrue(all(vision.voice_activity([-3.0] * 100)))

    def test_digital_silence_counts_as_nothing(self):
        self.assertFalse(any(vision.voice_activity([-99.0] * 100)))

    def test_noisy_floor_still_separates_speech(self):
        flags = vision.voice_activity([-55.0] * 50 + [-15.0] * 50)
        self.assertFalse(flags[10])
        self.assertTrue(flags[90])

    def test_flag_lookup_is_total(self):
        self.assertIsNone(vision.voice_flag_at([], 0.0))
        voice = [False, True]
        self.assertTrue(vision.voice_flag_at(voice, 5.0))     # clamps to end
        self.assertFalse(vision.voice_flag_at(voice, 0.0))


class SpeakerTrackerTests(unittest.TestCase):
    def frames(self, speaker: str, frames_n: int) -> list[list[vision.Person]]:
        """Person frames where `speaker` (a/b) is the only one moving."""
        out = []
        for _ in range(frames_n):
            bx = 14 if speaker in ("b", "both") else None
            ax = 4 if speaker in ("a", "both") else None
            skin = skin_for(bx, ax if ax is not None else None)
            motion = motion_for(
                bx if speaker in ("b", "both") else None,
                ax if speaker in ("a", "both") else None,
            )
            out.append(vision.person_frames_from_heat([skin + motion], GRID)[0])
        return out

    def test_the_speaker_is_followed_not_the_listener(self):
        candidates = self.frames("b", 30)
        result = vision.run_speaker_tracker(candidates, [True] * 30, 5.0)
        self.assertTrue(result.targets)
        self.assertTrue(all(x > 0.6 for _t, x, _y in result.targets),
                        "must sit on person B (the speaker)")
        self.assertEqual(result.switches, 0)
        self.assertTrue(result.voice_used)

    def test_the_camera_hands_over_when_the_turn_changes(self):
        # both people stay in frame (a real two-shot); first A speaks, then B
        def build(speaker_moving: str):
            out = []
            ax = 4
            bx = 14
            for _ in range(30):
                skin = skin_for(bx, ax)
                motion = motion_for(
                    bx if speaker_moving == "b" else None,
                    ax if speaker_moving == "a" else None,
                )
                out.append(vision.person_frames_from_heat([skin + motion], GRID)[0])
            return out

        candidates = build("a") + build("b")
        result = vision.run_speaker_tracker(candidates, [True] * 60, 5.0)
        self.assertEqual(result.switches, 1)
        self.assertLess(result.targets[0][1], 0.4)    # starts on A
        self.assertGreater(result.targets[-1][1], 0.6)  # ends on B

    def test_without_voice_the_most_present_person_wins(self):
        big = skin_for(14, None)
        # make B cover more cells than A
        big = bytearray(big)
        for x in (13, 14, 15):
            for y in (1, 2, 3, 4, 5):
                big[y * 20 + x] = 200
        candidates = vision.person_frames_from_heat(
            [bytes(big) + bytes(220)] * 30, GRID
        )
        result = vision.run_speaker_tracker(candidates, None, 5.0)
        self.assertFalse(result.voice_used)
        self.assertGreater(result.targets[0][1], 0.6)
        self.assertGreaterEqual(result.confidence, 0.5)

    def test_a_brief_challenge_does_not_steal_the_shot(self):
        # B moves for half a second: far shorter than SWITCH_HOLD
        candidates = self.frames("a", 20) + self.frames("b", 2) + self.frames("a", 20)
        result = vision.run_speaker_tracker(candidates, [True] * 42, 5.0)
        self.assertEqual(result.switches, 0)
        self.assertTrue(all(x < 0.4 for _t, x, _y in result.targets))

    def test_empty_input_is_safe(self):
        result = vision.run_speaker_tracker([], [], 5.0)
        self.assertEqual(result.targets, [])
        self.assertEqual(result.confidence, 0.0)


class TrackWindowUnitTests(unittest.TestCase):
    def test_off_mode_returns_none(self):
        self.assertIsNone(vision.track_window(
            Path("nope.mp4"), 0, 10, 1920, 1080, 720, 1280, 0.3, 1.0, mode="off"
        ))

    def test_switches_survive_the_cache_round_trip(self):
        plan = vision.TrackPath(
            keyframes=[(0.0, 0.5, 0.5), (1.0, 0.6, 0.5)],
            switches=2, backend="heat-speaker",
        )
        back = vision.TrackPath.from_dict(plan.as_dict())
        self.assertEqual(back.switches, 2)
        self.assertEqual(back.backend, "heat-speaker")
        # and a v0.6.0 cache entry (no switches field) still loads
        legacy = vision.TrackPath.from_dict(
            {"keyframes": [[0.0, 0.5, 0.5], [1.0, 0.5, 0.5]]}
        )
        self.assertEqual(legacy.switches, 0)


# --------------------------------------------------------------------------
# Fix 3 — endings
# --------------------------------------------------------------------------
def line(start: float, text: str, dur: float = 3.0) -> Segment:
    return Segment(start, start + dur, text)


class EndingSignalTests(unittest.TestCase):
    def setUp(self):
        # gaps of 1.5 s sit above the 1.2 s merge threshold, so every line is
        # its own utterance and each one knows the pause that follows it
        self.segs = [
            line(0.0, "let me tell you the truth about money"),
            line(4.5, "nobody tells you this when you start"),
            line(9.0, "i lost everything in my first business."),
            line(13.5, "it was the worst year of my life"),
            line(18.0, "but that failure taught me more"),
        ]
        self.sents = to_sentences(self.segs)

    def test_sentences_carry_terminal_and_pause_flags(self):
        self.assertEqual(len(self.sents), 5)
        third = self.sents[2]  # ends with a period
        self.assertTrue(third.terminal)
        self.assertIsNotNone(third.pause_after)
        first = self.sents[0]
        self.assertFalse(first.terminal)
        self.assertGreaterEqual(first.pause_after or 0.0, 0.35)

    def test_a_run_on_utterance_records_the_following_silence(self):
        # overlapping caption lines merge into one utterance; the pause after
        # the merged utterance is still the real silence before the next line
        merged = to_sentences(
            [line(0.0, "words that keep going and going", 3.0),
             line(2.8, "and never stop until here", 3.0),
             line(9.0, "the next line after a long gap", 3.0)]
        )
        first = merged[0]
        self.assertGreater(first.duration, 5.0)          # both lines merged
        self.assertGreaterEqual(first.pause_after or 0.0, 2.0)

    def test_ends_cleanly_knows_the_three_good_stops(self):
        clean, why = highlights.ends_cleanly(self.sents, len(self.sents) - 1)
        self.assertTrue(clean)
        self.assertEqual(why, "ends with the source")
        clean, why = highlights.ends_cleanly(self.sents, 2)
        self.assertTrue(clean)
        self.assertEqual(why, "full sentence")
        clean, why = highlights.ends_cleanly(self.sents, 1)
        self.assertTrue(clean)
        self.assertEqual(why, "lands on a pause")

    def test_mid_flow_endings_are_rejected(self):
        from autoshorts.transcripts import Sentence
        sents = [
            # the 40-word cap flushes an utterance while speech continues
            Sentence(0.0, 3.0, "a b c d e f", ["a"], terminal=False,
                     pause_after=0.1),
            Sentence(3.1, 6.0, "g h i j k l", ["g"], terminal=False,
                     pause_after=0.1),
            Sentence(6.2, 9.0, "the payoff line.", ["the"], terminal=True,
                     pause_after=2.0),
        ]
        clean, why = highlights.ends_cleanly(sents, 0)
        self.assertFalse(clean)
        self.assertEqual(why, "")
        clean, _ = highlights.ends_cleanly(sents, 1)
        self.assertFalse(clean)

    def test_extend_to_clean_end_pushes_to_a_stop(self):
        from autoshorts.transcripts import Sentence
        sents = [
            Sentence(0.0, 3.0, "a", ["a"], terminal=False, pause_after=0.1),
            Sentence(3.1, 6.0, "b", ["b"], terminal=False, pause_after=0.1),
            Sentence(6.2, 9.0, "c.", ["c"], terminal=True, pause_after=2.0),
        ]
        self.assertEqual(highlights.extend_to_clean_end(sents, 0, 0, 30.0), 2)
        self.assertEqual(highlights.extend_to_clean_end(sents, 0, 2, 30.0), 2)
        # no clean stop within budget: index unchanged
        self.assertEqual(highlights.extend_to_clean_end(sents, 0, 0, 3.0), 0)


class HighlightEndingTests(unittest.TestCase):
    def test_highlights_prefer_windows_that_end_on_a_stop(self):
        # a run-on trap: the hook-dense first utterance hits the word cap
        # while speech continues; the clean stop is a little further out
        run_on = (
            "the truth is nobody tells you the secret about money and most "
            "people dont know what happened next here is the thing about that "
            "day when one day everything changed for me and it was the worst "
            "and best moment of my whole entire life that anybody has ever "
        )
        segs = [
            Segment(0.0, 14.0, run_on),
            Segment(14.1, 18.0, "and the punch line saved everything."),
        ]
        sents = to_sentences(segs)
        moments = highlights.find_highlights(sents, count=2, min_dur=4, max_dur=18)
        self.assertTrue(moments)
        index_of = {id(s): k for k, s in enumerate(sents)}
        for moment in moments:
            j = index_of.get(id(moment.sentences[-1]))
            self.assertIsNotNone(j)
            clean, why = highlights.ends_cleanly(sents, j)
            self.assertTrue(clean, f"picked moment ends mid-flow ({why})")

    def test_every_returned_moment_ends_cleanly(self):
        segs = [
            line(0.0, "the biggest mistake i made was chasing investors", 3.0),
            line(4.5, "instead of talking to customers every single day", 3.0),
            line(9.0, "the truth is that failure saved my life.", 3.0),
            line(13.5, "so we rebuilt the whole thing from scratch", 3.0),
            line(18.0, "and that is why we won in the end.", 3.0),
        ]
        sents = to_sentences(segs)
        moments = highlights.find_highlights(sents, count=3, min_dur=3, max_dur=20)
        self.assertTrue(moments)
        index_of = {id(s): k for k, s in enumerate(sents)}
        for moment in moments:
            j = index_of.get(id(moment.sentences[-1]))
            clean, why = highlights.ends_cleanly(sents, j)
            self.assertTrue(clean, f"moment {moment.start}-{moment.end}: {why}")


class TailAndBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.segs = [
            Segment(0.0, 3.2, "a"), Segment(3.5, 6.7, "b"),
            Segment(7.0, 10.2, "c"), Segment(10.5, 13.7, "d"),
        ]

    def test_tail_pad_sits_inside_the_gap(self):
        new_end = quality.tail_pad(self.segs, 0.0, 3.2)   # gap 3.2..3.5
        self.assertGreaterEqual(new_end, 3.2)
        self.assertLess(new_end, 3.5)
        # the window never grows by more than END_TAIL_MAX
        self.assertLessEqual(new_end - 3.2, config.END_TAIL_MAX)

    def test_tail_pad_takes_the_full_tail_at_the_end_of_source(self):
        new_end = quality.tail_pad(self.segs, 0.0, 13.7, hard_end=15.0)
        self.assertGreaterEqual(new_end, 13.7 + config.END_TAIL_MIN)

    def test_tail_pad_never_overlaps_the_next_word(self):
        tight = [Segment(0.0, 3.2, "a"), Segment(3.25, 6.7, "b")]
        new_end = quality.tail_pad(tight, 0.0, 3.2)
        self.assertLessEqual(new_end, 3.23)

    def test_finalize_window_finishes_a_chopped_word(self):
        start, end = quality.finalize_window(self.segs, 0.0, 5.9)  # inside 3.5-6.7
        self.assertGreaterEqual(end, 6.7)

    def test_finalize_window_backs_up_when_the_rest_is_too_long(self):
        start, end = quality.finalize_window(self.segs, 0.0, 3.6)  # 3.1s left
        self.assertLess(end, 3.6)   # back to the pause at 3.2 + min tail
        self.assertGreaterEqual(end, 3.2 + config.END_TAIL_MIN - 0.01)

    def test_finalize_window_pulls_a_mid_word_start_back(self):
        start, end = quality.finalize_window(self.segs, 3.6, 10.0)
        self.assertLessEqual(start, 3.45)

    def test_finalize_window_respects_the_source_end(self):
        start, end = quality.finalize_window(
            self.segs, 0.0, 5.9, hard_end=6.0
        )
        self.assertLessEqual(end, 6.0)

    def test_refine_moments_adds_the_tail(self):
        from autoshorts.highlights import Highlight
        moment = Highlight(
            start=0.0, end=10.2, score=10.0, title="t",
            sentences=[],
        )
        refined = quality.refine_moments([moment], self.segs, enabled=True)
        self.assertTrue(refined)
        self.assertGreater(refined[0].end, 10.2)
        self.assertLessEqual(refined[0].end - 10.2, config.END_TAIL_MAX + 0.3)


# --------------------------------------------------------------------------
# Fix 2 — the free engine's reliability
# --------------------------------------------------------------------------
class EngineReliabilityTests(unittest.TestCase):
    def _patch_urlopen(self, fn):
        from unittest import mock
        return mock.patch("urllib.request.urlopen", fn)

    def test_transient_failures_are_retried(self):
        import io
        import urllib.error
        calls = {"n": 0}

        class FakeResponse:
            status = 200

            def read(self):
                return b'{"ok": true}'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(request, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.HTTPError(
                    "http://x", 503, "unavailable", {}, io.BytesIO(b"busy")
                )
            return FakeResponse()

        with self._patch_urlopen(fake_urlopen):
            out = engine._post_json("http://x", {}, {}, 5.0)
        self.assertEqual(calls["n"], 2, "a 503 must be retried once")
        self.assertEqual(out, {"ok": True})

    def test_timeouts_are_retried_too(self):
        calls = {"n": 0}

        def fake_urlopen(request, timeout=None):
            calls["n"] += 1
            raise TimeoutError("timed out")

        with self._patch_urlopen(fake_urlopen):
            out = engine._post_json("http://x", {}, {}, 5.0)
        self.assertEqual(calls["n"], config.AI_RETRIES + 1)
        self.assertEqual(out, {"__error__": "timed out"})

    def test_bad_keys_fail_fast_without_a_retry(self):
        import io
        import urllib.error
        calls = {"n": 0}

        def fake_urlopen(request, timeout=None):
            calls["n"] += 1
            raise urllib.error.HTTPError(
                "http://x", 401, "nope", {}, io.BytesIO(b"denied")
            )

        with self._patch_urlopen(fake_urlopen):
            out = engine._post_json("http://x", {}, {}, 5.0)
        self.assertEqual(calls["n"], 1, "a 401 must not be retried")
        self.assertIn("401", out["__error__"])

    def test_refine_pack_reports_the_real_reason(self):
        import io
        import urllib.error

        def fake_urlopen(request, timeout=None):
            raise urllib.error.HTTPError(
                "http://x", 404, "model not found", {}, io.BytesIO(b"gone")
            )

        with self._patch_urlopen(fake_urlopen):
            pack = {"titles": ["t1", "t2"], "hashtags": ["#shorts"],
                    "description": "d"}
            out, provider, notice = engine.refine_pack(
                pack, "ctx", {"ai_provider": "groq", "groq_key": "k"},
            )
        self.assertEqual(out, pack, "the offline pack must be kept")
        self.assertEqual(provider, "offline")
        self.assertIn("404", notice, "the user deserves the actual reason")
        self.assertIn("offline pack used", notice)

    def test_refine_pack_without_a_key_is_silent(self):
        pack = {"titles": ["t1"]}
        out, provider, notice = engine.refine_pack(
            pack, "ctx", {"ai_provider": "offline"}
        )
        self.assertEqual(notice, "")

    def test_defaults_point_at_live_free_models(self):
        # Groq retired llama-3.1-8b-instant (2026-08-16) and Google deprecated
        # the 2.0 Flash family (2026-06-01); the old defaults made every call
        # 404, which users saw as "AI unavailable during render". v0.6.5 moved
        # Gemini to the current GA Flash generation.
        self.assertEqual(config.GROQ_MODEL, "openai/gpt-oss-20b")
        self.assertEqual(config.GEMINI_MODEL, "gemini-3.6-flash")

    def test_gemini_payload_disables_thinking(self):
        # 2.5-class models spend the output budget on thinking tokens unless
        # the budget is pinned to zero, arriving with empty text.
        import inspect
        import autoshorts.engine as eng
        source = inspect.getsource(eng)
        self.assertIn("thinkingConfig", source)
        self.assertIn("thinkingBudget", source)

    def test_render_timeout_is_a_real_budget(self):
        self.assertGreaterEqual(config.AI_RENDER_TIMEOUT, 20.0)
        self.assertGreaterEqual(config.AI_RETRIES, 1)


class VersionAndDefaultsTests(unittest.TestCase):
    def test_version_bumped(self):
        # 0.6.6 owns the file now; this suite defends the floor it shipped on
        self.assertGreaterEqual(config.APP_VERSION, "0.6.2")
        self.assertGreaterEqual(config.APP_VERSION, "0.6.5")

    def test_shorts_got_longer(self):
        self.assertEqual(config.MIN_CLIP_SECONDS, 25)
        self.assertEqual(config.MAX_CLIP_SECONDS, 90)
        self.assertGreater(config.MAX_CLIP_SECONDS, 60)

    def test_server_still_accepts_the_new_defaults(self):
        self.assertTrue(8 <= config.MIN_CLIP_SECONDS <= 120)
        self.assertTrue(10 <= config.MAX_CLIP_SECONDS <= 180)


if __name__ == "__main__":
    unittest.main()
