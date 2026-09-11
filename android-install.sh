#!/usr/bin/env bash
# ============================================================
#   Qyro — ONE-CLICK installer for Android/Termux
#
#   Paste this single line into Termux:
#   pkg update -y && pkg install -y curl && curl -fsSL https://raw.githubusercontent.com/peterparker25552-star/https-github.com-divyaprakash0426-autoshorts/main/android-install.sh -o "$HOME/autoshorts-android-install.sh" && bash "$HOME/autoshorts-android-install.sh"
#
#   Download first and run second: a 404 response must never be piped to bash.
# ============================================================
set -e

if [ -z "$TERMUX_VERSION" ]; then
  echo
  echo " [X] This must run inside the Termux app on Android."
  echo
  exit 1
fi

REF="${AUTOSHORTS_REF:-main}"
REPO="https://github.com/peterparker25552-star/https-github.com-divyaprakash0426-autoshorts.git"

echo
echo ">>> [1/4] Installing Python, ffmpeg and git (5-10 minutes)..."
pkg update -y || true
pkg install -y python ffmpeg git

echo
echo ">>> [2/4] Downloading Qyro..."
rm -rf ~/autoshorts
git clone -q -b "$REF" "$REPO" ~/autoshorts

echo
echo ">>> [3/4] Installing Qyro dependencies (quick — pure Python only)..."
cd ~/autoshorts
bash install-android.sh

echo
echo ">>> [4/4] Creating the start shortcut..."
cat > ~/start-autoshorts.sh <<'EOF'
#!/usr/bin/env bash
bash ~/autoshorts/run-android.sh
EOF
chmod +x ~/start-autoshorts.sh
# try to make 'autoshorts' a command too (works if ~/bin is in PATH)
mkdir -p ~/bin
cp ~/start-autoshorts.sh ~/bin/autoshorts 2>/dev/null || true

echo
echo "============================================================"
echo "  ✅ Qyro v0.6.1 is installed!"
echo
echo "  TO START IT (any time):"
echo "     open Termux and type:  bash ~/start-autoshorts.sh"
echo
echo "  then open Chrome at:      http://localhost:8000"
echo "  (keep Termux open while using it)"
echo
echo "  Want the app icon on your home screen? Open the address in"
echo "  Chrome, then:  menu (⋮) → Add to Home screen."
echo
echo "  Full beginner guide:  ANDROID-GUIDE.md  in this folder"
echo "============================================================"
