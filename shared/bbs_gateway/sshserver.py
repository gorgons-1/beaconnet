"""SSH server shared by bbs-ssh (public) and bbs-admin (management).

The server only ever runs the BBS menu. There is no shell, no exec, no subsystems
(sftp/scp), no port, agent or X11 forwarding - those requests are refused.
Identity is decided by bbs-core: the gateway passes on the public key the client
proved it holds during the SSH handshake, and core says whose key it is.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

import asyncssh

from .client import ApiError, CoreAPI, Session
from .term import Disconnect, Term

log = logging.getLogger("bbs-gateway")


def log_event(name: str, level: int = logging.INFO, **fields: object) -> None:
    log.log(level, name, extra={"fields": fields})


@dataclass
class GatewayConfig:
    service: str                     # "ssh" or "admin"
    core_url: str
    token: str
    host_keys: list[str]
    port: int
    no_auth_users: dict[str, str]    # username -> mode ('guest' / 'register'); empty for admin
    max_per_ip: int = 4
    max_total: int = 64
    public_host: str = "this-node"
    public_port: int = 2222
    banner: str = ""
    login_timeout: int = 60

    @classmethod
    def from_env(cls, service: str, default_port: int, no_auth_users: dict[str, str]) -> "GatewayConfig":
        def need(var: str) -> str:
            v = os.environ.get(var)
            if not v:
                raise SystemExit(f"CONFIGURATION ERROR: {var} is not set")
            return v

        token = Path(need("BBS_GATEWAY_TOKEN_FILE")).read_text().strip()
        if len(token) < 32:
            raise SystemExit("CONFIGURATION ERROR: gateway token is shorter than 32 characters")
        keys = [k for k in need("BBS_HOST_KEY_FILES").split(",") if k.strip()]
        for k in keys:
            if not Path(k).is_file():
                raise SystemExit(f"CONFIGURATION ERROR: host key {k} not found")
        try:
            return cls(
                service=service, core_url=os.environ.get("BBS_CORE_URL", "http://bbs-core:8080"), token=token,
                host_keys=keys, port=int(os.environ.get("BBS_LISTEN_PORT", default_port)), no_auth_users=no_auth_users,
                max_per_ip=int(os.environ.get("BBS_MAX_CONN_PER_IP", "4")),
                max_total=int(os.environ.get("BBS_MAX_CONN_TOTAL", "64")),
                public_host=os.environ.get("BBS_PUBLIC_HOST", "this-node"),
                public_port=int(os.environ.get("BBS_PUBLIC_PORT", "2222")),
            )
        except ValueError as exc:
            raise SystemExit(f"CONFIGURATION ERROR: {exc}") from exc


@dataclass
class Gateway:
    cfg: GatewayConfig
    api: CoreAPI
    app: Callable[["Gateway", Term, Session, "BBSServer"], Awaitable[None]]
    register_app: Callable[["Gateway", Term, Session], Awaitable[None]] | None = None
    per_ip: Counter = field(default_factory=Counter)
    total: int = 0


class BBSServer(asyncssh.SSHServer):
    def __init__(self, gw: Gateway) -> None:
        self.gw = gw
        self.conn: asyncssh.SSHServerConnection | None = None
        self.ip = "-"
        self.over_limit = False
        self.counted = False
        self.username = ""
        self.mode = "key"            # key | guest | register
        self.proven_key: str | None = None

    # --- connection accounting ---------------------------------------------------------

    def connection_made(self, conn: asyncssh.SSHServerConnection) -> None:
        self.conn = conn
        peer = conn.get_extra_info("peername")
        self.ip = peer[0] if peer else "-"
        gw = self.gw
        if gw.total >= gw.cfg.max_total or gw.per_ip[self.ip] >= gw.cfg.max_per_ip:
            self.over_limit = True
            log_event("connection_refused", logging.WARNING, client_ip=self.ip, why="connection limit")
        else:
            gw.total += 1
            gw.per_ip[self.ip] += 1
            self.counted = True

    def connection_lost(self, exc: Exception | None) -> None:
        if self.counted:
            self.gw.total -= 1
            self.gw.per_ip[self.ip] -= 1
            if self.gw.per_ip[self.ip] <= 0:
                del self.gw.per_ip[self.ip]
            self.counted = False

    # --- authentication ----------------------------------------------------------------

    def begin_auth(self, username: str) -> bool:
        self.username = username
        if self.over_limit:
            self._banner("Too many connections - please try again later.\n")
            return True  # require auth that can never succeed
        mode = self.gw.cfg.no_auth_users.get(username.lower())
        if mode:
            self.mode = mode
            return False  # guests and new registrations need no credentials
        if self.gw.cfg.banner:
            self._banner(self.gw.cfg.banner)
        return True

    def _banner(self, text: str) -> None:
        try:
            if self.conn:
                self.conn.send_auth_banner(text)
        except Exception:  # banner is best-effort
            pass

    def password_auth_supported(self) -> bool:
        return False

    def kbdint_auth_supported(self) -> bool:
        return False

    def host_based_auth_supported(self) -> bool:
        return False

    def public_key_auth_supported(self) -> bool:
        return True

    async def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
        if self.over_limit:
            return False
        openssh = key.export_public_key("openssh").decode().strip()
        try:
            r = await self.gw.api.call("POST", "/v1/auth/ssh-key/check",
                                       json={"username": username, "public_key": openssh, "client_ip": self.ip})
        except ApiError as exc:
            log_event("auth_check_failed", logging.WARNING, client_ip=self.ip, code=exc.code)
            return False
        if r.get("accepted"):
            # asyncssh calls this for each key offered and verifies the signature after a
            # True; the key that finally authenticates is the last one accepted here.
            self.proven_key = openssh
            return True
        return False


async def _session(gw: Gateway, process: asyncssh.SSHServerProcess) -> None:
    server: BBSServer = process.channel.get_connection().get_owner()
    if process.command is not None or process.subsystem is not None:
        process.stdout.write("This system provides the BBS menu only; commands and subsystems are not available.\n")
        process.exit(1)
        log_event("exec_refused", logging.WARNING, client_ip=server.ip, user=server.username)
        return
    session: Session | None = None
    try:
        if server.mode in ("guest", "register"):
            data = await gw.api.call("POST", "/v1/sessions/guest", json={"client_ip": server.ip, "transport": "ssh"})
        else:
            if server.proven_key is None:
                raise ApiError("unauthenticated", "no key", 401)
            data = await gw.api.call("POST", "/v1/sessions/ssh-key", json={
                "username": server.username, "public_key": server.proven_key, "client_ip": server.ip})
        session = Session(gw.api, data["token"], data["kind"], int(data.get("idle_timeout_s", 600)))
        term = Term(process, idle_timeout=session.idle_timeout_s)
        log_event("session_start", kind=session.kind, client_ip=server.ip, user=server.username if session.kind != "guest" else "guest")
        if server.mode == "register" and gw.register_app is not None:
            await gw.register_app(gw, term, session)
        else:
            await gw.app(gw, term, session, server)
    except Disconnect:
        pass
    except ApiError as exc:
        try:
            process.stdout.write(f"\r\n*** {exc.message}\r\n")
        except Exception:
            pass
        log_event("session_error", logging.WARNING, client_ip=server.ip, code=exc.code)
    except Exception:
        log.exception("session_crashed")
        try:
            process.stdout.write("\r\n*** Internal error - disconnecting.\r\n")
        except Exception:
            pass
    finally:
        if session is not None:
            await session.end()
        log_event("session_end", client_ip=server.ip)
        try:
            process.exit(0)
        except Exception:
            pass


async def start(gw: Gateway, host: str = "0.0.0.0") -> asyncssh.SSHAcceptor:
    keys = [asyncssh.read_private_key(k) for k in gw.cfg.host_keys]
    return await asyncssh.create_server(
        lambda: BBSServer(gw), host, gw.cfg.port,
        server_host_keys=keys,
        process_factory=lambda p: _session(gw, p),
        encoding="utf-8",
        line_editor=True,
        line_history=20,
        max_line_length=4096,
        allow_scp=False,
        sftp_factory=None,
        agent_forwarding=False,
        x11_forwarding=False,
        allow_pty=True,
        login_timeout=gw.cfg.login_timeout,
        keepalive_interval=60,
        keepalive_count_max=3,
        server_version="BeaconNet",
    )


def run(gw_factory: Callable[[], Gateway]) -> None:
    from .logs import setup

    setup(os.environ.get("BBS_SERVICE_NAME", "bbs-gateway"), os.environ.get("BBS_LOG_LEVEL", "INFO"))

    async def main() -> None:
        gw = gw_factory()
        await start(gw)
        log_event("gateway_listening", gateway=gw.cfg.service, port=gw.cfg.port)
        await asyncio.Event().wait()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("stopped")
