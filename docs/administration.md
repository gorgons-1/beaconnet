# Administration

## Reaching the console

By default the admin console listens only on the Pi's loopback address, so you have to
be able to log in to the Pi first. Any of these works:

```sh
ssh -J <you>@<pi> -p 2223 <admin>@127.0.0.1        # from your PC, in one step
ssh -L 2223:127.0.0.1:2223 <you>@<pi>              # or a tunnel, then: ssh -p 2223 <admin>@127.0.0.1
ssh -p 2223 <admin>@127.0.0.1                      # on the Pi itself (needs the admin key there)
```

Your admin key must belong to an account with role `admin` and status `ACTIVE`.

```text
SCOTLAND BBS - ADMINISTRATION

1. Pending Registrations     approve / reject (and mark the callsign verified)
2. Active Users              details, suspend, revoke keys, verify callsigns
3. Suspended Users           reactivate
4. Bulletin Moderation       hide / unhide / delete; B = create, rename, archive boards
5. Message Queues            live emergency traffic; forward queue per peer, failures
6. Connected Nodes           peers: status, address, queued messages, last sync, last error
7. System Health             counts, sessions, database, disk, memory, uptime, version/updates
8. Audit Log                 every admin action and refusal, newest first
9. Backup Status             last run, last success, recent files
10. Configuration            effective settings (no secrets)
```

## Approving people

Check the callsign before you approve: look it up in Ofcom's or the RSGB's records, or
ask the person on air. When you approve an account that claims a callsign, the console
asks whether you have verified it. Only verified callsigns can be used as mail addresses,
and unverified ones show with a `?` in lists. A callsign can belong to only one live
account. A username can't equal anyone else's callsign or username, and a username that
looks like a callsign must be that user's own callsign.

## More administrators

Create one from the Pi with
`sudo ./scripts/bootstrap-admin.sh --username NAME --key-file KEY.pub`. There is
deliberately no way to promote a user from the console, so a hijacked admin session
can't create more admins.

## Keys

Users add and revoke their own keys under *My Account & Keys*, which is how they rotate
keys. They can't revoke their last key. An admin can revoke any key (*K* in the user's
details), and any session using that key ends at once. To lock someone out completely,
suspend them.

## Audit

The audit log records each change with the admin's identity, a UTC timestamp, the target
and the outcome (`success` / `denied`). It also records logins, registrations, emergency
posts and refused admin actions. Entries are kept for `retention.audit_days` (730 days
by default).

## Logs

`docker compose logs bbs-core` shows JSON lines. Logs never contain message bodies,
private mail, contact details, keys or tokens; they hold metadata only.
