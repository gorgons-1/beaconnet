"""Business rules. Every permission decision in the system is made here, in bbs-core."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterator

from .config import BOARD_NAME_PATTERN, Settings
from .db import Database, integrity_check, parse_ts, schema_version, transaction, ts, ts_in, utcnow
from .errors import BBSError
from .logs import event
from .mesh import USER_RE, Mesh
from .security import (
    RateLimiter,
    hash_password,
    hash_token,
    ip_in_networks,
    verify_password,
    validate_password,
    new_token,
    parse_public_key,
    sanitize_body,
    sanitize_line,
    looks_like_callsign,
    validate_callsign,
    validate_username,
)

log = logging.getLogger("bbs-core")

PRIORITY_RANK = {"EMERGENCY": 3, "PRIORITY": 2, "ROUTINE": 1}
STARTED = time.time()

EMERGENCY_DISCLAIMER = (
    "This BBS is run by volunteers. It is NOT an official emergency service and delivery "
    "is not guaranteed. In an emergency call your local emergency number (112, 911, 999...)."
)


class Denied(Exception):
    """Raised inside a transaction to refuse an action. The transaction rolls back, then
    the refusal is written to the audit log in its own transaction (see BBS.tx)."""

    def __init__(self, error: BBSError, who: "Principal | None", action: str, target_type: str | None = None,
                 target_id: Any = None, detail: dict | None = None, client_ip: str | None = None) -> None:
        super().__init__(error.message)
        self.error = error
        self.args_ = (who, action, "denied", target_type, target_id, detail, client_ip)


@dataclass(frozen=True)
class Principal:
    """Who is making a request, as established by bbs-core from the session token."""

    session_id: int
    kind: str  # guest | user | admin
    service: str  # ssh | admin
    user_id: int | None
    username: str | None
    callsign: str | None
    callsign_verified: bool
    role: str | None
    status: str | None  # PENDING | ACTIVE | SUSPENDED | REJECTED (None for guests)
    client_ip: str | None
    idle_timeout_s: int
    sysop_lan: bool = False  # public-gateway session from an allowed LAN network

    @property
    def is_active(self) -> bool:
        return self.user_id is not None and self.status == "ACTIVE"

    @property
    def is_admin(self) -> bool:
        if self.role != "admin" or self.status != "ACTIVE":
            return False
        if self.kind == "admin" and self.service == "admin":
            return True
        # An administrator on the public BBS, connecting from the LAN: Sysop menu.
        return self.service == "ssh" and self.kind == "user" and self.sysop_lan

    @property
    def label(self) -> str:
        return self.username or "guest"


def new_mid(node_id: str) -> str:
    return f"{uuid.uuid4().hex}@{node_id}"


def content_hash(board: str, subject: str, body: str) -> str:
    norm = lambda s: re.sub(r"\s+", " ", s).strip().lower()  # noqa: E731
    return hashlib.sha256(f"{board.upper()}\x00{norm(subject)}\x00{norm(body)}".encode()).hexdigest()


def fts_query(words: str, column: str | None = None) -> str | None:
    """Turn free text into a safe FTS5 query: each word quoted, prefix-matched, ANDed.
    Nothing the user types is passed through as FTS syntax."""
    tokens = re.findall(r"[A-Za-z0-9]+", words or "")[:8]
    if not tokens:
        return None
    col = f"{column} : " if column else ""
    return " AND ".join(f'{col}"{t}"*' for t in tokens)


class BBS:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.s = settings
        self.cfg = settings.bbs
        self.db = db
        self.limits = RateLimiter()
        self.mesh = Mesh(self)

    # --- start-up ------------------------------------------------------------------

    def seed(self) -> None:
        """Create boards from boards.yaml that don't exist yet, and register known nodes."""
        with self.db.session() as conn, transaction(conn):
            for order, b in enumerate(self.s.boards):
                exists = conn.execute("SELECT 1 FROM boards WHERE name = ?", (b.name,)).fetchone()
                if not exists:
                    conn.execute(
                        "INSERT INTO boards(name, description, post_policy, is_public, is_emergency, sort_order, created_at)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (b.name, b.description, b.post, int(b.public), int(b.emergency), (order + 1) * 10, ts()),
                    )
            node = self.cfg.node
            conn.execute(
                "INSERT INTO nodes(node_id, name, is_local, enabled, status) VALUES (?,?,1,1,'LOCAL')"
                " ON CONFLICT(node_id) DO UPDATE SET name = excluded.name, is_local = 1, status = 'LOCAL'",
                (node.id, node.name),
            )
            conn.execute("UPDATE nodes SET is_local = 0, status = 'UNKNOWN' WHERE is_local = 1 AND node_id != ?", (node.id,))
            for p in self.s.peers:
                conn.execute(
                    "INSERT INTO nodes(node_id, name, address, public_key, enabled) VALUES (?,?,?,?,?)"
                    " ON CONFLICT(node_id) DO UPDATE SET name = excluded.name, address = excluded.address,"
                    " public_key = excluded.public_key, enabled = excluded.enabled,"
                    " status = CASE WHEN nodes.status = 'REMOVED' THEN 'NEVER_CONNECTED' ELSE nodes.status END",
                    (p.id, p.name, f"{p.address}:{p.port}" if p.address else "(dials in)", p.public_key, int(p.enabled)),
                )
            known = [p.id for p in self.s.peers]
            marks = ",".join("?" * len(known)) or "''"
            conn.execute(f"UPDATE nodes SET enabled = 0, status = 'REMOVED' WHERE is_local = 0 AND node_id NOT IN ({marks})",
                         known)
            for b in self.s.boards:  # boards.yaml decides which seeded boards are shared with peers
                conn.execute("UPDATE boards SET forward = ? WHERE name = ?", (int(b.forward), b.name))

    # --- audit ---------------------------------------------------------------------

    def audit(
        self,
        conn: sqlite3.Connection,
        who: Principal | None,
        action: str,
        outcome: str,
        target_type: str | None = None,
        target_id: Any = None,
        detail: dict | None = None,
        client_ip: str | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO audit_log(ts, actor_id, actor_name, service, action, target_type, target_id, outcome, detail, client_ip)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                ts(),
                who.user_id if who else None,
                who.label if who else "system",
                who.service if who else "core",
                action,
                target_type,
                None if target_id is None else str(target_id),
                outcome,
                json.dumps(detail, default=str) if detail else None,
                client_ip or (who.client_ip if who else None),
            ),
        )

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """One write transaction. A Denied raised inside rolls back, is audited, and
        surfaces to the caller as its BBSError."""
        conn = self.db.connect()
        try:
            try:
                with transaction(conn):
                    yield conn
            except Denied as d:
                with transaction(conn):
                    self.audit(conn, *d.args_)
                raise d.error from None
        finally:
            conn.close()

    def audit_now(self, who: Principal | None, action: str, outcome: str, **kw: Any) -> None:
        with self.db.session() as conn, transaction(conn):
            self.audit(conn, who, action, outcome, **kw)

    # --- sessions ------------------------------------------------------------------

    def _idle_for(self, kind: str) -> int:
        s = self.cfg.sessions
        return {"guest": s.guest_idle_timeout_s, "user": s.user_idle_timeout_s, "admin": s.admin_idle_timeout_s}[kind]

    def _create_session(
        self, conn: sqlite3.Connection, kind: str, service: str, transport: str, client_ip: str | None,
        user_id: int | None = None, key_id: int | None = None,
    ) -> str:
        token = new_token()
        now = ts()
        conn.execute(
            "INSERT INTO sessions(token_hash, user_id, kind, service, transport, client_ip, key_id, created_at, last_seen_at, expires_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (hash_token(token), user_id, kind, service, transport, client_ip, key_id, now, now,
             ts_in(hours=self.cfg.sessions.session_ttl_hours)),
        )
        return token

    def create_guest_session(self, service: str, client_ip: str | None, transport: str = "ssh") -> dict:
        if service != "ssh":
            raise BBSError("forbidden", "guest sessions are only available on the public gateway")
        if not self.cfg.guest.enabled:
            raise BBSError("forbidden", "guest access is disabled on this node")
        with self.db.session() as conn, transaction(conn):
            token = self._create_session(conn, "guest", service, transport, client_ip)
        event(log, "session_created", kind="guest", service=service, client_ip=client_ip)
        return {"token": token, "kind": "guest", "idle_timeout_s": self._idle_for("guest")}

    def _key_owner(self, conn: sqlite3.Connection, username: str, public_key: str) -> sqlite3.Row | None:
        try:
            key = parse_public_key(public_key)
        except BBSError:
            return None
        return conn.execute(
            "SELECT u.id AS user_id, u.username, u.role, u.status, k.id AS key_id FROM ssh_keys k"
            " JOIN users u ON u.id = k.user_id"
            " WHERE k.fingerprint = ? AND k.revoked_at IS NULL AND u.username = ?",
            (key.fingerprint, username.strip().lower()),
        ).fetchone()

    def check_key(self, service: str, username: str, public_key: str, client_ip: str | None) -> bool:
        """Would this (username, key) be allowed to log in on this gateway? Used during the
        SSH handshake, before the client has proved it holds the private key."""
        self.limits.check("auth", client_ip or "-", self.cfg.rate_limits.auth_attempts_per_ip_per_min, 60)
        with self.db.session() as conn:
            row = self._key_owner(conn, username, public_key)
        if row is None:
            return False
        if service == "admin":
            return row["role"] == "admin" and row["status"] == "ACTIVE"
        return row["status"] != "REJECTED"

    def create_key_session(self, service: str, username: str, public_key: str, client_ip: str | None, transport: str = "ssh") -> dict:
        """Called by a gateway after the SSH layer verified the client's signature with this key."""
        with self.tx() as conn:
            row = self._key_owner(conn, username, public_key)
            if row is None:
                raise Denied(BBSError("unauthenticated", "key not registered for this user"), None, "login", "user",
                             username[:32], {"service": service, "why": "unknown key"}, client_ip)
            if service == "admin":
                if row["role"] != "admin" or row["status"] != "ACTIVE":
                    raise Denied(BBSError("forbidden", "not an active administrator"), None, "admin_login", "user",
                                 row["user_id"], {"why": "not an active admin"}, client_ip)
                kind = "admin"
            else:
                if row["status"] == "REJECTED":
                    raise BBSError("forbidden", "this registration was rejected")
                kind = "user"
            conn.execute("UPDATE ssh_keys SET last_used_at = ? WHERE id = ?", (ts(), row["key_id"]))
            token = self._create_session(conn, kind, service, transport, client_ip, row["user_id"], row["key_id"])
            self.audit(conn, None, "admin_login" if kind == "admin" else "login", "success", "user", row["user_id"],
                       {"service": service, "username": row["username"]}, client_ip)
        event(log, "session_created", kind=kind, service=service, user=row["username"], client_ip=client_ip)
        return {"token": token, "kind": kind, "idle_timeout_s": self._idle_for(kind)}

    # --- password login (devices without an SSH key) ------------------------------------

    def _password_owner(self, conn: sqlite3.Connection, service: str, username: str, password: str,
                        client_ip: str | None) -> sqlite3.Row | None:
        """Check a password, with per-IP rate limiting and a per-account lockout."""
        rl = self.cfg.rate_limits
        self.limits.check("auth", client_ip or "-", rl.auth_attempts_per_ip_per_min, 60)
        if service != "ssh" or not self.cfg.registration.allow_passwords:
            return None  # the admin console stays key-only
        row = conn.execute("SELECT id AS user_id, username, role, status, password_hash, failed_logins, locked_until"
                           " FROM users WHERE username = ?", ((username or "").strip().lower()[:64],)).fetchone()
        if row is not None and row["locked_until"] and parse_ts(row["locked_until"]) > utcnow():
            verify_password(password, None)  # same cost either way
            return None
        ok = verify_password(password, row["password_hash"] if row else None)
        if row is None:
            return None
        if not ok:
            fails = row["failed_logins"] + 1
            locked = ts_in(minutes=rl.lock_minutes) if fails >= rl.failed_logins_before_lock else None
            conn.execute("UPDATE users SET failed_logins = ?, locked_until = ? WHERE id = ?",
                         (0 if locked else fails, locked, row["user_id"]))
            if locked:
                self.audit(conn, None, "account_locked", "denied", "user", row["user_id"],
                           {"why": f"{fails} wrong passwords", "minutes": rl.lock_minutes}, client_ip)
            return None
        if row["status"] == "REJECTED":
            return None
        if row["failed_logins"] or row["locked_until"]:
            conn.execute("UPDATE users SET failed_logins = 0, locked_until = NULL WHERE id = ?", (row["user_id"],))
        return row

    def check_password(self, service: str, username: str, password: str, client_ip: str | None) -> bool:
        with self.db.session() as conn, transaction(conn):
            return self._password_owner(conn, service, username, password, client_ip) is not None

    def create_password_session(self, service: str, username: str, password: str, client_ip: str | None,
                                transport: str = "ssh") -> dict:
        with self.tx() as conn:
            row = self._password_owner(conn, service, username, password, client_ip)
            if row is None:
                raise Denied(BBSError("unauthenticated", "wrong username or password"), None, "login", "user",
                             (username or "")[:32], {"service": service, "why": "bad password"}, client_ip)
            token = self._create_session(conn, "user", service, transport, client_ip, row["user_id"])
            self.audit(conn, None, "login", "success", "user", row["user_id"],
                       {"service": service, "username": row["username"], "method": "password"}, client_ip)
        event(log, "session_created", kind="user", service=service, user=row["username"], client_ip=client_ip)
        return {"token": token, "kind": "user", "idle_timeout_s": self._idle_for("user")}

    def set_my_password(self, who: Principal, password: str | None) -> dict:
        """Set, change or (password=None) remove your own password."""
        if who.user_id is None or who.status != "ACTIVE":
            raise BBSError("forbidden", "only approved users can change their password")
        if password is not None and not self.cfg.registration.allow_passwords:
            raise BBSError("forbidden", "password logins are turned off on this node")
        with self.tx() as conn:
            if password is None:
                n = conn.execute("SELECT COUNT(*) FROM ssh_keys WHERE user_id = ? AND revoked_at IS NULL",
                                 (who.user_id,)).fetchone()[0]
                if n == 0:
                    raise BBSError("conflict", "you have no SSH key, so the password is your only way in")
                conn.execute("UPDATE users SET password_hash = NULL, updated_at = ? WHERE id = ?", (ts(), who.user_id))
            else:
                conn.execute("UPDATE users SET password_hash = ?, failed_logins = 0, locked_until = NULL, updated_at = ?"
                             " WHERE id = ?", (hash_password(validate_password(password)), ts(), who.user_id))
            self.audit(conn, who, "password_removed" if password is None else "password_set", "success",
                       "user", who.user_id, {}, who.client_ip)
        return {"ok": True, "has_password": password is not None}

    def principal(self, token: str | None, service: str) -> Principal:
        """Resolve a session token. Account status and role are re-read on every request,
        so a suspension or revoked key takes effect immediately."""
        if not token:
            raise BBSError("unauthenticated", "no session")
        with self.db.session() as conn:
            row = conn.execute(
                "SELECT s.id, s.kind, s.service, s.user_id, s.client_ip, s.expires_at, s.ended_at, s.last_seen_at,"
                " s.key_id, k.revoked_at AS key_revoked, u.username, u.callsign, u.callsign_verified, u.role, u.status"
                " FROM sessions s LEFT JOIN users u ON u.id = s.user_id LEFT JOIN ssh_keys k ON k.id = s.key_id"
                " WHERE s.token_hash = ?",
                (hash_token(token),),
            ).fetchone()
            if row is None or row["ended_at"] is not None:
                raise BBSError("unauthenticated", "session not found or ended")
            if row["service"] != service:
                raise BBSError("unauthenticated", "session belongs to another gateway")
            now = utcnow()
            if parse_ts(row["expires_at"]) <= now:
                raise BBSError("unauthenticated", "session expired")
            if parse_ts(row["last_seen_at"]) + timedelta(seconds=self._idle_for(row["kind"]) + 60) <= now:
                raise BBSError("unauthenticated", "session idle too long")
            if row["key_id"] is not None and row["key_revoked"] is not None:
                raise BBSError("unauthenticated", "the key for this session was revoked")
            if row["kind"] == "admin" and (row["role"] != "admin" or row["status"] != "ACTIVE"):
                raise BBSError("unauthenticated", "administrator access withdrawn")
            if row["last_seen_at"] != ts(now):
                conn.execute("UPDATE sessions SET last_seen_at = ? WHERE id = ?", (ts(now), row["id"]))
        return Principal(
            session_id=row["id"], kind=row["kind"], service=row["service"], user_id=row["user_id"],
            username=row["username"], callsign=row["callsign"], callsign_verified=bool(row["callsign_verified"]),
            role=row["role"], status=row["status"], client_ip=row["client_ip"], idle_timeout_s=self._idle_for(row["kind"]),
            sysop_lan=self._sysop_lan(row["service"], row["client_ip"]),
        )

    def _sysop_lan(self, service: str, client_ip: str | None) -> bool:
        sc = self.cfg.sysop
        return service == "ssh" and sc.menu_in_bbs and ip_in_networks(client_ip, sc.allowed_networks)

    def end_session(self, who: Principal) -> None:
        with self.db.session() as conn:
            conn.execute("UPDATE sessions SET ended_at = ? WHERE id = ? AND ended_at IS NULL", (ts(), who.session_id))

    def me(self, who: Principal) -> dict:
        out: dict[str, Any] = {
            "kind": who.kind, "username": who.username, "callsign": who.callsign,
            "callsign_verified": who.callsign_verified, "role": who.role, "status": who.status,
            "idle_timeout_s": who.idle_timeout_s,
            "can_post": who.is_active, "can_mail": who.is_active,
            "sysop": who.service == "ssh" and who.is_admin,
        }
        if who.user_id:
            with self.db.session() as conn:
                u = conn.execute("SELECT display_name, contact, status_reason, created_at, password_hash FROM users WHERE id = ?", (who.user_id,)).fetchone()
                out["has_password"] = bool(u["password_hash"])
                out["contact"] = u["contact"]  # your own; only sysops see anyone else's
                out["display_name"] = u["display_name"]
                out["status_reason"] = u["status_reason"]
                out["created_at"] = u["created_at"]
                out["unread_mail"] = self._unread_mail(conn, who.user_id) if who.is_active else 0
        return out

    # --- system --------------------------------------------------------------------

    def system_info(self) -> dict:
        n = self.cfg.node
        with self.db.session() as conn:
            counts = conn.execute(
                "SELECT (SELECT COUNT(*) FROM users WHERE status = 'ACTIVE') AS users,"
                " (SELECT COUNT(*) FROM messages WHERE status = 'VISIBLE') AS bulletins,"
                " (SELECT COUNT(*) FROM boards WHERE archived = 0) AS boards"
            ).fetchone()
        return {
            "node_id": n.id, "node_name": n.name, "location": n.location, "sysop": n.sysop,
            "status": "ONLINE", "version": os.environ.get("BBS_VERSION", "1.0.0"),
            "time_utc": ts(), "uptime_s": int(time.time() - STARTED),
            "active_users": counts["users"], "bulletins": counts["bulletins"], "boards": counts["boards"],
            "registration_open": self.cfg.registration.enabled, "guest_access": self.cfg.guest.enabled,
            "disclaimer": EMERGENCY_DISCLAIMER,
        }

    def health(self) -> dict:
        with self.db.session() as conn:
            ok = conn.execute("SELECT 1").fetchone()[0] == 1
            version = schema_version(conn)
        return {"status": "ok" if ok else "error", "schema_version": version}

    # --- registration --------------------------------------------------------------

    def register(self, who: Principal, data: dict) -> dict:
        if not self.cfg.registration.enabled:
            raise BBSError("forbidden", "registration is closed on this node")
        if who.user_id is not None:
            raise BBSError("forbidden", "you already have an account")
        rl = self.cfg.rate_limits
        ip = who.client_ip or "-"
        username = validate_username(data.get("username", ""))
        callsign = validate_callsign(data.get("callsign"))
        if looks_like_callsign(username) and username.upper() != callsign:
            raise BBSError("invalid_username", "a username that looks like a callsign must be your own callsign (enter it as the callsign too)")
        if self.cfg.registration.require_callsign and not callsign:
            raise BBSError("invalid_callsign", "a callsign is required to register on this node")
        display = sanitize_line(data.get("display_name") or "", 40) or None
        contact = sanitize_line(data.get("contact") or "", 120) or None
        key = parse_public_key(data["public_key"]) if data.get("public_key") else None
        pw_hash = None
        if data.get("password") is not None:
            if not self.cfg.registration.allow_passwords:
                raise BBSError("forbidden", "password logins are turned off on this node; register with an SSH key")
            pw_hash = hash_password(validate_password(data["password"]))
        if key is None and pw_hash is None:
            raise BBSError("invalid_input", "an SSH key or a password is required")
        self.limits.check("register_ip", ip, rl.registrations_per_ip_per_hour, 3600)
        self.limits.check("register_all", "-", rl.registrations_global_per_hour, 3600)
        now = ts()
        with self.db.session() as conn, transaction(conn):
            # Impersonation: a new username may not equal anyone's callsign, and a new
            # callsign may not equal anyone's username or live callsign.
            clash = conn.execute(
                "SELECT 1 FROM users WHERE username = ? OR (callsign = ? AND status != 'REJECTED')",
                (username, username),
            ).fetchone()
            if clash:
                raise BBSError("conflict", "that username is taken")
            if callsign:
                clash = conn.execute(
                    "SELECT 1 FROM users WHERE username = ? OR (callsign = ? AND status != 'REJECTED')",
                    (callsign.lower(), callsign),
                ).fetchone()
                if clash:
                    raise BBSError("conflict", "that callsign is already registered; contact the sysop if it is yours")
            if key and conn.execute("SELECT 1 FROM ssh_keys WHERE fingerprint = ?", (key.fingerprint,)).fetchone():
                raise BBSError("conflict", "this device's SSH key is already registered to an account")
            cur = conn.execute(
                "INSERT INTO users(username, callsign, display_name, contact, role, status, password_hash, created_at, updated_at)"
                " VALUES (?,?,?,?, 'user', 'PENDING', ?, ?, ?)",
                (username, callsign, display, contact, pw_hash, now, now),
            )
            uid = cur.lastrowid
            if key:
                conn.execute(
                    "INSERT INTO ssh_keys(user_id, key_type, key_data, fingerprint, comment, created_at) VALUES (?,?,?,?,?,?)",
                    (uid, key.key_type, key.key_data, key.fingerprint, key.comment, now),
                )
            conn.execute(
                "INSERT INTO registrations(user_id, requested_at, client_ip, transport) VALUES (?,?,?,?)",
                (uid, now, who.client_ip, who.service),
            )
            fp = key.fingerprint if key else None
            self.audit(conn, who, "registration_submitted", "success", "user", uid,
                       {"username": username, "callsign": callsign, "fingerprint": fp, "password": pw_hash is not None})
        event(log, "registration_submitted", user=username, client_ip=who.client_ip)
        return {"username": username, "callsign": callsign, "status": "PENDING", "fingerprint": fp,
                "password": pw_hash is not None}

    # --- boards and bulletins ------------------------------------------------------

    def _board(self, conn: sqlite3.Connection, name: str, who: Principal, include_archived: bool = False) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM boards WHERE name = ?", ((name or "").strip(),)).fetchone()
        if row is None or (row["archived"] and not (include_archived or who.is_admin)):
            raise BBSError("not_found", "no such board")
        if not row["is_public"] and not (who.is_active or who.is_admin):
            raise BBSError("not_found", "no such board")
        return row

    def _readable_boards_sql(self, who: Principal) -> str:
        if who.is_admin:
            return "1=1"
        if who.is_active:
            return "b.archived = 0"
        return "b.archived = 0 AND b.is_public = 1"

    def list_boards(self, who: Principal) -> list[dict]:
        uid = who.user_id if who.is_active else None
        with self.db.session() as conn:
            rows = conn.execute(
                "SELECT b.name, b.description, b.post_policy, b.is_public, b.is_emergency, b.archived,"
                " (SELECT COUNT(*) FROM messages m WHERE m.board_id = b.id AND m.status = 'VISIBLE') AS count,"
                " (SELECT MAX(id) FROM messages m WHERE m.board_id = b.id AND m.status = 'VISIBLE') AS latest,"
                " (SELECT COUNT(*) FROM messages m WHERE m.board_id = b.id AND m.status = 'VISIBLE'"
                "   AND ? IS NOT NULL AND NOT EXISTS (SELECT 1 FROM message_reads r WHERE r.user_id = ? AND r.message_id = m.id)) AS unread"
                f" FROM boards b WHERE {self._readable_boards_sql(who)} ORDER BY b.sort_order, b.name",
                (uid, uid),
            ).fetchall()
        return [
            {
                "name": r["name"], "description": r["description"], "count": r["count"], "latest": r["latest"],
                "unread": r["unread"], "emergency": bool(r["is_emergency"]), "archived": bool(r["archived"]),
                "public": bool(r["is_public"]), "can_post": self._can_post_row(r, who),
            }
            for r in rows
        ]

    def _can_post_row(self, board: sqlite3.Row, who: Principal) -> bool:
        if board["archived"] or not who.is_active:
            return False
        if board["post_policy"] == "admins":
            return who.role == "admin"
        return True

    def _msg_summary(self, r: sqlite3.Row, read_ids: set[int] | None = None) -> dict:
        out = {
            "number": r["id"], "mid": r["mid"], "board": r["board"], "author": r["author_name"],
            "callsign": r["author_callsign"], "subject": r["subject"], "created_at": r["created_at"],
            "priority": r["priority"], "area": r["area"], "expires_at": r["expires_at"],
            "status": r["status"], "origin_node": r["origin_node"],
            "expired": bool(r["expires_at"] and parse_ts(r["expires_at"]) <= utcnow()),
        }
        if read_ids is not None:
            out["read"] = r["id"] in read_ids
        return out

    def _read_ids(self, conn: sqlite3.Connection, who: Principal, ids: list[int]) -> set[int] | None:
        if not who.is_active or not ids:
            return None
        q = ",".join("?" * len(ids))
        return {r[0] for r in conn.execute(
            f"SELECT message_id FROM message_reads WHERE user_id = ? AND message_id IN ({q})", (who.user_id, *ids))}

    def list_messages(self, who: Principal, board: str | None, before: int | None, limit: int) -> dict:
        limit = max(1, min(limit, 50))
        with self.db.session() as conn:
            params: list[Any] = []
            where = [self._readable_boards_sql(who)]
            where.append("m.status = 'VISIBLE'" if not who.is_admin else "m.status != 'DELETED'")
            b = None
            if board:
                b = self._board(conn, board, who)
                where.append("m.board_id = ?")
                params.append(b["id"])
            if before:
                where.append("m.id < ?")
                params.append(before)
            order = "m.id DESC"
            if b is not None and b["is_emergency"]:
                # Emergency board: live traffic first, highest priority first, then newest.
                order = ("(m.expires_at IS NOT NULL AND m.expires_at <= strftime('%Y-%m-%dT%H:%M:%SZ','now')) ASC,"
                         " CASE m.priority WHEN 'EMERGENCY' THEN 3 WHEN 'PRIORITY' THEN 2 ELSE 1 END DESC, m.id DESC")
            rows = conn.execute(
                "SELECT m.*, b.name AS board FROM messages m JOIN boards b ON b.id = m.board_id"
                f" WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ?",
                (*params, limit + 1),
            ).fetchall()
            more = len(rows) > limit
            rows = rows[:limit]
            reads = self._read_ids(conn, who, [r["id"] for r in rows])
        out: dict[str, Any] = {"messages": [self._msg_summary(r, reads) for r in rows], "more": more}
        if b is not None:
            out["board"] = {"name": b["name"], "description": b["description"], "emergency": bool(b["is_emergency"]),
                            "can_post": self._can_post_row(b, who)}
            if b["is_emergency"]:
                out["disclaimer"] = EMERGENCY_DISCLAIMER
        if b is not None and b["is_emergency"] and before:
            # Emergency ordering isn't by number, so "before" paging doesn't apply there.
            out["more"] = False
        return out

    def feed(self, who: Principal, after: int | None, limit: int = 50) -> dict:
        """What a desktop client needs for notifications, in one call: bulletins newer than
        `after` (message number), unread mail, and - for administrators only - pending
        registrations. Without `after` it returns just the latest number, so a new client
        doesn't get a flood of old posts."""
        limit = max(1, min(limit, 100))
        with self.db.session() as conn:
            latest = conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
            rows = []
            if after is not None:
                where = [self._readable_boards_sql(who), "m.status = 'VISIBLE'", "m.id > ?"]
                rows = conn.execute(
                    "SELECT m.*, b.name AS board, b.is_emergency FROM messages m JOIN boards b ON b.id = m.board_id"
                    f" WHERE {' AND '.join(where)} ORDER BY m.id ASC LIMIT ?", (after, limit)).fetchall()
            bulletins = []
            for r in rows:
                d = self._msg_summary(r)
                d["emergency_board"] = bool(r["is_emergency"])
                bulletins.append(d)
            mail: list[dict] = []
            if who.is_active:
                mail = [self._pm_dict(r, who) for r in conn.execute(
                    "SELECT * FROM private_messages WHERE recipient_id = ? AND recipient_state = 'ACTIVE'"
                    " AND read_at IS NULL ORDER BY id DESC LIMIT 50", (who.user_id,))]
            out: dict[str, Any] = {"latest": latest, "bulletins": bulletins, "unread_mail": mail,
                                   "sysop": who.is_admin}
            if who.is_admin:
                out["pending"] = [{"id": r["id"], "username": r["username"], "callsign": r["callsign"],
                                   "created_at": r["created_at"]} for r in conn.execute(
                    "SELECT id, username, callsign, created_at FROM users WHERE status = 'PENDING' ORDER BY id LIMIT 50")]
        return out

    def get_message(self, who: Principal, number: int) -> dict:
        with self.db.session() as conn:
            r = conn.execute(
                "SELECT m.*, b.name AS board, b.is_public, b.archived, b.is_emergency FROM messages m"
                " JOIN boards b ON b.id = m.board_id WHERE m.id = ?",
                (number,),
            ).fetchone()
            if r is None or r["status"] == "DELETED":
                raise BBSError("not_found", "no such message")
            if not who.is_admin:
                if r["status"] != "VISIBLE" or r["archived"] or (not r["is_public"] and not who.is_active):
                    raise BBSError("not_found", "no such message")
            out = self._msg_summary(r)
            out["body"] = r["body"]
            out["parent_mid"] = r["parent_mid"]
            out["hop_count"] = r["hop_count"]
            if r["parent_mid"]:
                p = conn.execute("SELECT id FROM messages WHERE mid = ?", (r["parent_mid"],)).fetchone()
                out["parent_number"] = p["id"] if p else None
            if r["is_emergency"]:
                out["disclaimer"] = EMERGENCY_DISCLAIMER
            if who.is_admin:
                out["moderation_reason"] = r["moderation_reason"]
            if who.is_active:
                conn.execute(
                    "INSERT OR IGNORE INTO message_reads(user_id, message_id, read_at) VALUES (?,?,?)",
                    (who.user_id, number, ts()),
                )
        return out

    def mark_read(self, who: Principal, number: int) -> None:
        if not who.is_active:
            raise BBSError("forbidden", "only approved users keep read markers")
        self.get_message(who, number)

    def post(self, who: Principal, data: dict) -> dict:
        if not who.is_active:
            raise BBSError("forbidden", "posting needs an approved account" if who.user_id else "guests cannot post")
        mc = self.cfg.messages
        subject = sanitize_line(data.get("subject", ""), mc.subject_max)
        body = sanitize_body(data.get("body", ""), mc.body_max_bytes)
        if not subject:
            raise BBSError("invalid_input", "a subject is required")
        if not body:
            raise BBSError("invalid_input", "the message is empty")
        priority = (data.get("priority") or "ROUTINE").upper()
        if priority not in PRIORITY_RANK:
            raise BBSError("invalid_input", "priority must be ROUTINE, PRIORITY or EMERGENCY")
        self.limits.check("post", str(who.user_id), self.cfg.rate_limits.posts_per_user_per_hour, 3600)
        now = utcnow()
        with self.tx() as conn:
            b = self._board(conn, data.get("board", ""), who)
            if not self._can_post_row(b, who):
                raise Denied(BBSError("forbidden", f"you can't post on {b['name']}"), who, "post", "board", b["name"])
            area = expires = None
            if b["is_emergency"]:
                area = sanitize_line(data.get("area") or "", 40)
                if not area:
                    raise BBSError("invalid_input", "emergency bulletins need a geographic area")
                hours = int(data.get("expires_hours") or mc.emergency_default_expiry_hours)
                if not 1 <= hours <= mc.emergency_max_expiry_hours:
                    raise BBSError("invalid_input", f"expiry must be 1-{mc.emergency_max_expiry_hours} hours")
                expires = ts(now + timedelta(hours=hours))
            else:
                priority = "ROUTINE"
            parent_mid = None
            if data.get("reply_to"):
                parent = conn.execute("SELECT mid, status FROM messages WHERE id = ?", (int(data["reply_to"]),)).fetchone()
                if parent is None or parent["status"] != "VISIBLE":
                    raise BBSError("not_found", "the message you are replying to doesn't exist")
                parent_mid = parent["mid"]
            chash = content_hash(b["name"], subject, body)
            if mc.duplicate_window_hours:
                dup = conn.execute(
                    "SELECT id FROM messages WHERE board_id = ? AND content_hash = ? AND created_at >= ? AND status != 'DELETED'",
                    (b["id"], chash, ts(now - timedelta(hours=mc.duplicate_window_hours))),
                ).fetchone()
                if dup:
                    raise BBSError("duplicate", f"this is a duplicate of message #{dup['id']}")
            mid = new_mid(self.cfg.node.id)
            cur = conn.execute(
                "INSERT INTO messages(mid, origin_node, board_id, author_id, author_name, author_callsign, subject, body,"
                " parent_mid, priority, area, expires_at, content_hash, route, created_at, received_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (mid, self.cfg.node.id, b["id"], who.user_id, who.username, who.callsign, subject, body, parent_mid,
                 priority, area, expires, chash, json.dumps([self.cfg.node.id]), ts(now), ts(now), ts(now)),
            )
            number = cur.lastrowid
            conn.execute("INSERT INTO message_reads(user_id, message_id, read_at) VALUES (?,?,?)", (who.user_id, number, ts(now)))
            if b["is_emergency"]:
                self.audit(conn, who, "emergency_post", "success", "message", number, {"priority": priority, "area": area})
            queued = 0
            if b["forward"] and self.mesh.peers:
                payload = {"author": who.username, "callsign": who.callsign, "subject": subject, "body": body,
                           "parent_mid": parent_mid, "area": area}
                queued = self.mesh.send_new(conn, self.mesh.envelope(
                    "bulletin", mid, {"board": b["name"]}, payload, ts(now), priority, expires))
        event(log, "bulletin_posted", user=who.username, board=b["name"], number=number, priority=priority, peers=queued)
        return {"number": number, "mid": mid, "board": b["name"], "forwarded_to": queued}

    def search(self, who: Principal, q: dict) -> dict:
        limit = max(1, min(int(q.get("limit") or 25), 50))
        where = [self._readable_boards_sql(who), "m.status = 'VISIBLE'"]
        params: list[Any] = []
        join = ""
        if q.get("number"):
            where.append("m.id = ?")
            params.append(int(q["number"]))
        if q.get("board"):
            where.append("b.name = ?")
            params.append(str(q["board"]).strip())
        if q.get("sender"):
            s = sanitize_line(str(q["sender"]), 40)
            where.append("(m.author_name = ? OR m.author_callsign = ?)")
            params += [s, s]
        for field in ("subject", "text"):
            if q.get(field):
                fq = fts_query(str(q[field]), "subject" if field == "subject" else None)
                if fq is None:
                    continue
                join = " JOIN messages_fts f ON f.rowid = m.id"
                where.append("messages_fts MATCH ?")
                params.append(fq)
        if len(where) == 2:
            raise BBSError("invalid_input", "give a message number, board, sender, subject or text to search for")
        with self.db.session() as conn:
            rows = conn.execute(
                f"SELECT m.*, b.name AS board FROM messages m JOIN boards b ON b.id = m.board_id{join}"
                f" WHERE {' AND '.join(where)} ORDER BY m.id DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
            reads = self._read_ids(conn, who, [r["id"] for r in rows])
        return {"messages": [self._msg_summary(r, reads) for r in rows]}

    # --- private mail --------------------------------------------------------------

    def _need_active(self, who: Principal, what: str = "private mail") -> None:
        if not who.is_active:
            raise BBSError("forbidden", f"{what} needs an approved account")

    def _unread_mail(self, conn: sqlite3.Connection, uid: int) -> int:
        return conn.execute(
            "SELECT COUNT(*) FROM private_messages WHERE recipient_id = ? AND recipient_state = 'ACTIVE' AND read_at IS NULL",
            (uid,),
        ).fetchone()[0]

    def _pm_dict(self, r: sqlite3.Row, who: Principal, with_body: bool = False) -> dict:
        out = {
            "id": r["id"], "mid": r["mid"], "from": r["sender_name"], "to": r["recipient_address"],
            "subject": r["subject"], "created_at": r["created_at"], "delivery_state": r["delivery_state"],
            "read": r["read_at"] is not None, "parent_id": r["parent_id"],
            "folder": "inbox" if r["recipient_id"] == who.user_id else "sent",
        }
        if with_body:
            out["body"] = r["body"]
        return out

    def mail_list(self, who: Principal, folder: str, before: int | None, limit: int) -> dict:
        self._need_active(who)
        limit = max(1, min(limit, 50))
        if folder == "inbox":
            where, state = "recipient_id = ?", "recipient_state"
        elif folder == "sent":
            where, state = "sender_id = ?", "sender_state"
        elif folder == "archive":
            where, state = "(recipient_id = ? AND recipient_state = 'ARCHIVED') OR (sender_id = ? AND sender_state = 'ARCHIVED')", None
        else:
            raise BBSError("invalid_input", "folder must be inbox, sent or archive")
        params: list[Any] = [who.user_id] if state else [who.user_id, who.user_id]
        sql = f"SELECT * FROM private_messages WHERE ({where})"
        if state:
            sql += f" AND {state} = 'ACTIVE'"
        if before:
            sql += " AND id < ?"
            params.append(before)
        with self.db.session() as conn:
            rows = conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*params, limit + 1)).fetchall()
            unread = self._unread_mail(conn, who.user_id)
        return {"messages": [self._pm_dict(r, who) for r in rows[:limit]], "more": len(rows) > limit, "unread": unread}

    def _own_pm(self, conn: sqlite3.Connection, who: Principal, pm_id: int) -> sqlite3.Row:
        r = conn.execute("SELECT * FROM private_messages WHERE id = ?", (pm_id,)).fetchone()
        if r is None:
            raise BBSError("not_found", "no such message")
        is_rcpt = r["recipient_id"] == who.user_id and r["recipient_state"] != "DELETED"
        is_sender = r["sender_id"] == who.user_id and r["sender_state"] != "DELETED"
        if not (is_rcpt or is_sender):
            # Same answer as a missing message: don't confirm that someone else's mail exists.
            raise BBSError("not_found", "no such message")
        return r

    def mail_get(self, who: Principal, pm_id: int) -> dict:
        self._need_active(who)
        with self.db.session() as conn:
            r = self._own_pm(conn, who, pm_id)
            if r["recipient_id"] == who.user_id and r["read_at"] is None:
                conn.execute("UPDATE private_messages SET read_at = ? WHERE id = ?", (ts(), pm_id))
            return self._pm_dict(r, who, with_body=True)

    def mail_send(self, who: Principal, data: dict) -> dict:
        self._need_active(who)
        mc = self.cfg.messages
        to = sanitize_line(data.get("to", ""), 64)
        subject = sanitize_line(data.get("subject", ""), mc.subject_max)
        body = sanitize_body(data.get("body", ""), mc.body_max_bytes)
        if not to or not subject or not body:
            raise BBSError("invalid_input", "recipient, subject and message are all required")
        node = None
        if "@" in to:
            to, _, node = to.partition("@")
            node = node.upper()
            if node == self.cfg.node.id:
                node = None
        self.limits.check("mail", str(who.user_id), self.cfg.rate_limits.mail_per_user_per_hour, 3600)
        if node is not None:
            return self._mail_remote(who, to, node, subject, body)
        with self.db.session() as conn, transaction(conn):
            rcpt = conn.execute(
                "SELECT id, username, status FROM users WHERE (username = ? OR (callsign = ? AND callsign_verified = 1))",
                (to.lower(), to.upper()),
            ).fetchone()
            if rcpt is None or rcpt["status"] != "ACTIVE":
                raise BBSError("not_found", f"no active user called {to}")
            parent_id = None
            if data.get("reply_to"):
                parent = self._own_pm(conn, who, int(data["reply_to"]))
                parent_id = parent["id"]
            now = ts()
            cur = conn.execute(
                "INSERT INTO private_messages(mid, origin_node, sender_id, sender_name, recipient_id, recipient_address,"
                " subject, body, parent_id, delivery_state, created_at, delivered_at) VALUES (?,?,?,?,?,?,?,?,?, 'DELIVERED', ?, ?)",
                (new_mid(self.cfg.node.id), self.cfg.node.id, who.user_id, who.username, rcpt["id"], rcpt["username"],
                 subject, body, parent_id, now, now),
            )
        event(log, "mail_sent", sender=who.username, recipient=rcpt["username"], id=cur.lastrowid)
        return {"id": cur.lastrowid, "to": rcpt["username"], "delivery_state": "DELIVERED"}

    def _mail_remote(self, who: Principal, user: str, node: str, subject: str, body: str) -> dict:
        from .mesh import NODE_RE
        if not USER_RE.match(user) or not NODE_RE.match(node):
            raise BBSError("invalid_input", "remote addresses look like user@NODE-ID")
        if not self.mesh.peers:
            raise BBSError("not_available", "this node isn't linked to any other node, so it can't send mail off-node")
        address = f"{user}@{node}"
        mid, now = new_mid(self.cfg.node.id), ts()
        with self.tx() as conn:
            cur = conn.execute(
                "INSERT INTO private_messages(mid, origin_node, sender_id, sender_name, recipient_id, recipient_address,"
                " subject, body, delivery_state, created_at) VALUES (?,?,?,?,NULL,?,?,?, 'QUEUED', ?)",
                (mid, self.cfg.node.id, who.user_id, who.username, address, subject, body, now),
            )
            payload = {"from": who.username, "callsign": who.callsign, "to": user, "subject": subject, "body": body}
            env = self.mesh.envelope("private", mid, {"user": user, "node": node}, payload, now,
                                     expires_at=ts_in(days=14))
            self.mesh.send_new(conn, env)
        event(log, "mail_queued", sender=who.username, recipient=address, id=cur.lastrowid)
        return {"id": cur.lastrowid, "to": address, "delivery_state": "QUEUED"}

    def mail_set_state(self, who: Principal, pm_id: int, state: str) -> None:
        self._need_active(who)
        if state not in ("ARCHIVED", "DELETED", "ACTIVE"):
            raise BBSError("invalid_input", "bad state")
        with self.db.session() as conn, transaction(conn):
            r = self._own_pm(conn, who, pm_id)
            if r["recipient_id"] == who.user_id:
                conn.execute("UPDATE private_messages SET recipient_state = ? WHERE id = ?", (state, pm_id))
            if r["sender_id"] == who.user_id:
                conn.execute("UPDATE private_messages SET sender_state = ? WHERE id = ?", (state, pm_id))
            # Once both sides have deleted it, the content goes for good.
            conn.execute(
                "DELETE FROM private_messages WHERE id = ? AND recipient_state = 'DELETED' AND sender_state = 'DELETED'"
                " AND NOT EXISTS (SELECT 1 FROM private_messages c WHERE c.parent_id = private_messages.id)",
                (pm_id,),
            )

    # --- the user's own keys -------------------------------------------------------

    def _keys(self, conn: sqlite3.Connection, uid: int) -> list[dict]:
        return [
            {"id": r["id"], "type": r["key_type"], "fingerprint": r["fingerprint"], "comment": r["comment"],
             "created_at": r["created_at"], "revoked_at": r["revoked_at"], "last_used_at": r["last_used_at"]}
            for r in conn.execute("SELECT * FROM ssh_keys WHERE user_id = ? ORDER BY id", (uid,))
        ]

    def update_my_profile(self, who: Principal, data: dict) -> dict:
        """Change your own display name, contact details or callsign. A new callsign is
        unverified until a sysop checks it, and the same impersonation rules as at
        registration apply."""
        if who.user_id is None or who.status not in ("ACTIVE", "PENDING"):
            raise BBSError("forbidden", "only registered users can change their details")
        changes: dict[str, Any] = {}
        if "display_name" in data:
            changes["display_name"] = sanitize_line(data.get("display_name") or "", 40) or None
        if "contact" in data:
            changes["contact"] = sanitize_line(data.get("contact") or "", 120) or None
        with self.tx() as conn:
            u = conn.execute("SELECT username, callsign FROM users WHERE id = ?", (who.user_id,)).fetchone()
            if "callsign" in data:
                call = validate_callsign(data.get("callsign"))
                if call != u["callsign"]:
                    if looks_like_callsign(u["username"]) and call != u["username"].upper():
                        raise BBSError("invalid_callsign", "your username is a callsign, so your callsign must stay the same")
                    if self.cfg.registration.require_callsign and not call:
                        raise BBSError("invalid_callsign", "a callsign is required on this node")
                    if call and conn.execute(
                            "SELECT 1 FROM users WHERE id != ? AND (username = ? OR (callsign = ? AND status != 'REJECTED'))",
                            (who.user_id, call.lower(), call)).fetchone():
                        raise BBSError("conflict", "that callsign is already registered; contact the sysop if it is yours")
                    changes["callsign"] = call
                    changes["callsign_verified"] = 0
            if not changes:
                return {"ok": True, "changed": []}
            sets = ", ".join(f"{k} = ?" for k in changes)
            conn.execute(f"UPDATE users SET {sets}, updated_at = ? WHERE id = ?", (*changes.values(), ts(), who.user_id))
            self.audit(conn, who, "profile_updated", "success", "user", who.user_id,
                       {k: v for k, v in changes.items() if k != "contact"}, who.client_ip)
        return {"ok": True, "changed": sorted(k for k in changes if k != "callsign_verified")}

    def my_keys(self, who: Principal) -> list[dict]:
        if who.user_id is None:
            raise BBSError("forbidden", "guests have no keys")
        with self.db.session() as conn:
            return self._keys(conn, who.user_id)

    def add_my_key(self, who: Principal, public_key: str) -> dict:
        self._need_active(who, "adding keys")
        key = parse_public_key(public_key)
        with self.db.session() as conn, transaction(conn):
            live = conn.execute("SELECT COUNT(*) FROM ssh_keys WHERE user_id = ? AND revoked_at IS NULL", (who.user_id,)).fetchone()[0]
            if live >= self.cfg.registration.max_keys_per_user:
                raise BBSError("conflict", f"you already have {live} keys; revoke one first")
            if conn.execute("SELECT 1 FROM ssh_keys WHERE fingerprint = ?", (key.fingerprint,)).fetchone():
                raise BBSError("conflict", "that key is already registered")
            cur = conn.execute(
                "INSERT INTO ssh_keys(user_id, key_type, key_data, fingerprint, comment, created_at) VALUES (?,?,?,?,?,?)",
                (who.user_id, key.key_type, key.key_data, key.fingerprint, key.comment, ts()),
            )
            self.audit(conn, who, "key_added", "success", "ssh_key", cur.lastrowid, {"fingerprint": key.fingerprint})
        return {"id": cur.lastrowid, "fingerprint": key.fingerprint}

    def revoke_my_key(self, who: Principal, key_id: int) -> None:
        if who.user_id is None:
            raise BBSError("forbidden", "guests have no keys")
        with self.db.session() as conn, transaction(conn):
            row = conn.execute("SELECT * FROM ssh_keys WHERE id = ? AND user_id = ?", (key_id, who.user_id)).fetchone()
            if row is None or row["revoked_at"]:
                raise BBSError("not_found", "no such active key")
            live = conn.execute("SELECT COUNT(*) FROM ssh_keys WHERE user_id = ? AND revoked_at IS NULL", (who.user_id,)).fetchone()[0]
            has_pw = conn.execute("SELECT password_hash IS NOT NULL FROM users WHERE id = ?", (who.user_id,)).fetchone()[0]
            if live <= 1 and not has_pw:
                raise BBSError("conflict", "that is your only key; add a new one (or set a password) before revoking it")
            conn.execute("UPDATE ssh_keys SET revoked_at = ?, revoked_by = ? WHERE id = ?", (ts(), who.user_id, key_id))
            self.audit(conn, who, "key_revoked", "success", "ssh_key", key_id, {"fingerprint": row["fingerprint"], "by": "owner"})

    # --- administration ------------------------------------------------------------

    def _need_admin(self, who: Principal) -> None:
        if not who.is_admin:
            self.audit_now(who, "admin_access", "denied")
            raise BBSError("forbidden", "administrator access only")

    def _user_dict(self, conn: sqlite3.Connection, r: sqlite3.Row, keys: bool = False) -> dict:
        out = {k: r[k] for k in ("id", "username", "callsign", "display_name", "contact", "role", "status",
                                 "status_reason", "created_at", "updated_at", "approved_at")}
        out["callsign_verified"] = bool(r["callsign_verified"])
        out["has_password"] = bool(r["password_hash"])
        if keys:
            out["keys"] = self._keys(conn, r["id"])
            out["registrations"] = [dict(x) for x in conn.execute(
                "SELECT requested_at, client_ip, transport, decision, decided_at, reason,"
                " (SELECT username FROM users a WHERE a.id = decided_by) AS decided_by"
                " FROM registrations WHERE user_id = ? ORDER BY id", (r["id"],))]
        return out

    def admin_users(self, who: Principal, status: str | None, offset: int = 0, limit: int = 20) -> dict:
        self._need_admin(who)
        limit = max(1, min(limit, 50))
        with self.db.session() as conn:
            if status:
                rows = conn.execute("SELECT * FROM users WHERE status = ? ORDER BY id LIMIT ? OFFSET ?",
                                    (status.upper(), limit + 1, max(0, offset))).fetchall()
            else:
                rows = conn.execute("SELECT * FROM users ORDER BY id LIMIT ? OFFSET ?", (limit + 1, max(0, offset))).fetchall()
            return {"users": [self._user_dict(conn, r) for r in rows[:limit]], "more": len(rows) > limit}

    def admin_user(self, who: Principal, user_id: int) -> dict:
        self._need_admin(who)
        with self.db.session() as conn:
            r = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if r is None:
                raise BBSError("not_found", "no such user")
            return self._user_dict(conn, r, keys=True)

    _TRANSITIONS = {
        "approve": ({"PENDING"}, "ACTIVE"),
        "reject": ({"PENDING"}, "REJECTED"),
        "suspend": ({"ACTIVE", "PENDING"}, "SUSPENDED"),
        "reactivate": ({"SUSPENDED"}, "ACTIVE"),
    }

    def admin_set_status(self, who: Principal, user_id: int, action: str, reason: str | None = None,
                         callsign_verified: bool | None = None) -> dict:
        self._need_admin(who)
        if action not in self._TRANSITIONS:
            raise BBSError("invalid_input", "unknown action")
        allowed_from, to = self._TRANSITIONS[action]
        reason = sanitize_line(reason or "", 200) or None
        with self.tx() as conn:
            r = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if r is None:
                raise BBSError("not_found", "no such user")
            if r["id"] == who.user_id:
                raise Denied(BBSError("forbidden", "you can't change the status of your own account"), who,
                             f"user_{action}", "user", user_id, {"why": "own account"})
            if r["status"] not in allowed_from:
                raise Denied(BBSError("conflict", f"can't {action} an account that is {r['status']}"), who,
                             f"user_{action}", "user", user_id, {"from": r["status"]})
            now = ts()
            sets = "status = ?, status_reason = ?, updated_at = ?"
            params: list[Any] = [to, reason, now]
            if action == "approve":
                sets += ", approved_by = ?, approved_at = ?"
                params += [who.user_id, now]
                if callsign_verified is not None and r["callsign"]:
                    sets += ", callsign_verified = ?"
                    params.append(int(callsign_verified))
            conn.execute(f"UPDATE users SET {sets} WHERE id = ?", (*params, user_id))
            if action in ("approve", "reject"):
                conn.execute(
                    "UPDATE registrations SET decision = ?, decided_by = ?, decided_at = ?, reason = ?"
                    " WHERE user_id = ? AND decision IS NULL",
                    ("APPROVED" if action == "approve" else "REJECTED", who.user_id, now, reason, user_id),
                )
            if action in ("suspend", "reject"):
                conn.execute("UPDATE sessions SET ended_at = ? WHERE user_id = ? AND ended_at IS NULL", (now, user_id))
            self.audit(conn, who, f"user_{action}", "success", "user", user_id,
                       {"username": r["username"], "from": r["status"], "to": to, "reason": reason})
        event(log, "user_status_changed", admin=who.username, user=r["username"], action=action)
        return {"id": user_id, "status": to}

    def admin_verify_callsign(self, who: Principal, user_id: int, verified: bool) -> None:
        self._need_admin(who)
        with self.db.session() as conn, transaction(conn):
            r = conn.execute("SELECT username, callsign FROM users WHERE id = ?", (user_id,)).fetchone()
            if r is None or not r["callsign"]:
                raise BBSError("not_found", "no such user with a callsign")
            conn.execute("UPDATE users SET callsign_verified = ?, updated_at = ? WHERE id = ?", (int(verified), ts(), user_id))
            self.audit(conn, who, "callsign_verified" if verified else "callsign_unverified", "success", "user", user_id,
                       {"callsign": r["callsign"]})

    def admin_revoke_key(self, who: Principal, key_id: int) -> None:
        self._need_admin(who)
        with self.db.session() as conn, transaction(conn):
            row = conn.execute("SELECT * FROM ssh_keys WHERE id = ?", (key_id,)).fetchone()
            if row is None or row["revoked_at"]:
                raise BBSError("not_found", "no such active key")
            if row["user_id"] == who.user_id:
                live = conn.execute("SELECT COUNT(*) FROM ssh_keys WHERE user_id = ? AND revoked_at IS NULL", (who.user_id,)).fetchone()[0]
                if live <= 1:
                    raise BBSError("conflict", "that is your only key; you would lock yourself out")
            now = ts()
            conn.execute("UPDATE ssh_keys SET revoked_at = ?, revoked_by = ? WHERE id = ?", (now, who.user_id, key_id))
            conn.execute("UPDATE sessions SET ended_at = ? WHERE key_id = ? AND ended_at IS NULL", (now, key_id))
            self.audit(conn, who, "key_revoked", "success", "ssh_key", key_id,
                       {"user_id": row["user_id"], "fingerprint": row["fingerprint"], "by": "admin"})

    def admin_messages(self, who: Principal, board: str | None, status: str | None, before: int | None, limit: int = 20) -> dict:
        self._need_admin(who)
        limit = max(1, min(limit, 50))
        where, params = ["1=1"], []
        if board:
            where.append("b.name = ?")
            params.append(board)
        if status:
            where.append("m.status = ?")
            params.append(status.upper())
        if before:
            where.append("m.id < ?")
            params.append(before)
        with self.db.session() as conn:
            rows = conn.execute(
                "SELECT m.*, b.name AS board FROM messages m JOIN boards b ON b.id = m.board_id"
                f" WHERE {' AND '.join(where)} ORDER BY m.id DESC LIMIT ?", (*params, limit + 1)).fetchall()
        return {"messages": [self._msg_summary(r) for r in rows[:limit]], "more": len(rows) > limit}

    def admin_moderate(self, who: Principal, number: int, action: str, reason: str | None) -> dict:
        self._need_admin(who)
        to = {"hide": "HIDDEN", "restore": "VISIBLE", "delete": "DELETED"}.get(action)
        if to is None:
            raise BBSError("invalid_input", "action must be hide, restore or delete")
        reason = sanitize_line(reason or "", 200) or None
        with self.db.session() as conn, transaction(conn):
            r = conn.execute("SELECT id, status, subject FROM messages WHERE id = ?", (number,)).fetchone()
            if r is None:
                raise BBSError("not_found", "no such message")
            if r["status"] == "DELETED":
                raise BBSError("conflict", "message already deleted")
            now = ts()
            conn.execute(
                "UPDATE messages SET status = ?, moderated_by = ?, moderated_at = ?, moderation_reason = ?, updated_at = ? WHERE id = ?",
                (to, who.user_id, now, reason, now, number),
            )
            if to == "DELETED":
                # Keep the id/mid (so a forwarded copy isn't re-imported) but drop the content.
                conn.execute("UPDATE messages SET body = '[deleted by moderator]' WHERE id = ?", (number,))
            self.audit(conn, who, f"message_{action}", "success", "message", number,
                       {"from": r["status"], "to": to, "reason": reason})
        return {"number": number, "status": to}

    def admin_board(self, who: Principal, action: str, data: dict) -> dict:
        self._need_admin(who)
        name = (data.get("name") or "").strip().upper()
        with self.db.session() as conn, transaction(conn):
            if action == "create":
                if not re.match(BOARD_NAME_PATTERN, name):
                    raise BBSError("invalid_input", "board names: 2-24 chars, A-Z 0-9 _ -")
                if conn.execute("SELECT 1 FROM boards WHERE name = ?", (name,)).fetchone():
                    raise BBSError("conflict", "a board with that name exists")
                policy = data.get("post") or "users"
                if policy not in ("users", "admins"):
                    raise BBSError("invalid_input", "post must be users or admins")
                conn.execute(
                    "INSERT INTO boards(name, description, post_policy, is_public, is_emergency, sort_order, created_at)"
                    " VALUES (?,?,?,?,0,(SELECT COALESCE(MAX(sort_order),0)+10 FROM boards),?)",
                    (name, sanitize_line(data.get("description") or "", 80), policy, int(bool(data.get("public", True))), ts()),
                )
                detail: dict[str, Any] = {"name": name, "post": policy}
            else:
                b = conn.execute("SELECT * FROM boards WHERE name = ?", (name,)).fetchone()
                if b is None:
                    raise BBSError("not_found", "no such board")
                if action == "rename":
                    new = (data.get("new_name") or "").strip().upper()
                    if not re.match(BOARD_NAME_PATTERN, new):
                        raise BBSError("invalid_input", "board names: 2-24 chars, A-Z 0-9 _ -")
                    if conn.execute("SELECT 1 FROM boards WHERE name = ? AND id != ?", (new, b["id"])).fetchone():
                        raise BBSError("conflict", "a board with that name exists")
                    conn.execute("UPDATE boards SET name = ? WHERE id = ?", (new, b["id"]))
                    detail = {"from": b["name"], "to": new}
                elif action in ("archive", "unarchive"):
                    conn.execute("UPDATE boards SET archived = ? WHERE id = ?", (int(action == "archive"), b["id"]))
                    detail = {"name": b["name"]}
                elif action == "describe":
                    conn.execute("UPDATE boards SET description = ? WHERE id = ?",
                                 (sanitize_line(data.get("description") or "", 80), b["id"]))
                    detail = {"name": b["name"]}
                else:
                    raise BBSError("invalid_input", "action must be create, rename, archive, unarchive or describe")
            self.audit(conn, who, f"board_{action}", "success", "board", name, detail)
        return {"ok": True}

    def admin_audit(self, who: Principal, before: int | None, limit: int = 20) -> dict:
        self._need_admin(who)
        limit = max(1, min(limit, 100))
        with self.db.session() as conn:
            sql, params = "SELECT * FROM audit_log", []
            if before:
                sql += " WHERE id < ?"
                params.append(before)
            rows = conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*params, limit + 1)).fetchall()
        return {"entries": [dict(r) for r in rows[:limit]], "more": len(rows) > limit}

    def admin_queues(self, who: Principal) -> dict:
        self._need_admin(who)
        with self.db.session() as conn:
            states = {r["state"]: r["n"] for r in conn.execute("SELECT state, COUNT(*) AS n FROM forward_queue GROUP BY state")}
            failing = [dict(r) for r in conn.execute(
                "SELECT mid, peer_node, attempts, last_error, updated_at FROM forward_queue WHERE state = 'FAILED'"
                " ORDER BY updated_at DESC LIMIT 10")]
            emergency = conn.execute(
                "SELECT COUNT(*) FROM messages m JOIN boards b ON b.id = m.board_id WHERE b.is_emergency = 1"
                " AND m.status = 'VISIBLE' AND (m.expires_at IS NULL OR m.expires_at > ?)", (ts(),)).fetchone()[0]
            per_peer = {r["peer_node"]: r["n"] for r in conn.execute(
                "SELECT peer_node, COUNT(*) AS n FROM forward_queue WHERE state = 'QUEUED' GROUP BY peer_node")}
        peers = self.mesh.peers
        return {"forward_queue": states, "failed": failing, "live_emergency_bulletins": emergency,
                "forwarding_enabled": bool(peers), "waiting_per_peer": per_peer,
                "note": (f"Linked to {len(peers)} peer node(s); bbs-sync delivers queued messages as links come up."
                         if peers else "No peer nodes configured. Add one with scripts/add-peer.sh (docs/mesh.md).")}

    def admin_nodes(self, who: Principal) -> dict:
        self._need_admin(who)
        with self.db.session() as conn:
            rows = conn.execute(
                "SELECT n.node_id, n.name, n.is_local, n.address, n.enabled, n.status, n.last_seen_at, n.last_sync_at,"
                " n.last_error, (SELECT COUNT(*) FROM forward_queue q WHERE q.peer_node = n.node_id AND q.state = 'QUEUED')"
                " AS waiting FROM nodes n ORDER BY n.is_local DESC, n.node_id").fetchall()
            return {"nodes": [dict(r) for r in rows]}

    def admin_health(self, who: Principal, full_check: bool = False) -> dict:
        self._need_admin(who)
        p = self.s.paths
        with self.db.session() as conn:
            c = conn.execute(
                "SELECT (SELECT COUNT(*) FROM users WHERE status='PENDING') AS pending,"
                " (SELECT COUNT(*) FROM users WHERE status='ACTIVE') AS active,"
                " (SELECT COUNT(*) FROM users WHERE status='SUSPENDED') AS suspended,"
                " (SELECT COUNT(*) FROM messages WHERE status='VISIBLE') AS bulletins,"
                " (SELECT COUNT(*) FROM messages WHERE status='HIDDEN') AS hidden,"
                " (SELECT COUNT(*) FROM private_messages) AS private,"
                " (SELECT COUNT(*) FROM forward_queue WHERE state = 'QUEUED') AS queue,"
                " (SELECT COUNT(*) FROM forward_queue WHERE state = 'FAILED') AS failed"
            ).fetchone()
            live = conn.execute(
                "SELECT kind, COUNT(*) AS n FROM sessions WHERE ended_at IS NULL AND last_seen_at >= ? AND expires_at > ? GROUP BY kind",
                (ts_in(minutes=-15), ts()),
            ).fetchall()
            page_count = conn.execute("PRAGMA page_count").fetchone()[0]
            page_size = conn.execute("PRAGMA page_size").fetchone()[0]
            freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
            check = integrity_check(conn, quick=not full_check)
            version = schema_version(conn)

        def disk(path: Path) -> dict:
            try:
                u = shutil.disk_usage(path)
                return {"total_mb": u.total // 2**20, "free_mb": u.free // 2**20, "used_pct": round(100 * u.used / u.total, 1)}
            except OSError:
                return {"error": "unavailable"}

        def meminfo() -> dict:
            try:
                info = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines() if ":" in line)
                kb = lambda k: int(info[k].strip().split()[0]) // 1024  # noqa: E731
                return {"total_mb": kb("MemTotal"), "available_mb": kb("MemAvailable")}
            except (OSError, KeyError, ValueError):
                return {}

        wal = p.db_path.with_name(p.db_path.name + "-wal")
        try:
            host_uptime = int(float(Path("/proc/uptime").read_text().split()[0]))
        except (OSError, ValueError):
            host_uptime = None
        return {
            "counts": dict(c),
            "sessions_active_15m": {r["kind"]: r["n"] for r in live},
            "database": {
                "path": str(p.db_path), "size_mb": round(page_count * page_size / 2**20, 2),
                "wal_mb": round(wal.stat().st_size / 2**20, 2) if wal.exists() else 0.0,
                "free_pages": freelist, "schema_version": version,
                ("integrity_check" if full_check else "quick_check"): check,
            },
            "disk": {"data": disk(p.db_path.parent), "backups": disk(p.backup_dir)},
            "memory": meminfo(),
            "core_uptime_s": int(time.time() - STARTED),
            "host_uptime_s": host_uptime,
            "load_avg": list(os.getloadavg()),
            "software": {"version": os.environ.get("BBS_VERSION", "dev"), "update": self._update_status()},
        }

    @staticmethod
    def _update_status() -> dict:
        """What the host's weekly update check found (state/update.json, read-only mount)."""
        f = Path(os.environ.get("BBS_STATE_DIR", "/etc/bbs-state")) / "update.json"
        try:
            data = json.loads(f.read_text()[:4096])
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {k: sanitize_line(str(data.get(k) or ""), 80) for k in ("checked_at", "current", "latest", "state", "message")}

    def admin_backup_status(self, who: Principal) -> dict:
        self._need_admin(who)
        bdir = self.s.paths.backup_dir
        status: dict[str, Any] = {}
        sf = bdir / "status.json"
        if sf.exists():
            try:
                status = json.loads(sf.read_text())
            except (OSError, ValueError):
                status = {"error": "status.json unreadable"}
        files = []
        if bdir.is_dir():
            for f in sorted(bdir.glob("bbs-*.sqlite3.gz"), reverse=True)[:14]:
                st = f.stat()
                files.append({"name": f.name, "size_kb": st.st_size // 1024, "sha256_file": f.with_suffix(f.suffix + ".sha256").exists()})
        return {"status": status, "recent_files": files, "backup_dir": str(bdir)}

    def admin_config(self, who: Principal) -> dict:
        self._need_admin(who)
        return self.s.public_dict()

    # --- bootstrap (CLI only, never over the API) -----------------------------------

    def bootstrap_admin(self, username: str, callsign: str | None, display_name: str | None, public_key: str) -> dict:
        username = validate_username(username)
        callsign = validate_callsign(callsign)
        key = parse_public_key(public_key)
        now = ts()
        with self.db.session() as conn, transaction(conn):
            if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
                raise BBSError("conflict", "that username already exists")
            if callsign and conn.execute("SELECT 1 FROM users WHERE callsign = ? AND status != 'REJECTED'", (callsign,)).fetchone():
                raise BBSError("conflict", "that callsign already exists")
            if conn.execute("SELECT 1 FROM ssh_keys WHERE fingerprint = ?", (key.fingerprint,)).fetchone():
                raise BBSError("conflict", "that SSH key is already registered")
            cur = conn.execute(
                "INSERT INTO users(username, callsign, callsign_verified, display_name, role, status, created_at, updated_at, approved_at)"
                " VALUES (?,?,?,?, 'admin', 'ACTIVE', ?, ?, ?)",
                (username, callsign, int(bool(callsign)), sanitize_line(display_name or "", 40) or None, now, now, now),
            )
            conn.execute(
                "INSERT INTO ssh_keys(user_id, key_type, key_data, fingerprint, comment, created_at) VALUES (?,?,?,?,?,?)",
                (cur.lastrowid, key.key_type, key.key_data, key.fingerprint, key.comment, now),
            )
            self.audit(conn, None, "admin_bootstrap", "success", "user", cur.lastrowid,
                       {"username": username, "fingerprint": key.fingerprint})
        return {"id": cur.lastrowid, "username": username, "fingerprint": key.fingerprint}

    def admin_count(self) -> int:
        with self.db.session() as conn:
            return conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND status = 'ACTIVE'").fetchone()[0]

    # --- housekeeping --------------------------------------------------------------

    def prune(self) -> dict:
        r = self.cfg.retention
        with self.db.session() as conn, transaction(conn):
            out = {}
            out["sessions"] = conn.execute(
                "DELETE FROM sessions WHERE (ended_at IS NOT NULL AND ended_at < ?) OR expires_at < ?",
                (ts_in(days=-r.session_days), ts_in(days=-r.session_days)),
            ).rowcount
            if r.audit_days:
                out["audit"] = conn.execute("DELETE FROM audit_log WHERE ts < ?", (ts_in(days=-r.audit_days),)).rowcount
            old_rejected = [row[0] for row in conn.execute(
                "SELECT id FROM users WHERE status = 'REJECTED' AND updated_at < ?", (ts_in(days=-r.rejected_registration_days),))]
            for uid in old_rejected:
                conn.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
                conn.execute("DELETE FROM users WHERE id = ? AND NOT EXISTS (SELECT 1 FROM messages WHERE author_id = ?)", (uid, uid))
            out["rejected_users"] = len(old_rejected)
            out["forward_queue"] = conn.execute(
                "DELETE FROM forward_queue WHERE (state IN ('ACKED', 'EXPIRED') AND updated_at < ?)"
                " OR (state = 'FAILED' AND updated_at < ?)", (ts_in(days=-7), ts_in(days=-30))).rowcount
            out["sync_seen"] = conn.execute("DELETE FROM sync_seen WHERE seen_at < ?",
                                            (ts_in(days=-self.cfg.mesh.seen_days),)).rowcount
            out["mail_undelivered"] = conn.execute(
                "UPDATE private_messages SET delivery_state = 'FAILED' WHERE delivery_state = 'QUEUED' AND created_at < ?",
                (ts_in(days=-14),)).rowcount
        return out
