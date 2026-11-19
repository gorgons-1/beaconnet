# Node-to-node message envelope (v1)

Every message moving between nodes travels in this envelope. bbs-core builds and checks
envelopes (`services/bbs-core/bbs_core/mesh.py`); bbs-sync carries them over SSH
(`services/bbs-sync/bbs_sync/link.py`, protocol described at the top of that file).

```json
{
  "v": 1,
  "kind": "bulletin",                       // bulletin | private | receipt
  "mid": "3f9c...e1@BEACON-HUB-01",       // global id; never changes; dedup key
  "origin_node": "BEACON-HUB-01",
  "destination": {"board": "EMERGENCY"},     // private: {"user","node"}; receipt: {"node"}
  "created_at": "2026-11-18T14:02:00Z",      // UTC, set once by the origin
  "expires_at": "2026-11-21T14:02:00Z",      // optional; expired envelopes are dropped
  "priority": "EMERGENCY",                   // ROUTINE | PRIORITY | EMERGENCY - queue order only
  "hop_count": 2,
  "hop_limit": 8,
  "route": ["BEACON-HUB-01", "BEACON-CENTRAL-02"],
  "payload": {"author": "alice", "callsign": "GM4AAA", "area": "Highland",
              "subject": "...", "body": "...", "parent_mid": null},
  "payload_sha256": "<hex sha256 of the canonical JSON of payload>"
}
```

Canonical JSON = keys sorted, no spaces, UTF-8. Payloads:

* bulletin: `author, callsign?, area?, subject, body, parent_mid?`
* private: `from, callsign?, to, subject, body`
* receipt: `ref_mid, status (DELIVERED|FAILED), reason?` - sent back by the destination

## Rules

* **Dedup:** every accepted `mid` is recorded in `sync_seen` (60 days); a repeat is
  answered `duplicate` and changes nothing. Every answer (accepted, duplicate, expired,
  rejected) is final and acked, so the sender stops; only transient errors are retried.
* **Loops:** answered `duplicate` if this node is already in `route` or is the origin;
  refused when `hop_count + 1` exceeds `min(hop_limit, local mesh.hop_limit)`.
* **Integrity / trust:** `payload_sha256` must match. Trust is hop-by-hop: the SSH link
  authenticates the peer with its pinned node key and only listed peers are accepted.
  (Origin signatures are a possible later addition; `v` would change.)
* **Queue:** the `forward_queue` table holds one row per (mid, peer) with the envelope,
  ordered by priority then age. Failed sends back off exponentially with jitter. States:
  QUEUED → ACKED, or FAILED (20 failures) / EXPIRED (30 days for bulletins, 14 for mail).
* **Routing:** mail and receipts go straight to the destination when it is a direct peer,
  otherwise they flood like bulletins and only the destination delivers them.
* **Conflicts:** messages are immutable once created. A moderation state (hidden or
  deleted) at the origin travels as a separate signed control message, and each node
  applies its own local moderation on top.
* **Resume:** acks are recorded per envelope as they arrive, so an interrupted sync
  resends only what wasn't acked (and dedup absorbs any overlap).
