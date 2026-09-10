#!/usr/bin/env bash
# ============================================================
#   Qyro - installer for ANDROID (run inside Termux)
#   Get Termux from F-Droid: https://f-droid.org/packages/com.termux/
#
#   Android uses Qyro's built-in pure-Python server, so there
#   is NOTHING heavy to compile here — no FastAPI / pydantic / Rust
#   (those have no wheels for Termux Python and pip cannot install
#   them). Only yt-dlp (pure Python) is needed for YouTube downloads.
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

echo "Installing yt-dlp (YouTube downloads)..."
if pkg install -y yt-dlp 2>/dev/null; then
  echo " (yt-dlp installed from Termux packages)"
else
  echo " (no yt-dlp in Termux repos — installing via pip)"
  python -m pip install --upgrade pip || true
  if python -m pip install yt-dlp; then
    echo " (yt-dlp installed via pip)"
  else
    echo
    echo " [!] yt-dlp install failed — continuing anyway."
    echo "     Demo mode will still work fully offline."
    echo "     For YouTube downloads, retry later with:  pip install yt-dlp"
    echo
  fi
fi

if ! command -v yt-dlp >/dev/null 2>&1 \
   && ! python -c "import yt_dlp" 2>/dev/null; then
  echo
  echo " [!] yt-dlp was not found."
  echo "     Demo mode works; YouTube mode needs:  pip install yt-dlp"
  echo
fi

echo
echo "============================================================"
echo "  Installed! Start the app with:  bash run-android.sh"
echo "  Then open http://localhost:8000 in Chrome."
echo "============================================================"
