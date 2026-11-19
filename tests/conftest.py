"""Shared fixtures: a real bbs-core (temp database) and real SSH gateways on free ports."""

from __future__ import annotations

import asyncio
import shutil
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in ("services/bbs-core", "shared", "services/bbs-ssh", "services/bbs-admin", "services/bbs-sync"):
    sys.path.insert(0, str(ROOT / p))

import asyncssh  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from bbs_core.api import create_app  # noqa: E402
from bbs_core.config import load_settings  # noqa: E402
from bbs_core.db import Database, migrate  # noqa: E402
from bbs_core.service import BBS  # noqa: E402

SSH_TOKEN = "s" * 20 + "ssh-gateway-test-token-0123456789"
ADMIN_TOKEN = "a" * 20 + "admin-gateway-test-token-0123456789"
SYNC_TOKEN = "y" * 20 + "sync-gateway-test-token-0123456789"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_env(tmp: Path, node_id: str | None = None, peers: list[dict] | None = None) -> dict[str, str]:
    import yaml

    tmp.mkdir(parents=True, exist_ok=True)
    cfg = tmp / "config"
    shutil.copytree(ROOT / "config.example", cfg)
    if peers is not None:
        (cfg / "peers.yaml").write_text(yaml.safe_dump({"peers": peers}))
    (tmp / "ssh.tok").write_text(SSH_TOKEN)
    (tmp / "admin.tok").write_text(ADMIN_TOKEN)
    (tmp / "sync.tok").write_text(SYNC_TOKEN)
    (tmp / "backups").mkdir()
    extra = {"BBS_NODE_ID": node_id} if node_id else {}
    return {**extra,
        "BBS_CONFIG_DIR": str(cfg),
        "BBS_DB_PATH": str(tmp / "data" / "bbs.sqlite3"),
        "BBS_BACKUP_DIR": str(tmp / "backups"),
        "BBS_MIGRATIONS_DIR": str(ROOT / "migrations"),
        "BBS_SSH_GATEWAY_TOKEN_FILE": str(tmp / "ssh.tok"),
        "BBS_ADMIN_GATEWAY_TOKEN_FILE": str(tmp / "admin.tok"),
        "BBS_SYNC_GATEWAY_TOKEN_FILE": str(tmp / "sync.tok"),
    }


def make_bbs(env: dict[str, str]) -> BBS:
    s = load_settings(env)
    bbs = BBS(s, Database(s.paths.db_path))
    migrate(bbs.db, s.paths.migrations_dir)
    bbs.seed()
    return bbs


def gen_key(alg: str = "ssh-ed25519") -> asyncssh.SSHKey:
    return asyncssh.generate_private_key(alg)


def pub(key: asyncssh.SSHKey, comment: str = "test") -> str:
    return key.export_public_key("openssh").decode().strip() + f" {comment}"


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return make_env(tmp_path)


@pytest.fixture
def bbs(env: dict[str, str]) -> BBS:
    return make_bbs(env)


class Api:
    """Thin helper over TestClient acting as one gateway."""

    def __init__(self, client: TestClient, token: str) -> None:
        self.c = client
        self.token = token

    def call(self, method: str, path: str, session: str | None = None, **kw):
        headers = {"X-Service-Token": self.token, **kw.pop("headers", {})}
        if session:
            headers["Authorization"] = f"Bearer {session}"
        return self.c.request(method, path, headers=headers, **kw)

    def guest(self, ip: str = "10.0.0.1") -> str:
        r = self.call("POST", "/v1/sessions/guest", json={"client_ip": ip})
        assert r.status_code == 200, r.text
        return r.json()["token"]

    def login(self, username: str, key: asyncssh.SSHKey, ip: str = "10.0.0.1"):
        return self.call("POST", "/v1/sessions/ssh-key", json={"username": username, "public_key": pub(key), "client_ip": ip})


@pytest.fixture
def client(bbs: BBS) -> TestClient:
    return TestClient(create_app(bbs), raise_server_exceptions=False)


@pytest.fixture
def ssh_api(client: TestClient) -> Api:
    return Api(client, SSH_TOKEN)


@pytest.fixture
def admin_api(client: TestClient) -> Api:
    return Api(client, ADMIN_TOKEN)


@pytest.fixture
def admin_key(bbs: BBS) -> asyncssh.SSHKey:
    key = gen_key()
    bbs.bootstrap_admin("sysop1", "GM0SYS", "Test Sysop", pub(key))
    return key


@pytest.fixture
def admin_session(admin_api: Api, admin_key) -> str:
    r = admin_api.login("sysop1", admin_key)
    assert r.status_code == 200, r.text
    return r.json()["token"]


def register(api: Api, username: str, key, callsign: str | None = None, ip: str = "10.0.0.2") -> dict:
    g = api.guest(ip)
    r = api.call("POST", "/v1/registrations", g,
                 json={"username": username, "callsign": callsign, "public_key": pub(key)})
    assert r.status_code == 200, r.text
    return r.json()


def user_id(admin_api: Api, admin_session: str, username: str) -> int:
    for status in ("PENDING", "ACTIVE", "SUSPENDED", "REJECTED"):
        r = admin_api.call("GET", "/v1/admin/users", admin_session, params={"status": status, "limit": 50})
        for u in r.json()["users"]:
            if u["username"] == username:
                return u["id"]
    raise KeyError(username)


def approve(admin_api: Api, admin_session: str, username: str) -> None:
    uid = user_id(admin_api, admin_session, username)
    r = admin_api.call("POST", f"/v1/admin/users/{uid}/status", admin_session, json={"action": "approve"})
    assert r.status_code == 200, r.text


@pytest.fixture
def active_user(ssh_api: Api, admin_api: Api, admin_session: str):
    """An approved user: returns (username, key, session token)."""
    key = gen_key()
    register(ssh_api, "alice", key, "GM4AAA")
    approve(admin_api, admin_session, "alice")
    r = ssh_api.login("alice", key)
    assert r.status_code == 200, r.text
    return "alice", key, r.json()["token"]


# --- live stack: uvicorn core + SSH gateways -----------------------------------------


class LiveCore:
    def __init__(self, bbs: BBS) -> None:
        import uvicorn

        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        config = uvicorn.Config(create_app(bbs), host="127.0.0.1", port=self.port, log_config=None, access_log=False)
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> "LiveCore":
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("core did not start")
            time.sleep(0.05)
        return self

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture
def live_core(bbs: BBS):
    core = LiveCore(bbs).start()
    yield core
    core.stop()


@pytest.fixture
async def gateways(live_core: LiveCore, tmp_path: Path):
    """Start bbs-ssh and bbs-admin servers in this event loop. Yields (ssh_port, admin_port)."""
    from bbs_admin.__main__ import menu as admin_menu
    from bbs_gateway.client import CoreAPI
    from bbs_gateway.sshserver import Gateway, GatewayConfig, start
    from bbs_ssh.__main__ import NO_AUTH_USERS
    from bbs_ssh.menus import register_only, run as ssh_menu

    hk = tmp_path / "hostkey"
    gen_key().write_private_key(str(hk))
    ssh_cfg = GatewayConfig(service="ssh", core_url=live_core.url, token=SSH_TOKEN, host_keys=[str(hk)],
                            port=free_port(), no_auth_users=NO_AUTH_USERS, public_host="testhost", public_port=2222)
    adm_cfg = GatewayConfig(service="admin", core_url=live_core.url, token=ADMIN_TOKEN, host_keys=[str(hk)],
                            port=free_port(), no_auth_users={})
    ssh_gw = Gateway(cfg=ssh_cfg, api=CoreAPI(ssh_cfg.core_url, SSH_TOKEN), app=ssh_menu, register_app=register_only,
                     client_api=True)
    adm_gw = Gateway(cfg=adm_cfg, api=CoreAPI(adm_cfg.core_url, ADMIN_TOKEN), app=admin_menu)
    s1 = await start(ssh_gw, "127.0.0.1")
    s2 = await start(adm_gw, "127.0.0.1")
    yield ssh_cfg.port, adm_cfg.port, ssh_gw
    s1.close()
    s2.close()
    await ssh_gw.api.close()
    await adm_gw.api.close()


class Term:
    """Drive a BBS session like a person at a terminal."""

    def __init__(self, conn: asyncssh.SSHClientConnection, proc: asyncssh.SSHClientProcess) -> None:
        self.conn = conn
        self.proc = proc
        self.buf = ""
        self.all = ""

    async def expect(self, text: str, timeout: float = 8.0) -> str:
        deadline = asyncio.get_running_loop().time() + timeout
        while text not in self.buf:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise AssertionError(f"timed out waiting for {text!r}; screen so far:\n{self.buf[-1500:]}")
            try:
                chunk = await asyncio.wait_for(self.proc.stdout.read(4096), timeout=remaining)
            except asyncio.TimeoutError:
                continue
            if not chunk:
                raise AssertionError(f"connection closed waiting for {text!r}; screen:\n{self.buf[-1500:]}")
            self.buf += chunk
            self.all += chunk
        idx = self.buf.index(text) + len(text)
        seen, self.buf = self.buf[:idx], self.buf[idx:]
        return seen

    def send(self, line: str) -> None:
        self.proc.stdin.write(line + "\n")

    async def close(self) -> None:
        self.conn.close()
        await self.conn.wait_closed()


async def connect(port: int, username: str, key=None, pty: bool = False, password: str | None = None) -> Term:
    conn = await asyncssh.connect(
        "127.0.0.1", port, username=username, client_keys=[key] if key else [],
        known_hosts=None, agent_path=None, password=password, kbdint_auth=False,
    )
    proc = await conn.create_process(term_type="xterm" if pty else None, encoding="utf-8")
    return Term(conn, proc)


# --- mesh: several nodes ---------------------------------------------------------------


class Node:
    """One BBS node for mesh tests: its own database, config, node key and peers."""

    def __init__(self, tmp: Path, node_id: str, key: asyncssh.SSHKey) -> None:
        self.tmp, self.id, self.key = tmp / node_id, node_id, key
        self.pub = key.export_public_key("openssh").decode().strip()
        self.peers: list[dict] = []
        self.bbs: BBS | None = None

    def link(self, other: "Node", port: int = 2224, address: str | None = "127.0.0.1", enabled: bool = True) -> None:
        self.peers.append({"id": other.id, "name": other.id.title(), "address": address, "port": port,
                           "public_key": other.pub, "enabled": enabled})

    def build(self) -> BBS:
        self.bbs = make_bbs(make_env(self.tmp, self.id, self.peers))
        return self.bbs

    def client(self) -> TestClient:
        return TestClient(create_app(self.bbs), raise_server_exceptions=False)

    def user(self, username: str, callsign: str | None = None) -> tuple[Api, str]:
        """Create an ACTIVE user (as an admin would) and return (ssh api, session)."""
        c = self.client()
        ssh, adm = Api(c, SSH_TOKEN), Api(c, ADMIN_TOKEN)
        akey = gen_key()
        if self.bbs.admin_count() == 0:
            self.bbs.bootstrap_admin("sysop1", None, None, pub(akey))
            self._admin = akey
        asess = adm.login("sysop1", self._admin).json()["token"]
        ukey = gen_key()
        register(ssh, username, ukey, callsign)
        approve(adm, asess, username)
        return ssh, ssh.login(username, ukey).json()["token"]


@pytest.fixture
def mesh(tmp_path: Path):
    """Factory: mesh("A", "B", ...) -> dict of unbuilt Nodes with fresh node keys."""
    def make(*ids: str) -> dict[str, Node]:
        return {i: Node(tmp_path, i, gen_key()) for i in ids}
    return make
