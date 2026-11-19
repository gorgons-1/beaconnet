# Administration

## The easy way: Sysop menu in the BBS

Log in to the normal BBS from your home network (`ssh -p 2222 <admin>@<pi>`, with your
key or password). Administrator accounts connecting from a private LAN address
(10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16) get an extra **S. Sysop Menu**, which is the
whole admin console below. From the internet the same account sees only the user menu.

Change the networks in `config/bbs.yaml`, then `docker compose restart bbs-core`:

```yaml
sysop:
  menu_in_bbs: true            # false = admin only through the separate console
  allowed_networks: [192.168.0.0/16]
```

Don't port-forward 2223 on your router. You don't need it for the Sysop menu.

## The separate console

The admin console on port 2223 still works and is key-only. By default it listens only
on the Pi's loopback address, so you have to be able to log in to the Pi first. Any of
these works:

```sh
ssh -J <you>@<pi> -p 2223 <admin>@127.0.0.1        # from your PC, in one step
ssh -L 2223:127.0.0.1:2223 <you>@<pi>              # or a tunnel, then: ssh -p 2223 <admin>@127.0.0.1
ssh -p 2223 <admin>@127.0.0.1                      # on the Pi itself (needs the admin key there)
```

Your admin key must belong to an account with role `admin` and status `ACTIVE`.

```text
BEACONNET - ADMINISTRATION

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

Check the callsign before you approve: look it up in your national licensing authority's records (Ofcom in the UK, the FCC in the US, ...), or
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

## Users' own details

Users can change their display name, callsign and contact details themselves (BBS: *My
Account & Keys > E*; BeaconNet Desktop: *My account*). A new callsign goes back to
**unverified**, so check it again in the user's details (*V*). Every change is in the
audit log as `profile_updated`.

## Keys and passwords

When someone registers, the BBS offers to use the SSH key their device already has, so
that device logs them in automatically. A device without a key registers with a password
instead (8+ characters, stored as a scrypt hash). Users set or change a password, and
add or revoke keys, under *My Account & Keys*. They can't remove their last way in.
After `rate_limits.failed_logins_before_lock` wrong passwords in a row (10) the account
locks for `lock_minutes` (15). Set `registration.allow_passwords: false` to make a node
key-only. The admin console never accepts passwords.

To use the Sysop menu from a PC that doesn't have your admin key, log in once from a
machine that does (on the LAN), open *My Account & Keys* and set a password. An admin can revoke any key (*K* in the user's
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
