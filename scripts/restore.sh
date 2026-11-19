#!/usr/bin/env bash
# Restore the BBS database from a backup (and optionally config/.env/secrets).
#
#   sudo ./scripts/restore.sh /var/backups/scotland-bbs/bbs-20261118T031500Z.sqlite3.gz
#   sudo ./scripts/restore.sh <db-backup.sqlite3.gz> --config <bbs-config-....tar.gz>
#
# The backup is verified before anything is touched. The current database is kept beside
# the restored one (bbs.sqlite3.pre-restore-<time>) inside the volume.
set -euo pipefail
cd "$(dirname "$0")/.."
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
DB_BACKUP=${1:-}
[[ -n "$DB_BACKUP" && -f "$DB_BACKUP" ]] || { sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }
CONFIG_TAR=""
[[ "${2:-}" == "--config" ]] && CONFIG_TAR=${3:?--config needs a file}
set -a; # shellcheck disable=SC1091
. ./.env; set +a
DIR=${BBS_BACKUP_DIR:-/var/backups/scotland-bbs}

name=$(basename "$DB_BACKUP")
if [[ "$(realpath "$(dirname "$DB_BACKUP")")" != "$(realpath "$DIR")" ]]; then
  cp "$DB_BACKUP" "$DIR/$name"
  [[ -f "$DB_BACKUP.sha256" ]] && cp "$DB_BACKUP.sha256" "$DIR/$name.sha256"
  chown 10001:10001 "$DIR/$name"*
fi

echo "==> Verifying $name"
docker compose run --rm --no-deps -T bbs-core verify "/backups/$name"

read -rp "Restore will stop the BBS and replace its database. Continue? [y/N] " ans
[[ "$ans" == [yY]* ]] || { echo "cancelled"; exit 1; }

if [[ -n "$CONFIG_TAR" ]]; then
  echo "==> Restoring config, .env and secrets from $(basename "$CONFIG_TAR")"
  keep="pre-restore-$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p "backups-local/$keep" && chmod 700 backups-local
  cp -a config .env secrets "backups-local/$keep/" 2>/dev/null || true
  tar -xzf "$CONFIG_TAR" config .env secrets
  echo "    previous files kept in backups-local/$keep"
fi

echo "==> Stopping services"
docker compose stop bbs-ssh bbs-admin bbs-core
echo "==> Restoring database"
docker compose run --rm --no-deps -T bbs-core restore "/backups/$name"
echo "==> Starting services"
docker compose up -d
"$(dirname "$0")/healthcheck.sh" || true
echo "Restore complete."
