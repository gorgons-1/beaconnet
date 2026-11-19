# Linking nodes (the mesh)

Each BBS node can link with other nodes. Linked nodes pass messages along:

* **Bulletins** on shared boards (all boards by default, EMERGENCY included) reach every
  linked node. They also pass on through it to the nodes it is linked with.
* **Private mail** to `user@NODE-ID` travels from node to node until it reaches that
  node. The sender then gets a delivery receipt: the message shows DELIVERED, or FAILED
  with a reason.

It is store-and-forward. If a link is down, messages wait in the queue, up to 30 days for
bulletins and 14 days for mail. They are delivered when the link comes back. Nothing
depends on a central server, so any node can link to any other.

## How a link works

* Each node has one **node key** (`secrets/node_ed25519_key`), created by the installer.
* Two sysops swap **peer cards**: one line each, holding the node id, the address to dial
  and the public node key. A card holds no secrets, so you can paste it into email, chat
  or a bulletin.
* Links run over SSH on TCP **2224**. Each end checks the other's key against the card it
  was given. The link is encrypted and both ends prove who they are. A node not in your
  `peers.yaml` can't connect, and a node with the wrong key is refused.
* Only one side has to be reachable. A node behind NAT, such as a home broadband router
  or 4G, dials out to its peer and sends and receives over that one connection.

## Link two nodes

On **each** node:

```sh
sudo /opt/scotland-bbs/scripts/peer-card.sh
```

```text
SCOTBBS-PEER/1 GM4ABC-1 bbs.example.org 2224 ssh-ed25519 AAAAC3Nza...  Highland Node
```

Send your card to the other sysop and get theirs. Then add theirs:

```sh
sudo /opt/scotland-bbs/scripts/add-peer.sh 'SCOTBBS-PEER/1 GM4XYZ-1 - 2224 ssh-ed25519 AAAA... Fife Node'
```

That's all. Both sysops must add each other. Then check the admin console's **Connected
Nodes** screen: it shows each peer's status (CONNECTED, UNREACHABLE or REFUSED), its
queue and the last sync. Or watch `docker compose logs -f bbs-sync`.

Other commands:

```sh
sudo scripts/add-peer.sh --list           # peers this node has
sudo scripts/add-peer.sh '<new card>'     # same node id again = update (e.g. new address)
sudo scripts/remove-peer.sh GM4XYZ-1      # unlink
```

`config/peers.yaml` is plain YAML, so you can also edit it by hand. After a hand edit,
run `docker compose restart bbs-core bbs-sync`.

## Reachability

| This node | What to set |
|---|---|
| Has a public IP or a port-forward | Forward **TCP 2224** to the Pi. Put the public name or IP in `BBS_SYNC_PUBLIC_HOST` (in `.env`) so your card shows it. |
| Behind NAT, can't be reached | Set `BBS_SYNC_PUBLIC_HOST=-`. Your card then says `-`, so peers wait for you to dial them. Link with at least one reachable node. |
| On a VPN (WireGuard, Tailscale, ZeroTier) | Use the VPN address in `BBS_SYNC_PUBLIC_HOST`. Nothing else changes. |

If both nodes are behind NAT, neither can dial the other. Each one links to a third node
that is reachable, and messages relay through it.

After changing `.env`, run `docker compose up -d`, then hand out your card again.

## Topology tips

* The mesh floods: a bulletin goes to every peer except where it came from. Duplicates
  are dropped by message id, and each message records its route, so loops stop on their
  own. A message crosses at most `mesh.hop_limit` nodes (default 8).
* Two or three links per node is plenty. With more, each message is sent more often but
  arrives no sooner.
* Mail goes straight to the destination node when it is a direct peer. Otherwise it is
  flooded like a bulletin and only the destination delivers it. Nodes in between store
  and relay it, but it never appears in anyone's mailbox there. To stop a node relaying
  mail for others, set `mesh.forward_private_mail: false` in `config/bbs.yaml`.
* To keep a board's posts on one node, set `forward: false` on that board in
  `config/boards.yaml` and restart bbs-core.

## Node ids

Every node id must be unique across the mesh, because mail is addressed `user@NODE-ID`.
A good scheme is the sysop's callsign plus a number (`GM4ABC-1`), or a region
(`SCOTLAND-NORTH-01`). The installer asks for one on a first install. Don't change it
later: peers know you by it, so a change means swapping cards again.

## Trust and moderation

* Trust is hop by hop. You decide which nodes to link with, and you take their traffic.
  Every envelope is still checked: its size, format, payload hash, route, hop count and
  dates. Each peer also has a rate limit, `mesh.max_envelopes_per_peer_per_hour`.
* A message from another node shows its author as `alice@GM4XYZ-1`. Its callsign
  is the one the origin node recorded; your node hasn't verified it.
* Moderation is local. Hiding or deleting a bulletin here doesn't touch the other nodes'
  copies. To stop a misbehaving node, remove it with `remove-peer.sh`.
* If your node key leaks, delete `secrets/node_ed25519_key*` and run `install.sh` to
  make a new one. Then hand every peer your new card.

## Security notes

* Port 2224 accepts only public-key logins, and only from keys listed in `peers.yaml`. It
  offers no shell, exec, port forwarding or sftp, only the sync protocol.
* bbs-sync holds no data and can't open user sessions. Its API token works only on
  bbs-core's `/v1/sync/*` routes, on its own internal network.
* Links are encrypted, but mail is not end-to-end encrypted. Every node that relays it
  can read it, just as the sysop of your own node could.
* When the radio link arrives (Phase 3), only content the RF policy allows will go over
  the air, and never encrypted. Over radio the same protocol runs in the clear, as the
  licence requires. See [radio-integration.md](radio-integration.md).
