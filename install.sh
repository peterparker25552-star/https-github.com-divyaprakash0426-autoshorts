#!/usr/bin/env bash
# ============================================================
#   Qyro - one-click installer (macOS / Linux)          (formerly AutoShorts)
# ============================================================
set -e
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo
  echo " [X] Python 3 not found."
  echo "     Install it from https://www.python.org/downloads/ (macOS)"
  echo "     or with your package manager, then run ./install.sh again."
  echo
  exit 1
fi

echo "Creating a private Python environment..."
python3 -m venv .venv

echo "Installing Qyro dependencies (this downloads ffmpeg too)..."
.venv/bin/python -m pip install --upgrade pip --quiet
.venv/bin/pip install --quiet -r requirements.txt

# Read the version from the checkout instead of maintaining a second,
# easy-to-stale version string in this installer.
APP_VERSION="$(.venv/bin/python -c 'from autoshorts import __version__; print(__version__)' 2>/dev/null || printf '%s' 'unknown')"

echo
echo "============================================================"
echo "  Qyro v${APP_VERSION} installed! Start the app with:  ./run.sh"
echo "  Then open http://localhost:8000 in your browser."
echo "============================================================"
