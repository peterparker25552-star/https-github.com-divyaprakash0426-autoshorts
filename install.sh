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

echo
echo "============================================================"
echo "  Qyro v0.6.4 installed! Start the app with:  ./run.sh"
echo "  Then open http://localhost:8000 in your browser."
echo "============================================================"
