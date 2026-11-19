#!/usr/bin/env bash
# Rotate secrets. Old files are kept in secrets/old-<time>/ until you delete them.
#
#   sudo ./scripts/rotate-secrets.sh --tokens      # new gateway<->core API tokens (no user impact)
#   sudo ./scripts/rotate-secrets.sh --host-keys   # new SSH host keys: every user sees a
#                                                  # "host key changed" warning - announce it first
set -euo pipefail
cd "$(dirname "$0")/.."
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
what=${1:-}
case "$what" in --tokens) files=(ssh_gateway_token admin_gateway_token sync_gateway_token) ;;
                --host-keys) files=(ssh_host_ed25519_key ssh_host_rsa_key admin_host_ed25519_key) ;;
                *) sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;; esac
old="secrets/old-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$old" && chmod 700 "$old"
for f in "${files[@]}"; do mv "secrets/$f" "$old/"; [[ -f "secrets/$f.pub" ]] && mv "secrets/$f.pub" "$old/"; done
echo "old secrets moved to $old"
# install.sh regenerates whatever is missing and restarts the stack with it.
INSTALL_DIR=$(pwd) ./scripts/install.sh --skip-backup-test
