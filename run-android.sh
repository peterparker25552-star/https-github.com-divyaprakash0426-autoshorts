#!/usr/bin/env bash
# ============================================================
#   Qyro - launcher for ANDROID (run inside Termux)
# ============================================================
cd "$(dirname "$0")"
# System Python is used on Termux (it is already per-app private, and
# .venv creation fails on some Termux builds). An old .venv from a previous
# install is still honoured if present.
if [ -x .venv/bin/python ]; then
  PY=".venv/bin/python"
elif command -v python >/dev/null 2>&1; then
  PY="python"
else
  echo "Please run:  bash install-android.sh   first."
  exit 1
fi

IP=$(ip addr show wlan0 2>/dev/null | grep -o 'inet [0-9.]*' | awk '{print $2}' | head -1)
echo
echo "  Qyro is starting... keep Termux OPEN and the phone AWAKE."
echo
echo "  On this phone : http://localhost:8000"
[ -n "$IP" ] && echo "  On other devices on the same Wi-Fi: http://$IP:8000"
echo
echo "  To stop: press Ctrl+C (Volume-Down + C on the keyboard)."
echo
exec "$PY" run.py --host 0.0.0.0 --port 8000
