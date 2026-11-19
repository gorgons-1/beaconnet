# Packet radio integration (Phase 3 plan)

This document is a plan; nothing in this release transmits.

## Settings already in place (pre-stage)

The settings for Phase 3 exist now, so nodes and users can fill them in ahead of time.
They are checked when saved, and nothing transmits.

* **Node:** the `radio:` section of `config/bbs.yaml` holds the BBS's own station
  callsign (e.g. `GM4ABC-10`), the TNC (`kiss-tcp` to Dire Wolf on port 8001, or
  `kiss-serial` with a device and speed), the beacon interval and the maximum number of
  radio users. `enabled: true` is refused until the `bbs-radio` service exists.
* **BeaconNet Desktop:** Settings > Connection > **Packet radio** holds your callsign,
  the BBS callsign, up to two digipeaters, the TNC (Dire Wolf over TCP, or a serial TNC)
  and how often to check for new messages (10 minutes to 4 hours). There's no password
  and no frequency: the BBS knows you by your verified callsign, and the frequency is
  set on the radio. Until Phase 3, Radio mode says it can't connect yet.

## Shape

A new `bbs-radio` container runs **Dire Wolf** (a software TNC, with a USB sound card
and PTT interface) or talks KISS to a hardware TNC. It accepts AX.25 connected-mode
sessions and turns each one into a BBS session, using the same internal API and the same
rules as `bbs-ssh`. Radio users never get a shell, and the admin console is never
offered over RF.

The container needs the sound or serial device, and nothing else (`devices:` for that one
device only, no privileged mode). It joins a new internal network `core-radio` with its
own service token. Core will treat `service=radio` sessions like `ssh` sessions with
stricter content rules (below).

## Identity over radio

You can't prove who someone is on air with a password or key, and you must not send one.
A radio session is tied to the **verified callsign** of an ACTIVE account. On RF, that
gives the user reduced rights:

* read bulletins
* post to boards an admin has marked RF-postable
* read only the private mail that the user chose to make RF-readable

Anything that needs real authentication stays on SSH.

## Rules before anything transmits

Document and check each of these against your country's amateur licence conditions and guidance (for example Ofcom in the UK)
before enabling RF:

* station identification and callsign use (the node's callsign in every frame or beacon)
* permitted frequencies, modes, bandwidth and power for the band in use
* unattended and remote operation conditions, and the operator's responsibilities
* third-party traffic restrictions
* the prohibition on encryption and on messages obscured to hide their meaning
* content limits: nothing commercial, nothing offensive

## Content policy (core, configurable)

* Each board has a `forward_rf` flag, off by default. Private mail never goes over RF
  unless the sender ticks "may be sent by radio" and the policy allows it.
* Emergency priority sets queue order. It never overrides licence conditions or
  transmit limits.
* Transmit limits: frames per minute, maximum message size and quiet hours.
* There is no auto-forward of content the RF policy hasn't cleared.

## Terminal

The radio terminal is a low-bandwidth, short-command interface in the classic style:
`L` list, `R n` read, `S board` send, `K n` kill, `B` bye. It drops boxes and prompts
down to one line, and retries on link failure. The `R <n>` / `L` shortcuts already in
the SSH menu are the start of this.
