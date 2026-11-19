#!/usr/bin/env bash
# Back up the BBS: online SQLite backup (verified), plus config, .env and secrets.
# Run as root (the systemd timer does this daily). Writes status.json for the admin console.
#
#   sudo ./scripts/backup.sh
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; # shellcheck disable=SC1091
. ./.env; set +a
DIR=${BBS_BACKUP_DIR:-/var/backups/scotland-bbs}
KEEP=${BACKUP_KEEP_DAYS:-14}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
STATUS="$DIR/status.json"
STARTED=$(date -u +%Y-%m-%dT%H:%M:%SZ)

write_status() {  # result db_file error offsite
  python3 - "$STATUS" "$1" "$2" "$3" "$4" "$STARTED" <<'PY'
import json, sys, datetime, os
path, result, db_file, error, offsite, started = sys.argv[1:7]
try:
    old = json.load(open(path))
except Exception:
    old = {}
now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
new = {"started_at": started, "finished_at": now, "result": result, "db_file": db_file or old.get("db_file"),
       "error": error or None, "offsite": offsite or None,
       "last_success_at": now if result == "ok" else old.get("last_success_at")}
tmp = path + ".tmp"
with open(tmp, "w") as fh:
    json.dump(new, fh, indent=1)
os.chmod(tmp, 0o644)
os.replace(tmp, path)
PY
}

fail() {
  write_status failed "" "$1" ""
  logger -t scotland-bbs-backup -p user.err "BACKUP FAILED: $1" || true
  echo "BACKUP FAILED: $1" >&2
  exit 1
}
trap 'fail "unexpected error at line $LINENO"' ERR

[[ $EUID -eq 0 ]] || { echo "run as root (secrets are root-only)" >&2; exit 1; }
[[ -d "$DIR" ]] || fail "backup directory $DIR missing"
docker compose ps --status running --services 2>/dev/null | grep -qx bbs-core || fail "bbs-core is not running"

# 1. Database: SQLite online backup API inside bbs-core (safe while users are connected).
out=$(docker compose exec -T bbs-core python -m bbs_core.cli backup) || fail "database backup command failed"
db_file=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["file"])' "$out")
# 2. Verify the compressed copy independently (checksum + integrity_check of the decompressed file).
docker compose exec -T bbs-core python -m bbs_core.cli verify "/backups/$db_file" >/dev/null || fail "verification of $db_file failed"

# 3. Configuration, .env and secrets (root-only file). Needed to rebuild on a replacement Pi.
cfg_file="bbs-config-$STAMP.tar.gz"
( umask 077; tar -czf "$DIR/$cfg_file" config .env secrets docker-compose.yml )
tar -tzf "$DIR/$cfg_file" >/dev/null || fail "config archive unreadable"

# 4. Retention.
find "$DIR" -maxdepth 1 -type f \( -name 'bbs-*.sqlite3.gz' -o -name 'bbs-*.sqlite3.gz.sha256' -o -name 'bbs-config-*.tar.gz' \) \
     -mtime +"$KEEP" -delete

# 5. Optional off-site copy.
offsite=""
if [[ -n "${BACKUP_REMOTE:-}" ]]; then
  if rsync -a --timeout=60 -e "ssh -o BatchMode=yes -o ConnectTimeout=15" \
       "$DIR/$db_file" "$DIR/$db_file.sha256" "$DIR/$cfg_file" "$BACKUP_REMOTE"; then
    offsite="copied to $BACKUP_REMOTE at $(date -u +%H:%MZ)"
  else
    trap - ERR
    write_status failed "$db_file" "local backup ok, but off-site copy to $BACKUP_REMOTE failed" ""
    logger -t scotland-bbs-backup -p user.err "off-site copy failed (local backup $db_file is fine)" || true
    exit 1
  fi
fi

trap - ERR
write_status ok "$db_file" "" "$offsite"
echo "backup ok: $DIR/$db_file + $DIR/$cfg_file${offsite:+ ($offsite)}"
