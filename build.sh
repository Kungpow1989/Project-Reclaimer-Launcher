#!/usr/bin/env bash
# Builds the launcher on macOS or Linux. Output goes in ./release
#   macOS : Reclaimer-Launcher-macOS.zip  (contains "Reclaimer Launcher.app")
#   Linux : reclaimer-launcher-linux-<arch>.tar.gz  (single executable)
#
# Needs Python 3.9+ with Tk:
#   macOS : python.org installer, or `brew install python python-tk`
#   Linux : e.g. `sudo apt install python3 python3-venv python3-tk`
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
"$PY" -c "import tkinter" || { echo "This Python has no Tk. See the comments at the top of build.sh."; exit 1; }

"$PY" -m venv .venv-build
# shellcheck disable=SC1091
source .venv-build/bin/activate
python -m pip install --quiet --upgrade pip pyinstaller -r requirements.txt

rm -rf build dist release
mkdir -p release
COMMON=(--noconfirm --clean --add-data "assets:assets" --hidden-import PIL._tkinter_finder)

if [[ "$(uname -s)" == "Darwin" ]]; then
  python -m PyInstaller "${COMMON[@]}" --windowed --name "Reclaimer Launcher" \
    --icon assets/icon.png --osx-bundle-identifier local.reclaimer.launcher launcher.py
  "dist/Reclaimer Launcher.app/Contents/MacOS/Reclaimer Launcher" --version
  ditto -c -k --keepParent "dist/Reclaimer Launcher.app" release/Reclaimer-Launcher-macOS.zip
else
  python -m PyInstaller "${COMMON[@]}" --onefile --windowed --name reclaimer-launcher launcher.py
  ./dist/reclaimer-launcher --version
  tar -C dist -czf "release/reclaimer-launcher-linux-$(uname -m).tar.gz" reclaimer-launcher
fi

# One checksum file per download, e.g. reclaimer-launcher-linux-x86_64.tar.gz.sha256
(cd release && for f in *; do
  if command -v sha256sum >/dev/null; then sha256sum "$f"; else shasum -a 256 "$f"; fi > "$f.sha256"
done)

deactivate
echo
echo "Done:"
ls -1 release
