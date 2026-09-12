#!/usr/bin/env python3
"""Qyro test runner: byte-compile, node --check, then every test module.

v0.5.0 added ``test_units_v050`` (logo remover maths, beat peak-picking, audio
graphs, caption brands, the free engine's fallbacks, the media prober) and
``test_http_v050`` (every new route on both servers, the PWA assets, the
no-emoji rule and one real 1440p end-to-end render).

v0.6.0 added ``test_units_v060`` (the subject tracker's maths, caption
fonts/animations/languages, transitions, the quality gate, option validation)
and ``test_http_v060`` (the new API surface plus a real render that measures
where the subject lands with tracking on and off).

v0.6.3 added ``test_units_v063`` (the spectrum ident: its timeline, the
autoplay-unlock dance and the CSS/markup contract) which runs
``tests/ident_harness.js`` — a headless node harness that drives web/intro.js
against a stubbed DOM and WebAudio and asserts the ta-dum is really scheduled
in step with the beams.

v0.6.6 added ``test_units_v066``: the ident that can never strand the app
(wall-clock catch-up, the dismissal timer, a hidden tab, a lost canvas, the
reload cool-down — and the harness's starved-frame scenarios), the labelled
demo placeholder that can never pass for a downloaded video, the ``?dl=1``
download disposition, and the retired-model rescue in the free AI engine.

v0.6.8 added ``test_units_v068``: the other half of the ident contract — an
*open* of the app must be greeted. A launch is told from a reload, a return to
the foreground after 30 s counts as a launch, a busy app gets a two-second
ident instead of none, the greeting no longer waits for /api/health, and
``recover_interrupted_jobs`` stops a dead server's jobs from reporting the app
busy forever (proved end to end against a real stdlib server restart).

Usage:  python -m tests.run_all   (or python tests/run_all.py from the repo)
"""
from __future__ import annotations

import compileall
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def step(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(4, 60 - len(title)))


def main() -> int:
    failures = 0

    step("py_compileall")
    ok = compileall.compile_dir(
        str(REPO_ROOT / "autoshorts"), quiet=1, force=True
    ) and compileall.compile_dir(str(REPO_ROOT / "tests"), quiet=1, force=True)
    print("compileall:", "OK" if ok else "FAILED")
    failures += 0 if ok else 1

    step("node --check (web UI)")
    node = shutil.which("node")
    if node:
        for script in ("web/app.js", "web/intro.js", "web/sw.js",
                       "tests/ident_harness.js"):
            proc = subprocess.run(
                [node, "--check", str(REPO_ROOT / script)],
                capture_output=True, text=True,
            )
            print(f"node --check {script}:", "OK" if proc.returncode == 0 else proc.stderr)
            failures += proc.returncode != 0
    else:
        print("node not found — skipped")

    step("unit + HTTP tests")
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for module in (
        "tests.test_units",
        "tests.test_units_v050",
        "tests.test_http_fastapi",
        "tests.test_http_stdlib",
        "tests.test_http_v050",
        "tests.test_units_v060",
        "tests.test_units_v061",
        "tests.test_http_v060",
        "tests.test_units_v063",
        "tests.test_units_v065",
        "tests.test_units_v066",
        # v0.6.7 — the YouTube 429 path: player-client rotation, on-disk
        # transcript recovery, an honest cooldown, and the session upload
        # that lets a phone user apply the real fix.
        "tests.test_units_v067",
        "tests.test_http_v067",
        # v0.6.8 — "the intro does not come when I open the app": a launch is
        # not a reload, a resume is a launch, busy shortens instead of
        # cancelling, and a dead server's jobs stop looking like work.
        "tests.test_units_v068",
    ):
        suite.addTests(loader.loadTestsFromName(module))
    runner = unittest.TextTestRunner(verbosity=1)
    result = runner.run(suite)
    failures += len(result.failures) + len(result.errors)
    skipped = len(result.skipped)

    print("\n" + "=" * 66)
    print(
        f"RESULT: {'ALL GREEN ✅' if failures == 0 else f'{failures} FAILURE(S) ❌'}"
        f" — {result.testsRun} tests, {failures} failed, {skipped} skipped"
    )
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
