# BeaconNet

**BeaconNet** is a network of independent, volunteer-run bulletin boards for
amateur radio operators anywhere in the world. Each node is a terminal BBS in the style
of LinBPQ, BPQMail and LinFBB, and nodes link up to pass bulletins and mail along, like
beacon fires from hill to hill. A node runs on a Raspberry Pi 5, or any 64-bit Debian
box, in Docker. It needs no cloud services and keeps working with no internet connection.

* People connect over **SSH** and use a classic menu, with no Linux shell. Registering
  is easy: if the user's device has an SSH key the BBS offers to use it (no copying
  keys about), otherwise they pick a password.
* It has bulletin boards, an **EMERGENCY** board for prioritised, area-tagged and
  expiring bulletins, and private mail.
* Accounts are approved by a sysop, and callsigns are checked.
* Nodes **link into a mesh**. They swap bulletins and mail (`user@NODE-ID`) over
  encrypted SSH links with pinned keys, store and forward when a link is down, and need
  no central server.
* **Updates come from GitHub.** One command, or a weekly check, with a backup taken
  before every update.

> This BBS is run by volunteers. It is **not** an official emergency service and
> delivery is not guaranteed. In an emergency call your local emergency number (**112**, **911**, **999**, ...).

## Using it

```text
ssh -p 2222 guest@<node>        # read public bulletins, no account needed
ssh -p 2222 new@<node>          # register (a sysop approves every account)
ssh -p 2222 <username>@<node>   # log in: automatic with your device's key, else your password
```

Sysops log in the same way. An administrator connecting from the local network
(10.x, 172.16-31.x, 192.168.x) gets an extra **S. Sysop Menu** to approve users, moderate
and check the node. From the internet the same account sees only the normal menu.

**Commodore 64 and other 40-column screens:** the BBS fits itself to 40 columns, in plain
ASCII with no colour codes, when your SSH client reports a narrow screen or a terminal type
such as `c64` or `petscii`. Press **W** at the main menu to switch between 40 and 80
columns by hand.

### On your computer: BeaconNet Desktop

Don't want to open SSH to check for news? **[BeaconNet Desktop](clients/desktop/README.md)**
runs on Windows, macOS and Linux. It pops up a notification for new bulletins,
EMERGENCY traffic, mail and (for sysops) registrations, and gives you a browser page to
read, post, send mail and approve users.

**Download:** [Windows](https://github.com/gorgons-1/beaconnet/releases/latest/download/BeaconNet-Windows.zip) · [macOS](https://github.com/gorgons-1/beaconnet/releases/latest/download/BeaconNet-macOS.zip) · [Linux](https://github.com/gorgons-1/beaconnet/releases/latest/download/BeaconNet-Linux-x86_64.tar.gz) (more in [clients](clients/README.md))

**[Code signing policy](CODE_SIGNING.md)**: free code signing provided by [SignPath.io](https://about.signpath.io/), certificate by [SignPath Foundation](https://signpath.org/).

## Install a node

You need:

* a Raspberry Pi 5 running Raspberry Pi OS Lite 64-bit (bookworm or trixie), or another
  64-bit Debian 12/13 machine
* storage: an NVMe SSD is best, a USB SSD is fine, and an **SD card works too**. On an SD
  card the installer uses fewer writes; use a good A1/A2 card and keep backups off it
  (see [installation](docs/installation.md#running-from-an-sd-card))
* 1 GB of RAM minimum, 2 GB or more recommended. A Pi 4 or Pi 5 is ideal, and a Pi 3 works.
* Ethernet
* an SSH key pair for yourself as sysop (`ssh-keygen -t ed25519` on your PC)

```sh
sudo apt-get install -y git
git clone https://github.com/gorgons-1/beaconnet.git ~/scotland-bbs
sudo ~/scotland-bbs/scripts/install.sh
```

The installer asks for:

* a **node id**, unique in the mesh, such as `GM4ABC-1`
* your admin username and callsign
* the path to your SSH **public** key

It then installs Docker, generates the secrets and node key, builds and starts everything,
tests guest and admin access, and makes a test backup.

On a Raspberry Pi, the first run turns on the kernel memory controller and asks you to
reboot. Run the same command again after the reboot to finish.

The node lives in `/opt/scotland-bbs`. Running the installer again is always safe. See
[docs/installation.md](docs/installation.md) for details and unattended options.

## Join the mesh

```sh
sudo /opt/scotland-bbs/scripts/peer-card.sh                  # your card: give it to a neighbour
sudo /opt/scotland-bbs/scripts/add-peer.sh '<their card>'    # add theirs
```

Each sysop adds the other's card. Links use TCP **2224**, and only one side needs to be
reachable, so a node behind NAT just dials out. See [docs/mesh.md](docs/mesh.md).

## Updates

```sh
sudo /opt/scotland-bbs/scripts/update.sh            # back up, fetch the latest release, rebuild, restart
sudo /opt/scotland-bbs/scripts/update.sh --check    # just check
```

A weekly timer checks for new releases and shows them in the admin console. It installs
them by itself only if you set `BBS_AUTO_UPDATE=true`. See
[docs/updating.md](docs/updating.md).

## Day to day

| Task | Command (in `/opt/scotland-bbs`) |
|---|---|
| Admin | log in to the BBS from your LAN and press **S**, or `ssh -J <you>@<pi> -p 2223 <admin>@127.0.0.1` |
| Status | `sudo ./scripts/healthcheck.sh` |
| Logs | `docker compose logs -f [bbs-core\|bbs-ssh\|bbs-admin\|bbs-sync]` |
| Stop / start | `docker compose stop` / `docker compose up -d` |
| Update | `sudo ./scripts/update.sh` |
| Peers | `sudo ./scripts/add-peer.sh --list`, `add-peer.sh '<card>'`, `remove-peer.sh ID` |
| Back up now / restore | `sudo ./scripts/backup.sh` / `sudo ./scripts/restore.sh <file>` |
| Rotate keys/tokens | `sudo ./scripts/rotate-secrets.sh --tokens` or `--host-keys` |
| New admin | `sudo ./scripts/bootstrap-admin.sh --username NAME --key-file KEY.pub` |
| Run the tests | `./scripts/run-tests.sh` |

To rebuild a dead Pi from its backups, see [docs/disaster-recovery.md](docs/disaster-recovery.md).

## Firewall / ports

| Port | What | Expose? |
|---|---|---|
| 2222/tcp | BBS for users | yes, to your users (LAN or internet) |
| 2224/tcp | mesh links | yes, if you want peers to be able to dial you |
| 2223/tcp | admin console | **never**: bound to 127.0.0.1 |

## Layout

```text
docker-compose.yml   the four services, networks, limits, secrets
.env.example         per-node settings (copied to .env)
config.example/      default bbs.yaml, boards.yaml, peers.yaml (copied to config/, which is node-local)
migrations/          numbered SQL schema migrations
services/bbs-core    API + all rules + the only SQLite access (FastAPI)
services/bbs-ssh     public SSH terminal (asyncssh)
services/bbs-admin   private admin console (asyncssh)
services/bbs-sync    node-to-node mesh links (asyncssh)
clients/desktop      BeaconNet Desktop: notifications + browser page for your PC
services/bbs-radio   Phase 3 design notes (AX.25 packet radio)
shared/              gateway library, API contract (openapi.json), envelope format
scripts/             install, update, peers, backup, restore, healthcheck, rotate, tests
tests/               unit, integration (real SSH sessions and mesh links), security
docs/                architecture, installation, mesh, updating, administration, security, DR, radio
ansible/             optional: deploy to several nodes from a controller
```

## Status

| Phase | |
|---|---|
| 1: local BBS (users, boards, priority bulletins, mail, admin, backups) | done |
| 2: store-and-forward mesh between nodes, updates from GitHub | done |
| 3: AX.25 packet radio (Dire Wolf / KISS TNC) | planned ([docs/radio-integration.md](docs/radio-integration.md)) |
| 4: UPS / solar power monitoring | planned |

Contributions are welcome: open an issue or a pull request. Run `./scripts/run-tests.sh`
first; it needs only Docker.

## Licence

MIT. See [LICENSE](LICENSE). Running it on the air is up to each operator's own licence
conditions.
