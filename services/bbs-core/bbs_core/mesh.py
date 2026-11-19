"""Node-to-node mesh: building, queueing and accepting message envelopes.

bbs-core owns the rules and the database; bbs-sync only carries envelopes between this
node's queue and its peers (see shared/protocols/message-envelope.md).

Trust is hop-by-hop. A peer proves who it is with its node key on the SSH link, and
this node only takes envelopes from peers listed in peers.yaml. Everything an envelope
says is still checked here as untrusted input: shape, sizes, hash, loops, hop limit and
expiry.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import sqlite3
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Iterable

from .config import BOARD_NAME_PATTERN, NODE_ID_PATTERN
from .db import parse_ts, ts, utcnow
from .errors import BBSError
from .logs import event
from .security import sanitize_body, sanitize_line, validate_callsign

if TYPE_CHECKING:
    from .service import BBS

log = logging.getLogger("bbs-core")

ENVELOPE_VERSION = 1
KINDS = ("bulletin", "private", "receipt")
PRIORITIES = ("ROUTINE", "PRIORITY", "EMERGENCY")
NODE_RE = re.compile(NODE_ID_PATTERN)
BOARD_RE = re.compile(BOARD_NAME_PATTERN)
MID_RE = re.compile(r"^[0-9a-f]{32}@([A-Z0-9][A-Z0-9-]{2,31})$")
USER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,19}$")
MAX_ROUTE = 32
MAX_ENVELOPE_BYTES = 96 * 1024
QUEUE_MAX_AGE = timedelta(days=30)       # a bulletin waits at most this long for a peer
MAIL_MAX_AGE = timedelta(days=14)        # undelivered remote mail fails after this
MAX_ATTEMPTS = 20                        # per-envelope send failures before FAILED


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def payload_hash(payload: dict) -> str:
    return hashlib.sha256(canonical(payload)).hexdigest()


class Reject(Exception):
    """The envelope is not acceptable. It is acknowledged, so the sender won't retry it."""


class Mesh:
    def __init__(self, bbs: "BBS") -> None:
        self.bbs = bbs
        self.node = bbs.cfg.node.id
        self.mc = bbs.cfg.mesh

    @property
    def peers(self) -> dict:
        return {p.id: p for p in self.bbs.s.peers if p.enabled}

    # --- outbound ------------------------------------------------------------------

    def envelope(self, kind: str, mid: str, destination: dict, payload: dict, created_at: str,
                 priority: str = "ROUTINE", expires_at: str | None = None) -> dict:
        return {
            "v": ENVELOPE_VERSION, "kind": kind, "mid": mid, "origin_node": self.node,
            "destination": destination, "created_at": created_at, "expires_at": expires_at,
            "priority": priority, "hop_count": 0, "hop_limit": self.mc.hop_limit, "route": [self.node],
            "payload": payload, "payload_sha256": payload_hash(payload),
        }

    def targets(self, env: dict, exclude: Iterable[str] = ()) -> list[str]:
        """Peers to send an envelope to. Mail and receipts go straight to the destination
        node when it is a direct peer; otherwise everything floods to every other peer."""
        skip = set(exclude) | set(env.get("route", []))
        peers = [p for p in self.peers if p not in skip]
        if env["kind"] in ("private", "receipt"):
            dest = env["destination"].get("node")
            if dest in peers:
                return [dest]
        return peers

    def enqueue(self, conn: sqlite3.Connection, env: dict, peers: Iterable[str]) -> int:
        now = utcnow()
        limit = now + (MAIL_MAX_AGE if env["kind"] != "bulletin" else QUEUE_MAX_AGE)
        if env.get("expires_at"):
            limit = min(limit, parse_ts(env["expires_at"]))
        body = json.dumps(env, separators=(",", ":"), ensure_ascii=False)
        n = 0
        for peer in peers:
            n += conn.execute(
                "INSERT OR IGNORE INTO forward_queue(mid, kind, peer_node, priority, envelope, expires_at,"
                " next_attempt_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (env["mid"], env["kind"], peer, env["priority"], body, ts(limit), ts(now), ts(now), ts(now)),
            ).rowcount
        return n

    def send_new(self, conn: sqlite3.Connection, env: dict) -> int:
        """Queue an envelope this node created. Its id goes into sync_seen so the copy that
        comes back round a loop is recognised."""
        conn.execute("INSERT OR IGNORE INTO sync_seen(mid, kind, from_node, seen_at) VALUES (?,?,?,?)",
                     (env["mid"], env["kind"], self.node, ts()))
        return self.enqueue(conn, env, self.targets(env))

    # --- inbound -------------------------------------------------------------------

    def validate(self, env: Any) -> dict:
        """Check an envelope's shape and limits. Raises Reject with a short reason."""
        if not isinstance(env, dict):
            raise Reject("not an object")
        if len(canonical(env)) > MAX_ENVELOPE_BYTES:
            raise Reject("too large")
        if env.get("v") != ENVELOPE_VERSION:
            raise Reject("unsupported envelope version")
        kind = env.get("kind")
        if kind not in KINDS:
            raise Reject("unknown kind")
        mid, origin = env.get("mid"), env.get("origin_node")
        m = MID_RE.match(mid) if isinstance(mid, str) else None
        if not m or m.group(1) != origin:
            raise Reject("bad message id")
        route = env.get("route")
        if (not isinstance(route, list) or not 1 <= len(route) <= MAX_ROUTE or route[0] != origin
                or not all(isinstance(r, str) and NODE_RE.match(r) for r in route)):
            raise Reject("bad route")
        hops, hop_limit = env.get("hop_count"), env.get("hop_limit")
        if type(hops) is not int or type(hop_limit) is not int or not 0 <= hops < MAX_ROUTE or not 1 <= hop_limit <= 16:
            raise Reject("bad hop count")
        if env.get("priority") not in PRIORITIES:
            raise Reject("bad priority")
        try:
            created = parse_ts(env["created_at"])
            expires = parse_ts(env["expires_at"]) if env.get("expires_at") is not None else None
        except (KeyError, TypeError, ValueError):
            raise Reject("bad timestamp") from None
        if created > utcnow() + timedelta(hours=self.mc.max_clock_skew_hours):
            raise Reject("dated in the future")
        dest, payload = env.get("destination"), env.get("payload")
        if not isinstance(dest, dict) or not isinstance(payload, dict):
            raise Reject("bad destination or payload")
        if env.get("payload_sha256") != payload_hash(payload):
            raise Reject("payload hash mismatch")

        def text(key: str, required: bool = True, maxlen: int = 65536) -> str | None:
            v = payload.get(key)
            if v is None and not required:
                return None
            if not isinstance(v, str) or (required and not v.strip()) or len(v) > maxlen:
                raise Reject(f"bad {key}")
            return v

        if kind == "bulletin":
            if not isinstance(dest.get("board"), str) or not BOARD_RE.match(dest["board"]):
                raise Reject("bad board")
            text("author", maxlen=40), text("subject", maxlen=400), text("body")
            text("callsign", False, 16), text("area", False, 80)
            if payload.get("parent_mid") is not None and not MID_RE.match(str(payload["parent_mid"])):
                raise Reject("bad parent_mid")
        elif kind == "private":
            if not NODE_RE.match(str(dest.get("node", ""))) or not USER_RE.match(str(dest.get("user", ""))):
                raise Reject("bad mail address")
            text("from", maxlen=40), text("subject", maxlen=400), text("body"), text("callsign", False, 16)
        else:
            if not NODE_RE.match(str(dest.get("node", ""))) or not MID_RE.match(str(payload.get("ref_mid", ""))):
                raise Reject("bad receipt")
            if payload.get("status") not in ("DELIVERED", "FAILED"):
                raise Reject("bad receipt status")
            text("reason", False, 200)
        env = dict(env)
        env["_expired"] = expires is not None and expires <= utcnow()
        return env

    def receive(self, from_peer: str, raw: Any) -> dict:
        """Accept one envelope from an authenticated peer. Returns {"status": ...}:
        accepted | duplicate | expired | rejected (with reason). All of them are final,
        so the sender stops resending; an exception means 'try again later'."""
        if from_peer not in self.peers:
            raise BBSError("forbidden", "not an enabled peer of this node")
        self.bbs.limits.check("sync_in", from_peer, self.mc.max_envelopes_per_peer_per_hour, 3600)
        try:
            env = self.validate(raw)
        except Reject as r:
            event(log, "envelope_rejected", logging.WARNING, peer=from_peer, reason=str(r),
                  mid=str(raw.get("mid"))[:80] if isinstance(raw, dict) else None)
            return {"status": "rejected", "reason": str(r)}
        mid, kind = env["mid"], env["kind"]
        if env["origin_node"] == self.node or self.node in env["route"]:
            return {"status": "duplicate"}          # our own message, back round a loop
        if env["_expired"]:
            return {"status": "expired"}
        hop = env["hop_count"] + 1
        limit = min(env["hop_limit"], self.mc.hop_limit)
        if hop > limit:
            return {"status": "rejected", "reason": "hop limit reached"}
        del env["_expired"]
        fwd = dict(env, hop_count=hop, route=env["route"] + [self.node])
        onward = hop < limit
        with self.bbs.tx() as conn:
            if conn.execute("SELECT 1 FROM sync_seen WHERE mid = ?", (mid,)).fetchone():
                return {"status": "duplicate"}
            conn.execute("INSERT INTO sync_seen(mid, kind, from_node, seen_at) VALUES (?,?,?,?)",
                         (mid, kind, from_peer, ts()))
            if kind == "bulletin":
                result = self._bulletin(conn, fwd)
                onward = onward and result["status"] == "accepted"
            elif env["destination"]["node"] == self.node:
                result = self._mail(conn, fwd) if kind == "private" else self._receipt(conn, fwd)
                onward = False
            elif kind == "private" and not self.mc.forward_private_mail:
                result, onward = {"status": "rejected", "reason": "this node does not relay mail"}, False
            else:
                result = {"status": "accepted", "relayed": True}
            if onward:
                result["forwarded_to"] = self.enqueue(conn, fwd, self.targets(fwd, exclude=[from_peer]))
        event(log, "envelope_received", peer=from_peer, kind=kind, mid=mid, origin=env["origin_node"],
              hops=hop, status=result["status"])
        return result

    def _bulletin(self, conn: sqlite3.Connection, env: dict) -> dict:
        p = env["payload"]
        b = conn.execute("SELECT * FROM boards WHERE name = ?", (env["destination"]["board"],)).fetchone()
        if b is None or b["archived"]:
            return {"status": "rejected", "reason": "no such board here"}
        if not b["forward"]:
            return {"status": "rejected", "reason": "board is not shared on this node"}
        if conn.execute("SELECT 1 FROM messages WHERE mid = ?", (env["mid"],)).fetchone():
            return {"status": "duplicate"}
        mc = self.bbs.cfg.messages
        subject = sanitize_line(p["subject"], mc.subject_max) or "(no subject)"
        body = sanitize_body(p["body"], mc.body_max_bytes) or "(empty)"
        priority = env["priority"] if b["is_emergency"] else "ROUTINE"
        area = sanitize_line(p.get("area") or "", 40) or ("unspecified" if b["is_emergency"] else None)
        author = f"{sanitize_line(p['author'], 20)}@{env['origin_node']}"
        try:
            callsign = validate_callsign(p.get("callsign"))
        except BBSError:
            callsign = None
        from .service import content_hash
        now = ts()
        cur = conn.execute(
            "INSERT INTO messages(mid, origin_node, board_id, author_name, author_callsign, subject, body, parent_mid,"
            " priority, area, expires_at, content_hash, hop_count, route, created_at, received_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (env["mid"], env["origin_node"], b["id"], author, callsign, subject, body, p.get("parent_mid"),
             priority, area, env.get("expires_at"), content_hash(b["name"], subject, body), env["hop_count"],
             json.dumps(env["route"]), env["created_at"], now, now),
        )
        return {"status": "accepted", "number": cur.lastrowid}

    def _mail(self, conn: sqlite3.Connection, env: dict) -> dict:
        p, user = env["payload"], env["destination"]["user"]
        rcpt = conn.execute(
            "SELECT id, username FROM users WHERE status = 'ACTIVE' AND (username = ? OR (callsign = ? AND callsign_verified = 1))",
            (user.lower(), user.upper()),
        ).fetchone()
        if rcpt is None:
            status, reason = "FAILED", f"no active user called {user} on {self.node}"
        else:
            mc = self.bbs.cfg.messages
            now = ts()
            conn.execute(
                "INSERT INTO private_messages(mid, origin_node, sender_id, sender_name, recipient_id, recipient_address,"
                " subject, body, delivery_state, created_at, delivered_at) VALUES (?,?,NULL,?,?,?,?,?, 'DELIVERED', ?, ?)",
                (env["mid"], env["origin_node"], f"{sanitize_line(p['from'], 20)}@{env['origin_node']}", rcpt["id"],
                 rcpt["username"], sanitize_line(p["subject"], mc.subject_max) or "(no subject)",
                 sanitize_body(p["body"], mc.body_max_bytes) or "(empty)", env["created_at"], now),
            )
            status, reason = "DELIVERED", None
        self._send_receipt(conn, env, status, reason)
        return {"status": "accepted", "delivery": status}

    def _send_receipt(self, conn: sqlite3.Connection, env: dict, status: str, reason: str | None) -> None:
        from .service import new_mid
        payload = {"ref_mid": env["mid"], "status": status, "reason": reason}
        receipt = self.envelope("receipt", new_mid(self.node), {"node": env["origin_node"]}, payload, ts())
        self.send_new(conn, receipt)

    def _receipt(self, conn: sqlite3.Connection, env: dict) -> dict:
        p = env["payload"]
        conn.execute(
            "UPDATE private_messages SET delivery_state = ?, delivered_at = ? WHERE mid = ? AND origin_node = ?"
            " AND delivery_state = 'QUEUED'",
            (p["status"], ts() if p["status"] == "DELIVERED" else None, p["ref_mid"], self.node),
        )
        return {"status": "accepted"}

    # --- the sync service's view of the queue ------------------------------------------

    def sync_config(self) -> dict:
        return {"node_id": self.node, "peers": [
            {"id": p.id, "name": p.name, "address": p.address, "port": p.port, "public_key": p.public_key}
            for p in self.peers.values()]}

    def outbox(self, peer: str, limit: int) -> dict:
        if peer not in self.peers:
            raise BBSError("not_found", "not an enabled peer")
        now = ts()
        with self.bbs.tx() as conn:
            conn.execute(
                "UPDATE forward_queue SET state = 'EXPIRED', updated_at = ? WHERE peer_node = ? AND state = 'QUEUED'"
                " AND expires_at IS NOT NULL AND expires_at <= ?", (now, peer, now))
            rows = conn.execute(
                "SELECT id, envelope FROM forward_queue WHERE peer_node = ? AND state = 'QUEUED' AND next_attempt_at <= ?"
                " ORDER BY CASE priority WHEN 'EMERGENCY' THEN 3 WHEN 'PRIORITY' THEN 2 ELSE 1 END DESC, id LIMIT ?",
                (peer, now, limit)).fetchall()
            waiting = conn.execute("SELECT COUNT(*) FROM forward_queue WHERE peer_node = ? AND state = 'QUEUED'",
                                   (peer,)).fetchone()[0]
        return {"items": [{"qid": r["id"], "envelope": json.loads(r["envelope"])} for r in rows], "waiting": waiting}

    def results(self, peer: str, acked: list[int], failed: list[dict]) -> dict:
        now = utcnow()
        with self.bbs.tx() as conn:
            for qid in acked:
                conn.execute("UPDATE forward_queue SET state = 'ACKED', updated_at = ?, last_error = NULL"
                             " WHERE id = ? AND peer_node = ? AND state = 'QUEUED'", (ts(now), qid, peer))
            for f in failed:
                row = conn.execute("SELECT attempts FROM forward_queue WHERE id = ? AND peer_node = ? AND state = 'QUEUED'",
                                   (f["qid"], peer)).fetchone()
                if row is None:
                    continue
                attempts = row["attempts"] + 1
                delay = min(3600, 30 * 2 ** min(attempts, 7)) * random.uniform(0.8, 1.2)
                conn.execute(
                    "UPDATE forward_queue SET attempts = ?, next_attempt_at = ?, last_error = ?, updated_at = ?,"
                    " state = CASE WHEN ? >= ? THEN 'FAILED' ELSE state END WHERE id = ?",
                    (attempts, ts(now + timedelta(seconds=delay)), sanitize_line(str(f.get("error") or ""), 200),
                     ts(now), attempts, MAX_ATTEMPTS, f["qid"]))
        return {"ok": True}

    def peer_status(self, peer: str, state: str, error: str | None) -> dict:
        now = ts()
        with self.bbs.tx() as conn:
            if state == "CONNECTED":
                conn.execute("UPDATE nodes SET status = ?, last_seen_at = ?, last_sync_at = ?, last_error = NULL"
                             " WHERE node_id = ? AND is_local = 0", (state, now, now, peer))
            else:
                conn.execute("UPDATE nodes SET status = ?, last_error = ? WHERE node_id = ? AND is_local = 0",
                             (state, sanitize_line(error or "", 200) or None, peer))
        return {"ok": True}
