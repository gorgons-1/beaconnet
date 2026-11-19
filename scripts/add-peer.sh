#!/usr/bin/env bash
# Link this node with another one, using the peer card its sysop gave you.
#
#   sudo ./scripts/add-peer.sh 'SCOTBBS-PEER/1 NORTH-01 north.example.org 2224 ssh-ed25519 AAAA... Inverness'
#   sudo ./scripts/add-peer.sh --list
#
# Both sysops must add each other's card. Running it again with a new card for the same
# node id updates that peer (e.g. its address changed). Remove with remove-peer.sh.
set -euo pipefail
cd "$(dirname "$0")/.."
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
set -a; # shellcheck disable=SC1091
. ./.env; set +a
if [[ "${1:-}" == "--list" ]]; then exec docker compose run --rm --no-deps -T bbs-core peer-list; fi
card=${1:-}
[[ -n "$card" ]] || { sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }
cp -a config/peers.yaml "config/peers.yaml.bak-$(date -u +%Y%m%dT%H%M%SZ)"
docker compose run --rm --no-deps -T --user 0 -v "$PWD/config:/work" bbs-core \
  peer-add --file /work/peers.yaml --card "$card" --own-key "$(cat secrets/node_ed25519_key.pub)"
docker compose restart bbs-core bbs-sync >/dev/null
echo "peer added; bbs-core and bbs-sync restarted."
echo "Give the other sysop this node's card if you haven't yet:"
./scripts/peer-card.sh
