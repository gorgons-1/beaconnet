# Changelog

Releases are git tags (`vX.Y.Z`). Nodes update with `sudo scripts/update.sh`.

## 1.9.5 - a bell for new mail

* BeaconNet Desktop 1.4.5: a bell rings when new private mail arrives (once per check,
  however many came in; not for mail that was already waiting when the app started). It
  plays even with the window closed. **Settings > Sound** turns it on or off and has a
  **Test the bell** button.

## 1.9.4 - Update now

* BeaconNet Desktop 1.4.4: the update banner has **Update now**. The app downloads the new
  version, checks it against `BeaconNet-SHA256SUMS.txt` (new on each release), replaces
  itself, and reopens; the page reloads by itself. If anything goes wrong, the current app
  is left as it was and the banner explains why, with **Download update** as before. Apps
  run from the Python source, or from a read-only place (a macOS app still in Downloads),
  keep the Download button.

## 1.9.3 - mail stays in the conversation

* BeaconNet Desktop 1.4.3:
  * After you send, you're taken back to that conversation (with your new message at
    the bottom) instead of a raw list of sent mail.
  * *sent* and *archive* are grouped into conversations too, so a reply chain is one
    line with a message count and the subject without its "Re: Re: Re:".

## 1.9.2 - address book for big nodes

* The address book is searched by the node and sent a page at a time, so it works with
  thousands of members. `GET /v1/directory` takes `q` (name, username or verified
  callsign), `offset` and `limit`, and returns `total` and `more`.
* BeaconNet Desktop 1.4.2: **Address book** opens a pop-up window: search box, count,
  scrolling list, *Show more*, Esc to close. Typing in *To* shows the top matches.
* BBS: *Private Mail > M* (and `?` at the *To* prompt) asks what to search for, then
  lists the matches 20 at a time.

## 1.9.1 - address book and version in the header

* BeaconNet Desktop 1.4.1:
  * The header shows the app version, and "vX.Y.Z available" when there's an update.
  * **Address book** button next to *To* on a new message (the whole member list, one
    click to pick); the Mail tab's Members button is now Address book. If the list can't
    load, it says why instead of showing nothing. The browser no longer autofills *To*.

## 1.9.0 - conversations and app updates

* BeaconNet Desktop 1.4.0:
  * **Mail is grouped into conversations**: one line per person and subject, including
    your own replies, opening to the whole exchange in order.
  * **Update check**: twice a day it reads the latest release's `BeaconNet-version.txt`
    and, when a newer app is ready, shows a banner (Download update / What's new / Not
    now) and a desktop notification. Settings > App updates: Check now, or turn it off.
* Subjects no longer pile up as "Re: Re: Re:". The node keeps a single "Re:" on mail and
  bulletins, whichever client sent them.
* Releases attach `BeaconNet-version.txt` after all three downloads, so the app only
  announces an update that can be downloaded.

## 1.8.2 - desktop picks up node renames

* BeaconNet Desktop 1.3.1 re-reads the node's name and id every few minutes, so a
  renamed node shows its new name in the app header without restarting the app.

## 1.8.1 - name your node

* The installer explains how to name a node, with a pattern (`REGION-PLACE-NN`) and
  examples (`SCOT-OBAN-01`, `WALES-CONWY-01`, `US-TX-AUSTIN-01`, or a callsign). It
  also asks for a friendly node name and a location (new `--node-location` option). It
  asks again on later runs while the node still has the example id `BEACON-HUB-01`, and
  won't accept that id.
* The repository is now `gorgons-1/beaconnet`; links are updated (old links redirect).

## 1.8.0 - 40-column screens (Commodore 64)

* The terminal BBS fits 40-column screens. It switches automatically when the SSH client
  reports a screen narrower than 80 columns, or a terminal type such as `c64`, `c128`,
  `commodore` or `petscii` (which also turns off ANSI codes). **W** at the main menu
  switches between 40 and 80 columns by hand.
* At 40 columns, all text is word-wrapped one column short of the edge, so a C64 doesn't
  double-space it. Characters outside ASCII are folded (accents dropped, smart quotes
  straightened), and the bulletin, mail and member lists and the help screen have
  compact layouts.
* Wording: BeaconNet is no longer described as an "emergency" network ("Packet Radio
  BBS Network"). The EMERGENCY board and priority, and the notice to call your local
  emergency number, are unchanged.

## 1.7.0 - member list for mail

* Approved users can see who they can send private mail to on the node: username,
  display name and (once a sysop has verified it) callsign. Contact details are never
  listed, and pending, suspended or rejected accounts don't appear.
  * BeaconNet Desktop 1.3.0: **Mail > Members** (search, click someone to write to
    them), and the *To* box on a new message suggests members as you type.
  * BBS: *Private Mail > M* lists members; type `?` at the *To* prompt to see them.
  * API: `GET /v1/directory` (client op `directory`).

## 1.6.0 - radio settings (Phase 3 pre-stage)

* Packet radio settings can be filled in ahead of Phase 3. Nothing transmits yet.
  * Node: a `radio:` section in `config/bbs.yaml` (station callsign, TNC: Dire Wolf
    KISS-over-TCP or a serial TNC, beacon, max radio users). It's validated, and
    `enabled: true` is refused until the radio service exists.
  * BeaconNet Desktop 1.2.0: Settings > Connection > **Packet radio** (your callsign,
    BBS callsign, digipeaters, TNC, check interval), validated and saved. It says
    plainly that Radio mode connects from Phase 3.

## 1.5.0 - edit your details

* Users change their own display name, callsign and contact details: in BeaconNet
  Desktop (new **My account** tab) or in the BBS (*My Account & Keys > E*). A changed
  callsign is unverified until a sysop checks it; the registration rules against
  impersonation still apply. Pending users can edit their details too.
* BeaconNet Desktop 1.1.0: **My account** tab. Change your details or password, see the
  devices that log in by key (this computer is marked), and remove one.

## 1.4.0 - BeaconNet Desktop

* **BeaconNet Desktop** (`clients/desktop`): a double-click app for Windows (.exe), macOS
  (.app) and Linux, attached to each GitHub release; it lives in the tray / menu bar.
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
