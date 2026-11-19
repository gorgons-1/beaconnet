#!/usr/bin/env bash
# Print this node's peer card: one line to give to the sysop of a node you want to link
# with. It holds no secrets - just the node id, where to reach it, and its public node key.
#   ./scripts/peer-card.sh
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; # shellcheck disable=SC1091
. ./.env; set +a
[[ -r secrets/node_ed25519_key.pub ]] || { echo "no node key yet - run sudo ./scripts/install.sh" >&2; exit 1; }
host=${BBS_SYNC_PUBLIC_HOST:-$BBS_PUBLIC_HOST}
args=(peer-card --port "${BBS_SYNC_PORT:-2224}")
[[ "$host" != "-" ]] && args+=(--host "$host")
docker compose run --rm --no-deps -T bbs-core "${args[@]}" < secrets/node_ed25519_key.pub
