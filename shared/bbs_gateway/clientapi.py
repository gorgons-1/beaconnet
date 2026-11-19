"""'beaconnet-client' SSH subsystem: a small JSON-lines API for desktop clients.

The client logs in exactly like a person (key or password), then sends one JSON object
per line: {"id": 1, "op": "feed", "after": 120}. Each reply is one line:
{"id": 1, "ok": true, "data": {...}} or {"id": 1, "ok": false, "error": {"code", "message"}}.

Only the operations below exist. Each is mapped to one bbs-core route with the person's
own session, so bbs-core applies exactly the same rights as in the menus (admin
operations only work for an administrator on an allowed LAN network).
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from .client import ApiError, Session

PROTOCOL = 1
MAX_LINE = 256 * 1024


class BadRequest(Exception):
    pass


def _int(req: dict, key: str, default: int | None = None, lo: int = 0, hi: int = 2**31) -> int:
    v = req.get(key, default)
    if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
        raise BadRequest(f"{key} must be a whole number")
    return v


def _str(req: dict, key: str, max_len: int, required: bool = True) -> str | None:
    v = req.get(key)
    if v is None and not required:
        return None
    if not isinstance(v, str) or len(v) > max_len or (required and not v):
        raise BadRequest(f"{key} must be text (up to {max_len} characters)")
    return v


async def _hello(s: Session, r: dict) -> Any:
    return {"protocol": PROTOCOL, "me": await s.get("/v1/sessions/me"), "info": await s.get("/v1/system/info")}


async def _message(s: Session, r: dict) -> Any:
    n = _int(r, "number", lo=1)
    m = await s.get(f"/v1/messages/{n}")
    try:
        await s.post(f"/v1/messages/{n}/read")
    except ApiError:
        pass
    return m


async def _post(s: Session, r: dict) -> Any:
    body: dict[str, Any] = {"board": _str(r, "board", 32), "subject": _str(r, "subject", 400), "body": _str(r, "body", 65536)}
    for k, n in (("priority", 16), ("area", 80)):
        if r.get(k) is not None:
            body[k] = _str(r, k, n)
    if r.get("reply_to") is not None:
        body["reply_to"] = _int(r, "reply_to", lo=1)
    if r.get("expires_hours") is not None:
        body["expires_hours"] = _int(r, "expires_hours", lo=1, hi=8760)
    return await s.post("/v1/messages", body)


async def _mail_send(s: Session, r: dict) -> Any:
    body: dict[str, Any] = {"to": _str(r, "to", 64), "subject": _str(r, "subject", 400), "body": _str(r, "body", 65536)}
    if r.get("reply_to") is not None:
        body["reply_to"] = _int(r, "reply_to", lo=1)
    return await s.post("/v1/mail", body)


async def _admin_status(s: Session, r: dict) -> Any:
    action = _str(r, "action", 16)
    if action not in ("approve", "reject", "suspend", "reactivate"):
        raise BadRequest("action must be approve, reject, suspend or reactivate")
    body: dict[str, Any] = {"action": action}
    if r.get("reason") is not None:
        body["reason"] = _str(r, "reason", 400)
    if isinstance(r.get("callsign_verified"), bool):
        body["callsign_verified"] = r["callsign_verified"]
    return await s.post(f"/v1/admin/users/{_int(r, 'user_id', lo=1)}/status", body)


def _folder(r: dict) -> str:
    f = r.get("folder", "inbox")
    if f not in ("inbox", "sent", "archive"):
        raise BadRequest("folder must be inbox, sent or archive")
    return f


def _status(r: dict) -> str | None:
    st = r.get("status", "PENDING")
    if st not in ("PENDING", "ACTIVE", "SUSPENDED", "REJECTED"):
        raise BadRequest("status must be PENDING, ACTIVE, SUSPENDED or REJECTED")
    return st


OPS: dict[str, Callable[[Session, dict], Awaitable[Any]]] = {
    "hello": _hello,
    "feed": lambda s, r: s.get("/v1/feed", after=None if r.get("after") is None else _int(r, "after"),
                               limit=_int(r, "limit", 50, 1, 100)),
    "boards": lambda s, r: s.get("/v1/boards"),
    "messages": lambda s, r: s.get("/v1/messages", board=_str(r, "board", 32, required=False),
                                   before=r.get("before") and _int(r, "before", lo=1), limit=_int(r, "limit", 20, 1, 50)),
    "message": _message,
    "post": _post,
    "mail": lambda s, r: s.get("/v1/mail", folder=_folder(r), limit=_int(r, "limit", 20, 1, 50)),
    "mail_get": lambda s, r: s.get(f"/v1/mail/{_int(r, 'mail_id', lo=1)}"),
    "mail_send": _mail_send,
    "add_key": lambda s, r: s.post("/v1/account/keys", {"public_key": _str(r, "public_key", 16384)}),
    "admin_users": lambda s, r: s.get("/v1/admin/users", status=_status(r), limit=_int(r, "limit", 50, 1, 50)),
    "admin_user": lambda s, r: s.get(f"/v1/admin/users/{_int(r, 'user_id', lo=1)}"),
    "admin_status": _admin_status,
}


async def serve(process, s: Session) -> None:  # type: ignore[no-untyped-def]
    """Answer requests until the client closes the channel."""
    def send(obj: dict) -> None:
        process.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")

    while True:
        line = await process.stdin.readline()
        if not line:
            return
        if len(line) > MAX_LINE:
            send({"id": None, "ok": False, "error": {"code": "too_large", "message": "request too large"}})
            return
        rid = None
        try:
            req = json.loads(line)
            if not isinstance(req, dict):
                raise BadRequest("each request must be a JSON object")
            rid = req.get("id") if isinstance(req.get("id"), (int, str)) else None
            op = OPS.get(req.get("op"))  # type: ignore[arg-type]
            if op is None:
                raise BadRequest("unknown op")
            send({"id": rid, "ok": True, "data": await op(s, req)})
        except (ValueError, BadRequest) as exc:
            send({"id": rid, "ok": False, "error": {"code": "bad_request", "message": str(exc)[:200]}})
        except ApiError as exc:
            send({"id": rid, "ok": False, "error": {"code": exc.code, "message": exc.message}})
            if exc.status == 401:
                return  # session over: the client reconnects
