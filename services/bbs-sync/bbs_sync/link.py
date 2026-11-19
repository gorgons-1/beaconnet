"""bbs-sync: carries message envelopes between this node and its peer nodes.

Links are SSH connections. Each node has one ed25519 node key, and it uses that key
two ways:
  * as the SSH host key when peers dial in, and
  * as the client key when this node dials out.
peers.yaml pins each peer's key in both directions. That gives an encrypted link with
mutual authentication, and no passwords, certificates or CAs.

The session is one subsystem ("scotland-bbs-sync") that exchanges JSON lines:

    dialler                         listener
    hello{node}           ──►
                          ◄──       hello{node}
    env{qid, env} ...     ──►                  (dialler's queue for the listener)
                          ◄──       ack{qid,status} / nack{qid,error}
    done                  ──►
    pull                  ──►
                          ◄──       env{qid, env} ...   (listener's queue for the dialler)
    ack / nack            ──►
                          ◄──       done
    bye                   ──►

Only one side has to be reachable, so a node behind NAT dials out and still receives
its traffic. bbs-sync has no database: bbs-core does all the checks and queueing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from dataclasses import dataclass, field
from pathlib import Path

import asyncssh

from bbs_gateway.client import ApiError, CoreAPI

log = logging.getLogger("bbs-sync")

PROTO = "scotland-bbs-sync"
VERSION = 1
MAX_LINE = 256 * 1024
READ_TIMEOUT = 90
BATCH = 50
MAX_PER_SESSION = 2000


def event(name: str, level: int = logging.INFO, **fields: object) -> None:
    log.log(level, name, extra={"fields": fields})


class ProtocolError(Exception):
    pass


@dataclass
class Peer:
    id: str
    name: str
    address: str | None
    port: int
    key: asyncssh.SSHKey

    @property
    def key_line(self) -> str:
        return self.key.export_public_key("openssh").decode().split()[1]


@dataclass
class State:
    api: CoreAPI
    node_key: asyncssh.SSHKey
    node_id: str = ""
    peers: dict[str, Peer] = field(default_factory=dict)
    interval: int = 300          # dial each reachable peer at least this often (seconds)
    poll: int = 10               # check the queue this often for new traffic
    max_inbound: int = 16
    inbound: int = 0

    async def refresh(self) -> None:
        cfg = await self.api.call("GET", "/v1/sync/config")
        own = self.node_key.export_public_key("openssh").decode().split()[1]
        peers = {}
        for p in cfg["peers"]:
            key = asyncssh.import_public_key(p["public_key"])
            if key.export_public_key("openssh").decode().split()[1] == own:
                event("peer_skipped", logging.WARNING, peer=p["id"], why="peer has this node's own key")
                continue
            peers[p["id"]] = Peer(p["id"], p["name"], p.get("address"), int(p["port"]), key)
        self.node_id, self.peers = cfg["node_id"], peers


class Channel:
    def __init__(self, reader: asyncssh.SSHReader, writer: asyncssh.SSHWriter) -> None:
        self.reader, self.writer = reader, writer

    async def send(self, msg: dict) -> None:
        self.writer.write(json.dumps(msg, separators=(",", ":"), ensure_ascii=False) + "\n")
        await self.writer.drain()

    async def recv(self) -> dict:
        try:
            line = await asyncio.wait_for(self.reader.readline(), READ_TIMEOUT)
        except asyncio.TimeoutError:
            raise ProtocolError("peer stopped answering") from None
        if not line:
            raise ProtocolError("link closed")
        if len(line) > MAX_LINE:
            raise ProtocolError("line too long")
        try:
            msg = json.loads(line)
        except ValueError:
            raise ProtocolError("not JSON") from None
        if not isinstance(msg, dict) or not isinstance(msg.get("t"), str):
            raise ProtocolError("bad message")
        return msg


async def hello(ch: Channel, state: State, expect: str, first: bool) -> None:
    mine = {"t": "hello", "v": VERSION, "node": state.node_id}
    if first:
        await ch.send(mine)
    theirs = await ch.recv()
    if theirs.get("t") != "hello" or theirs.get("v") != VERSION:
        raise ProtocolError("bad hello / protocol version")
    if str(theirs.get("node", "")).upper() != expect:
        raise ProtocolError(f"peer says it is {theirs.get('node')!r}, expected {expect}")
    if not first:
        await ch.send(mine)


async def push(ch: Channel, state: State, peer: str) -> int:
    """Send this node's queue for `peer`, then 'done'. Returns envelopes accepted."""
    sent = 0
    while sent < MAX_PER_SESSION:
        box = await state.api.call("GET", f"/v1/sync/outbox/{peer}", params={"limit": BATCH})
        if not box["items"]:
            break
        acked: list[int] = []
        failed: list[dict] = []
        try:
            for item in box["items"]:
                qid = item["qid"]
                await ch.send({"t": "env", "qid": qid, "env": item["envelope"]})
                r = await ch.recv()
                if r.get("qid") != qid or r["t"] not in ("ack", "nack"):
                    raise ProtocolError("ack out of order")
                if r["t"] == "ack":
                    acked.append(qid)
                else:
                    failed.append({"qid": qid, "error": str(r.get("error", "peer error"))[:200]})
        finally:
            if acked or failed:
                await state.api.call("POST", f"/v1/sync/outbox/{peer}/results", json={"acked": acked, "failed": failed})
        sent += len(acked)
        if failed:
            break  # the peer is struggling; the rest waits for the next session
    await ch.send({"t": "done"})
    return sent


async def receive(ch: Channel, state: State, peer: str) -> int:
    """Accept envelopes from `peer` until it says 'done'. Returns envelopes taken."""
    n = 0
    while True:
        m = await ch.recv()
        if m["t"] == "done":
            return n
        if m["t"] != "env" or not isinstance(m.get("qid"), int):
            raise ProtocolError(f"unexpected {m['t']!r}")
        try:
            r = await state.api.call("POST", f"/v1/sync/inbox/{peer}", json=m.get("env"))
            await ch.send({"t": "ack", "qid": m["qid"], "status": r.get("status")})
            n += 1
        except ApiError as exc:
            if exc.status == 422:   # not even an object: final, don't let it be resent forever
                await ch.send({"t": "ack", "qid": m["qid"], "status": "rejected"})
            else:                   # core busy / rate limited / down: the sender retries later
                await ch.send({"t": "nack", "qid": m["qid"], "error": exc.code})


async def report(state: State, peer: str, status: str, error: str | None = None) -> None:
    try:
        await state.api.call("POST", f"/v1/sync/peers/{peer}/status", json={"state": status, "error": error})
    except ApiError:
        pass


# --- dialling out ----------------------------------------------------------------------


async def dial(state: State, peer: Peer) -> tuple[int, int]:
    assert peer.address
    async with asyncssh.connect(
        peer.address, peer.port, username=state.node_id, client_keys=[state.node_key],
        known_hosts=([peer.key], [], []), server_host_key_algs=["ssh-ed25519"],
        agent_path=None, password=None, kbdint_auth=False, gss_host=None,
        connect_timeout=20, login_timeout=30, keepalive_interval=30, keepalive_count_max=3,
    ) as conn:
        proc = await conn.create_process(subsystem=PROTO, encoding="utf-8")
        ch = Channel(proc.stdout, proc.stdin)
        await hello(ch, state, peer.id, first=True)
        sent = await push(ch, state, peer.id)
        await ch.send({"t": "pull"})
        got = await receive(ch, state, peer.id)
        await ch.send({"t": "bye"})
        proc.stdin.write_eof()
    return sent, got


async def peer_loop(state: State, peer_id: str) -> None:
    next_due = 0.0          # time of the next regular sync
    hold_until = 0.0        # after a failure, don't redial before this
    backoff = 0
    while True:
        peer = state.peers.get(peer_id)
        if peer is None or not peer.address:
            return
        now = time.monotonic()
        go = now >= next_due
        if not go and now >= hold_until:
            try:
                box = await state.api.call("GET", f"/v1/sync/outbox/{peer_id}", params={"limit": 1})
                go = bool(box["items"])
            except ApiError:
                go = False
        if go and now >= hold_until:
            try:
                sent, got = await dial(state, peer)
                backoff = 0
                next_due = time.monotonic() + state.interval
                await report(state, peer_id, "CONNECTED")
                if sent or got:
                    event("sync_done", peer=peer_id, direction="out", sent=sent, received=got)
            except (OSError, asyncssh.Error, asyncio.TimeoutError, ProtocolError, ApiError) as exc:
                backoff = min(900, max(30, backoff * 2))
                hold_until = next_due = time.monotonic() + backoff * random.uniform(0.8, 1.2)
                refused = isinstance(exc, (asyncssh.PermissionDenied, asyncssh.HostKeyNotVerifiable))
                why = f"{type(exc).__name__}: {exc}"[:200]
                await report(state, peer_id, "REFUSED" if refused else "UNREACHABLE", why)
                event("sync_failed", logging.WARNING, peer=peer_id, error=why, retry_in_s=backoff)
        await asyncio.sleep(state.poll)


# --- listening -------------------------------------------------------------------------


class SyncServer(asyncssh.SSHServer):
    def __init__(self, state: State) -> None:
        self.state = state
        self.ip = "-"
        self.counted = False
        self.over = False

    def connection_made(self, conn: asyncssh.SSHServerConnection) -> None:
        peer = conn.get_extra_info("peername")
        self.ip = peer[0] if peer else "-"
        if self.state.inbound >= self.state.max_inbound:
            self.over = True
        else:
            self.state.inbound += 1
            self.counted = True

    def connection_lost(self, exc: Exception | None) -> None:
        if self.counted:
            self.state.inbound -= 1
            self.counted = False

    def begin_auth(self, username: str) -> bool:
        return True

    def password_auth_supported(self) -> bool:
        return False

    def kbdint_auth_supported(self) -> bool:
        return False

    def host_based_auth_supported(self) -> bool:
        return False

    def public_key_auth_supported(self) -> bool:
        return True

    def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
        peer = self.state.peers.get(username.upper())
        offered = key.export_public_key("openssh").decode().split()[1]
        ok = not self.over and peer is not None and offered == peer.key_line
        if not ok:
            event("link_refused", logging.WARNING, client_ip=self.ip, claimed=username[:40],
                  why="connection limit" if self.over else "not a peer, or wrong key")
        return ok


async def serve_session(state: State, process: asyncssh.SSHServerProcess) -> None:
    peer = str(process.get_extra_info("username") or "").upper()
    ip = (process.get_extra_info("peername") or ["-"])[0]
    try:
        if process.subsystem != PROTO or peer not in state.peers:
            process.stdout.write("this port only speaks the BBS node sync protocol\n")
            return
        ch = Channel(process.stdin, process.stdout)
        await hello(ch, state, peer, first=False)
        got = await receive(ch, state, peer)
        m = await ch.recv()
        sent = 0
        if m["t"] == "pull":
            sent = await push(ch, state, peer)
            m = await ch.recv()
        if m["t"] != "bye":
            raise ProtocolError(f"unexpected {m['t']!r}")
        await report(state, peer, "CONNECTED")
        if sent or got:
            event("sync_done", peer=peer, direction="in", sent=sent, received=got, client_ip=ip)
    except (ProtocolError, ApiError, asyncssh.Error, OSError) as exc:
        event("sync_session_error", logging.WARNING, peer=peer, client_ip=ip, error=f"{type(exc).__name__}: {exc}"[:200])
    finally:
        try:
            process.exit(0)
        except Exception:
            pass


async def start_server(state: State, host: str, port: int) -> asyncssh.SSHAcceptor:
    return await asyncssh.create_server(
        lambda: SyncServer(state), host, port,
        server_host_keys=[state.node_key],
        process_factory=lambda p: serve_session(state, p),
        encoding="utf-8", line_editor=False, allow_pty=False, allow_scp=False, sftp_factory=None,
        agent_forwarding=False, x11_forwarding=False, login_timeout=30,
        keepalive_interval=30, keepalive_count_max=3, server_version="ScotlandBBS-sync",
    )


async def supervise(state: State, refresh_s: int = 60) -> None:
    """Keep the peer list fresh and one dialler task running per reachable peer."""
    tasks: dict[str, asyncio.Task] = {}
    while True:
        try:
            await state.refresh()
        except (ApiError, KeyError, ValueError, asyncssh.Error) as exc:
            event("config_refresh_failed", logging.WARNING, error=str(exc)[:200])
        for pid, task in list(tasks.items()):
            if task.done() or pid not in state.peers or not state.peers[pid].address:
                task.cancel()
                del tasks[pid]
        for pid, p in state.peers.items():
            if p.address and pid not in tasks:
                tasks[pid] = asyncio.create_task(peer_loop(state, pid), name=f"peer-{pid}")
        await asyncio.sleep(refresh_s)


def main() -> None:
    from bbs_gateway.logs import setup

    setup("bbs-sync", os.environ.get("BBS_LOG_LEVEL", "INFO"))

    def need(var: str) -> str:
        v = os.environ.get(var)
        if not v:
            raise SystemExit(f"CONFIGURATION ERROR: {var} is not set")
        return v

    token = Path(need("BBS_GATEWAY_TOKEN_FILE")).read_text().strip()
    if len(token) < 32:
        raise SystemExit("CONFIGURATION ERROR: sync token is shorter than 32 characters")
    key = asyncssh.read_private_key(need("BBS_NODE_KEY_FILE"))
    if key.algorithm != b"ssh-ed25519":
        raise SystemExit("CONFIGURATION ERROR: the node key must be ed25519")
    port = int(os.environ.get("BBS_LISTEN_PORT", "2224"))

    async def run() -> None:
        state = State(api=CoreAPI(os.environ.get("BBS_CORE_URL", "http://bbs-core:8080"), token), node_key=key,
                      interval=int(os.environ.get("BBS_SYNC_INTERVAL", "300")))
        for attempt in range(60):
            try:
                await state.refresh()
                break
            except ApiError as exc:
                event("waiting_for_core", logging.WARNING, error=exc.code, attempt=attempt)
                await asyncio.sleep(5)
        await start_server(state, "0.0.0.0", port)
        event("sync_listening", port=port, node=state.node_id, peers=sorted(state.peers))
        await supervise(state)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
