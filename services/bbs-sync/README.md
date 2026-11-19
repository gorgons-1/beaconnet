# bbs-sync

Node-to-node store-and-forward. It carries envelopes between this node's queue in
bbs-core and its peers over SSH (port 2224). Each link is encrypted, and both ends
prove who they are with pinned ed25519 node keys.

* Code: [bbs_sync/link.py](bbs_sync/link.py), where the protocol is described at the top.
* Rules (dedup, loops, hop limit, expiry, what is accepted): bbs-core
  [mesh.py](../bbs-core/bbs_core/mesh.py).
* Envelope format: [shared/protocols/message-envelope.md](../../shared/protocols/message-envelope.md).
* Operating it (peer cards, NAT, firewall): [docs/mesh.md](../../docs/mesh.md).
