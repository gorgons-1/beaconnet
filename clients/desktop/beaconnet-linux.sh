#!/bin/sh
# BeaconNet Desktop for Linux. Uses the distro's asyncssh if present, else a private venv.
cd "$(dirname "$0")" || exit 1
if python3 -c "import asyncssh" 2>/dev/null; then PY=python3
else
  VENV="${XDG_DATA_HOME:-$HOME/.local/share}/beaconnet/venv"
  [ -x "$VENV/bin/python" ] || { python3 -m venv "$VENV" && "$VENV/bin/pip" install -q -r requirements.txt; } || {
    echo "Install python3-venv (or python3-asyncssh) and try again."; exit 1; }
  PY="$VENV/bin/python"
fi
exec "$PY" beaconnet_desktop.py "$@"
