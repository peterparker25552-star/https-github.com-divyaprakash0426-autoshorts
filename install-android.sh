#!/usr/bin/env bash
# ============================================================
#   AutoShorts - installer for ANDROID (run inside Termux)
#   Get Termux from F-Droid: https://f-droid.org/packages/com.termux/
# ============================================================
set -e
cd "$(dirname "$0")"

if [ -z "$TERMUX_VERSION" ]; then
  echo
  echo " [X] This must run inside Termux on Android."
  echo     "     Install Termux from F-Droid, then try again."
  echo
  exit 1
fi

echo "Updating Termux packages..."
pkg update -y || true
pkg install -y python ffmpeg

echo "Creating a private Python environment..."
if python -m venv .venv 2>/dev/null; then
  PY=".venv/bin/python"
else
  echo " (venv unavailable — using system Python)"
  PY="python"
fi

echo "Installing AutoShorts dependencies..."
$PY -m pip install --upgrade pip --quiet
# note: ffmpeg comes from 'pkg install ffmpeg' (native ARM build) —
# the imageio-ffmpeg wheel is skipped on purpose (x86-only binaries).
$PY -m pip install --quiet fastapi uvicorn yt-dlp

echo
echo "============================================================"
echo "  Installed! Start the app with:  bash run-android.sh"
echo "  Then open http://localhost:8000 in Chrome."
echo "============================================================"
