"""Two real nodes linked by bbs-sync over SSH: both directions, pinned keys, impostors refused."""

import asyncio

import asyncssh
import pytest

from conftest import SYNC_TOKEN, LiveCore, free_port, gen_key
from bbs_gateway.client import CoreAPI
from bbs_sync import link

pytestmark = pytest.mark.asyncio


def count(node, board="GENERAL") -> int:
    with node.bbs.db.session() as conn:
        return conn.execute("SELECT COUNT(*) FROM messages m JOIN boards b ON b.id = m.board_id WHERE b.name = ?",
                            (board,)).fetchone()[0]


def post(node, user, subject):
    api, s = user
    r = api.call("POST", "/v1/messages", s, json={"board": "GENERAL", "subject": subject, "body": subject + " body"})
    assert r.status_code == 200, r.text


@pytest.fixture
async def linked(mesh):
    """NODE-A (behind NAT: no address for it on B) dials NODE-B, which listens."""
    n = mesh("NODE-A", "NODE-B")
    a, b = n["NODE-A"], n["NODE-B"]
    port_b = free_port()
    a.link(b, port=port_b)
    b.link(a, address=None)
    a.build()
    b.build()
    cores = [LiveCore(a.bbs).start(), LiveCore(b.bbs).start()]
    sa = link.State(api=CoreAPI(cores[0].url, SYNC_TOKEN), node_key=a.key)
    sb = link.State(api=CoreAPI(cores[1].url, SYNC_TOKEN), node_key=b.key)
    await sa.refresh()
    await sb.refresh()
    server = await link.start_server(sb, "127.0.0.1", port_b)
    yield a, b, sa, sb, port_b
    server.close()
    await sa.api.close()
    await sb.api.close()
    for c in cores:
        c.stop()


async def test_both_directions_over_one_dial(linked):
    a, b, sa, sb, _ = linked
    post(a, a.user("alice"), "From A")
    post(b, b.user("bob"), "From B")
    sent, got = await link.dial(sa, sa.peers["NODE-B"])
    assert (sent, got) == (1, 1)
    assert count(a) == 2 and count(b) == 2
    # a second sync moves nothing (acked, and dedup would catch it anyway)
    assert await link.dial(sa, sa.peers["NODE-B"]) == (0, 0)
    with b.bbs.db.session() as conn:
        assert conn.execute("SELECT status FROM nodes WHERE node_id = 'NODE-A'").fetchone()[0] == "CONNECTED"


async def test_wrong_host_key_is_refused(linked):
    a, b, sa, sb, port = linked
    impostor = link.Peer("NODE-B", "B", "127.0.0.1", port, gen_key())  # A expects a different key for B
    with pytest.raises(asyncssh.Error):
        await link.dial(sa, impostor)


async def test_unknown_node_key_is_refused(linked):
    a, b, sa, sb, port = linked
    stranger = link.State(api=sa.api, node_key=gen_key(), node_id="NODE-A")
    with pytest.raises(asyncssh.PermissionDenied):
        await link.dial(stranger, sa.peers["NODE-B"])


async def test_shell_and_other_subsystems_refused(linked):
    a, b, sa, sb, port = linked
    async with asyncssh.connect("127.0.0.1", port, username="NODE-A", client_keys=[a.key], known_hosts=None,
                                agent_path=None) as conn:
        r = await conn.run("id", check=False)
        assert "uid=" not in (r.stdout or "") and "sync protocol" in r.stdout
        with pytest.raises(asyncssh.Error):
            await conn.start_sftp_client()


async def test_peer_loop_retries_and_delivers(linked, monkeypatch):
    a, b, sa, sb, _ = linked
    sa.poll = 0.05
    post(a, a.user("alice"), "Queued")
    task = asyncio.create_task(link.peer_loop(sa, "NODE-B"))
    try:
        for _ in range(100):
            if count(b) == 1:
                break
            await asyncio.sleep(0.05)
        assert count(b) == 1
    finally:
        task.cancel()
