#!/usr/bin/env bash
# Create an administrator directly in bbs-core (only someone with root on the Pi can).
#
#   sudo ./scripts/bootstrap-admin.sh --username alice --callsign GM4ABC --key-file /home/alice/.ssh/id_ed25519.pub
#   ... or pipe the public key on stdin instead of --key-file.
set -euo pipefail
cd "$(dirname "$0")/.."
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
args=(create-admin --key -)
KEY_FILE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --username) args+=(--username "$2"); shift 2 ;;
    --callsign) args+=(--callsign "$2"); shift 2 ;;
    --display-name) args+=(--display-name "$2"); shift 2 ;;
    --key-file) KEY_FILE=$2; shift 2 ;;
    *) sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
  esac
done
if [[ -n "$KEY_FILE" ]]; then key=$(head -n1 "$KEY_FILE"); else read -r key; fi
[[ "$key" == *"PRIVATE KEY"* ]] && { echo "that is a PRIVATE key - give the .pub file" >&2; exit 1; }
if docker compose ps --status running --services | grep -qx bbs-core; then
  printf '%s\n' "$key" | docker compose exec -T bbs-core python -m bbs_core.cli "${args[@]}"
else
  printf '%s\n' "$key" | docker compose run --rm --no-deps -T bbs-core "${args[@]}"
fi
