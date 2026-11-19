#!/usr/bin/env bash
# One-screen health report. Exit 0 = healthy, 1 = something needs attention.
#   sudo ./scripts/healthcheck.sh        (works without sudo for docker-group members,
#                                         minus the parts that read root-only files)
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; # shellcheck disable=SC1091
. ./.env; set +a
problems=0
bad() { echo "  PROBLEM: $*"; problems=$((problems + 1)); }

echo "Scotland BBS health - $(date -u +%Y-%m-%dT%H:%M:%SZ) on $(hostname)"
echo "Containers:"
for svc in bbs-core bbs-ssh bbs-admin bbs-sync; do
  st=$(docker compose ps --format '{{.Health}} {{.State}} {{.Status}}' "$svc" 2>/dev/null || true)
  printf '  %-10s %s\n' "$svc" "${st:-not running}"
  [[ "$st" == healthy* ]] || bad "$svc is not healthy"
done

if docker compose exec -T bbs-core python -m bbs_core.cli integrity-check --quick >/dev/null 2>&1; then
  echo "Database: quick_check ok"
else
  bad "database quick_check failed - see docs/disaster-recovery.md"
fi

out=$(printf 'Q\n' | timeout 20 ssh -T -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o BatchMode=yes \
      -o LogLevel=ERROR -p "${BBS_SSH_PORT:-2222}" guest@127.0.0.1 2>&1 || true)
if grep -q "SCOTLAND EMERGENCY PACKET RADIO BBS" <<<"$out"; then echo "Guest login: ok"; else bad "guest login failed"; fi

DIR=${BBS_BACKUP_DIR:-/var/backups/scotland-bbs}
if [[ -r "$DIR/status.json" ]]; then
  python3 - "$DIR/status.json" <<'PY' || problems=$((problems + 1))
import json, sys, datetime
s = json.load(open(sys.argv[1]))
print(f"Backup: last run {s.get('finished_at')} result {s.get('result')}; last success {s.get('last_success_at')}")
ok = s.get("last_success_at")
if s.get("result") != "ok":
    print(f"  PROBLEM: last backup failed: {s.get('error')}")
    sys.exit(1)
if ok:
    age = datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.strptime(ok, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    if age.total_seconds() > 36 * 3600:
        print(f"  PROBLEM: last successful backup is {age} old")
        sys.exit(1)
PY
else
  bad "no backup status yet ($DIR/status.json)"
fi

df -Pm / "$DIR" | awk 'NR>1 {pct=$5; sub("%","",pct); printf "Disk %-28s %6d MB free (%s used)\n", $6, $4, $5; if (pct+0 > 90) exit 1}' \
  || bad "a disk is over 90% full"
awk '/MemAvailable/ {printf "Memory: %d MB available\n", $2/1024}' /proc/meminfo
echo "Uptime: $(uptime -p)"
command -v vcgencmd >/dev/null && echo "Pi: $(vcgencmd measure_temp 2>/dev/null) $(vcgencmd get_throttled 2>/dev/null)"

if (( problems )); then echo "$problems problem(s)"; exit 1; fi
echo "All checks passed."
