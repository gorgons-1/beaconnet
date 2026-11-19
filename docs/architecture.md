# Architecture

## Services

```text
                 LAN / internet
                       │ ssh :2222 (BBS_SSH_BIND)
                 ┌─────▼─────┐   network "edge" (published port only)
                 │  bbs-ssh  │   public terminal: guest, registration, users
                 └─────┬─────┘
       core-public     │ HTTP + X-Service-Token(ssh) + Bearer session
       (internal)      │
                 ┌─────▼─────┐         ┌──────────────┐
                 │ bbs-core  │─────────│ SQLite (WAL) │  volume bbs-db, NVMe
                 └─────▲─────┘         └──────────────┘
       core-admin      │ HTTP + X-Service-Token(admin) + Bearer session
       (internal)      │
                 ┌─────┴─────┐   network "mgmt"
                 │ bbs-admin │   admin console
                 └─────▲─────┘
                       │ ssh :2223 bound to BBS_ADMIN_BIND (default 127.0.0.1)
                 Pi itself / SSH tunnel / management interface

       core-sync (internal) - bbs-core <-> bbs-sync, X-Service-Token(sync), /v1/sync/* only
                 ┌───────────┐   network "sync-edge"
                 │ bbs-sync  │◄──── ssh :2224 ────► peer nodes (pinned ed25519 node keys)
                 └───────────┘
```

* **bbs-sync** links this node with its peers ([mesh.md](mesh.md)). It holds no data; it
  moves envelopes between bbs-core's forward queue and the peers. bbs-core validates,
  deduplicates and queues every envelope (`bbs_core/mesh.py`); the sync token can't
  create sessions or reach any non-sync route.

* **bbs-core** is the only service that opens the database. It makes every permission
  decision and sits only on internal networks, so it has no route out and no published
  port. The versioned API (`/v1/...`) is in `shared/schemas/openapi.json`.
* **bbs-ssh** and **bbs-admin** are terminal front-ends. They hold no data and decide
  nothing. They pass on who connected: the public key the SSH client proved it holds
  during the handshake, plus the client IP. bbs-core works out the identity from that.
* The two gateways share no network, and each holds only its own API token. A
  compromised public gateway can't reach the admin network or call the admin API: its
  token is refused there, and its sessions are `service=ssh`.

## Identity and sessions

1. During the SSH handshake, the gateway asks core (`/v1/auth/ssh-key/check`) whether a
   (username, public key) pair may log in. asyncssh then checks that the client signed
   with that key's private half.
2. After authentication, the gateway asks for a session (`/v1/sessions/ssh-key` or
   `/v1/sessions/guest`). Core looks the key up again and issues a random bearer token.
   Only its SHA-256 is stored, in the `sessions` table.
3. On every request, core resolves the token and re-reads the account's status and role
   from the database. That means suspensions, rejections and key revocations take effect
   on the user's very next keypress, without a reconnect.
4. Usernames `guest`/`bbs` (guest) need no credentials. `new`/`register` accepts any key
   the client proves it holds (so registration can offer to use it), or lets a client
   without a key straight in to register with a password. Other logins use a registered
   key, or the account's password (`/v1/sessions/password`) on the public gateway only.
5. A public-gateway session of an ACTIVE admin from `sysop.allowed_networks` counts as
   admin (`Principal.is_admin`), which opens the `/v1/admin/*` routes to the Sysop menu.

Account states: `PENDING` → `ACTIVE` (approved) or `REJECTED`. `ACTIVE` ↔ `SUSPENDED`.
Pending and suspended accounts can read what guests can read. Only `ACTIVE` accounts
can post or use mail.

## Data

The SQLite database uses WAL mode with `synchronous=FULL`, foreign keys and explicit
`BEGIN IMMEDIATE` transactions. Each migration is applied in a single transaction and
recorded with a checksum. If a migration file changes after it has been applied, start-up
stops.

* Each bulletin has a global `mid` (`<uuid>@<NODE-ID>`), which never changes when the
  bulletin is forwarded. It also has a local number (`id`, AUTOINCREMENT, never reused).
  Receiving a `mid` that this node has already seen is a no-op (`sync_seen`), which is
  how the mesh deduplicates.
* Text is cleaned when it comes in. Every control character except newline is removed,
  including ESC and CSI, and so are Unicode bidi/format characters. The gateways clean
  again on output. Nothing a user types can drive another user's terminal.
* Searches use an FTS5 index on subject and body. User input is reduced to quoted words,
  so FTS syntax can't be injected. All SQL uses parameters.
* Emergency board messages carry a priority (ROUTINE/PRIORITY/EMERGENCY), an area and an
  expiry time. Listings show live traffic first, highest priority first. Priority is
  ignored on other boards. Posting a duplicate (same board, subject and body within
  `duplicate_window_hours`) is refused.

## Decisions taken where the brief left a choice

| Topic | Decision |
|---|---|
| Admin access | A Sysop menu in the public BBS for admins connecting from the LAN, plus the separate key-only console bound to 127.0.0.1. |
| Login | The device's SSH key, picked up at registration with the user's consent, or a password for devices without one. |
| Pending users | They can log in with their key and read. Their status is shown on screen. |
| Suspended users | Read-only access with a notice. Their existing sessions end when the suspension is applied. |
| Admin reading private mail | Off (`privacy.admin_can_read_private_mail: false`), and no API exists for it. |
| Remote mail (`user@NODE`) | Queued and relayed through the mesh; the destination sends back a delivery receipt (DELIVERED/FAILED). |
| Mesh trust | Hop-by-hop: peers are listed by hand with pinned node keys (peer cards). No auto-discovery. |
| Internal API transport | Plain HTTP on internal-only Docker networks, authenticated per gateway and per session. There is no TLS inside the host. |
| Rate limits | Held in memory in core, so they reset when core restarts. |
| Board seeding | `boards.yaml` only creates boards that are missing. After that, the admin console manages boards. |
| SSH port | 2222 inside and out. The Pi's own sshd keeps port 22. |
