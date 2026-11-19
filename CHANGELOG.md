# Changelog

Releases are git tags (`vX.Y.Z`). Nodes update with `sudo scripts/update.sh`.

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
