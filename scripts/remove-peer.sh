#!/usr/bin/env bash
# Unlink a peer node:  sudo ./scripts/remove-peer.sh NODE-ID
# Messages still queued for it are kept until they expire (they can't be sent).
set -euo pipefail
cd "$(dirname "$0")/.."
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
[[ -n "${1:-}" ]] || { sed -n '2,3p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }
set -a; # shellcheck disable=SC1091
. ./.env; set +a
cp -a config/peers.yaml "config/peers.yaml.bak-$(date -u +%Y%m%dT%H%M%SZ)"
docker compose run --rm --no-deps -T --user 0 -v "$PWD/config:/work" bbs-core peer-remove --file /work/peers.yaml --id "$1"
docker compose restart bbs-core bbs-sync >/dev/null
echo "peer $1 removed; bbs-core and bbs-sync restarted."
