#!/usr/bin/env bash
# ============================================================
#   AutoShorts - start the app (macOS / Linux)
# ============================================================
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "Please run ./install.sh first."
  exit 1
fi
echo
echo "  AutoShorts is starting... keep this terminal OPEN."
echo "  Open http://localhost:8000 in your browser."
echo "  To stop the app, press Ctrl+C."
echo
exec .venv/bin/python run.py --host 127.0.0.1 --port 8000
