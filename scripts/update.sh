#!/usr/bin/env bash
# Update this node to the latest release from its git repository (GitHub).
#
#   sudo ./scripts/update.sh              # show what's new, ask, back up, update, restart
#   sudo ./scripts/update.sh --check      # only check; exit 10 if an update is available
#   sudo ./scripts/update.sh --yes        # don't ask
#   sudo ./scripts/update.sh --to v1.1.0  # a specific release (also how you roll back)
#   sudo ./scripts/update.sh --local      # rebuild from the code already in this directory
#
# Releases are git tags (v1.2.3). Your .env, config/, secrets/, database and backups are
# not in git and are never touched. Every update takes a verified backup first.
# The weekly timer runs --auto, which checks and only installs when BBS_AUTO_UPDATE=true.
set -euo pipefail
# git checkout replaces this file while it runs, so run from a copy.
if [[ -z "${BBS_UPDATE_DIR:-}" ]]; then
  BBS_UPDATE_DIR=$(cd "$(dirname "$0")/.." && pwd)
  copy=$(mktemp /tmp/scotland-bbs-update.XXXXXX)
  cp "$0" "$copy"
  BBS_UPDATE_DIR=$BBS_UPDATE_DIR exec bash "$copy" "$@"
fi
trap 'rm -f "$0"' EXIT
cd "$BBS_UPDATE_DIR"
DIR=$(pwd)
MODE=update YES=0 TARGET="" QUIET=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --check) MODE=check; shift ;;
    --auto) MODE=auto; QUIET=1; shift ;;
    --yes|-y) YES=1; shift ;;
    --to) TARGET=$2; shift 2 ;;
    --local) MODE=local; shift ;;
    -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
say() { (( QUIET )) || echo "$@"; }
status() {  # status <state> <latest> <message>: shown in the admin console (System health)
  install -d -m 0755 state
  printf '{"checked_at":"%s","current":"%s","latest":"%s","state":"%s","message":"%s"}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$CURRENT" "$2" "$1" "$3" > state/update.json.tmp
  chmod 0644 state/update.json.tmp && mv state/update.json.tmp state/update.json
}

CURRENT=$(tr -d ' \n' < VERSION)
if [[ $MODE == local ]]; then
  ./scripts/backup.sh
  INSTALL_DIR="$DIR" ./scripts/install.sh --skip-backup-test
  exit $?
fi
[[ -d .git ]] || { echo "this install isn't a git checkout, so it can't fetch updates."
                   echo "Reinstall from a clone (docs/updating.md), or use --local after copying new code here."; exit 1; }
git config --global --get-all safe.directory 2>/dev/null | grep -qx "$DIR" || git config --global --add safe.directory "$DIR"

say "Checking $(git remote get-url origin) for releases ..."
if ! git fetch --quiet --tags --force --prune origin; then
  status error "" "could not reach the update server"
  echo "could not fetch from $(git remote get-url origin) - is the network up?" >&2; exit 1
fi
LATEST=${TARGET:-$(git tag -l 'v[0-9]*' --sort=-v:refname | head -n1)}
[[ -n "$LATEST" ]] || { status error "" "the repository has no releases (v* tags)"; echo "no releases (v* tags) found" >&2; exit 1; }
git rev-parse -q --verify "refs/tags/$LATEST" >/dev/null || { echo "no such release: $LATEST" >&2; exit 1; }
NEW=${LATEST#v}
HERE=$(git rev-parse HEAD)
THERE=$(git rev-parse "$LATEST^{commit}")

if [[ "$HERE" == "$THERE" ]]; then
  status current "$NEW" "up to date"
  say "Up to date: $CURRENT"
  exit 0
fi
newer=$(printf '%s\n%s\n' "$CURRENT" "$NEW" | sort -V | tail -n1)
if [[ -z "$TARGET" && "$newer" == "$CURRENT" && "$NEW" != "$CURRENT" ]]; then
  status current "$NEW" "running $CURRENT, newer than the latest release"
  say "Running $CURRENT, which is newer than the latest release $NEW; nothing to do."
  exit 0
fi
status available "$NEW" "release $NEW is available (running $CURRENT)"

if [[ $MODE == check || $MODE == auto ]]; then
  say "Update available: $CURRENT -> $NEW"
  say "Changes:"; (( QUIET )) || git log --oneline --no-decorate "HEAD..$THERE" | head -30 | sed 's/^/  /'
  [[ $MODE == check ]] && exit 10
  set -a; # shellcheck disable=SC1091
  . ./.env; set +a
  [[ "${BBS_AUTO_UPDATE:-false}" == "true" ]] || exit 0
  YES=1
  echo "BBS_AUTO_UPDATE=true: installing $NEW"
fi

echo "Update: $CURRENT -> $NEW"
git log --oneline --no-decorate "HEAD..$THERE" | head -30 | sed 's/^/  /'
[[ -f CHANGELOG.md ]] && echo "  (full notes: CHANGELOG.md after the update)"
if (( ! YES )); then
  [[ -t 0 ]] || { echo "not a terminal: re-run with --yes" >&2; exit 1; }
  read -rp "Back up and update now? Users are disconnected for about a minute. [y/N] " ans
  [[ "$ans" == [yY]* ]] || { echo "cancelled"; exit 0; }
fi

echo "==> Backup before updating"
./scripts/backup.sh
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  stash="local-changes-$(date -u +%Y%m%dT%H%M%SZ)"
  git -c user.name=scotland-bbs -c user.email=bbs@localhost stash push --quiet -m "$stash"
  echo "    local edits to tracked files were stashed as '$stash' (git stash list)"
fi
echo "==> Switching code to $LATEST"
git -c advice.detachedHead=false checkout --quiet "$LATEST"
echo "==> Rebuilding and restarting (migrations run automatically)"
if INSTALL_DIR="$DIR" ./scripts/install.sh --skip-backup-test; then
  CURRENT=$NEW status current "$NEW" "updated to $NEW"
  echo "Updated to $NEW."
else
  rc=$?
  status error "$NEW" "update to $NEW failed during install"
  echo "The update to $NEW failed (exit $rc). The previous code is at $HERE." >&2
  echo "Roll back:  sudo ./scripts/update.sh --to <previous tag>   and, if the database was migrated," >&2
  echo "restore the backup just taken: sudo ./scripts/restore.sh <file>  (docs/updating.md)" >&2
  exit "$rc"
fi
