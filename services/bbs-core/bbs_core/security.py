"""Tokens, SSH public keys, input validation and text sanitising."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
import unicodedata
from collections import deque
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.hazmat.primitives.serialization import load_ssh_public_key

from .errors import BBSError

# --- tokens ------------------------------------------------------------------------


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def match_service(presented: str | None, tokens: dict[str, str]) -> str | None:
    """Return the service whose token was presented, comparing in constant time."""
    if not presented:
        return None
    found = None
    for service, token in tokens.items():
        if hmac.compare_digest(presented.encode(), token.encode()):
            found = service
    return found


# --- SSH public keys ---------------------------------------------------------------

ALLOWED_KEY_TYPES = {
    "ssh-ed25519",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "ssh-rsa",
}
MIN_RSA_BITS = 2048


@dataclass(frozen=True)
class PublicKey:
    key_type: str
    key_data: str  # base64 blob
    fingerprint: str
    comment: str

    @property
    def openssh(self) -> str:
        return f"{self.key_type} {self.key_data}"


def parse_public_key(line: str) -> PublicKey:
    """Parse one OpenSSH public key line ('type base64 [comment]')."""
    line = (line or "").strip()
    if len(line) > 16384 or "\n" in line or "\r" in line:
        raise BBSError("invalid_key", "the public key must be a single line")
    parts = line.split(None, 2)
    if len(parts) < 2:
        raise BBSError("invalid_key", "expected '<type> <base64> [comment]', e.g. 'ssh-ed25519 AAAA... you@pc'")
    key_type, b64 = parts[0], parts[1]
    comment = sanitize_line(parts[2], 60) if len(parts) > 2 else ""
    if key_type not in ALLOWED_KEY_TYPES:
        raise BBSError("invalid_key", f"key type {key_type[:30]!r} not accepted; use ssh-ed25519, ECDSA or RSA >= {MIN_RSA_BITS}")
    try:
        blob = base64.b64decode(b64, validate=True)
        key = load_ssh_public_key(f"{key_type} {b64}".encode())
    except Exception as exc:  # cryptography raises several types for bad keys
        raise BBSError("invalid_key", "the public key could not be decoded") from exc
    if isinstance(key, rsa.RSAPublicKey):
        if key.key_size < MIN_RSA_BITS:
            raise BBSError("invalid_key", f"RSA keys must be at least {MIN_RSA_BITS} bits")
    elif not isinstance(key, (ed25519.Ed25519PublicKey, ec.EllipticCurvePublicKey)):
        raise BBSError("invalid_key", "unsupported key algorithm")
    fp = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return PublicKey(key_type=key_type, key_data=b64, fingerprint=fp, comment=comment)


# --- text --------------------------------------------------------------------------

# Every C0/C1 control character except tab and newline. Removing ESC (0x1b) and CSI
# (0x9b) means nothing a user writes can move another user's cursor, retitle their
# terminal or hide text.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _clean(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL.sub("", text)
    # Unicode format characters (bidi overrides etc.) can disguise text.
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


def sanitize_line(text: str, max_len: int) -> str:
    text = _clean(text or "").replace("\n", " ").replace("\t", " ").strip()
    return text[:max_len]


def sanitize_body(text: str, max_bytes: int) -> str:
    text = _clean(text or "").replace("\t", "    ")
    lines = [ln.rstrip() for ln in text.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    body = "\n".join(lines)
    if len(body.encode("utf-8")) > max_bytes:
        raise BBSError("too_long", f"message body is over {max_bytes} bytes")
    return body


USERNAME_RE = re.compile(r"^[a-z][a-z0-9_-]{2,19}$")
# Amateur callsigns: optional 1-2 char prefix + digit + 1-4 char suffix, e.g. GM4ABC, W1AW, VK2XYZ,
# 2M0ABC, GS3XYZ. Stored upper case; an SSID ('-7') is a station, not a person, so not allowed.
CALLSIGN_RE = re.compile(r"^(?=.{3,8}$)[A-Z0-9]{1,3}[0-9][A-Z]{1,4}$")
RESERVED_NAMES = {
    "guest", "new", "register", "bbs", "admin", "administrator", "root", "sysop", "system",
    "operator", "postmaster", "support", "info", "all", "everyone", "nobody", "daemon",
    "ssh", "radio", "sync", "node", "help", "anonymous", "user", "users",
}


def validate_username(name: str) -> str:
    name = (name or "").strip().lower()
    if not USERNAME_RE.match(name):
        raise BBSError("invalid_username", "username: 3-20 characters, a-z 0-9 _ -, starting with a letter")
    if name in RESERVED_NAMES:
        raise BBSError("invalid_username", "that username is reserved")
    return name


def looks_like_callsign(name: str) -> bool:
    return bool(CALLSIGN_RE.match((name or "").upper()))


def validate_callsign(call: str | None) -> str | None:
    call = (call or "").strip().upper()
    if not call:
        return None
    if not CALLSIGN_RE.match(call):
        raise BBSError("invalid_callsign", "that doesn't look like an amateur callsign (e.g. GM4ABC, W1AW, VK2XYZ)")
    return call


# --- rate limiting -----------------------------------------------------------------


class RateLimiter:
    """Sliding-window counters kept in memory (reset when bbs-core restarts)."""

    def __init__(self) -> None:
        self._hits: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.Lock()

    def hit(self, action: str, key: str, limit: int, window_s: float) -> bool:
        """Record an attempt. Returns False (and records nothing) once the limit is reached."""
        now = time.monotonic()
        with self._lock:
            q = self._hits.setdefault((action, key), deque())
            while q and q[0] <= now - window_s:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            if len(self._hits) > 50000:  # bound memory under a flood of distinct keys
                self._hits = {k: v for k, v in self._hits.items() if v and v[-1] > now - 3600}
            return True

    def check(self, action: str, key: str, limit: int, window_s: float) -> None:
        if not self.hit(action, key, limit, window_s):
            raise BBSError("rate_limited", "too many attempts; please wait and try again", status=429)


# --- passwords (fallback for devices without an SSH key) ------------------------------

PASSWORD_MIN = 8
PASSWORD_MAX = 128
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def validate_password(password: str | None) -> str:
    if password is None or not (PASSWORD_MIN <= len(password) <= PASSWORD_MAX):
        raise BBSError("invalid_password", f"the password must be {PASSWORD_MIN}-{PASSWORD_MAX} characters")
    if password.strip() != password or any(ord(ch) < 32 for ch in password):
        raise BBSError("invalid_password", "the password can't start or end with a space or contain control characters")
    return password


def hash_password(password: str) -> str:
    """scrypt (stdlib, memory-hard), stored as scrypt$n$r$p$salt$hash."""
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, dklen=32, **_SCRYPT)
    enc = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${enc(salt)}${enc(dk)}"


_DUMMY_HASH: str | None = None


def verify_password(password: str, stored: str | None) -> bool:
    """Constant-ish time: an account without a password still costs one scrypt."""
    global _DUMMY_HASH
    if not stored:
        if _DUMMY_HASH is None:
            _DUMMY_HASH = hash_password("x" * 16)
        stored, password = _DUMMY_HASH, password + "\0"
    try:
        algo, n, r, p, salt, want = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt), dklen=32,
                            n=int(n), r=int(r), p=int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, base64.b64decode(want))


def ip_in_networks(ip: str | None, networks: list[str]) -> bool:
    import ipaddress

    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return any(addr in ipaddress.ip_network(n, strict=False) for n in networks)
