"""Internal versioned API (v1). Only reachable on the internal Docker networks.

Every request carries two credentials:
  X-Service-Token  - which gateway is calling (ssh, admin or sync); one token per gateway.
  Authorization    - 'Bearer <session token>' for the person using that gateway.
bbs-core decides what the person may do; gateways are never trusted for identity or rights.

The sync token opens only the /v1/sync/* routes (node-to-node mesh); it cannot create
or use user sessions.
"""


import logging
import time
from typing import Annotated, Any, Literal

from fastapi import Body, Depends, FastAPI, Header, Query, Request
from fastapi import Path as FPath
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .config import NODE_ID_PATTERN
from .errors import BBSError
from .logs import event
from .security import match_service
from .service import BBS, Principal

log = logging.getLogger("bbs-core")
MAX_BODY = 64 * 1024
MAX_REQUEST = 160 * 1024  # a sync envelope can carry a full-size message plus its metadata


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GuestSessionIn(_In):
    client_ip: str | None = Field(default=None, max_length=64)
    transport: Literal["ssh", "radio"] = "ssh"


class KeyIn(_In):
    username: str = Field(max_length=64)
    public_key: str = Field(max_length=16384)
    client_ip: str | None = Field(default=None, max_length=64)


class PasswordLoginIn(_In):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)
    client_ip: str | None = Field(default=None, max_length=64)


class RegistrationIn(_In):
    username: str = Field(max_length=64)
    callsign: str | None = Field(default=None, max_length=16)
    display_name: str | None = Field(default=None, max_length=80)
    contact: str | None = Field(default=None, max_length=200)
    public_key: str | None = Field(default=None, max_length=16384)   # captured from the SSH login, with consent
    password: str | None = Field(default=None, max_length=256)       # for devices without a key


class ProfileIn(_In):
    display_name: str | None = Field(default=None, max_length=80)
    contact: str | None = Field(default=None, max_length=200)
    callsign: str | None = Field(default=None, max_length=16)


class PasswordIn(_In):
    password: str | None = Field(default=None, max_length=256)        # null removes the password


class PostIn(_In):
    board: str = Field(max_length=32)
    subject: str = Field(max_length=400)
    body: str = Field(max_length=MAX_BODY)
    reply_to: int | None = None
    priority: Literal["ROUTINE", "PRIORITY", "EMERGENCY"] | None = None
    area: str | None = Field(default=None, max_length=80)
    expires_hours: int | None = Field(default=None, ge=1, le=8760)


class MailIn(_In):
    to: str = Field(max_length=64)
    subject: str = Field(max_length=400)
    body: str = Field(max_length=MAX_BODY)
    reply_to: int | None = None


class MailStateIn(_In):
    state: Literal["ARCHIVED", "DELETED", "ACTIVE"]


class AddKeyIn(_In):
    public_key: str = Field(max_length=16384)


class StatusIn(_In):
    action: Literal["approve", "reject", "suspend", "reactivate"]
    reason: str | None = Field(default=None, max_length=400)
    callsign_verified: bool | None = None


class VerifyIn(_In):
    verified: bool


class ModerateIn(_In):
    action: Literal["hide", "restore", "delete"]
    reason: str | None = Field(default=None, max_length=400)


class SyncResultsIn(_In):
    acked: list[int] = Field(default_factory=list, max_length=1000)
    failed: list[dict] = Field(default_factory=list, max_length=1000)


class PeerStatusIn(_In):
    state: Literal["CONNECTED", "UNREACHABLE", "REFUSED", "ERROR"]
    error: str | None = Field(default=None, max_length=400)


class BoardIn(_In):
    action: Literal["create", "rename", "archive", "unarchive", "describe"]
    name: str = Field(max_length=32)
    new_name: str | None = Field(default=None, max_length=32)
    description: str | None = Field(default=None, max_length=200)
    post: Literal["users", "admins"] | None = None
    public: bool | None = True


def create_app(bbs: BBS) -> FastAPI:
    app = FastAPI(
        title="BeaconNet internal API", version="1.0.0",
        docs_url=None, redoc_url=None, openapi_url=None,  # schema is exported by the CLI, not served
    )
    app.state.bbs = bbs

    @app.exception_handler(BBSError)
    async def _bbs_error(_: Request, exc: BBSError) -> JSONResponse:
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(x) for x in first.get("loc", []) if x != "body")
        return JSONResponse({"error": {"code": "invalid_input", "message": f"{where}: {first.get('msg', 'invalid')}"}},
                            status_code=422)

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_error")
        return JSONResponse({"error": {"code": "internal", "message": "internal error"}}, status_code=500)

    @app.middleware("http")
    async def _limits_and_log(request: Request, call_next):  # type: ignore[no-untyped-def]
        started = time.monotonic()
        length = request.headers.get("content-length")
        if length and (not length.isdigit() or int(length) > MAX_REQUEST):
            return JSONResponse({"error": {"code": "too_large", "message": "request too large"}}, status_code=413)
        response = await call_next(request)
        route = request.scope.get("route")
        event(log, "api_request", logging.DEBUG, method=request.method,
              path=getattr(route, "path", "?"), status=response.status_code,
              ms=round((time.monotonic() - started) * 1000, 1))
        return response

    # --- dependencies --------------------------------------------------------------

    def any_service(x_service_token: Annotated[str | None, Header()] = None) -> str:
        svc = match_service(x_service_token, bbs.s.service_tokens)
        if svc is None:
            raise BBSError("unauthenticated", "service token missing or wrong")
        return svc

    def service(svc: Annotated[str, Depends(any_service)]) -> str:
        if svc == "sync":
            raise BBSError("forbidden", "the sync service has no access to user sessions")
        return svc

    def sync_service(svc: Annotated[str, Depends(any_service)]) -> str:
        if svc != "sync":
            raise BBSError("forbidden", "only the sync service may use this API")
        return svc

    def admin_service(svc: Annotated[str, Depends(service)]) -> str:
        # The admin gateway, or the public gateway for an administrator connecting from
        # an allowed LAN network (Principal.is_admin decides; every route checks it).
        if svc not in ("admin", "ssh"):
            raise BBSError("forbidden", "administrator API is only open to the admin gateway")
        return svc

    def principal(svc: Annotated[str, Depends(service)], authorization: Annotated[str | None, Header()] = None) -> Principal:
        token = None
        if authorization and authorization.startswith("Bearer "):
            token = authorization[7:].strip()
        return bbs.principal(token, svc)

    def admin(svc: Annotated[str, Depends(admin_service)], who: Annotated[Principal, Depends(principal)]) -> Principal:
        if not who.is_admin:
            bbs.audit_now(who, "admin_access", "denied")
            raise BBSError("forbidden", "administrator access only")
        return who

    Svc = Annotated[str, Depends(service)]
    Sync = Annotated[str, Depends(sync_service)]
    PeerId = Annotated[str, FPath(pattern=NODE_ID_PATTERN)]
    Who = Annotated[Principal, Depends(principal)]
    Admin = Annotated[Principal, Depends(admin)]

    # --- health (no credentials; internal network only, reveals nothing) -------------

    @app.get("/v1/health")
    def health() -> dict:
        return bbs.health()

    # --- sessions ------------------------------------------------------------------

    @app.post("/v1/sessions/guest")
    def guest_session(body: GuestSessionIn, svc: Svc) -> dict:
        return bbs.create_guest_session(svc, body.client_ip, body.transport)

    @app.post("/v1/auth/ssh-key/check")
    def check_key(body: KeyIn, svc: Svc) -> dict:
        return {"accepted": bbs.check_key(svc, body.username, body.public_key, body.client_ip)}

    @app.post("/v1/sessions/ssh-key")
    def key_session(body: KeyIn, svc: Svc) -> dict:
        return bbs.create_key_session(svc, body.username, body.public_key, body.client_ip)

    @app.post("/v1/auth/password/check")
    def check_password(body: PasswordLoginIn, svc: Svc) -> dict:
        return {"accepted": bbs.check_password(svc, body.username, body.password, body.client_ip)}

    @app.post("/v1/sessions/password")
    def password_session(body: PasswordLoginIn, svc: Svc) -> dict:
        return bbs.create_password_session(svc, body.username, body.password, body.client_ip)

    @app.get("/v1/sessions/me")
    def me(who: Who) -> dict:
        return bbs.me(who)

    @app.delete("/v1/sessions/me")
    def logout(who: Who) -> dict:
        bbs.end_session(who)
        return {"ok": True}

    # --- public --------------------------------------------------------------------

    @app.get("/v1/system/info")
    def system_info(who: Who) -> dict:
        return bbs.system_info()

    @app.post("/v1/registrations")
    def register(body: RegistrationIn, who: Who) -> dict:
        return bbs.register(who, body.model_dump())

    @app.get("/v1/boards")
    def boards(who: Who) -> dict:
        return {"boards": bbs.list_boards(who)}

    @app.get("/v1/messages")
    def messages(who: Who, board: str | None = None, before: int | None = None,
                 limit: Annotated[int, Query(ge=1, le=50)] = 15) -> dict:
        return bbs.list_messages(who, board, before, limit)

    @app.get("/v1/feed")
    def feed(who: Who, after: Annotated[int | None, Query(ge=0)] = None, limit: Annotated[int, Query(ge=1, le=100)] = 50) -> dict:
        return bbs.feed(who, after, limit)

    @app.get("/v1/messages/search")
    def search(who: Who, number: int | None = None, board: str | None = Query(None, max_length=32),
               sender: str | None = Query(None, max_length=40), subject: str | None = Query(None, max_length=80),
               text: str | None = Query(None, max_length=80), limit: Annotated[int, Query(ge=1, le=50)] = 25) -> dict:
        return bbs.search(who, {"number": number, "board": board, "sender": sender, "subject": subject,
                                "text": text, "limit": limit})

    @app.get("/v1/messages/{number}")
    def message(number: int, who: Who) -> dict:
        return bbs.get_message(who, number)

    @app.post("/v1/messages/{number}/read")
    def mark_read(number: int, who: Who) -> dict:
        bbs.mark_read(who, number)
        return {"ok": True}

    @app.post("/v1/messages")
    def post(body: PostIn, who: Who) -> dict:
        return bbs.post(who, body.model_dump())

    # --- private mail --------------------------------------------------------------

    @app.get("/v1/mail")
    def mail_list(who: Who, folder: Literal["inbox", "sent", "archive"] = "inbox", before: int | None = None,
                  limit: Annotated[int, Query(ge=1, le=50)] = 15) -> dict:
        return bbs.mail_list(who, folder, before, limit)

    @app.get("/v1/mail/{pm_id}")
    def mail_get(pm_id: int, who: Who) -> dict:
        return bbs.mail_get(who, pm_id)

    @app.post("/v1/mail")
    def mail_send(body: MailIn, who: Who) -> dict:
        return bbs.mail_send(who, body.model_dump())

    @app.post("/v1/mail/{pm_id}/state")
    def mail_state(pm_id: int, body: MailStateIn, who: Who) -> dict:
        bbs.mail_set_state(who, pm_id, body.state)
        return {"ok": True}

    # --- own account ---------------------------------------------------------------

    @app.get("/v1/account/keys")
    def my_keys(who: Who) -> dict:
        return {"keys": bbs.my_keys(who)}

    @app.post("/v1/account/keys")
    def add_key(body: AddKeyIn, who: Who) -> dict:
        return bbs.add_my_key(who, body.public_key)

    @app.post("/v1/account/profile")
    def update_profile(body: ProfileIn, who: Who) -> dict:
        return bbs.update_my_profile(who, body.model_dump(exclude_unset=True))

    @app.post("/v1/account/password")
    def set_password(body: PasswordIn, who: Who) -> dict:
        return bbs.set_my_password(who, body.password)

    @app.delete("/v1/account/keys/{key_id}")
    def revoke_key(key_id: int, who: Who) -> dict:
        bbs.revoke_my_key(who, key_id)
        return {"ok": True}

    # --- administration (admin gateway + admin session only) --------------------------

    @app.get("/v1/admin/users")
    def admin_users(who: Admin, status: Literal["PENDING", "ACTIVE", "SUSPENDED", "REJECTED"] | None = None,
                    offset: int = 0, limit: Annotated[int, Query(ge=1, le=50)] = 20) -> dict:
        return bbs.admin_users(who, status, offset, limit)

    @app.get("/v1/admin/users/{user_id}")
    def admin_user(user_id: int, who: Admin) -> dict:
        return bbs.admin_user(who, user_id)

    @app.post("/v1/admin/users/{user_id}/status")
    def admin_status(user_id: int, body: StatusIn, who: Admin) -> dict:
        return bbs.admin_set_status(who, user_id, body.action, body.reason, body.callsign_verified)

    @app.post("/v1/admin/users/{user_id}/callsign")
    def admin_callsign(user_id: int, body: VerifyIn, who: Admin) -> dict:
        bbs.admin_verify_callsign(who, user_id, body.verified)
        return {"ok": True}

    @app.delete("/v1/admin/keys/{key_id}")
    def admin_revoke(key_id: int, who: Admin) -> dict:
        bbs.admin_revoke_key(who, key_id)
        return {"ok": True}

    @app.get("/v1/admin/messages")
    def admin_messages(who: Admin, board: str | None = Query(None, max_length=32),
                       status: Literal["VISIBLE", "HIDDEN", "DELETED"] | None = None,
                       before: int | None = None, limit: Annotated[int, Query(ge=1, le=50)] = 20) -> dict:
        return bbs.admin_messages(who, board, status, before, limit)

    @app.post("/v1/admin/messages/{number}/moderate")
    def admin_moderate(number: int, body: ModerateIn, who: Admin) -> dict:
        return bbs.admin_moderate(who, number, body.action, body.reason)

    @app.post("/v1/admin/boards")
    def admin_board(body: BoardIn, who: Admin) -> dict:
        return bbs.admin_board(who, body.action, body.model_dump())

    @app.get("/v1/admin/audit")
    def admin_audit(who: Admin, before: int | None = None, limit: Annotated[int, Query(ge=1, le=100)] = 20) -> dict:
        return bbs.admin_audit(who, before, limit)

    @app.get("/v1/admin/queues")
    def admin_queues(who: Admin) -> dict:
        return bbs.admin_queues(who)

    @app.get("/v1/admin/nodes")
    def admin_nodes(who: Admin) -> dict:
        return bbs.admin_nodes(who)

    @app.get("/v1/admin/health")
    def admin_health(who: Admin, full: bool = False) -> dict:
        return bbs.admin_health(who, full)

    @app.get("/v1/admin/backups")
    def admin_backups(who: Admin) -> dict:
        return bbs.admin_backup_status(who)

    @app.get("/v1/admin/config")
    def admin_config(who: Admin) -> dict:
        return bbs.admin_config(who)

    # --- node-to-node mesh (sync service only) ---------------------------------------

    @app.get("/v1/sync/config")
    def sync_config(svc: Sync) -> dict:
        return bbs.mesh.sync_config()

    @app.get("/v1/sync/outbox/{peer}")
    def sync_outbox(peer: PeerId, svc: Sync, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> dict:
        return bbs.mesh.outbox(peer, limit)

    @app.post("/v1/sync/outbox/{peer}/results")
    def sync_results(peer: PeerId, body: SyncResultsIn, svc: Sync) -> dict:
        failed = [{"qid": int(f["qid"]), "error": str(f.get("error", ""))[:200]} for f in body.failed
                  if isinstance(f.get("qid"), int)]
        return bbs.mesh.results(peer, body.acked, failed)

    @app.post("/v1/sync/inbox/{peer}")
    def sync_inbox(peer: PeerId, svc: Sync, envelope: Annotated[dict, Body()]) -> dict:
        return bbs.mesh.receive(peer, envelope)

    @app.post("/v1/sync/peers/{peer}/status")
    def sync_peer_status(peer: PeerId, body: PeerStatusIn, svc: Sync) -> dict:
        return bbs.mesh.peer_status(peer, body.state, body.error)

    return app


def openapi_schema(bbs: BBS) -> dict[str, Any]:
    from fastapi.openapi.utils import get_openapi

    app = create_app(bbs)
    return get_openapi(title=app.title, version=app.version, routes=app.routes,
                       description=__doc__)
