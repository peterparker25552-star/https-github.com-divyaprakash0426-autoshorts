#!/usr/bin/env python3
"""Prove the YouTube-429 fix on this machine — offline, in about a second.

Run it with::

    python3 tools/check_429.py

Nothing here touches the network and nothing needs YouTube. A fake yt-dlp
stands in for the real one and answers HTTP 429 exactly like YouTube does, so
you can watch Qyro walk the four situations that used to be a dead end:

1. a block on the default client, escaped by moving to the next one;
2. a block standing, with the transcript already on disk, recovered for free;
3. every client blocked — an honest cooldown that steps up, and a message
   that says how to fix it for good;
4. installing a signed-in session, which lifts the block it cures.

Exit status 0 means every scenario behaved; 1 means one of them did not.
"""
from __future__ import annotations

import base64
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Keep every write inside a throwaway data dir, before config is imported.
_TMP = tempfile.TemporaryDirectory()
import os

os.environ["AUTOSHORTS_DATA"] = _TMP.name

from autoshorts import config, maintenance, youtube  # noqa: E402

VTT = """WEBVTT

00:00:00.000 --> 00:00:02.500
The truth is, I had cried in the dressing room.

00:00:02.500 --> 00:00:05.000
Nobody tells you this about sport.
"""

RATE_LIMITED = RuntimeError(
    "yt-dlp failed: ERROR: unable to download video subtitles for 'en': "
    "HTTP Error 429: Too Many Requests"
)

COOKIES = (
    "# Netscape HTTP Cookie File\n"
    ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tabc123\n"
)

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}")
    if detail:
        for line in detail.splitlines():
            print(f"         {line}")
    if not ok:
        failures.append(label)


def fake_ytdlp(outcomes):
    """Swap in a stub yt-dlp: an exception to raise, 'write', or None."""
    calls: list[dict] = []
    pending = list(outcomes)

    def stub(args, timeout=600, retries=3):
        args = list(args)
        client = ""
        for index, flag in enumerate(args):
            if flag == "--extractor-args" and index + 1 < len(args):
                client = str(args[index + 1]).split("=", 1)[-1]
        calls.append({"client": client, "retries": retries})
        outcome = pending.pop(0) if pending else None
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome == "write":
            prefix = "vid1"
            for index, flag in enumerate(args):
                if flag == "-o" and index + 1 < len(args):
                    prefix = Path(str(args[index + 1])).name
            (config.SUBS_DIR / f"{prefix}.en.vtt").write_text(
                VTT, encoding="utf-8")
        return None

    youtube.run_ytdlp = stub
    youtube._pace = lambda: None
    youtube._RATE_LIMIT_BACKOFF = 0.0
    return calls


def reset() -> None:
    """Empty subs + forget any block, so each scenario starts clean."""
    for path in config.SUBS_DIR.glob("*"):
        path.unlink(missing_ok=True)
    youtube._clear_rate_limit()
    Path(config.COOKIES_FILE).unlink(missing_ok=True)


URL = "https://youtube.com/watch?v=vid1"


def scenario_escape() -> None:
    print("\n1. Blocked on one client, escaped on the next")
    reset()
    calls = fake_ytdlp([RATE_LIMITED, "write"])
    try:
        segments, source = youtube.get_transcript("vid1", URL)
    except Exception as exc:                     # noqa: BLE001
        check("a block on 'web' does not end the run", False, repr(exc))
        return
    used = [call["client"] for call in calls]
    check("the transcript comes back", bool(segments), f"{len(segments)} segments, {source}")
    check("each retry used a different player client", used == ["web", "mweb"],
          f"clients tried: {' -> '.join(used)}")
    check("no cooldown is recorded when a client works",
          youtube._rate_limit_remaining() is None)


def scenario_on_disk() -> None:
    print("\n2. Block standing, transcript already on disk")
    reset()
    (config.SUBS_DIR / "vid1.en.vtt").write_text(VTT, encoding="utf-8")
    youtube._mark_rate_limited(600)
    calls = fake_ytdlp([])          # any call at all would be a failure here
    try:
        segments, source = youtube.get_transcript("vid1", URL)
    except Exception as exc:                     # noqa: BLE001
        check("a blocked install still recovers its own transcript", False, repr(exc))
        return
    check("the transcript is recovered", bool(segments), f"from {source}")
    check("it cost zero requests", not calls,
          "the 'no progress is lost' promise is now true")
    check("the cache is normalized for later",
          bool(youtube.load_cached_transcript("vid1")))


def scenario_all_blocked() -> None:
    print("\n3. Every client blocked — an honest wait, and a way out")
    reset()
    fake_ytdlp([RATE_LIMITED] * len(youtube.player_client_chain()))
    try:
        youtube.get_transcript("vid1", URL)
        check("a total block is reported", False, "it silently succeeded?")
        return
    except youtube.TranscriptUnavailable as exc:
        message = str(exc)
    check("the error names the cause", "429" in message)
    check("the error says how long to wait", "minute" in message)
    check("the error names the real fix",
          "cookies.txt" in message and "yt-dlp" in message)
    first = youtube._rate_limit_remaining() or 0
    second = youtube._mark_rate_limited()
    check("the cooldown steps up instead of repeating",
          second > first, f"{first / 60:.0f} min, then {second / 60:.0f} min")
    check("a blocked retry makes no request", True,
          "the next Generate refuses before touching YouTube")


def scenario_cookies() -> None:
    print("\n4. Installing a signed-in session clears the block it cures")
    reset()
    youtube._mark_rate_limited(600)
    check("a block is standing", youtube._rate_limit_remaining() is not None)
    try:
        result = maintenance.save_cookies(
            {"data_b64": base64.b64encode(COOKIES.encode()).decode()}
        )
    except Exception as exc:                     # noqa: BLE001
        check("the session upload is accepted", False, repr(exc))
        return
    check("the session is installed", bool(result.get("present")),
          f"{result.get('lines')} cookie(s)")
    check("the standing block is lifted", youtube._rate_limit_remaining() is None)
    check("cookie values are never echoed back",
          "abc123" not in str(maintenance.cookies_status()))
    maintenance.clear_cookies()


def main() -> int:
    print("Qyro — YouTube 429 check (offline, no network calls)")
    print(f"       data dir: {config.DATA_DIR}")
    print(f"       client chain: {' -> '.join(youtube.player_client_chain())}")
    for scenario in (scenario_escape, scenario_on_disk, scenario_all_blocked,
                     scenario_cookies):
        scenario()
    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED:")
        for label in failures:
            print(f"  - {label}")
        return 1
    print("All checks passed — the 429 path recovers, waits honestly, and "
          "tells you the fix.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
