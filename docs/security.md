# Security

## Containers

Every service runs as uid 10001, never root. Each container also has:

* a read-only root filesystem, with a 16 MB `noexec` tmpfs on `/tmp`
* all Linux capabilities dropped and `no-new-privileges` set
* no privileged mode, host namespaces or devices, and no Docker socket
* CPU, memory and process-count limits, a health check and `restart: unless-stopped`

`bbs-core` is the only container that mounts the database volume. It has no published
ports and is attached only to `internal: true` networks.

The `tests/security/test_deployment.py` tests check all of the above, and also check:
the secrets each service receives, dependency pinning, that no service code runs a shell
(`subprocess`, `os.system`, `eval` ...), that there are no hard-coded secrets, and that
log calls never carry body, key or token fields.

## Secrets

`secrets/` holds the three gateway tokens, the SSH host keys and the node key. `install.sh` generates
them from `/dev/urandom` and `ssh-keygen`. Each file is mode 0400 and owned by the
container user. Compose mounts each one only into the services that need it, at
`/run/secrets/<name>`. They are excluded from images (`.dockerignore`) and from git
(`.gitignore`). `config/` and `.env` hold no secrets. Rotate with
`scripts/rotate-secrets.sh`.

## People

* No shell: shell, exec, sftp/scp, port forwarding, agent forwarding and X11 are all
  refused (tested).
* SSH keys, or a password for devices that have no key. Passwords are scrypt hashes;
  wrong passwords are rate-limited per IP and lock the account after 10 in a row. Each
  gateway limits connections per IP and in total. The admin console is key-only.
* At registration the key is taken from the SSH login itself, after the client proved
  it holds the private key. A key that was offered but not signed for is discarded.
* Admin from the public BBS (the Sysop menu) needs an ACTIVE admin account **and** a
  connection from `sysop.allowed_networks` (private LAN ranges by default), checked by
  bbs-core on every request. Connections that Docker's proxy rewrote to its own network
  address are never treated as LAN.
* Registrations are rate-limited per IP and across the whole node. No account is
  approved automatically.
* Impersonation: usernames and callsigns are unique and case-insensitive across both
  fields. Reserved names (`sysop`, `admin`, `root`, ...) are refused. A callsign stays a
  claim until a sysop verifies it.
* Private mail can be read only by its sender and recipient. Anyone else gets "not
  found", the same answer as for a missing message. Admins have no API to read it.
* Nothing a user writes can carry terminal escape sequences.

## Radio is public

Amateur radio transmissions are not confidential, and licence conditions in most countries restrict
encryption and content. When radio arrives (Phase 3), nothing secret goes over RF: no
keys, tokens, contact details or private mail without explicit policy and user consent.
See [radio-integration.md](radio-integration.md).

## Mesh links

* Port 2224 takes public-key logins only, and only from node keys listed in
  `config/peers.yaml`; the dialling side checks the listener's key the same way. No
  shell, exec, sftp or forwarding - just the sync subsystem (tested).
* Every envelope from a peer is untrusted input: size, shape, ids, payload hash, route,
  hop limit, dates and a per-peer rate limit are checked in bbs-core.
* Mail relayed between nodes is readable by each node on the way (not end-to-end
  encrypted). See [mesh.md](mesh.md).

## Updates

* `update.sh` installs only tagged releases from the repository the node was cloned from,
  and backs up first. Automatic installation is off unless `BBS_AUTO_UPDATE=true`.
* Anyone who can push tags to that repository can change what nodes run when they
  update - protect the repository (2FA, branch/tag protection) accordingly.

## Not covered yet

* The internal API is plain HTTP on internal Docker networks, so no TLS. It is
  authenticated, and those networks are not reachable from outside the host.
* Rate-limit counters live in memory and reset when bbs-core restarts.
* Host hardening is out of scope for the installer: OS updates, sshd settings and the
  firewall for anything other than the BBS port.
