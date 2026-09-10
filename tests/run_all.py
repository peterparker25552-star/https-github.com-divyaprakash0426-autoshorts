#!/usr/bin/env python3
"""AutoShorts test runner: byte-compile, node --check, then every test module.

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
        for script in ("app.js",):
            proc = subprocess.run(
                [node, "--check", str(REPO_ROOT / "web" / script)],
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
        "tests.test_http_fastapi",
        "tests.test_http_stdlib",
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
