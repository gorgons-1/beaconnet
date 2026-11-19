# Installation

## What you need

* A Raspberry Pi 5 (4 GB or more) running **Raspberry Pi OS Lite 64-bit**: Debian 12
  bookworm or 13 trixie. A conventional arm64 or x86-64 Debian box also works.
* Storage: an **NVMe SSD** is best and a USB SSD is fine. An **SD card** works too (see
  below). The installer detects which you have.
* At least 1 GB of RAM, 2 GB or more recommended (Pi 3, 4 or 5).
* An Ethernet connection, plus internet during installation and updates (Docker packages,
  base images, Python wheels, the git repository). The BBS itself needs no internet; mesh
  links need a route to at least one peer (LAN, VPN or internet).
* An SSH key pair for the first administrator (`ssh-keygen -t ed25519` on your PC).

## Install

```sh
sudo apt-get install -y git
git clone https://github.com/gorgons-1/beaconnet.git ~/scotland-bbs
sudo ~/scotland-bbs/scripts/install.sh
```

Installing from a git clone is what lets the node update itself later
([updating.md](updating.md)). The installer asks for the **node id** (unique in the mesh;
pattern `REGION-PLACE-NN`, e.g. `SCOT-OBAN-01`, or a callsign such as `GM4ABC-1`), a
friendly node name and location, the first admin's username, callsign and **public** key. You can
give them on the command line instead:

```sh
sudo ~/scotland-bbs/scripts/install.sh --node-id SCOT-OBAN-01 --node-name "Oban Packet Radio BBS" \
     --node-location "Oban, Scotland" \
     --admin-user alice --admin-callsign GM4ABC --admin-key-file ~/admin_key.pub
```

On a Raspberry Pi the first run turns on the kernel memory cgroup (so container memory
limits work), asks you to reboot and exits with code 3. Run the same command again after
the reboot.

What it does, in order (it stops at the first failure and says why):

1. Checks the OS (Debian or Raspberry Pi OS, bookworm or trixie), the architecture
   (aarch64 or x86_64), storage type (NVMe, USB or SD card), RAM and free space. Copies the project to `/opt/scotland-bbs`.
2. Installs Docker Engine and the Compose plugin from download.docker.com if they are
   missing, and enables Docker at boot. Adds you to the `docker` group.
3. Creates `.env` from `.env.example` and fills in this Pi's IP as `BBS_PUBLIC_HOST`,
   the node id and the version; adds any settings new in this release to an existing
   `.env`. Creates `config/` from `config.example/` (only files that don't exist yet)
   and `/var/backups/scotland-bbs`.
4. Generates the secrets in `secrets/`: three gateway API tokens, three SSH host keys and
   the node key that identifies this node to its peers.
   Each file is mode 0400, owned by the container user (10001). It imports them instead
   if you pass `--import-secrets`.
5. Validates the Compose file. The networks are `edge`, `mgmt`, `sync-edge`,
   `core-public`, `core-admin` and `core-sync` (the `core-*` ones are internal).
6. Builds the four images locally for this architecture, on top of the pinned base image
   and locked dependencies.
7. Validates `config/*.yaml` and applies the database migrations.
8. Creates the first administrator, if there isn't one yet.
9. Starts the stack (`restart: unless-stopped`).
10. Waits for all four health checks.
11. Logs in as `guest` over SSH and checks the menu appears. Checks that a command
    (`ssh ... id`) is refused.
12. Checks the admin console refuses a login without a key, and that its port is not
    reachable on the LAN address.
13. Installs the daily backup timer and the weekly update-check timer, then makes and
    verifies a test backup. Prints this node's peer card.

## Running from an SD card

It installs and runs the same way. The installer notices the SD card and:

* caps each container's logs at 2 MB × 3 instead of 10 MB × 5
  (`BBS_LOG_MAX_SIZE` / `BBS_LOG_MAX_FILES` in `.env`)
* warns you if the backups would land on the same card as the database.

The BBS itself writes very little: small SQLite transactions, with nothing written while
it's idle. Cards fail from age and power cuts more than from this load. To make that
less likely:

1. **Use a good card.** Pick a known brand, A1 or A2 rated, 16 GB or more. A "high
   endurance" card is better still.
2. **Keep backups off the card.** Plug in a USB stick and point `BBS_BACKUP_DIR` at it
   (for example `/mnt/usb/scotland-bbs`, mounted from `/etc/fstab` with `nofail`). You
   can also set `BACKUP_REMOTE` to copy each backup to another machine. Then re-run
   `install.sh`.
3. **Use a stable power supply,** ideally a UPS HAT. An unclean power cut is the usual
   way an SD card gets corrupted. The database survives power cuts (WAL with
   `synchronous=FULL`), but the card's filesystem may not.
4. Optionally cut the OS's own writes: `log2ram`, or a volatile journald
   (`Storage=volatile` in `/etc/systemd/journald.conf`).

If the card does die, rebuild from the backups. See
[disaster-recovery.md](disaster-recovery.md).

## Configure

* `.env` holds the node id, the address shown to users, ports, the admin bind address,
  the backup directory, retention, the backup time and an optional off-site rsync target.
* `config/bbs.yaml` holds the node name, guest and registration policy, timeouts, rate
  limits, message limits, retention and log level.
* `config/boards.yaml` holds the boards created on first start.
* `config/peers.yaml` holds the peer nodes (managed with `scripts/add-peer.sh`, see
  [mesh.md](mesh.md)).
* `config/` belongs to this node and is not in git; `config.example/` holds the
  defaults that new installs start from.

Apply changes with `docker compose up -d`, or `docker compose restart bbs-core` for YAML
edits. Invalid values stop bbs-core with a message that names the field. Check with
`docker compose logs bbs-core`.

## Commands

```sh
cd /opt/scotland-bbs
docker compose ps                      # state and health
docker compose logs -f bbs-core        # JSON logs (no message bodies, keys or tokens)
docker compose stop                    # stop everything
docker compose up -d                   # start (also re-applies .env changes)
sudo ./scripts/update.sh               # latest release from GitHub: backup, rebuild, restart
sudo ./scripts/healthcheck.sh          # one-screen health report
./scripts/run-tests.sh                 # automated tests (in a container)
```

## Firewall

The BBS publishes `BBS_SSH_PORT` (2222) on `BBS_SSH_BIND` and the mesh port
`BBS_SYNC_PORT` (2224) on `BBS_SYNC_BIND`. The admin console listens on
`BBS_ADMIN_BIND` (127.0.0.1). Docker manages its own iptables rules for published ports.
If you put the node on the internet, forward only TCP 2222 (users) and, if peers should
be able to dial you, TCP 2224.
