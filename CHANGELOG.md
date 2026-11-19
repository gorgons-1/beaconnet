# Changelog

Releases are git tags (`vX.Y.Z`). Nodes update with `sudo scripts/update.sh`.

## 1.4.0 - BeaconNet Desktop

* **BeaconNet Desktop** (`clients/desktop`): a small app for Windows, macOS and Linux.
  It pops up desktop notifications for new bulletins, EMERGENCY traffic, private mail
  and (for sysops) new registrations, and serves a page at `http://127.0.0.1:8765` to
  read, post, send mail and approve registrations. It logs in once with your password,
  then with its own key ("remember this computer").
* The node offers a `beaconnet-client` SSH subsystem on port 2222 for it: JSON lines,
  a fixed list of operations, and the logged-in person's own rights. There's a new
  `/v1/feed` route in bbs-core.

## 1.3.0 - easy logins and a Sysop menu

* **No more copying keys.** When someone registers with `ssh new@<node>`, the BBS sees
  the SSH key their device already has and asks "Use this device's key to log in?".
* **Passwords for devices without a key.** They register with a password instead, and
  log in with `ssh <username>@<node>` and their password. Passwords are scrypt-hashed,
  rate-limited, and lock the account for 15 minutes after 10 wrong tries. Users can set
  or change a password in *My Account & Keys*. `registration.allow_passwords` turns it off.
* **Sysop menu in the BBS.** Administrators connecting from a private LAN address
  (10/8, 172.16/12, 192.168/16 by default, `sysop.allowed_networks`) get
  **S. Sysop Menu**, the full admin console. From the internet they get the user menu.
  The separate console on port 2223 is unchanged and still key-only.
* Database migration 0003 (password columns).

## 1.2.0 - renamed to BeaconNet

* The project is now called **BeaconNet**, a name for operators anywhere in the world.
  The banner, menus, docs and defaults no longer assume Scotland or the UK.
* The emergency notice says to call your local emergency number (112, 911, 999...)
  instead of only 999/112.
* New installs get a REGIONAL board instead of SCOTLAND, and example node ids look like
  `BEACON-HUB-01`.
* Nothing changes for an existing node: its node id, node name, boards, data, install
  folder (`/opt/scotland-bbs`) and peer cards all stay as they are, and nodes on 1.1.0
  and 1.2.0 link with each other.

## 1.1.0 - mesh and updates

* **Mesh:** a new `bbs-sync` service links nodes over SSH on port 2224, with pinned
  ed25519 node keys. Bulletins on shared boards, including EMERGENCY, flood to all
  linked nodes. Mail to `user@NODE-ID` is relayed to its node, which sends back a
  delivery receipt. Messages are stored and forwarded while a link is down. Duplicates,
  loops, the hop limit and expiry are all handled. A node behind NAT dials out.
* Peer cards: `scripts/peer-card.sh`, `add-peer.sh` and `remove-peer.sh`.
* The admin console shows each peer's status, queue, last sync and errors. System health
  shows the software version and whether an update is available.
* **Updates from GitHub:** `scripts/update.sh` (`--check`, `--to`, `--yes`, `--local`)
  backs up, checks out the release tag, rebuilds and migrates. A weekly check timer
  installs updates only if `BBS_AUTO_UPDATE=true`.
* The installer asks for a node id that is unique in the mesh. It also adds new `.env`
  settings, and creates `config/` from `config.example/`. `config/` is now node-local
  and not tracked.
* Database migration 0002: `boards.forward`, a rebuilt `forward_queue`, `sync_seen`, and
  peer columns on `nodes`.
* Boards accept `forward: false` in `boards.yaml` to keep a board local.

## 1.0.0 - local BBS

* SSH terminal BBS with guest access, registration with sysop approval, boards, an
  emergency board, private mail, an admin console, backups, tests and docs.
