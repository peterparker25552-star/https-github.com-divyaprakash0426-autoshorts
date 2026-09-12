#!/usr/bin/env python3
"""Why can't Qyro get this episode's transcript? Answer it in one command.

Run the offline proof (no network, about a second)::

    python3 tools/check_transcript.py

or diagnose *this machine* against a real episode::

    python3 tools/check_transcript.py --live "https://www.youtube.com/watch?v=VIDEOID"
    python3 tools/check_transcript.py --live          # uses the seed playlist's first episode

Until v0.6.9 every caption failure that was not an HTTP 429 arrived as one
sentence — "Transcript unavailable: No captions available for this episode" —
which is a claim about the video, and was usually wrong. The offline half below
acts out each real failure with a stub yt-dlp that reproduces YouTube's actual
output, and shows what Qyro now says and does instead:

1. captions **withheld** from an anonymous client (a "PO Token" skip: exit 0,
   a warning, nothing on disk) → another player client is asked, and the
   transcript arrives;
2. every client refused → the message says captions were withheld and names the
   cure, and never says "no captions";
3. a **bot check** ("Sign in to confirm you're not a bot") → named, with the
   session cure, and the walk moves to another client;
4. a **dead connection** (TLS reset) → named as a connection problem, capped
   instead of walking the whole chain;
5. a **stale extractor** → named, with the installed yt-dlp's age;
6. a **private video** → named, after exactly one request;
7. a genuinely caption-less episode → the only case allowed to say "No
   captions available", and only once the episode's own metadata agrees;
8. an episode whose captions are in a language nobody asked for → auto-detect
   detects it.

Exit status 0 means every scenario behaved; 1 means one of them did not.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Keep every write of the offline half inside a throwaway data dir, before
# config is imported. The live half writes to the real data dir on purpose: a
# transcript it manages to fetch is a transcript the app now has cached.
_TMP = tempfile.TemporaryDirectory()
_LIVE = "--live" in sys.argv or "--url" in sys.argv
if not _LIVE:
    os.environ["AUTOSHORTS_DATA"] = _TMP.name

from autoshorts import config, maintenance, youtube  # noqa: E402

VTT = """WEBVTT

00:00:00.000 --> 00:00:02.500
The truth is, I had cried in the dressing room.

00:00:02.500 --> 00:00:05.000
Nobody tells you this about sport.

00:00:05.000 --> 00:00:08.000
The body gives up before the mind does.
"""

# Exactly what yt-dlp prints when YouTube withholds caption tracks from an
# anonymous client: a warning, an "nothing to write" line, and exit status 0.
# v0.6.8 read that as "this episode has no captions" — and --no-warnings had
# already deleted the one line that said otherwise.
PO_TOKEN = (
    "[youtube] Extracting URL: https://youtube.com/watch?v=vid1\n"
    "WARNING: [youtube] Some web client subtitles require a PO Token which was "
    "not provided. They will be discarded since they are not downloadable "
    "as-is.\n"
    "[info] There are no subtitles for the requested languages\n"
)
NO_SUBTITLES = "[info] There are no subtitles for the requested languages\n"

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

URL = "https://youtube.com/watch?v=vid1"
CHAIN = youtube.sub_lang_chain("auto")

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}")
    if detail:
        for line in str(detail).splitlines():
            print(f"         {line}")
    if not ok:
        failures.append(label)


def client_label(client: str) -> str:
    """An empty entry means "let the installed yt-dlp choose"."""
    return client or "yt-dlp default"


class FakeYtDlp:
    """A stub yt-dlp: one outcome per subtitle request, one per -J probe."""

    def __init__(self, subs=(), meta=()):
        self.subs = list(subs)
        self.meta = list(meta)
        self.calls: list[dict] = []

    def __call__(self, args, timeout=600, retries=3):
        args = list(args)
        client, lang = "", ""
        for index, flag in enumerate(args):
            if flag == "--extractor-args" and index + 1 < len(args):
                client = str(args[index + 1]).split("=", 1)[-1]
            if flag == "--sub-langs" and index + 1 < len(args):
                lang = str(args[index + 1])
        probe = "-J" in args
        self.calls.append({"client": client, "lang": lang, "probe": probe,
                           "args": args})

        queue = self.meta if probe else self.subs
        if queue:
            outcome = queue.pop(0)
            if not queue:
                queue.append(outcome)      # the last outcome repeats
        else:
            outcome = {} if probe else NO_SUBTITLES

        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, dict):
            return subprocess.CompletedProcess(args, 0, json.dumps(outcome), "")
        if outcome == "write":
            prefix = "vid1"
            for index, flag in enumerate(args):
                if flag == "-o" and index + 1 < len(args):
                    prefix = Path(str(args[index + 1])).name
            (config.SUBS_DIR / f"{prefix}.{lang or 'en'}.vtt").write_text(
                VTT, encoding="utf-8")
        stdout = outcome if isinstance(outcome, str) else ""
        return subprocess.CompletedProcess(args, 0, stdout, "")

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


def inventory(*languages: str) -> dict:
    tracks = {lang: [{"ext": "json3"}] for lang in languages}
    return {"id": "vid1", "title": "Episode 1", "subtitles": {},
            "automatic_captions": tracks}


def reset() -> None:
    for path in config.SUBS_DIR.glob("*"):
        path.unlink(missing_ok=True)
    youtube._clear_rate_limit()


def install(stub: FakeYtDlp) -> FakeYtDlp:
    youtube.run_ytdlp = stub
    youtube._pace = lambda: None
    youtube._RATE_LIMIT_BACKOFF = 0.0
    config.CLIENT_ROTATION_PAUSE = 0.0
    return stub


def attempt(language: str = "auto"):
    """``(segments, source, message)`` — the message is what the UI shows."""
    try:
        segments, source = youtube.get_transcript("vid1", URL, language)
        return segments, source, ""
    except youtube.TranscriptUnavailable as exc:
        return [], "", str(exc)


# --------------------------------------------------------------------------
def scenario_withheld() -> None:
    print("\n1. Captions withheld from one client, handed over by the next")
    reset()
    stub = install(FakeYtDlp([*(PO_TOKEN for _ in CHAIN), "write"]))
    segments, source, message = attempt()
    check("the transcript comes back", bool(segments),
          f"{len(segments)} segments from {source}" if segments else message)
    used = []
    for client in stub.clients:
        if client not in used:
            used.append(client)
    check("a refusal moves to another player client", len(used) > 1,
          "clients tried: " + " -> ".join(client_label(c) for c in used))
    check("the first pass pins no client of its own",
          "--extractor-args" not in stub.calls[0]["args"],
          "the installed yt-dlp chooses — it leads with a client YouTube "
          "does not challenge the way it challenges `web`")


def scenario_every_client_refused() -> None:
    print("\n2. Every client refused — the message says so, and says the cure")
    reset()
    install(FakeYtDlp([PO_TOKEN], meta=[inventory("en", "hi", "en-orig")]))
    segments, _source, message = attempt()
    check("it still fails", not segments)
    check("it does NOT claim the episode has no captions",
          "No captions available" not in message)
    check("it says the captions exist", "does have" in message or
          "has captions" in message)
    check("it names the cure", "cookies.txt" in message, message)


def scenario_bot_check() -> None:
    print("\n3. Bot check — named, cured, and not retried into the ground")
    reset()
    stub = install(FakeYtDlp([BOT_CHECK]))
    _segments, _source, message = attempt()
    check("the bot check is named", "bot check" in message.lower(), message)
    check("YouTube's own words are quoted", "not a bot" in message)
    check("the cure is a session, not a wait", "cookies.txt" in message)
    check("it asked more than one client", len(set(stub.clients)) > 1,
          f"{len(set(stub.clients))} of {len(youtube.player_client_chain())}")
    check("it asked each client once, not once per language",
          len(stub.clients) == len(set(stub.clients)))


def scenario_dead_connection() -> None:
    print("\n4. Dead connection — a network problem, not a missing transcript")
    reset()
    stub = install(FakeYtDlp([TLS_RESET]))
    _segments, _source, message = attempt()
    check("the connection is named", "connection" in message.lower(), message)
    check("YouTube's own words are quoted", "TLS/SSL" in message)
    check("it does not claim the episode has no captions",
          "No captions available" not in message)
    check("the walk is capped, so the user is not left waiting",
          len(stub.clients) <= config.TRANSCRIPT_MAX_PASSES,
          f"{len(stub.clients)} request(s), cap "
          f"{config.TRANSCRIPT_MAX_PASSES}")


def scenario_stale_extractor() -> None:
    print("\n5. Extractor YouTube has outrun — update yt-dlp")
    reset()
    original = youtube.ytdlp_version
    youtube.ytdlp_version = lambda: "2024.04.09"
    try:
        install(FakeYtDlp([STALE_EXTRACTOR]))
        _segments, _source, message = attempt()
    finally:
        youtube.ytdlp_version = original
    check("the update is the remedy", "pip install -U yt-dlp" in message, message)
    check("the installed version and its age are named",
          "2024.04.09" in message and "days old" in message)
    check("it does not claim the episode has no captions",
          "No captions available" not in message)


def scenario_private_video() -> None:
    print("\n6. Private video — one request, and no blame on the captions")
    reset()
    stub = install(FakeYtDlp([PRIVATE_VIDEO]))
    _segments, _source, message = attempt()
    check("the video is named as the problem", "Private video" in message, message)
    check("it says no session or client fixes that",
          "no player client or session" in message)
    check("exactly one request was spent", len(stub.clients) == 1,
          f"{len(stub.clients)} request(s)")


def scenario_no_captions() -> None:
    print("\n7. An episode that really has no captions — and the proof")
    reset()
    stub = install(FakeYtDlp([NO_SUBTITLES], meta=[inventory()]))
    _segments, _source, message = attempt()
    check("the honest verdict is still available",
          "No captions available" in message, message)
    check("the episode's own metadata was asked first", stub.probes == 1,
          f"{stub.probes} metadata request(s)")
    check("it offers a way to keep working", "Exact range" in message,
          "a manual clip renders without a transcript, and the message says so")


def scenario_other_language() -> None:
    print("\n8. Captions in a language nobody asked for — auto-detect detects")
    reset()
    stub = install(FakeYtDlp([*(NO_SUBTITLES for _ in CHAIN), "write"],
                            meta=[inventory("ta", "ta-orig")]))
    segments, _source, message = attempt("auto")
    check("the Tamil track is fetched", bool(segments), message)
    check("auto asked for it",
          any(lang.startswith("ta") for lang in stub.languages),
          "languages asked: " + ", ".join(stub.languages))
    reset()
    install(FakeYtDlp([NO_SUBTITLES], meta=[inventory("ta", "ta-orig")]))
    _segments, _source, message = attempt("hi")
    check("an explicit Hindi choice is told what exists instead",
          "Auto-detect" in message and "ta" in message, message)


# --------------------------------------------------------------------------
def _video_id(url: str) -> str:
    match = re.search(r"(?:v=|youtu\.be/|shorts/|live/)([A-Za-z0-9_-]{6,})",
                      str(url or ""))
    return match.group(1) if match else str(url or "").strip()


def _first_seed_episode() -> str:
    """The seed playlist's first episode, so --live works with no argument."""
    try:
        entries = youtube.list_playlist(config.DEFAULT_PLAYLIST, limit=1)
        return str(entries[0]["url"])
    except Exception as exc:                       # noqa: BLE001
        print(f"  could not read the seed playlist ({exc})")
        return ""


def live(url: str) -> int:
    """Diagnose this machine against a real episode. Reads only."""
    print("\nLive diagnosis — this does contact YouTube")
    if not url:
        url = _first_seed_episode()
    if not url:
        print("  no URL to test; pass one: --live <youtube url>")
        return 1
    video_id = _video_id(url)
    print(f"       episode: {url}")

    status = youtube.ytdlp_status()
    age = status["age_days"]
    print(f"       yt-dlp:  {status['version']}"
          + (f" ({age} days old"
             + (" — STALE, update it" if status["stale"] else "")
             + ")" if age is not None else ""))
    print(f"       ffmpeg:  {config.FFMPEG_BIN}")
    cookies = maintenance.cookies_status()
    print("       session: "
          + (f"installed ({cookies['lines']} cookies)" if cookies["present"]
             else "not installed — every request is anonymous"))
    block = youtube.rate_limit_status()
    print("       block:   "
          + (f"standing, {block['minutes']} min left" if block["blocked"]
             else "none"))
    print("       clients: "
          + " -> ".join(client_label(c) for c in youtube.player_client_chain()))

    print("\n  · asking YouTube which caption tracks this episode has…")
    found, kind, error = youtube.caption_inventory(url)
    if found is None:
        check("the metadata request answers", False,
              f"{kind}: {youtube.detail_line(error)}")
    else:
        languages = found["languages"]
        check("the episode reports caption tracks", bool(languages),
              f"title: {found.get('title') or '?'}\n"
              f"manual: {', '.join(found['manual']) or 'none'}\n"
              f"auto-generated: {len(found['auto'])} language(s): "
              f"{', '.join(languages[:12]) or 'none'}")

    print("\n  · fetching a transcript the way Generate does…")
    if youtube.load_cached_transcript(video_id):
        print("         (a cached transcript already exists — deleting it "
              "first so this is a real test)")
        (config.SUBS_DIR / f"{video_id}.segments.json").unlink(missing_ok=True)
    try:
        segments, source = youtube.get_transcript(video_id, url, "auto")
    except youtube.TranscriptUnavailable as exc:
        check("the transcript arrives", False, str(exc))
        print("\n  That message is the diagnosis. Whatever it names is the "
              "thing to fix:")
        print("    · a bot check or withheld captions → Tools ▸ YouTube "
              "session (a signed-in cookies.txt)")
        print("    · a stale extractor → pip install -U yt-dlp, then restart")
        print("    · a connection problem → network / VPN / DNS")
        print("    · a private or region-locked video → another episode")
        return 1
    check("the transcript arrives", bool(segments),
          f"{len(segments)} segments, source {source}")
    return 0 if not failures else 1


def main() -> int:
    print("Qyro — transcript diagnosis")
    print(f"       version: {config.APP_NAME} {config.APP_VERSION}")
    print(f"       data dir: {config.DATA_DIR}")
    if _LIVE:
        arg = ""
        for flag in ("--live", "--url"):
            if flag in sys.argv:
                index = sys.argv.index(flag)
                if index + 1 < len(sys.argv):
                    arg = sys.argv[index + 1]
                    if arg.startswith("-"):
                        arg = ""
        return live(arg)

    print(f"       offline proof — no network calls, stub yt-dlp")
    print("       client chain: "
          + " -> ".join(client_label(c) for c in youtube.player_client_chain()))
    for scenario in (scenario_withheld, scenario_every_client_refused,
                     scenario_bot_check, scenario_dead_connection,
                     scenario_stale_extractor, scenario_private_video,
                     scenario_no_captions, scenario_other_language):
        scenario()
    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED:")
        for label in failures:
            print(f"  - {label}")
        return 1
    print("All checks passed — every caption failure now says what it is and "
          "what to do, and only a caption-less episode is told it has none.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
