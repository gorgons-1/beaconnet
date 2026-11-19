#!/bin/bash
# BeaconNet Desktop for macOS: double-click to start (first time: right-click > Open).
cd "$(dirname "$0")" || exit 1
PY=$(command -v python3) || { osascript -e 'display dialog "Python 3 is needed. Install it from python.org, then open BeaconNet again." buttons {"OK"}'; exit 1; }
"$PY" -c "import asyncssh" 2>/dev/null || "$PY" -m pip install --user --disable-pip-version-check -r requirements.txt || exit 1
nohup "$PY" beaconnet_desktop.py >/dev/null 2>&1 &
