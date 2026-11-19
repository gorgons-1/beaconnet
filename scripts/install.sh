#!/usr/bin/env bash
# BeaconNet - install / reinstall / update on a Raspberry Pi 5
# (Raspberry Pi OS Lite 64-bit, Debian bookworm or trixie) or any arm64/x86-64 Debian box.
#
#   sudo ./scripts/install.sh                          # interactive: asks for node id and first admin
#   sudo ./scripts/install.sh --node-id GM4ABC-1 --admin-user alice --admin-callsign GM4ABC \
#        --admin-key-file ~/.ssh/id_ed25519.pub        # unattended
#   sudo ./scripts/install.sh --import-secrets /path/bbs-config-....tar.gz   # rebuild a dead Pi
#
# Safe to run again at any time: it keeps existing .env, config/, secrets, database and
# backups, rebuilds the images and restarts the stack. Steps follow docs/installation.md.
# To move to a newer release use scripts/update.sh (docs/updating.md).
set -euo pipefail

INSTALL_DIR=${INSTALL_DIR:-/opt/scotland-bbs}
SRC_DIR=$(cd "$(dirname "$0")/.." && pwd)
CONTAINER_UID=10001

ADMIN_USER="" ADMIN_CALL="" ADMIN_NAME="" ADMIN_KEY_FILE="" IMPORT_SECRETS="" SKIP_DOCKER=0 SKIP_BACKUP_TEST=0
NODE_ID="" NODE_NAME=""
usage() { sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --node-id) NODE_ID=${2^^}; shift 2 ;;
    --node-name) NODE_NAME=$2; shift 2 ;;
    --admin-user) ADMIN_USER=$2; shift 2 ;;
    --admin-callsign) ADMIN_CALL=$2; shift 2 ;;
    --admin-name) ADMIN_NAME=$2; shift 2 ;;
    --admin-key-file) ADMIN_KEY_FILE=$2; shift 2 ;;
    --import-secrets) IMPORT_SECRETS=$2; shift 2 ;;
    --install-dir) INSTALL_DIR=$2; shift 2 ;;
    --skip-docker-install) SKIP_DOCKER=1; shift ;;
    --skip-backup-test) SKIP_BACKUP_TEST=1; shift ;;
    -h|--help) usage 0 ;;
    *) echo "unknown option: $1" >&2; usage 1 ;;
  esac
done

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '    ok: %s\n' "$*"; }
warn() { printf '    WARNING: %s\n' "$*" >&2; }
die()  { printf '\nINSTALL FAILED: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run with sudo (it installs Docker and protects the secrets)"
[[ -n "${SUDO_USER:-}" && "$SUDO_USER" != root ]] && OPERATOR=$SUDO_USER || OPERATOR=""

# --- 1. operating system and architecture ------------------------------------------
step "1/13 Checking operating system and hardware"
# shellcheck disable=SC1091
. /etc/os-release
case "${ID:-}" in debian|raspbian) ;; *) die "needs Debian or Raspberry Pi OS (found ${ID:-unknown})" ;; esac
case "${VERSION_CODENAME:-}" in bookworm|trixie) ;; *) die "needs Debian 12 bookworm or 13 trixie (found ${VERSION_CODENAME:-?})" ;; esac
ARCH=$(uname -m)
case "$ARCH" in aarch64|x86_64) ;; *) die "needs a 64-bit OS (arm64 or x86-64); found $ARCH" ;; esac
ok "$PRETTY_NAME on $ARCH"
if grep -q "Raspberry Pi" /proc/device-tree/model 2>/dev/null; then ok "$(tr -d '\0' < /proc/device-tree/model)"; fi
ROOT_DEV=$(findmnt -no SOURCE / || true)
case "$ROOT_DEV" in
  /dev/nvme*) STORAGE=nvme; ok "root filesystem on NVMe ($ROOT_DEV)" ;;
  /dev/mmcblk*) STORAGE=sd
    ok "root filesystem on an SD card ($ROOT_DEV) - supported; using the SD-card profile (fewer writes)"
    echo "    Use a good A1/A2 card (16 GB+), keep backups OFF the card (USB stick or BACKUP_REMOTE)," ;
    echo "    and see docs/installation.md 'Running from an SD card'." ;;
  /dev/sd*) STORAGE=usb; ok "root filesystem on a USB/SATA drive ($ROOT_DEV)" ;;
  *) STORAGE=other; ok "root filesystem on $ROOT_DEV" ;;
esac
MEM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
(( MEM_MB >= 900 )) || die "needs at least 1 GB of RAM (found ${MEM_MB} MB)"
(( MEM_MB >= 1800 )) && ok "${MEM_MB} MB RAM" || warn "${MEM_MB} MB RAM: it runs, but 2 GB or more is recommended"
FREE_MB=$(df -Pm / | awk 'NR==2 {print $4}')
(( FREE_MB > 3000 )) || die "less than 3 GB free on / (${FREE_MB} MB)"
ok "${FREE_MB} MB free"

# Copy the project to INSTALL_DIR if we were started from somewhere else (e.g. a fresh copy).
if [[ "$SRC_DIR" != "$(realpath -m "$INSTALL_DIR")" ]]; then
  step "Copying project from $SRC_DIR to $INSTALL_DIR"
  mkdir -p "$INSTALL_DIR"
  command -v rsync >/dev/null || { apt-get update -qq && apt-get install -y -qq rsync; }
  rsync -a --delete --exclude /.env --exclude /.env.imported --exclude '/secrets/*' --exclude /backups \
        --exclude /config/ --exclude /state/ --exclude ansible/inventory.ini \
        --exclude '__pycache__' --exclude '.pytest_cache' "$SRC_DIR"/ "$INSTALL_DIR"/
  ok "copied (your .env, config/ and secrets in $INSTALL_DIR are kept)"
  [[ -d "$INSTALL_DIR/.git" ]] && ok "git checkout: future updates come from $(git -C "$INSTALL_DIR" remote get-url origin 2>/dev/null || echo 'its origin')"
  args=()
  [[ -n "$NODE_ID" ]] && args+=(--node-id "$NODE_ID")
  [[ -n "$NODE_NAME" ]] && args+=(--node-name "$NODE_NAME")
  [[ -n "$ADMIN_USER" ]] && args+=(--admin-user "$ADMIN_USER")
  [[ -n "$ADMIN_CALL" ]] && args+=(--admin-callsign "$ADMIN_CALL")
  [[ -n "$ADMIN_NAME" ]] && args+=(--admin-name "$ADMIN_NAME")
  [[ -n "$ADMIN_KEY_FILE" ]] && args+=(--admin-key-file "$(realpath "$ADMIN_KEY_FILE")")
  [[ -n "$IMPORT_SECRETS" ]] && args+=(--import-secrets "$(realpath "$IMPORT_SECRETS")")
  (( SKIP_DOCKER )) && args+=(--skip-docker-install)
  (( SKIP_BACKUP_TEST )) && args+=(--skip-backup-test)
  exec env INSTALL_DIR="$INSTALL_DIR" bash "$INSTALL_DIR/scripts/install.sh" "${args[@]}"
fi
cd "$INSTALL_DIR"

# --- 2. Docker and Compose -----------------------------------------------------------
step "2/13 Docker Engine and the Compose plugin"
if docker compose version >/dev/null 2>&1; then
  ok "$(docker --version)"; ok "$(docker compose version)"
elif (( SKIP_DOCKER )); then
  die "docker compose not available and --skip-docker-install given"
else
  echo "    installing Docker from download.docker.com ..."
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl gnupg openssh-client rsync python3 git >/dev/null
  for pkg in docker.io docker-doc docker-compose podman-docker containerd runc; do
    dpkg -s "$pkg" >/dev/null 2>&1 && apt-get remove -y -qq "$pkg" >/dev/null || true
  done
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
  ok "$(docker --version)"; ok "$(docker compose version)"
fi
systemctl enable --now docker >/dev/null 2>&1
systemctl is-enabled docker >/dev/null && ok "docker starts at boot (the BBS comes back by itself after a power cut)"
for tool in ssh ssh-keygen python3 rsync git; do
  command -v "$tool" >/dev/null || { apt-get update -qq; apt-get install -y -qq openssh-client python3 rsync git >/dev/null; break; }
done
if [[ -n "$OPERATOR" ]] && ! id -nG "$OPERATOR" | grep -qw docker; then
  usermod -aG docker "$OPERATOR"
  ok "added $OPERATOR to the docker group (takes effect at their next login)"
fi
# Raspberry Pi kernels boot with the memory cgroup controller off, so container memory
# limits would be silently ignored. Turn it on (needs one reboot).
if grep -qw memory /sys/fs/cgroup/cgroup.controllers 2>/dev/null; then
  ok "kernel memory cgroup enabled (container memory limits are enforced)"
else
  CMDLINE=/boot/firmware/cmdline.txt
  [[ -f $CMDLINE ]] || CMDLINE=/boot/cmdline.txt
  if [[ -f $CMDLINE ]]; then
    if ! grep -qw "cgroup_enable=memory" "$CMDLINE"; then
      cp -a "$CMDLINE" "$CMDLINE.bak-scotland-bbs"
      sed -i '1 s/$/ cgroup_enable=memory cgroup_memory=1/' "$CMDLINE"
      ok "added cgroup_enable=memory to $CMDLINE (backup: $CMDLINE.bak-scotland-bbs)"
    fi
    echo
    echo "    The kernel memory controller turns on at the next boot. Reboot, then run this"
    echo "    installer again - it carries on from here:"
    echo "        sudo reboot"
    echo "        sudo $INSTALL_DIR/scripts/install.sh"
    if [[ -t 0 ]]; then
      read -rp "    Reboot now? [y/N] " ans
      [[ "$ans" == [yY]* ]] && { sync; systemctl reboot; }
    fi
    exit 3
  else
    warn "memory cgroup controller not available: container memory limits will not be enforced"
  fi
fi

# --- 3. configuration and persistent storage -----------------------------------------
step "3/13 Configuration and persistent storage"
setenv() {  # setenv KEY VALUE: set or add KEY=VALUE in .env
  if grep -q "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else printf '%s=%s\n' "$1" "$2" >> .env; fi
}
FRESH_ENV=0
if [[ ! -f .env ]]; then
  cp .env.example .env
  FRESH_ENV=1
  IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}' | head -1)
  [[ -n "$IP" ]] && setenv BBS_PUBLIC_HOST "$IP"
  ok "created .env from .env.example (BBS_PUBLIC_HOST=${IP:-unset}) - review it"
else
  ok "keeping existing .env"
fi
# Settings added in later releases are appended with their defaults.
while IFS= read -r line; do
  key=${line%%=*}
  [[ "$line" =~ ^[A-Z_]+= ]] && ! grep -q "^$key=" .env && { echo "$line" >> .env; ok "added new setting ${line%%#*}"; }
done < .env.example
# Node id: must be unique in the mesh, so ask on a first install.
if [[ -z "$NODE_ID" && $FRESH_ENV -eq 1 && -t 0 ]]; then
  suggest=$(hostname -s | tr 'a-z_' 'A-Z-' | tr -cd 'A-Z0-9-')
  [[ ${#suggest} -ge 3 ]] || suggest=BEACON-NODE-01
  echo "    Every node in the mesh needs its own id (A-Z 0-9 -, 3-32 chars), e.g. GM4ABC-1 or BEACON-NORTH-01."
  while :; do
    read -rp "    Node id [$suggest]: " ans; ans=${ans:-$suggest}; ans=${ans^^}
    [[ "$ans" =~ ^[A-Z0-9][A-Z0-9-]{2,31}$ ]] && { NODE_ID=$ans; break; }
    echo "    not valid - use A-Z, 0-9 and '-', 3-32 characters"
  done
  read -rp "    Node name shown to users [BeaconNet BBS]: " NODE_NAME
fi
[[ -n "$NODE_ID" ]] && { [[ "$NODE_ID" =~ ^[A-Z0-9][A-Z0-9-]{2,31}$ ]] || die "bad node id $NODE_ID"; setenv BBS_NODE_ID "$NODE_ID"; ok "node id $NODE_ID"; }
NODE_NAME=$(printf '%s' "$NODE_NAME" | tr -cd "A-Za-z0-9 .,()-" | cut -c1-60)
[[ -n "$NODE_NAME" ]] && setenv BBS_NODE_NAME "\"$NODE_NAME\""
if [[ $STORAGE == sd ]]; then
  # SD-card profile: smaller container logs (fewer writes, less space). Only if not set by hand.
  [[ -z "$(grep -E '^BBS_LOG_MAX_SIZE=[^ #]' .env || true)" ]] && setenv BBS_LOG_MAX_SIZE 2m
  [[ -z "$(grep -E '^BBS_LOG_MAX_FILES=[^ #]' .env || true)" ]] && setenv BBS_LOG_MAX_FILES 3
  ok "SD-card profile: container logs capped at 2 MB x 3 per service"
fi
VERSION=$(tr -d ' \n' < VERSION)
setenv BBS_VERSION "$VERSION"
ok "software version $VERSION"
chmod 0644 .env   # no secrets in .env; docker-group members need it for 'docker compose'
# config/ is this node's own configuration (not tracked by git). New files come from config.example/.
install -d -m 0755 config state
for f in config.example/*.yaml; do
  [[ -f "config/$(basename "$f")" ]] || { cp "$f" config/; ok "created config/$(basename "$f")"; }
done
set -a; # shellcheck disable=SC1091
. ./.env; set +a
[[ "${BBS_ADMIN_BIND:-127.0.0.1}" == "0.0.0.0" || "${BBS_ADMIN_BIND:-}" == "::" ]] && die "BBS_ADMIN_BIND must not be 0.0.0.0 - the admin console must stay private"
[[ -n "${BBS_PUBLIC_HOST:-}" ]] || die "set BBS_PUBLIC_HOST in .env"
BACKUP_DIR=${BBS_BACKUP_DIR:-/var/backups/scotland-bbs}
install -d -m 0750 -o "$CONTAINER_UID" -g "$CONTAINER_UID" "$BACKUP_DIR"
ok "backup directory $BACKUP_DIR"
if [[ $STORAGE == sd && "$(findmnt -no SOURCE --target "$BACKUP_DIR")" == "$ROOT_DEV" && -z "${BACKUP_REMOTE:-}" ]]; then
  warn "backups are on the same SD card as the database - if the card dies you lose both."
  warn "Set BBS_BACKUP_DIR to a USB stick or BACKUP_REMOTE in .env (docs/installation.md)."
fi
ok "database volume scotland-bbs_bbs-db (under /var/lib/docker on $ROOT_DEV)"

# --- 4. secrets ---------------------------------------------------------------------
step "4/13 Secrets (gateway tokens and SSH host keys)"
install -d -m 0700 secrets
if [[ -n "$IMPORT_SECRETS" ]]; then
  [[ -f "$IMPORT_SECRETS" ]] || die "no such file: $IMPORT_SECRETS"
  tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
  tar -xzf "$IMPORT_SECRETS" -C "$tmp"
  [[ -d "$tmp/secrets" ]] || die "$IMPORT_SECRETS has no secrets/ directory (expected a bbs-config-*.tar.gz from backup.sh)"
  cp -a "$tmp/secrets/." secrets/
  [[ -f "$tmp/.env" && ! -s .env.imported ]] && cp "$tmp/.env" .env.imported && ok "previous .env saved as .env.imported (compare with .env)"
  [[ -d "$tmp/config" ]] && cp -a "$tmp/config/." config/ && ok "restored config/ from the backup"
  ok "imported secrets from $(basename "$IMPORT_SECRETS")"
fi
gen_token() { [[ -s "secrets/$1" ]] && return 0; head -c 48 /dev/urandom | base64 | tr -d '\n/+=' > "secrets/$1"; echo "    generated $1"; }
gen_key() {
  [[ -s "secrets/$1" ]] && return 0
  if [[ $2 == rsa ]]; then ssh-keygen -q -t rsa -b 3072 -N '' -C "scotland-bbs" -f "secrets/$1"; else ssh-keygen -q -t ed25519 -N '' -C "scotland-bbs" -f "secrets/$1"; fi
  echo "    generated $1"
}
gen_token ssh_gateway_token
gen_token admin_gateway_token
gen_token sync_gateway_token
gen_key ssh_host_ed25519_key ed25519
gen_key ssh_host_rsa_key rsa
gen_key admin_host_ed25519_key ed25519
gen_key node_ed25519_key ed25519      # this node's identity in the mesh (in its peer card)
[[ $(cat secrets/*_gateway_token | sort | uniq -d | wc -l) -eq 0 ]] || die "two gateway tokens are identical"
for f in ssh_gateway_token admin_gateway_token sync_gateway_token ssh_host_ed25519_key ssh_host_rsa_key \
         admin_host_ed25519_key node_ed25519_key; do
  chown "$CONTAINER_UID:$CONTAINER_UID" "secrets/$f"; chmod 0400 "secrets/$f"
done
chmod 0644 secrets/*.pub 2>/dev/null || true
ok "secrets/ is root-only; each file is readable only by the container user ($CONTAINER_UID)"
for k in ssh_host_ed25519_key ssh_host_rsa_key; do ok "BBS host key: $(ssh-keygen -lf "secrets/$k.pub")"; done

# --- 5-6. networks and images -------------------------------------------------------
step "5/13 Internal networks (created by Compose: edge, mgmt, sync-edge, core-public, core-admin, core-sync)"
docker compose config --quiet || die "docker-compose.yml / .env don't validate"
ok "compose configuration valid"
step "6/13 Building images for $ARCH (first build on a Pi takes a few minutes)"
docker compose build --pull
ok "images built"

# --- 7. migrations ------------------------------------------------------------------
step "7/13 Database migrations"
docker compose run --rm --no-deps -T bbs-core check-config >/dev/null || die "configuration invalid (see message above)"
docker compose run --rm --no-deps -T bbs-core migrate
ok "schema up to date"

# --- 8. first administrator ---------------------------------------------------------
step "8/13 Initial administrator"
ADMINS=$(docker compose run --rm --no-deps -T bbs-core admin-count | tr -dc 0-9)
if [[ "${ADMINS:-0}" -gt 0 ]]; then
  ok "$ADMINS administrator(s) already exist"
else
  create_admin() {
    local key_line=""
    if [[ -n "$ADMIN_KEY_FILE" ]]; then
      [[ -f "$ADMIN_KEY_FILE" ]] || { echo "    no such file: $ADMIN_KEY_FILE"; return 1; }
      key_line=$(head -n1 "$ADMIN_KEY_FILE")
    else
      key_line=${ADMIN_KEY_LINE:-}
    fi
    [[ -n "$key_line" ]] || { echo "    no admin public key given"; return 1; }
    [[ "$key_line" == *"PRIVATE KEY"* ]] && { echo "    that is a PRIVATE key - give the .pub file"; return 1; }
    local args=(create-admin --username "$ADMIN_USER" --key -)
    [[ -n "$ADMIN_CALL" ]] && args+=(--callsign "$ADMIN_CALL")
    [[ -n "$ADMIN_NAME" ]] && args+=(--display-name "$ADMIN_NAME")
    printf '%s\n' "$key_line" | docker compose run --rm --no-deps -T bbs-core "${args[@]}"
  }
  if [[ -z "$ADMIN_USER" && -t 0 ]]; then
    echo "    No administrator yet. Admins log in to the admin console with an SSH key."
    for attempt in 1 2 3; do
      read -rp "    Admin username (3-20 chars, a-z 0-9 _ -) [${ADMIN_USER}]: " ans; ADMIN_USER=${ans:-$ADMIN_USER}
      read -rp "    Amateur callsign, e.g. GM4ABC (blank if none): " ADMIN_CALL
      read -rp "    Path to the admin's SSH PUBLIC key (.pub), or paste the key [${ADMIN_KEY_FILE}]: " ans
      if [[ -n "$ans" ]]; then
        if [[ -f "$ans" ]]; then ADMIN_KEY_FILE=$ans; ADMIN_KEY_LINE=""; else ADMIN_KEY_FILE=""; ADMIN_KEY_LINE=$ans; fi
      fi
      if create_admin; then ok "administrator $ADMIN_USER created"; break; fi
      (( attempt < 3 )) || die "could not create the administrator"
      echo "    Try again (Enter keeps the value in [brackets])."
    done
  elif [[ -n "$ADMIN_USER" ]]; then
    create_admin || die "could not create the administrator"
    ok "administrator $ADMIN_USER created"
  else
    warn "no administrator created; run: sudo ./scripts/bootstrap-admin.sh --username NAME --key-file KEY.pub"
  fi
fi

# --- 9-10. start and health ---------------------------------------------------------
step "9/13 Starting services"
docker compose up -d --remove-orphans
step "10/13 Waiting for health checks"
for _ in $(seq 1 60); do
  states=$(docker compose ps --format '{{.Service}}={{.Health}}' 2>/dev/null | sort | tr '\n' ' ')
  [[ "$states" == *"bbs-admin=healthy"*"bbs-core=healthy"*"bbs-ssh=healthy"*"bbs-sync=healthy"* ]] && break
  sleep 3
done
echo "    $states"
[[ "$states" == *"bbs-admin=healthy"*"bbs-core=healthy"*"bbs-ssh=healthy"*"bbs-sync=healthy"* ]] || die "services not healthy - see: docker compose logs"
ok "all services healthy"

SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o LogLevel=ERROR -o ConnectTimeout=10)
# --- 11. guest access ---------------------------------------------------------------
step "11/13 Verifying guest terminal access"
out=$(printf 'Q\n' | timeout 30 ssh -T "${SSH_OPTS[@]}" -p "${BBS_SSH_PORT:-2222}" guest@127.0.0.1 2>&1 || true)
grep -q "BEACONNET" <<<"$out" || { echo "$out" | tail -5; die "guest login did not show the BBS"; }
ok "guest session shows the BBS menu"
out=$(timeout 20 ssh -T "${SSH_OPTS[@]}" -p "${BBS_SSH_PORT:-2222}" guest@127.0.0.1 id 2>&1 || true)
grep -q "uid=" <<<"$out" && die "a command ran on the gateway!"
ok "commands are refused (no shell)"

# --- 12. admin access ---------------------------------------------------------------
step "12/13 Verifying the administrator console"
ADMIN_BIND=${BBS_ADMIN_BIND:-127.0.0.1}
out=$(timeout 20 ssh -T "${SSH_OPTS[@]}" -o PreferredAuthentications=none,publickey -i /dev/null -p "${BBS_ADMIN_PORT:-2223}" nobody@"$ADMIN_BIND" true 2>&1 || true)
grep -q "Permission denied" <<<"$out" || { echo "$out" | tail -3; die "admin console did not refuse an unauthenticated login"; }
ok "admin console listening on $ADMIN_BIND:${BBS_ADMIN_PORT:-2223} and refusing logins without an admin key"
if [[ "$ADMIN_BIND" == 127.* ]]; then
  pub_ip=${BBS_PUBLIC_HOST}
  if timeout 5 bash -c "exec 3<>/dev/tcp/$pub_ip/${BBS_ADMIN_PORT:-2223}" 2>/dev/null; then die "admin port answers on $pub_ip"; fi
  ok "admin port is not reachable on $pub_ip"
fi
ADMINS=$(docker compose exec -T bbs-core python -m bbs_core.cli admin-count | tr -dc 0-9)
ok "$ADMINS active administrator(s)"

# --- 13. backups --------------------------------------------------------------------
step "13/13 Backups"
install -m 0644 scripts/systemd/scotland-bbs-backup.service /etc/systemd/system/scotland-bbs-backup.service
sed -e "s|@BACKUP_TIME@|${BACKUP_TIME:-03:15}|" scripts/systemd/scotland-bbs-backup.timer > /etc/systemd/system/scotland-bbs-backup.timer
sed -i "s|@INSTALL_DIR@|$INSTALL_DIR|g" /etc/systemd/system/scotland-bbs-backup.service
sed "s|@INSTALL_DIR@|$INSTALL_DIR|g" scripts/systemd/scotland-bbs-update-check.service > /etc/systemd/system/scotland-bbs-update-check.service
install -m 0644 scripts/systemd/scotland-bbs-update-check.timer /etc/systemd/system/scotland-bbs-update-check.timer
systemctl daemon-reload
systemctl enable --now scotland-bbs-backup.timer >/dev/null 2>&1
if [[ -d .git ]]; then
  systemctl enable --now scotland-bbs-update-check.timer >/dev/null 2>&1
  ok "weekly update check enabled (BBS_AUTO_UPDATE=${BBS_AUTO_UPDATE:-false})"
else
  systemctl disable --now scotland-bbs-update-check.timer >/dev/null 2>&1 || true
  warn "not a git checkout, so no update checks: install from a git clone to get updates (docs/updating.md)"
fi
ok "daily backup timer: $(systemctl show -p TimersCalendar --value scotland-bbs-backup.timer | sed 's/.*OnCalendar=\([^;]*\).*/\1/')"
if (( SKIP_BACKUP_TEST )); then
  warn "backup test skipped"
else
  ./scripts/backup.sh
  ok "test backup written and verified"
fi

CARD=$(./scripts/peer-card.sh 2>/dev/null || echo "(run scripts/peer-card.sh)")
set -a; # shellcheck disable=SC1091
. ./.env; set +a
cat <<EOF

=====================================================================
 BeaconNet ${VERSION} is running on node ${BBS_NODE_ID}

 Visitors:        ssh -p ${BBS_SSH_PORT:-2222} guest@${BBS_PUBLIC_HOST}
 Register:        ssh -p ${BBS_SSH_PORT:-2222} new@${BBS_PUBLIC_HOST}
 Approved users:  ssh -p ${BBS_SSH_PORT:-2222} <username>@${BBS_PUBLIC_HOST}
 Admin console:   ssh -J <you>@${BBS_PUBLIC_HOST} -p ${BBS_ADMIN_PORT:-2223} <admin>@127.0.0.1
                  (or on the Pi itself: ssh -p ${BBS_ADMIN_PORT:-2223} <admin>@127.0.0.1)

 Status:   sudo $INSTALL_DIR/scripts/healthcheck.sh
 Logs:     cd $INSTALL_DIR && docker compose logs -f
 Backups:  $BACKUP_DIR  (daily at ${BACKUP_TIME:-03:15}; docs/disaster-recovery.md)
 Updates:  sudo $INSTALL_DIR/scripts/update.sh   (docs/updating.md)

 Mesh: give this peer card to the sysop of a node you want to link with,
 and add theirs with:  sudo $INSTALL_DIR/scripts/add-peer.sh '<their card>'
   $CARD
=====================================================================
EOF
